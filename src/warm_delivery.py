"""
Warm lead delivery for BrokenSite-Weekly.
Identifies engaged leads and delivers them to subscribers.
"""

import csv
import io
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import Config, OUTPUT_DIR, SMTPConfig, PortalConfig
from .delivery import create_email, send_email, _sanitize_csv_value
from .portal_auth import generate_portal_token
from .lead_utils import compute_lead_tier, has_marketing_pixel, suggested_pitch_from_reasons
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
    "review_count",
    "website_score",
    "engagement_score",
    "email",
    "audit_url",
    "reasons",
    "lead_tier",
    "suggested_pitch",
    "has_marketing_pixel",
    "exclusive_until",
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
        reasons = lead.get("reasons", "")
        writer.writerow([
            _sanitize_csv_value(lead.get("name", "")),
            _sanitize_csv_value(lead.get("website", "")),
            _sanitize_csv_value(lead.get("phone", "")),
            _sanitize_csv_value(lead.get("address", "")),
            _sanitize_csv_value(lead.get("city", "")),
            _sanitize_csv_value(lead.get("category", "")),
            _sanitize_csv_value(lead.get("review_count", "")),
            lead.get("score", 0),
            lead.get("engagement_score", 0),
            _sanitize_csv_value(lead.get("email", "")),
            _sanitize_csv_value(lead.get("audit_url", "")),
            _sanitize_csv_value(reasons),
            lead.get("lead_tier") or compute_lead_tier(int(lead.get("score") or 0)),
            suggested_pitch_from_reasons(reasons),
            "yes" if has_marketing_pixel(reasons) else "no",
            _sanitize_csv_value(lead.get("exclusive_until", "")),
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
    portal_config: PortalConfig = None,
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
                portal_config=portal_config,
            )

            send_email(msg, config.smtp, config.retry)
            logger.info(f"Sent warm leads to {subscriber.email}")
            results.append({
                "subscriber_email": subscriber.email,
                "success": True,
                "lead_count": len(warm_leads),
                "csv_path": str(csv_path),
            })

        except Exception as e:
            logger.error(f"Failed to deliver warm leads to {subscriber.email}: {e}")
            results.append({
                "subscriber_email": subscriber.email,
                "success": False,
                "error": str(e),
                "csv_path": str(csv_path),
            })

    return results


def _create_warm_lead_email(
    subscriber, csv_content: str, csv_filename: str, lead_count: int, config: SMTPConfig, portal_config: PortalConfig = None
):
    """Create warm lead delivery email using existing create_email pattern."""
    portal_url = None
    if portal_config and portal_config.secret and portal_config.base_url:
        token = generate_portal_token(
            email=subscriber.email,
            secret=portal_config.secret,
            ttl_days=portal_config.token_ttl_days,
        )
        base_url = portal_config.base_url.rstrip("/")
        portal_url = f"{base_url}/portal?token={token}"

    # Reuse the existing create_email function from delivery.py
    # but with a modified subject line indicating these are warm leads
    msg = create_email(
        subscriber=subscriber,
        csv_content=csv_content,
        csv_filename=csv_filename,
        lead_count=lead_count,
        config=config,
        portal_url=portal_url,
    )
    # Override subject to indicate warm leads
    del msg["Subject"]
    msg["Subject"] = f"Your Warm Website Leads ({lead_count} engaged leads)"
    return msg


def deliver_warm_leads_with_isolation(
    subscribers: List,
    warm_leads: List[Dict],
    config: Config,
    portal_config: PortalConfig = None,
) -> Tuple[List[Dict], Optional[str]]:
    """Never raises - returns (results, error_message)."""
    try:
        results = deliver_warm_leads(subscribers, warm_leads, config, portal_config=portal_config)
        return results, None
    except Exception as e:
        logger.error(f"Warm lead delivery failed: {e}")
        return [], str(e)
