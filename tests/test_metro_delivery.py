"""
Tests for per-subscriber metro delivery (scrape-set derivation, exclusive
per-metro caps, claimed-metro exclusion, unmapped holds).
"""

import pytest

from src.gumroad import Subscriber, _dedupe_by_email, enforce_exclusive_caps
from src.run_weekly import derive_scrape_cities, partition_subscribers_by_metro
from src.subscriber_prefs import SubscriberPrefs, SubscriberPrefsStore


def _sub(email: str, tier: str = "basic", created_at: str = "2026-01-01T00:00:00Z") -> Subscriber:
    return Subscriber(
        email=email,
        subscriber_id=f"sub_{email}",
        created_at=created_at,
        status="alive",
        tier=tier,
    )


@pytest.fixture
def prefs_store(tmp_path):
    return SubscriberPrefsStore(path=tmp_path / "prefs.json")


def _map(store: SubscriberPrefsStore, email: str, *metros: str) -> None:
    store.set(SubscriberPrefs(email=email, cities=list(metros)))


# ── Scrape-set derivation ────────────────────────────────────────────────────

class TestDeriveScrapeCities:
    def test_union_of_mapped_metros(self, prefs_store):
        subs = [_sub("a@x.com"), _sub("b@x.com"), _sub("c@x.com")]
        _map(prefs_store, "a@x.com", "Austin, TX")
        _map(prefs_store, "b@x.com", "Denver, CO")
        _map(prefs_store, "c@x.com", "Austin, TX")  # duplicate metro

        cities, fell_back = derive_scrape_cities(
            subs, prefs_store, ["Phoenix, AZ"]
        )
        assert cities == ["Austin, TX", "Denver, CO"]
        assert fell_back is False

    def test_fallback_to_target_cities_when_unmapped(self, prefs_store):
        subs = [_sub("a@x.com"), _sub("b@x.com")]
        fallback = ["Austin, TX", "Denver, CO", "Phoenix, AZ"]
        cities, fell_back = derive_scrape_cities(subs, prefs_store, fallback)
        assert cities == fallback
        assert fell_back is True

    def test_no_subscribers_falls_back(self, prefs_store):
        cities, fell_back = derive_scrape_cities([], prefs_store, ["Austin, TX"])
        assert cities == ["Austin, TX"]
        assert fell_back is True

    def test_zero_subscriber_metros_excluded(self, prefs_store):
        """A configured city with no subscribers is not scraped."""
        subs = [_sub("a@x.com")]
        _map(prefs_store, "a@x.com", "Austin, TX")
        cities, fell_back = derive_scrape_cities(
            subs, prefs_store, ["Austin, TX", "Denver, CO", "Phoenix, AZ"]
        )
        assert cities == ["Austin, TX"]
        assert "Denver, CO" not in cities
        assert "Phoenix, AZ" not in cities
        assert fell_back is False

    def test_metro_matching_case_insensitive(self, prefs_store):
        subs = [_sub("a@x.com"), _sub("b@x.com")]
        _map(prefs_store, "a@x.com", "Austin, TX")
        _map(prefs_store, "b@x.com", "austin, tx")
        cities, fell_back = derive_scrape_cities(subs, prefs_store, ["Denver, CO"])
        assert len(cities) == 1
        assert fell_back is False


# ── Subscriber partition (holds + claimed metros) ────────────────────────────

