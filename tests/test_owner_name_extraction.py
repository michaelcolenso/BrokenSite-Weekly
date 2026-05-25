"""
Tests for owner/decision-maker name extraction from website HTML.
"""

import pytest
from unittest.mock import Mock, patch
from bs4 import BeautifulSoup

from src.contact_finder import (
    _extract_owner_from_jsonld,
    _is_valid_person_name,
    _extract_owner_from_patterns,
    _find_about_page_url,
    find_owner_name,
    find_contact_email,
    ContactInfo,
)


# ─────────────────────────────────────────────────────────────────────────────
# Name validation
# ─────────────────────────────────────────────────────────────────────────────

class TestValidPersonName:
    """Tests for _is_valid_person_name()."""

    def test_accepts_first_last_name(self):
        assert _is_valid_person_name("John Smith")

    def test_accepts_first_middle_last(self):
        assert _is_valid_person_name("John David Smith")

    def test_accepts_name_with_suffix(self):
        assert _is_valid_person_name("John Smith Jr")

    def test_rejects_single_word(self):
        assert not _is_valid_person_name("John")

    def test_rejects_all_caps(self):
        assert not _is_valid_person_name("JOHN SMITH")

    def test_rejects_lowercase_start(self):
        assert not _is_valid_person_name("john Smith")

    def test_rejects_too_short(self):
        assert not _is_valid_person_name("A B")

    def test_rejects_false_positive_phrases(self):
        assert not _is_valid_person_name("Your Name Here")
        assert not _is_valid_person_name("About Us Team")
        assert not _is_valid_person_name("Contact Us Now")
        assert not _is_valid_person_name("Learn More Today")
        assert not _is_valid_person_name("Copyright Holder")
        assert not _is_valid_person_name("Powered by Shopify")
        assert not _is_valid_person_name("Privacy Policy Page")

    def test_rejects_too_many_words(self):
        assert not _is_valid_person_name("John David Michael Smith Jr")

    def test_rejects_empty(self):
        assert not _is_valid_person_name("")
        assert not _is_valid_person_name(None)


# ─────────────────────────────────────────────────────────────────────────────
# JSON-LD extraction
# ─────────────────────────────────────────────────────────────────────────────

_JSONLD_PERSON = """<script type="application/ld+json">
{"@type": "Person", "name": "Jane Doe"}
</script>"""

_JSONLD_ORG_FOUNDER = """<script type="application/ld+json">
{
  "@type": "Organization",
  "name": "ACME Plumbing",
  "founder": {"@type": "Person", "name": "Bob Roberts"}
}
</script>"""

_JSONLD_ORG_FOUNDER_STRING = """<script type="application/ld+json">
{
  "@type": "LocalBusiness",
  "name": "ACME Plumbing",
  "founder": "Alice Anderson"
}
</script>"""

_JSONLD_ORG_AUTHOR = """<script type="application/ld+json">
{
  "@type": "Organization",
  "name": "BizCo",
  "author": {"@type": "Person", "name": "Charlie Chen"}
}
</script>"""

_JSONLD_NO_PERSON = """<script type="application/ld+json">
{
  "@type": "WebSite",
  "name": "My Business",
  "url": "https://example.com"
}
</script>"""


class TestJsonldOwnerExtraction:
    """Tests for _extract_owner_from_jsonld()."""

    def test_extracts_person_type(self):
        soup = BeautifulSoup(_JSONLD_PERSON, "html.parser")
        result = _extract_owner_from_jsonld(soup)
        assert result is not None
        assert result[0] == "Jane Doe"
        assert result[1] == 0.90

    def test_extracts_org_founder_object(self):
        soup = BeautifulSoup(_JSONLD_ORG_FOUNDER, "html.parser")
        result = _extract_owner_from_jsonld(soup)
        assert result is not None
        assert result[0] == "Bob Roberts"
        assert result[1] == 0.90

    def test_extracts_org_founder_string(self):
        soup = BeautifulSoup(_JSONLD_ORG_FOUNDER_STRING, "html.parser")
        result = _extract_owner_from_jsonld(soup)
        assert result is not None
        assert result[0] == "Alice Anderson"
        assert result[1] == 0.85

    def test_extracts_org_author(self):
        soup = BeautifulSoup(_JSONLD_ORG_AUTHOR, "html.parser")
        result = _extract_owner_from_jsonld(soup)
        assert result is not None
        assert result[0] == "Charlie Chen"
        assert result[1] == 0.80

    def test_no_person_returns_none(self):
        soup = BeautifulSoup(_JSONLD_NO_PERSON, "html.parser")
        result = _extract_owner_from_jsonld(soup)
        assert result is None

    def test_multiple_scripts_returns_first_match(self):
        html = _JSONLD_NO_PERSON + _JSONLD_PERSON
        soup = BeautifulSoup(html, "html.parser")
        result = _extract_owner_from_jsonld(soup)
        assert result is not None
        assert result[0] == "Jane Doe"

    def test_invalid_json_handled_gracefully(self):
        html = '<script type="application/ld+json">{invalid json}</script>'
        soup = BeautifulSoup(html, "html.parser")
        result = _extract_owner_from_jsonld(soup)
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Pattern-based extraction
# ─────────────────────────────────────────────────────────────────────────────

