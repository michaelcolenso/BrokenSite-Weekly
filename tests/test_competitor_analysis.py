"""
Tests for competitor analysis module.
"""

import json
import pytest
from unittest.mock import Mock, patch

from src.competitor_analysis import (
    find_competitors,
    score_competitor_websites,
    build_competitor_summary,
    analyze_competitors_for_lead,
    _clear_scrape_cache,
)
from src.config import Config, ScraperConfig, ScoringConfig, RetryConfig


@pytest.fixture(autouse=True)
def clear_cache():
    _clear_scrape_cache()


def _mock_config():
    return Config(
        scraper=ScraperConfig(competitor_analysis_enabled=True),
        scoring=ScoringConfig(),
        retry=RetryConfig(),
    )


class TestFindCompetitors:
    @patch("src.competitor_analysis.scrape_with_isolation")
    def test_finds_competitors_excluding_self(self, mock_scrape):
        mock_scrape.return_value = (
            [
                Mock(place_id="self", name="Self Biz", website="https://self.com", review_count=100),
                Mock(place_id="c1", name="Competitor A", website="https://a.com", review_count=200),
                Mock(place_id="c2", name="Competitor B", website="https://b.com", review_count=150),
                Mock(place_id="c3", name="Competitor C", website="https://c.com", review_count=50),
            ],
            None,
        )

        result = find_competitors("Austin, TX", "plumber", "self", _mock_config())

        assert len(result) == 3
        assert result[0]["place_id"] == "c1"
        assert result[1]["place_id"] == "c2"
        assert result[2]["place_id"] == "c3"
        mock_scrape.assert_called_once()

    @patch("src.competitor_analysis.scrape_with_isolation")
    def test_caches_scrape_results(self, mock_scrape):
        mock_scrape.return_value = (
            [
                Mock(place_id="c1", name="A", website="https://a.com", review_count=10),
            ],
            None,
        )

        config = _mock_config()
        find_competitors("Austin, TX", "plumber", "self", config)
        find_competitors("Austin, TX", "plumber", "other", config)

        assert mock_scrape.call_count == 1

    @patch("src.competitor_analysis.scrape_with_isolation")
    def test_skips_competitors_without_website(self, mock_scrape):
        mock_scrape.return_value = (
            [
                Mock(place_id="c1", name="A", website=None, review_count=100),
                Mock(place_id="c2", name="B", website="https://b.com", review_count=50),
            ],
            None,
        )

        result = find_competitors("Austin, TX", "plumber", "self", _mock_config())

        assert len(result) == 1
        assert result[0]["place_id"] == "c2"

    @patch("src.competitor_analysis.scrape_with_isolation")
    def test_returns_empty_on_scrape_error(self, mock_scrape):
        mock_scrape.return_value = ([], "timeout")

        result = find_competitors("Austin, TX", "plumber", "self", _mock_config())

        assert result == []


class TestScoreCompetitorWebsites:
    @patch("src.competitor_analysis.evaluate_with_isolation")
    def test_scores_each_competitor(self, mock_eval):
        mock_eval.side_effect = [
            Mock(score=45, reasons=["no_https"]),
            Mock(score=10, reasons=[]),
        ]

        competitors = [
            {"name": "A", "website": "https://a.com"},
            {"name": "B", "website": "https://b.com"},
        ]

        result = score_competitor_websites(competitors, _mock_config())

        assert len(result) == 2
        assert result[0]["score"] == 45
        assert result[0]["reasons"] == ["no_https"]
        assert result[1]["score"] == 10


class TestBuildCompetitorSummary:
    def test_significant_gap(self):
        competitors = [
            {"name": "A", "score": 10, "review_count": 100},
            {"name": "B", "score": 20, "review_count": 80},
        ]
        summary = build_competitor_summary(80, competitors)
        assert summary["gap_text"] == "Your site is significantly behind competitors"
        assert summary["avg_competitor_score"] == 15.0

    def test_leads_competitors(self):
        competitors = [
            {"name": "A", "score": 90, "review_count": 100},
            {"name": "B", "score": 85, "review_count": 80},
        ]
        summary = build_competitor_summary(10, competitors)
        assert summary["gap_text"] == "Your site leads competitors in quality"

    def test_empty_competitors(self):
        summary = build_competitor_summary(50, [])
        assert summary["gap_text"] == "No competitors found"


class TestAnalyzeCompetitorsForLead:
    @patch("src.competitor_analysis.find_competitors")
    @patch("src.competitor_analysis.score_competitor_websites")
    def test_returns_json_when_enabled(self, mock_score, mock_find):
        mock_find.return_value = [
            {"place_id": "c1", "name": "A", "website": "https://a.com", "review_count": 10}
        ]
        mock_score.return_value = [
            {"place_id": "c1", "name": "A", "website": "https://a.com", "review_count": 10, "score": 20, "reasons": []}
        ]

        lead = Mock(
            place_id="self",
            city="Austin, TX",
            category="plumber",
            score=80,
        )

        result = analyze_competitors_for_lead(lead, _mock_config())
        assert result is not None
        parsed = json.loads(result)
        assert parsed["gap_text"] == "Your site is significantly behind competitors"
        assert len(parsed["competitors"]) == 1

    def test_returns_none_when_disabled(self):
        config = _mock_config()
        config.scraper.competitor_analysis_enabled = False

        lead = Mock(place_id="self", city="Austin, TX", category="plumber", score=80)
        result = analyze_competitors_for_lead(lead, config)
        assert result is None