class TestPartitionSubscribersByMetro:
    def test_unmapped_subscriber_held(self, prefs_store):
        subs = [_sub("mapped@x.com", "pro"), _sub("unmapped@x.com", "pro")]
        _map(prefs_store, "mapped@x.com", "Austin, TX")

        groups, held = partition_subscribers_by_metro(subs, prefs_store)

        assert list(groups.keys()) == [("pro", "Austin, TX")]
        assert [s.email for s in groups[("pro", "Austin, TX")]] == ["mapped@x.com"]
        assert len(held) == 1
        assert held[0]["email"] == "unmapped@x.com"
        assert "no metro mapped" in held[0]["reason"]

    def test_groups_by_tier_and_metro(self, prefs_store):
        subs = [
            _sub("p1@x.com", "pro"),
            _sub("p2@x.com", "pro"),
            _sub("b1@x.com", "basic"),
        ]
        _map(prefs_store, "p1@x.com", "Austin, TX")
        _map(prefs_store, "p2@x.com", "Austin, TX")
        _map(prefs_store, "b1@x.com", "Denver, CO")

        groups, held = partition_subscribers_by_metro(subs, prefs_store)

        assert held == []
        assert {s.email for s in groups[("pro", "Austin, TX")]} == {"p1@x.com", "p2@x.com"}
        assert [s.email for s in groups[("basic", "Denver, CO")]] == ["b1@x.com"]

    def test_claimed_metro_excludes_basic_and_pro(self, prefs_store):
        subs = [
            _sub("vip@x.com", "exclusive"),
            _sub("basic@x.com", "basic"),
            _sub("pro@x.com", "pro"),
        ]
        _map(prefs_store, "vip@x.com", "Austin, TX")
        _map(prefs_store, "basic@x.com", "Austin, TX")
        _map(prefs_store, "pro@x.com", "Austin, TX")

        groups, held = partition_subscribers_by_metro(subs, prefs_store)

        assert list(groups.keys()) == [("exclusive", "Austin, TX")]
        held_emails = {h["email"] for h in held}
        assert held_emails == {"basic@x.com", "pro@x.com"}
        for h in held:
            assert "claimed by the exclusive tier" in h["reason"]

    def test_claimed_metro_other_metros_unaffected(self, prefs_store):
        subs = [
            _sub("vip@x.com", "exclusive"),
            _sub("basic@x.com", "basic"),
        ]
        _map(prefs_store, "vip@x.com", "Austin, TX")
        _map(prefs_store, "basic@x.com", "Denver, CO")

        groups, held = partition_subscribers_by_metro(subs, prefs_store)
        assert held == []
        assert ("basic", "Denver, CO") in groups

    def test_unmapped_exclusive_subscriber_held(self, prefs_store):
        subs = [_sub("vip@x.com", "exclusive")]
        groups, held = partition_subscribers_by_metro(subs, prefs_store)
        assert groups == {}
        assert len(held) == 1
        assert held[0]["tier"] == "exclusive"


# ── Exclusive tier ───────────────────────────────────────────────────────────

class TestExclusiveTier:
    def test_dedupe_keeps_exclusive_over_pro(self):
        subs = [
            _sub("dual@x.com", "pro", created_at="2026-01-01T00:00:00Z"),
            _sub("dual@x.com", "exclusive", created_at="2026-02-01T00:00:00Z"),
            _sub("other@x.com", "basic"),
        ]
        result = _dedupe_by_email(subs)
        by_email = {s.email: s for s in result}
        assert by_email["dual@x.com"].tier == "exclusive"
        assert by_email["other@x.com"].tier == "basic"

    def test_per_metro_cap_keeps_earliest_three(self, prefs_store):
        subs = [
            _sub(f"e{i}@x.com", "exclusive", created_at=f"2026-01-0{i}T00:00:00Z")
            for i in range(1, 5)  # e1 earliest ... e4 latest
        ]
        for s in subs:
            _map(prefs_store, s.email, "Austin, TX")

        allowed, held = enforce_exclusive_caps(subs, prefs_store, cap=3)

        allowed_emails = {s.email for s in allowed}
        assert allowed_emails == {"e1@x.com", "e2@x.com", "e3@x.com"}
        assert len(held) == 1
        assert held[0]["email"] == "e4@x.com"
        assert "seat cap" in held[0]["reason"]

    def test_per_metro_cap_is_per_metro(self, prefs_store):
        """Four exclusive subs split across two metros are all within cap."""
        subs = [
            _sub("e1@x.com", "exclusive", created_at="2026-01-01T00:00:00Z"),
            _sub("e2@x.com", "exclusive", created_at="2026-01-02T00:00:00Z"),
            _sub("e3@x.com", "exclusive", created_at="2026-01-03T00:00:00Z"),
            _sub("e4@x.com", "exclusive", created_at="2026-01-04T00:00:00Z"),
        ]
        _map(prefs_store, "e1@x.com", "Austin, TX")
        _map(prefs_store, "e2@x.com", "Austin, TX")
        _map(prefs_store, "e3@x.com", "Denver, CO")
        _map(prefs_store, "e4@x.com", "Denver, CO")

        allowed, held = enforce_exclusive_caps(subs, prefs_store, cap=3)
        assert len(allowed) == 4
        assert held == []

    def test_cap_does_not_touch_other_tiers(self, prefs_store):
        subs = [_sub(f"b{i}@x.com", "basic") for i in range(10)]
        for s in subs:
            _map(prefs_store, s.email, "Austin, TX")
        allowed, held = enforce_exclusive_caps(subs, prefs_store, cap=3)
        assert len(allowed) == 10
        assert held == []

    def test_unmapped_exclusive_not_cap_held(self, prefs_store):
        """Unmapped exclusive subs don't consume seats; delivery holds them."""
        subs = [_sub(f"e{i}@x.com", "exclusive") for i in range(5)]
        allowed, held = enforce_exclusive_caps(subs, prefs_store, cap=3)
        assert len(allowed) == 5
        assert held == []
