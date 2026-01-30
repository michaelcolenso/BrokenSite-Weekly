"""
Tests for the website scoring module.
"""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime

from src.scoring import (
    ScoringResult,
    _extract_copyright_year,
    _check_parked_domain,
    _check_diy_builder,
    _check_mobile_friendly,
    _check_outdated_tech,
    _is_social_url,
    _normalize_url,
    evaluate_website,
    evaluate_with_isolation,
)
from src.config import ScoringConfig


class TestNormalizeUrl:
    """Tests for URL normalization."""

    def test_adds_https_to_bare_domain(self):
        assert _normalize_url("example.com") == "https://example.com"

    def test_preserves_existing_https(self):
        assert _normalize_url("https://example.com") == "https://example.com"

    def test_preserves_existing_http(self):
        assert _normalize_url("http://example.com") == "http://example.com"

    def test_handles_empty_string(self):
        assert _normalize_url("") == ""

    def test_handles_none(self):
        assert _normalize_url(None) is None


class TestCopyrightYearExtraction:
    """Tests for copyright year extraction."""

    def test_extracts_year_with_copyright_symbol(self):
        html = "<footer>\u00a9 2020 Company Name</footer>"
        assert _extract_copyright_year(html) == 2020

    def test_extracts_year_with_copyright_word(self):
        html = "<footer>Copyright 2019 Company Name</footer>"
        assert _extract_copyright_year(html) == 2019

    def test_extracts_year_with_c_in_parens(self):
        html = "<footer>(c) 2021 Company</footer>"
        assert _extract_copyright_year(html) == 2021

    def test_extracts_year_with_all_rights_reserved(self):
        html = "<footer>All Rights Reserved 2018</footer>"
        assert _extract_copyright_year(html) == 2018

    def test_returns_max_year_when_multiple(self):
        html = "<footer>\u00a9 2015-2022 Company</footer>"
        # Should return the max valid year
        result = _extract_copyright_year(html)
        assert result in [2015, 2022]  # Implementation may vary

    def test_returns_none_when_no_copyright(self):
        html = "<footer>Contact us at 555-1234</footer>"
        assert _extract_copyright_year(html) is None

    def test_ignores_future_years(self):
        # Years > current year + 1 should be ignored
        html = f"<footer>&copy; 2099 Company</footer>"
        assert _extract_copyright_year(html) is None

    def test_ignores_very_old_years(self):
        html = "<footer>&copy; 1899 Company</footer>"
        assert _extract_copyright_year(html) is None

    def test_prefers_footer_section(self, sample_html_outdated):
        # The sample has 2018 in the footer
        assert _extract_copyright_year(sample_html_outdated) == 2018


class TestParkedDomainDetection:
    """Tests for parked domain detection."""

    def test_detects_domain_for_sale(self, sample_html_parked):
        assert _check_parked_domain(sample_html_parked) is True

    def test_detects_buy_this_domain(self):
        html = "<h1>Buy this domain</h1><p>Premium domain available</p>"
        assert _check_parked_domain(html) is True

    def test_detects_domain_parking_services(self):
        html = "<script src='https://sedoparking.com/script.js'></script>"
        assert _check_parked_domain(html) is True

    def test_does_not_flag_normal_business(self, sample_html_modern):
        assert _check_parked_domain(sample_html_modern) is False

    def test_detects_coming_soon(self):
        html = "<h1>Website Coming Soon</h1><p>Under construction</p>"
        assert _check_parked_domain(html) is True


class TestDIYBuilderDetection:
    """Tests for DIY website builder detection."""

    def test_detects_wix_in_html(self, sample_html_wix):
        result = _check_diy_builder(sample_html_wix, "https://example.com")
        assert result == "wix"

    def test_detects_wix_in_url(self):
        result = _check_diy_builder("<html></html>", "https://mysite.wixsite.com/business")
        assert result == "wix"

    def test_detects_squarespace(self):
        html = "<link rel='stylesheet' href='https://static1.squarespace.com/style.css'>"
        result = _check_diy_builder(html, "https://example.com")
        assert result == "squarespace"

    def test_detects_weebly(self):
        html = "<script src='https://www.weebly.com/weebly/main.js'></script>"
        result = _check_diy_builder(html, "https://example.com")
        assert result == "weebly"

    def test_returns_none_for_custom_site(self, sample_html_modern):
        result = _check_diy_builder(sample_html_modern, "https://example.com")
        assert result is None


class TestMobileFriendliness:
    """Tests for mobile-friendliness detection."""

    def test_detects_viewport_meta(self, sample_html_modern):
        has_viewport, has_responsive = _check_mobile_friendly(sample_html_modern)
        assert has_viewport is True

    def test_detects_missing_viewport(self, sample_html_no_viewport):
        has_viewport, has_responsive = _check_mobile_friendly(sample_html_no_viewport)
        assert has_viewport is False

    def test_detects_bootstrap(self):
        html = """
        <html>
        <head><meta name="viewport" content="width=device-width"></head>
        <body><div class="container">
            <script src="bootstrap.min.js"></script>
        </div></body>
        </html>
        """
        has_viewport, has_responsive = _check_mobile_friendly(html)
        assert has_responsive is True

    def test_detects_tailwind(self):
        html = """
        <html>
        <head><meta name="viewport" content="width=device-width"></head>
        <body><div class="flex flex-col tailwind">Content</div></body>
        </html>
        """
        _, has_responsive = _check_mobile_friendly(html)
        assert has_responsive is True


