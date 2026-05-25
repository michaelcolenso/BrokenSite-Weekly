"""
Google PageSpeed Insights integration for BrokenSite-Weekly.

Opt-in scoring signal: checks PageSpeed performance score and flags
sites with low scores as likely broken/neglected.

Requires PAGESPEED_API_KEY environment variable.
Free tier: 25,000 requests/day via Google Cloud Console.

Features:
  - In-memory cache with configurable TTL (avoids repeat calls)
  - Rate limiting (1 request/sec)
  - Graceful degradation: never blocks scoring on API failure
"""

from __future__ import annotations

import time
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple
from urllib.parse import quote

import requests

from .config import ScoringConfig
from .logging_setup import get_logger

logger = get_logger("pagespeed")

PAGESPEED_API_URL = (
    "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
)

# ── Cache ────────────────────────────────────────────────────────────────────

class PageSpeedCache:
    """Thread-safe in-memory cache for PageSpeed results."""

    def __init__(self, ttl_seconds: int = 604800):  # 7 days default
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._ttl = ttl_seconds
        self._lock = threading.RLock()

    def get(self, url: str) -> Optional[Dict[str, Any]]:
        """Return cached result if not expired, else None."""
        with self._lock:
            entry = self._cache.get(url)
            if entry is None:
                return None
            timestamp, data = entry
            if time.time() - timestamp > self._ttl:
                del self._cache[url]
                return None
            return data

    def set(self, url: str, data: Dict[str, Any]) -> None:
        with self._lock:
            self._cache[url] = (time.time(), data)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._cache)


# Global cache instance
_cache = PageSpeedCache()


def _clear_cache() -> None:
    """Clear the global PageSpeed cache (useful for testing)."""
    _cache.clear()


# ── Rate limiter ─────────────────────────────────────────────────────────────

class RateLimiter:
    """Simple token-bucket rate limiter (1 request per second by default)."""

    def __init__(self, requests_per_second: float = 1.0):
        self._min_interval = 1.0 / requests_per_second
        self._last_call = 0.0
        self._lock = threading.RLock()

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_call = time.time()


_rate_limiter = RateLimiter()


# ── API client ───────────────────────────────────────────────────────────────

def run_pagespeed(
    url: str,
    api_key: str,
    strategy: str = "mobile",
    timeout: int = 15,
) -> Optional[Dict[str, Any]]:
    """
    Call the PageSpeed Insights API for a URL.

    Args:
        url: Full URL to analyze.
        api_key: Google API key.
        strategy: 'mobile' or 'desktop'.
        timeout: Request timeout in seconds.

    Returns:
        Parsed API response dict, or None on failure.
    """
    # Check cache first
    cache_key = f"{url}|{strategy}"
    cached = _cache.get(cache_key)
    if cached is not None:
        logger.debug(f"PageSpeed cache hit: {url}")
        return cached

    # Rate limit
    _rate_limiter.wait()

    try:
        params = {
            "url": url,
            "key": api_key,
            "strategy": strategy,
            "category": ["performance", "accessibility", "best-practices", "seo"],
        }
        resp = requests.get(PAGESPEED_API_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()

        # Cache successful responses
        _cache.set(cache_key, data)
        return data

    except requests.exceptions.Timeout:
        logger.warning(f"PageSpeed API timeout for {url}")
        return None
    except requests.exceptions.RequestException as e:
        logger.warning(f"PageSpeed API error for {url}: {e}")
        return None
    except Exception as e:
        logger.error(f"PageSpeed unexpected error for {url}: {e}")
        return None


def extract_pagespeed_scores(
    data: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Extract key scores from a PageSpeed API response.

    Returns dict with:
      - performance_score: 0-100 (higher = better)
      - accessibility_score: 0-100
      - best_practices_score: 0-100
      - seo_score: 0-100
      - fcp_ms: First Contentful Paint in ms
      - lcp_ms: Largest Contentful Paint in ms
      - tbt_ms: Total Blocking Time in ms
      - cls: Cumulative Layout Shift
      - error: Optional error message
    """
    result: Dict[str, Any] = {
        "performance_score": None,
        "accessibility_score": None,
        "best_practices_score": None,
        "seo_score": None,
        "fcp_ms": None,
        "lcp_ms": None,
        "tbt_ms": None,
        "cls": None,
        "error": None,
    }

    if not data:
        result["error"] = "no_data"
        return result

    # Check for API-level errors
    if "error" in data:
        result["error"] = data["error"].get("message", "api_error")
        return result

    # Extract category scores
    categories = data.get("lighthouseResult", {}).get("categories", {})
    for cat_name, cat_data in categories.items():
        score = cat_data.get("score")
        if score is not None:
            # Normalize hyphens to underscores (API uses "best-practices")
            key = f"{cat_name.replace('-', '_')}_score"
            result[key] = int(score * 100)

    # Extract key metrics from audits
    audits = data.get("lighthouseResult", {}).get("audits", {})

    metric_map = {
        "first-contentful-paint": ("fcp_ms", "numericValue"),
        "largest-contentful-paint": ("lcp_ms", "numericValue"),
        "total-blocking-time": ("tbt_ms", "numericValue"),
        "cumulative-layout-shift": ("cls", "numericValue"),
    }

    for audit_id, (key, value_field) in metric_map.items():
        audit = audits.get(audit_id, {})
        value = audit.get(value_field)
        if value is not None:
            result[key] = round(value, 2)

    return result


def check_pagespeed(
    url: str,
    config: ScoringConfig,
) -> Tuple[Optional[int], Optional[str], Optional[Dict[str, Any]]]:
    """
    Check PageSpeed score for a URL. Opt-in; requires API key.

    Args:
        url: Website URL to check.
        config: ScoringConfig with PageSpeed settings.

    Returns:
        (performance_score, reason_text, full_scores_dict).
        Returns (None, None, None) if disabled or API unavailable.
    """
    if not config.pagespeed_enabled:
        return None, None, None

    api_key = config.pagespeed_api_key
    if not api_key:
        return None, None, None

    try:
        data = run_pagespeed(url, api_key, strategy="mobile", timeout=15)
        if data is None:
            return None, None, None

        scores = extract_pagespeed_scores(data)
        if scores.get("error"):
            return None, None, None

        perf = scores.get("performance_score")
        if perf is None:
            return None, None, None

        # Return the performance score and full dict for downstream use
        if perf < config.pagespeed_score_threshold:
            reason = f"pagespeed_low_{perf}"
            return perf, reason, scores
        else:
            reason = f"pagespeed_ok_{perf}"
            return perf, reason, scores

    except Exception as e:
        logger.warning(f"PageSpeed check failed for {url}: {e}")
        return None, None, None
