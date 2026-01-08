"""
Gumroad API integration for BrokenSite-Weekly.
Retrieves active subscribers for a single subscription product.

This does NOT create products - uses existing Gumroad subscription product.
"""

from typing import List, Optional, Dict, Any
from dataclasses import dataclass

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

    def get_product(self) -> Dict[str, Any]:
        """Get product details."""
        try:
            data = self._request("GET", f"products/{self.config.product_id}")
            return data.get("product", {})
        except Exception as e:
            logger.error(f"Failed to get product {self.config.product_id}: {e}")
            raise GumroadError(f"Failed to get product: {e}")

    def get_active_subscribers(self) -> List[Subscriber]:
        """
        Get all active subscribers for the configured product.

        Returns only subscribers with active subscriptions (not cancelled,
        not failed payments, etc.)
        """
        subscribers: List[Subscriber] = []

        try:
            # Get product info for context
            product = self.get_product()
            product_name = product.get("name", "Unknown Product")
            logger.info(f"Fetching subscribers for product: {product_name}")

            # Fetch subscribers with pagination
            page = 1
            while True:
                data = self._request(
                    "GET",
                    f"products/{self.config.product_id}/subscribers",
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
        subscribers = client.get_active_subscribers()
        return subscribers, None
    except Exception as e:
        logger.error(f"Failed to get subscribers: {e}")
        return [], str(e)