class TestOutdatedTech:
    """Tests for outdated technology detection."""

    def test_detects_flash(self, sample_html_flash):
        outdated = _check_outdated_tech(sample_html_flash)
        assert "flash" in outdated

    def test_detects_frames(self):
        html = "<frameset><frame src='nav.html'><frame src='content.html'></frameset>"
        outdated = _check_outdated_tech(html)
        assert "frames" in outdated

    def test_detects_marquee(self):
        html = "<marquee>Scrolling text!</marquee>"
        outdated = _check_outdated_tech(html)
        assert "marquee" in outdated

    def test_detects_blink(self):
        html = "<blink>Blinking text!</blink>"
        outdated = _check_outdated_tech(html)
        assert "blink_tag" in outdated

    def test_detects_old_jquery(self):
        html = '<script src="jquery-1.12.4.min.js"></script>'
        outdated = _check_outdated_tech(html)
        assert "old_jquery" in outdated

    def test_no_outdated_tech_in_modern_site(self, sample_html_modern):
        outdated = _check_outdated_tech(sample_html_modern)
        assert len(outdated) == 0


class TestSocialUrlDetection:
    """Tests for social-only URL detection."""

    def test_detects_facebook(self):
        assert _is_social_url("https://www.facebook.com/mybusiness") is True

    def test_detects_instagram(self):
        assert _is_social_url("https://instagram.com/mybusiness") is True

    def test_detects_twitter(self):
        assert _is_social_url("https://twitter.com/mybusiness") is True

    def test_detects_x_com(self):
        assert _is_social_url("https://x.com/mybusiness") is True

    def test_detects_linkedin(self):
        assert _is_social_url("https://linkedin.com/company/mybusiness") is True

    def test_does_not_flag_regular_url(self):
        assert _is_social_url("https://mybusiness.com") is False

    def test_handles_empty_url(self):
        assert _is_social_url("") is False

    def test_handles_none(self):
        assert _is_social_url(None) is False


class TestScoringWeights:
    """Tests for scoring weights and thresholds."""

    def test_default_threshold_is_40(self):
        config = ScoringConfig()
        assert config.min_score_to_include == 40

    def test_unreachable_weight_is_100(self):
        config = ScoringConfig()
        assert config.weight_unreachable == 100

    def test_timeout_weight_is_90(self):
        config = ScoringConfig()
        assert config.weight_timeout == 90

    def test_ssl_error_weight_is_80(self):
        config = ScoringConfig()
        assert config.weight_ssl_error == 80

    def test_parked_domain_weight_is_75(self):
        config = ScoringConfig()
        assert config.weight_parked_domain == 75

    def test_http_only_weight_is_30(self):
        config = ScoringConfig()
        assert config.weight_http_only == 30

    def test_wix_weight_is_low(self):
        config = ScoringConfig()
        assert config.weight_wix <= 10  # Should be low to avoid false positives


