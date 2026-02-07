"""
Email delivery module for BrokenSite-Weekly.
Sends weekly CSV attachments to subscribers via SMTP.
"""

import csv
import smtplib
import ssl
from io import StringIO
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from .config import SMTPConfig, RetryConfig, OUTPUT_DIR, PortalConfig
from .retry import retry_with_backoff
from .gumroad import Subscriber
from .portal_auth import generate_portal_token
from .lead_utils import compute_lead_tier, has_marketing_pixel, suggested_pitch_from_reasons, parse_reasons
from .logging_setup import get_logger

logger = get_logger("delivery")


@dataclass
class DeliveryResult:
    """Result of email delivery attempt."""
    subscriber_email: str
    success: bool
    error: Optional[str] = None
    csv_path: Optional[str] = None


def _sanitize_csv_value(value: Any) -> Any:
    """Prefix risky spreadsheet formulas with a single quote.

    >>> _sanitize_csv_value("=HYPERLINK('https://example.com')")
    "'=HYPERLINK('https://example.com')"
    >>> _sanitize_csv_value("@sum(A1:A2)")
    "'@sum(A1:A2)"
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def generate_csv(leads: List[Dict[str, Any]], output_path: Path = None) -> tuple[str, Path]:
    """
    Generate CSV content from leads.
    Returns (csv_content, file_path).
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if output_path is None:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        output_path = OUTPUT_DIR / f"leads_{date_str}.csv"

    # CSV columns
    fieldnames = [
        "name",
        "website",
        "address",
        "phone",
        "review_count",
        "city",
        "category",
        "score",
        "reasons",
        "lead_tier",
        "suggested_pitch",
        "has_marketing_pixel",
        "exclusive_until",
        "place_id",
    ]

    # Write to string buffer
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    for lead in leads:
        # Ensure all fields exist
        reasons = lead.get("reasons", "")
        reasons_list = parse_reasons(reasons)
        row = {
            field: _sanitize_csv_value(lead.get(field, ""))
            for field in fieldnames
        }
        row["lead_tier"] = lead.get("lead_tier") or compute_lead_tier(int(lead.get("score") or 0))
        row["suggested_pitch"] = suggested_pitch_from_reasons(reasons)
        row["has_marketing_pixel"] = "yes" if has_marketing_pixel(reasons) else "no"
        row["reasons"] = _sanitize_csv_value(",".join(reasons_list))
        writer.writerow(row)

    csv_content = buffer.getvalue()

    # Also write to file
    output_path.write_text(csv_content, encoding="utf-8")
    logger.info(f"Generated CSV with {len(leads)} leads: {output_path}")

    return csv_content, output_path


def generate_manual_review_csv(
    leads: List[Dict[str, Any]],
    output_path: Path = None,
) -> Optional[Path]:
    """
    Generate a CSV for manual review (unverified leads).
    Returns file path or None if no leads.
    """
    if not leads:
        return None

    if output_path is None:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        output_path = OUTPUT_DIR / f"manual_review_{date_str}.csv"

    generate_csv(leads, output_path=output_path)
    return output_path


def _build_portal_url(subscriber: Subscriber, portal_config: Optional[PortalConfig]) -> Optional[str]:
    if not portal_config or not portal_config.secret:
        return None
    base_url = portal_config.base_url.rstrip("/")
    if not base_url:
        return None
    token = generate_portal_token(
        email=subscriber.email,
        secret=portal_config.secret,
        ttl_days=portal_config.token_ttl_days,
    )
    return f"{base_url}/portal?token={token}"


def create_email(
    subscriber: Subscriber,
    csv_content: str,
    csv_filename: str,
    lead_count: int,
    config: SMTPConfig,
    portal_url: Optional[str] = None,
) -> MIMEMultipart:
    """Create email message with CSV attachment."""
    msg = MIMEMultipart()
    msg["From"] = f"{config.from_name} <{config.from_email}>"
    msg["To"] = subscriber.email
    msg["Subject"] = f"Your Weekly Broken Website Leads ({lead_count} leads)"

    # Email body
    greeting = f"Hi{' ' + subscriber.full_name.split()[0] if subscriber.full_name else ''},"
    portal_block = f"\nPortal access:\n{portal_url}\n" if portal_url else ""
    body = f"""{greeting}

Your weekly batch of broken/outdated website leads is attached.

This week's highlights:
• {lead_count} new leads identified
• All leads scored 40+ on our broken-site scale
• Sorted by score (highest = most broken)

Quick tips:
• Focus on scores 75+ first (hard failures: site down, SSL errors, parked domains)
• Scores 40-74 are softer signals (outdated copyright, no mobile, HTTP-only)
• The "reasons" column explains why each site scored high
{portal_block}

Happy prospecting!

---
BrokenSite Weekly
Unsubscribe anytime from your Gumroad account.
"""

    msg.attach(MIMEText(body, "plain"))

    # Attach CSV
    attachment = MIMEBase("application", "octet-stream")
    attachment.set_payload(csv_content.encode("utf-8"))
    encoders.encode_base64(attachment)
    attachment.add_header(
        "Content-Disposition",
        f'attachment; filename="{csv_filename}"',
    )
    msg.attach(attachment)

    return msg


