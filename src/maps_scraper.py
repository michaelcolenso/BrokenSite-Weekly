"""
Hardened Google Maps scraper using Playwright.
Designed for unattended VPS operation with robust error handling.
"""

import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any
from dataclasses import dataclass

from playwright.sync_api import (
    sync_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeout,
    Error as PlaywrightError,
)

from .config import ScraperConfig, DEBUG_DIR
from .logging_setup import get_logger

logger = get_logger("maps_scraper")


@dataclass
class Business:
    """Scraped business data."""
    place_id: str
    cid: Optional[str]
    name: str
    website: Optional[str]
    address: Optional[str]
    phone: Optional[str]
    city: str
    category: str


class ScraperError(Exception):
    """Base exception for scraper errors."""
    pass


class ConsentError(ScraperError):
    """Failed to handle consent dialog."""
    pass


class NoResultsError(ScraperError):
    """No results found for query."""
    pass


# Multiple selector strategies for resilience
SELECTORS = {
    # Consent dialog buttons (Google cookie consent)
    "consent_buttons": [
        "button:has-text('Accept all')",
        "button:has-text('Reject all')",
        "button:has-text('Accept')",
        "[aria-label='Accept all']",
        "[aria-label='Accept cookies']",
        "form[action*='consent'] button",
    ],
    # Results feed container
    "results_feed": [
        "div[role='feed']",
        "div[aria-label*='Results']",
        "div.m6QErb[aria-label]",
    ],
    # Individual business cards in results
    "business_cards": [
        "div[role='feed'] > div > div[jsaction]",
        "div[role='article']",
        "a[href*='/maps/place/']",
    ],
    # Business name in detail panel
    "business_name": [
        "h1.DUwDvf",
        "h1[data-attrid='title']",
        "div[role='main'] h1",
        "h1",
    ],
    # Website link in detail panel
    "website_link": [
        "a[data-item-id='authority']",
        "a[aria-label*='Website']",
        "a[href]:has-text('Website')",
        "a.CsEnBe[href*='http']",
    ],
    # Address
    "address": [
        "button[data-item-id='address']",
        "[data-item-id='address']",
        "button[aria-label*='Address']",
    ],
    # Phone
    "phone": [
        "button[data-item-id*='phone']",
        "[data-item-id*='phone']",
        "button[aria-label*='Phone']",
    ],
}


def _try_selectors(page: Page, selector_list: List[str], timeout: int = 5000) -> Optional[Any]:
    """Try multiple selectors, return first match or None."""
    for selector in selector_list:
        try:
            element = page.wait_for_selector(selector, timeout=timeout, state="visible")
            if element:
                return element
        except PlaywrightTimeout:
            continue
        except PlaywrightError:
            continue
    return None


def _query_all_selectors(page: Page, selector_list: List[str]) -> List[Any]:
    """Try multiple selectors, return all matches from first working selector."""
    for selector in selector_list:
        try:
            elements = page.query_selector_all(selector)
            if elements:
                return elements
        except PlaywrightError:
            continue
    return []


def _extract_place_id_from_url(url: str) -> Optional[str]:
    """Extract place_id from Google Maps URL."""
    # Pattern: /maps/place/.../data=...!1s0x...:0x...
    # The place_id is often in the data parameter
    match = re.search(r'!1s(0x[a-f0-9]+:0x[a-f0-9]+)', url)
    if match:
        return match.group(1)

    # Alternative: ChIJ... format
    match = re.search(r'place_id[=:]([A-Za-z0-9_-]+)', url)
    if match:
        return match.group(1)

    # Fallback: use URL hash as pseudo-ID
    match = re.search(r'/place/([^/]+)/', url)
    if match:
        return f"url_hash_{hash(match.group(1)) & 0xFFFFFFFF:08x}"

    return None


def _extract_cid_from_url(url: str) -> Optional[str]:
    """Extract CID from Google Maps URL."""
    match = re.search(r'cid[=:](\d+)', url)
    if match:
        return match.group(1)
    return None


