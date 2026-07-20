"""
Engagement tracking server for BrokenSite-Weekly.
FastAPI app that serves audit pages and tracks opens, page views, CTA clicks,
and unsubscribes.
"""

import hmac
import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape

from .config import OUTPUT_DIR, PROJECT_ROOT, load_config
from .db import Database
from .gumroad import get_subscribers_with_isolation
from .delivery import send_email
from .portal_auth import verify_portal_token
from .rate_limit import SlidingWindowLimiter
from .logging_setup import get_logger

logger = get_logger("tracking")

app = FastAPI(title="BrokenSite Tracking", docs_url=None, redoc_url=None)

AUDITS_DIR = OUTPUT_DIR / "audits"
TEMPLATES_DIR = PROJECT_ROOT / "templates"

# Engagement events (pixel opens, audit page views, CTA clicks) are
# unauthenticated and directly feed get_engagement_score()/get_warm_leads(),
# so a naive refresh-spam script could otherwise inflate a lead into "warm"
# tier. Dedupe repeats of the same event from the same IP within this window.
ENGAGEMENT_EVENT_DEDUPE_WINDOW_SECONDS = 60
_engagement_event_limiter = SlidingWindowLimiter()

# The CTA POST endpoint has higher amplification cost than plain tracking: it
# writes a full inquiry record and emails every pro subscriber, so it gets
# its own tighter, IP-scoped limit independent of the engagement dedupe above.
CTA_SUBMIT_MAX_PER_WINDOW = 5
CTA_SUBMIT_WINDOW_SECONDS = 3600
CTA_FIELD_MAX_LENGTH = 200
CTA_NOTES_MAX_LENGTH = 2000
_cta_submit_limiter = SlidingWindowLimiter()

# Jinja2 template environment
_jinja_env = None


def _get_jinja_env() -> Environment:
    """Get or create Jinja2 environment."""
    global _jinja_env
    if _jinja_env is None:
        _jinja_env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
        )
    return _jinja_env


def _render_template(name: str, **context) -> str:
    """Render a Jinja2 template by name."""
    try:
        template = _get_jinja_env().get_template(name)
        return template.render(**context)
    except TemplateNotFound:
        logger.error(f"Template not found: {name}")
        raise


def _render_error_page(title: str, message: str, from_email: str, status_code: int = 400) -> HTMLResponse:
    """Return a branded error page."""
    try:
        html = _render_template("error.html", title=title, message=message, from_email=from_email)
    except Exception:
        html = f"<!doctype html><html><body><h1>{title}</h1><p>{message}</p></body></html>"
    return HTMLResponse(content=html, status_code=status_code)

# 1x1 transparent GIF
TRACKING_PIXEL = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff"
    b"\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,"
    b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)

# Lazy-initialized database
_db = None


def _get_db() -> Database:
    """Get or create database connection."""
    global _db
    if _db is None:
        config = load_config()
        _db = Database(config.database)
    return _db


