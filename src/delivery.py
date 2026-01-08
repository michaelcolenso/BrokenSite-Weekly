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

from .config import SMTPConfig, RetryConfig, OUTPUT_DIR
from .retry import retry_with_backoff
from .gumroad import Subscriber
from .logging_setup import get_logger

logger = get_logger("delivery")


@dataclass
class DeliveryResult:
    """Result of email delivery attempt."""
    subscriber_email: str
    success: bool
    error: Optional[str] = None


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
        "score",
        "reasons",
        "address",
        "phone",
        "city",
        "category",
        "place_id",
    ]

    # Write to string buffer
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    for lead in leads:
        # Ensure all fields exist
        row = {field: lead.get(field, "") for field in fieldnames}
        writer.writerow(row)

    csv_content = buffer.getvalue()

    # Also write to file
    output_path.write_text(csv_content, encoding="utf-8")
    logger.info(f"Generated CSV with {len(leads)} leads: {output_path}")

    return csv_content, output_path


def create_email(
    subscriber: Subscriber,
    csv_content: str,
    csv_filename: str,
    lead_count: int,
    config: SMTPConfig,
) -> MIMEMultipart:
    """Create email message with CSV attachment."""
    msg = MIMEMultipart()
    msg["From"] = f"{config.from_name} <{config.from_email}>"
    msg["To"] = subscriber.email
    msg["Subject"] = f"Your Weekly Broken Website Leads ({lead_count} leads)"

    # Email body
    body = f"""Hi{' ' + subscriber.full_name.split()[0] if subscriber.full_name else ''},

Your weekly batch of broken/outdated website leads is attached.

This week's highlights:
• {lead_count} new leads identified
• All leads scored 40+ on our broken-site scale
• Sorted by score (highest = most broken)

Quick tips:
• Focus on scores 75+ first (hard failures: site down, SSL errors, parked domains)
• Scores 40-74 are softer signals (outdated copyright, no mobile, HTTP-only)
• The "reasons" column explains why each site scored high

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
    csv_filename = f"broken_site_leads_{date_str}.csv"
    csv_content, csv_path = generate_csv(leads)

    logger.info(f"Delivering {len(leads)} leads to {len(subscribers)} subscribers")

    for subscriber in subscribers:
        try:
            msg = create_email(
                subscriber=subscriber,
                csv_content=csv_content,
                csv_filename=csv_filename,
                lead_count=len(leads),
                config=config,
            )

            send_email(msg, config, retry_config)

            logger.info(f"Delivered to: {subscriber.email}")
            results.append(DeliveryResult(
                subscriber_email=subscriber.email,
                success=True,
            ))

        except Exception as e:
            logger.error(f"Failed to deliver to {subscriber.email}: {e}")
            results.append(DeliveryResult(
                subscriber_email=subscriber.email,
                success=False,
                error=str(e),
            ))

    success_count = sum(1 for r in results if r.success)
    logger.info(f"Delivery complete: {success_count}/{len(subscribers)} successful")

    return results


def deliver_with_isolation(
    subscribers: List[Subscriber],
    leads: List[Dict[str, Any]],
    config: SMTPConfig,
    retry_config: RetryConfig = None,
) -> tuple[List[DeliveryResult], Optional[str]]:
    """
    Deliver to subscribers with full error isolation.
    Returns (results, error_message).
    Never raises exceptions to caller.
    """
    try:
        results = deliver_to_subscribers(subscribers, leads, config, retry_config)
        return results, None
    except Exception as e:
        logger.error(f"Delivery system error: {e}")
        return [], str(e)
