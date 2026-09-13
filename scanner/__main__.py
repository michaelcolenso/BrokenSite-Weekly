"""Run the BSW v1 scanner over a metro CSV.

Reads ``data/metros/<metro>.csv``, scans each business website with the P0
check suite, screenshots Tier A/B leads, and emits ``results.json`` plus
``verification_sample.csv`` for the manual accuracy gate (HANDOFF Phase 1).

Usage:
    python -m scanner --metro seattle
    python -m scanner --metro seattle --limit 25 --no-screenshots
    python -m scanner --metro seattle --output-dir output/seattle/2026-W37

Per-site request budget (HANDOFF hard rule 4): homepage GET, one HTTP probe
only when HTTPS fails, and up to two internal pages — three pages per site.
Auxiliary check requests (form actions, images) go through a
DomainThrottledSession so they obey the same per-domain pacing.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

from scanner.checks import CheckResult
from scanner.checks.p0 import (
    broken_images,
    broken_pages,
    dead_cms,
    dead_form,
    no_https,
    not_mobile,
    parse_homepage,
    ssl_expired,
    stale_copyright,
)
from scanner.crawl import DomainThrottledSession, FetchResult, PoliteCrawler
from scanner.emit import LeadRecord, ResultsPayload, write_results, write_verification_sample
from scanner.score import lead_severity, lead_tier

logger = logging.getLogger("scanner")

EXTRA_PAGES_PER_SITE = 2  # homepage + 2 internal = 3 pages/site budget
SKIP_LINK_PREFIXES = ("#", "mailto:", "tel:", "javascript:")
SKIP_LINK_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf",
    ".zip", ".css", ".js", ".ico",
)


@dataclass(frozen=True)
class BusinessRow:
    business_name: str
    vertical: str
    phone: str
    address: str
    domain: str


def normalize_domain(raw: str) -> str:
    """Reduce a freeform domain/URL cell to a bare hostname."""
    value = (raw or "").strip().lower()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    host = urlparse(value).netloc or urlparse(value).path.split("/")[0]
    return host.split(":")[0]


def load_metro_csv(path: str | Path) -> list[BusinessRow]:
    """Load a metro CSV (business_name,vertical,phone,address,domain)."""
    rows: list[BusinessRow] = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            domain = normalize_domain(record.get("domain", ""))
            if not domain:
                logger.warning("skipping row with empty domain: %s", record.get("business_name", "?"))
                continue
            rows.append(BusinessRow(
                business_name=(record.get("business_name") or "").strip(),
                vertical=(record.get("vertical") or "").strip(),
                phone=(record.get("phone") or "").strip(),
                address=(record.get("address") or "").strip(),
                domain=domain,
            ))
    return rows


def pick_internal_links(base_url: str, html: str, *, limit: int = EXTRA_PAGES_PER_SITE) -> list[str]:
    """Choose up to `limit` same-domain page links from homepage HTML."""
    base_host = urlparse(base_url).netloc.lower()
    links: list[str] = []
    seen: set[str] = set()
    for href in parse_homepage(html).links:
        href = href.strip()
        if not href or href.lower().startswith(SKIP_LINK_PREFIXES):
            continue
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        if parsed.netloc.lower() != base_host:
            continue
        if parsed.path.lower().endswith(SKIP_LINK_EXTENSIONS):
            continue
        clean = parsed._replace(fragment="", query="").geturl()
        if clean == base_url or clean in seen:
            continue
        seen.add(clean)
        links.append(clean)
        if len(links) >= limit:
            break
    return links


def _skipped_content_check(check_id: str, severity: int) -> CheckResult:
    return CheckResult(check_id, False, "homepage unavailable; check not run", severity)


def scan_business(
    crawler: PoliteCrawler,
    session: DomainThrottledSession,
    row: BusinessRow,
    *,
    screenshots_dir: Path | None = None,
) -> LeadRecord | None:
    """Scan one business website. Returns a LeadRecord if any P0 check triggers."""
    domain = row.domain
    https_url = f"https://{domain}/"

    home = crawler.fetch(https_url)
    https_status = home.status_code
    http_status = None

    if https_status is None or https_status >= 400:
        # HTTPS is failing: probe plain HTTP (doubles as the no_https signal
        # and as a fallback homepage source).
        probe = crawler.fetch(f"http://{domain}/")
        http_status = probe.status_code
        if http_status is not None and http_status < 400:
            home = probe

    base_url = home.final_url or https_url
    html = home.text or ""

    # Homepage status reflects the page we actually used (HTTP fallback counts as served).
    page_statuses: list[int | None] = [home.status_code]
    if html:
        for link in pick_internal_links(base_url, html):
            result = crawler.fetch(link)
            page_statuses.append(result.status_code)

    checks: list[CheckResult] = [
        ssl_expired(domain),
        no_https(domain, http_status, https_status),
        broken_pages(page_statuses),
    ]

    if html:
        checks.extend([
            dead_form(base_url, html, session=session),
            not_mobile(html),
            stale_copyright(html),
            broken_images(base_url, html, session=session),
            dead_cms(html),
        ])
    else:
        checks.extend([
            _skipped_content_check("dead_form", 5),
            _skipped_content_check("not_mobile", 3),
            _skipped_content_check("stale_copyright", 2),
            _skipped_content_check("broken_images", 3),
            _skipped_content_check("dead_cms", 4),
        ])

    if not any(check.triggered for check in checks):
        return None

    severity = lead_severity(checks)
    tier = lead_tier(severity)

    screenshot_key = None
    if screenshots_dir is not None and tier in ("A", "B") and html:
        screenshot_key = _try_screenshot(base_url, domain, screenshots_dir)

    return LeadRecord(
        domain=domain,
        business_name=row.business_name,
        vertical=row.vertical,
        phone=row.phone,
        address=row.address,
        checks=checks,
        tier=tier,
        screenshot_key=screenshot_key,
    )


def _try_screenshot(url: str, domain: str, screenshots_dir: Path) -> str | None:
    """Capture a homepage screenshot; failures are logged and non-fatal."""
    safe_name = domain.replace("/", "_")
    path = screenshots_dir / f"{safe_name}.jpg"
    try:
        from scanner.screenshot import capture_homepage

        capture_homepage(url, path)
        return str(path)
    except Exception as exc:  # noqa: BLE001 - screenshot is best-effort
        logger.warning("screenshot failed for %s: %s", domain, exc)
        return None


def run_scan(
    rows: list[BusinessRow],
    *,
    screenshots_dir: Path | None = None,
    progress_every: int = 25,
) -> tuple[int, list[LeadRecord]]:
    """Scan all rows; returns (scanned_count, leads)."""
    crawler = PoliteCrawler()
    session = DomainThrottledSession(crawler)

    leads: list[LeadRecord] = []
    for index, row in enumerate(rows, start=1):
        try:
            lead = scan_business(crawler, session, row, screenshots_dir=screenshots_dir)
        except Exception as exc:  # noqa: BLE001 - one bad site must not kill the run
            logger.error("scan failed for %s: %s", row.domain, exc)
            continue
        if lead is not None:
            leads.append(lead)
        if index % progress_every == 0:
            logger.info("progress: %d/%d scanned, %d leads", index, len(rows), len(leads))
    return len(rows), leads


def iso_week_label(today: date | None = None) -> str:
    today = today or date.today()
    year, week, _ = today.isocalendar()
    return f"{year}-W{week:02d}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the BSW v1 scanner over a metro CSV.")
    parser.add_argument("--metro", default="seattle", help="metro name; reads data/metros/<metro>.csv")
    parser.add_argument("--data-dir", default="data/metros", help="directory containing metro CSVs")
    parser.add_argument("--output-dir", default=None, help="output directory (default: output/<metro>/<iso-week>)")
    parser.add_argument("--limit", type=int, default=None, help="scan at most N businesses (smoke tests)")
    parser.add_argument("--sample-size", type=int, default=50, help="verification sample size")
    parser.add_argument("--seed", type=int, default=42, help="verification sample shuffle seed")
    parser.add_argument("--no-screenshots", action="store_true", help="skip Tier A/B screenshots")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    csv_path = Path(args.data_dir) / f"{args.metro}.csv"
    if not csv_path.exists():
        logger.error("metro CSV not found: %s", csv_path)
        return 1

    rows = load_metro_csv(csv_path)
    if args.limit is not None:
        rows = rows[: args.limit]
    if not rows:
        logger.error("no businesses to scan in %s", csv_path)
        return 1

    week = iso_week_label()
    output_dir = Path(args.output_dir) if args.output_dir else Path("output") / args.metro / week
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("scanning %d businesses from %s", len(rows), csv_path)
    screenshots_dir = None if args.no_screenshots else output_dir / "screenshots"
    scanned, leads = run_scan(rows, screenshots_dir=screenshots_dir)

    payload = ResultsPayload(metro=args.metro, week=week, scanned=scanned, leads=leads)
    results_path = output_dir / "results.json"
    sample_path = output_dir / "verification_sample.csv"
    write_results(payload, results_path)
    write_verification_sample(leads, sample_path, sample_size=args.sample_size, seed=args.seed)

    tiers = {"A": 0, "B": 0, "C": 0}
    for lead in leads:
        tiers[lead.tier] = tiers.get(lead.tier, 0) + 1
    logger.info(
        "done: %d scanned, %d leads (A=%d B=%d C=%d) -> %s",
        scanned, len(leads), tiers["A"], tiers["B"], tiers["C"], results_path,
    )
    print(f"scanned={scanned} leads={len(leads)} tierA={tiers['A']} tierB={tiers['B']} tierC={tiers['C']}")
    print(f"results: {results_path}")
    print(f"verification sample: {sample_path}")
    print("next: manually verify the sample; gate is >=90% true positives before Phase 2.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