_HTML_OWNER_LABEL = """<html><body>
<p>Owner: John Smith founded the company in 2010.</p>
</body></html>"""

_HTML_FOUNDER_LABEL = """<html><body>
<p>Founder: Mary Johnson has 20 years of experience.</p>
</body></html>"""

_HTML_CEO_LABEL = """<html><body>
<p>CEO: Robert Williams leads our team.</p>
</body></html>"""

_HTML_MEET_OWNER = """<html><body>
<h2>Meet Sarah Davis, our Owner</h2>
<p>Sarah started this business in 2015.</p>
</body></html>"""

_HTML_NAME_ROLE = """<html><body>
<p>Tom Brown, Owner</p>
</body></html>"""

_HTML_AUTHOR_META = """<html><head>
<meta name="author" content="David Wilson">
</head><body></body></html>"""

_HTML_ABOUT_OWNER = """<html><body>
<h2>About the Owner</h2>
<p>Jennifer Taylor brings 15 years of expertise to every project.</p>
</body></html>"""


class TestPatternOwnerExtraction:
    """Tests for _extract_owner_from_patterns()."""

    def test_extracts_owner_labeled(self):
        soup = BeautifulSoup(_HTML_OWNER_LABEL, "html.parser")
        result = _extract_owner_from_patterns(_HTML_OWNER_LABEL, soup)
        assert result is not None
        assert result[0] == "John Smith"
        assert result[1] >= 0.80

    def test_extracts_founder_labeled(self):
        soup = BeautifulSoup(_HTML_FOUNDER_LABEL, "html.parser")
        result = _extract_owner_from_patterns(_HTML_FOUNDER_LABEL, soup)
        assert result is not None
        assert result[0] == "Mary Johnson"

    def test_extracts_ceo_labeled(self):
        soup = BeautifulSoup(_HTML_CEO_LABEL, "html.parser")
        result = _extract_owner_from_patterns(_HTML_CEO_LABEL, soup)
        assert result is not None
        assert result[0] == "Robert Williams"

    def test_extracts_meet_the_owner(self):
        soup = BeautifulSoup(_HTML_MEET_OWNER, "html.parser")
        result = _extract_owner_from_patterns(_HTML_MEET_OWNER, soup)
        assert result is not None
        assert result[0] == "Sarah Davis"

    def test_extracts_name_comma_role(self):
        soup = BeautifulSoup(_HTML_NAME_ROLE, "html.parser")
        result = _extract_owner_from_patterns(_HTML_NAME_ROLE, soup)
        assert result is not None
        assert result[0] == "Tom Brown"

    def test_extracts_author_meta(self):
        soup = BeautifulSoup(_HTML_AUTHOR_META, "html.parser")
        result = _extract_owner_from_patterns(_HTML_AUTHOR_META, soup)
        assert result is not None
        assert result[0] == "David Wilson"
        assert result[1] == 0.60

    def test_no_owner_in_plain_page(self):
        html = "<html><body><p>Welcome to our business.</p></body></html>"
        soup = BeautifulSoup(html, "html.parser")
        result = _extract_owner_from_patterns(html, soup)
        assert result is None

    def test_filters_false_positive_names(self):
        """A role label followed by a non-name phrase should be filtered."""
        html = "<html><body><p>Owner: Contact Us for more information.</p></body></html>"
        soup = BeautifulSoup(html, "html.parser")
        result = _extract_owner_from_patterns(html, soup)
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# About page URL detection
# ─────────────────────────────────────────────────────────────────────────────

class TestFindAboutPage:
    """Tests for _find_about_page_url()."""

    def test_finds_about_page(self):
        html = """<html><body>
        <a href="/about">About</a>
        <a href="/contact">Contact</a>
        </body></html>"""
        soup = BeautifulSoup(html, "html.parser")
        url = _find_about_page_url(soup, "https://example.com")
        assert url == "https://example.com/about"

    def test_finds_about_us(self):
        html = '<a href="/about-us">About Us</a>'
        soup = BeautifulSoup(html, "html.parser")
        url = _find_about_page_url(soup, "https://example.com")
        assert url == "https://example.com/about-us"

    def test_finds_team_page(self):
        html = '<a href="/our-team">Our Team</a>'
        soup = BeautifulSoup(html, "html.parser")
        url = _find_about_page_url(soup, "https://example.com")
        assert url == "https://example.com/our-team"

    def test_finds_meet_the_team(self):
        html = '<a href="/meet-the-team">Meet the Team</a>'
        soup = BeautifulSoup(html, "html.parser")
        url = _find_about_page_url(soup, "https://example.com")
        assert url == "https://example.com/meet-the-team"

    def test_finds_leadership_page(self):
        html = '<a href="/leadership">Leadership</a>'
        soup = BeautifulSoup(html, "html.parser")
        url = _find_about_page_url(soup, "https://example.com")
        assert url == "https://example.com/leadership"

    def test_finds_founder_page(self):
        html = '<a href="/founder">Founder</a>'
        soup = BeautifulSoup(html, "html.parser")
        url = _find_about_page_url(soup, "https://example.com")
        assert url == "https://example.com/founder"

    def test_no_about_page_returns_none(self):
        html = '<a href="/services">Services</a><a href="/contact">Contact</a>'
        soup = BeautifulSoup(html, "html.parser")
        url = _find_about_page_url(soup, "https://example.com")
        assert url is None


