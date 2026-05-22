"""
Competitor analysis for BrokenSite-Weekly.
Finds and scores competitor businesses from the same city/category.
"""

from __future__ import annotations

import json
from typing import List, Dict, Any, Optional

from .config import Config
from .maps_scraper import scrape_with_isolation
from .scoring import evaluate_with_isolation
from .logging_setup import get_logger

logger = get_logger("competitor_analysis")

# Cache scrape results per (city, category) to avoid redundant API calls
_scrape_cache: dict[tuple[str, str], list] = {}


def _clear_scrape_cache() -> None:
    """Clear the internal scrape cache. Useful for testing."""
    _scrape_cache.clear()


def find_competitors(
    city: str,
    category: str,
    exclude_place_id: str,
    config: Config,
    max_results: int = 15,
) -> List[Dict[str, Any]]:
    """
    Find top competitor businesses for a given city/category.
    Returns list of competitor dicts with name, website, review_count.
    """
    cache_key = (city, category)
    if cache_key not in _scrape_cache:
        businesses, error = scrape_with_isolation(
            city=city,
            category=category,
            config=config.scraper,
            max_results=max_results,
        )
        if error:
            logger.warning(f"Competitor scrape failed for {category} in {city}: {error}")
            return []
        _scrape_cache[cache_key] = businesses
    else:
        businesses = _scrape_cache[cache_key]

    competitors = [
        {
            "place_id": b.place_id,
            "name": b.name,
            "website": b.website,
            "review_count": b.review_count,
        }
        for b in businesses
        if b.place_id != exclude_place_id and b.website
    ]
    competitors.sort(key=lambda c: c.get("review_count") or 0, reverse=True)
    return competitors[:3]


def score_competitor_websites(
    competitors: List[Dict[str, Any]],
    config: Config,
) -> List[Dict[str, Any]]:
    """Score each competitor's website and enrich the dict."""
    scored = []
    for comp in competitors:
        result = evaluate_with_isolation(
            url=comp["website"],
            config=config.scoring,
            retry_config=config.retry,
        )
        comp = dict(comp)
        comp["score"] = result.score
        comp["reasons"] = result.reasons
        scored.append(comp)
    return scored


def build_competitor_summary(
    lead_score: int,
    competitors: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a gap summary comparing the lead to its competitors."""
    if not competitors:
        return {"competitors": [], "gap_text": "No competitors found"}

    avg_competitor_score = sum(c["score"] for c in competitors) / len(competitors)
    gap = lead_score - avg_competitor_score

    if gap > 20:
        gap_text = "Your site is significantly behind competitors"
    elif gap > 0:
        gap_text = "Your site trails most competitors"
    elif gap > -20:
        gap_text = "Your site is roughly on par with competitors"
    else:
        gap_text = "Your site leads competitors in quality"

    return {
        "competitors": competitors,
        "gap_text": gap_text,
        "lead_score": lead_score,
        "avg_competitor_score": round(avg_competitor_score, 1),
    }


def analyze_competitors_for_lead(
    lead,
    config: Config,
) -> Optional[str]:
    """
    Find, score, and summarize competitors for a single lead.
    Returns JSON string for storage, or None if skipped.
    """
    if not config.scraper.competitor_analysis_enabled:
        return None

    competitors = find_competitors(
        city=lead.city,
        category=lead.category,
        exclude_place_id=lead.place_id,
        config=config,
    )
    if not competitors:
        return None

    scored = score_competitor_websites(competitors, config)
    summary = build_competitor_summary(lead.score, scored)
    return json.dumps(summary)
