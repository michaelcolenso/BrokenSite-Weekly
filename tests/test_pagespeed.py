"""
Tests for Google PageSpeed Insights integration.
"""

import pytest
from unittest.mock import Mock, patch

from src.pagespeed import (
    PageSpeedCache,
    RateLimiter,
    run_pagespeed,
    extract_pagespeed_scores,
    check_pagespeed,
    _clear_cache,
)
from src.config import ScoringConfig


# ── Cache tests ──────────────────────────────────────────────────────────────

class TestPageSpeedCache:
    def test_set_and_get(self):
        cache = PageSpeedCache(ttl_seconds=3600)
        cache.set("https://example.com", {"score": 85})
        result = cache.get("https://example.com")
        assert result is not None
        assert result["score"] == 85

    def test_miss_returns_none(self):
        cache = PageSpeedCache()
        assert cache.get("https://nonexistent.com") is None

    def test_expiry(self):
        cache = PageSpeedCache(ttl_seconds=-1)  # Expired immediately
        cache.set("https://example.com", {"score": 85})
        assert cache.get("https://example.com") is None

    def test_clear(self):
        cache = PageSpeedCache()
        cache.set("https://example.com", {"score": 85})
        cache.clear()
        assert cache.get("https://example.com") is None

    def test_size(self):
        cache = PageSpeedCache()
        assert cache.size() == 0
        cache.set("url1", {})
        cache.set("url2", {})
        assert cache.size() == 2


# ── Rate limiter tests ───────────────────────────────────────────────────────

class TestRateLimiter:
    def test_does_not_block_first_call(self):
        import time
        rl = RateLimiter(requests_per_second=100)  # Very permissive
        start = time.time()
        rl.wait()
        elapsed = time.time() - start
        assert elapsed < 0.05  # Should return almost instantly


# ── Score extraction tests ───────────────────────────────────────────────────

_SAMPLE_RESPONSE = {
    "lighthouseResult": {
        "categories": {
            "performance": {"score": 0.65},
            "accessibility": {"score": 0.90},
            "best-practices": {"score": 0.85},
            "seo": {"score": 0.72},
        },
        "audits": {
            "first-contentful-paint": {
                "numericValue": 2340.5,
            },
            "largest-contentful-paint": {
                "numericValue": 4200.0,
            },
            "total-blocking-time": {
                "numericValue": 350.0,
            },
            "cumulative-layout-shift": {
                "numericValue": 0.12,
            },
        },
    }
}

_SAMPLE_RESPONSE_NO_METRICS = {
    "lighthouseResult": {
        "categories": {
            "performance": {"score": 0.42},
        },
        "audits": {},
    }
}

_ERROR_RESPONSE = {
    "error": {"message": "API key not valid", "code": 400}
}


class TestExtractScores:
    def test_extracts_all_scores(self):
        scores = extract_pagespeed_scores(_SAMPLE_RESPONSE)
        assert scores["performance_score"] == 65
        assert scores["accessibility_score"] == 90
        assert scores["best_practices_score"] == 85
        assert scores["seo_score"] == 72
        assert scores["fcp_ms"] == 2340.5
        assert scores["lcp_ms"] == 4200.0
        assert scores["tbt_ms"] == 350.0
        assert scores["cls"] == 0.12
        assert scores["error"] is None

    def test_partial_metrics(self):
        scores = extract_pagespeed_scores(_SAMPLE_RESPONSE_NO_METRICS)
        assert scores["performance_score"] == 42
        assert scores["fcp_ms"] is None  # No metric data
        assert scores["error"] is None

    def test_api_error(self):
        scores = extract_pagespeed_scores(_ERROR_RESPONSE)
        assert scores["error"] is not None

    def test_none_input(self):
        scores = extract_pagespeed_scores({})
        assert scores["error"] == "no_data"


# ── check_pagespeed tests ────────────────────────────────────────────────────

class TestCheckPageSpeed:
    def test_disabled_returns_none(self):
        config = ScoringConfig(pagespeed_enabled=False)
        result = check_pagespeed("https://example.com", config)
        assert result == (None, None, None)

    def test_no_api_key_returns_none(self):
        config = ScoringConfig(pagespeed_enabled=True, pagespeed_api_key="")
        result = check_pagespeed("https://example.com", config)
        assert result == (None, None, None)

    @patch("src.pagespeed.run_pagespeed")
    def test_low_score_flagged(self, mock_run):
        _clear_cache()
        mock_run.return_value = _SAMPLE_RESPONSE_NO_METRICS
        config = ScoringConfig(
            pagespeed_enabled=True,
            pagespeed_api_key="test_key",
            pagespeed_score_threshold=50,
        )
        perf, reason, scores = check_pagespeed("https://example.com", config)
        assert perf == 42
        assert reason == "pagespeed_low_42"
        assert scores is not None

    @patch("src.pagespeed.run_pagespeed")
    def test_ok_score_not_flagged(self, mock_run):
        _clear_cache()
        mock_run.return_value = _SAMPLE_RESPONSE  # perf=65, threshold=50
        config = ScoringConfig(
            pagespeed_enabled=True,
            pagespeed_api_key="test_key",
            pagespeed_score_threshold=50,
        )
        perf, reason, scores = check_pagespeed("https://example.com", config)
        assert perf == 65
        assert reason == "pagespeed_ok_65"
        # "ok" reason doesn't start with "pagespeed_low_"
        assert not reason.startswith("pagespeed_low_")

    @patch("src.pagespeed.run_pagespeed")
    def test_api_failure_graceful(self, mock_run):
        _clear_cache()
        mock_run.return_value = None
        config = ScoringConfig(
            pagespeed_enabled=True,
            pagespeed_api_key="test_key",
        )
        result = check_pagespeed("https://example.com", config)
        assert result == (None, None, None)

    @patch("src.pagespeed.run_pagespeed")
    def test_api_error_response_graceful(self, mock_run):
        _clear_cache()
        mock_run.return_value = _ERROR_RESPONSE
        config = ScoringConfig(
            pagespeed_enabled=True,
            pagespeed_api_key="test_key",
        )
        result = check_pagespeed("https://example.com", config)
        assert result == (None, None, None)


