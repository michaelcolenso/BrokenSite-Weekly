#!/usr/bin/env python3
"""
Quick evaluation runner for BrokenSite-Weekly.
Scrapes a small set of queries and summarizes scoring results.
"""

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import List, Dict, Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import ScraperConfig, ScoringConfig, RetryConfig
from src.maps_scraper import scrape_with_isolation
from src.scoring import evaluate_with_isolation
from src.logging_setup import setup_logging


def bucket_score(score: int) -> str:
    start = (score // 10) * 10
    end = min(start + 9, 100)
    return f"{start:02d}-{end:02d}"


def normalize_list(values: List[str]) -> List[str]:
    return [v.strip() for v in values if v and v.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a small scrape + score evaluation")
    parser.add_argument("--cities", nargs="+", required=True, help="City names")
    parser.add_argument("--categories", nargs="+", required=True, help="Search categories")
    parser.add_argument("--max-results", type=int, default=10, help="Max results per query")
    parser.add_argument("--max-scrolls", type=int, default=5, help="Max scroll iterations")
    parser.add_argument("--timeout-ms", type=int, default=20000, help="Playwright timeout")
    parser.add_argument("--min-score", type=int, default=30, help="Min score to qualify")
    parser.add_argument("--max-retries", type=int, default=1, help="Max request retries")
    parser.add_argument("--max-leads", type=int, default=15, help="Max qualifying leads to display")
    args = parser.parse_args()

    setup_logging()

    cities = normalize_list(args.cities)
    categories = normalize_list(args.categories)
    if not cities or not categories:
        print("No cities or categories provided.")
        return 1

    scraper_cfg = ScraperConfig(
        max_results_per_query=args.max_results,
        max_scrolls=args.max_scrolls,
        timeout_ms=args.timeout_ms,
    )
    scoring_cfg = ScoringConfig(min_score_to_include=args.min_score)
    retry_cfg = RetryConfig(max_retries=args.max_retries)

    totals = Counter()
    score_buckets = Counter()
    reason_counts = Counter()
    per_query = defaultdict(lambda: Counter())
    qualifying: List[Dict[str, Any]] = []

    for city in cities:
        for category in categories:
            totals["queries_attempted"] += 1
            businesses, error = scrape_with_isolation(
                city=city,
                category=category,
                config=scraper_cfg,
            )

            if error:
                per_query[f"{category} in {city}"]["errors"] += 1
                print(f"ERROR: {category} in {city}: {error}")
                continue

            totals["queries_succeeded"] += 1
            per_query[f"{category} in {city}"]["businesses"] += len(businesses)

            for business in businesses:
                totals["businesses_found"] += 1
                if not business.website:
                    totals["no_website"] += 1
                    if scoring_cfg.include_no_website_leads:
                        score = scoring_cfg.weight_no_website
                        reasons = ["no_website"]
                    else:
                        continue
                else:
                    totals["with_website"] += 1
                    result = evaluate_with_isolation(
                        url=business.website,
                        config=scoring_cfg,
                        retry_config=retry_cfg,
                    )
                    score = result.score
                    reasons = result.reasons

                score_buckets[bucket_score(score)] += 1
                reason_counts.update(reasons)
                if "unverified" in reasons:
                    totals["unverified"] += 1

                if score >= scoring_cfg.min_score_to_include:
                    qualifying.append({
                        "name": business.name,
                        "website": business.website,
                        "city": business.city,
                        "category": business.category,
                        "score": score,
                        "reasons": ",".join(reasons),
                    })

    print("\nSummary")
    print(f"- Queries attempted: {totals['queries_attempted']}")
    print(f"- Queries succeeded: {totals['queries_succeeded']}")
    print(f"- Businesses found: {totals['businesses_found']}")
    print(f"- With website: {totals['with_website']}")
    print(f"- No website: {totals['no_website']}")
    print(f"- Unverified (capped): {totals['unverified']}")

    print("\nScore distribution (bucketed by 10s)")
    for bucket in sorted(score_buckets.keys()):
        print(f"- {bucket}: {score_buckets[bucket]}")

    print("\nTop reasons")
    for reason, count in reason_counts.most_common(10):
        print(f"- {reason}: {count}")

    qualifying = sorted(qualifying, key=lambda x: x["score"], reverse=True)
    print(f"\nQualifying leads (score >= {scoring_cfg.min_score_to_include})")
    for lead in qualifying[: args.max_leads]:
        website = lead["website"] or "no website"
        print(f"- {lead['name']} | {website} | {lead['city']} | {lead['category']} | "
              f"score={lead['score']} | {lead['reasons']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