def _record_event(place_id: str, event_type: str, request: Request):
    """Record a tracking event to the database.

    Deduplicates repeats of the same (ip, place_id, event_type) within
    ENGAGEMENT_EVENT_DEDUPE_WINDOW_SECONDS so a refresh-spam script can't
    trivially inflate engagement scores. Best-effort only — see rate_limit.py.
    """
    try:
        ip_address = request.client.host if request.client else None
        dedupe_key = f"{event_type}:{place_id}:{ip_address or 'unknown'}"
        if not _engagement_event_limiter.allow(
            dedupe_key, max_events=1, window_seconds=ENGAGEMENT_EVENT_DEDUPE_WINDOW_SECONDS
        ):
            logger.debug(f"Deduped {event_type} for {place_id} from {ip_address}")
            return

        db = _get_db()
        user_agent = request.headers.get("user-agent", "")
        db.record_event(
            place_id=place_id,
            event_type=event_type,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        logger.debug(f"Recorded {event_type} for {place_id}")
    except Exception as e:
        logger.error(f"Failed to record event {event_type} for {place_id}: {e}")


def _send_inquiry_notifications(place_id: str, name: str, email: str, phone: str, notes: str):
    """Notify Pro subscribers of a CTA inquiry submission."""
    try:
        config = load_config()
        subscribers, err = get_subscribers_with_isolation(config.gumroad, config.retry)
        if err or not subscribers:
            return
        pro_subs = [s for s in subscribers if s.tier == "pro"]
        if not pro_subs:
            return

        db = _get_db()
        lead = db.get_lead_summary(place_id)
        lead_name = lead.get("name") if lead else ""
        audit_url = lead.get("audit_url") if lead else ""

        subject = f"Warm lead inquiry: {lead_name or place_id}"
        body = f"""New CTA inquiry received.

Business: {lead_name or place_id}
Website: {lead.get('website') if lead else ''}
City/Category: {lead.get('city') if lead else ''} / {lead.get('category') if lead else ''}
Audit URL: {audit_url}

Contact Name: {name}
Contact Email: {email}
Contact Phone: {phone}
Notes: {notes}
"""
        for sub in pro_subs:
            msg = MIMEMultipart()
            msg["Subject"] = subject
            msg["From"] = f"{config.smtp.from_name} <{config.smtp.from_email}>"
            msg["To"] = sub.email
            msg.attach(MIMEText(body, "plain"))
            send_email(msg, config.smtp, config.retry)
    except Exception as e:
        logger.error(f"Failed to send inquiry notifications: {e}")


def _build_portal_exports(exports: list[dict], token: str) -> list[dict]:
    """Build export list with rendered links for the portal template."""
    result = []
    for exp in exports:
        csv_path = exp.get("csv_path") or ""
        filename = Path(csv_path).name if csv_path else ""
        link = f"/portal/download/{filename}?token={token}" if filename else ""
        result.append({
            "sent_at": exp.get("sent_at", ""),
            "export_type": exp.get("export_type", ""),
            "tier": exp.get("tier", ""),
            "lead_count": exp.get("lead_count", ""),
            "filename": filename or "n/a",
            "link": link,
        })
    return result


@app.get("/track/{place_id}/open.gif")
async def track_open(place_id: str, request: Request):
    """Track email open via tracking pixel."""
    _record_event(place_id, "email_opened", request)
    return Response(
        content=TRACKING_PIXEL,
        media_type="image/gif",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/audit/{place_id}")
async def view_audit(place_id: str, request: Request):
    """Serve audit page and track view."""
    _record_event(place_id, "page_view", request)

    audit_path = AUDITS_DIR / f"{place_id}.html"
    if audit_path.exists():
        return FileResponse(
            audit_path, media_type="text/html", headers={"Cache-Control": "no-cache"}
        )

    config = load_config()
    return _render_error_page(
        title="Report not found",
        message="This checkup report is no longer available. It may have expired or been removed.",
        from_email=config.smtp.from_email,
        status_code=404,
    )


@app.get("/track/{place_id}/cta")
async def track_cta_form(place_id: str, request: Request):
    """Show CTA form and track view."""
    db = _get_db()
    lead = db.get_lead_summary(place_id)
    business_name = lead.get("name") if lead else ""

    html = _render_template(
        "cta_form.html",
        business_name=business_name,
        action_url=f"/track/{place_id}/cta",
    )
    return HTMLResponse(content=html, status_code=200)


@app.post("/track/{place_id}/cta")
async def track_cta_submit(
    place_id: str,
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
):
    """Handle CTA form submission.

    Unauthenticated and fans out an email to every pro subscriber per
    submission, so it carries its own tighter, IP-scoped rate limit beyond
    the general engagement-event dedupe in _record_event().
    """
    _record_event(place_id, "cta_click", request)

    ip_address = request.client.host if request.client else "unknown"
    if not _cta_submit_limiter.allow(
        f"cta_submit:{ip_address}", CTA_SUBMIT_MAX_PER_WINDOW, CTA_SUBMIT_WINDOW_SECONDS
    ):
        logger.warning(f"CTA submission rate limit exceeded for {ip_address}")
        config = load_config()
        return _render_error_page(
            title="Too many requests",
            message="You've submitted this form a few times recently. Please try again later.",
            from_email=config.smtp.from_email,
            status_code=429,
        )

    try:
        db = _get_db()
        name = name[:CTA_FIELD_MAX_LENGTH]
        email = email[:CTA_FIELD_MAX_LENGTH]
        phone = phone[:CTA_FIELD_MAX_LENGTH]
        notes = notes[:CTA_NOTES_MAX_LENGTH]
        db.record_lead_inquiry(place_id, name, email, phone, notes)
        _send_inquiry_notifications(place_id, name, email, phone, notes)
    except Exception as e:
        logger.error(f"Failed to record CTA inquiry: {e}")

    html = _render_template("thank_you.html")
    return HTMLResponse(content=html, status_code=200)


@app.get("/unsubscribe/{place_id}")
async def unsubscribe(place_id: str, request: Request):
    """Handle unsubscribe requests."""
    _record_event(place_id, "unsubscribe", request)

    try:
        db = _get_db()
        # Get the email for this place_id from contacts or outreach
        contact = db.get_contact(place_id)
        email = contact["email"] if contact else ""
        db.add_unsubscribe(place_id, email)
        if email:
            # Suppress by email (not just place_id) so a business with
            # multiple Google Place IDs/locations sharing one contact email
            # can't keep receiving outreach after unsubscribing once.
            db.add_suppression(email, "unsubscribed")
        logger.info(f"Unsubscribed {place_id}")
    except Exception as e:
        logger.error(f"Error processing unsubscribe for {place_id}: {e}")

    config = load_config()
    html = _render_template("unsubscribe.html", from_email=config.smtp.from_email)
    return HTMLResponse(content=html, status_code=200)


@app.get("/portal")
async def portal(token: str = ""):
    """Subscriber portal with token auth."""
    config = load_config()
    if not config.portal.secret:
        return _render_error_page(
            title="Portal not configured",
            message="The subscriber portal is not set up yet. Please contact support.",
            from_email=config.smtp.from_email,
            status_code=403,
        )
    verified = verify_portal_token(token, config.portal.secret)
    if not verified:
        return _render_error_page(
            title="Link expired",
            message="This portal link is invalid or has expired. Please request a new one.",
            from_email=config.smtp.from_email,
            status_code=403,
        )

    email, _ = verified
    db = _get_db()
    exports = db.get_recent_exports(email, limit=4)
    warm_export = db.get_latest_warm_export(email)
    warm_count = warm_export.get("lead_count") if warm_export else None
    export_items = _build_portal_exports(exports, token)
    html = _render_template(
        "portal.html",
        email=email,
        exports=export_items,
        warm_count=warm_count,
    )
    return HTMLResponse(content=html, status_code=200)


@app.get("/portal/download/{filename}")
async def portal_download(filename: str, token: str = ""):
    """Download export CSV with token auth."""
    config = load_config()
    if not config.portal.secret:
        return _render_error_page(
            title="Portal not configured",
            message="The subscriber portal is not set up yet. Please contact support.",
            from_email=config.smtp.from_email,
            status_code=403,
        )
    verified = verify_portal_token(token, config.portal.secret)
    if not verified:
        return _render_error_page(
            title="Link expired",
            message="This portal link is invalid or has expired. Please request a new one.",
            from_email=config.smtp.from_email,
            status_code=403,
        )

    email, _ = verified
    output_dir = OUTPUT_DIR.resolve()
    requested = (output_dir / filename).resolve()
    try:
        requested.relative_to(output_dir)
    except ValueError:
        return _render_error_page(
            title="Invalid request",
            message="The file path you requested is not allowed.",
            from_email=config.smtp.from_email,
            status_code=400,
        )
    exports = _get_db().get_recent_exports(email, limit=50)
    allowed_paths = {
        Path(export["csv_path"]).resolve()
        for export in exports
        if export.get("csv_path")
    }
    if requested not in allowed_paths:
        return _render_error_page(
            title="File not found",
            message="The file you requested is not available for this account.",
            from_email=config.smtp.from_email,
            status_code=404,
        )
    if not requested.exists():
        return _render_error_page(
            title="File not found",
            message="The file you requested no longer exists or may have been moved.",
            from_email=config.smtp.from_email,
            status_code=404,
        )
    return FileResponse(requested, media_type="text/csv")


@app.get("/")
@app.get("/dashboard")
async def dashboard(request: Request, token: str = ""):
    """Operator dashboard showing run status, leads, and log tail.

    Gated behind DASHBOARD_TOKEN. The dashboard exposes the lead database and
    a tail of the server log, so it must never be reachable without the shared
    secret. When the token is unset the dashboard is disabled entirely.
    """
    config = load_config()
    expected = config.portal.dashboard_token
    if not expected:
        return _render_error_page(
            title="Dashboard disabled",
            message="The operator dashboard is not enabled. Set DASHBOARD_TOKEN to use it.",
            from_email=config.smtp.from_email,
            status_code=403,
        )
    if not token or not hmac.compare_digest(token, expected):
        return _render_error_page(
            title="Not authorized",
            message="A valid dashboard token is required.",
            from_email=config.smtp.from_email,
            status_code=403,
        )

    db = _get_db()

    # Key stats
    stats = db.get_stats()
    total_leads = stats.get("total_leads", 0)
    runs_count = stats.get("total_runs", 0)

    # Current run + qualifying leads
    current_run = None
    qualifying = 0
    recent_leads = []
    top_signals = []
    try:
        with db._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM leads WHERE score >= 40").fetchone()
            qualifying = row[0] if row else 0

            rows = conn.execute(
                "SELECT name, website, city, category, score, reasons, lead_tier "
                "FROM leads ORDER BY last_seen DESC LIMIT 20"
            ).fetchall()
            recent_leads = [dict(r) for r in rows]

            all_reasons = conn.execute(
                "SELECT reasons FROM leads WHERE reasons IS NOT NULL AND reasons != ''"
            ).fetchall()
            from collections import Counter
            counter = Counter()
            for (reasons_str,) in all_reasons:
                for r in reasons_str.split(","):
                    r = r.strip()
                    if r and not r.startswith("broken_image_") and not r.startswith("dead_social_link_"):
                        counter[r] += 1
            top_signals = counter.most_common(10)

            current_row = conn.execute(
                "SELECT run_id, started_at, status, queries_attempted, businesses_found "
                "FROM runs WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if current_row:
                current_run = dict(current_row)
    except Exception:
        pass

    # Run status
    last_run = stats.get("last_run", {})
    run_status = last_run.get("status", "idle") if last_run else "idle"
    if current_run:
        run_status = "running"

    # Last completed duration
    last_completed = stats.get("last_completed_run")
    last_run_duration = "—"
    if last_completed:
        completed_at = last_completed.get("completed_at")
        started_at = last_completed.get("started_at")
        if completed_at and started_at and isinstance(started_at, str) and isinstance(completed_at, str):
            try:
                from datetime import datetime as dt
                start = dt.fromisoformat(started_at)
                end = dt.fromisoformat(completed_at)
                seconds = int((end - start).total_seconds())
                if seconds >= 3600:
                    last_run_duration = f"{seconds // 3600}h {(seconds % 3600) // 60}m"
                elif seconds >= 60:
                    last_run_duration = f"{seconds // 60}m {seconds % 60}s"
                else:
                    last_run_duration = f"{seconds}s"
            except Exception:
                pass

    # Log tail
    log_tail = ""
    try:
        log_path = Path("/opt/brokensite-weekly/logs/brokensite-weekly.log")
        if log_path.exists():
            with open(log_path, "r") as f:
                import os as _os
                f.seek(0, _os.SEEK_END)
                size = f.tell()
                if size > 10000:
                    f.seek(size - 10000)
                else:
                    f.seek(0)
                f.readline()  # skip partial first line
                lines = f.readlines()
                log_tail = "".join(lines[-30:])
    except Exception:
        log_tail = "(could not read log)"

    now = datetime.utcnow().strftime("%H:%M:%S UTC")

    html = _render_template(
        "dashboard.html",
        now=now,
        run_status=run_status,
        total_leads=total_leads,
        qualifying_leads=qualifying,
        runs_count=runs_count,
        last_run_duration=last_run_duration,
        current_run=current_run,
        last_completed=last_completed,
        recent_leads=recent_leads,
        top_signals=top_signals,
        log_tail=log_tail,
    )
    return HTMLResponse(content=html, status_code=200)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
