"""
Outreach email system for BrokenSite-Weekly.
Sends personalized audit emails to businesses with broken websites.
"""

import smtplib
import time
import random
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional

from .audit_generator import ISSUE_DESCRIPTIONS
from .config import OutreachConfig, SMTPConfig
from .contact_finder import ContactInfo
from .logging_setup import get_logger

logger = get_logger("outreach")

EMAIL_TEMPLATE_PLAIN = """Hi,

I was researching {category} businesses in {city} and came across {business_name}.

I noticed a few issues with your website that might be costing you customers:

{issue_summary}

I put together a free report with more details:
{audit_url}

No sales pitch here - just thought you'd want to know. Many local businesses aren't aware of these issues until a potential customer mentions it.

Best,
{company_name}

---
You're receiving this because your business is listed on Google Maps.
Don't want to hear from us? Click here: {unsubscribe_url}

{physical_address}
"""

EMAIL_TEMPLATE_HTML = """<html>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #2d3748; line-height: 1.6; max-width: 600px; margin: 0 auto; padding: 20px;">
{tracking_pixel}
<p>Hi,</p>

<p>I was researching <strong>{category}</strong> businesses in <strong>{city}</strong> and came across <strong>{business_name}</strong>.</p>

<p>I noticed a few issues with your website that might be costing you customers:</p>

{issue_summary_html}

<p>I put together a free report with more details:</p>
<p><a href="{audit_url}" style="display: inline-block; background: #667eea; color: white; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: 600;">View Your Free Report</a></p>

<p>No sales pitch here &mdash; just thought you'd want to know. Many local businesses aren't aware of these issues until a potential customer mentions it.</p>

<p>Best,<br>{company_name}</p>

<hr style="border: none; border-top: 1px solid #e2e8f0; margin: 30px 0;">
<p style="font-size: 12px; color: #718096;">
You're receiving this because your business is listed on Google Maps.<br>
<a href="{unsubscribe_url}" style="color: #667eea;">Don't want to hear from us? Click here.</a><br>
{physical_address}
</p>
</body>
</html>"""

FOLLOWUP_TEMPLATE_PLAIN = """Hi,

Just a quick follow-up on the website issues I flagged for {business_name}.

If you'd like the report again, here it is:
{audit_url}

If you'd prefer not to receive these, you can unsubscribe here:
{unsubscribe_url}

Best,
{company_name}

{physical_address}
"""

FOLLOWUP_TEMPLATE_HTML = """<html>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #2d3748; line-height: 1.6; max-width: 600px; margin: 0 auto; padding: 20px;">
{tracking_pixel}
<p>Hi,</p>

<p>Just a quick follow-up on the website issues I flagged for <strong>{business_name}</strong>.</p>

<p>If you'd like the report again, here it is:</p>
<p><a href="{audit_url}" style="display: inline-block; background: #667eea; color: white; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-weight: 600;">View Report</a></p>

<p>If you'd prefer not to receive these, you can unsubscribe here:<br>
<a href="{unsubscribe_url}" style="color: #667eea;">Unsubscribe</a></p>

<p>Best,<br>{company_name}</p>
<p style="font-size: 12px; color: #718096;">{physical_address}</p>
</body>
</html>"""


@dataclass
class OutreachResult:
    """Result of an outreach attempt."""

    place_id: str
    email: str
    success: bool
    error: Optional[str] = None
    sent_at: Optional[datetime] = None
    permanent_failure: bool = False


def _format_issue_summary(reasons: str, max_issues: int = 3) -> str:
    """Format top issues as plain-text bullet points."""
    if not reasons:
        return ""
    summaries = []
    for reason in reasons.split(",")[:max_issues]:
        reason = reason.strip()
        desc = ISSUE_DESCRIPTIONS.get(reason)
        if desc:
            summaries.append(f"  - {desc['title']}: {desc['description'][:100]}...")
    return "\n".join(summaries) if summaries else "  - Multiple website issues detected"


def _format_issue_summary_html(reasons: str, max_issues: int = 3) -> str:
    """Format top issues as HTML list."""
    if not reasons:
        return "<ul><li>Multiple website issues detected</li></ul>"
    items = []
    for reason in reasons.split(",")[:max_issues]:
        reason = reason.strip()
        desc = ISSUE_DESCRIPTIONS.get(reason)
        if desc:
            items.append(
                f'<li><strong>{desc["title"]}</strong>: {desc["description"][:120]}...</li>'
            )
    if not items:
        items.append("<li>Multiple website issues detected</li>")
    return "<ul>" + "".join(items) + "</ul>"


def _within_sending_window(config: OutreachConfig) -> bool:
    """Check if current local time is within configured send window."""
    start = config.send_start_hour
    end = config.send_end_hour
    now_hour = datetime.now().hour
    if start == end:
        return True
    if start < end:
        return start <= now_hour < end
    # Overnight window (e.g., 20 -> 6)
    return now_hour >= start or now_hour < end


