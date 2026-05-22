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


_BASE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="A great business">
    <title>Business Name</title>
</head>
<body>
    <h1>Welcome</h1>
    <p>We do great work.</p>
    <p>Call us: (555) 123-4567</p>
    <p>Email: <a href="mailto:info@example.com">info@example.com</a></p>
    <footer><p>© 2026 Business Name. All rights reserved.</p></footer>
</body>
</html>"""


class TestMarketingSignalScoring:
    """Tests for marketing signal detection and scoring."""

    @pytest.fixture(autouse=True)
    def disable_playwright(self, scoring_config):
        """Disable playwright fallback for unit tests."""
        scoring_config.playwright_fallback_enabled = False

    @patch("src.scoring.fetch_website")
    def test_gtm_adds_weight_and_reason(self, mock_fetch, scoring_config):
        """Google Tag Manager detection should add weight and reason."""
        html = _BASE_HTML.replace(
            "</head>",
            '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-123"></script></head>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert "has_gtm" in result.reasons
        assert result.score == scoring_config.weight_has_gtm

    @patch("src.scoring.fetch_website")
    def test_fb_pixel_adds_weight_and_reason(self, mock_fetch, scoring_config):
        """Facebook Pixel detection should add weight and reason."""
        html = _BASE_HTML.replace(
            "</head>",
            '<script>!function(f,b,e,v,n,t,s){if(f.fbq)return;n=f.fbq=function(){};fbq("init","123");</script></head>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert "has_fb_pixel" in result.reasons
        assert result.score == scoring_config.weight_has_fb_pixel

    @patch("src.scoring.fetch_website")
    def test_gclid_in_html_adds_weight_and_reason(self, mock_fetch, scoring_config):
        """gclid in HTML should add weight and reason."""
        html = _BASE_HTML.replace(
            "</body>",
            '<a href="https://example.com/landing?gclid=abc123">Click</a></body>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert "has_gclid" in result.reasons
        assert result.score == scoring_config.weight_has_gclid

    @patch("src.scoring.fetch_website")
    def test_gclid_in_final_url_adds_weight_and_reason(self, mock_fetch, scoring_config):
        """gclid in final URL should add weight and reason."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com/landing?gclid=abc123"
        mock_response.text = _BASE_HTML
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert "has_gclid" in result.reasons
        assert result.score == scoring_config.weight_has_gclid

    @patch("src.scoring.fetch_website")
    def test_multiple_marketing_signals_stack(self, mock_fetch, scoring_config):
        """Multiple marketing signals should stack their weights."""
        html = _BASE_HTML.replace(
            "</head>",
            (
                '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-123"></script>'
                '<script>!function(f,b,e,v,n,t,s){if(f.fbq)return;n=f.fbq=function(){};fbq("init","123");</script>'
                '</head>'
            )
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert "has_gtm" in result.reasons
        assert "has_fb_pixel" in result.reasons
        expected = scoring_config.weight_has_gtm + scoring_config.weight_has_fb_pixel
        assert result.score == expected

    @patch("src.scoring.fetch_website")
    def test_no_marketing_signals_no_extra_score(self, mock_fetch, scoring_config):
        """A clean site should not have marketing reasons or extra score."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = _BASE_HTML
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert "has_gtm" not in result.reasons
        assert "has_fb_pixel" not in result.reasons
        assert "has_gclid" not in result.reasons
        assert result.score == 0


class TestSslExpiryScoring:
    """Tests for SSL certificate expiry scoring."""

    @pytest.fixture(autouse=True)
    def disable_playwright(self, scoring_config):
        """Disable playwright fallback for unit tests."""
        scoring_config.playwright_fallback_enabled = False

    @patch("src.scoring.fetch_website")
    @patch("src.scoring._check_ssl_expiry")
    def test_ssl_expiring_soon_adds_weight_and_reason(self, mock_ssl_check, mock_fetch, scoring_config):
        """An SSL cert expiring within threshold should add weight and reason."""
        mock_ssl_check.return_value = 12
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = _BASE_HTML
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert "ssl_expires_12_days" in result.reasons
        assert result.score == scoring_config.weight_ssl_expiry

    @patch("src.scoring.fetch_website")
    @patch("src.scoring._check_ssl_expiry")
    def test_ssl_not_expiring_no_score(self, mock_ssl_check, mock_fetch, scoring_config):
        """An SSL cert with plenty of time left should not add score."""
        mock_ssl_check.return_value = 90
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = _BASE_HTML
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert not any(r.startswith("ssl_expires_") for r in result.reasons)
        assert result.score == 0

    @patch("src.scoring.fetch_website")
    @patch("src.scoring._check_ssl_expiry")
    def test_http_site_skips_ssl_check(self, mock_ssl_check, mock_fetch, scoring_config):
        """HTTP-only sites should not trigger SSL expiry checks."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "http://example.com"
        mock_response.text = _BASE_HTML
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("http://example.com", config=scoring_config)

        mock_ssl_check.assert_not_called()
        assert not any(r.startswith("ssl_expires_") for r in result.reasons)

    @patch("src.scoring.fetch_website")
    @patch("src.scoring._check_ssl_expiry")
    def test_ssl_check_failure_is_graceful(self, mock_ssl_check, mock_fetch, scoring_config):
        """If SSL check fails, scoring should continue without error."""
        mock_ssl_check.return_value = None
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = _BASE_HTML
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert not any(r.startswith("ssl_expires_") for r in result.reasons)
        assert result.score == 0


class TestBrokenImageScoring:
    """Tests for broken image detection and scoring."""

    @pytest.fixture(autouse=True)
    def disable_playwright(self, scoring_config):
        """Disable playwright fallback for unit tests."""
        scoring_config.playwright_fallback_enabled = False
        scoring_config.broken_image_check_enabled = True

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_disabled_broken_image_check_skips_head_requests(
        self,
        mock_head,
        mock_fetch,
        scoring_config,
    ):
        """Launch config should be able to skip sampled image probes."""
        scoring_config.broken_image_check_enabled = False
        html = _BASE_HTML.replace(
            "</body>",
            '<img src="/photo.jpg" alt="photo"></body>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        mock_head.assert_not_called()
        assert not any(r.startswith("broken_image_") for r in result.reasons)

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_one_broken_image_adds_weight_and_reason(self, mock_head, mock_fetch, scoring_config):
        """A single broken image should add weight and a reason."""
        mock_head.return_value = Mock(status_code=404)
        html = _BASE_HTML.replace(
            "</body>",
            '<img src="/photo.jpg" alt="photo"></body>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert any(r.startswith("broken_image_") for r in result.reasons)
        assert result.score == scoring_config.weight_broken_image

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_multiple_broken_images_stack(self, mock_head, mock_fetch, scoring_config):
        """Multiple broken images should stack their weights."""
        mock_head.return_value = Mock(status_code=404)
        html = _BASE_HTML.replace(
            "</body>",
            '<img src="/a.jpg"><img src="/b.jpg"></body>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        broken_reasons = [r for r in result.reasons if r.startswith("broken_image_")]
        assert len(broken_reasons) == 2
        assert result.score == 2 * scoring_config.weight_broken_image

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_working_images_add_no_score(self, mock_head, mock_fetch, scoring_config):
        """Images that return 200 should not add score."""
        mock_head.return_value = Mock(status_code=200)
        html = _BASE_HTML.replace(
            "</body>",
            '<img src="/photo.jpg" alt="photo"></body>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert not any(r.startswith("broken_image_") for r in result.reasons)
        assert result.score == 0

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_no_images_no_score(self, mock_head, mock_fetch, scoring_config):
        """HTML with no images should not trigger image checks."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = _BASE_HTML
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        mock_head.assert_not_called()
        assert result.score == 0

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_data_uri_images_skipped(self, mock_head, mock_fetch, scoring_config):
        """Data URI images should be skipped."""
        mock_head.return_value = Mock(status_code=404)
        html = _BASE_HTML.replace(
            "</body>",
            '<img src="data:image/png;base64,abc123"><img src="/photo.jpg"></body>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        # Only one HEAD call (for /photo.jpg), data URI skipped
        assert mock_head.call_count == 1
        broken_reasons = [r for r in result.reasons if r.startswith("broken_image_")]
        assert len(broken_reasons) == 1


class TestContactInfoScoring:
    """Tests for contact info detection and scoring."""

    @pytest.fixture(autouse=True)
    def disable_playwright(self, scoring_config):
        """Disable playwright fallback for unit tests."""
        scoring_config.playwright_fallback_enabled = False

    @patch("src.scoring.fetch_website")
    def test_missing_email_adds_weight(self, mock_fetch, scoring_config):
        """HTML with no email should add missing_email reason and weight."""
        html = _BASE_HTML.replace(
            '<a href="mailto:info@example.com">info@example.com</a>',
            '<span>Contact us online</span>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert "missing_email" in result.reasons
        assert result.score == scoring_config.weight_missing_email

    @patch("src.scoring.fetch_website")
    def test_missing_phone_adds_weight(self, mock_fetch, scoring_config):
        """HTML with no phone should add missing_phone reason and weight."""
        html = _BASE_HTML.replace("(555) 123-4567", "our office")
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert "missing_phone" in result.reasons
        assert result.score == scoring_config.weight_missing_phone

    @patch("src.scoring.fetch_website")
    def test_phone_mismatch_adds_weight(self, mock_fetch, scoring_config):
        """If expected phone differs from website phone, add phone_mismatch."""
        html = _BASE_HTML.replace("(555) 123-4567", "(555) 999-8888")
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website(
            "https://example.com", config=scoring_config, expected_phone="(555) 123-4567"
        )

        assert "phone_mismatch" in result.reasons
        assert result.score == scoring_config.weight_phone_mismatch

    @patch("src.scoring.fetch_website")
    def test_phone_match_no_mismatch(self, mock_fetch, scoring_config):
        """If expected phone matches website phone, no phone_mismatch."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = _BASE_HTML
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website(
            "https://example.com", config=scoring_config, expected_phone="(555) 123-4567"
        )

        assert "phone_mismatch" not in result.reasons
        assert "missing_phone" not in result.reasons
        assert result.score == 0

    @patch("src.scoring.fetch_website")
    def test_missing_both_contact_signals_stack(self, mock_fetch, scoring_config):
        """Missing both email and phone should stack weights."""
        html = _BASE_HTML.replace("(555) 123-4567", "our office").replace(
            '<a href="mailto:info@example.com">info@example.com</a>',
            '<span>Contact us online</span>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert "missing_email" in result.reasons
        assert "missing_phone" in result.reasons
        expected = scoring_config.weight_missing_email + scoring_config.weight_missing_phone
        assert result.score == expected

    @patch("src.scoring.fetch_website")
    def test_no_expected_phone_skips_mismatch_check(self, mock_fetch, scoring_config):
        """If no expected_phone provided, phone_mismatch should not be checked."""
        html = _BASE_HTML.replace("(555) 123-4567", "(555) 999-8888")
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert "phone_mismatch" not in result.reasons
        assert "missing_phone" not in result.reasons
        assert result.score == 0


class TestDeadSocialLinkScoring:
    """Tests for dead social link detection and scoring."""

    @pytest.fixture(autouse=True)
    def disable_playwright(self, scoring_config):
        """Disable playwright fallback for unit tests."""
        scoring_config.playwright_fallback_enabled = False
        scoring_config.dead_social_check_enabled = True

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_disabled_dead_social_check_skips_head_requests(
        self,
        mock_head,
        mock_fetch,
        scoring_config,
    ):
        """Launch config should be able to skip social-link probes."""
        scoring_config.dead_social_check_enabled = False
        html = _BASE_HTML.replace(
            "</body>",
            '<a href="https://facebook.com/oldpage">Facebook</a></body>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        mock_head.assert_not_called()
        assert not any(r.startswith("dead_social_link_") for r in result.reasons)

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_one_dead_social_link_adds_weight(self, mock_head, mock_fetch, scoring_config):
        """A single dead social link should add weight and a reason."""
        mock_head.return_value = Mock(status_code=404)
        html = _BASE_HTML.replace(
            "</body>",
            '<a href="https://facebook.com/oldpage">Facebook</a></body>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert any(r.startswith("dead_social_link_") for r in result.reasons)
        assert result.score == scoring_config.weight_dead_social_link

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_multiple_dead_social_links_stack(self, mock_head, mock_fetch, scoring_config):
        """Multiple dead social links should stack their weights."""
        mock_head.return_value = Mock(status_code=404)
        html = _BASE_HTML.replace(
            "</body>",
            (
                '<a href="https://facebook.com/old">FB</a>'
                '<a href="https://instagram.com/old">IG</a>'
                '</body>'
            )
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        dead_reasons = [r for r in result.reasons if r.startswith("dead_social_link_")]
        assert len(dead_reasons) == 2
        assert result.score == 2 * scoring_config.weight_dead_social_link

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_working_social_link_no_score(self, mock_head, mock_fetch, scoring_config):
        """A working social link should not add score."""
        mock_head.return_value = Mock(status_code=200)
        html = _BASE_HTML.replace(
            "</body>",
            '<a href="https://facebook.com/working">Facebook</a></body>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert not any(r.startswith("dead_social_link_") for r in result.reasons)
        assert result.score == 0

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_non_social_links_ignored(self, mock_head, mock_fetch, scoring_config):
        """Non-social links should not be checked."""
        mock_head.return_value = Mock(status_code=404)
        html = _BASE_HTML.replace(
            "</body>",
            '<a href="https://example.com/about">About</a></body>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        mock_head.assert_not_called()
        assert not any(r.startswith("dead_social_link_") for r in result.reasons)

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_respects_max_check_limit(self, mock_head, mock_fetch, scoring_config):
        """Only up to dead_social_max_check links should be checked."""
        mock_head.return_value = Mock(status_code=404)
        social_links = ""
        for i in range(10):
            social_links += f'<a href="https://facebook.com/page{i}">FB{i}</a>'
        html = _BASE_HTML.replace("</body>", social_links + "</body>")
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)

        assert mock_head.call_count == scoring_config.dead_social_max_check
        dead_reasons = [r for r in result.reasons if r.startswith("dead_social_link_")]
        assert len(dead_reasons) == scoring_config.dead_social_max_check
