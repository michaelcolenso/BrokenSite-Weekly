"""
Tests for the warm lead system components:
- audit_generator
- contact_finder
- outreach
- tracking
- warm_delivery
- database extensions
"""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.audit_generator import (
    ISSUE_DESCRIPTIONS,
    _parse_copyright_year,
    _parse_diy_builder,
    _parse_reasons,
    _parse_server_error,
    generate_audit_html,
    get_issues_json,
)
from src.config import OutreachConfig, DeliveryConfig
from src.contact_finder import (
    ContactInfo,
    _clean_email,
    _extract_from_jsonld,
    _extract_mailto,
    _extract_via_regex,
    _find_contact_page_url,
    _is_valid_email,
)


# ============================================================
# Audit Generator Tests
# ============================================================


class TestParseReasons:
    """Tests for _parse_reasons() and related helpers."""

    def test_known_reason(self):
        issues = _parse_reasons("ssl_error")
        assert len(issues) == 1
        assert issues[0]["title"] == "SSL Certificate Error"
        assert issues[0]["severity"] == "critical"

    def test_multiple_reasons(self):
        issues = _parse_reasons("ssl_error,no_https,no_viewport")
        assert len(issues) == 3
        assert issues[0]["title"] == "SSL Certificate Error"
        assert issues[1]["title"] == "No Secure Connection"
        assert issues[2]["title"] == "Not Mobile-Friendly"

    def test_empty_reasons(self):
        assert _parse_reasons("") == []
        assert _parse_reasons(None) == []

    def test_unknown_reason_skipped(self):
        issues = _parse_reasons("ssl_error,unknown_reason,no_https")
        assert len(issues) == 2

    def test_copyright_year_parsed(self):
        issues = _parse_reasons("copyright_2018")
        assert len(issues) == 1
        assert "2018" in issues[0]["title"]
        assert issues[0]["severity"] == "medium"

    def test_server_error_parsed(self):
        issues = _parse_reasons("server_error_500")
        assert len(issues) == 1
        assert "500" in issues[0]["title"]
        assert issues[0]["severity"] == "critical"

    def test_server_error_4xx(self):
        result = _parse_server_error("server_error_404")
        assert result["severity"] == "high"

    def test_diy_builder_parsed(self):
        issues = _parse_reasons("diy_wix")
        assert len(issues) == 1
        assert "Wix" in issues[0]["title"]

    def test_diy_builder_unknown(self):
        assert _parse_diy_builder("diy_unknown_builder") is None


class TestGenerateAuditHtml:
    """Tests for generate_audit_html()."""

    def test_generates_html(self, tmp_path):
        """Test that valid HTML is generated."""
        # Set up templates dir
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        # Copy the template
        template_src = Path(__file__).parent.parent / "templates" / "audit.html"
        if template_src.exists():
            (templates_dir / "audit.html").write_text(
                template_src.read_text(), encoding="utf-8"
            )
        else:
            pytest.skip("Template file not found")

        lead = {
            "place_id": "test123",
            "name": "Test Plumbing",
            "website": "http://testplumbing.com",
            "city": "Austin, TX",
            "category": "plumber",
            "score": 75,
            "reasons": "ssl_error,no_viewport",
        }

        with patch("src.audit_generator.TEMPLATES_DIR", templates_dir):
            html = generate_audit_html(lead, "https://track.example.com")

        assert html is not None
        assert "Test Plumbing" in html
        assert "SSL Certificate Error" in html
        assert "Not Mobile-Friendly" in html
        assert "track.example.com" in html

    def test_returns_none_for_no_issues(self):
        lead = {"place_id": "test", "reasons": ""}
        result = generate_audit_html(lead, "https://track.example.com")
        assert result is None


class TestGetIssuesJson:
    """Tests for get_issues_json()."""

    def test_serializes_issues(self):
        lead = {"reasons": "ssl_error,no_https"}
        result = get_issues_json(lead)
        data = json.loads(result)
        assert len(data) == 2
        assert data[0]["title"] == "SSL Certificate Error"

    def test_empty_reasons(self):
        lead = {"reasons": ""}
        result = get_issues_json(lead)
        assert json.loads(result) == []


# ============================================================
# Contact Finder Tests
# ============================================================


