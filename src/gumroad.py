"""
Gumroad API integration for BrokenSite-Weekly.
Retrieves active subscribers for a single subscription product.

This does NOT create products - uses existing Gumroad subscription product.
"""

from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass

import json
import requests
from requests.exceptions import RequestException

from .config import GumroadConfig, RetryConfig
from .retry import retry_with_backoff
from .logging_setup import get_logger

logger = get_logger("gumroad")

# Tier privilege ranking: higher wins when deduping by email; lower is the
# safer fallback when a subscriber's variant is missing or unrecognized.
_TIER_RANK = {"exclusive": 3, "pro": 2, "basic": 1, "standard": 1}


@dataclass
class Subscriber:
    """Active Gumroad subscriber."""
    email: str
    subscriber_id: str
    created_at: str
    status: str
    tier: str = "basic"
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    full_name: Optional[str] = None


class GumroadError(Exception):
    """Gumroad API error."""
    pass


class GumroadClient:
    """
    Gumroad API client for subscriber management.

    This client only reads subscriber data - it does not create or modify
    products. Your subscription product must already exist in Gumroad.
    """

    def __init__(self, config: GumroadConfig, retry_config: RetryConfig = None):
        self.config = config
        self.retry_config = retry_config or RetryConfig()
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {config.access_token}",
            "Content-Type": "application/json",
        })

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """Make authenticated request to Gumroad API."""
        url = f"{self.config.api_base_url}/{endpoint}"

        def do_request():
            if method.upper() == "GET":
                resp = self.session.get(url, params=params, timeout=30)
            else:
                resp = self.session.post(url, json=params, timeout=30)

            resp.raise_for_status()
            data = resp.json()

            if not data.get("success", True):
                raise GumroadError(f"API error: {data.get('message', 'Unknown error')}")

            return data

        return retry_with_backoff(
            func=do_request,
            config=self.retry_config,
            exceptions=(RequestException, ConnectionError),
            logger=logger,
            operation_name=f"gumroad_{endpoint}",
        )

    def get_product(self, product_id: str) -> Dict[str, Any]:
        """Get product details."""
        try:
            data = self._request("GET", f"products/{product_id}")
            return data.get("product", {})
        except Exception as e:
            logger.error(f"Failed to get product {product_id}: {e}")
            raise GumroadError(f"Failed to get product: {e}")

    def get_active_subscribers(self, product_id: str, tier: str) -> List[Subscriber]:
        """
        Get all active subscribers for the configured product.

        Returns only subscribers with active subscriptions (not cancelled,
        not failed payments, etc.)
        """
        subscribers: List[Subscriber] = []

        try:
            # Get product info for context
            product = self.get_product(product_id)
            product_name = product.get("name", "Unknown Product")
            logger.info(f"Fetching subscribers for product: {product_name} ({tier})")

            # Fetch subscribers with pagination
            page = 1
            while True:
                data = self._request(
                    "GET",
                    f"products/{product_id}/subscribers",
                    params={"page": page}
                )

                page_subscribers = data.get("subscribers", [])
                if not page_subscribers:
                    break

                for sub in page_subscribers:
                    # Only include active subscriptions
                    status = sub.get("status", "").lower()

                    # Gumroad subscription statuses:
                    # "alive" = active subscription
                    # "pending_cancellation" = will cancel at end of period (still active)
                    # "cancelled" = cancelled
                    # "failed_payment" = payment failed

                    if status in ("alive", "pending_cancellation"):
                        subscribers.append(Subscriber(
                            email=sub.get("email", ""),
                            subscriber_id=sub.get("id", ""),
                            created_at=sub.get("created_at", ""),
                            status=status,
                            # Tiered memberships: read the tier from this
                            # subscriber's variant, not the product mapping.
                            tier=_tier_from_variant(sub.get("variants"), tier),
                            product_id=product_id,
                            product_name=product_name,
                            full_name=sub.get("full_name"),
                        ))

                page += 1

                # Safety limit
                if page > 100:
                    logger.warning("Hit pagination safety limit (100 pages)")
                    break

            logger.info(f"Found {len(subscribers)} active subscribers")
            return subscribers

        except GumroadError:
            raise
        except Exception as e:
            logger.error(f"Failed to fetch subscribers: {e}")
            raise GumroadError(f"Failed to fetch subscribers: {e}")

    def verify_credentials(self) -> bool:
        """Verify API credentials are valid."""
        try:
            # Try to fetch user info
            data = self._request("GET", "user")
            user = data.get("user", {})
            logger.info(f"Gumroad credentials valid for: {user.get('email', 'unknown')}")
            return True
        except Exception as e:
            logger.error(f"Gumroad credential verification failed: {e}")
            return False


