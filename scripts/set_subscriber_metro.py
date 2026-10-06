#!/usr/bin/env python3
"""
Set/list/remove a subscriber's metro for BrokenSite-Weekly.

Gumroad offers no per-buyer custom field, so metros are collected out-of-band
(post-purchase email) and recorded here in the subscriber prefs store
(`data/subscriber_prefs.json`), keyed by lowercased email.

Usage:
    python scripts/set_subscriber_metro.py set <email> "<City, ST>"
    python scripts/set_subscriber_metro.py list
    python scripts/set_subscriber_metro.py remove <email>

Canonical metro format is "City, ST" (e.g. "Austin, TX"). The city string is
validated against the union of TARGET_CITIES_JSON and the distinct cities
already in the leads database. Unknown metros only produce a warning (not an
error): a brand-new metro is legitimate — it becomes a new scrape target on
the next weekly run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a plain script (python scripts/set_subscriber_metro.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.subscriber_prefs import SubscriberPrefs, SubscriberPrefsStore  # noqa: E402


def _canonical_cities(config, prefs_path: Path) -> list[str]:
    """Union of config.target_cities and distinct cities in the leads table."""
    cities: dict[str, str] = {}
    for city in config.target_cities:
        cities.setdefault(city.strip().lower(), city.strip())

    db_path = config.database.db_path
    if db_path.exists():
        try:
            # Import here so the CLI works even if the DB is missing/corrupt.
            from src.db import Database

            db = Database(config.database)
            for city in db.get_distinct_cities():
                cities.setdefault(city.strip().lower(), city.strip())
        except Exception as e:
            print(f"Warning: could not read cities from database ({db_path}): {e}",
                  file=sys.stderr)

    return sorted(cities.values())


def cmd_set(args, config) -> int:
    email = args.email.lower().strip()
    metro = args.metro.strip()
    if not metro:
        print("Error: metro must not be empty", file=sys.stderr)
        return 1

    canonical = _canonical_cities(config, args.prefs_path)
    canonical_by_lower = {c.lower(): c for c in canonical}

    if metro.lower() in canonical_by_lower:
        # Normalize to the canonical casing ("austin, tx" -> "Austin, TX").
        metro = canonical_by_lower[metro.lower()]
    else:
        print(
            f"Warning: '{metro}' is not a known metro (not in TARGET_CITIES_JSON "
            f"and no leads scraped there yet).",
            file=sys.stderr,
        )
        print(
            "It will be added as a new scrape target on the next weekly run. "
            "Canonical format is 'City, ST' (e.g. 'Austin, TX').",
            file=sys.stderr,
        )

    store = SubscriberPrefsStore(path=args.prefs_path)
    prefs = store.get(email) or SubscriberPrefs(email=email)
    prefs.cities = [metro]
    store.set(prefs)
    print(f"Set metro for {email}: {metro}")
    return 0


def cmd_list(args, config) -> int:
    store = SubscriberPrefsStore(path=args.prefs_path)
    all_prefs = sorted(store.list_all(), key=lambda p: p.email)
    if not all_prefs:
        print("No subscriber metros configured.")
        return 0

    width = max(len(p.email) for p in all_prefs)
    print(f"{'EMAIL'.ljust(width)}  METRO")
    print(f"{'-' * width}  {'-' * 20}")
    for prefs in all_prefs:
        metro = ", ".join(prefs.cities) if prefs.cities else "(unmapped)"
        print(f"{prefs.email.ljust(width)}  {metro}")
    return 0


def cmd_remove(args, config) -> int:
    email = args.email.lower().strip()
    store = SubscriberPrefsStore(path=args.prefs_path)
    prefs = store.get(email)
    if prefs is None:
        print(f"No preferences found for {email} (already unmapped).")
        return 0
    if not prefs.cities:
        print(f"{email} is already unmapped.")
        return 0
    # Clear only the metro; preserve any other prefs (niches, thresholds).
    prefs.cities = []
    store.set(prefs)
    print(f"Removed metro for {email} (now unmapped — delivery will be held).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set/list/remove subscriber metros (per-submetro delivery).",
    )
    parser.add_argument(
        "--prefs-path",
        type=Path,
        default=None,
        help="Override subscriber prefs JSON path (default: data/subscriber_prefs.json)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_set = sub.add_parser("set", help="Set/replace a subscriber's metro")
    p_set.add_argument("email")
    p_set.add_argument("metro", help='Canonical metro, e.g. "Austin, TX"')

    sub.add_parser("list", help="List email -> metro mappings")

    p_rm = sub.add_parser("remove", help="Remove a subscriber's metro (back to unmapped)")
    p_rm.add_argument("email")

    args = parser.parse_args()
    if args.prefs_path is None:
        from src.subscriber_prefs import DEFAULT_PREFS_PATH

        args.prefs_path = DEFAULT_PREFS_PATH

    config = load_config()

    if args.command == "set":
        return cmd_set(args, config)
    if args.command == "list":
        return cmd_list(args, config)
    if args.command == "remove":
        return cmd_remove(args, config)
    return 1


if __name__ == "__main__":
    sys.exit(main())
