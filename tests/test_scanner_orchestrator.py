"""Tests for the scanner orchestrator (scanner/__main__.py).

Network access is avoided by driving scan_business with a FakeCrawler and
using homepage HTML with no forms or images, so dead_form/broken_images never
issue auxiliary requests. ssl_expired is monkeypatched because it opens real
TLS sockets.
"""

import csv
import json

import pytest

import scanner.__main__ as orchestrator
from scanner.__main__ import (
    BusinessRow,
    iso_week_label,
    load_metro_csv,
    main,
    normalize_domain,
    pick_internal_links,
    scan_business,
)
from scanner.checks import CheckResult
from scanner.crawl import DomainThrottledSession, FetchResult


HEALTHY_HTML = (
    '<html><head><meta name="viewport" content="width=device-width">'
    "<title>Acme Plumbing</title></head>"
    '<body><a href="/about">About</a><a href="/contact">Contact</a>'
    "<p>&copy; 2026 Acme Plumbing</p></body></html>"
)


class FakeCrawler:
    """Serves canned FetchResults keyed by URL."""

    def __init__(self, responses):
        self.responses = responses
        self.fetched = []

    def fetch(self, url, *, allow_binary=False, method="GET"):
        self.fetched.append(url)
        return self.responses.get(url, FetchResult(url=url, status_code=None, error="connection refused"))

    def wait_for_domain(self, domain):
        return None

    def record_request(self, domain):
        return None


@pytest.fixture
def no_tls(monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "ssl_expired",
        lambda domain, **_: CheckResult("ssl_expired", False, "certificate valid", 5),
    )


def make_row(domain="example.com"):
    return BusinessRow(
        business_name="Acme Plumbing",
        vertical="plumber",
        phone="206-555-0100",
        address="1 Pike St, Seattle, WA",
        domain=domain,
    )


def make_session(crawler):
    return DomainThrottledSession(crawler)


# --- normalize_domain ---


def test_normalize_domain_strips_scheme_and_path():
    assert normalize_domain("https://Example.com/about") == "example.com"


def test_normalize_domain_accepts_bare_host_and_port():
    assert normalize_domain("example.com") == "example.com"
    assert normalize_domain("example.com:8443") == "example.com"


def test_normalize_domain_empty():
    assert normalize_domain("   ") == ""


# --- load_metro_csv ---


def test_load_metro_csv_parses_rows_and_skips_empty_domains(tmp_path):
    path = tmp_path / "seattle.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["business_name", "vertical", "phone", "address", "domain"])
        writer.writerow(["Acme Plumbing", "plumber", "206-555-0100", "1 Pike St", "acme.com"])
        writer.writerow(["No Site LLC", "roofer", "", "", ""])
    rows = load_metro_csv(path)
    assert len(rows) == 1
    assert rows[0].domain == "acme.com"
    assert rows[0].vertical == "plumber"


# --- pick_internal_links ---


def test_pick_internal_links_same_domain_only():
    html = (
        '<a href="/about">a</a><a href="https://other.com/x">b</a>'
        '<a href="mailto:x@example.com">c</a><a href="/logo.png">d</a>'
        '<a href="/contact">e</a><a href="/about#team">f</a>'
    )
    links = pick_internal_links("https://example.com/", html)
    assert links == ["https://example.com/about", "https://example.com/contact"]


# --- scan_business ---


def test_scan_business_healthy_site_returns_no_lead(no_tls):
    crawler = FakeCrawler({
        "https://example.com/": FetchResult(
            url="https://example.com/", status_code=200, text=HEALTHY_HTML,
            content_type="text/html", final_url="https://example.com/",
        ),
        "https://example.com/about": FetchResult(url="https://example.com/about", status_code=200),
        "https://example.com/contact": FetchResult(url="https://example.com/contact", status_code=200),
    })
    lead = scan_business(crawler, make_session(crawler), make_row())
    assert lead is None