# ── Scoring integration test ─────────────────────────────────────────────────

class TestPageSpeedScoring:
    """Verify that PageSpeed is wired into evaluate_website."""

    @pytest.fixture(autouse=True)
    def disable_playwright(self):
        """Disable playwright fallback for unit tests."""
        pass

    @patch("src.scoring.fetch_website")
    @patch("src.pagespeed.run_pagespeed")
    def test_low_pagespeed_adds_weight_and_reason(self, mock_ps, mock_fetch):
        _clear_cache()
        from src.scoring import evaluate_website

        mock_ps.return_value = _SAMPLE_RESPONSE_NO_METRICS  # perf=42
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = """<html><head>
        <meta name="viewport" content="width=device-width">
        <meta name="description" content="A business">
        <title>Example Business</title></head>
        <body><h1>Business</h1><p>Content here.</p>
        <p>Call: (555)123-4567</p>
        <p>Email: <a href="mailto:info@example.com">info@example.com</a></p>
        <footer>&copy; 2025</footer></body></html>"""
        mock_fetch.return_value = (mock_response, None)

        config = ScoringConfig(
            pagespeed_enabled=True,
            pagespeed_api_key="test_key",
            pagespeed_score_threshold=50,
            weight_pagespeed_low=25,
        )
        config.playwright_fallback_enabled = False

        result = evaluate_website("https://example.com", config=config)
        assert any(r.startswith("pagespeed_low_") for r in result.reasons)
        assert result.score >= config.weight_pagespeed_low

    @patch("src.scoring.fetch_website")
    @patch("src.pagespeed.run_pagespeed")
    def test_good_pagespeed_no_extra_score(self, mock_ps, mock_fetch):
        _clear_cache()
        from src.scoring import evaluate_website

        mock_ps.return_value = _SAMPLE_RESPONSE  # perf=65, above threshold 50
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = """<html><head>
        <meta name="viewport" content="width=device-width">
        <meta name="description" content="A business">
        <title>Example Business</title></head>
        <body><h1>Business</h1><p>Content here.</p>
        <p>Call: (555)123-4567</p>
        <p>Email: <a href="mailto:info@example.com">info@example.com</a></p>
        <footer>&copy; 2025</footer></body></html>"""
        mock_fetch.return_value = (mock_response, None)

        config = ScoringConfig(
            pagespeed_enabled=True,
            pagespeed_api_key="test_key",
            pagespeed_score_threshold=50,
            weight_pagespeed_low=25,
        )
        config.playwright_fallback_enabled = False

        result = evaluate_website("https://example.com", config=config)
        assert not any(r.startswith("pagespeed_low_") for r in result.reasons)

    @patch("src.scoring.fetch_website")
    def test_pagespeed_disabled_skips_check(self, mock_fetch):
        _clear_cache()
        from src.scoring import evaluate_website

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = """<html><head>
        <meta name="viewport" content="width=device-width">
        <meta name="description" content="A business">
        <title>Example Business</title></head>
        <body><h1>Business</h1><p>Content here.</p>
        <p>Call: (555)123-4567</p>
        <p>Email: <a href="mailto:info@example.com">info@example.com</a></p>
        <footer>&copy; 2025</footer></body></html>"""
        mock_fetch.return_value = (mock_response, None)

        config = ScoringConfig(
            pagespeed_enabled=False,  # Disabled
            pagespeed_api_key="test_key",
            pagespeed_score_threshold=50,
            weight_pagespeed_low=25,
        )
        config.playwright_fallback_enabled = False

        result = evaluate_website("https://example.com", config=config)
        assert not any(r.startswith("pagespeed") for r in result.reasons)


# ── Global cache reset ───────────────────────────────────────────────────────

def test_clear_cache_resets_global():
    _clear_cache()
    cache_instance = __import__("src.pagespeed", fromlist=["_cache"])._cache
    assert cache_instance.size() == 0
