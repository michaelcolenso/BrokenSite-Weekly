"""
Audit page generator for BrokenSite-Weekly.
Generates personalized HTML audit pages from lead data using Jinja2 templates.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

from .config import PROJECT_ROOT, OUTPUT_DIR
from .lead_utils import parse_reasons
from .logging_setup import get_logger

logger = get_logger("audit_generator")

TEMPLATES_DIR = PROJECT_ROOT / "templates"
AUDITS_DIR = OUTPUT_DIR / "audits"

# Mapping from scoring reasons to human-readable issue descriptions
ISSUE_DESCRIPTIONS = {
    "ssl_error": {
        "title": "SSL Certificate Error",
        "severity": "critical",
        "description": "Visitors see a security warning before reaching your site. This can cause up to 85% of potential customers to leave immediately.",
        "impact": "Lost customers, damaged trust",
    },
    "dns_failed": {
        "title": "Website Unreachable",
        "severity": "critical",
        "description": "Your website cannot be reached at all. Potential customers searching for your services will find nothing.",
        "impact": "Complete loss of online presence",
    },
    "unreachable": {
        "title": "Website Unreachable",
        "severity": "critical",
        "description": "Your website cannot be reached. Potential customers will see an error page instead of your business.",
        "impact": "Complete loss of online presence",
    },
    "timeout": {
        "title": "Website Too Slow",
        "severity": "critical",
        "description": "Your website takes too long to load. Most visitors leave if a page doesn't load within 3 seconds.",
        "impact": "Lost visitors, lower search rankings",
    },
    "parked_domain": {
        "title": "Parked Domain",
        "severity": "critical",
        "description": "Your domain shows a 'parked' or 'for sale' page instead of your business information.",
        "impact": "No online presence, confused customers",
    },
    "no_https": {
        "title": "No Secure Connection",
        "severity": "high",
        "description": "Your site doesn't use HTTPS. Google marks these sites as 'Not Secure' and ranks them lower in search results.",
        "impact": "Lower search rankings, customer hesitation",
    },
    "no_viewport": {
        "title": "Not Mobile-Friendly",
        "severity": "high",
        "description": "Your website doesn't adapt to mobile screens. Over 60% of local searches happen on phones.",
        "impact": "Poor mobile experience, lost mobile customers",
    },
    "not_responsive": {
        "title": "Not Responsive Design",
        "severity": "medium",
        "description": "Your website doesn't use responsive design techniques. It may look broken on tablets and phones.",
        "impact": "Poor user experience on mobile devices",
    },
    "outdated_flash": {
        "title": "Uses Adobe Flash",
        "severity": "high",
        "description": "Your website uses Adobe Flash, which is no longer supported by any modern browser.",
        "impact": "Content invisible to all visitors",
    },
    "no_website": {
        "title": "No Website Found",
        "severity": "critical",
        "description": "No website is listed for your business on Google Maps. You're invisible to customers searching online.",
        "impact": "Missing out on all online search traffic",
    },
    "social_only": {
        "title": "No Dedicated Website",
        "severity": "medium",
        "description": "Your business uses social media as its website. While useful, a dedicated website builds more credibility and ranks better in local searches.",
        "impact": "Limited search visibility, less professional appearance",
    },
    "fetch_failed": {
        "title": "Website Error",
        "severity": "high",
        "description": "Your website returned an error when we tried to access it. Visitors may be seeing the same error.",
        "impact": "Potential visitors turned away",
    },
    "outdated_frames": {
        "title": "Uses HTML Frames",
        "severity": "medium",
        "description": "Your website uses HTML frames, a technique from the 1990s that hurts SEO and accessibility.",
        "impact": "Poor search indexing, accessibility issues",
    },
    "outdated_marquee": {
        "title": "Uses Marquee Tags",
        "severity": "medium",
        "description": "Your website uses scrolling marquee text, which looks dated and is not accessible.",
        "impact": "Unprofessional appearance",
    },
    "missing_meta_description": {
        "title": "Missing Meta Description",
        "severity": "medium",
        "description": "Your site is missing a meta description, which reduces click-through rates from search results.",
        "impact": "Lower search visibility and fewer clicks",
    },
    "missing_h1": {
        "title": "Missing Main Heading",
        "severity": "medium",
        "description": "Your homepage lacks a clear H1 heading, which hurts SEO and confuses visitors.",
        "impact": "Weaker SEO signals and unclear messaging",
    },
    "generic_title": {
        "title": "Generic Page Title",
        "severity": "medium",
        "description": "Your page title is generic (e.g., 'Home'), which reduces SEO effectiveness.",
        "impact": "Lower rankings and fewer qualified visitors",
    },
    "under_construction": {
        "title": "Under Construction Page",
        "severity": "critical",
        "description": "Your website shows an under-construction page instead of business information.",
        "impact": "Visitors can't learn about your business",
    },
}

NON_ISSUE_REASONS = {
    "has_gtm",
    "has_fb_pixel",
    "has_gclid",
}


def _parse_copyright_year(reason: str) -> Optional[Dict[str, str]]:
    """Parse copyright_YYYY reason into issue dict."""
    match = re.match(r"copyright_(\d{4})", reason)
    if not match:
        return None
    year = match.group(1)
    years_old = datetime.now().year - int(year)
    return {
        "title": f"Outdated Copyright Year ({year})",
        "severity": "medium",
        "description": (
            f"Your website footer shows a copyright year of {year}, which is "
            f"{years_old} years out of date. This signals to visitors that the "
            "site is neglected and outdated."
        ),
        "impact": "Appears unprofessional, suggests business is inactive",
    }


def _parse_server_error(reason: str) -> Optional[Dict[str, str]]:
    """Parse server_error_NNN reason into issue dict."""
    match = re.match(r"server_error_(\d{3})", reason)
    if not match:
        return None
    status_code = match.group(1)
    return {
        "title": f"Server Error ({status_code})",
        "severity": "critical" if status_code.startswith("5") else "high",
        "description": (
            f"Your website is returning a {status_code} server error. "
            "Visitors are seeing an error page instead of your business information."
        ),
        "impact": "No one can access your website right now",
    }


def _parse_diy_builder(reason: str) -> Optional[Dict[str, str]]:
    """Parse diy_* builder reasons into issue dict."""
    builders = {
        "diy_wix": "Wix",
        "diy_squarespace": "Squarespace",
        "diy_weebly": "Weebly",
        "diy_godaddy": "GoDaddy Website Builder",
    }
    if reason not in builders:
        return None
    name = builders[reason]
    return {
        "title": f"Using {name}",
        "severity": "medium",
        "description": (
            f"Your website is built with {name}, which can be limiting for "
            "SEO, customization, and performance."
        ),
        "impact": "Limited SEO capabilities, slower load times",
    }


def _parse_reasons(reasons_input: str | List[str]) -> List[Dict[str, str]]:
    """Convert reasons into list of issue dicts."""
    issues = []
    for reason in parse_reasons(reasons_input):
        if not reason:
            continue
        if reason in NON_ISSUE_REASONS:
            continue
        issue = ISSUE_DESCRIPTIONS.get(reason)
        if issue:
            issues.append(issue.copy())
        elif reason.startswith("copyright_"):
            parsed = _parse_copyright_year(reason)
            if parsed:
                issues.append(parsed)
        elif reason.startswith("server_error_"):
            parsed = _parse_server_error(reason)
            if parsed:
                issues.append(parsed)
        elif reason.startswith("diy_"):
            parsed = _parse_diy_builder(reason)
            if parsed:
                issues.append(parsed)
        else:
            logger.warning(f"Unknown reason '{reason}' - skipping in audit")
    return issues


def generate_audit_html(lead_data: Dict, tracking_base_url: str) -> Optional[str]:
    """
    Generate audit page HTML from lead data using Jinja2 template.

    Returns rendered HTML string, or None on error.
    """
    try:
        issues = _parse_reasons(lead_data.get("reasons", ""))
        if not issues:
            logger.warning(
                f"No valid issues for lead {lead_data.get('place_id')} - skipping audit"
            )
            return None

        env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
        template = env.get_template("audit.html")

        html = template.render(
            business_name=lead_data.get("name", "Unknown Business"),
            website=lead_data.get("website", ""),
            city=lead_data.get("city", "Unknown Location"),
            category=lead_data.get("category", ""),
            score=lead_data.get("score", 0),
            issues=issues,
            tracking_base_url=tracking_base_url,
            place_id=lead_data.get("place_id", ""),
            generated_date=datetime.now().strftime("%B %d, %Y"),
        )
        logger.info(
            f"Generated audit HTML for {lead_data.get('name')} ({len(issues)} issues)"
        )
        return html

    except TemplateNotFound as e:
        logger.error(f"Template not found: {e}")
        return None
    except Exception as e:
        logger.error(f"Error generating audit HTML: {e}", exc_info=True)
        return None


def generate_audit_page(lead_data: Dict, config) -> Tuple[Optional[str], Optional[str]]:
    """
    Generate audit page file and return (audit_url, file_path).

    Returns (None, None) on error.
    """
    try:
        AUDITS_DIR.mkdir(parents=True, exist_ok=True)

        html = generate_audit_html(lead_data, config.outreach.tracking_base_url)
        if not html:
            return None, None

        place_id = lead_data.get("place_id", "unknown")
        file_path = AUDITS_DIR / f"{place_id}.html"
        file_path.write_text(html, encoding="utf-8")

        audit_url = f"{config.outreach.tracking_base_url}/audit/{place_id}"
        logger.info(f"Saved audit page to {file_path}")
        return audit_url, str(file_path)

    except Exception as e:
        logger.error(
            f"Error generating audit page for {lead_data.get('place_id')}: {e}",
            exc_info=True,
        )
        return None, None


def get_issues_json(lead_data: Dict) -> str:
    """Get JSON representation of issues for database storage."""
    issues = _parse_reasons(lead_data.get("reasons", ""))
    return json.dumps(issues)
