"""P0 detection checks for BrokenSite Weekly v1."""

from __future__ import annotations

import re
import socket
import ssl
from html.parser import HTMLParser
from typing import Iterable, Optional
from urllib.parse import urljoin

import requests

from .core import CheckResult

STALE_COPYRIGHT_RE = re.compile(r"(?:©|&copy;|\(c\))\s*(\d{4})", re.IGNORECASE)
WP_VERSION_RE = re.compile(r"wp-(?:content|includes)|wordpress\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
JOOMLA_RE = re.compile(r"joomla!?(?:\s*|-)([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
DRUPAL_RE = re.compile(r"drupal\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
FLASH_RE = re.compile(r"\.(?:swf)(?:[?'\"]|$)|application/x-shockwave-flash", re.IGNORECASE)


class HomepageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.has_viewport = False
        self.forms: list[str] = []
        self.images: list[str] = []
        self.generator = ""
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        data = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta" and data.get("name", "").lower() == "viewport":
            self.has_viewport = True
        if tag == "meta" and data.get("name", "").lower() == "generator":
            self.generator = data.get("content", "")
        if tag == "form":
            self.forms.append(data.get("action", ""))
        if tag == "img" and data.get("src"):
            self.images.append(data["src"])
        if tag == "a" and data.get("href"):
            self.links.append(data["href"])


def parse_homepage(html: str) -> HomepageParser:
    parser = HomepageParser()
    parser.feed(html or "")
    return parser


def ssl_expired(domain: str, *, timeout: float = 5.0) -> CheckResult:
    try:
        context = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=domain):
                return CheckResult("ssl_expired", False, "certificate valid", 5)
    except ssl.SSLCertVerificationError as exc:
        return CheckResult("ssl_expired", True, str(exc), 5)
    except OSError as exc:
        return CheckResult("ssl_expired", False, f"443 unreachable: {exc}", 5)


def no_https(domain: str, http_status: Optional[int], https_status: Optional[int]) -> CheckResult:
    if http_status and http_status < 500 and (https_status is None or https_status >= 400):
        return CheckResult("no_https", True, "HTTP serves but HTTPS is unavailable", 4)
    return CheckResult("no_https", False, "HTTPS available or HTTP unavailable", 4)


def dead_form(base_url: str, html: str, *, session: Optional[requests.Session] = None) -> CheckResult:
    parser = parse_homepage(html)
    session = session or requests.Session()
    for action in parser.forms:
        if not action or action.startswith("#") or action.lower().startswith("javascript:"):
            continue
        url = urljoin(base_url, action)
        try:
            response = session.head(url, timeout=10, allow_redirects=True)
            if response.status_code in (405, 501):
                response = session.get(url, timeout=10, allow_redirects=True)
        except requests.RequestException as exc:
            return CheckResult("dead_form", True, f"form action unreachable: {url} ({exc})", 5)
        if response.status_code >= 400:
            return CheckResult("dead_form", True, f"form action {url} returned {response.status_code}", 5)
    return CheckResult("dead_form", False, "no dead form actions found", 5)


def broken_pages(statuses: Iterable[Optional[int]]) -> CheckResult:
    values = list(statuses)[:3]
    failures = [s for s in values if s is None or s >= 400]
    if values and (values[0] is None or values[0] >= 400):
        return CheckResult("broken_pages", True, f"homepage returned {values[0]}", 5)
    if len(failures) >= 2:
        return CheckResult("broken_pages", True, f"{len(failures)} of {len(values)} crawled pages failed", 5)
    return CheckResult("broken_pages", False, "fewer than two crawled pages failed", 5)


def not_mobile(html: str) -> CheckResult:
    parser = parse_homepage(html)
    triggered = not parser.has_viewport
    evidence = "homepage missing viewport meta" if triggered else "viewport meta present"
    return CheckResult("not_mobile", triggered, evidence, 3)


def stale_copyright(html: str) -> CheckResult:
    years = [int(y) for y in STALE_COPYRIGHT_RE.findall(html or "")]
    if years and max(years) <= 2021:
        return CheckResult("stale_copyright", True, f"max copyright year {max(years)}", 2)
    return CheckResult("stale_copyright", False, "copyright current or absent", 2)


def broken_images(base_url: str, html: str, *, session: Optional[requests.Session] = None) -> CheckResult:
    parser = parse_homepage(html)
    session = session or requests.Session()
    broken = 0
    checked = 0
    for src in parser.images[:10]:
        checked += 1
        try:
            response = session.head(urljoin(base_url, src), timeout=10, allow_redirects=True)
            if response.status_code in (405, 501):
                response = session.get(urljoin(base_url, src), timeout=10, allow_redirects=True)
        except requests.RequestException:
            broken += 1
            continue
        if response.status_code >= 400:
            broken += 1
    return CheckResult("broken_images", broken >= 3, f"{broken} of {checked} checked images broken", 3)


def dead_cms(html: str) -> CheckResult:
    text = html or ""
    gen = parse_homepage(text).generator
    haystack = f"{gen}\n{text}"
    if FLASH_RE.search(haystack):
        return CheckResult("dead_cms", True, "Flash embed detected", 4)
    for regex, minimum, name in ((WP_VERSION_RE, 5.0, "WordPress"), (JOOMLA_RE, 3.9, "Joomla"), (DRUPAL_RE, 8.0, "Drupal")):
        match = regex.search(haystack)
        if match and match.groups() and match.group(1):
            try:
                version = float(match.group(1))
            except ValueError:
                continue
            if version < minimum:
                return CheckResult("dead_cms", True, f"{name} {version} detected", 4)
    return CheckResult("dead_cms", False, "no dead CMS indicators detected", 4)
