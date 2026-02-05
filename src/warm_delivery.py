"""
Warm lead delivery for BrokenSite-Weekly.
Identifies engaged leads and delivers them to subscribers.
"""

import csv
import io
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import Config, OUTPUT_DIR, SMTPConfig
from .delivery import create_email, send_email, _sanitize_csv_value
from .logging_setup import get_logger

logger = get_logger("warm_delivery")

ENGAGEMENT_WEIGHTS = {
    "email_sent": 0,
    "email_opened": 5,
    "page_view": 25,
    "cta_click": 50,
    "unsubscribe": -100,
}

WARM_CSV_COLUMNS = [
    "name",
    "website",
    "phone",
    "address",
    "city",
    "category",
    "website_score",
    "engagement_score",
    "email",
    "audit_url",
    "reasons",
]


def generate_warm_lead_csv(
    warm_leads: List[Dict], output_path: Path = None
) -> Tuple[str, Path]:
    """
    Generate CSV from warm leads with engagement data.

    Returns (csv_content, file_path).
    """
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    if output_path is None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = OUTPUT_DIR / f"warm_leads_{date_str}.csv"

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(WARM_CSV_COLUMNS)

    for lead in warm_leads:
        writer.writerow([
            _sanitize_csv_value(lead.get("name", "")),
            _sanitize_csv_value(lead.get("website", "")),
            _sanitize_csv_value(lead.get("phone", "")),
            _sanitize_csv_value(lead.get("address", "")),
            _sanitize_csv_value(lead.get("city", "")),
            _sanitize_csv_value(lead.get("category", "")),
            lead.get("score", 0),
            lead.get("engagement_score", 0),
            _sanitize_csv_value(lead.get("email", "")),
            _sanitize_csv_value(lead.get("audit_url", "")),
            _sanitize_csv_value(lead.get("reasons", "")),
        ])

    csv_content = output.getvalue()

    # Write to file
    output_path.write_text(csv_content, encoding="utf-8")
    logger.info(f"Generated warm lead CSV: {output_path} ({len(warm_leads)} leads)")

    return csv_content, output_path


def deliver_warm_leads(
    subscribers: List,
    warm_leads: List[Dict],
    config: Config,
) -> List[Dict]:
    """
    Deliver warm leads to subscribers.

    Returns list of delivery results.
    """
    if not warm_leads:
        logger.info("No warm leads to deliver")
        return []

    if not subscribers:
        logger.info("No subscribers to deliver to")
        return []

    # Generate CSV
    csv_content, csv_path = generate_warm_lead_csv(warm_leads)
    csv_filename = csv_path.name

    results = []
    for subscriber in subscribers:
        try:
            msg = _create_warm_lead_email(
                subscriber=subscriber,
                csv_content=csv_content,
                csv_filename=csv_filename,
                lead_count=len(warm_leads),
                config=config.smtp,
            )

            send_email(msg, config.smtp, config.retry)
            logger.info(f"Sent warm leads to {subscriber.email}")
            results.append({
                "subscriber_email": subscriber.email,
                "success": True,
                "lead_count": len(warm_leads),
            })

        except Exception as e:
            logger.error(f"Failed to deliver warm leads to {subscriber.email}: {e}")
            results.append({
                "subscriber_email": subscriber.email,
                "success": False,
                "error": str(e),
            })

    return results


def _create_warm_lead_email(
    subscriber, csv_content: str, csv_filename: str, lead_count: int, config: SMTPConfig
):
    """Create warm lead delivery email using existing create_email pattern."""
    # Reuse the existing create_email function from delivery.py
    # but with a modified subject line indicating these are warm leads
    msg = create_email(
        subscriber=subscriber,
        csv_content=csv_content,
        csv_filename=csv_filename,
        lead_count=lead_count,
        config=config,
    )
    # Override subject to indicate warm leads
    del msg["Subject"]
    msg["Subject"] = f"Your Warm Website Leads ({lead_count} engaged leads)"
    return msg


def deliver_warm_leads_with_isolation(
    subscribers: List,
    warm_leads: List[Dict],
    config: Config,
) -> Tuple[List[Dict], Optional[str]]:
    """Never raises - returns (results, error_message)."""
    try:
        results = deliver_warm_leads(subscribers, warm_leads, config)
        return results, None
    except Exception as e:
        logger.error(f"Warm lead delivery failed: {e}")
        return [], str(e)
