"""
Tests for subscriber preferences system.
"""

import json
from pathlib import Path
import pytest

from src.subscriber_prefs import (
    SubscriberPrefs,
    SubscriberPrefsDefaults,
    SubscriberPrefsStore,
    filter_leads_for_subscriber,
    _looks_like_chain,
)


# ── Dataclass tests ──────────────────────────────────────────────────────────

class TestSubscriberPrefs:
    def test_defaults(self):
        prefs = SubscriberPrefs(email="test@example.com")
        assert prefs.email == "test@example.com"
        assert prefs.included_niches == []
        assert prefs.excluded_niches == []
        assert prefs.cities == []
        assert prefs.min_review_count == 0
        assert prefs.min_score == 0
        assert prefs.exclude_chains is False
        assert prefs.lead_tier_filter == []
        assert prefs.active is True

    def test_roundtrip_dict(self):
        prefs = SubscriberPrefs(
            email="test@example.com",
            included_niches=["plumber", "dentist"],
            excluded_niches=["hvac"],
            cities=["Austin, TX"],
            min_review_count=25,
            min_score=50,
            exclude_chains=True,
            lead_tier_filter=["hot", "warm"],
            active=False,
        )
        data = prefs.to_dict()
        restored = SubscriberPrefs.from_dict(data)
        assert restored.email == prefs.email
        assert restored.included_niches == prefs.included_niches
        assert restored.excluded_niches == prefs.excluded_niches
        assert restored.min_review_count == 25
        assert restored.min_score == 50
        assert restored.exclude_chains is True
        assert restored.lead_tier_filter == ["hot", "warm"]
        assert restored.active is False

    def test_from_dict_handles_missing_keys(self):
        restored = SubscriberPrefs.from_dict({"email": "a@b.com"})
        assert restored.email == "a@b.com"
        assert restored.min_review_count == 0
        assert restored.cities == []


# ── Store tests ──────────────────────────────────────────────────────────────

class TestSubscriberPrefsStore:
    def test_set_and_get(self, tmp_path):
        store = SubscriberPrefsStore(path=tmp_path / "prefs.json")
        prefs = SubscriberPrefs(email="user@test.com", min_review_count=10)
        store.set(prefs)

        got = store.get("user@test.com")
        assert got is not None
        assert got.min_review_count == 10

    def test_get_nonexistent(self, tmp_path):
        store = SubscriberPrefsStore(path=tmp_path / "prefs.json")
        assert store.get("nobody@test.com") is None

    def test_get_or_default_falls_back(self, tmp_path):
        store = SubscriberPrefsStore(path=tmp_path / "prefs.json")
        defaults = SubscriberPrefsDefaults(min_review_count=0, min_score=40)
        prefs = store.get_or_default("new@test.com", defaults=defaults)
        assert prefs.email == "new@test.com"
        assert prefs.min_score == 40  # from defaults

    def test_get_or_default_returns_stored(self, tmp_path):
        store = SubscriberPrefsStore(path=tmp_path / "prefs.json")
        stored = SubscriberPrefs(email="stored@test.com", min_score=60)
        store.set(stored)

        result = store.get_or_default(
            "stored@test.com",
            defaults=SubscriberPrefsDefaults(min_score=40),
        )
        assert result.min_score == 60  # stored overrides default

    def test_remove(self, tmp_path):
        store = SubscriberPrefsStore(path=tmp_path / "prefs.json")
        store.set(SubscriberPrefs(email="x@y.com"))
        assert store.remove("x@y.com") is True
        assert store.get("x@y.com") is None
        assert store.remove("x@y.com") is False

    def test_persistence(self, tmp_path):
        path = tmp_path / "prefs.json"
        store1 = SubscriberPrefsStore(path=path)
        store1.set(SubscriberPrefs(email="persist@test.com", min_score=75))

        # New store instance reads from disk
        store2 = SubscriberPrefsStore(path=path)
        got = store2.get("persist@test.com")
        assert got is not None
        assert got.min_score == 75

    def test_count(self, tmp_path):
        store = SubscriberPrefsStore(path=tmp_path / "prefs.json")
        assert store.count() == 0
        store.set(SubscriberPrefs(email="a@a.com"))
        store.set(SubscriberPrefs(email="b@b.com"))
        assert store.count() == 2

    def test_email_case_insensitive(self, tmp_path):
        store = SubscriberPrefsStore(path=tmp_path / "prefs.json")
        store.set(SubscriberPrefs(email="User@Example.com", min_score=80))
        got = store.get("user@example.com")
        assert got is not None
        assert got.min_score == 80


# ── Chain detection ──────────────────────────────────────────────────────────

class TestChainDetection:
    def test_detects_franchise(self):
        assert _looks_like_chain("Joe's Plumbing Franchise")

    def test_detects_chain(self):
        assert _looks_like_chain("Quick Fix Chain")

    def test_detects_multiple_locations(self):
        assert _looks_like_chain("ACME HVAC - Multiple Locations")

    def test_normal_business_not_chain(self):
        assert not _looks_like_chain("Joe's Plumbing")
        assert not _looks_like_chain("Austin Family Dentistry")


