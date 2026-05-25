"""
Tests for Yelp scraper and cross-reference scoring.
"""

import pytest
from unittest.mock import Mock, patch

from src.yelp_scraper import (
    _extract_rating,
    _extract_review_count,
    _normalize_name,
    _names_match,
    apply_yelp_scoring,
    YelpBusiness,
)
from src.config import ScoringConfig


# ── Rating extraction ────────────────────────────────────────────────────────

class TestExtractRating:
    def test_standard_rating(self):
        assert _extract_rating("4.5 star rating") == 4.5

    def test_whole_number(self):
        assert _extract_rating("4") == 4.0

    def test_aria_label(self):
        assert _extract_rating("4.5 star rating. This business has 4.5 stars") == 4.5

    def test_no_rating(self):
        assert _extract_rating("No ratings yet") is None

    def test_out_of_range(self):
        assert _extract_rating("12.0 stars") is None

    def test_empty(self):
        assert _extract_rating("") is None
        assert _extract_rating(None) is None


# ── Review count extraction ──────────────────────────────────────────────────

class TestExtractReviewCount:
    def test_standard(self):
        assert _extract_review_count("123 reviews") == 123

    def test_with_commas(self):
        assert _extract_review_count("1,234 reviews") == 1234

    def test_k_format(self):
        assert _extract_review_count("1.2k reviews") == 1200

    def test_k_no_decimal(self):
        assert _extract_review_count("5k reviews") == 5000

    def test_case_insensitive(self):
        assert _extract_review_count("500 Reviews") == 500

    def test_just_number(self):
        assert _extract_review_count("42") == 42

    def test_very_large_number(self):
        assert _extract_review_count("99999 reviews") == 99999

    def test_no_reviews(self):
        assert _extract_review_count("No reviews yet") is None

    def test_empty(self):
        assert _extract_review_count("") is None
        assert _extract_review_count(None) is None


# ── Name normalization ───────────────────────────────────────────────────────

class TestNormalizeName:
    def test_lowercase(self):
        assert _normalize_name("Joe's Plumbing") == "joes plumbing"

    def test_punctuation(self):
        assert _normalize_name("Joe's Plumbing & Heating, LLC") == "joes plumbing heating llc"

    def test_extra_spaces(self):
        assert _normalize_name("  Joe's   Plumbing  ") == "joes plumbing"


# ── Name matching ────────────────────────────────────────────────────────────

class TestNamesMatch:
    def test_exact_match(self):
        assert _names_match("Joe's Plumbing", "Joes Plumbing")

    def test_case_insensitive(self):
        assert _names_match("JOE'S PLUMBING", "joes plumbing")

    def test_substring_match(self):
        assert _names_match("Joe's Plumbing", "Joe's Plumbing & Heating")

    def test_word_overlap(self):
        # 2/3 overlap = 0.67 — below 0.7 threshold with LLC suffix
        # Using names that have stronger overlap
        assert _names_match("Joe's Plumbing Services", "Joe's Plumbing")

    def test_no_match(self):
        assert not _names_match("Joe's Plumbing", "Bob's Electric")

    def test_threshold_edge(self):
        # 2 out of 4 words = 0.5, below 0.7 threshold
        assert not _names_match("Joe's Plumbing Services Inc", "Joe's HVAC Repair")

    def test_partial_overlap(self):
        # "acme plumbing austin" vs "acme plumbing dallas" = 2/3 = 0.66
        assert not _names_match("Acme Plumbing Austin", "Acme Plumbing Dallas")

    def test_single_word_both(self):
        assert _names_match("Plumbing", "Plumbing")


# ── Yelp scoring signals ────────────────────────────────────────────────────

class TestApplyYelpScoring:
    def test_low_rating_adds_score(self):
        config = ScoringConfig(
            weight_yelp_low_rating=20,
            yelp_low_rating_threshold=3.0,
        )
        yelp_biz = YelpBusiness(
            name="Test Biz",
            yelp_url="https://yelp.com/biz/test",
            rating=2.5,
            review_count=25,
        )
        lead = {"review_count": 25}  # Same count — no mismatch signal
        delta, reasons = apply_yelp_scoring(lead, yelp_biz, config)
        assert delta == 20
        assert "yelp_low_rating_2.5" in reasons

    def test_good_rating_no_score(self):
        config = ScoringConfig(
            weight_yelp_low_rating=20,
            yelp_low_rating_threshold=3.0,
        )
        yelp_biz = YelpBusiness(
            name="Test Biz",
            yelp_url="https://yelp.com/biz/test",
            rating=4.5,
            review_count=25,
        )
        lead = {"review_count": 25}  # Same count — no mismatch signal
        delta, reasons = apply_yelp_scoring(lead, yelp_biz, config)
        assert delta == 0

    def test_rating_none_no_score(self):
        config = ScoringConfig()
        yelp_biz = YelpBusiness(
            name="Test Biz",
            yelp_url="https://yelp.com/biz/test",
            rating=None,
        )
        delta, reasons = apply_yelp_scoring({}, yelp_biz, config)
        assert delta == 0

    def test_review_mismatch_high_ratio(self):
        config = ScoringConfig(weight_yelp_review_mismatch=15)
        yelp_biz = YelpBusiness(
            name="Test Biz",
            yelp_url="https://yelp.com/biz/test",
            rating=4.0,
            review_count=200,
        )
        lead = {"review_count": 10}  # 200 vs 10 = 20x ratio
        delta, reasons = apply_yelp_scoring(lead, yelp_biz, config)
        assert delta == 15
        assert any("yelp_review_mismatch" in r for r in reasons)

    def test_review_close_no_mismatch(self):
        config = ScoringConfig(weight_yelp_review_mismatch=15)
        yelp_biz = YelpBusiness(
            name="Test Biz",
            yelp_url="https://yelp.com/biz/test",
            rating=4.0,
            review_count=50,
        )
        lead = {"review_count": 40}  # 50/40 = 1.25x, below 3x threshold
        delta, reasons = apply_yelp_scoring(lead, yelp_biz, config)
        assert delta == 0

    def test_missing_google_reviews_no_error(self):
        config = ScoringConfig(weight_yelp_review_mismatch=15)
        yelp_biz = YelpBusiness(
            name="Test Biz",
            yelp_url="https://yelp.com/biz/test",
            review_count=50,
        )
        lead = {}  # No review_count
        delta, reasons = apply_yelp_scoring(lead, yelp_biz, config)
        assert delta == 0

    def test_zero_reviews_handled(self):
        config = ScoringConfig(weight_yelp_review_mismatch=15)
        yelp_biz = YelpBusiness(
            name="Test Biz",
            yelp_url="https://yelp.com/biz/test",
            review_count=0,
        )
        lead = {"review_count": 0}
        delta, reasons = apply_yelp_scoring(lead, yelp_biz, config)
        assert delta == 0

    def test_cross_reference_isolation(self):
        """cross_reference_with_isolation returns gracefully on import error."""
        from src.yelp_scraper import cross_reference_with_isolation
        from src.config import ScraperConfig

        # Without a real browser, this will error — the isolation wrapper catches it
        results, error = cross_reference_with_isolation(
            [{"name": "Test", "city": "Austin, TX"}],
            ScraperConfig(headless=True),
            max_leads=1,
        )
        # Either results are empty or an error is returned — both are valid
        assert isinstance(results, list)
