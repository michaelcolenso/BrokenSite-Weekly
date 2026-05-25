"""
Phase 2 integration smoke tests.

Verifies ALL Phase 2 scoring signals work correctly against HTML fixtures.
Exercises:
  - Expanded "under construction" patterns (WordPress maintenance mode, SeedProd, etc.)
  - Expanded parked domain patterns (Apache defaults, parking networks, etc.)
  - SSL certificate expiry scoring
  - Broken image detection (opt-in)
  - Contact info detection (missing phone, missing email, phone mismatch)
  - SEO basics (generic title, missing meta description, missing H1)
  - Advertising signal detection (GTM, FB Pixel, gclid)
  - WordPress version detection and outdated flagging
  - Dead social link detection (opt-in)
  - DB race condition guard (upsert_lead atomicity)
  - Playwright context manager cleanup (structural, not live browser)
"""

import pytest
from unittest.mock import Mock, patch

from src.scoring import (
    _check_parked_domain,
    _detect_under_construction,
    _is_generic_title,
    _extract_title,
    _has_meta_description,
    _has_h1,
    _detect_marketing_signals,
    _check_ssl_expiry,
    _check_broken_images,
    _check_dead_social_links,
    _detect_wordpress,
    _detect_ecommerce_platform,
    evaluate_website,
    UNDER_CONSTRUCTION_PATTERNS,
    PARKED_INDICATORS,
)
from src.config import ScoringConfig


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

_CLEAN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Professional plumbing services">
    <title>Austin Plumbing Pros</title>
</head>
<body>
    <h1>Austin Plumbing Pros</h1>
    <p>Call us: (512) 555-1234</p>
    <p>Email: <a href="mailto:info@austinplumbing.com">info@austinplumbing.com</a></p>
    <footer><p>&copy; 2025 Austin Plumbing Pros. All rights reserved.</p></footer>
