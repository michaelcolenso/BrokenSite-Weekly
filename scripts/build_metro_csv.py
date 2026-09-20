"""Build data/metros/<metro>.csv from unofficial scrape exports.

Accepts Outscraper/Apify-style JSON/CSV exports or legacy maps_scraper dumps
and normalizes them to the v1 scanner input schema:

    business_name,vertical,phone,address,domain

Filtering rules:
  - rows without a website are dropped (the scanner needs a domain)
  - social/profile-only destinations (facebook.com, yelp.com, ...) are dropped
  - national chains are dropped via data/national_chain_blocklist.csv
  - dedupe by domain, then by (business_name, address)

Usage:
    python scripts/build_metro_csv.py --input raw/seattle_outscraper.json --metro seattle
    python scripts/build_metro_csv.py --input raw/maps_dump.csv --metro seattle --append
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner.__main__ import normalize_domain  # noqa: E402

logger = logging.getLogger("build_metro_csv")

FIELD_ALIASES = {
    "business_name": ("business_name", "name", "title", "company"),
    "vertical": ("vertical", "category", "type", "subtypes"),
    "phone": ("phone", "phone_number", "phoneNumber"),
    "address": ("address", "full_address", "street_address", "location"),
    # NB: bare "url" is deliberately excluded — in Outscraper exports it is
    # the Google Maps link, not the business website.
    "website": ("website", "site", "website_link", "website_url", "domain"),
}

# Domains that are never scan targets: social profiles, aggregators, link hubs.
SOCIAL_ONLY_DOMAINS = {
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "tiktok.com", "youtube.com", "yelp.com", "nextdoor.com", "linktr.ee",
    "google.com", "maps.google.com", "tripadvisor.com", "angi.com",
    "thumbtack.com", "yellowpages.com", "bbb.org", "foursquare.com",
}

OUTPUT_FIELDS = ["business_name", "vertical", "phone", "address", "domain"]


def _first_value(record: dict, aliases: tuple[str, ...]) -> str:
    for key in aliases:
        value = record.get(key)
        if isinstance(value, list):
            value = value[0] if value else ""
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _domain_matches(domain: str, listed: str) -> bool:
    return domain == listed or domain.endswith(f".{listed}")


def load_blocklist(path: str | Path) -> set[str]:
    path = Path(path)
    if not path.exists():
        logger.warning("blocklist not found at %s; chain filtering disabled", path)
        return set()
    domains = set()
    with path.open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            domain = (record.get("domain") or "").strip().lower()
            if domain:
                domains.add(domain)
    return domains


def load_records(path: str | Path) -> list[dict]:
    """Load raw scrape export as a list of dicts (JSON array or CSV)."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list):
                    data = value
                    break
        if not isinstance(data, list):
            raise ValueError(f"{path}: JSON must be a list of objects (or contain one)")
        return [row for row in data if isinstance(row, dict)]
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def normalize_records(
    records: list[dict],
    *,
    blocklist: set[str],
    stats: dict | None = None,
) -> list[dict]:
    """Normalize raw records to the v1 schema, applying drops and dedupe."""
    stats = stats if stats is not None else {}
    for key in ("no_website", "social_only", "chain", "dupe_domain", "dupe_identity"):
        stats.setdefault(key, 0)

    seen_domains: set[str] = set()
    seen_identities: set[tuple[str, str]] = set()
    rows: list[dict] = []

    for record in records:
        website = _first_value(record, FIELD_ALIASES["website"])
        domain = normalize_domain(website)
        if not domain:
            stats["no_website"] += 1
            continue
        bare = domain[4:] if domain.startswith("www.") else domain
        if any(_domain_matches(bare, social) for social in SOCIAL_ONLY_DOMAINS):
            stats["social_only"] += 1
            continue
        if any(_domain_matches(bare, chain) for chain in blocklist):
            stats["chain"] += 1
            continue
        if bare in seen_domains:
            stats["dupe_domain"] += 1
            continue

        name = _first_value(record, FIELD_ALIASES["business_name"])
        address = _first_value(record, FIELD_ALIASES["address"])
        identity = (name.lower(), address.lower())
        if name and identity in seen_identities:
            stats["dupe_identity"] += 1
            continue

        seen_domains.add(bare)
        seen_identities.add(identity)
        rows.append({
            "business_name": name,
            "vertical": _first_value(record, FIELD_ALIASES["vertical"]),
            "phone": _first_value(record, FIELD_ALIASES["phone"]),
            "address": address,
            "domain": bare,
        })
    return rows


def write_metro_csv(rows: list[dict], path: str | Path, *, append: bool = False) -> int:
    """Write metro CSV. With append=True, merge into an existing file (deduped by domain)."""
    path = Path(path)
    existing: list[dict] = []
    if append and path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            existing = list(csv.DictReader(handle))
        seen = {row["domain"] for row in existing}
        rows = [row for row in rows if row["domain"] not in seen]

    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if (append and existing) else "w"
    with path.open(mode, newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        if mode == "w":
            writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a v1 metro CSV from unofficial scrape exports.")
    parser.add_argument("--input", required=True, help="raw export file (.json or .csv)")
    parser.add_argument("--metro", required=True, help="metro name; writes <data-dir>/<metro>.csv")
    parser.add_argument("--data-dir", default="data/metros")
    parser.add_argument("--blocklist", default="data/national_chain_blocklist.csv")
    parser.add_argument("--append", action="store_true", help="merge into existing metro CSV (deduped by domain)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    records = load_records(args.input)
    blocklist = load_blocklist(args.blocklist)
    stats: dict = {}
    rows = normalize_records(records, blocklist=blocklist, stats=stats)

    output_path = Path(args.data_dir) / f"{args.metro}.csv"
    written = write_metro_csv(rows, output_path, append=args.append)

    print(f"input={len(records)} kept={written} "
          f"no_website={stats['no_website']} social_only={stats['social_only']} "
          f"chain={stats['chain']} dupe_domain={stats['dupe_domain']} "
          f"dupe_identity={stats['dupe_identity']}")
    print(f"wrote: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