def test_scan_business_broken_homepage_is_tier_a_lead(no_tls):
    crawler = FakeCrawler({
        "https://example.com/": FetchResult(url="https://example.com/", status_code=404),
    })
    lead = scan_business(crawler, make_session(crawler), make_row())
    assert lead is not None
    assert lead.tier == "A"
    triggered = {c.check_id for c in lead.checks if c.triggered}
    assert "broken_pages" in triggered
    # Content checks must not fire on an unreachable homepage.
    assert "not_mobile" not in triggered
    dead_form_result = next(c for c in lead.checks if c.check_id == "dead_form")
    assert dead_form_result.evidence.endswith("check not run")


def test_scan_business_http_fallback_triggers_no_https_not_broken_pages(no_tls):
    crawler = FakeCrawler({
        "http://example.com/": FetchResult(
            url="http://example.com/", status_code=200, text=HEALTHY_HTML,
            content_type="text/html", final_url="http://example.com/",
        ),
        "http://example.com/about": FetchResult(url="http://example.com/about", status_code=200),
        "http://example.com/contact": FetchResult(url="http://example.com/contact", status_code=200),
    })
    lead = scan_business(crawler, make_session(crawler), make_row())
    assert lead is not None
    triggered = {c.check_id: c for c in lead.checks if c.triggered}
    assert "no_https" in triggered
    assert "broken_pages" not in triggered
    # The HTTP probe must have been attempted exactly once.
    assert crawler.fetched.count("http://example.com/") == 1


def test_scan_business_screenshot_failure_is_non_fatal(no_tls, tmp_path, monkeypatch):
    crawler = FakeCrawler({
        "https://example.com/": FetchResult(url="https://example.com/", status_code=404),
    })
    monkeypatch.setattr(
        "scanner.screenshot.capture_homepage",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no browser")),
    )
    lead = scan_business(
        crawler, make_session(crawler), make_row(), screenshots_dir=tmp_path / "shots",
    )
    assert lead is not None
    assert lead.screenshot_key is None


# --- end-to-end via main() ---


def test_main_writes_results_and_sample(no_tls, tmp_path, monkeypatch):
    data_dir = tmp_path / "metros"
    data_dir.mkdir()
    with (data_dir / "seattle.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["business_name", "vertical", "phone", "address", "domain"])
        writer.writerow(["Broken Co", "hvac", "", "", "broken.example"])
        writer.writerow(["Healthy Co", "dentist", "", "", "healthy.example"])

    healthy = FetchResult(
        url="https://healthy.example/", status_code=200, text=HEALTHY_HTML,
        content_type="text/html", final_url="https://healthy.example/",
    )
    fake = FakeCrawler({
        "https://healthy.example/": healthy,
        "https://healthy.example/about": FetchResult(url="https://healthy.example/about", status_code=200),
        "https://healthy.example/contact": FetchResult(url="https://healthy.example/contact", status_code=200),
        "https://broken.example/": FetchResult(url="https://broken.example/", status_code=500),
    })
    monkeypatch.setattr(orchestrator, "PoliteCrawler", lambda: fake)

    output_dir = tmp_path / "out"
    exit_code = main([
        "--metro", "seattle",
        "--data-dir", str(data_dir),
        "--output-dir", str(output_dir),
        "--no-screenshots",
    ])
    assert exit_code == 0

    results = json.loads((output_dir / "results.json").read_text())
    assert results["scanned"] == 2
    assert len(results["leads"]) == 1
    assert results["leads"][0]["domain"] == "broken.example"
    assert results["leads"][0]["tier"] == "A"

    sample_lines = (output_dir / "verification_sample.csv").read_text().strip().splitlines()
    assert sample_lines[0] == "domain,business_name,vertical,tier,triggered_checks"
    assert "broken.example" in sample_lines[1]


def test_main_missing_csv_returns_1(tmp_path):
    assert main(["--metro", "nowhere", "--data-dir", str(tmp_path)]) == 1


def test_iso_week_label_format():
    from datetime import date

    assert iso_week_label(date(2026, 9, 13)) == "2026-W37"
