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
import time
from datetime import datetime, timezone
from typing import Tuple, List, Optional, Dict, Any
from dataclasses import dataclass
from urllib.parse import urlparse, urljoin

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
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        _SESSION = session
    return _SESSION


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


@dataclass
class SimpleResponse:
    """Minimal response wrapper for non-requests fetchers."""
    status_code: int
    url: str
    text: str


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

# Social-only destinations (case-insensitive, in hostname)
SOCIAL_ONLY_DOMAINS = [
    "facebook.com",
    "fb.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "tiktok.com",
    "youtube.com",
    "pinterest.com",
]

# Bot protection indicators (case-insensitive)
BOT_PROTECTION_INDICATORS = [
    "cloudflare",
    "attention required",
    "checking your browser",
    "verify you are human",
    "are you human",
    "access denied",
    "unusual traffic",
    "captcha",
    "ddos protection",
    "sucuri",
    "incapsula",
    "akamai",
    "ray id",
]

# JS-required indicators (case-insensitive)
JS_REQUIRED_INDICATORS = [
    "enable javascript",
    "please enable javascript",
    "requires javascript",
    "you need to enable javascript",
]

# Basic SEO and construction indicators
GENERIC_TITLE_PATTERNS = [
    r"^home$",
    r"^homepage$",
    r"^welcome$",
    r"^index$",
    r"^untitled$",
    r"^website$",
]

UNDER_CONSTRUCTION_PATTERNS = [
    "under construction",
    "coming soon",
    "site is being built",
    "website coming soon",
    "launching soon",
]

MARKETING_SIGNALS = {
    "has_gtm": [
        "googletagmanager.com/gtm.js",
        "gtm-",
    ],
    "has_fb_pixel": [
        "connect.facebook.net/en_us/fbevents.js",
        "fbq(",
    ],
    "has_gclid": [
        "gclid=",
    ],
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


def _parse_last_modified_years(headers: Dict[str, Any]) -> Optional[float]:
    """Return age in years from Last-Modified header if available."""
    if not headers:
        return None
    last_modified = None
    if isinstance(headers, dict):
        last_modified = headers.get("Last-Modified") or headers.get("last-modified")
    if not last_modified:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            parsed = datetime.strptime(last_modified, fmt)
            parsed = parsed.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - parsed).days
            return age_days / 365.25
        except ValueError:
            continue
    return None


def _extract_title(html: str) -> Optional[str]:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return title or None


def _is_generic_title(title: Optional[str]) -> bool:
    if not title:
        return False
    normalized = re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()
    for pattern in GENERIC_TITLE_PATTERNS:
        if re.match(pattern, normalized):
            return True
    return False


def _has_meta_description(html: str) -> bool:
    return bool(re.search(r'<meta\s+name=["\']description["\']', html, re.IGNORECASE))


def _has_h1(html: str) -> bool:
    return "<h1" in html.lower()


def _detect_under_construction(html: str) -> bool:
    html_lower = html.lower()
    return any(pattern in html_lower for pattern in UNDER_CONSTRUCTION_PATTERNS)


def _detect_marketing_signals(html: str) -> List[str]:
    html_lower = html.lower()
    found = []
    for key, patterns in MARKETING_SIGNALS.items():
        for pattern in patterns:
            if pattern in html_lower:
                found.append(key)
                break
    return found


def _check_ssl_expiry(hostname: str, port: int = 443, timeout: int = 5) -> Optional[int]:
    """
    Check SSL certificate expiry for a hostname.
    Returns days until expiry, or None if check fails.
    """
    try:
        context = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
                if not cert:
                    return None
                expiry_str = cert.get("notAfter")
                if not expiry_str:
                    return None
                expiry_date = ssl.cert_time_to_seconds(expiry_str)
                expiry = datetime.fromtimestamp(expiry_date, tz=timezone.utc)
                now = datetime.now(tz=timezone.utc)
                return (expiry - now).days
    except Exception:
        return None


_IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