def _save_debug_dump(page: Page, context: str, config: ScraperConfig):
    """Save screenshot and HTML for debugging failures."""
    if not (config.screenshot_on_failure or config.html_dump_on_failure):
        return

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_context = re.sub(r'[^\w\-]', '_', context)[:50]

    try:
        if config.screenshot_on_failure:
            screenshot_path = DEBUG_DIR / f"{timestamp}_{safe_context}.png"
            page.screenshot(path=str(screenshot_path), full_page=True)
            logger.debug(f"Saved screenshot: {screenshot_path}")

        if config.html_dump_on_failure:
            html_path = DEBUG_DIR / f"{timestamp}_{safe_context}.html"
            html_path.write_text(page.content(), encoding="utf-8")
            logger.debug(f"Saved HTML: {html_path}")
    except Exception as e:
        logger.warning(f"Failed to save debug dump: {e}")


def _handle_consent(page: Page, config: ScraperConfig) -> bool:
    """
    Handle Google consent dialogs.
    Returns True if consent was handled or not needed.
    """
    time.sleep(1)  # Brief wait for consent dialog to appear

    for selector in SELECTORS["consent_buttons"]:
        try:
            button = page.query_selector(selector)
            if button and button.is_visible():
                button.click()
                logger.info(f"Clicked consent button: {selector}")
                time.sleep(1)
                return True
        except PlaywrightError:
            continue

    # No consent dialog found - that's OK
    return True


def _scroll_results(page: Page, config: ScraperConfig) -> int:
    """
    Scroll the results feed to load more businesses.
    Returns number of scroll iterations completed.
    """
    feed = _try_selectors(page, SELECTORS["results_feed"], timeout=config.timeout_ms)
    if not feed:
        logger.warning("Could not find results feed to scroll")
        return 0

    scroll_count = 0
    last_count = 0
    no_change_count = 0

    for i in range(config.max_scrolls):
        try:
            # Scroll within the feed element
            feed.evaluate("el => el.scrollBy(0, 1000)")
            time.sleep(config.scroll_pause_ms / 1000)

            # Check if new results loaded
            cards = _query_all_selectors(page, SELECTORS["business_cards"])
            current_count = len(cards)

            if current_count == last_count:
                no_change_count += 1
                if no_change_count >= 3:
                    logger.debug(f"Scrolling stopped: no new results after {i+1} scrolls")
                    break
            else:
                no_change_count = 0

            last_count = current_count
            scroll_count = i + 1

            # Stop if we have enough
            if current_count >= config.max_results_per_query:
                logger.debug(f"Reached max results: {current_count}")
                break

        except PlaywrightError as e:
            logger.warning(f"Scroll error at iteration {i}: {e}")
            break

    return scroll_count


def _extract_business_details(page: Page, city: str, category: str, config: ScraperConfig) -> Optional[Business]:
    """Extract business details from the detail panel."""
    try:
        # Wait for detail panel to load
        time.sleep(1.5)

        # Get current URL for place_id extraction
        current_url = page.url
        place_id = _extract_place_id_from_url(current_url)
        cid = _extract_cid_from_url(current_url)

        if not place_id:
            # Generate fallback ID from URL
            place_id = f"fallback_{hash(current_url) & 0xFFFFFFFFFFFF:012x}"

        # Extract name
        name_el = _try_selectors(page, SELECTORS["business_name"], timeout=3000)
        name = name_el.inner_text().strip() if name_el else None

        if not name:
            logger.debug("Could not extract business name")
            return None

        # Extract website
        website = None
        website_el = _try_selectors(page, SELECTORS["website_link"], timeout=2000)
        if website_el:
            website = website_el.get_attribute("href")
            # Clean Google redirect URLs
            if website and "google.com/url" in website:
                parsed = urllib.parse.urlparse(website)
                params = urllib.parse.parse_qs(parsed.query)
                if "q" in params:
                    website = params["q"][0]

        # Extract address
        address = None
        address_el = _try_selectors(page, SELECTORS["address"], timeout=1000)
        if address_el:
            address = address_el.get_attribute("aria-label") or address_el.inner_text()
            address = address.replace("Address: ", "").strip() if address else None

        # Extract phone
        phone = None
        phone_el = _try_selectors(page, SELECTORS["phone"], timeout=1000)
        if phone_el:
            phone = phone_el.get_attribute("aria-label") or phone_el.inner_text()
            phone = phone.replace("Phone: ", "").strip() if phone else None

        return Business(
            place_id=place_id,
            cid=cid,
            name=name,
            website=website,
            address=address,
            phone=phone,
            city=city,
            category=category,
        )

    except PlaywrightError as e:
        logger.debug(f"Error extracting business details: {e}")
        return None


