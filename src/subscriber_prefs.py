"""
Subscriber preference store for BrokenSite-Weekly.

Each subscriber can set filters to customize their lead exports:
  - included_niches / excluded_niches: business categories
  - cities: geographic focus
  - min_review_count: minimum Google review count
  - min_score: override the global min_score threshold
  - exclude_chains: filter out franchise/multi-location businesses
  - lead_tier_filter: only export leads of specified tiers

Preferences are stored as a JSON file keyed by subscriber email.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any, Set

from .logging_setup import get_logger

logger = get_logger("subscriber_prefs")

DEFAULT_PREFS_PATH = Path(__file__).parent.parent / "data" / "subscriber_prefs.json"


@dataclass
class SubscriberPrefs:
    """Per-subscriber lead export preferences."""
    email: str
    included_niches: List[str] = field(default_factory=list)     # empty = all niches
    excluded_niches: List[str] = field(default_factory=list)     # niches to skip
    cities: List[str] = field(default_factory=list)               # empty = all cities
    min_review_count: int = 0                                      # 0 = no filter
    min_score: int = 0                                             # 0 = use global config
    exclude_chains: bool = False
    lead_tier_filter: List[str] = field(default_factory=list)     # hot, warm, cool; empty = all
    active: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SubscriberPrefs":
        return cls(
            email=str(data.get("email", "")),
            included_niches=[str(n) for n in data.get("included_niches", []) or []],
            excluded_niches=[str(n) for n in data.get("excluded_niches", []) or []],
            cities=[str(c) for c in data.get("cities", []) or []],
            min_review_count=int(data.get("min_review_count", 0)),
            min_score=int(data.get("min_score", 0)),
            exclude_chains=bool(data.get("exclude_chains", False)),
            lead_tier_filter=[str(t) for t in data.get("lead_tier_filter", []) or []],
            active=bool(data.get("active", True)),
        )


@dataclass
class SubscriberPrefsDefaults:
    """System-wide default preferences applied when subscriber has no custom prefs."""
    min_review_count: int = 0
    min_score: int = 0
    exclude_chains: bool = False


class SubscriberPrefsStore:
    """JSON-file-backed store for subscriber preferences."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or DEFAULT_PREFS_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prefs: Dict[str, SubscriberPrefs] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._prefs = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._prefs = {
                email: SubscriberPrefs.from_dict(data)
                for email, data in raw.items()
                if isinstance(data, dict)
            }
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load subscriber prefs from {self.path}: {e}")
            self._prefs = {}

    def _save(self) -> None:
        try:
            data = {email: prefs.to_dict() for email, prefs in self._prefs.items()}
            self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.error(f"Failed to save subscriber prefs: {e}")

    def get(self, email: str) -> Optional[SubscriberPrefs]:
        """Get preferences for a subscriber, or None if not configured."""
        return self._prefs.get(email.lower().strip())

    def get_or_default(
        self, email: str, defaults: Optional[SubscriberPrefsDefaults] = None
    ) -> SubscriberPrefs:
        """Get preferences for a subscriber, falling back to defaults."""
        prefs = self.get(email)
        if prefs is not None:
            return prefs
        defaults = defaults or SubscriberPrefsDefaults()
        return SubscriberPrefs(
            email=email,
            min_review_count=defaults.min_review_count,
            min_score=defaults.min_score,
            exclude_chains=defaults.exclude_chains,
        )

    def set(self, prefs: SubscriberPrefs) -> None:
        """Set or update preferences for a subscriber."""
        email = prefs.email.lower().strip()
        self._prefs[email] = prefs
        self._save()
        logger.info(f"Updated preferences for {email}")

    def remove(self, email: str) -> bool:
        """Remove preferences for a subscriber. Returns True if existed."""
        email = email.lower().strip()
        if email in self._prefs:
            del self._prefs[email]
            self._save()
            logger.info(f"Removed preferences for {email}")
            return True
        return False

    def list_all(self) -> List[SubscriberPrefs]:
        """List all stored preferences."""
        return list(self._prefs.values())

    def count(self) -> int:
        return len(self._prefs)


# ── Lead filtering ───────────────────────────────────────────────────────────

_CHAIN_INDICATORS: Set[str] = {
    "franchise", "franchises", "chain", "multiple locations",
    "locations nationwide", "locations worldwide",
}


def _looks_like_chain(name: str) -> bool:
    """Heuristic: check if business name suggests a chain/franchise."""
    name_lower = name.lower()
    return any(indicator in name_lower for indicator in _CHAIN_INDICATORS)


def filter_leads_for_subscriber(
    leads: List[Dict[str, Any]],
    prefs: SubscriberPrefs,
    global_min_score: int = 40,
) -> List[Dict[str, Any]]:
    """
    Filter a list of lead dicts according to subscriber preferences.

    Args:
        leads: List of lead dicts (as returned by DB queries).
        prefs: Subscriber preferences.
        global_min_score: Fallback min_score when prefs.min_score is 0.

    Returns:
        Filtered list of lead dicts.
    """
    min_score = prefs.min_score if prefs.min_score > 0 else global_min_score
    included_niches = {n.lower() for n in prefs.included_niches} if prefs.included_niches else set()
    excluded_niches = {n.lower() for n in prefs.excluded_niches}
    cities = {c.lower() for c in prefs.cities} if prefs.cities else set()
    tier_filter = {t.lower() for t in prefs.lead_tier_filter} if prefs.lead_tier_filter else set()

    filtered: List[Dict[str, Any]] = []
    for lead in leads:
        # Score threshold
        score = int(lead.get("score") or 0)
        if score < min_score:
            continue

        # Niche include filter (if specified, only include matching niches)
        category = str(lead.get("category", "")).lower()
        if included_niches and category not in included_niches:
            continue

        # Niche exclude filter
        if category in excluded_niches:
            continue

        # City filter
        city = str(lead.get("city", "")).lower()
        if cities and city not in cities:
            continue

        # Review count filter
        review_count = lead.get("review_count")
        if review_count is not None and prefs.min_review_count > 0:
            try:
                if int(review_count) < prefs.min_review_count:
                    continue
            except (ValueError, TypeError):
                pass

        # Chain filter
        if prefs.exclude_chains:
            name = str(lead.get("name", ""))
            if _looks_like_chain(name):
                continue

        # Tier filter
        if tier_filter:
            lead_tier = str(lead.get("lead_tier", "")).lower()
            if lead_tier not in tier_filter:
                continue

        filtered.append(lead)

    return filtered