_A_HREF_RE = re.compile(r'<a[^>]+href=["\']([^"\']+)["\']', re.IGNORECASE)

_PHONE_RE = re.compile(
    r'(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})'
)

_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')


def _normalize_phone(phone: str) -> str:
    """Strip non-digits and remove leading US country code."""
    digits = re.sub(r'\D', '', phone or '')
    if len(digits) == 11 and digits.startswith('1'):
        return digits[1:]
    return digits


def _extract_phone_numbers(html: str) -> List[str]:
    """Extract phone numbers from HTML."""
    if not html:
        return []
    return _PHONE_RE.findall(html)


def _check_broken_images(html: str, base_url: str, max_images: int = 10, timeout: int = 5) -> Tuple[int, List[str]]:
    """
    Sample image tags and check if they load.
    Returns (broken_count, list_of_reasons).
    """
    if not html or not base_url:
        return 0, []

    srcs = _IMG_SRC_RE.findall(html)[:max_images]
    if not srcs:
        return 0, []

    broken_count = 0
    broken_reasons: List[str] = []

    for src in srcs:
        if src.startswith(("data:", "#", "javascript:")):
            continue
        full_url = urljoin(base_url, src)
        try:
            resp = requests.head(full_url, timeout=timeout, allow_redirects=True)
            if resp.status_code >= 400:
                broken_count += 1
                broken_reasons.append(f"broken_image_{src}")
        except Exception:
            broken_count += 1
            broken_reasons.append(f"broken_image_{src}")

    return broken_count, broken_reasons


def _check_dead_social_links(
    html: str, base_url: str, max_check: int = 5, timeout: int = 5
) -> Tuple[int, List[str]]:
    """
    Check social media links on a page and return dead ones.
    Returns (dead_count, list_of_reasons).
    """
    if not html or not base_url:
        return 0, []

    hrefs = _A_HREF_RE.findall(html)
    if not hrefs:
        return 0, []

    seen: set[str] = set()
    dead_count = 0
    dead_reasons: List[str] = []

    for href in hrefs:
        if len(seen) >= max_check:
            break
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        if not _is_social_url(full_url):
            continue
        seen.add(full_url)
        try:
            resp = requests.head(full_url, timeout=timeout, allow_redirects=True)
            if resp.status_code in (404, 410):
                dead_count += 1
                dead_reasons.append(f"dead_social_link_{full_url}")
        except Exception:
            dead_count += 1
            dead_reasons.append(f"dead_social_link_{full_url}")

    return dead_count, dead_reasons


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


def _check_bot_protection(html: str, status_code: Optional[int]) -> bool:
    """Detect bot protection or access blocks."""
    if not html:
        return False
    html_lower = html.lower()
    matches = [indicator for indicator in BOT_PROTECTION_INDICATORS if indicator in html_lower]
    if not matches:
        return False
    if status_code in (403, 429, 503):
        return True
    return len(matches) >= 2


def _check_js_required(html: str) -> bool:
    """Detect pages that require JavaScript to render content."""
    if not html:
        return False
    html_lower = html.lower()
    if len(html_lower) > 2000:
        return False
    return any(indicator in html_lower for indicator in JS_REQUIRED_INDICATORS)


def _dns_resolves(hostname: str) -> Optional[bool]:
    """Return True if DNS resolves, False if NXDOMAIN, None on unknown error."""
    if not hostname:
        return None
    try:
        socket.getaddrinfo(hostname, None)
        return True
    except socket.gaierror:
        return False
    except Exception:
        return None


def _apply_unverified_cap(
    score: int,
    reasons: List[str],
    config: ScoringConfig,
) -> Tuple[int, List[str]]:
    """Cap unverified leads to avoid auto-export when configured."""
    if config.include_unverified_leads:
        return score, reasons

    if any(reason in config.unverified_reasons for reason in reasons):
        if "unverified" not in reasons:
            reasons.append("unverified")
        score = min(score, config.unverified_score_cap)

    return score, reasons


