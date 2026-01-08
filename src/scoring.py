"""
Website scoring module for BrokenSite-Weekly.
Evaluates websites for "broken" or "outdated" signals.

Scoring philosophy:
- Hard failures (unreachable, 5xx, parked) = high score (75-100)
- Medium signals (no SSL, outdated copyright) = medium score (15-40)
- Weak signals (DIY builders) = low score (5-10) to minimize false positives
"""

import re
import ssl
import socket
from datetime import datetime
from typing import Tuple, List, Optional, Dict, Any
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from requests.exceptions import (
    RequestException,
    Timeout,
    ConnectionError,
    SSLError,
    TooManyRedirects,
)

from .config import ScoringConfig, RetryConfig
from .retry import retry_with_backoff
from .logging_setup import get_logger

logger = get_logger("scoring")


@dataclass
class ScoringResult:
    """Result of website evaluation."""
    url: str
    score: int
    reasons: List[str]
    http_status: Optional[int]
    response_time_ms: Optional[int]
    final_url: Optional[str]
    error: Optional[str]


# Parked domain indicators (case-insensitive)
PARKED_INDICATORS = [
    "domain for sale",
    "this domain is for sale",
    "buy this domain",
    "domain parking",
    "parked domain",
    "domain has expired",
    "this site is under construction",
    "website coming soon",
    "future home of",
    "hostgator.com",
    "godaddy.com/domainsearch",
    "sedoparking.com",
    "domainmarket.com",
    "hugedomains.com",
    "afternic.com",
    "dan.com/buy-domain",
]

# DIY website builders (case-insensitive, in HTML source)
DIY_BUILDERS = {
    "wix.com": "wix",
    "squarespace.com": "squarespace",
    "weebly.com": "weebly",
    "site123.com": "site123",
    "godaddy.com/websites": "godaddy_builder",
    "wordpress.com": "wordpress_com",  # Note: self-hosted WP is fine
    "jimdo.com": "jimdo",
    "webflow.io": "webflow",
    "carrd.co": "carrd",
    "wixsite.com": "wix",
}

# Footer/copyright patterns for year extraction
# We specifically look for copyright context to avoid false positives from
# phone numbers, addresses, prices, etc.
COPYRIGHT_PATTERNS = [
    r'©\s*(\d{4})',
    r'copyright\s*(?:©)?\s*(\d{4})',
    r'\(c\)\s*(\d{4})',
    r'all rights reserved[^0-9]*(\d{4})',
]


def _normalize_url(url: str) -> str:
    """Ensure URL has a scheme."""
    if not url:
        return url
    if not url.startswith(('http://', 'https://')):
        return f"https://{url}"
    return url


def _extract_copyright_year(html: str) -> Optional[int]:
    """
    Extract copyright year from HTML, focusing on footer context.
    Returns None if no copyright year found.
    """
    html_lower = html.lower()

    # First, try to find a footer section and search there
    footer_markers = ['<footer', 'class="footer"', 'id="footer"', '</body>']
    footer_start = -1

    for marker in footer_markers:
        pos = html_lower.rfind(marker)
        if pos > footer_start:
            footer_start = pos

    # Search in footer area (last 20% of page if no footer found)
    if footer_start > 0:
        search_area = html[footer_start:]
    else:
        search_area = html[int(len(html) * 0.8):]

    # Look for copyright patterns
    years_found = []
    for pattern in COPYRIGHT_PATTERNS:
        matches = re.findall(pattern, search_area, re.IGNORECASE)
        years_found.extend(int(y) for y in matches if 1990 <= int(y) <= datetime.now().year + 1)

    if years_found:
        return max(years_found)

    # Fallback: search entire page but require copyright context
    for pattern in COPYRIGHT_PATTERNS:
        matches = re.findall(pattern, html, re.IGNORECASE)
        years_found.extend(int(y) for y in matches if 1990 <= int(y) <= datetime.now().year + 1)

    return max(years_found) if years_found else None