def _is_permanent_smtp_error(err: Exception) -> bool:
    if isinstance(err, smtplib.SMTPRecipientsRefused):
        return True
    if isinstance(err, smtplib.SMTPDataError):
        try:
            code = err.smtp_code
            return int(code) >= 500
        except Exception:
            return False
    return False


def send_audit_email(
    lead: Dict,
    contact: ContactInfo,
    audit_url: str,
    smtp_config: SMTPConfig,
    outreach_config: OutreachConfig,
) -> OutreachResult:
    """
    Send personalized audit email to a business.

    Returns OutreachResult with success/failure info. Never raises.
    """
    try:
        business_name = lead.get("name", "your business")
        category = lead.get("category", "local")
        city = lead.get("city", "your area")
        reasons = lead.get("reasons", "")
        place_id = lead.get("place_id", "")

        unsubscribe_url = f"{outreach_config.tracking_base_url}/unsubscribe/{place_id}"
        tracking_pixel = (
            f'<img src="{outreach_config.tracking_base_url}/track/{place_id}/open.gif" '
            f'width="1" height="1" style="display:none" alt=""/>'
        )

        # Build plain text body
        plain_body = EMAIL_TEMPLATE_PLAIN.format(
            business_name=business_name,
            category=category,
            city=city,
            issue_summary=_format_issue_summary(reasons),
            audit_url=audit_url,
            unsubscribe_url=unsubscribe_url,
            company_name=outreach_config.company_name,
            physical_address=outreach_config.physical_address,
        )

        # Build HTML body
        html_body = EMAIL_TEMPLATE_HTML.format(
            business_name=business_name,
            category=category,
            city=city,
            issue_summary_html=_format_issue_summary_html(reasons),
            audit_url=audit_url,
            unsubscribe_url=unsubscribe_url,
            company_name=outreach_config.company_name,
            physical_address=outreach_config.physical_address,
            tracking_pixel=tracking_pixel,
        )

        # Compose MIME message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Found some issues with {business_name}'s website"
        msg["From"] = f"{smtp_config.from_name} <{smtp_config.from_email}>"
        msg["To"] = contact.email
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"

        msg.attach(MIMEText(plain_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        # Send
        with smtplib.SMTP(smtp_config.host, smtp_config.port) as server:
            if smtp_config.use_tls:
                server.starttls()
            server.login(smtp_config.username, smtp_config.password)
            server.send_message(msg)

        logger.info(f"Sent audit email to {contact.email} for {business_name}")
        return OutreachResult(
            place_id=place_id,
            email=contact.email,
            success=True,
            sent_at=datetime.utcnow(),
        )

    except Exception as e:
        logger.error(f"Failed to send email to {contact.email}: {e}")
        permanent = _is_permanent_smtp_error(e)
        return OutreachResult(
            place_id=lead.get("place_id", ""),
            email=contact.email,
            success=False,
            error=str(e),
            permanent_failure=permanent,
        )


def send_followup_email(
    lead: Dict,
    contact: ContactInfo,
    audit_url: str,
    smtp_config: SMTPConfig,
    outreach_config: OutreachConfig,
) -> OutreachResult:
    """Send a short follow-up email."""
    try:
        business_name = lead.get("name", "your business")
        place_id = lead.get("place_id", "")
        unsubscribe_url = f"{outreach_config.tracking_base_url}/unsubscribe/{place_id}"
        tracking_pixel = (
            f'<img src="{outreach_config.tracking_base_url}/track/{place_id}/open.gif" '
            f'width="1" height="1" style="display:none" alt=""/>'
        )

        plain_body = FOLLOWUP_TEMPLATE_PLAIN.format(
            business_name=business_name,
            audit_url=audit_url,
            unsubscribe_url=unsubscribe_url,
            company_name=outreach_config.company_name,
            physical_address=outreach_config.physical_address,
        )
        html_body = FOLLOWUP_TEMPLATE_HTML.format(
            business_name=business_name,
            audit_url=audit_url,
            unsubscribe_url=unsubscribe_url,
            company_name=outreach_config.company_name,
            physical_address=outreach_config.physical_address,
            tracking_pixel=tracking_pixel,
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Quick follow-up for {business_name}"
        msg["From"] = f"{smtp_config.from_name} <{smtp_config.from_email}>"
        msg["To"] = contact.email
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"

        msg.attach(MIMEText(plain_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(smtp_config.host, smtp_config.port) as server:
            if smtp_config.use_tls:
                server.starttls()
            server.login(smtp_config.username, smtp_config.password)
            server.send_message(msg)

        logger.info(f"Sent follow-up email to {contact.email} for {business_name}")
        return OutreachResult(
            place_id=place_id,
            email=contact.email,
            success=True,
            sent_at=datetime.utcnow(),
        )

    except Exception as e:
        logger.error(f"Failed to send follow-up to {contact.email}: {e}")
        permanent = _is_permanent_smtp_error(e)
        return OutreachResult(
            place_id=lead.get("place_id", ""),
            email=contact.email,
            success=False,
            error=str(e),
            permanent_failure=permanent,
        )


def run_outreach(
    leads: List[Dict],
    db,
    smtp_config: SMTPConfig,
    outreach_config: OutreachConfig,
    shutdown=None,
) -> int:
    """
    Send outreach emails to qualifying leads.

    Respects rate limits and graceful shutdown.
    Returns number of emails sent.
    """
    sent_count = 0
    sent_this_hour = 0
    current_hour = datetime.now().hour

    def _rate_limit_sleep():
        low = min(outreach_config.delay_between_emails_min_seconds, outreach_config.delay_between_emails_max_seconds)
        high = max(outreach_config.delay_between_emails_min_seconds, outreach_config.delay_between_emails_max_seconds)
        delay = random.uniform(low, high)
        time.sleep(delay)

    for lead in leads:
        # Check shutdown
        if shutdown and shutdown.check():
            logger.info("Shutdown requested, stopping outreach")
            break

        # Business hours check
        if not _within_sending_window(outreach_config):
            logger.info("Outside business hours, pausing outreach")
            break

        # Check daily limit
        if sent_count >= outreach_config.max_emails_per_day:
            logger.info(f"Daily limit reached ({outreach_config.max_emails_per_day})")
            break

        # Hourly limit window
        now_hour = datetime.now().hour
        if now_hour != current_hour:
            current_hour = now_hour
            sent_this_hour = 0
        if sent_this_hour >= outreach_config.max_emails_per_hour:
            logger.info(f"Hourly limit reached ({outreach_config.max_emails_per_hour})")
            break

        place_id = lead.get("place_id", "")

        # Get contact info from DB
        contact_data = db.get_contact(place_id)
        if not contact_data:
            continue

        contact = ContactInfo(
            email=contact_data["email"],
            source=contact_data["source"],
            confidence=contact_data["confidence"],
        )

        # Skip suppressed emails
        if db.is_suppressed(contact.email):
            logger.debug(f"Skipping suppressed email: {contact.email}")
            continue

        # Skip low-confidence contacts
        if contact.confidence < outreach_config.min_contact_confidence:
            logger.debug(
                f"Skipping {place_id}: confidence {contact.confidence} < {outreach_config.min_contact_confidence}"
            )
            continue

        # Get audit URL
        audit_url = db.get_audit_url(place_id)
        if not audit_url:
            logger.debug(f"Skipping {place_id}: no audit URL")
            continue

        # Send email
        result = send_audit_email(
            lead, contact, audit_url, smtp_config, outreach_config
        )

        # Record in DB
        db.record_outreach(
            place_id=result.place_id,
            email=result.email,
            audit_url=audit_url,
            success=result.success,
            error=result.error,
        )

        if result.permanent_failure:
            db.add_suppression(contact.email, result.error or "permanent_smtp_failure")

        if result.success:
            sent_count += 1
            sent_this_hour += 1
            # Rate limiting delay
            if sent_count < outreach_config.max_emails_per_day:
                _rate_limit_sleep()

    logger.info(f"Outreach complete: {sent_count} emails sent")
    return sent_count


def run_followups(
    db,
    smtp_config: SMTPConfig,
    outreach_config: OutreachConfig,
    shutdown=None,
    max_to_send: int = None,
) -> int:
    """Send follow-up emails to eligible leads."""
    leads = db.get_leads_for_followup(min_days_since_sent=3)
    if not leads:
        return 0

    sent_count = 0
    sent_this_hour = 0
    current_hour = datetime.now().hour
    max_to_send = max_to_send if max_to_send is not None else outreach_config.max_emails_per_day

    def _rate_limit_sleep():
        low = min(outreach_config.delay_between_emails_min_seconds, outreach_config.delay_between_emails_max_seconds)
        high = max(outreach_config.delay_between_emails_min_seconds, outreach_config.delay_between_emails_max_seconds)
        delay = random.uniform(low, high)
        time.sleep(delay)

    for lead in leads:
        if shutdown and shutdown.check():
            logger.info("Shutdown requested, stopping follow-ups")
            break
        if not _within_sending_window(outreach_config):
            logger.info("Outside business hours, pausing follow-ups")
            break
        if sent_count >= max_to_send:
            break

        now_hour = datetime.now().hour
        if now_hour != current_hour:
            current_hour = now_hour
            sent_this_hour = 0
        if sent_this_hour >= outreach_config.max_emails_per_hour:
            break

        contact = ContactInfo(
            email=lead.get("email", ""),
            source="followup",
            confidence=lead.get("confidence", 1.0),
        )
        if db.is_suppressed(contact.email):
            continue

        audit_url = lead.get("audit_url")
        if not audit_url:
            continue

        result = send_followup_email(
            lead, contact, audit_url, smtp_config, outreach_config
        )
        db.record_followup(place_id=result.place_id, success=result.success, error=result.error)

        if result.permanent_failure:
            db.add_suppression(contact.email, result.error or "permanent_smtp_failure")

        if result.success:
            sent_count += 1
            sent_this_hour += 1
            if sent_count < max_to_send:
                _rate_limit_sleep()

    logger.info(f"Follow-up complete: {sent_count} emails sent")
    return sent_count