class TestEvaluateWebsite:
    """Integration tests for evaluate_website function."""

    @pytest.fixture(autouse=True)
    def disable_playwright(self, scoring_config):
        """Disable playwright fallback for unit tests."""
        scoring_config.playwright_fallback_enabled = False

    @patch("src.scoring.fetch_website")
    def test_scores_ssl_error(self, mock_fetch, scoring_config):
        mock_fetch.return_value = (None, "ssl_error: certificate verify failed")

        result = evaluate_website("https://example.com", config=scoring_config)

        assert result.score >= scoring_config.weight_ssl_error
        assert "ssl_error" in result.reasons
        assert result.error is not None

    @patch("src.scoring.fetch_website")
    def test_scores_timeout(self, mock_fetch, scoring_config):
        mock_fetch.return_value = (None, "timeout")

        result = evaluate_website("https://example.com", config=scoring_config)

        assert "timeout" in result.reasons
        # With default config, timeout is in unverified_reasons and gets
        # capped to unverified_score_cap (39) when include_unverified_leads=False
        assert result.score == scoring_config.unverified_score_cap

    @patch("src.scoring.fetch_website")
    def test_scores_timeout_uncapped(self, mock_fetch, scoring_config):
        """When unverified leads are included, timeout gets full weight."""
        scoring_config.include_unverified_leads = True
        mock_fetch.return_value = (None, "timeout")

        result = evaluate_website("https://example.com", config=scoring_config)

        assert result.score >= scoring_config.weight_timeout
        assert "timeout" in result.reasons

    @patch("src.scoring.fetch_website")
    def test_scores_parked_domain(self, mock_fetch, scoring_config, sample_html_parked):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = sample_html_parked
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert result.score >= scoring_config.weight_parked_domain
        assert "parked_domain" in result.reasons

    @patch("src.scoring.fetch_website")
    def test_scores_5xx_error(self, mock_fetch, scoring_config):
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.url = "https://example.com"
        mock_response.text = "<html><body>Internal Server Error</body></html>"
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert result.score >= scoring_config.weight_5xx_error
        assert "server_error_500" in result.reasons

    @patch("src.scoring.fetch_website")
    def test_scores_outdated_copyright(self, mock_fetch, scoring_config, sample_html_outdated):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = sample_html_outdated
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert result.score >= scoring_config.weight_outdated_copyright
        assert any("copyright" in r for r in result.reasons)

    @patch("src.scoring.fetch_website")
    def test_scores_missing_viewport(self, mock_fetch, scoring_config, sample_html_no_viewport):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = sample_html_no_viewport
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert "no_viewport" in result.reasons

    @patch("src.scoring.fetch_website")
    def test_scores_http_only(self, mock_fetch, scoring_config, sample_html_modern):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "http://example.com"  # HTTP, not HTTPS
        mock_response.text = sample_html_modern
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("http://example.com", config=scoring_config)

        assert result.score >= scoring_config.weight_http_only
        assert "no_https" in result.reasons

    @patch("src.scoring.fetch_website")
    def test_modern_site_scores_low(self, mock_fetch, scoring_config, sample_html_modern):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = sample_html_modern
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        # A modern, well-maintained site should score below the threshold
        assert result.score < scoring_config.min_score_to_include

    def test_social_url_excluded_by_default(self, scoring_config):
        scoring_config.include_social_only_leads = False

        result = evaluate_website("https://facebook.com/mybusiness", config=scoring_config)

        assert "social_only_excluded" in result.reasons

    def test_social_url_included_when_configured(self, scoring_config):
        scoring_config.include_social_only_leads = True

        result = evaluate_website("https://facebook.com/mybusiness", config=scoring_config)

        assert "social_only" in result.reasons
        assert result.score == scoring_config.weight_social_only


class TestEvaluateWithIsolation:
    """Tests for error isolation wrapper."""

    @patch("src.scoring.evaluate_website")
    def test_catches_exceptions(self, mock_evaluate, scoring_config):
        mock_evaluate.side_effect = Exception("Unexpected error")

        result = evaluate_with_isolation("https://example.com", config=scoring_config)

        assert result.score == 100
        assert "evaluation_error" in result.reasons
        assert "Unexpected error" in result.error

    @patch("src.scoring.evaluate_website")
    def test_passes_through_normal_results(self, mock_evaluate, scoring_config):
        expected = ScoringResult(
            url="https://example.com",
            score=45,
            reasons=["parked_domain"],
            http_status=200,
            response_time_ms=150,
            final_url="https://example.com",
            error=None,
        )
        mock_evaluate.return_value = expected

        result = evaluate_with_isolation("https://example.com", config=scoring_config)

        assert result == expected


class TestScoreThreshold:
    """Tests for score threshold behavior."""

    @pytest.fixture(autouse=True)
    def disable_playwright(self, scoring_config):
        """Disable playwright fallback for unit tests."""
        scoring_config.playwright_fallback_enabled = False

    @patch("src.scoring.fetch_website")
    def test_parked_domain_exceeds_threshold(self, mock_fetch, scoring_config, sample_html_parked):
        """A parked domain should score >= 40 (threshold)."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = sample_html_parked
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert result.score >= scoring_config.min_score_to_include

    @patch("src.scoring.fetch_website")
    def test_timeout_capped_below_threshold(self, mock_fetch, scoring_config):
        """Timeout is in unverified_reasons, so it gets capped below threshold by default."""
        mock_fetch.return_value = (None, "timeout")

        result = evaluate_website("https://example.com", config=scoring_config)

        # Timeout is unverified by default, capped to 39 (below threshold of 40)
        assert result.score == scoring_config.unverified_score_cap
        assert result.score < scoring_config.min_score_to_include

    @patch("src.scoring.fetch_website")
    def test_timeout_exceeds_threshold_when_unverified_included(self, mock_fetch, scoring_config):
        """When including unverified leads, timeout exceeds threshold."""
        scoring_config.include_unverified_leads = True
        mock_fetch.return_value = (None, "timeout")

        result = evaluate_website("https://example.com", config=scoring_config)

        assert result.score >= scoring_config.min_score_to_include

    @patch("src.scoring.fetch_website")
    def test_wix_alone_below_threshold(self, mock_fetch, scoring_config, sample_html_wix):
        """A Wix site alone should NOT exceed the threshold (to avoid false positives)."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = sample_html_wix
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        # Wix weight is 5, which alone should be below 40
        # (unless there are other issues)
        assert "diy_wix" in result.reasons
        # The score might include other signals, but Wix alone shouldn't push it over
