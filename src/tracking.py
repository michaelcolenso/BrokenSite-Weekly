"""
Engagement tracking server for BrokenSite-Weekly.
FastAPI app that serves audit pages and tracks opens, page views, CTA clicks,
and unsubscribes.
"""

import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from .config import OUTPUT_DIR, load_config
from .db import Database
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
async def track_cta(place_id: str, request: Request):
    """Track CTA click and show thank-you page."""
    _record_event(place_id, "cta_click", request)

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
<p>We've noted your interest. A web professional will be in touch to discuss
fixing these issues for your business.</p>
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


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