def _check_parked_domain(html: str) -> bool:
    """Check if page appears to be a parked domain."""
    html_lower = html.lower()
    matches = sum(1 for indicator in PARKED_INDICATORS if indicator in html_lower)
    # Require at least 1 strong indicator or 2 weak ones
    return matches >= 1


def _check_diy_builder(html: str, url: str) -> Optional[str]:
    """Check if site uses a DIY builder. Returns builder name or None."""
    html_lower = html.lower()
    url_lower = url.lower()

    for pattern, builder_name in DIY_BUILDERS.items():
        if pattern in html_lower or pattern in url_lower:
            return builder_name
    return None


def _check_mobile_friendly(html: str) -> Tuple[bool, bool]:
    """
    Check for mobile-friendliness indicators.
    Returns (has_viewport, has_responsive_hints)
    """
    html_lower = html.lower()

    has_viewport = 'name="viewport"' in html_lower or "name='viewport'" in html_lower

    responsive_hints = [
        "@media",
        "bootstrap",
        "tailwind",
        "foundation",
        "responsive",
        "mobile-friendly",
    ]
    has_responsive = any(hint in html_lower for hint in responsive_hints)

    return has_viewport, has_responsive


def _check_outdated_tech(html: str) -> List[str]:
    """Check for outdated web technologies."""
    html_lower = html.lower()
    outdated = []

    # Flash
    if '<object' in html_lower and ('flash' in html_lower or '.swf' in html_lower):
        outdated.append("flash")

    # Very old HTML patterns
    if '<frameset' in html_lower or '<frame ' in html_lower:
        outdated.append("frames")

    if '<marquee' in html_lower:
        outdated.append("marquee")

    if '<blink' in html_lower:
        outdated.append("blink_tag")

    # Old jQuery (1.x or 2.x)
    jquery_match = re.search(r'jquery[.-]?([12])\.\d+', html_lower)
    if jquery_match:
        outdated.append("old_jquery")

    return outdated


def fetch_website(
    url: str,
    config: ScoringConfig,
    retry_config: RetryConfig = None,
) -> Tuple[Optional[requests.Response], Optional[str]]:
    """
    Fetch a website with proper error handling.
    Returns (response, error_message).
    """
    url = _normalize_url(url)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    def do_fetch():
        return requests.get(
            url,
            timeout=config.request_timeout_seconds,
            headers=headers,
            allow_redirects=True,
            verify=True,  # Verify SSL
        )

    try:
        if retry_config:
            response = retry_with_backoff(
                func=do_fetch,
                config=retry_config,
                exceptions=(ConnectionError, Timeout),
                logger=logger,
                operation_name=f"fetch_{urlparse(url).netloc}",
            )
        else:
            response = do_fetch()

        return response, None

    except SSLError as e:
        return None, f"ssl_error: {e}"
    except Timeout:
        return None, "timeout"
    except ConnectionError as e:
        return None, f"connection_error: {e}"
    except TooManyRedirects:
        return None, "too_many_redirects"
    except RequestException as e:
        return None, f"request_error: {e}"


