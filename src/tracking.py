"""
Engagement tracking server for BrokenSite-Weekly.
FastAPI app that serves audit pages and tracks opens, page views, CTA clicks,
and unsubscribes.
"""

import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import OUTPUT_DIR, load_config
from .db import Database
from .gumroad import get_subscribers_with_isolation
from .delivery import send_email
from .portal_auth import verify_portal_token
from .logging_setup import get_logger

logger = get_logger("tracking")

app = FastAPI(title="BrokenSite Tracking", docs_url=None, redoc_url=None)

AUDITS_DIR = OUTPUT_DIR / "audits"

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
    """Record a tracking event to the database."""
    try:
        db = _get_db()
        ip_address = request.client.host if request.client else None
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


def _render_portal_page(email: str, exports: list[dict], warm_export: dict | None, token: str) -> str:
    items = []
    for exp in exports:
        csv_path = exp.get("csv_path") or ""
        filename = Path(csv_path).name if csv_path else ""
        link = f"/portal/download/{filename}?token={token}" if filename else ""
        items.append(f"""
        <tr>
          <td>{exp.get('sent_at','')}</td>
          <td>{exp.get('export_type','')}</td>
          <td>{exp.get('tier','')}</td>
          <td>{exp.get('lead_count','')}</td>
          <td><a href="{link}">{filename or 'n/a'}</a></td>
        </tr>
        """)
    warm_count = warm_export.get("lead_count") if warm_export else 0
    return f"""<!DOCTYPE html>
<html>
<head>
  <title>BrokenSite Weekly Portal</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; background: #f7fafc; color: #2d3748; padding: 40px; }}
    .card {{ background: white; border-radius: 12px; padding: 24px; box-shadow: 0 4px 12px rgba(0,0,0,0.06); }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
    th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #e2e8f0; }}
    th {{ font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: #718096; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Subscriber Portal</h1>
    <p>Signed in as <strong>{email}</strong></p>
    <p>Most recent warm lead batch: <strong>{warm_count}</strong> leads</p>
    <h2>Recent Exports</h2>
    <table>
      <thead>
        <tr><th>Date</th><th>Type</th><th>Tier</th><th>Leads</th><th>Download</th></tr>
      </thead>
      <tbody>
        {''.join(items) if items else '<tr><td colspan="5">No exports yet</td></tr>'}
      </tbody>
    </table>
  </div>
</body>
</html>"""


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

    return HTMLResponse(
        content="<h1>Report not found</h1><p>This report is no longer available.</p>",
        status_code=404,
    )


@app.get("/track/{place_id}/cta")
async def track_cta_form(place_id: str, request: Request):
    """Show CTA form and track view."""
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html>
<head><title>Get Help</title>
<style>
body {{ font-family: -apple-system, sans-serif; display: flex; justify-content: center;
       align-items: center; min-height: 100vh; background: #f7fafc; color: #2d3748; }}
.card {{ background: white; padding: 32px; border-radius: 12px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.07); max-width: 520px; width: 100%; }}
label {{ display: block; margin-top: 12px; font-weight: 600; }}
input, textarea {{ width: 100%; padding: 10px; margin-top: 6px; border: 1px solid #e2e8f0; border-radius: 6px; }}
button {{ margin-top: 16px; background: #667eea; color: white; border: none; padding: 12px 20px; border-radius: 6px; font-weight: 600; }}
</style>
</head>
<body>
<div class="card">
  <h1>Let’s Fix These Issues</h1>
  <p>Leave your details and we’ll reach out with next steps.</p>
  <form method="post" action="/track/{place_id}/cta">
    <label>Name</label>
    <input name="name" type="text" required />
    <label>Email</label>
    <input name="email" type="email" required />
    <label>Phone</label>
    <input name="phone" type="text" />
    <label>Notes</label>
    <textarea name="notes" rows="4"></textarea>
    <button type="submit">Request Help</button>
  </form>
</div>
</body>
</html>""",
        status_code=200,
    )


@app.post("/track/{place_id}/cta")
async def track_cta_submit(
    place_id: str,
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
):
    """Handle CTA form submission."""
    _record_event(place_id, "cta_click", request)
    try:
        db = _get_db()
        db.record_lead_inquiry(place_id, name, email, phone, notes)
        _send_inquiry_notifications(place_id, name, email, phone, notes)
    except Exception as e:
        logger.error(f"Failed to record CTA inquiry: {e}")

    return HTMLResponse(
        content="""<!DOCTYPE html>
<html>
<head><title>Thank You</title>
<style>
body { font-family: -apple-system, sans-serif; display: flex; justify-content: center;
       align-items: center; min-height: 100vh; background: #f7fafc; color: #2d3748; }
.card { background: white; padding: 40px; border-radius: 12px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.07); text-align: center; max-width: 500px; }
h1 { color: #667eea; margin-bottom: 20px; }
</style>
</head>
<body>
<div class="card">
<h1>Thank You!</h1>
<p>We received your request. A web professional will be in touch shortly.</p>
<p style="margin-top:20px;color:#718096;font-size:14px;">
You can close this page.</p>
</div>
</body>
</html>""",
        status_code=200,
    )


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
        logger.info(f"Unsubscribed {place_id}")
    except Exception as e:
        logger.error(f"Error processing unsubscribe for {place_id}: {e}")

    return HTMLResponse(
        content="""<!DOCTYPE html>
<html>
<head><title>Unsubscribed</title>
<style>
body { font-family: -apple-system, sans-serif; display: flex; justify-content: center;
       align-items: center; min-height: 100vh; background: #f7fafc; color: #2d3748; }
.card { background: white; padding: 40px; border-radius: 12px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.07); text-align: center; max-width: 500px; }
h1 { margin-bottom: 20px; }
</style>
</head>
<body>
<div class="card">
<h1>Unsubscribed</h1>
<p>You've been removed from our list. We won't contact you again.</p>
</div>
</body>
</html>""",
        status_code=200,
    )


@app.get("/portal")
async def portal(token: str = ""):
    """Subscriber portal with token auth."""
    config = load_config()
    if not config.portal.secret:
        return HTMLResponse(content="Portal not configured.", status_code=403)
    verified = verify_portal_token(token, config.portal.secret)
    if not verified:
        return HTMLResponse(content="Invalid or expired token.", status_code=403)

    email, _ = verified
    db = _get_db()
    exports = db.get_recent_exports(email, limit=4)
    warm_export = db.get_latest_warm_export(email)
    html = _render_portal_page(email, exports, warm_export, token)
    return HTMLResponse(content=html, status_code=200)


@app.get("/portal/download/{filename}")
async def portal_download(filename: str, token: str = ""):
    """Download export CSV with token auth."""
    config = load_config()
    if not config.portal.secret:
        return HTMLResponse(content="Portal not configured.", status_code=403)
    verified = verify_portal_token(token, config.portal.secret)
    if not verified:
        return HTMLResponse(content="Invalid or expired token.", status_code=403)

    requested = (OUTPUT_DIR / filename).resolve()
    if not str(requested).startswith(str(OUTPUT_DIR.resolve())):
        return HTMLResponse(content="Invalid file path.", status_code=400)
    if not requested.exists():
        return HTMLResponse(content="File not found.", status_code=404)
    return FileResponse(requested, media_type="text/csv")


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
