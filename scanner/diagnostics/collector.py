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
from scanner.diagnostics.guard import FormPostBlocked, GuardedNavigator, RobotsBlocked
from scanner.diagnostics.schema import ContactMethod, DiagnosticReport

import logging

logger = logging.getLogger("scanner.diagnostics")

DESKTOP_VIEWPORT = {"width": 1440, "height": 900}
MOBILE_VIEWPORT = {"width": 390, "height": 844}
NAV_TIMEOUT_MS = 15_000
SSL_TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 2
MAX_REDIRECTS = 10
SETTLE_MS = 500  # let immediate JS / meta-refresh navigations land before capture
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
    blocked_navigation_url: Optional[str] = None


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

# clientWidth is the layout viewport (390 with a device-width meta tag, 980 without);
# innerWidth would already be stretched to the content width in mobile mode.
def _top_level_url(frame) -> str:
    while frame.parent_frame is not None:
        frame = frame.parent_frame
    return frame.url.split("#")[0]


def is_mixed_content_request(url: str, is_navigation: bool, is_main_frame: bool) -> bool:
    """HTTP subresource or child-frame document. Only main-frame navigations are exempt."""
    return url.startswith("http://") and not (is_navigation and is_main_frame)


_OVERFLOW_JS = "() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"


def browse(url: str, out_dir: Path, stem: str, allow_fn=None, wait_fn=None, record_fn=None) -> BrowseResult:
    """Load `url` in headless Chromium and gather facts. May raise.

    Every main-frame navigation (initial, redirect hop, JS or meta-refresh) is vetted with
    `allow_fn(url) -> bool` (robots.txt); a disallowed one is aborted and RobotsBlocked raised.
    `wait_fn(netloc)` reserves a request slot on, and `record_fn(netloc)` stamps, each navigation's
    own host. `allow_fn` is expected to pace its own robots.txt fetches (PoliteCrawler.allowed does). Sub-resource
    requests are not paced: that is the screenshot exception in HANDOFF hard rule 4.
    """
    from playwright.sync_api import sync_playwright

    result = BrowseResult()
    broken: dict[str, Optional[str]] = {}  # url -> issuing document URL
    mixed: dict[str, Optional[str]] = {}

    with sync_playwright() as p:
        # BSW_CHROMIUM_PATH: optional override when Playwright's pinned build isn't installed.
        browser = p.chromium.launch(executable_path=os.environ.get("BSW_CHROMIUM_PATH") or None)
        try:
            # service_workers="block": requests a service worker handles are invisible to context.route,
            # so a worker could serve pages past the robots and pacing guard.
            context = browser.new_context(viewport=DESKTOP_VIEWPORT, user_agent=USER_AGENT,
                                          service_workers="block")
            page = context.new_page()
            page.set_default_timeout(NAV_TIMEOUT_MS)
            nav = GuardedNavigator(
                context, page, allow_fn=allow_fn, wait_fn=wait_fn, record_fn=record_fn,
                nav_timeout_ms=NAV_TIMEOUT_MS, settle_ms=SETTLE_MS, max_redirects=MAX_REDIRECTS,
            )

            # Tag each request with the document URL that issued it, so observations from a
            # document we later left (redirect hop, client-side navigation) are excluded.
            issuer: dict = {}

            def on_request(req):
                try:
                    # Attribute to the top-level document, also for requests made inside iframes.
                    issuer[req] = _top_level_url(req.frame)
                except Exception:  # noqa: BLE001 - detached frame
                    issuer[req] = None
                try:
                    main_frame = req.frame.parent_frame is None
                except Exception:  # noqa: BLE001
                    main_frame = False
                if is_mixed_content_request(req.url, req.is_navigation_request(), main_frame):
                    mixed[req.url] = issuer[req]

            doc = {"status": 0, "url": ""}  # latest main-frame document actually served

            def on_response(response):
                req = response.request
                if req.is_navigation_request() and req.frame.parent_frame is None:
                    if not nav.pending:  # skip the blank page we serve for a redirect hop
                        doc["status"], doc["url"] = response.status, response.url
                    return
                if req.resource_type in ASSET_TYPES and response.status >= 400:
                    broken[response.url] = issuer.get(req)

            def on_failed(req):
                if req.resource_type in ASSET_TYPES:
                    broken[req.url] = issuer.get(req)

            page.on("response", on_response)
            page.on("requestfailed", on_failed)
            page.on("request", on_request)

            result.redirect_chain = nav.goto(url)
            # Navigation is now frozen so every capture below describes one document.
            nav.freeze()
            if nav.blocked:
                raise RobotsBlocked(nav.blocked[0])
            if not doc["url"]:
                raise RuntimeError("no_response")
            result.http_status, result.final_url = doc["status"], doc["url"]
            if page.url.split("#")[0] != doc["url"].split("#")[0]:
                raise RuntimeError("document_changed_during_capture")

            out_dir.mkdir(parents=True, exist_ok=True)
            desktop_path = out_dir / f"{stem}-desktop.png"
            page.screenshot(path=str(desktop_path), type="png", full_page=False)
            result.screenshot_path = str(desktop_path)

            contacts = page.evaluate(_CONTACT_JS)
            result.contact_methods = [m for m in ("phone", "email", "form") if contacts.get(m)]

            # Mobile emulation on the already-loaded page (no second fetch). is_mobile can't be
            # toggled on a Playwright context, so use CDP (Chromium-only). This honours the
            # meta viewport: pages without one lay out at 980px, as on a phone.
            page.set_viewport_size(MOBILE_VIEWPORT)  # sizes the screenshot; CDP override sets mobile semantics
            cdp = context.new_cdp_session(page)
            cdp.send("Emulation.setDeviceMetricsOverride", {**MOBILE_VIEWPORT, "deviceScaleFactor": 1, "mobile": True})
            page.wait_for_timeout(250)  # let responsive CSS settle
            result.mobile_viewport_overflow = bool(page.evaluate(_OVERFLOW_JS))
            mobile_path = out_dir / f"{stem}-mobile.png"
            page.screenshot(path=str(mobile_path), type="png", full_page=False)
            result.mobile_screenshot_path = str(mobile_path)
            if nav.frozen_hits:
                result.blocked_navigation_url = nav.frozen_hits[0]
            if nav.blocked:
                raise RobotsBlocked(nav.blocked[0])
            if page.url.split("#")[0] != doc["url"].split("#")[0]:
                raise RuntimeError("document_changed_during_capture")
        finally:
            browser.close()

    final_doc = result.final_url.split("#")[0]
    # Only meaningful when the page itself was served over HTTPS.
    if result.final_url.startswith("https://"):
        result.mixed_content_count = sum(1 for doc in mixed.values() if doc == final_doc)
    result.broken_assets = [u for u, doc in broken.items() if doc == final_doc]
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
    try:
        parsed = urlparse(url)
        domain = (parsed.hostname or "").lower()
    except ValueError:  # e.g. malformed bracketed IPv6 host
        parsed, domain = None, ""

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
        checked_tls = None
        if parsed.scheme == "https":
            checked_tls = (domain, parsed.port or 443)
            valid, expires, ssl_error = ssl_fn(*checked_tls)
            report.ssl_valid, report.ssl_expires_at, report.ssl_error = valid, expires, ssl_error
        else:
            report.ssl_error = "no_https"

        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            # No wait/record here: browse() reserves and stamps a slot per navigation itself, and a
            # second wait would reserve a second slot (an extra delay) for the same request.
            try:
                browsed = browse_fn(url, Path(output_dir), _safe_stem(domain), crawler.allowed,
                                   crawler.wait_for_domain, crawler.record_request)
            except RobotsBlocked as exc:
                report.status, report.error = "blocked", f"robots_disallow: {exc}"
                return finish(report)
            except FormPostBlocked as exc:
                report.status, report.error = "blocked", f"form_post_blocked: {exc}"
                return finish(report)
            except Exception as exc:  # noqa: BLE001 - isolation boundary
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("diagnostics attempt %d failed for %s: %s", attempt, domain, last_error)
            else:
                last_error = None
                break
            if attempt < MAX_ATTEMPTS:
                sleep(BACKOFF_SECONDS * attempt)

        if last_error is not None:
            report.error = last_error
            if "ERR_CERT" in last_error or "SSL" in last_error.upper():
                report.ssl_valid = False
                if report.ssl_error in (None, "no_https"):  # keep a more specific pre-check error
                    report.ssl_error = last_error
            return finish(report)

        # TLS must describe where the browser actually ended up (http -> https redirects).
        final = urlparse(browsed.final_url)
        if final.scheme == "https" and final.hostname:
            target = (final.hostname.lower(), final.port or 443)
            if target != checked_tls:
                report.ssl_valid, report.ssl_expires_at, report.ssl_error = ssl_fn(*target)
        elif final.scheme == "http":
            report.ssl_valid, report.ssl_expires_at, report.ssl_error = False, None, "no_https"

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
        report.blocked_navigation_url = browsed.blocked_navigation_url
        return finish(report)
    except Exception as exc:  # noqa: BLE001 - never raise
        logger.error("diagnostics crashed for %s: %s", domain, exc)
        return finish(DiagnosticReport(domain=domain, url=url, status="error", error=f"{type(exc).__name__}: {exc}"))