def send_email(
    msg: MIMEMultipart,
    config: SMTPConfig,
    retry_config: RetryConfig = None,
) -> None:
    """Send email via SMTP with retry logic."""

    def do_send():
        context = ssl.create_default_context()

        if config.use_tls:
            with smtplib.SMTP(config.host, config.port) as server:
                server.starttls(context=context)
                server.login(config.username, config.password)
                server.send_message(msg)
        else:
            with smtplib.SMTP_SSL(config.host, config.port, context=context) as server:
                server.login(config.username, config.password)
                server.send_message(msg)

    if retry_config:
        retry_with_backoff(
            func=do_send,
            config=retry_config,
            exceptions=(smtplib.SMTPException, ConnectionError, TimeoutError),
            logger=logger,
            operation_name=f"send_to_{msg['To']}",
        )
    else:
        do_send()


def deliver_to_subscribers(
    subscribers: List[Subscriber],
    leads: List[Dict[str, Any]],
    config: SMTPConfig,
    retry_config: RetryConfig = None,
    portal_config: PortalConfig = None,
    csv_label: str = None,
) -> List[DeliveryResult]:
    """
    Deliver CSV to all subscribers.
    Returns list of delivery results (success/failure per subscriber).

    Each subscriber delivery is isolated - one failure doesn't affect others.
    """
    results: List[DeliveryResult] = []

    if not leads:
        logger.warning("No leads to deliver")
        return results

    if not subscribers:
        logger.warning("No subscribers to deliver to")
        return results

    # Generate CSV once
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    label = f"_{csv_label}" if csv_label else ""
    csv_filename = f"broken_site_leads_{date_str}{label}.csv"
    csv_content, csv_path = generate_csv(leads, output_path=OUTPUT_DIR / csv_filename)

    logger.info(f"Delivering {len(leads)} leads to {len(subscribers)} subscribers")

    for subscriber in subscribers:
        try:
            portal_url = _build_portal_url(subscriber, portal_config)
            msg = create_email(
                subscriber=subscriber,
                csv_content=csv_content,
                csv_filename=csv_filename,
                lead_count=len(leads),
                config=config,
                portal_url=portal_url,
            )

            send_email(msg, config, retry_config)

            logger.info(f"Delivered to: {subscriber.email}")
            results.append(DeliveryResult(
                subscriber_email=subscriber.email,
                success=True,
                csv_path=str(csv_path),
            ))

        except Exception as e:
            logger.error(f"Failed to deliver to {subscriber.email}: {e}")
            results.append(DeliveryResult(
                subscriber_email=subscriber.email,
                success=False,
                error=str(e),
                csv_path=str(csv_path),
            ))

    success_count = sum(1 for r in results if r.success)
    logger.info(f"Delivery complete: {success_count}/{len(subscribers)} successful")

    return results


def deliver_with_isolation(
    subscribers: List[Subscriber],
    leads: List[Dict[str, Any]],
    config: SMTPConfig,
    retry_config: RetryConfig = None,
    portal_config: PortalConfig = None,
    csv_label: str = None,
) -> tuple[List[DeliveryResult], Optional[str]]:
    """
    Deliver to subscribers with full error isolation.
    Returns (results, error_message).
    Never raises exceptions to caller.
    """
    try:
        results = deliver_to_subscribers(
            subscribers=subscribers,
            leads=leads,
            config=config,
            retry_config=retry_config,
            portal_config=portal_config,
            csv_label=csv_label,
        )
        return results, None
    except Exception as e:
        logger.error(f"Delivery system error: {e}")
        return [], str(e)
