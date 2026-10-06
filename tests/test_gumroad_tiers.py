"""
Tests for tiered-membership tier classification (per-subscriber variant
tiers) and single-fetch dedupe for multi-tier product mappings.
"""

import json

import pytest

from src.config import GumroadConfig, RetryConfig
from src.gumroad import (
    GumroadClient,
    _dedupe_by_email,
    _tier_from_variant,
    get_subscribers_with_isolation,
)
from src.subscriber_prefs import SubscriberPrefs, SubscriberPrefsStore


TIERED_PRODUCT_ID = "ev-M070rRXDuR_AAKaIIQA=="


def _config(products: dict) -> GumroadConfig:
    return GumroadConfig(
        access_token="test_token",
        products_json=json.dumps(products),
    )


@pytest.fixture
def retry_config() -> RetryConfig:
    return RetryConfig(max_retries=1, base_delay_seconds=0.1, max_delay_seconds=1.0)


@pytest.fixture
def prefs_store(tmp_path):
    return SubscriberPrefsStore(path=tmp_path / "prefs.json")


class _FakeGumroadAPI:
    """Drop-in replacement for GumroadClient._request (no network)."""

    def __init__(self, subscribers, product_name="BrokenSite Weekly"):
        self.subscribers = subscribers
        self.product_name = product_name
        self.calls = []

    def __call__(self, method, endpoint, params=None):
        self.calls.append(endpoint)
        if endpoint.endswith("/subscribers"):
            page = (params or {}).get("page", 1)
            subs = self.subscribers if page == 1 else []
            return {"success": True, "subscribers": subs}
        return {"success": True, "product": {"name": self.product_name}}


def _raw_sub(email, variants=None, created_at="2026-01-01T00:00:00Z"):
    sub = {
        "id": f"sub_{email}",
        "email": email,
        "status": "alive",
        "created_at": created_at,
    }
    if variants is not None:
        sub["variants"] = variants
    return sub


# ── Variant parsing ──────────────────────────────────────────────────────────

class TestTierFromVariant:
    @pytest.mark.parametrize(
        "variants,expected",
        [
            ({"Tier": "Metro Exclusive"}, "exclusive"),
            ({"Tier": "metro exclusive"}, "exclusive"),
            ({"Tier": "Standard"}, "standard"),
            (["Standard"], "standard"),
            (["Metro Exclusive"], "exclusive"),
            ("Metro Exclusive", "exclusive"),
            ("Standard", "standard"),
        ],
    )
    def test_variant_shapes(self, variants, expected):
        assert _tier_from_variant(variants, "basic") == expected

    @pytest.mark.parametrize("variants", [None, {}, [], "", {"Tier": "Unknown Plan"}])
    def test_fallback_when_no_match(self, variants):
        assert _tier_from_variant(variants, "basic") == "basic"
        assert _tier_from_variant(variants, "exclusive") == "exclusive"


# ── Per-subscriber tier in get_active_subscribers ────────────────────────────

class TestActiveSubscriberTiers:
    def _fetch(self, monkeypatch, retry_config, subscribers, tier="standard"):
        api = _FakeGumroadAPI(subscribers)
        monkeypatch.setattr(GumroadClient, "_request", api)
        client = GumroadClient(_config({"standard": TIERED_PRODUCT_ID}), retry_config)
        return client.get_active_subscribers(TIERED_PRODUCT_ID, tier), api

    def test_variant_dict_sets_tier(self, monkeypatch, retry_config):
        subs, _ = self._fetch(
            monkeypatch, retry_config,
            [_raw_sub("vip@x.com", {"Tier": "Metro Exclusive"})],
        )
        assert subs[0].tier == "exclusive"

    def test_variant_list_sets_tier(self, monkeypatch, retry_config):
        subs, _ = self._fetch(
            monkeypatch, retry_config,
            [_raw_sub("std@x.com", ["Standard"])],
        )
        assert subs[0].tier == "standard"

    def test_variant_string_sets_tier(self, monkeypatch, retry_config):
        subs, _ = self._fetch(
            monkeypatch, retry_config,
            [_raw_sub("vip@x.com", "Metro Exclusive")],
        )
        assert subs[0].tier == "exclusive"

    def test_missing_variants_falls_back_to_mapped_tier(self, monkeypatch, retry_config):
        subs, _ = self._fetch(
            monkeypatch, retry_config,
            [_raw_sub("legacy@x.com")],
            tier="exclusive",
        )
        assert subs[0].tier == "exclusive"


# ── Dedupe rank ──────────────────────────────────────────────────────────────

class TestDedupeStandardRank:
    def test_standard_not_misclassified(self):
        from src.gumroad import Subscriber

        def sub(email, tier):
            return Subscriber(email=email, subscriber_id=email,
                              created_at="2026-01-01T00:00:00Z",
                              status="alive", tier=tier)

        result = _dedupe_by_email([sub("a@x.com", "standard"), sub("a@x.com", "basic")])
        assert result[0].tier in ("standard", "basic")  # same rank, first wins
        result = _dedupe_by_email([sub("a@x.com", "standard"), sub("a@x.com", "pro")])
        assert result[0].tier == "pro"


# ── End-to-end: one tiered product mapped to two tiers ───────────────────────

class TestTieredMembershipEndToEnd:
    def test_same_product_two_tiers(self, monkeypatch, retry_config, prefs_store):
        """The original bug: both tiers mapped to one product id used to
        double-fetch and classify EVERY subscriber as exclusive."""
        raw_subs = [
            _raw_sub("std1@x.com", {"Tier": "Standard"}, "2026-01-01T00:00:00Z"),
            _raw_sub("std2@x.com", {"Tier": "Standard"}, "2026-01-02T00:00:00Z"),
            _raw_sub("std3@x.com", ["Standard"], "2026-01-03T00:00:00Z"),
            _raw_sub("std4@x.com", "Standard", "2026-01-04T00:00:00Z"),
            _raw_sub("vip1@x.com", {"Tier": "Metro Exclusive"}, "2026-01-05T00:00:00Z"),
        ]
        api = _FakeGumroadAPI(raw_subs)
        monkeypatch.setattr(GumroadClient, "_request", api)

        config = _config({
            "standard": TIERED_PRODUCT_ID,
            "exclusive": TIERED_PRODUCT_ID,
        })
        for email in ("std1@x.com", "std2@x.com", "std3@x.com", "std4@x.com", "vip1@x.com"):
            prefs_store.set(SubscriberPrefs(email=email, cities=["Austin, TX"]))

        held = []
        subs, err = get_subscribers_with_isolation(
            config, retry_config, prefs_store=prefs_store, held_out=held
        )
        assert err is None

        # Product fetched once, not once per mapped tier.
        product_fetches = [c for c in api.calls if not c.endswith("/subscribers")]
        assert len(product_fetches) == 1

        tiers = {s.email: s.tier for s in subs}
        assert tiers == {
            "std1@x.com": "standard",
            "std2@x.com": "standard",
            "std3@x.com": "standard",
            "std4@x.com": "standard",
            "vip1@x.com": "exclusive",
        }

        # Standard subscribers are NOT held by the exclusive seat cap.
        assert held == []
        assert len(subs) == 5
