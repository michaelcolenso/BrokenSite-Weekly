"""
Market saturation reports for BrokenSite-Weekly.
Generates per-city/niche summary reports from scrape batch data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from collections import Counter
from datetime import datetime

from .logging_setup import get_logger

logger = get_logger("market_reports")


def generate_market_report(
    city: str,
    category: str,
    businesses: List[Any],
    all_leads: List[Any],
    min_score: int = 40,
) -> Optional[str]:
    """
    Generate a market saturation report for a city/category batch.

    Args:
        city: Target city
        category: Business category
        businesses: Raw businesses scraped from Maps
        all_leads: All processed leads from the batch (including non-qualifying)
        min_score: Threshold for "broken/outdated"

    Returns:
        Report text or None if no data.
    """
    if not businesses:
        return None

    total = len(businesses)
    qualifying = [l for l in all_leads if l.score >= min_score]
    broken_count = len(qualifying)
    broken_pct = round((broken_count / total) * 100, 1) if total else 0

    # Common issues from qualifying leads
    all_reasons = []
    for lead in qualifying:
        reasons = lead.reasons if isinstance(lead.reasons, list) else lead.reasons.split(",")
        all_reasons.extend(r.strip() for r in reasons if r.strip())

    reason_counts = Counter(all_reasons)
    top_reasons = reason_counts.most_common(10)

    # Top competitors by review count (from raw businesses, excluding those without review_count)
    competitors = sorted(
        [b for b in businesses if getattr(b, "review_count", None)],
        key=lambda b: b.review_count or 0,
        reverse=True,
    )[:3]

    lines = [
        f"{city} - {category.title()} (Weekly Summary)",
        "━" * 40,
        f"Total businesses scraped:     {total}",
        f"With broken/outdated sites:    {broken_count} ({broken_pct}%)",
    ]

    if top_reasons:
        lines.append("Common issues:")
        for reason, count in top_reasons:
            pct = round((count / broken_count) * 100, 1) if broken_count else 0
            lines.append(f"  - {reason}: {pct}%")

    if competitors:
        lines.append("Top competitors (by reviews):")
        for i, comp in enumerate(competitors, 1):
            lines.append(f"  {i}. {comp.name} ({comp.review_count} reviews)")

    return "\n".join(lines)


def write_market_report(
    report_text: str,
    output_dir: Path,
    city: str,
    category: str,
    date_str: Optional[str] = None,
) -> Path:
    """Write a market report to disk. Returns file path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = date_str or datetime.utcnow().strftime("%Y-%m-%d")
    safe_city = city.replace(", ", "_").replace(" ", "_")
    safe_category = category.replace(" ", "_")
    filename = f"market_report_{safe_city}_{safe_category}_{date_str}.txt"
    path = output_dir / filename
    path.write_text(report_text, encoding="utf-8")
    logger.info(f"Market report written: {path}")
    return path