# ── Lead filtering ───────────────────────────────────────────────────────────

def _make_lead(**overrides):
    lead = {
        "place_id": "test_place",
        "name": "Test Business",
        "website": "https://test.com",
        "score": 65,
        "category": "plumber",
        "city": "Austin, TX",
        "review_count": 50,
        "lead_tier": "warm",
    }
    lead.update(overrides)
    return lead


class TestFilterLeadsForSubscriber:
    def test_no_filters_returns_all(self):
        leads = [_make_lead(), _make_lead(place_id="p2")]
        prefs = SubscriberPrefs(email="sub@test.com")
        result = filter_leads_for_subscriber(leads, prefs, global_min_score=40)
        assert len(result) == 2

    def test_min_score_filter(self):
        leads = [
            _make_lead(score=85),
            _make_lead(score=45, place_id="p2"),
            _make_lead(score=30, place_id="p3"),
        ]
        prefs = SubscriberPrefs(email="sub@test.com", min_score=50)
        result = filter_leads_for_subscriber(leads, prefs, global_min_score=40)
        assert len(result) == 1
        assert result[0]["score"] == 85

    def test_global_min_score_fallback(self):
        leads = [_make_lead(score=50), _make_lead(score=30, place_id="p2")]
        prefs = SubscriberPrefs(email="sub@test.com", min_score=0)
        result = filter_leads_for_subscriber(leads, prefs, global_min_score=50)
        assert len(result) == 1
        assert result[0]["score"] == 50

    def test_included_niches(self):
        leads = [
            _make_lead(category="plumber"),
            _make_lead(category="dentist", place_id="p2"),
        ]
        prefs = SubscriberPrefs(email="sub@test.com", included_niches=["plumber"])
        result = filter_leads_for_subscriber(leads, prefs)
        assert len(result) == 1
        assert result[0]["category"] == "plumber"

    def test_excluded_niches(self):
        leads = [
            _make_lead(category="plumber"),
            _make_lead(category="hvac", place_id="p2"),
        ]
        prefs = SubscriberPrefs(email="sub@test.com", excluded_niches=["hvac"])
        result = filter_leads_for_subscriber(leads, prefs)
        assert len(result) == 1
        assert result[0]["category"] == "plumber"

    def test_city_filter(self):
        leads = [
            _make_lead(city="Austin, TX"),
            _make_lead(city="Denver, CO", place_id="p2"),
        ]
        prefs = SubscriberPrefs(email="sub@test.com", cities=["Austin, TX"])
        result = filter_leads_for_subscriber(leads, prefs)
        assert len(result) == 1
        assert result[0]["city"] == "Austin, TX"

    def test_review_count_filter(self):
        leads = [
            _make_lead(review_count=100),
            _make_lead(review_count=5, place_id="p2"),
        ]
        prefs = SubscriberPrefs(email="sub@test.com", min_review_count=25)
        result = filter_leads_for_subscriber(leads, prefs)
        assert len(result) == 1
        assert result[0]["review_count"] == 100

    def test_review_count_none_passed_through(self):
        """Leads with no review_count should pass through the filter."""
        leads = [_make_lead(review_count=None)]
        prefs = SubscriberPrefs(email="sub@test.com", min_review_count=25)
        result = filter_leads_for_subscriber(leads, prefs)
        assert len(result) == 1

    def test_exclude_chains(self):
        leads = [
            _make_lead(name="Joe's Plumbing"),
            _make_lead(name="Quick Fix Chain", place_id="p2"),
        ]
        prefs = SubscriberPrefs(email="sub@test.com", exclude_chains=True)
        result = filter_leads_for_subscriber(leads, prefs)
        assert len(result) == 1
        assert result[0]["name"] == "Joe's Plumbing"

    def test_tier_filter(self):
        leads = [
            _make_lead(lead_tier="hot", score=85),
            _make_lead(lead_tier="warm", score=65, place_id="p2"),
            _make_lead(lead_tier="cool", score=45, place_id="p3"),
        ]
        prefs = SubscriberPrefs(email="sub@test.com", lead_tier_filter=["hot", "warm"])
        result = filter_leads_for_subscriber(leads, prefs)
        assert len(result) == 2
        tiers = {r["lead_tier"] for r in result}
        assert tiers == {"hot", "warm"}

    def test_combined_filters(self):
        leads = [
            _make_lead(score=85, category="plumber", city="Austin, TX", review_count=50),
            _make_lead(score=45, category="dentist", city="Denver, CO", review_count=10, place_id="p2"),
            _make_lead(score=75, category="plumber", city="Denver, CO", review_count=30, place_id="p3"),
        ]
        prefs = SubscriberPrefs(
            email="sub@test.com",
            included_niches=["plumber"],
            cities=["Austin, TX", "Denver, CO"],
            min_review_count=25,
            min_score=50,
        )
        result = filter_leads_for_subscriber(leads, prefs)
        assert len(result) == 2
        # p2 excluded by score + niche + review_count
        ids = {r["place_id"] for r in result}
        assert ids == {"test_place", "p3"}
