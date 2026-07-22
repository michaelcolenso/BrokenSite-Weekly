"""V1 scanner orchestration for metro CSV inputs."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

from scanner.business_list import load_businesses
from scanner.checks.p0 import (
    broken_images,
    broken_pages,
    dead_cms,
    dead_form,
    no_https,
    not_mobile,
    ssl_expired,
    stale_copyright,
    parse_homepage,
)
from scanner.crawl import MAX_PAGES_PER_SITE, PoliteCrawler
from scanner.emit import LeadRecord, ResultsPayload, write_results, write_verification_sample
from scanner.models import Business, PageFetch
from scanner.score import lead_severity, lead_tier


def current_iso_week(today: date | None = None) -> str:
    today = today or date.today()
    year, week, _ = today.isocalendar()
    return f"{year}-W{week:02d}"


def homepage_urls(domain: str) -> tuple[str, str]:
    return f"https://{domain}/", f"http://{domain}/"


def choose_internal_urls(home_url: str, html: str) -> list[str]:
    """Choose contact page if discoverable and then one internal link, capped by site budget."""
    parsed_home = urlparse(home_url)
    parser = parse_homepage(html)
    contact: str | None = None
    internal: str | None = None
    for href in parser.links:
        candidate = urljoin(home_url, href)
        parsed = urlparse(candidate)
        if parsed.netloc != parsed_home.netloc:
            continue
        if not contact and "contact" in parsed.path.lower():
            contact = candidate
        elif not internal and parsed.path not in ("", "/"):
            internal = candidate
        if contact and internal:
            break
    return [url for url in (contact, internal) if url][: MAX_PAGES_PER_SITE - 1]


def scan_business(business: Business, crawler: PoliteCrawler) -> LeadRecord | None:
    https_url, http_url = homepage_urls(business.domain)
    https = crawler.fetch(https_url)
    http = crawler.fetch(http_url) if https.status_code is None or https.status_code >= 400 else None
    home = https if https.status_code and https.status_code < 400 else http
    if home is None:
        home = https

    statuses: list[int | None] = [home.status_code]
    for url in choose_internal_urls(home.final_url or home.url, home.text)[: MAX_PAGES_PER_SITE - 1]:
        fetched = crawler.fetch(url)
        statuses.append(fetched.status_code)

    checks = [
        ssl_expired(business.domain),
        no_https(business.domain, http.status_code if http else None, https.status_code),
        dead_form(home.final_url or home.url, home.text, session=crawler.session),
        broken_pages(statuses),
        not_mobile(home.text),
        stale_copyright(home.text),
        broken_images(home.final_url or home.url, home.text, session=crawler.session),
        dead_cms(home.text),
    ]
    if not any(check.triggered for check in checks):
        return None
    severity = lead_severity(checks)
    tier = lead_tier(severity)
    screenshot_key = f"{{week}}/{business.domain}.jpg" if tier in {"A", "B"} else None
    return LeadRecord(
        domain=business.domain,
        business_name=business.business_name,
        vertical=business.vertical,
        phone=business.phone,
        address=business.address,
        checks=checks,
        tier=tier,
        screenshot_key=screenshot_key,
    )


def scan_metro(
    metro: str,
    csv_path: str | Path,
    *,
    blocklist_path: str | Path = "data/national_chain_blocklist.csv",
    output_dir: str | Path = "output/scanner",
    week: str | None = None,
) -> ResultsPayload:
    week = week or current_iso_week()
    businesses = load_businesses(csv_path, blocklist_path=blocklist_path)
    crawler = PoliteCrawler()
    leads: list[LeadRecord] = []
    for business in businesses:
        lead = scan_business(business, crawler)
        if lead:
            if lead.screenshot_key:
                lead.screenshot_key = lead.screenshot_key.format(week=week)
            leads.append(lead)
    payload = ResultsPayload(metro=metro, week=week, scanned=len(businesses), leads=leads)
    output = Path(output_dir)
    write_results(payload, output / "results.json")
    write_verification_sample(leads, output / "verification_sample.csv")
    return payload
