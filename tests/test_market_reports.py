"""
Tests for market saturation reports.
"""

import pytest
from unittest.mock import Mock
from pathlib import Path

from src.market_reports import generate_market_report, write_market_report


class TestGenerateMarketReport:
    def test_empty_businesses_returns_none(self):
        assert generate_market_report("Austin, TX", "plumber", [], []) is None

    def test_generates_report_with_stats(self):
        businesses = []
        for name, rc in [("Biz A", 100), ("Biz B", 50), ("Biz C", 200)]:
            m = Mock()
            m.name = name
            m.review_count = rc
            businesses.append(m)
        leads = [
            Mock(score=80, reasons=["no_https", "outdated_copyright"]),
            Mock(score=45, reasons=["no_https"]),
            Mock(score=10, reasons=[]),
        ]

        report = generate_market_report("Austin, TX", "plumber", businesses, leads, min_score=40)

        assert "Austin, TX - Plumber" in report
        assert "Total businesses scraped:     3" in report
        assert "With broken/outdated sites:    2 (66.7%)" in report
        assert "Common issues:" in report
        assert "no_https" in report
        assert "Top competitors (by reviews):" in report
        assert "Biz C (200 reviews)" in report

    def test_no_qualifying_leads_shows_zero_broken(self):
        businesses = [Mock(name="Biz A", review_count=10)]
        leads = [Mock(score=5, reasons=[])]

        report = generate_market_report("Austin, TX", "plumber", businesses, leads, min_score=40)

        assert "With broken/outdated sites:    0 (0.0%)" in report
        assert "Common issues:" not in report

    def test_string_reasons_handled(self):
        businesses = [Mock(name="Biz A", review_count=10)]
        leads = [Mock(score=80, reasons="no_https,outdated_copyright")]

        report = generate_market_report("Austin, TX", "plumber", businesses, leads, min_score=40)

        assert "no_https" in report
        assert "outdated_copyright" in report


class TestWriteMarketReport:
    def test_writes_file(self, tmp_path: Path):
        report_text = "Test report"
        path = write_market_report(
            report_text=report_text,
            output_dir=tmp_path,
            city="Austin, TX",
            category="plumber",
            date_str="2026-05-20",
        )

        assert path.exists()
        assert path.read_text() == report_text
        assert "market_report_Austin_TX_plumber_2026-05-20.txt" == path.name