def scrape_businesses(
    city: str,
    category: str,
    config: ScraperConfig = None,
    max_results: int = None,
) -> List[Business]:
    """
    Scrape businesses from Google Maps for a city/category combination.

    Args:
        city: Target city (e.g., "Austin, TX")
        category: Business category (e.g., "plumber")
        config: Scraper configuration
        max_results: Override max results per query

    Returns:
        List of Business objects
    """
    config = config or ScraperConfig()
    max_results = max_results or config.max_results_per_query

    query = f"{category} in {city}"
    encoded_query = urllib.parse.quote(query)
    url = f"https://www.google.com/maps/search/{encoded_query}"

    logger.info(f"Scraping: {query}")
    results: List[Business] = []

    # Use context manager pattern to ensure proper cleanup
    with sync_playwright() as playwright:
        browser: Optional[Browser] = None
        context: Optional[BrowserContext] = None

        try:
            browser = playwright.chromium.launch(
                headless=config.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ]
            )

            context = browser.new_context(
                user_agent=config.user_agent,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/Chicago",
            )

            # Set default timeout
            context.set_default_timeout(config.timeout_ms)

            page = context.new_page()

            # Navigate to Maps
            page.goto(url, wait_until="domcontentloaded")
            logger.debug(f"Navigated to: {url}")

            # Handle consent dialog
            _handle_consent(page, config)

            # Wait for results to appear
            feed = _try_selectors(page, SELECTORS["results_feed"], timeout=config.timeout_ms)
            if not feed:
                logger.warning(f"No results feed found for: {query}")
                _save_debug_dump(page, f"no_feed_{query}", config)
                return results

            # Scroll to load more results
            _scroll_results(page, config)

            # Get all business cards
            cards = _query_all_selectors(page, SELECTORS["business_cards"])
            logger.info(f"Found {len(cards)} business cards for: {query}")

            if not cards:
                _save_debug_dump(page, f"no_cards_{query}", config)
                return results

            # Process each card
            seen_place_ids = set()

            for i, card in enumerate(cards[:max_results]):
                try:
                    # Click on card to open detail panel
                    card.click()
                    time.sleep(1)

                    # Extract details
                    business = _extract_business_details(page, city, category, config)

                    if business and business.place_id not in seen_place_ids:
                        seen_place_ids.add(business.place_id)
                        results.append(business)
                        logger.debug(f"Extracted: {business.name} ({business.website or 'no website'})")

                except PlaywrightError as e:
                    logger.debug(f"Error processing card {i}: {e}")
                    continue

                # Brief pause between cards
                time.sleep(0.5)

            logger.info(f"Successfully scraped {len(results)} businesses for: {query}")
            return results

        except PlaywrightTimeout as e:
            logger.error(f"Timeout scraping {query}: {e}")
            if 'page' in locals():
                _save_debug_dump(page, f"timeout_{query}", config)
            raise ScraperError(f"Timeout: {e}")

        except PlaywrightError as e:
            logger.error(f"Playwright error scraping {query}: {e}")
            if 'page' in locals():
                _save_debug_dump(page, f"error_{query}", config)
            raise ScraperError(f"Playwright error: {e}")

        except Exception as e:
            logger.error(f"Unexpected error scraping {query}: {e}")
            if 'page' in locals():
                _save_debug_dump(page, f"unexpected_{query}", config)
            raise

        finally:
            # Cleanup browser and context (playwright cleanup is handled by context manager)
            try:
                if context:
                    context.close()
                if browser:
                    browser.close()
            except Exception as e:
                logger.warning(f"Error during cleanup: {e}")


def scrape_with_isolation(
    city: str,
    category: str,
    config: ScraperConfig = None,
) -> tuple[List[Business], Optional[str]]:
    """
    Scrape with error isolation - returns results and error message.
    Never raises exceptions to caller.
    """
    try:
        results = scrape_businesses(city, category, config)
        return results, None
    except Exception as e:
        logger.error(f"Isolated scrape error for {category} in {city}: {e}")
        return [], str(e)
