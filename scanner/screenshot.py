"""Homepage screenshot capture for Tier A/B leads."""

from __future__ import annotations

from pathlib import Path

from scanner.crawl import USER_AGENT


def capture_homepage(url: str, output_path: str | Path) -> None:
    """Capture a 1280x800 JPEG screenshot using Playwright.

    Import is intentionally local so scanner checks can run without Playwright installed.
    """
    from playwright.sync_api import sync_playwright

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800}, user_agent=USER_AGENT)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.screenshot(path=str(output_path), type="jpeg", quality=70, full_page=False)
        browser.close()
