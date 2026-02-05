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
                            tier=tier,
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


def _dedupe_by_email(subscribers: List[Subscriber]) -> List[Subscriber]:
    """Deduplicate by email, keeping highest tier (pro > basic)."""
    rank = {"pro": 2, "basic": 1}
    by_email: Dict[str, Subscriber] = {}
    for sub in subscribers:
        existing = by_email.get(sub.email)
        if not existing:
            by_email[sub.email] = sub
            continue
        if rank.get(sub.tier, 0) > rank.get(existing.tier, 0):
            by_email[sub.email] = sub
    return list(by_email.values())


def get_subscribers_with_isolation(
    config: GumroadConfig,
    retry_config: RetryConfig = None,
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

        all_subscribers: List[Subscriber] = []
        for product in products:
            tier = str(product.get("tier", "basic")).lower()
            product_id = product.get("id")
            if not product_id:
                continue
            subs = client.get_active_subscribers(product_id, tier)
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
        return subscribers, None
    except Exception as e:
        logger.error(f"Failed to get subscribers: {e}")
        return [], str(e)