# ─────────────────────────────────────────────────────────────────────────────
# Integration: find_owner_name
# ─────────────────────────────────────────────────────────────────────────────

class TestFindOwnerName:
    """Integration tests for find_owner_name()."""

    @patch("src.contact_finder.requests.Session.get")
    def test_finds_owner_from_jsonld_on_homepage(self, mock_get):
        mock_response = Mock()
        mock_response.text = _JSONLD_PERSON
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = find_owner_name("https://example.com")
        assert result is not None
        assert result[0] == "Jane Doe"
        assert result[1] >= 0.85

    @patch("src.contact_finder.requests.Session.get")
    def test_finds_owner_from_pattern_on_homepage(self, mock_get):
        mock_response = Mock()
        mock_response.text = _HTML_OWNER_LABEL
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = find_owner_name("https://example.com")
        assert result is not None
        assert result[0] == "John Smith"

    @patch("src.contact_finder.requests.Session.get")
    def test_falls_back_to_about_page_when_homepage_has_no_owner(self, mock_get):
        homepage = Mock()
        homepage.text = """<html><body>
        <a href="/about">About Us</a>
        <p>Welcome to our business.</p>
        </body></html>"""
        homepage.raise_for_status = Mock()

        about_page = Mock()
        about_page.text = _JSONLD_PERSON
        about_page.raise_for_status = Mock()

        mock_get.side_effect = [homepage, about_page]

        result = find_owner_name("https://example.com")
        assert result is not None
        assert result[0] == "Jane Doe"

    @patch("src.contact_finder.requests.Session.get")
    def test_returns_none_when_no_owner_found(self, mock_get):
        mock_response = Mock()
        mock_response.text = "<html><body><p>Just a business page.</p></body></html>"
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = find_owner_name("https://example.com")
        assert result is None

    @patch("src.contact_finder.requests.Session.get")
    def test_handles_http_error_gracefully(self, mock_get):
        mock_get.side_effect = Exception("Connection refused")

        result = find_owner_name("https://example.com")
        assert result is None

    def test_works_with_preexisting_soup(self):
        """When soup is provided, no HTTP request is made."""
        soup = BeautifulSoup(_JSONLD_PERSON, "html.parser")
        result = find_owner_name(
            "https://example.com", soup=soup
        )
        assert result is not None
        assert result[0] == "Jane Doe"


# ─────────────────────────────────────────────────────────────────────────────
# Integration: find_contact_email now includes owner_name
# ─────────────────────────────────────────────────────────────────────────────

_HTML_WITH_EMAIL_AND_OWNER = """<html><head>
<meta name="author" content="Sarah Johnson">
</head><body>
<a href="mailto:info@abccorp.com">Email Us</a>
<p>Owner: Sarah Johnson founded this company.</p>
</body></html>"""

_HTML_WITH_OWNER_NO_EMAIL = """<html><body>
<p>Owner: Michael Brown started this business.</p>
</body></html>"""


class TestContactInfoIncludesOwner:
    """Tests that find_contact_email now includes owner_name."""

    @patch("src.contact_finder.requests.Session.get")
    def test_email_found_with_owner_name(self, mock_get):
        mock_response = Mock()
        mock_response.text = _HTML_WITH_EMAIL_AND_OWNER
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = find_contact_email("https://example.com")
        assert result is not None
        assert result.email == "info@abccorp.com"
        assert result.owner_name is not None
        assert "Sarah" in result.owner_name or "Johnson" in str(result.owner_name)

    @patch("src.contact_finder.requests.Session.get")
    def test_owner_only_when_no_email_found(self, mock_get):
        mock_response = Mock()
        mock_response.text = _HTML_WITH_OWNER_NO_EMAIL
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = find_contact_email("https://example.com")
        assert result is not None
        assert result.email == ""
        assert result.source == "owner_only"
        assert result.confidence == 0.3
        assert result.owner_name is not None

    @patch("src.contact_finder.requests.Session.get")
    def test_email_without_owner_still_works(self, mock_get):
        mock_response = Mock()
        mock_response.text = '<a href="mailto:hello@biz.com">Email</a>'
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = find_contact_email("https://example.com")
        assert result is not None
        assert result.email == "hello@biz.com"
        assert result.owner_name is None