def evaluate_website(
    url: str,
    config: ScoringConfig = None,
    retry_config: RetryConfig = None,
    response: requests.Response = None,
) -> ScoringResult:
    """
    Evaluate a website and return a score with reasons.

    Higher score = more likely to be broken/outdated.
    Score >= config.min_score_to_include suggests a good lead.
    """
    config = config or ScoringConfig()
    url = _normalize_url(url)

    score = 0
    reasons: List[str] = []
    http_status = None
    response_time_ms = None
    final_url = None
    error = None

    # Fetch if no response provided
    if response is None:
        import time
        start = time.time()
        response, error = fetch_website(url, config, retry_config)
        if response:
            response_time_ms = int((time.time() - start) * 1000)

    # === Hard failures (high score) ===

    if error:
        if "ssl_error" in error:
            score += config.weight_ssl_error
            reasons.append("ssl_error")
        elif "timeout" in error:
            score += config.weight_timeout
            reasons.append("timeout")
        elif "connection_error" in error or "unreachable" in error:
            score += config.weight_unreachable
            reasons.append("unreachable")
        else:
            score += config.weight_unreachable
            reasons.append("fetch_failed")

        return ScoringResult(
            url=url,
            score=score,
            reasons=reasons,
            http_status=None,
            response_time_ms=response_time_ms,
            final_url=None,
            error=error,
        )

    # We have a response
    http_status = response.status_code
    final_url = response.url

    # 5xx server errors
    if 500 <= http_status < 600:
        score += config.weight_5xx_error
        reasons.append(f"server_error_{http_status}")

    # 4xx client errors (but not 403/404 which might be intentional)
    elif 400 <= http_status < 500 and http_status not in (403, 404):
        score += 40
        reasons.append(f"client_error_{http_status}")

    # Even 403/404 is a problem for a business site
    elif http_status in (403, 404):
        score += 50
        reasons.append(f"http_{http_status}")

    # === Analyze page content ===

    try:
        html = response.text
    except Exception:
        html = ""

    if not html or len(html) < 100:
        score += 60
        reasons.append("empty_page")
        return ScoringResult(
            url=url,
            score=score,
            reasons=reasons,
            http_status=http_status,
            response_time_ms=response_time_ms,
            final_url=final_url,
            error=error,
        )

    # Parked domain check
    if _check_parked_domain(html):
        score += config.weight_parked_domain
        reasons.append("parked_domain")

    # HTTP only (no SSL)
    if url.startswith("http://") and not final_url.startswith("https://"):
        score += config.weight_http_only
        reasons.append("no_https")

    # === Medium signals ===

    # Outdated copyright year
    copyright_year = _extract_copyright_year(html)
    if copyright_year:
        years_old = datetime.now().year - copyright_year
        if years_old >= 2:
            score += config.weight_outdated_copyright
            reasons.append(f"copyright_{copyright_year}")

    # Missing viewport (not mobile-friendly)
    has_viewport, has_responsive = _check_mobile_friendly(html)
    if not has_viewport:
        score += config.weight_missing_viewport
        reasons.append("no_viewport")
    if not has_responsive and not has_viewport:
        score += config.weight_missing_responsive
        reasons.append("not_responsive")

    # Outdated technologies
    outdated_tech = _check_outdated_tech(html)
    for tech in outdated_tech:
        if tech == "flash":
            score += config.weight_flash_detected
        else:
            score += 10
        reasons.append(f"outdated_{tech}")

    # === Weak signals (low weight) ===

    diy_builder = _check_diy_builder(html, final_url or url)
    if diy_builder:
        weight = {
            "wix": config.weight_wix,
            "squarespace": config.weight_squarespace,
            "weebly": config.weight_weebly,
            "godaddy_builder": config.weight_godaddy_builder,
        }.get(diy_builder, 5)

        score += weight
        reasons.append(f"diy_{diy_builder}")

    return ScoringResult(
        url=url,
        score=score,
        reasons=reasons,
        http_status=http_status,
        response_time_ms=response_time_ms,
        final_url=final_url,
        error=error,
    )


def evaluate_with_isolation(
    url: str,
    config: ScoringConfig = None,
    retry_config: RetryConfig = None,
) -> ScoringResult:
    """
    Evaluate website with full error isolation.
    Never raises exceptions to caller.
    """
    try:
        return evaluate_website(url, config, retry_config)
    except Exception as e:
        logger.error(f"Unexpected error evaluating {url}: {e}")
        return ScoringResult(
            url=url,
            score=100,  # Assume broken if we can't check
            reasons=["evaluation_error"],
            http_status=None,
            response_time_ms=None,
            final_url=None,
            error=str(e),
        )