def _parse_products(config: GumroadConfig) -> List[Dict[str, Any]]:
    """Parse multi-product configuration from JSON or legacy env vars."""
    products: List[Dict[str, Any]] = []
    raw = (config.products_json or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                for tier, product_id in data.items():
                    products.append({"id": product_id, "tier": str(tier).lower()})
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and (item.get("id") or item.get("product_id")):
                        products.append({
                            "id": item.get("id") or item.get("product_id"),
                            "tier": str(item.get("tier", "basic")).lower(),
                        })
            else:
                logger.warning("GUMROAD_PRODUCTS_JSON should be a dict or list")
        except Exception as e:
            logger.error(f"Failed to parse GUMROAD_PRODUCTS_JSON: {e}")

    if not products and config.product_id:
        products = [{"id": config.product_id, "tier": "basic"}]

    return products


def _tier_from_variant(variants: Any, fallback: str) -> str:
    """Resolve a subscriber's tier from their Gumroad variant selection.

    A tiered-membership product is a single product whose tiers live in each
    subscriber's `variants` field (dict, list of strings, or plain string).
    Falls back to the configured mapping tier when no variant matches.
    """
    names: List[str] = []
    if isinstance(variants, dict):
        names = [str(v) for v in variants.values()]
    elif isinstance(variants, (list, tuple)):
        names = [str(v) for v in variants]
    elif isinstance(variants, str):
        names = [variants]
    for name in names:
        lowered = name.lower()
        if "exclusive" in lowered:
            return "exclusive"
        if "standard" in lowered:
            return "standard"
    return fallback


def _safest_fallback_tier(tiers: List[str]) -> str:
    """Pick the lowest-privilege tier among those mapped to one product id.

    Used as the variant fallback so a missing/unrecognized variant can never
    inherit a higher tier just because it appeared first in the mapping.
    Unknown tier names rank 0 (least privilege).
    """
    return min(tiers, key=lambda t: _TIER_RANK.get(t, 0))


def _dedupe_by_email(subscribers: List[Subscriber]) -> List[Subscriber]:
    """Deduplicate by email, keeping highest tier (exclusive > pro > basic/standard)."""
    by_email: Dict[str, Subscriber] = {}
    for sub in subscribers:
        existing = by_email.get(sub.email)
        if not existing:
            by_email[sub.email] = sub
            continue
        if _TIER_RANK.get(sub.tier, 0) > _TIER_RANK.get(existing.tier, 0):
            by_email[sub.email] = sub
    return list(by_email.values())


def enforce_exclusive_caps(
    subscribers: List[Subscriber],
    prefs_store=None,
    cap: int = 3,
) -> Tuple[List[Subscriber], List[Dict[str, Any]]]:
    """Enforce the per-metro seat cap for the exclusive tier.

    Groups exclusive-tier subscribers by metro (from the subscriber prefs
    store) and keeps the earliest `created_at` subscribers per metro, up to
    `cap` seats. Subscribers over the cap are *held* — never silently
    dropped — and returned in the held list so the operator can refund or
    reassign them. Each metro in a multi-metro prefs list counts separately.

    Returns (allowed_subscribers, held) where held is a list of dicts:
    {"email", "tier", "metro", "reason"}.
    """
    exclusive_subs = [s for s in subscribers if s.tier == "exclusive"]
    if not exclusive_subs or not cap or cap <= 0:
        return list(subscribers), []

    if prefs_store is None:
        # Late import to keep gumroad.py usable without the prefs module.
        from .subscriber_prefs import SubscriberPrefsStore

        prefs_store = SubscriberPrefsStore()

    def _metros(sub: Subscriber) -> List[str]:
        prefs = prefs_store.get_or_default(sub.email)
        return [c.strip() for c in prefs.cities if c and c.strip()]

    # Per metro, rank exclusive subscribers by earliest created_at.
    subs_by_metro: Dict[str, List[Tuple[Subscriber, str]]] = {}
    for sub in exclusive_subs:
        for metro in _metros(sub):
            subs_by_metro.setdefault(metro.lower(), []).append((sub, metro))

    kept_seats: set = set()  # (email_lower, metro_lower)
    held: List[Dict[str, Any]] = []
    for metro_key, entries in subs_by_metro.items():
        entries_sorted = sorted(entries, key=lambda e: e[0].created_at or "")
        for sub, _metro in entries_sorted[:cap]:
            kept_seats.add((sub.email.lower(), metro_key))
        for sub, metro in entries_sorted[cap:]:
            held.append({
                "email": sub.email,
                "tier": "exclusive",
                "metro": metro,
                "reason": (
                    f"exclusive seat cap reached for {metro} "
                    f"({cap} seats, earliest created_at wins) — refund or reassign"
                ),
            })

    # Delivery uses the primary metro (cities[0]). An exclusive subscriber is
    # allowed iff their primary metro seat is within the cap. Unmapped
    # exclusive subscribers pass through here — delivery holds them instead.
    allowed: List[Subscriber] = []
    for sub in subscribers:
        if sub.tier != "exclusive":
            allowed.append(sub)
            continue
        metros = _metros(sub)
        if not metros or (sub.email.lower(), metros[0].lower()) in kept_seats:
            allowed.append(sub)

    if held:
        logger.warning(
            f"Exclusive seat cap: holding {len(held)} metro seat(s) over the cap of {cap}"
        )
    return allowed, held


def get_subscribers_with_isolation(
    config: GumroadConfig,
    retry_config: RetryConfig = None,
    prefs_store=None,
    held_out: Optional[List[Dict[str, Any]]] = None,
) -> tuple[List[Subscriber], Optional[str]]:
    """
    Get subscribers with error isolation.
    Returns (subscribers, error_message).
    Never raises exceptions.
    """
    try:
        client = GumroadClient(config, retry_config)
        products = _parse_products(config)
        if not products:
            return [], "No Gumroad products configured"

        # Tiered memberships map several tiers to the SAME product id —
        # fetch each product once; per-record variant tiers handle the rest.
        tiers_by_product: Dict[str, List[str]] = {}
        for product in products:
            tier = str(product.get("tier", "basic")).lower()
            product_id = product.get("id")
            if product_id:
                tiers_by_product.setdefault(product_id, []).append(tier)

        all_subscribers: List[Subscriber] = []
        for product_id, tiers in tiers_by_product.items():
            # Least-privilege fallback, independent of mapping order, so an
            # untagged subscriber can't inherit the highest mapped tier.
            subs = client.get_active_subscribers(
                product_id, _safest_fallback_tier(tiers)
            )
            all_subscribers.extend(subs)

        # Enforce Pro seat cap
        if config.pro_seat_cap and config.pro_seat_cap > 0:
            pro_subs = [s for s in all_subscribers if s.tier == "pro"]
            if len(pro_subs) > config.pro_seat_cap:
                # Keep earliest created_at subscribers for fairness
                pro_sorted = sorted(pro_subs, key=lambda s: s.created_at or "")
                allowed = set(s.email for s in pro_sorted[:config.pro_seat_cap])
                all_subscribers = [
                    s for s in all_subscribers
                    if s.tier != "pro" or s.email in allowed
                ]
                logger.warning(
                    f"Pro seat cap reached: keeping {config.pro_seat_cap} of {len(pro_subs)} pro subscribers"
                )

        subscribers = _dedupe_by_email(all_subscribers)

        # Enforce per-metro exclusive tier seat cap (held subs are flagged,
        # never silently dropped).
        subscribers, held = enforce_exclusive_caps(
            subscribers,
            prefs_store=prefs_store,
            cap=getattr(config, "exclusive_seat_cap", 3),
        )
        if held_out is not None:
            held_out.extend(held)
        return subscribers, None
    except Exception as e:
        logger.error(f"Failed to get subscribers: {e}")
        return [], str(e)