def _is_social_url(url: str) -> bool:
    """Check if the URL points to a social-only profile/page."""
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return any(host == domain or host.endswith(f".{domain}") for domain in SOCIAL_ONLY_DOMAINS)


def _should_attempt_playwright(error: Optional[str]) -> bool:
    """Determine whether to retry with Playwright based on error type."""
    if not error:
        return False
    lowered = error.lower()
    if "ssl_error" in lowered:
        return False
    return any(token in lowered for token in (
        "timeout",
        "connection_error",
        "request_error",
        "fetch_failed",
        "too_many_redirects",
    ))


def _fetch_with_playwright(
    url: str,
    config: ScoringConfig,
) -> Tuple[Optional[SimpleResponse], Optional[str]]:
    """Fetch page content with Playwright as a fallback for failed requests."""
    try:
        from playwright.sync_api import (
            sync_playwright,
            TimeoutError as PlaywrightTimeout,
            Error as PlaywrightError,
        )
    except Exception as e:
        return None, f"playwright_import_error: {e}"

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )
            page = context.new_page()
            try:
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=config.playwright_fallback_timeout_ms,
                )
                final_url = page.url or url
                html = page.content()
                status_code = response.status if response else 0
                if status_code == 0 and not html:
                    return None, "playwright_no_response"
                return SimpleResponse(
                    status_code=status_code,
                    url=final_url,
                    text=html,
                ), None
            finally:
                context.close()
                browser.close()
    except PlaywrightTimeout as e:
        return None, f"playwright_timeout: {e}"
    except PlaywrightError as e:
        return None, f"playwright_error: {e}"
    except Exception as e:
        return None, f"playwright_error: {e}"


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

    session = _get_session()

    def do_fetch(fetch_url: str):
        return session.get(
            fetch_url,
            timeout=config.request_timeout_seconds,
            allow_redirects=True,
            verify=True,  # Verify SSL
        )

    def attempt(fetch_url: str) -> Tuple[Optional[requests.Response], Optional[str]]:
        try:
            if retry_config:
                response = retry_with_backoff(
                    func=lambda: do_fetch(fetch_url),
                    config=retry_config,
                    exceptions=(ConnectionError, Timeout),
                    logger=logger,
                    operation_name=f"fetch_{urlparse(fetch_url).netloc}",
                )
            else:
                response = do_fetch(fetch_url)

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

    response, error = attempt(url)
    if response or not config.allow_scheme_fallback:
        return response, error

    parsed = urlparse(url)
    if parsed.scheme == "https":
        fallback_url = parsed._replace(scheme="http").geturl()
    elif parsed.scheme == "http":
        fallback_url = parsed._replace(scheme="https").geturl()
    else:
        fallback_url = None

    if not fallback_url or fallback_url == url:
        return response, error

    fallback_response, fallback_error = attempt(fallback_url)
    if fallback_response:
        return fallback_response, None

    if error and fallback_error:
        return None, f"{error}; fallback_error: {fallback_error}"
    return None, error or fallback_error


