"""
Yelp scraper for BrokenSite-Weekly.

Cross-references Google Maps leads against Yelp for:
  - Business verification (does this business exist on Yelp?)
  - Rating comparison (Google vs Yelp)
  - Review count enrichment
  - Website and phone verification

No API key required — uses Playwright (same stack as maps_scraper.py).
Opt-in via YELP_ENABLED environment variable.

Yelp is more aggressive with bot detection than Google Maps, so:
  - Longer delays between actions
  - Multi-strategy selectors with broad fallbacks
  - Stealth browser config
  - Graceful degradation: blocking never crashes the run
"""

from __future__ import annotations

import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional, Any, Tuple

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

logger = get_logger("yelp_scraper")


@dataclass
class YelpBusiness:
    """Yelp business data for cross-reference."""
    name: str
    yelp_url: str
    rating: Optional[float] = None
    review_count: Optional[int] = None
    website: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    categories: Optional[List[str]] = None
    is_claimed: bool = False
    price_level: Optional[str] = None  # $, $$, $$$, $$$$
    matched_lead_place_id: Optional[str] = None


class YelpError(Exception):
    """Yelp scraper error."""
    pass


# ── Selectors (multi-strategy for resilience) ────────────────────────────────

YELP_SELECTORS = {
    "search_input": [
        "input[name='find_desc']",
        "input[aria-label*='search']",
        "input#search_description",
    ],
    "location_input": [
        "input[name='find_loc']",
        "input[aria-label*='near']",
        "input#search_location",
    ],
    "search_button": [
        "button[type='submit']",
        "button:has-text('Search')",
        "[aria-label*='Search']",
    ],
    "result_cards": [
        "div[data-testid='serp-ia-card']",
        "li div[class*='container'] a[href*='/biz/']",
        "div[class*='businessName']",
        "h3 a[href*='/biz/']",
        "a[href*='/biz/'][class*='css']",
    ],
    "business_name_page": [
        "h1",
        "h1[class*='heading']",
        "[data-testid='business-name']",
    ],
    "rating": [
        "span[data-testid='rating']",
        "div[aria-label*='star rating']",
        "div[class*='rating'] span",
    ],
    "review_count": [
        "a[href*='/biz/'][href*='#reviews']",
        "span:has-text('review')",
        "a:has-text('review')",
        "p:has-text('review')",
    ],
    "website": [
        "a[href*='biz_redir'][href*='url=']",
        "a:has-text('Website')",
        "a:has-text('Business website')",
        "p:has-text('Website') + a",
    ],
    "phone": [
        "p:has-text('Phone number') + p",
        "a[href^='tel:']",
        "p:has-text('(')",
    ],
    "address": [
        "address",
        "p:has-text('Get Directions')",
        "a[href*='maps']",
    ],
    "categories": [
        "span[data-testid='category']",
        "a[href*='/search?c=']",
        "a[class*='category']",
    ],
    "claim_badge": [
        "span:has-text('Claimed')",
        "div[aria-label*='Claimed']",
        "[class*='claim']",
    ],
    "price_level": [
        "span:has-text('$')",
        "[aria-label*='price']",
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


def _extract_rating(text: str) -> Optional[float]:
    """Extract numeric rating from text like '4.5 star rating' or just '4.5'."""
    if not text:
        return None
    match = re.search(r"(\d+\.?\d*)", text)
    if match:
        rating = float(match.group(1))
        if 1.0 <= rating <= 5.0:
            return rating
    return None


def _extract_review_count(text: str) -> Optional[int]:
    """Extract review count from text like '123 reviews' or '1.2k reviews'."""
    if not text:
        return None
    # Handle "1.2k reviews" style
    k_match = re.search(r"(\d+\.?\d*)\s*k\s*review", text, re.IGNORECASE)
    if k_match:
        return int(float(k_match.group(1)) * 1000)
    # Standard "123 reviews"
    match = re.search(r"(\d[\d,]*)\s*review", text, re.IGNORECASE)
    if match:
        return int(match.group(1).replace(",", ""))
    # Just a number near "review"
    match = re.search(r"(\d[\d,]*)", text)
    if match:
        num = int(match.group(1).replace(",", ""))
        if 1 <= num <= 100000:
            return num
    return None


def _handle_yelp_popups(page: Page) -> bool:
    """Handle Yelp cookie consent, location prompts, and app banners."""
    time.sleep(1.5)

    # Cookie consent
    consent_selectors = [
        "button:has-text('Accept')",
        "button:has-text('Accept All')",
        "button:has-text('OK')",
        "button:has-text('Got it')",
        "[data-tracking*='consent'] button",
        "#onetrust-accept-btn-handler",
    ]
    for sel in consent_selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                time.sleep(0.5)
                break
        except PlaywrightError:
            continue

    # App banner dismissal
    banner_selectors = [
        "button[aria-label='Close']",
        "button:has-text('Not now')",
        "button:has-text('Maybe later')",
        "span[aria-label='Close']",
        "[class*='dismiss']",
        "[class*='close']",
    ]
    for sel in banner_selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                time.sleep(0.3)
                break
        except PlaywrightError:
            continue

    return True


def _normalize_name(name: str) -> str:
    """Normalize business name for matching: lowercase, strip punctuation."""
    name = name.lower().strip()
    name = re.sub(r"[^\w\s]", "", name)  # Remove punctuation, not replace with space
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _names_match(lead_name: str, yelp_name: str, threshold: float = 0.7) -> bool:
    """Check if a Yelp business name plausibly matches a lead name."""
    lead = _normalize_name(lead_name)
    yelp = _normalize_name(yelp_name)

    if lead == yelp:
        return True

    # One contains the other
    if lead in yelp or yelp in lead:
        return True

    # Word overlap ratio
    lead_words = set(lead.split())
    yelp_words = set(yelp.split())
    if not lead_words or not yelp_words:
        return False

    overlap = len(lead_words & yelp_words)
    ratio = overlap / max(len(lead_words), len(yelp_words))
    return ratio >= threshold


def search_yelp_business(
    page: Page,
    business_name: str,
    city: str,
) -> Optional[str]:
    """
    Search Yelp for a business and return the business page URL if found.

    Args:
        page: Playwright page (already on yelp.com).
        business_name: Name of the business to search for.
        city: City to narrow the search.

    Returns:
        Yelp business URL or None.
    """
    try:
        # Navigate to Yelp search
        query = f"{business_name} {city}"
        encoded = urllib.parse.quote(query)
        search_url = f"https://www.yelp.com/search?find_desc={encoded}"

        page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
        time.sleep(2)

        _handle_yelp_popups(page)

        # Look for result cards
        cards = _query_all_selectors(page, YELP_SELECTORS["result_cards"])
        if not cards:
            logger.debug(f"No Yelp results for: {business_name}")
            return None

        # Check first 5 results for a name match
        for card in cards[:5]:
            try:
                card_text = card.inner_text()
                card_name = card_text.split("\n")[0].strip() if card_text else ""

                if _names_match(business_name, card_name):
                    # Get the business URL
                    href = card.get_attribute("href")
                    if not href:
                        link = card.query_selector("a[href*='/biz/']")
                        href = link.get_attribute("href") if link else None

                    if href:
                        if href.startswith("/biz/"):
                            href = f"https://www.yelp.com{href.split('?')[0]}"
                        logger.debug(f"Yelp match: {business_name} → {href}")
                        return href
            except PlaywrightError:
                continue

        logger.debug(f"No Yelp match for: {business_name}")
        return None

    except Exception as e:
        logger.debug(f"Yelp search error for {business_name}: {e}")
        return None


def extract_yelp_business(
    page: Page,
    yelp_url: str,
) -> Optional[YelpBusiness]:
    """
    Extract business details from a Yelp business page.

    Args:
        page: Playwright page.
        yelp_url: Full Yelp business page URL.

    Returns:
        YelpBusiness or None.
    """
    try:
        page.goto(yelp_url, wait_until="domcontentloaded", timeout=15000)
        time.sleep(2)

        _handle_yelp_popups(page)

        # Name
        name_el = _try_selectors(page, YELP_SELECTORS["business_name_page"], timeout=3000)
        name = name_el.inner_text().strip() if name_el else None
        if not name:
            return None

        # Rating
        rating = None
        rating_el = _try_selectors(page, YELP_SELECTORS["rating"], timeout=2000)
        if rating_el:
            rating_text = rating_el.get_attribute("aria-label") or rating_el.inner_text()
            rating = _extract_rating(rating_text)

        # Review count
        review_count = None
        review_el = _try_selectors(page, YELP_SELECTORS["review_count"], timeout=2000)
        if review_el:
            review_text = review_el.get_attribute("aria-label") or review_el.inner_text()
            review_count = _extract_review_count(review_text)

        # Website
        website = None
        website_el = _try_selectors(page, YELP_SELECTORS["website"], timeout=2000)
        if website_el:
            href = website_el.get_attribute("href")
            if href:
                # Yelp redirect URLs contain the real URL in the 'url' param
                if "biz_redir" in href or "url=" in href:
                    parsed = urllib.parse.urlparse(href)
                    params = urllib.parse.parse_qs(parsed.query)
                    website = params.get("url", [href])[0]
                elif href.startswith("http"):
                    website = href

        # Phone
        phone = None
        phone_el = _try_selectors(page, YELP_SELECTORS["phone"], timeout=2000)
        if phone_el:
            raw = phone_el.get_attribute("href") or phone_el.inner_text()
            phone = raw.replace("tel:", "").strip() if raw else None

        # Address
        address = None
        addr_el = _try_selectors(page, YELP_SELECTORS["address"], timeout=2000)
        if addr_el:
            address = addr_el.inner_text().strip() if addr_el else None

        # Categories
        categories = None
        cat_els = _query_all_selectors(page, YELP_SELECTORS["categories"])
        if cat_els:
            categories = [el.inner_text().strip() for el in cat_els[:10] if el.inner_text().strip()]

        # Claimed badge
        is_claimed = False
        claim_el = _try_selectors(page, YELP_SELECTORS["claim_badge"], timeout=1000)
        if claim_el:
            is_claimed = True

        # Price level
        price_level = None
        price_el = _try_selectors(page, YELP_SELECTORS["price_level"], timeout=1000)
        if price_el:
            price_text = price_el.inner_text().strip()
            if price_text and "$" in price_text:
                price_level = price_text

        return YelpBusiness(
            name=name,
            yelp_url=yelp_url,
            rating=rating,
            review_count=review_count,
            website=website,
            phone=phone,
            address=address,
            categories=categories,
            is_claimed=is_claimed,
            price_level=price_level,
        )

    except PlaywrightTimeout:
        logger.debug(f"Timeout extracting Yelp business: {yelp_url}")
        return None
    except Exception as e:
        logger.debug(f"Error extracting Yelp business {yelp_url}: {e}")
        return None


def cross_reference_leads(
    leads: List[Dict[str, Any]],
    config: ScraperConfig,
    max_leads: int = 25,
) -> List[YelpBusiness]:
    """
    Cross-reference a batch of leads against Yelp.

    Args:
        leads: List of lead dicts with 'name' and 'city'.
        config: ScraperConfig with Yelp settings.
        max_leads: Maximum number of leads to check (controls runtime).

    Returns:
        List of YelpBusiness results (only for matched leads).
    """
    if not leads:
        return []

    results: List[YelpBusiness] = []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=config.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )

            context = browser.new_context(
                user_agent=config.user_agent,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/Chicago",
            )
            context.set_default_timeout(20000)

            page = context.new_page()

            # First navigate to Yelp to establish session
            try:
                page.goto("https://www.yelp.com", wait_until="domcontentloaded", timeout=15000)
                time.sleep(2)
                _handle_yelp_popups(page)
            except Exception:
                pass  # If Yelp homepage fails, try searching directly

            checked = 0
            for lead in leads[:max_leads]:
                name = lead.get("name", "")
                city = lead.get("city", "")
                if not name:
                    continue

                try:
                    yelp_url = search_yelp_business(page, name, city)
                    if not yelp_url:
                        checked += 1
                        continue

                    yelp_biz = extract_yelp_business(page, yelp_url)
                    if yelp_biz:
                        yelp_biz.matched_lead_place_id = lead.get("place_id")
                        results.append(yelp_biz)
                        logger.debug(
                            f"Yelp cross-ref: {name} | "
                            f"rating={yelp_biz.rating} reviews={yelp_biz.review_count}"
                        )

                    checked += 1
                    # Longer delay between businesses to avoid detection
                    time.sleep(2)

                except Exception as e:
                    logger.debug(f"Yelp cross-ref error for {name}: {e}")
                    checked += 1
                    continue

            context.close()
            browser.close()

    except Exception as e:
        logger.warning(f"Yelp cross-reference session error: {e}")

    logger.info(
        f"Yelp cross-reference: checked {min(len(leads), max_leads)} leads, "
        f"found {len(results)} matches"
    )
    return results


def apply_yelp_scoring(
    lead: Dict[str, Any],
    yelp_biz: "YelpBusiness",
    config: "ScoringConfig",
) -> Tuple[int, List[str]]:
    """
    Apply Yelp-based scoring signals to a lead.

    Returns (score_delta, new_reasons).
    """
    score_delta = 0
    new_reasons: List[str] = []

    # Low Yelp rating signal
    if yelp_biz.rating is not None and yelp_biz.rating < config.yelp_low_rating_threshold:
        score_delta += config.weight_yelp_low_rating
        new_reasons.append(f"yelp_low_rating_{yelp_biz.rating}")

    # Review count mismatch (Google vs Yelp)
    google_reviews = lead.get("review_count")
    if google_reviews is not None and yelp_biz.review_count is not None:
        try:
            gr = int(google_reviews)
            yr = int(yelp_biz.review_count)
            if gr > 0 and yr > 0:
                ratio = max(gr, yr) / min(gr, yr)
                if ratio > 3:
                    score_delta += config.weight_yelp_review_mismatch
                    new_reasons.append(
                        f"yelp_review_mismatch_google_{gr}_yelp_{yr}"
                    )
        except (ValueError, TypeError, ZeroDivisionError):
            pass

    return score_delta, new_reasons


def cross_reference_with_isolation(
    leads: List[Dict[str, Any]],
    config: ScraperConfig,
    max_leads: int = 25,
) -> Tuple[List[YelpBusiness], Optional[str]]:
    """Never raises — returns (results, error_message)."""
    try:
        results = cross_reference_leads(leads, config, max_leads=max_leads)
        return results, None
    except Exception as e:
        logger.error(f"Yelp cross-reference isolated error: {e}")
        return [], str(e)
