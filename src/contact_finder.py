"""
Contact finder for BrokenSite-Weekly.
Extracts email addresses from business websites using multiple strategies.
"""

import json
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .logging_setup import get_logger

logger = get_logger("contact_finder")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_SESSION: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        _SESSION = session
    return _SESSION

# Domains/patterns that produce false positive emails
FALSE_POSITIVE_PATTERNS = [
    r"@example\.com",
    r"@domain\.com",
    r"@email\.com",
    r"@yoursite\.com",
    r"@yourdomain\.com",
    r"@test\.com",
    r"@localhost",
    r"wixpress\.com",
    r"sentry\.io",
    r"cloudflare\.com",
    r"googleapis\.com",
    r"w3\.org",
    r"schema\.org",
    r"gravatar\.com",
    r"wordpress\.org",
    r"wordpress\.com",
    r"squarespace\.com",
    r"@sentry\.",
    r"@webpack\.",
    r"@babel\.",
]

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

CONTACT_PAGE_PATTERNS = [
    "/contact",
    "/contact-us",
    "/about",
    "/about-us",
    "/reach-us",
    "/get-in-touch",
]


@dataclass
class ContactInfo:
    """Contact information extracted from a website."""

    email: str
    source: str  # "structured_data", "mailto", "contact_page", "regex"
    confidence: float  # 0.0 to 1.0
    owner_name: Optional[str] = None


def _clean_email(email: str) -> str:
    """Normalize email: strip whitespace, lowercase, remove mailto: prefix."""
    email = email.strip().lower()
    if email.startswith("mailto:"):
        email = email[7:]
    return email.split("?")[0]  # Strip query params


def _is_valid_email(email: str) -> bool:
    """Check if email is valid and not a false positive."""
    if not EMAIL_REGEX.fullmatch(email):
        return False
    for pattern in FALSE_POSITIVE_PATTERNS:
        if re.search(pattern, email, re.IGNORECASE):
            return False
    # Filter out very short local parts (likely noise)
    local_part = email.split("@")[0]
    if len(local_part) < 2:
        return False
    return True


def _extract_from_jsonld(soup: BeautifulSoup) -> Optional[str]:
    """Extract email from JSON-LD structured data."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            text = script.string
            if not text:
                continue
            data = json.loads(text)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                # Direct email field
                if "email" in item:
                    email = _clean_email(str(item["email"]))
                    if _is_valid_email(email):
                        return email
                # Nested contactPoint
                cp = item.get("contactPoint")
                if isinstance(cp, dict) and "email" in cp:
                    email = _clean_email(str(cp["email"]))
                    if _is_valid_email(email):
                        return email
                if isinstance(cp, list):
                    for point in cp:
                        if isinstance(point, dict) and "email" in point:
                            email = _clean_email(str(point["email"]))
                            if _is_valid_email(email):
                                return email
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
    return None


def _extract_mailto(soup: BeautifulSoup) -> Optional[str]:
    """Extract email from mailto: links."""
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if href.lower().startswith("mailto:"):
            email = _clean_email(href)
            if _is_valid_email(email):
                return email
    return None


def _find_contact_page_url(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    """Find URL of contact page from navigation links."""
    for link in soup.find_all("a", href=True):
        href = link["href"].lower()
        for pattern in CONTACT_PAGE_PATTERNS:
            if pattern in href:
                return urljoin(base_url, link["href"])
    return None


def _extract_via_regex(html: str) -> Optional[str]:
    """Last resort: regex for email patterns with false positive filtering."""
    matches = EMAIL_REGEX.findall(html)
    for email in matches:
        email = _clean_email(email)
        if _is_valid_email(email):
            return email
    return None


def find_contact_email(
    website_url: str,
    timeout: int = 10,
    session: Optional[requests.Session] = None,
) -> Optional[ContactInfo]:
    """
    Attempt to find a contact email for the business.

    Tries strategies in order of confidence:
    1. JSON-LD structured data (0.95)
    2. Mailto links (0.9)
    3. Contact page discovery (0.85)
    4. Regex fallback (0.6)

    Returns ContactInfo or None. Never raises.
    """
    try:
        logger.debug(f"Finding contact for {website_url}")
        session = session or _get_session()
        response = session.get(
            website_url,
            timeout=timeout,
            allow_redirects=True,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Strategy 1: JSON-LD structured data
        email = _extract_from_jsonld(soup)
        if email:
            logger.debug(f"Found email via JSON-LD: {email}")
            return ContactInfo(email=email, source="structured_data", confidence=0.95)

        # Strategy 2: Mailto links
        email = _extract_mailto(soup)
        if email:
            logger.debug(f"Found email via mailto: {email}")
            return ContactInfo(email=email, source="mailto", confidence=0.9)

        # Strategy 3: Contact page
        contact_url = _find_contact_page_url(soup, website_url)
        if contact_url:
            try:
                contact_resp = session.get(
                    contact_url,
                    timeout=timeout,
                    allow_redirects=True,
                )
                contact_resp.raise_for_status()
                contact_soup = BeautifulSoup(contact_resp.text, "html.parser")

                email = _extract_from_jsonld(contact_soup)
                if email:
                    return ContactInfo(
                        email=email, source="contact_page", confidence=0.85
                    )
                email = _extract_mailto(contact_soup)
                if email:
                    return ContactInfo(
                        email=email, source="contact_page", confidence=0.85
                    )
                email = _extract_via_regex(contact_resp.text)
                if email:
                    return ContactInfo(
                        email=email, source="contact_page", confidence=0.75
                    )
            except Exception as e:
                logger.debug(f"Contact page fetch failed for {contact_url}: {e}")

        # Strategy 4: Regex fallback on homepage
        email = _extract_via_regex(response.text)
        if email:
            logger.debug(f"Found email via regex: {email}")
            return ContactInfo(email=email, source="regex", confidence=0.6)

        logger.debug(f"No contact email found for {website_url}")
        return None

    except Exception as e:
        logger.warning(f"Contact finder error for {website_url}: {e}")
        return None


def find_contact_with_isolation(
    website_url: str,
    timeout: int = 10,
    session: Optional[requests.Session] = None,
) -> tuple[Optional[ContactInfo], Optional[str]]:
    """Never raises - returns (contact_info, error_message)."""
    try:
        result = find_contact_email(website_url, timeout, session=session)
        return result, None
    except Exception as e:
        logger.error(f"Contact finder isolation caught error for {website_url}: {e}")
        return None, str(e)