def evaluate_website(
    url: str,
    config: ScoringConfig = None,
    retry_config: RetryConfig = None,
    response: requests.Response = None,
    expected_phone: Optional[str] = None,
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

    if _is_social_url(url):
        if not config.include_social_only_leads:
            score, reasons = _apply_unverified_cap(
                score=0,
                reasons=["social_only_excluded"],
                config=config,
            )
            return ScoringResult(
                url=url,
                score=score,
                reasons=reasons,
                http_status=None,
                response_time_ms=None,
                final_url=None,
                error=None,
            )
        score, reasons = _apply_unverified_cap(
            score=config.weight_social_only,
            reasons=["social_only"],
            config=config,
        )
        return ScoringResult(
            url=url,
            score=score,
            reasons=reasons,
            http_status=None,
            response_time_ms=None,
            final_url=None,
            error=None,
        )

    # Fetch if no response provided
    if response is None:
        start = time.time()
        response, error = fetch_website(url, config, retry_config)
        if response:
            response_time_ms = int((time.time() - start) * 1000)

    if error and config.playwright_fallback_enabled and _should_attempt_playwright(error):
        fallback_start = time.time()
        fallback_response, fallback_error = _fetch_with_playwright(url, config)
        if fallback_response:
            response = fallback_response
            error = None
            response_time_ms = int((time.time() - fallback_start) * 1000)
        elif fallback_error:
            error = f"{error}; {fallback_error}"

    # === Hard failures (high score) ===

    if response is None and not error:
        error = "fetch_failed"

    if error:
        if config.dns_check_enabled and any(
            token in error for token in ("connection_error", "request_error", "fetch_failed")
        ):
            hostname = urlparse(url).netloc
            dns_result = _dns_resolves(hostname)
            if dns_result is False:
                score += config.weight_dns_failed
                reasons.append("dns_failed")
                score, reasons = _apply_unverified_cap(score, reasons, config)
                return ScoringResult(
                    url=url,
                    score=score,
                    reasons=reasons,
                    http_status=None,
                    response_time_ms=response_time_ms,
                    final_url=None,
                    error=error,
                )

        if "ssl_error" in error:
            score += config.weight_ssl_error
            reasons.append("ssl_error")
        elif "timeout" in error:
            score += config.weight_timeout
            reasons.append("timeout")
        elif "fetch_failed" in error:
            score += config.weight_fetch_failed
            reasons.append("fetch_failed")
        elif "connection_error" in error:
            score += config.weight_fetch_failed
            reasons.append("fetch_failed")
        elif "unreachable" in error:
            score += config.weight_unreachable
            reasons.append("unreachable")
        elif "request_error" in error or "too_many_redirects" in error:
            score += config.weight_fetch_failed
            reasons.append("fetch_failed")
        else:
            score += config.weight_fetch_failed
            reasons.append("fetch_failed")

        score, reasons = _apply_unverified_cap(score, reasons, config)
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
    if response_time_ms is None:
        try:
            response_time_ms = int(response.elapsed.total_seconds() * 1000)
        except Exception:
            response_time_ms = None

    # === SSL certificate expiry check ===
    check_url = final_url or url
    if check_url and check_url.startswith("https://"):
        hostname = urlparse(check_url).hostname
        if hostname:
            days_remaining = _check_ssl_expiry(hostname)
            if days_remaining is not None and days_remaining <= config.ssl_expiry_days_threshold:
                score += config.weight_ssl_expiry
                reasons.append(f"ssl_expires_{days_remaining}_days")

    # === Analyze page content ===
    if response_time_ms is not None and response_time_ms >= config.slow_response_ms_threshold:
        score += config.weight_slow_response
        reasons.append(f"slow_response_{response_time_ms}ms")

    redirect_count = 0
    try:
        redirect_count = len(response.history or [])
    except Exception:
        redirect_count = 0

    if redirect_count >= config.redirect_chain_length_threshold:
        score += config.weight_redirect_chain
        reasons.append(f"redirect_chain_{redirect_count}")

    last_modified_years = _parse_last_modified_years(getattr(response, "headers", None))
    if last_modified_years is not None and last_modified_years >= config.last_modified_years_threshold:
        score += config.weight_last_modified_stale
        reasons.append(f"last_modified_{last_modified_years:.1f}y")

    try:
        html = response.text
    except Exception:
        html = ""

    if _check_bot_protection(html, http_status):
        score += config.weight_bot_protection
        reasons.append("bot_protection")
        score, reasons = _apply_unverified_cap(score, reasons, config)
        return ScoringResult(
            url=url,
            score=score,
            reasons=reasons,
            http_status=http_status,
            response_time_ms=response_time_ms,
            final_url=final_url,
            error=error,
        )

    js_required = _check_js_required(html)
    if js_required:
        score += config.weight_js_required
        reasons.append("js_required")

    # 5xx server errors
    if 500 <= http_status < 600:
        score += config.weight_5xx_error
        reasons.append(f"server_error_{http_status}")

    # 4xx client errors (but not 403/404 which might be intentional)
    elif 400 <= http_status < 500 and http_status not in (403, 404):
        score += config.weight_client_error
        reasons.append(f"client_error_{http_status}")

    # Even 403/404 is a problem for a business site
    elif http_status in (403, 404):
        score += config.weight_not_found_or_forbidden
        reasons.append(f"http_{http_status}")

    if not html or len(html) < 100:
        if not js_required:
            score += 60
            reasons.append("empty_page")
        score, reasons = _apply_unverified_cap(score, reasons, config)
        return ScoringResult(
            url=url,
            score=score,
            reasons=reasons,
            http_status=http_status,
            response_time_ms=response_time_ms,
            final_url=final_url,
            error=error,
        )

    # Under construction / coming soon
    if _detect_under_construction(html):
        score += config.weight_under_construction
        reasons.append("under_construction")

    # Parked domain check
    if _check_parked_domain(html):
        score += config.weight_parked_domain
        reasons.append("parked_domain")

    # HTTP only (no SSL)
    if final_url and final_url.startswith("http://"):
        score += config.weight_http_only
        reasons.append("no_https")

    # === Medium signals ===

    # Missing meta description
    if not _has_meta_description(html):
        score += config.weight_missing_meta_description
        reasons.append("missing_meta_description")

    # Missing H1
    if not _has_h1(html):
        score += config.weight_missing_h1
        reasons.append("missing_h1")

    # Generic title tag
    title = _extract_title(html)
    if title and _is_generic_title(title):
        score += config.weight_generic_title
        reasons.append("generic_title")

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

    # Broken images
    if config.broken_image_check_enabled:
        broken_count, broken_reasons = _check_broken_images(
            html, final_url or url, max_images=config.broken_image_max_sample
        )
        if broken_count > 0:
            score += broken_count * config.weight_broken_image
            reasons.extend(broken_reasons)

    # Dead social links
    if config.dead_social_check_enabled:
        dead_social_count, dead_social_reasons = _check_dead_social_links(
            html, final_url or url, max_check=config.dead_social_max_check
        )
        if dead_social_count > 0:
            score += dead_social_count * config.weight_dead_social_link
            reasons.extend(dead_social_reasons)

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

    # Marketing spend indicators
    for signal in _detect_marketing_signals(html):
        if signal not in reasons:
            reasons.append(signal)
            weight = {
                "has_gtm": config.weight_has_gtm,
                "has_fb_pixel": config.weight_has_fb_pixel,
                "has_gclid": config.weight_has_gclid,
            }.get(signal, 0)
            score += weight
    if final_url and "gclid=" in final_url.lower() and "has_gclid" not in reasons:
        reasons.append("has_gclid")
        score += config.weight_has_gclid

    # Contact info checks
    if html:
        if _EMAIL_RE.search(html) is None:
            score += config.weight_missing_email
            reasons.append("missing_email")

        phones_found = _extract_phone_numbers(html)
        if not phones_found:
            score += config.weight_missing_phone
            reasons.append("missing_phone")
        elif expected_phone:
            expected_norm = _normalize_phone(expected_phone)
            if expected_norm and not any(_normalize_phone(p) == expected_norm for p in phones_found):
                score += config.weight_phone_mismatch
                reasons.append("phone_mismatch")

    score, reasons = _apply_unverified_cap(score, reasons, config)
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
    expected_phone: Optional[str] = None,
) -> ScoringResult:
    """
    Evaluate website with full error isolation.
    Never raises exceptions to caller.
    """
    try:
        return evaluate_website(url, config, retry_config, expected_phone=expected_phone)
    except Exception as e:
        logger.error(f"Unexpected error evaluating {url}: {e}")
        config = config or ScoringConfig()
        score = 100
        reasons = ["evaluation_error"]
        score, reasons = _apply_unverified_cap(score, reasons, config)
        return ScoringResult(
            url=url,
            score=score,
            reasons=reasons,
            http_status=None,
            response_time_ms=None,
            final_url=None,
            error=str(e),
        )