class TestCleanEmail:
    """Tests for _clean_email()."""

    def test_basic_clean(self):
        assert _clean_email("  Test@Example.COM  ") == "test@example.com"

    def test_strips_mailto_prefix(self):
        assert _clean_email("mailto:test@example.com") == "test@example.com"

    def test_strips_query_params(self):
        assert _clean_email("test@example.com?subject=Hi") == "test@example.com"


class TestIsValidEmail:
    """Tests for _is_valid_email()."""

    def test_valid_email(self):
        assert _is_valid_email("contact@business.com") is True

    def test_false_positive_example(self):
        assert _is_valid_email("user@example.com") is False

    def test_false_positive_wixpress(self):
        assert _is_valid_email("something@wixpress.com") is False

    def test_false_positive_sentry(self):
        assert _is_valid_email("error@sentry.io") is False

    def test_short_local_part(self):
        assert _is_valid_email("a@example.org") is False

    def test_invalid_format(self):
        assert _is_valid_email("not-an-email") is False


class TestExtractFromJsonld:
    """Tests for _extract_from_jsonld()."""

    def test_direct_email(self):
        from bs4 import BeautifulSoup

        html = """
        <script type="application/ld+json">
        {"@type": "LocalBusiness", "email": "info@business.com"}
        </script>
        """
        soup = BeautifulSoup(html, "html.parser")
        assert _extract_from_jsonld(soup) == "info@business.com"

    def test_contact_point_email(self):
        from bs4 import BeautifulSoup

        html = """
        <script type="application/ld+json">
        {"@type": "LocalBusiness", "contactPoint": {"email": "support@biz.com"}}
        </script>
        """
        soup = BeautifulSoup(html, "html.parser")
        assert _extract_from_jsonld(soup) == "support@biz.com"

    def test_no_jsonld(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<html><body>No JSON-LD</body></html>", "html.parser")
        assert _extract_from_jsonld(soup) is None

    def test_invalid_json(self):
        from bs4 import BeautifulSoup

        html = '<script type="application/ld+json">not valid json</script>'
        soup = BeautifulSoup(html, "html.parser")
        assert _extract_from_jsonld(soup) is None


class TestExtractMailto:
    """Tests for _extract_mailto()."""

    def test_mailto_link(self):
        from bs4 import BeautifulSoup

        html = '<a href="mailto:hello@business.com">Email Us</a>'
        soup = BeautifulSoup(html, "html.parser")
        assert _extract_mailto(soup) == "hello@business.com"

    def test_mailto_with_query(self):
        from bs4 import BeautifulSoup

        html = '<a href="mailto:info@biz.com?subject=Hello">Contact</a>'
        soup = BeautifulSoup(html, "html.parser")
        assert _extract_mailto(soup) == "info@biz.com"

    def test_no_mailto(self):
        from bs4 import BeautifulSoup

        html = '<a href="https://example.com">Link</a>'
        soup = BeautifulSoup(html, "html.parser")
        assert _extract_mailto(soup) is None


class TestFindContactPageUrl:
    """Tests for _find_contact_page_url()."""

    def test_finds_contact_page(self):
        from bs4 import BeautifulSoup

        html = '<nav><a href="/contact">Contact Us</a></nav>'
        soup = BeautifulSoup(html, "html.parser")
        url = _find_contact_page_url(soup, "https://example.com")
        assert url == "https://example.com/contact"

    def test_finds_about_page(self):
        from bs4 import BeautifulSoup

        html = '<a href="/about-us">About</a>'
        soup = BeautifulSoup(html, "html.parser")
        url = _find_contact_page_url(soup, "https://example.com")
        assert url == "https://example.com/about-us"

    def test_no_contact_page(self):
        from bs4 import BeautifulSoup

        html = '<a href="/services">Services</a>'
        soup = BeautifulSoup(html, "html.parser")
        assert _find_contact_page_url(soup, "https://example.com") is None


class TestExtractViaRegex:
    """Tests for _extract_via_regex()."""

    def test_finds_email_in_text(self):
        html = "<p>Contact us at info@realbusiness.com for inquiries.</p>"
        assert _extract_via_regex(html) == "info@realbusiness.com"

    def test_skips_false_positives(self):
        html = "<p>Template: user@example.com</p>"
        assert _extract_via_regex(html) is None

    def test_no_email(self):
        assert _extract_via_regex("<p>No email here</p>") is None


# ============================================================
# Database Extension Tests
# ============================================================


class TestDatabaseWarmLeadMethods:
    """Tests for warm lead database methods."""

    def test_record_and_get_audit(self, test_database):
        test_database.record_audit(
            "place1", "https://example.com/audit/place1", "/path/audit.html", '[]'
        )
        url = test_database.get_audit_url("place1")
        assert url == "https://example.com/audit/place1"

    def test_get_audit_url_missing(self, test_database):
        assert test_database.get_audit_url("nonexistent") is None

    def test_record_and_get_contact(self, test_database):
        test_database.record_contact("place1", "test@biz.com", "mailto", 0.9)
        contact = test_database.get_contact("place1")
        assert contact is not None
        assert contact["email"] == "test@biz.com"
        assert contact["source"] == "mailto"

    def test_get_contact_missing(self, test_database):
        assert test_database.get_contact("nonexistent") is None

    def test_record_outreach(self, test_database):
        test_database.record_outreach(
            "place1", "test@biz.com", "https://example.com/audit/place1", True
        )
        assert test_database.has_been_contacted("place1") is True

    def test_has_not_been_contacted(self, test_database):
        assert test_database.has_been_contacted("place1") is False

    def test_record_event(self, test_database):
        test_database.record_event("place1", "page_view", "127.0.0.1", "Mozilla/5.0")
        events = test_database.get_events_for_lead("place1")
        assert len(events) == 1
        assert events[0]["event_type"] == "page_view"

    def test_engagement_score_page_view(self, test_database):
        test_database.record_event("place1", "page_view")
        assert test_database.get_engagement_score("place1") == 25

    def test_engagement_score_cta_click(self, test_database):
        test_database.record_event("place1", "cta_click")
        assert test_database.get_engagement_score("place1") == 50

    def test_engagement_score_multiple_events(self, test_database):
        test_database.record_event("place1", "page_view")
        test_database.record_event("place1", "page_view")
        test_database.record_event("place1", "cta_click")
        # 25 + 25 + 50 = 100
        assert test_database.get_engagement_score("place1") == 100

    def test_engagement_score_unsubscribed(self, test_database):
        test_database.record_event("place1", "page_view")
        test_database.add_unsubscribe("place1", "test@biz.com")
        assert test_database.get_engagement_score("place1") == -100

    def test_unsubscribe(self, test_database):
        test_database.add_unsubscribe("place1", "test@biz.com")
        assert test_database.is_unsubscribed("place1") is True
        assert test_database.is_unsubscribed("place2") is False

    def test_get_warm_leads_empty(self, test_database):
        leads = test_database.get_warm_leads()
        assert leads == []


# ============================================================
# Outreach Tests
# ============================================================


class TestOutreachFormatting:
    """Tests for outreach email formatting."""

    def test_format_issue_summary(self):
        from src.outreach import _format_issue_summary

        summary = _format_issue_summary("ssl_error,no_https")
        assert "SSL Certificate Error" in summary
        assert "No Secure Connection" in summary

    def test_format_issue_summary_empty(self):
        from src.outreach import _format_issue_summary

        assert _format_issue_summary("") == ""

    def test_format_issue_summary_html(self):
        from src.outreach import _format_issue_summary_html

        html = _format_issue_summary_html("ssl_error")
        assert "<li>" in html
        assert "SSL Certificate Error" in html

    def test_format_issue_summary_limits(self):
        from src.outreach import _format_issue_summary

        # Only first 3 issues shown
        summary = _format_issue_summary(
            "ssl_error,no_https,no_viewport,parked_domain", max_issues=2
        )
        lines = [l for l in summary.split("\n") if l.strip()]
        assert len(lines) == 2


# ============================================================
# Config Tests
# ============================================================


class TestWarmLeadConfig:
    """Tests for warm lead configuration classes."""

    def test_outreach_config_defaults(self):
        config = OutreachConfig()
        assert config.max_emails_per_day == 100
        assert config.max_emails_per_hour == 20
        assert config.delay_between_emails_seconds == 30
        assert config.min_score_for_outreach == 50
        assert config.min_contact_confidence == 0.7

    def test_delivery_config_defaults(self):
        config = DeliveryConfig()
        assert config.include_cold_leads is True
        assert config.warm_lead_min_engagement == 25

    def test_config_has_outreach(self):
        from src.config import Config

        config = Config()
        assert hasattr(config, "outreach")
        assert hasattr(config, "delivery")
        assert isinstance(config.outreach, OutreachConfig)
        assert isinstance(config.delivery, DeliveryConfig)