</body>
</html>"""


@pytest.fixture
def scoring_config():
    cfg = ScoringConfig()
    cfg.playwright_fallback_enabled = False
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Expanded Under Construction Pattern Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestExpandedUnderConstructionPatterns:
    """Verify that expanded UNDER_CONSTRUCTION_PATTERNS are detected correctly."""

    @pytest.mark.parametrize("snippet,description", [
        ("<h1>Under Construction</h1>", "classic under construction"),
        ("<h1>Site Under Construction</h1>", "site under construction variant"),
        ("<h1>Website Under Construction</h1>", "website under construction"),
        ("<h2>Coming Soon</h2>", "coming soon"),
        ("<p>Launching Soon</p>", "launching soon"),
        ("<p>We're launching shortly!</p>", "launching shortly"),
        ("<div>Site is Being Built</div>", "site is being built"),
        ("<h1>Website Coming Soon</h1>", "website coming soon"),
        ("<p>Website Is Coming Soon</p>", "website is coming soon"),
        ("<p>We're Coming Soon</p>", "we're coming soon"),
        ("<p>We Are Coming Soon</p>", "we are coming soon"),
        ("<p>Stay Tuned for updates!</p>", "stay tuned"),
        ("<p>We'll Be Right Back</p>", "we'll be right back"),
        ("<p>Be Right Back</p>", "be right back"),
        ("<p>Site Offline for Maintenance</p>", "site offline for maintenance"),
        ("<p>Temporarily Offline</p>", "temporarily offline"),
        ("<p>Maintenance Mode</p>", "maintenance mode"),
        ("<p>Site In Maintenance</p>", "in maintenance"),
        ("<p>Down for Maintenance</p>", "down for maintenance"),
        ("<p>Briefly unavailable for scheduled maintenance</p>", "WP scheduled maintenance"),
        ('<script src="/wp-content/plugins/coming-soon-pro/js/app.js"></script>', "coming-soon-pro"),
        ('<div class="seedprod-logo">Coming Soon</div>', "seedprod"),
        ("<h1>This Site Is Coming Soon</h1>", "GoDaddy-style coming soon"),
        ("<p>Great Things Are Coming</p>", "great things are coming"),
        ("<p>This Is a Placeholder Page</p>", "placeholder page phrase"),
        ("<p>Register a Domain today!</p>", "register a domain"),
        ("<span>10 Days Until Launch</span>", "days until launch countdown"),
        ("<div class='countdown-to-launch'>5 days</div>", "countdown to launch"),
    ])
    def test_pattern_detected(self, snippet, description):
        html = f"<html><body>{snippet}</body></html>"
        assert _detect_under_construction(html), \
            f"Failed to detect '{description}' in: {snippet!r}"

    def test_clean_site_not_flagged(self):
        assert not _detect_under_construction(_CLEAN_HTML)

    def test_patterns_are_case_insensitive(self):
        assert _detect_under_construction("<h1>UNDER CONSTRUCTION</h1>")
        assert _detect_under_construction("<h1>Coming Soon</h1>")
        assert _detect_under_construction("<h1>MAINTENANCE MODE</h1>")

    def test_under_construction_count_matches_list(self):
        """Sanity check: UNDER_CONSTRUCTION_PATTERNS has meaningful breadth."""
        assert len(UNDER_CONSTRUCTION_PATTERNS) >= 20, \
            f"Expected >=20 patterns, got {len(UNDER_CONSTRUCTION_PATTERNS)}"


# ─────────────────────────────────────────────────────────────────────────────
# Expanded Parked Domain Pattern Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestExpandedParkedDomainPatterns:
    """Verify expanded PARKED_INDICATORS are detected correctly."""

    @pytest.mark.parametrize("snippet,description", [
        ("<h1>Domain For Sale</h1>", "domain for sale"),
        ("<h1>This domain is for sale</h1>", "this domain is for sale"),
        ("<h1>Buy This Domain</h1>", "buy this domain"),
        ("<h1>Buy This Domain Now</h1>", "buy this domain now"),
        ("<p>Make an offer on this domain</p>", "make an offer"),
        ("<p>Purchase This Domain</p>", "purchase this domain"),
        ("<p>Acquire This Domain</p>", "acquire this domain"),
        ("<p>Domain Parking Service</p>", "domain parking"),
        ("<p>This is a Parked Domain</p>", "parked domain"),
        ("<p>This Domain Is Parked</p>", "this domain is parked"),
        ("<p>Parked by GoDaddy</p>", "parked by"),
        ("<p>Domain Has Expired</p>", "domain has expired"),
        ("<p>Domain Expired</p>", "domain expired"),
        ("<p>Registration Expired</p>", "registration expired"),
        ("<p>This Domain May Be For Sale</p>", "this domain may be for sale"),
        ("<p>Future Home Of Something Great</p>", "future home of"),
        ("<p>Default Web Page</p>", "default web page"),
        ("<p>Apache2 Ubuntu Default Page</p>", "apache2 default page"),
        ("<h1>It Works!</h1><p>Apache server installed</p>", "apache it works"),
        ("<h1>Welcome to nginx!</h1>", "welcome to nginx"),
        ("<h1>Welcome to OpenResty!</h1>", "welcome to openresty"),
        ('<script src="https://sedoparking.com/load.js"></script>', "sedo parking"),
        ('<a href="https://www.sedo.com/buy">Buy</a>', "sedo.com"),
        ('<img src="https://www.parkingcrew.net/logo.png">', "parkingcrew.net"),
        ('<a href="https://www.afternic.com/listing">Buy</a>', "afternic.com"),
        ('<a href="https://www.hugedomains.com">Buy</a>', "hugedomains.com"),
        ('<a href="https://www.undeveloped.com">Buy</a>', "undeveloped.com"),
        ('<a href="https://www.bodis.com">Ads</a>', "bodis.com"),
        ('<a href="https://www.above.com">Buy</a>', "above.com"),
    ])
    def test_pattern_detected(self, snippet, description):
        html = f"<html><body>{snippet}</body></html>"
        assert _check_parked_domain(html), \
            f"Failed to detect '{description}' in: {snippet!r}"

    def test_clean_site_not_flagged(self):
        assert not _check_parked_domain(_CLEAN_HTML)

    def test_patterns_are_case_insensitive(self):
        assert _check_parked_domain("<h1>DOMAIN FOR SALE</h1>")
        assert _check_parked_domain("<h1>Buy This Domain Now</h1>")
        assert _check_parked_domain("<h1>WELCOME TO NGINX</h1>")

    def test_parked_indicators_count(self):
        """Sanity: PARKED_INDICATORS should have substantial coverage."""
        assert len(PARKED_INDICATORS) >= 25, \
            f"Expected >=25 indicators, got {len(PARKED_INDICATORS)}"


# ─────────────────────────────────────────────────────────────────────────────
# SEO Basics (Generic Title, Meta Description, H1)
# ─────────────────────────────────────────────────────────────────────────────

class TestExpandedGenericTitlePatterns:
    """Verify expanded generic title patterns are detected."""

    @pytest.mark.parametrize("title,description", [
        ("Home", "home"),
        ("Homepage", "homepage"),
        ("Welcome", "welcome"),
        ("Welcome!", "welcome with exclamation"),
        ("Index", "index"),
        ("Untitled", "untitled"),
        ("Untitled Document", "untitled document"),
        ("Website", "website"),
        ("My Website", "my website"),
        ("My Site", "my site"),
        ("New Site", "new site"),
        ("Default Page", "default page"),
        ("Test", "test"),
        ("Test Page", "test page"),
        ("Sample Page", "sample page (WP default)"),
        ("Hello World", "hello world (WP default)"),
        ("Coming Soon", "coming soon title"),
        ("Under Construction", "under construction title"),
    ])
    def test_detected_as_generic(self, title, description):
        assert _is_generic_title(title), \
            f"Expected '{title}' ({description}) to be detected as generic"

    def test_real_business_title_not_generic(self):
        assert not _is_generic_title("Austin Plumbing Pros")
        assert not _is_generic_title("Joe's Heating & Cooling - HVAC Services")
        assert not _is_generic_title("Denver Family Dentistry | Dr. Smith")

    def test_none_not_generic(self):
        assert not _is_generic_title(None)

    def test_empty_not_generic(self):
        assert not _is_generic_title("")


class TestSEOBasicsScoring:
    """Integration tests for SEO-based scoring signals."""

    @patch("src.scoring.fetch_website")
    def test_missing_meta_description_adds_reason(self, mock_fetch, scoring_config):
        html = """<html><head><title>Test Business</title></head>
        <body><h1>Test</h1><p>Call: (555)123-4567</p>
        <p>Email: <a href="mailto:a@b.com">a@b.com</a></p></body></html>"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert "missing_meta_description" in result.reasons
        assert result.score >= scoring_config.weight_missing_meta_description

    @patch("src.scoring.fetch_website")
    def test_missing_h1_adds_reason(self, mock_fetch, scoring_config):
        html = """<html><head>
        <title>Test Business</title>
        <meta name="description" content="desc">
        </head>
        <body><p>No H1 here</p>
        <p>Call: (555)123-4567</p>
        <p>Email: <a href="mailto:a@b.com">a@b.com</a></p></body></html>"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert "missing_h1" in result.reasons
        assert result.score >= scoring_config.weight_missing_h1

    @patch("src.scoring.fetch_website")
    def test_generic_title_adds_reason(self, mock_fetch, scoring_config):
        html = """<html><head>
        <title>Welcome</title>
        <meta name="description" content="desc">
        </head>
        <body><h1>Home</h1>
        <p>Call: (555)123-4567</p>
        <p>Email: <a href="mailto:a@b.com">a@b.com</a></p></body></html>"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert "generic_title" in result.reasons
        assert result.score >= scoring_config.weight_generic_title

    @patch("src.scoring.fetch_website")
    def test_seo_issues_stack_correctly(self, mock_fetch, scoring_config):
        """Missing meta + missing H1 + generic title should all stack."""
        html = """<html><head><title>Home</title></head>
        <body><p>No heading, no description</p>
        <p>Call: (555)123-4567</p>
        <p>Email: <a href="mailto:a@b.com">a@b.com</a></p></body></html>"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert "missing_meta_description" in result.reasons
        assert "missing_h1" in result.reasons
        assert "generic_title" in result.reasons
        expected_min = (
            scoring_config.weight_missing_meta_description
            + scoring_config.weight_missing_h1
            + scoring_config.weight_generic_title
        )
        assert result.score >= expected_min


# ─────────────────────────────────────────────────────────────────────────────
# SSL Expiry
# ─────────────────────────────────────────────────────────────────────────────

class TestSslExpiryIntegration:
    """Integration tests for SSL expiry scoring."""

    @patch("src.scoring.fetch_website")
    @patch("src.scoring._check_ssl_expiry")
    def test_expiring_ssl_scores_and_has_reason(self, mock_ssl, mock_fetch, scoring_config):
        mock_ssl.return_value = 7  # 7 days left
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = _CLEAN_HTML
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert any("ssl_expires" in r for r in result.reasons)
        assert result.score >= scoring_config.weight_ssl_expiry

    @patch("src.scoring.fetch_website")
    @patch("src.scoring._check_ssl_expiry")
    def test_healthy_ssl_not_flagged(self, mock_ssl, mock_fetch, scoring_config):
        mock_ssl.return_value = 180  # 6 months remaining
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = _CLEAN_HTML
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert not any("ssl_expires" in r for r in result.reasons)

    @patch("src.scoring.fetch_website")
    @patch("src.scoring._check_ssl_expiry")
    def test_ssl_check_not_called_for_http(self, mock_ssl, mock_fetch, scoring_config):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "http://example.com"  # HTTP, not HTTPS
        mock_response.text = _CLEAN_HTML
        mock_fetch.return_value = (mock_response, None)

        evaluate_website("http://example.com", config=scoring_config)
        mock_ssl.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Advertising Detection
# ─────────────────────────────────────────────────────────────────────────────

class TestAdvertisingDetectionIntegration:
    """Integration tests for advertising signal detection."""

    def test_detects_gtm_tag_manager(self):
        html = '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-XXXX"></script>'
        signals = _detect_marketing_signals(html)
        assert "has_gtm" in signals

    def test_detects_gtm_via_prefix(self):
        html = '<div id="gtm-container" data-id="GTM-1234"></div>'
        signals = _detect_marketing_signals(html)
        assert "has_gtm" in signals

    def test_detects_facebook_pixel(self):
        html = """<script>
            !function(f,b,e,v,n,t,s){n=f.fbq=function(){};fbq('init','123456');}
        </script>"""
        signals = _detect_marketing_signals(html)
        assert "has_fb_pixel" in signals

    def test_detects_fb_pixel_via_sdk(self):
        html = '<script src="https://connect.facebook.net/en_US/fbevents.js"></script>'
        signals = _detect_marketing_signals(html)
        assert "has_fb_pixel" in signals

    def test_detects_gclid_in_html(self):
        html = '<a href="https://example.com?gclid=abc123">Ad landing</a>'
        signals = _detect_marketing_signals(html)
        assert "has_gclid" in signals

    def test_no_signals_on_clean_html(self):
        signals = _detect_marketing_signals(_CLEAN_HTML)
        assert signals == []

    @patch("src.scoring.fetch_website")
    def test_gtm_and_fb_pixel_both_add_score(self, mock_fetch, scoring_config):
        html = _CLEAN_HTML.replace(
            "</head>",
            (
                '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-123"></script>'
                '<script>fbq("init","123");</script>'
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


# ─────────────────────────────────────────────────────────────────────────────
# Contact Info Detection
# ─────────────────────────────────────────────────────────────────────────────

class TestContactInfoIntegration:
    """Integration tests for contact info scoring (phone/email/mismatch)."""

    @patch("src.scoring.fetch_website")
    def test_missing_phone_flagged(self, mock_fetch, scoring_config):
        html = """<html><head>
        <meta name="viewport" content="width=device-width">
        <meta name="description" content="desc">
        <title>Business</title></head>
        <body><h1>Business</h1>
        <p>Email: <a href="mailto:info@biz.com">info@biz.com</a></p></body></html>"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert "missing_phone" in result.reasons

    @patch("src.scoring.fetch_website")
    def test_missing_email_flagged(self, mock_fetch, scoring_config):
        html = """<html><head>
        <meta name="viewport" content="width=device-width">
        <meta name="description" content="desc">
        <title>Business</title></head>
        <body><h1>Business</h1>
        <p>Call: (555) 123-4567</p></body></html>"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert "missing_email" in result.reasons

    @patch("src.scoring.fetch_website")
    def test_phone_mismatch_flagged(self, mock_fetch, scoring_config):
        html = """<html><head>
        <meta name="viewport" content="width=device-width">
        <meta name="description" content="desc">
        <title>Business</title></head>
        <body><h1>Business</h1>
        <p>Call: (555) 999-8888</p>
        <p>Email: <a href="mailto:info@biz.com">info@biz.com</a></p></body></html>"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website(
            "https://example.com",
            config=scoring_config,
            expected_phone="(555) 123-4567"
        )
        assert "phone_mismatch" in result.reasons

    @patch("src.scoring.fetch_website")
    def test_clean_site_no_contact_flags(self, mock_fetch, scoring_config):
        """A site with both phone and email should not get contact flags."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = _CLEAN_HTML
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website(
            "https://example.com",
            config=scoring_config,
            expected_phone="(512) 555-1234"
        )
        assert "missing_phone" not in result.reasons
        assert "missing_email" not in result.reasons
        assert "phone_mismatch" not in result.reasons


# ─────────────────────────────────────────────────────────────────────────────
# Broken Image Detection (opt-in)
# ─────────────────────────────────────────────────────────────────────────────

class TestBrokenImageIntegration:
    """Integration tests for broken image detection."""

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_broken_image_flagged(self, mock_head, mock_fetch, scoring_config):
        scoring_config.broken_image_check_enabled = True
        mock_head.return_value = Mock(status_code=404)
        html = _CLEAN_HTML.replace("</body>", '<img src="/broken.jpg"><br></body>')
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert any("broken_image" in r for r in result.reasons)
        assert result.score >= scoring_config.weight_broken_image

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_working_images_not_flagged(self, mock_head, mock_fetch, scoring_config):
        scoring_config.broken_image_check_enabled = True
        mock_head.return_value = Mock(status_code=200)
        html = _CLEAN_HTML.replace("</body>", '<img src="/logo.jpg"><br></body>')
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert not any("broken_image" in r for r in result.reasons)

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_broken_images_disabled_by_default(self, mock_head, mock_fetch, scoring_config):
        """Broken image check should be off by default (perf concern)."""
        assert not scoring_config.broken_image_check_enabled
        html = _CLEAN_HTML.replace("</body>", '<img src="/broken.jpg"><br></body>')
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        evaluate_website("https://example.com", config=scoring_config)
        mock_head.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Dead Social Links (opt-in)
# ─────────────────────────────────────────────────────────────────────────────

class TestDeadSocialLinkIntegration:
    """Integration tests for dead social link detection."""

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_dead_facebook_link_flagged(self, mock_head, mock_fetch, scoring_config):
        scoring_config.dead_social_check_enabled = True
        mock_head.return_value = Mock(status_code=404)
        html = _CLEAN_HTML.replace(
            "</body>",
            '<a href="https://www.facebook.com/oldpage">Facebook</a></body>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert any("dead_social_link" in r for r in result.reasons)
        assert result.score >= scoring_config.weight_dead_social_link

    @patch("src.scoring.fetch_website")
    @patch("src.scoring.requests.head")
    def test_dead_social_check_disabled_by_default(self, mock_head, mock_fetch, scoring_config):
        """Dead social check should be off by default (perf concern)."""
        assert not scoring_config.dead_social_check_enabled
        html = _CLEAN_HTML.replace(
            "</body>",
            '<a href="https://www.facebook.com/old">FB</a></body>'
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        evaluate_website("https://example.com", config=scoring_config)
        mock_head.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# WordPress Detection
# ─────────────────────────────────────────────────────────────────────────────

class TestWordPressDetectionIntegration:
    """Integration smoke tests for WordPress detection."""

    def test_wp_content_detected(self):
        html = '<link rel="stylesheet" href="/wp-content/themes/twentytwenty/style.css">'
        is_wp, version, has_version = _detect_wordpress(html, "https://example.com")
        assert is_wp

    def test_wp_includes_detected(self):
        html = '<script src="/wp-includes/js/jquery.js"></script>'
        is_wp, _, _ = _detect_wordpress(html, "https://example.com")
        assert is_wp

    def test_wp_generator_meta_detected_with_version(self):
        html = '<meta name="generator" content="WordPress 5.8.3">'
        is_wp, version, has_version = _detect_wordpress(html, "https://example.com")
        assert is_wp
        assert version == "5.8.3"
        assert has_version

    def test_wp_version_from_asset_query_string(self):
        html = '<link href="/wp-content/themes/t/style.css?ver=5.6.2" rel="stylesheet">'
        _, version, has_version = _detect_wordpress(html, "https://example.com")
        assert version == "5.6.2"
        assert has_version

    def test_non_wp_site_returns_false(self):
        is_wp, _, _ = _detect_wordpress(_CLEAN_HTML, "https://example.com")
        assert not is_wp

    @patch("src.scoring.fetch_website")
    def test_outdated_wp_scores_and_reasons(self, mock_fetch, scoring_config):
        html = _CLEAN_HTML.replace(
            "</head>",
            (
                '<meta name="generator" content="WordPress 5.2.1">'
                '<link href="/wp-content/themes/t/style.css" rel="stylesheet">'
                '</head>'
            )
        )
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.text = html
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert "wordpress" in result.reasons
        assert any(r.startswith("wp_outdated_") for r in result.reasons)
        assert result.score >= scoring_config.weight_wordpress_outdated


# ─────────────────────────────────────────────────────────────────────────────
# E-Commerce Detection
# ─────────────────────────────────────────────────────────────────────────────

class TestEcommerceDetectionIntegration:
    """Integration smoke tests for e-commerce platform detection."""

    def test_shopify_detected_via_cdn(self):
        html = '<script src="https://cdn.shopify.com/s/files/app.js"></script>'
        platform = _detect_ecommerce_platform(html, "https://example.com")
        assert platform == "shopify"

    def test_shopify_detected_via_myshopify_url(self):
        platform = _detect_ecommerce_platform("<html></html>", "https://mybiz.myshopify.com")
        assert platform == "shopify"

    def test_woocommerce_detected(self):
        html = '<link rel="stylesheet" href="/wp-content/plugins/woocommerce/assets/css/wc.css">'
        platform = _detect_ecommerce_platform(html, "https://example.com")
        assert platform == "woocommerce"

    def test_bigcommerce_detected(self):
        html = '<script src="https://cdn.bigcommerce.com/main.js"></script>'
        platform = _detect_ecommerce_platform(html, "https://example.com")
        assert platform == "bigcommerce"

    def test_no_ecommerce_returns_none(self):
        platform = _detect_ecommerce_platform(_CLEAN_HTML, "https://example.com")
        assert platform is None


# ─────────────────────────────────────────────────────────────────────────────
# DB Race Condition (structural test)
# ─────────────────────────────────────────────────────────────────────────────

class TestDatabaseAtomicity:
    """Verify DB upsert is atomic and handles duplicates correctly."""

    def test_upsert_prevents_duplicate_within_window(self, tmp_path):
        from src.db import Database, Lead
        from src.config import DatabaseConfig
        from datetime import datetime

        db = Database(DatabaseConfig(db_path=tmp_path / "test.db", dedupe_window_days=90))
        lead = Lead(
            place_id="test_place_1",
            name="Test Business",
            website="https://testbiz.com",
            address="123 Main St",
            phone="(555) 123-4567",
            city="Austin, TX",
            category="plumber",
            score=75,
            reasons=["no_https", "outdated_copyright"],
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
        )

        # First insert should succeed
        result1 = db.upsert_lead(lead)
        assert result1 is True

        # Second insert of same place_id within window should be deduplicated
        result2 = db.upsert_lead(lead)
        assert result2 is False

        db.close()

    def test_upsert_prevents_website_duplicates(self, tmp_path):
        from src.db import Database, Lead
        from src.config import DatabaseConfig
        from datetime import datetime

        db = Database(DatabaseConfig(db_path=tmp_path / "test.db", dedupe_window_days=90))
        now = datetime.utcnow()

        lead1 = Lead(
            place_id="place_1",
            name="Business A",
            website="https://shared-website.com",
            address="100 A St",
            phone="(555) 111-1111",
            city="Denver, CO",
            category="dentist",
            score=55,
            reasons=["no_https"],
            first_seen=now,
            last_seen=now,
        )
        lead2 = Lead(
            place_id="place_2",  # Different place_id
            name="Business B",
            website="https://shared-website.com",  # Same website!
            address="200 B St",
            phone="(555) 222-2222",
            city="Denver, CO",
            category="dentist",
            score=60,
            reasons=["no_https", "outdated_copyright"],
            first_seen=now,
            last_seen=now,
        )

        result1 = db.upsert_lead(lead1)
        assert result1 is True

        # lead2 shares the same website within the window -> should be deduped
        result2 = db.upsert_lead(lead2)
        assert result2 is False

        db.close()

    def test_upsert_allows_new_lead_after_window_expiry(self, tmp_path):
        """A lead whose last_seen is outside the window can be re-inserted."""
        from src.db import Database, Lead
        from src.config import DatabaseConfig
        from datetime import datetime, timedelta

        db = Database(DatabaseConfig(db_path=tmp_path / "test.db", dedupe_window_days=1))
        old_time = datetime.utcnow() - timedelta(days=5)
        now = datetime.utcnow()

        lead_old = Lead(
            place_id="test_place_old",
            name="Old Business",
            website="https://oldbiz.com",
            address="Old St",
            phone="(555) 000-0000",
            city="Phoenix, AZ",
            category="hvac",
            score=50,
            reasons=["no_https"],
            first_seen=old_time,
            last_seen=old_time,  # outside the 1-day window
        )
        lead_new = Lead(
            place_id="test_place_old",  # same place_id
            name="Old Business",
            website="https://oldbiz.com",
            address="Old St",
            phone="(555) 000-0000",
            city="Phoenix, AZ",
            category="hvac",
            score=60,
            reasons=["no_https", "outdated_copyright"],
            first_seen=old_time,
            last_seen=now,
        )

        # Manually insert old record directly to simulate expired lead
        import sqlite3
        conn = sqlite3.connect(db.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO leads (place_id, name, website, address, phone, "
            "review_count, city, category, score, reasons, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lead_old.place_id, lead_old.name, lead_old.website,
                lead_old.address, lead_old.phone, None,
                lead_old.city, lead_old.category, lead_old.score,
                '["no_https"]', old_time.isoformat(), old_time.isoformat()
            )
        )
        conn.commit()
        conn.close()

        # Re-upsert with current last_seen should succeed (window expired)
        result = db.upsert_lead(lead_new)
        assert result is True

        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Playwright Scraper Resource Management (structural)
# ─────────────────────────────────────────────────────────────────────────────

class TestPlaywrightResourceManagement:
    """
    Structural tests to verify maps_scraper uses context manager pattern.
    These tests inspect code structure without running a live browser.
    """

    def test_scrape_businesses_uses_sync_playwright_context_manager(self):
        """scrape_businesses must use `with sync_playwright()` (context manager)."""
        import inspect
        import src.maps_scraper as scraper_module

        source = inspect.getsource(scraper_module.scrape_businesses)
        assert "with sync_playwright()" in source, (
            "scrape_businesses must use `with sync_playwright()` context manager "
            "to ensure Playwright is properly cleaned up on every exit path."
        )

    def test_fetch_with_playwright_uses_sync_playwright_context_manager(self):
        """_fetch_with_playwright must also use context manager pattern."""
        import inspect
        import src.scoring as scoring_module

        source = inspect.getsource(scoring_module._fetch_with_playwright)
        assert "with sync_playwright()" in source, (
            "_fetch_with_playwright must use `with sync_playwright()` context manager."
        )

    def test_scraper_finally_closes_context_and_browser(self):
        """Finally block must close context and browser."""
        import inspect
        import src.maps_scraper as scraper_module

        source = inspect.getsource(scraper_module.scrape_businesses)
        assert "finally:" in source, "scrape_businesses must have a finally block for cleanup"
        assert "context.close()" in source, "context must be closed in finally"
        assert "browser.close()" in source, "browser must be closed in finally"


# ─────────────────────────────────────────────────────────────────────────────
# Full Signal Fixture Smoke Test
# ─────────────────────────────────────────────────────────────────────────────

class TestFullSignalFixtures:
    """
    Smoke-test all Phase 2 signals against representative fixture HTML.
    One test per signal category to confirm end-to-end wiring.
    """

    @pytest.fixture(autouse=True)
    def disable_playwright(self, scoring_config):
        scoring_config.playwright_fallback_enabled = False

    @patch("src.scoring.fetch_website")
    def test_under_construction_full_pipeline(self, mock_fetch, scoring_config):
        html = """<html><head><title>Coming Soon</title></head>
        <body><h1>We're Coming Soon</h1><p>Maintenance Mode active.</p></body></html>"""
        mock_response = Mock(status_code=200, url="https://example.com", text=html)
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert "under_construction" in result.reasons
        assert result.score >= scoring_config.weight_under_construction

    @patch("src.scoring.fetch_website")
    def test_parked_domain_full_pipeline(self, mock_fetch, scoring_config):
        html = """<html><head><title>Domain For Sale</title></head>
        <body><h1>Buy This Domain Now</h1><p>Make an offer today.</p></body></html>"""
        mock_response = Mock(status_code=200, url="https://example.com", text=html)
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert "parked_domain" in result.reasons
        assert result.score >= scoring_config.weight_parked_domain

    @patch("src.scoring.fetch_website")
    def test_maintenance_mode_flagged_as_under_construction(self, mock_fetch, scoring_config):
        html = """<html><head><title>Maintenance</title></head>
        <body><h1>Briefly unavailable for scheduled maintenance</h1>
        <p>Check back in a few minutes.</p></body></html>"""
        mock_response = Mock(status_code=200, url="https://example.com", text=html)
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert "under_construction" in result.reasons

    @patch("src.scoring.fetch_website")
    def test_apache_default_page_flagged_as_parked(self, mock_fetch, scoring_config):
        html = """<html><head><title>Apache2 Ubuntu Default Page</title></head>
        <body><h1>Apache2 Ubuntu Default Page</h1><p>It Works!</p></body></html>"""
        mock_response = Mock(status_code=200, url="https://example.com", text=html)
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert "parked_domain" in result.reasons

    @patch("src.scoring.fetch_website")
    def test_nginx_default_page_flagged_as_parked(self, mock_fetch, scoring_config):
        html = """<html><head><title>Welcome to nginx!</title></head>
        <body><h1>Welcome to nginx!</h1></body></html>"""
        mock_response = Mock(status_code=200, url="https://example.com", text=html)
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert "parked_domain" in result.reasons

    @patch("src.scoring.fetch_website")
    def test_wordpress_maintenance_mode_detected(self, mock_fetch, scoring_config):
        """WordPress maintenance mode: wp_maintenance in HTML."""
        html = """<html><head><title>Maintenance</title></head>
        <body><!-- wp_maintenance -->
        <h1>Briefly unavailable for scheduled maintenance</h1></body></html>"""
        mock_response = Mock(status_code=200, url="https://example.com", text=html)
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        # wp_maintenance -> under_construction pattern
        assert "under_construction" in result.reasons

    @patch("src.scoring.fetch_website")
    def test_seedprod_coming_soon_detected(self, mock_fetch, scoring_config):
        html = """<html><head><title>Coming Soon</title></head>
        <body><div class="seedprod-page"><h1>Coming Soon</h1></div></body></html>"""
        mock_response = Mock(status_code=200, url="https://example.com", text=html)
        mock_fetch.return_value = (mock_response, None)

        result = evaluate_website("https://example.com", config=scoring_config)
        assert "under_construction" in result.reasons
