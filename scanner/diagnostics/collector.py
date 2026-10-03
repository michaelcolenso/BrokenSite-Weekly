"""Deterministic technical diagnostics (no LLM).

One browser navigation per site: load at the desktop viewport, screenshot,
resize to the mobile viewport (re-layout, no refetch), measure overflow,
screenshot again. Politeness rules from HANDOFF apply: robots.txt is checked,
the honest scanner User-Agent is used, and the per-domain 10s pacing window is
shared with the crawler.

``collect_diagnostics`` never raises; failures come back as a report with
``status="error"`` and an ``error`` string.
"""

from __future__ import annotations

import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from scanner.crawl import USER_AGENT, PoliteCrawler
from scanner.diagnostics.schema import ContactMethod, DiagnosticReport

import logging

logger = logging.getLogger("scanner.diagnostics")

DESKTOP_VIEWPORT = {"width": 1440, "height": 900}
MOBILE_VIEWPORT = {"width": 390, "height": 844}
NAV_TIMEOUT_MS = 15_000
SSL_TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 2
BACKOFF_SECONDS = 2.0
ASSET_TYPES = {"image", "stylesheet", "script", "font"}
MAX_LISTED_ASSETS = 10


@dataclass
class BrowseResult:
    """Raw facts gathered from one page load."""

    http_status: int = 0
    final_url: str = ""
    redirect_chain: list[str] = field(default_factory=list)
    mixed_content_count: int = 0
    broken_assets: list[str] = field(default_factory=list)
    mobile_viewport_overflow: bool = False
    contact_methods: list[ContactMethod] = field(default_factory=list)
    screenshot_path: Optional[str] = None
    mobile_screenshot_path: Optional[str] = None


def normalize_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    return value


def check_ssl(hostname: str, port: int = 443) -> tuple[bool, Optional[str], Optional[str]]:
    """Return (valid, expires_at_iso, error). Verifies chain and hostname."""
    context = ssl.create_default_context()
    try:
        with socket.create_connection((hostname, port), timeout=SSL_TIMEOUT_SECONDS) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as tls:
                cert = tls.getpeercert()
    except ssl.SSLError as exc:
        return False, None, f"ssl_error: {exc}"
    except (OSError, ValueError) as exc:
        return False, None, f"connect_error: {exc}"
    expires = None
    not_after = cert.get("notAfter") if cert else None
    if not_after:
        expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), tz=timezone.utc).isoformat()
    return True, expires, None


_CONTACT_JS = """() => ({
  phone: !!document.querySelector('a[href^="tel:" i]'),
  email: !!document.querySelector('a[href^="mailto:" i]'),
  form: Array.from(document.querySelectorAll('form')).some(f =>
    f.querySelector('input:not([type=hidden]):not([type=search]), textarea, select')),
})"""

_OVERFLOW_JS = "() => document.documentElement.scrollWidth > window.innerWidth + 1"


def browse(url: str, out_dir: Path, stem: str) -> BrowseResult:
    """Load `url` in headless Chromium and gather facts. May raise."""
    from playwright.sync_api import sync_playwright

    result = BrowseResult()
    broken: dict[str, None] = {}
    mixed: set[str] = set()

    with sync_playwright() as p:
        # BSW_CHROMIUM_PATH: optional override when Playwright's pinned build isn't installed.
        browser = p.chromium.launch(executable_path=os.environ.get("BSW_CHROMIUM_PATH") or None)
        try:
            context = browser.new_context(viewport=DESKTOP_VIEWPORT, user_agent=USER_AGENT)
            page = context.new_page()
            page.set_default_timeout(NAV_TIMEOUT_MS)

            def on_response(response):
                req = response.request
                if req.resource_type in ASSET_TYPES and response.status >= 400:
                    broken[response.url] = None

            def on_failed(req):
                if req.resource_type in ASSET_TYPES:
                    broken[req.url] = None

            def on_request(req):
                if req.url.startswith("http://"):
                    mixed.add(req.url)

            page.on("response", on_response)
            page.on("requestfailed", on_failed)
            page.on("request", on_request)

            response = page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
            if response is None:
                raise RuntimeError("no_response")
            result.http_status = response.status
            result.final_url = page.url

            chain: list[str] = []
            req = response.request
            while req.redirected_from is not None:
                req = req.redirected_from
                chain.append(req.url)
            result.redirect_chain = list(reversed(chain))

            out_dir.mkdir(parents=True, exist_ok=True)
            desktop_path = out_dir / f"{stem}-desktop.png"
            page.screenshot(path=str(desktop_path), type="png", full_page=False)
            result.screenshot_path = str(desktop_path)

            contacts = page.evaluate(_CONTACT_JS)
            result.contact_methods = [m for m in ("phone", "email", "form") if contacts.get(m)]

            page.set_viewport_size(MOBILE_VIEWPORT)
            page.wait_for_timeout(250)  # let responsive CSS settle
            result.mobile_viewport_overflow = bool(page.evaluate(_OVERFLOW_JS))
            mobile_path = out_dir / f"{stem}-mobile.png"
            page.screenshot(path=str(mobile_path), type="png", full_page=False)
            result.mobile_screenshot_path = str(mobile_path)
        finally:
            browser.close()

    # Only meaningful when the page itself was served over HTTPS.
    if result.final_url.startswith("https://"):
        result.mixed_content_count = len(mixed)
    result.broken_assets = list(broken)
    return result


