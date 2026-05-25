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

# Owner/decision-maker name extraction patterns
# Ordered by confidence — earlier matches are stronger
OWNER_ROLE_PATTERNS = [
    # Schema.org Person markup (highest confidence)
    # Handled separately in _extract_owner_from_jsonld

    # Explicit role labels followed by a name (lazy quantifier stops at 2 words)
    (r"(?:owner|founder|ceo|president|principal|managing\s+director|proprietor)[:\-]?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}?)(?=[\s,.]|$)", 0.85),
    # "Meet [name], our [role]" pattern
    (r"meet\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}?),?\s+(?:our|the)\s+(?:owner|founder|ceo)", 0.80),
    # "[Name], Owner/Founder/CEO" — name followed by role
    (r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}?),\s*(?:owner|founder|ceo|president|proprietor)", 0.80),
    # "About the Owner" section header → grab the next name-like text
    (r"about\s+(?:the\s+)?(?:owner|founder)[:\-]?\s*</h[1-4]>\s*(?:<[^>]+>)*\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}?)", 0.70),
    # Author meta tag
    (r'<meta\s+name=["\']author["\'][^>]*content=["\']([^"\']+)["\']', 0.60),
]

# Phrases that indicate we found a false positive (not a real person name)
OWNER_FALSE_POSITIVES = [
    "your name",
    "your business",
    "company name",
    "business name",
    "contact us",
    "get in touch",
    "learn more",
    "read more",
    "click here",
    "welcome to",
    "about us",
    "our team",
    "the team",
    "our story",
    "our mission",
    "privacy policy",
    "terms of service",
    "all rights reserved",
    "copyright",
    "powered by",
    "designed by",
    "website by",
    "built by",
    "created by",
]

# Pages likely to contain owner info
ABOUT_PAGE_PATTERNS = [
    "/about",
    "/about-us",
    "/team",
    "/our-team",
    "/meet-the-team",
    "/who-we-are",
    "/our-story",
    "/founder",
    "/leadership",
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
    Attempt to find a contact email and owner name for the business.

    Tries strategies in order of confidence:
    1. JSON-LD structured data (0.95)
    2. Mailto links (0.9)
    3. Contact page discovery (0.85)
    4. Regex fallback (0.6)

    Also attempts to extract owner/decision-maker name.

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
        html = response.text

        # Extract owner name in parallel with email search
        owner_result = find_owner_name(
            website_url, html=html, soup=soup, timeout=timeout, session=session
        )
        owner_name = owner_result[0] if owner_result else None

        # Strategy 1: JSON-LD structured data
        email = _extract_from_jsonld(soup)
        if email:
            logger.debug(f"Found email via JSON-LD: {email}")
            return ContactInfo(
                email=email, source="structured_data", confidence=0.95,
                owner_name=owner_name,
            )

        # Strategy 2: Mailto links
        email = _extract_mailto(soup)
        if email:
            logger.debug(f"Found email via mailto: {email}")
            return ContactInfo(
                email=email, source="mailto", confidence=0.9,
                owner_name=owner_name,
            )

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
                        email=email, source="contact_page", confidence=0.85,
                        owner_name=owner_name,
                    )
                email = _extract_mailto(contact_soup)
                if email:
                    return ContactInfo(
                        email=email, source="contact_page", confidence=0.85,
                        owner_name=owner_name,
                    )
                email = _extract_via_regex(contact_resp.text)
                if email:
                    return ContactInfo(
                        email=email, source="contact_page", confidence=0.75,
                        owner_name=owner_name,
                    )
            except Exception as e:
                logger.debug(f"Contact page fetch failed for {contact_url}: {e}")

        # Strategy 4: Regex fallback on homepage
        email = _extract_via_regex(response.text)
        if email:
            logger.debug(f"Found email via regex: {email}")
            return ContactInfo(
                email=email, source="regex", confidence=0.6,
                owner_name=owner_name,
            )

        # Even if no email found, owner name alone is valuable
        if owner_name:
            logger.debug(f"Found owner name (no email): {owner_name}")
            return ContactInfo(
                email="", source="owner_only", confidence=0.3,
                owner_name=owner_name,
            )

        logger.debug(f"No contact email found for {website_url}")
        return None

    except Exception as e:
        logger.warning(f"Contact finder error for {website_url}: {e}")
        return None


