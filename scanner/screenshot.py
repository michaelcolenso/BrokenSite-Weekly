"""Homepage screenshot capture for Tier A/B leads."""

from __future__ import annotations

import os
from pathlib import Path

from scanner.crawl import USER_AGENT, PoliteCrawler


def capture_homepage(url: str, output_path: str | Path, *, crawler: PoliteCrawler) -> None:
    """Capture a 1280x800 JPEG screenshot using Playwright.

    Import is intentionally local so scanner checks can run without Playwright installed.

    HANDOFF hard rules 3 and 4: every document navigation (the URL, each redirect hop, any
    client-side navigation) is robots-checked and paced through the shared `crawler`, via
    GuardedNavigator. Only the browser's own sub-resource requests (CSS, images, scripts) are
    exempt from the per-domain delay, as is everything else the page itself triggers while
    rendering (media, XHR/fetch, iframe documents), under the screenshot exception in rule 4.

    Raises RobotsBlocked if robots.txt disallows the page or a redirect target.
    """
    from playwright.sync_api import sync_playwright

    from scanner.diagnostics.guard import FormPostBlocked, GuardedNavigator, RobotsBlocked

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        # BSW_CHROMIUM_PATH: optional override when Playwright's pinned build isn't installed.
        browser = p.chromium.launch(executable_path=os.environ.get("BSW_CHROMIUM_PATH") or None)
        try:
            # service_workers="block": a service worker would serve pages past the route guard.
            context = browser.new_context(viewport={"width": 1280, "height": 800}, user_agent=USER_AGENT,
                                          service_workers="block")
            page = context.new_page()
            nav = GuardedNavigator(
                context, page,
                allow_fn=crawler.allowed,
                wait_fn=crawler.wait_for_domain,
                record_fn=crawler.record_request,
                nav_timeout_ms=30000,
                wait_until="domcontentloaded",
            )
            nav.goto(url)
            nav.freeze()  # nothing may navigate away while we capture
            if nav.blocked:
                raise RobotsBlocked(nav.blocked[0])
            page.screenshot(path=str(output_path), type="jpeg", quality=70, full_page=False)
            if nav.post_hits:  # a timer submitted a form while the screenshot was being taken
                output_path.unlink(missing_ok=True)
                raise FormPostBlocked(nav.post_hits[0])
        finally:
            browser.close()