def _safe_stem(domain: str) -> str:
    return "".join(c if c.isalnum() or c in "-." else "_" for c in domain) or "site"


def collect_diagnostics(
    url: str,
    output_dir: str | Path,
    *,
    crawler: Optional[PoliteCrawler] = None,
    browse_fn=browse,
    ssl_fn=check_ssl,
    sleep=time.sleep,
) -> DiagnosticReport:
    """Collect ground-truth technical flags for `url`. Never raises."""
    started = time.monotonic()
    url = normalize_url(url)
    parsed = urlparse(url)
    domain = (parsed.hostname or "").lower()

    def finish(report: DiagnosticReport) -> DiagnosticReport:
        report.execution_time_ms = int((time.monotonic() - started) * 1000)
        return report

    if not domain:
        return finish(DiagnosticReport(domain="", url=url, status="error", error="invalid_url"))

    try:
        crawler = crawler or PoliteCrawler()
        if not crawler.allowed(url):
            return finish(DiagnosticReport(domain=domain, url=url, status="blocked", error="robots_disallow"))

        report = DiagnosticReport(domain=domain, url=url, status="error")
        if parsed.scheme == "https":
            valid, expires, ssl_error = ssl_fn(domain, parsed.port or 443)
            report.ssl_valid, report.ssl_expires_at, report.ssl_error = valid, expires, ssl_error
        else:
            report.ssl_error = "no_https"

        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            crawler.wait_for_domain(parsed.netloc.lower())
            try:
                browsed = browse_fn(url, Path(output_dir), _safe_stem(domain))
            except Exception as exc:  # noqa: BLE001 - isolation boundary
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("diagnostics attempt %d failed for %s: %s", attempt, domain, last_error)
            else:
                last_error = None
                break
            finally:
                crawler.record_request(parsed.netloc.lower())
            if attempt < MAX_ATTEMPTS:
                sleep(BACKOFF_SECONDS * attempt)

        if last_error is not None:
            report.error = last_error
            if "ERR_CERT" in last_error or "SSL" in last_error.upper():
                report.ssl_valid = False
                report.ssl_error = report.ssl_error or last_error
            return finish(report)

        report.status = "ok"
        report.http_status = browsed.http_status
        report.reachable = 0 < browsed.http_status < 400
        report.final_url = browsed.final_url
        report.redirect_chain = browsed.redirect_chain
        report.mixed_content_count = browsed.mixed_content_count
        report.mobile_viewport_overflow = browsed.mobile_viewport_overflow
        report.broken_asset_count = len(browsed.broken_assets)
        report.broken_assets = browsed.broken_assets[:MAX_LISTED_ASSETS]
        report.contact_methods_found = browsed.contact_methods
        report.screenshot_path = browsed.screenshot_path
        report.mobile_screenshot_path = browsed.mobile_screenshot_path
        return finish(report)
    except Exception as exc:  # noqa: BLE001 - never raise
        logger.error("diagnostics crashed for %s: %s", domain, exc)
        return finish(DiagnosticReport(domain=domain, url=url, status="error", error=f"{type(exc).__name__}: {exc}"))