def _extract_owner_from_jsonld(soup: BeautifulSoup) -> Optional[tuple[str, float]]:
    """Extract owner/person name from JSON-LD structured data."""
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
                item_type = item.get("@type", "").lower()
                # Person type = direct owner
                if item_type == "person":
                    name = item.get("name")
                    if name:
                        return (str(name).strip(), 0.90)
                # Organization founder
                if "founder" in item:
                    founder = item["founder"]
                    if isinstance(founder, dict) and founder.get("name"):
                        return (str(founder["name"]).strip(), 0.90)
                    if isinstance(founder, str):
                        return (founder.strip(), 0.85)
                # Organization with author or owner
                if item_type in ("organization", "localbusiness") and "author" in item:
                    author = item["author"]
                    if isinstance(author, dict) and author.get("name"):
                        return (str(author["name"]).strip(), 0.80)
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
    return None


def _is_valid_person_name(name: str) -> bool:
    """Check if a string looks like a real person name (not a false positive)."""
    if not name or len(name) < 3:
        return False
    name_lower = name.lower().strip()
    for fp in OWNER_FALSE_POSITIVES:
        if fp in name_lower:
            return False
    # Must have at least one space (first + last name)
    if " " not in name.strip():
        return False
    # Must start with uppercase letter
    if not name[0].isupper():
        return False
    # Must not be all-caps (likely a company name)
    if name.isupper():
        return False
    # Must be 2-4 words (first, maybe middle, last, maybe suffix)
    words = name.split()
    if len(words) < 2 or len(words) > 4:
        return False
    # Each word should start uppercase (proper name)
    for word in words:
        if word[0].islower():
            return False
    return True


def _extract_owner_from_patterns(html: str, soup: BeautifulSoup) -> Optional[tuple[str, float]]:
    """Extract owner name using regex patterns against visible text and HTML."""
    # Get visible text (strip tags for cleaner pattern matching)
    text = soup.get_text(separator=" ", strip=True) if soup else ""
    text_clean = re.sub(r"\s+", " ", text)

    for pattern, confidence in OWNER_ROLE_PATTERNS:
        # Try on visible text first (cleaner)
        if text_clean:
            match = re.search(pattern, text_clean, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                if _is_valid_person_name(name):
                    return (name, confidence)

        # Fall back to raw HTML for meta-tag patterns
        if html and "meta" in pattern:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                if _is_valid_person_name(name):
                    return (name, confidence)

    return None


def _find_about_page_url(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    """Find the URL of an About/Team page from navigation links."""
    for link in soup.find_all("a", href=True):
        href = link["href"].lower().strip()
        for pattern in ABOUT_PAGE_PATTERNS:
            if pattern in href:
                return urljoin(base_url, link["href"])
    return None


def find_owner_name(
    website_url: str,
    html: str = "",
    soup: Optional[BeautifulSoup] = None,
    timeout: int = 10,
    session: Optional[requests.Session] = None,
) -> Optional[tuple[str, float]]:
    """
    Attempt to find the business owner / decision-maker name.

    Tries strategies in order of confidence:
    1. JSON-LD structured data (0.80-0.90)
    2. Regex patterns on homepage (0.60-0.85)
    3. About page discovery + extraction (0.55-0.70)

    Returns (name, confidence) or None. Never raises.
    """
    try:
        session = session or _get_session()

        # If no soup provided, fetch the homepage
        if soup is None:
            response = session.get(
                website_url,
                timeout=timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
            html = response.text
            soup = BeautifulSoup(html, "html.parser")

        # Strategy 1: JSON-LD
        result = _extract_owner_from_jsonld(soup)
        if result:
            return result

        # Strategy 2: Pattern match on homepage
        result = _extract_owner_from_patterns(html or str(soup), soup)
        if result and result[1] >= 0.70:
            return result

        # Strategy 3: Find and check About page
        about_url = _find_about_page_url(soup, website_url)
        if about_url and about_url != website_url:
            try:
                about_resp = session.get(
                    about_url,
                    timeout=timeout,
                    allow_redirects=True,
                )
                about_resp.raise_for_status()
                about_soup = BeautifulSoup(about_resp.text, "html.parser")

                # Try JSON-LD on about page
                about_result = _extract_owner_from_jsonld(about_soup)
                if about_result:
                    return about_result

                # Pattern match on about page (lower confidence threshold)
                about_result = _extract_owner_from_patterns(
                    about_resp.text, about_soup
                )
                if about_result:
                    return about_result

            except Exception as e:
                logger.debug(f"About page fetch failed for {about_url}: {e}")

        # Return the homepage regex result even if low confidence
        if result:
            return result

        return None

    except Exception as e:
        logger.debug(f"Owner name extraction error for {website_url}: {e}")
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
