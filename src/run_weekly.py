#!/usr/bin/env python3
"""
Weekly orchestrator for BrokenSite-Weekly.
Coordinates scraping, scoring, deduplication, and delivery.

Designed for unattended VPS operation via systemd timer.
"""

from __future__ import annotations

import sys
import signal
import argparse
import json
import concurrent.futures
from datetime import datetime
from typing import Optional
from dataclasses import asdict

from .config import load_config, validate_config, Config, OUTPUT_DIR
from .logging_setup import setup_logging, RunContext, get_logger
from .db import Database, Lead
from .gumroad import get_subscribers_with_isolation
from .delivery import deliver_with_isolation, generate_csv, generate_manual_review_csv
from .audit_generator import generate_audit_page, get_issues_json
from .contact_finder import find_contact_with_isolation
from .outreach import run_outreach, run_followups
from .warm_delivery import deliver_warm_leads_with_isolation
from .lead_utils import compute_lead_tier, compute_exclusive_until
from .competitor_analysis import analyze_competitors_for_lead
from .market_reports import generate_market_report, write_market_report

logger = get_logger("orchestrator")


class GracefulShutdown:
    """Handle graceful shutdown on SIGTERM/SIGINT."""

    def __init__(self):
        self.shutdown_requested = False
        signal.signal(signal.SIGTERM, self._handler)
        signal.signal(signal.SIGINT, self._handler)

    def _handler(self, signum, frame):
        logger.warning(f"Shutdown requested (signal {signum})")
        self.shutdown_requested = True

    def check(self) -> bool:
        return self.shutdown_requested


def _build_validation_requirements(
    config: Config,
    *,
    skip_delivery: bool,
    skip_outreach: bool,
    dry_run: bool,
) -> dict[str, bool]:
    """Determine required configuration blocks for the current run mode."""
    delivery_enabled = not skip_delivery
    outreach_enabled = config.outreach.enabled and not skip_outreach

    return {
        "require_gumroad": delivery_enabled,
        "require_smtp": delivery_enabled or (outreach_enabled and not dry_run),
        "require_outreach": outreach_enabled,
        "require_portal": False,
    }


def _emit_run_kpis(run_ctx: RunContext, *, dry_run: bool) -> None:
    """Log and persist a baseline KPI snapshot for this run."""
    now = datetime.utcnow()
    duration_seconds = None
    if run_ctx.start_time:
        duration_seconds = int((now - run_ctx.start_time).total_seconds())

    attempted = run_ctx.stats["queries_attempted"]
    succeeded = run_ctx.stats["queries_succeeded"]
    checked = run_ctx.stats["websites_checked"]
    qualifying = run_ctx.stats["qualifying_leads"]

    query_success_rate = round((succeeded / attempted) * 100, 2) if attempted else 0.0
    qualification_rate = round((qualifying / checked) * 100, 2) if checked else 0.0

    snapshot = {
        "run_id": run_ctx.run_id,
        "captured_at": f"{now.isoformat()}Z",
        "duration_seconds": duration_seconds,
        "kpis": {
            "queries_attempted": attempted,
            "queries_succeeded": succeeded,
            "query_success_rate_pct": query_success_rate,
            "businesses_found": run_ctx.stats["businesses_found"],
            "websites_checked": checked,
            "qualifying_leads": qualifying,
            "qualification_rate_pct": qualification_rate,
            "leads_exported": run_ctx.stats["leads_exported"],
            "emails_sent": run_ctx.stats["emails_sent"],
            "audits_generated": run_ctx.stats["audits_generated"],
            "contacts_found": run_ctx.stats["contacts_found"],
            "outreach_sent": run_ctx.stats["outreach_sent"],
            "followups_sent": run_ctx.stats["followups_sent"],
            "errors": run_ctx.stats["errors"],
        },
        "phase_durations_seconds": run_ctx.stats.get("phase_durations_seconds", {}),
        "reason_counts": run_ctx.stats.get("reason_counts", {}),
    }

    logger.info(
        "KPI baseline | run_id=%s duration_s=%s query_success=%.2f%% qualification=%.2f%% "
        "qualified=%s exported=%s emails=%s errors=%s",
        run_ctx.run_id,
        duration_seconds,
        query_success_rate,
        qualification_rate,
        qualifying,
        run_ctx.stats["leads_exported"],
        run_ctx.stats["emails_sent"],
        run_ctx.stats["errors"],
    )

    if dry_run:
        return

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        run_snapshot = OUTPUT_DIR / f"kpi_baseline_{run_ctx.run_id}.json"
        latest_snapshot = OUTPUT_DIR / "kpi_baseline_latest.json"
        payload = json.dumps(snapshot, indent=2, sort_keys=True)
        run_snapshot.write_text(payload, encoding="utf-8")
        latest_snapshot.write_text(payload, encoding="utf-8")
        logger.info(f"Saved KPI baseline snapshot: {run_snapshot}")
    except Exception as e:
        logger.warning(f"Failed to save KPI baseline snapshot: {e}")


def process_business(
    business: Business,
    db: Database,
    config: Config,
    run_ctx: RunContext,
    dry_run: bool = False,
) -> Optional[Lead]:
    """
    Process a single business: check website, score, store.
    Returns Lead if it qualifies, None otherwise.
    Fully isolated - never raises exceptions.

    Args:
        dry_run: If True, skip database writes (scoring still happens).
    """
    try:
        # Handle no-website leads (optional)
        if not business.website:
            if not config.scoring.include_no_website_leads:
                logger.debug(f"Skipping {business.name}: no website")
                return None

            lead = Lead(
                place_id=business.place_id,
                cid=business.cid,
                name=business.name,
                website=None,
                address=business.address,
                phone=business.phone,
                review_count=business.review_count,
                city=business.city,
                category=business.category,
                score=config.scoring.weight_no_website,
                reasons=["no_website"],
                first_seen=datetime.utcnow(),
                last_seen=datetime.utcnow(),
                lead_tier=compute_lead_tier(config.scoring.weight_no_website),
            )
            if not dry_run:
                is_new = db.upsert_lead(lead)
                if not is_new:
                    logger.debug(f"Skipping {business.name}: duplicate within window")
                    return None

            if lead.score >= config.scoring.min_score_to_include:
                run_ctx.increment("qualifying_leads")
                logger.info(
                    f"Lead: {business.name} | no website | "
                    f"Score: {lead.score} | Reasons: {','.join(lead.reasons)}"
                )
            return lead

        run_ctx.increment("websites_checked")

        # Score the website
        from .scoring import evaluate_with_isolation

        result = evaluate_with_isolation(
            url=business.website,
            config=config.scoring,
            retry_config=config.retry,
            expected_phone=business.phone,
        )
        run_ctx.count_reasons(result.reasons)

        reasons_list = list(result.reasons)
        lead_tier = compute_lead_tier(result.score)
        exclusive_until = None
        exclusive_tier = None
        if (
            result.score >= 70
            and business.review_count is not None
            and business.review_count >= 15
            and business.website
            and "unverified" not in result.reasons
        ):
            exclusive_until = compute_exclusive_until(days=7)
            exclusive_tier = "pro"

        # Create lead record
        lead = Lead(
            place_id=business.place_id,
            cid=business.cid,
            name=business.name,
            website=business.website,
            address=business.address,
            phone=business.phone,
            review_count=business.review_count,
            city=business.city,
            category=business.category,
            score=result.score,
            reasons=reasons_list,
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
            exclusive_until=exclusive_until,
            exclusive_tier=exclusive_tier,
            lead_tier=lead_tier,
        )

        # Store in database (skip in dry-run mode)
        if dry_run:
            is_new = True  # Assume new in dry-run
        else:
            is_new = db.upsert_lead(lead)
            if not is_new:
                logger.debug(f"Skipping {business.name}: duplicate within window")
                return None

        if result.score >= config.scoring.min_score_to_include:
            run_ctx.increment("qualifying_leads")
            logger.info(
                f"Lead: {business.name} | {business.website} | "
                f"Score: {result.score} | Reasons: {','.join(result.reasons)}"
            )
        return lead

    except Exception as e:
        logger.error(f"Error processing {business.name}: {e}")
        run_ctx.increment("errors")
        return None


def run_scraping_phase(
    config: Config,
    db: Database,
    run_ctx: RunContext,
    shutdown: GracefulShutdown,
    dry_run: bool = False,
) -> list:
    """
    Phase 1: Scrape Google Maps and score websites.
    Returns list of qualifying leads.

    Args:
        dry_run: If True, skip database writes.
    """
    qualifying_leads = []
    market_report_paths = []

    from .maps_scraper import scrape_with_isolation

    for city in config.target_cities:
        if shutdown.check():
            logger.warning("Shutdown requested, stopping scrape phase")
            break

        for category in config.search_queries:
            if shutdown.check():
                break

            run_ctx.increment("queries_attempted")

            # Scrape with isolation
            businesses, scrape_error = scrape_with_isolation(
                city=city,
                category=category,
                config=config.scraper,
            )

            if scrape_error:
                logger.error(f"Scrape failed for {category} in {city}: {scrape_error}")
                run_ctx.increment("errors")
                continue

            run_ctx.increment("queries_succeeded")
            run_ctx.increment("businesses_found", len(businesses))

            # Process each business concurrently
            batch_leads = []
            max_workers = getattr(config.scraper, 'max_workers', 5)
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(process_business, business, db, config, run_ctx, dry_run): business
                    for business in businesses
                }
                for future in concurrent.futures.as_completed(futures):
                    if shutdown.check():
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    try:
                        lead = future.result()
                        if lead:
                            batch_leads.append(lead)
                    except Exception as e:
                        logger.error(f"Error processing business concurrently: {e}")

            # Filter qualifying leads from batch
            batch_qualifying = [
                l for l in batch_leads
                if l.score >= config.scoring.min_score_to_include
            ]
            qualifying_leads.extend(batch_qualifying)

            # Generate market report for this batch
            if businesses:
                try:
                    report_text = generate_market_report(
                        city=city,
                        category=category,
                        businesses=businesses,
                        all_leads=batch_leads,
                        min_score=config.scoring.min_score_to_include,
                    )
                    if report_text:
                        report_path = write_market_report(
                            report_text=report_text,
                            output_dir=OUTPUT_DIR,
                            city=city,
                            category=category,
                        )
                        market_report_paths.append(str(report_path))
                except Exception as e:
                    logger.warning(f"Failed to generate market report for {category} in {city}: {e}")

    run_ctx.stats["market_report_paths"] = market_report_paths

    duration = run_ctx.stats.get("phase_durations_seconds", {}).get("scraping", 0.0)
    websites_checked = run_ctx.stats.get("websites_checked", 0)
    if duration > 0 and websites_checked:
        rate = websites_checked / (duration / 60)
        logger.info(
            "Scraping throughput: %.2f websites/minute (%s checked in %.2fs)",
            rate,
            websites_checked,
            duration,
        )

    reason_counts = run_ctx.stats.get("reason_counts", {})
    if reason_counts:
        top_reasons = sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)[:10]
        logger.info("Top scoring reasons this run: %s", top_reasons)

    return qualifying_leads


def run_export_csv_phase(
    *,
    config: Config,
    db: Database,
    run_ctx: RunContext,
    scraped_qualifying_leads: Optional[list[Lead]] = None,
    label: str = "local",
) -> list[str]:
    """Generate local CSV(s) without emailing or marking leads as exported.

    If `scraped_qualifying_leads` is provided, exports that in-memory set.
    Otherwise exports from the existing DB using unexported-lead queries.
    Returns list of CSV file paths (as strings).
    """
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    csv_paths: list[str] = []

    if scraped_qualifying_leads is not None:
        leads = [asdict(l) for l in scraped_qualifying_leads]
        csv_filename = f"broken_site_leads_{date_str}_{label}_{run_ctx.run_id}.csv"
        _, csv_path = generate_csv(leads, output_path=OUTPUT_DIR / csv_filename)
        csv_paths.append(str(csv_path))
        logger.info(f"Local CSV report created: {csv_path} ({len(leads)} leads)")
        return csv_paths

    # Export from existing DB (use the same tier logic as delivery, but no subscribers/SMTP).
    leads_pro = db.get_unexported_leads_for_tier(
        min_score=config.scoring.min_score_to_include,
        tier="pro",
        limit=500,
    )
    leads_basic = db.get_unexported_leads_for_tier(
        min_score=config.scoring.min_score_to_include,
        tier="basic",
        limit=500,
    )

    if not leads_pro and not leads_basic:
        logger.info("No unexported leads available to write CSV")
        return csv_paths

    if leads_pro:
        csv_filename = f"broken_site_leads_{date_str}_pro_{run_ctx.run_id}.csv"
        _, csv_path = generate_csv(leads_pro, output_path=OUTPUT_DIR / csv_filename)
        csv_paths.append(str(csv_path))
        logger.info(f"Local CSV report created: {csv_path} ({len(leads_pro)} pro leads)")

    if leads_basic:
        csv_filename = f"broken_site_leads_{date_str}_basic_{run_ctx.run_id}.csv"
        _, csv_path = generate_csv(leads_basic, output_path=OUTPUT_DIR / csv_filename)
        csv_paths.append(str(csv_path))
        logger.info(f"Local CSV report created: {csv_path} ({len(leads_basic)} basic leads)")

    return csv_paths


def run_delivery_phase(
    config: Config,
    db: Database,
    run_ctx: RunContext,
    dry_run: bool = False,
) -> bool:
    """
    Phase 2: Get subscribers and deliver CSV.
    Returns True if delivery succeeded.

    Args:
        dry_run: If True, skip actual SMTP sends and database updates.
    """
    # Fetch subscribers with tiers
    subscribers, sub_error = get_subscribers_with_isolation(
        config=config.gumroad,
        retry_config=config.retry,
    )

    if sub_error:
        logger.error(f"Failed to get subscribers: {sub_error}")
        run_ctx.increment("errors")
        return False

    if not subscribers:
        logger.warning("No active subscribers found")
        return True

    pro_subs = [s for s in subscribers if s.tier == "pro"]
    basic_subs = [s for s in subscribers if s.tier != "pro"]

    # Fetch leads by tier
    leads_pro = db.get_unexported_leads_for_tier(
        min_score=config.scoring.min_score_to_include,
        tier="pro",
        limit=500,
    )
    leads_basic = db.get_unexported_leads_for_tier(
        min_score=config.scoring.min_score_to_include,
        tier="basic",
        limit=500,
    )

    if not leads_pro and not leads_basic:
        logger.info("No new leads to deliver")
        return True

    # Prepare portal config fallback
    portal_config = config.portal
    if portal_config and not portal_config.base_url:
        portal_config.base_url = config.outreach.tracking_base_url

    # Dry-run logging
    if dry_run:
        if leads_pro:
            logger.info(f"[DRY-RUN] Would deliver {len(leads_pro)} pro leads to {len(pro_subs)} pro subscribers")
        if leads_basic:
            logger.info(f"[DRY-RUN] Would deliver {len(leads_basic)} basic leads to {len(basic_subs)} basic subscribers")
        run_ctx.stats["emails_sent"] = 0
        run_ctx.stats["leads_exported"] = 0
        return True

    total_emails_sent = 0
    total_leads_exported = 0

    # Deliver to Pro tier
    if pro_subs and leads_pro:
        results, delivery_error = deliver_with_isolation(
            subscribers=pro_subs,
            leads=leads_pro,
            config=config.smtp,
            retry_config=config.retry,
            portal_config=portal_config,
            csv_label="pro",
        )
        if delivery_error:
            logger.error(f"Pro delivery error: {delivery_error}")
            run_ctx.increment("errors")
        else:
            success_count = sum(1 for r in results if r.success)
            total_emails_sent += success_count
            if success_count > 0:
                total_leads_exported += len(leads_pro)
                place_ids = [lead["place_id"] for lead in leads_pro]
                db.mark_exported(place_ids, tier="pro")
                logger.info(f"Marked {len(place_ids)} pro leads as exported")
            for result in results:
                if result.success:
                    db.record_export(
                        run_id=run_ctx.run_id,
                        subscriber_email=result.subscriber_email,
                        lead_count=len(leads_pro),
                        csv_path=result.csv_path or "",
                        tier="pro",
                        export_type="cold",
                    )

    # Deliver to Basic tier
    if basic_subs and leads_basic:
        results, delivery_error = deliver_with_isolation(
            subscribers=basic_subs,
            leads=leads_basic,
            config=config.smtp,
            retry_config=config.retry,
            portal_config=portal_config,
            csv_label="basic",
        )
        if delivery_error:
            logger.error(f"Basic delivery error: {delivery_error}")
            run_ctx.increment("errors")
        else:
            success_count = sum(1 for r in results if r.success)
            total_emails_sent += success_count
            if success_count > 0:
                total_leads_exported += len(leads_basic)
                place_ids = [lead["place_id"] for lead in leads_basic]
                db.mark_exported(place_ids, tier="basic")
                logger.info(f"Marked {len(place_ids)} basic leads as exported")
            for result in results:
                if result.success:
                    db.record_export(
                        run_id=run_ctx.run_id,
                        subscriber_email=result.subscriber_email,
                        lead_count=len(leads_basic),
                        csv_path=result.csv_path or "",
                        tier="basic",
                        export_type="cold",
                    )

    run_ctx.stats["emails_sent"] = total_emails_sent
    run_ctx.stats["leads_exported"] = total_leads_exported

    return True


def run_manual_review_phase(
    config: Config,
    db: Database,
    run_ctx: RunContext,
) -> None:
    """
    Phase 3: Generate manual review CSV for unverified leads.
    """
    if not config.scoring.manual_review_enabled:
        return

    leads = db.get_unverified_leads(limit=config.scoring.manual_review_limit)
    if not leads:
        logger.info("No unverified leads for manual review")
        return

    output_path = generate_manual_review_csv(leads)
    if output_path:
        logger.info(f"Manual review CSV created: {output_path}")
        run_ctx.stats["manual_review_leads"] = len(leads)


def run_audit_generation_phase(
    config: Config,
    db: Database,
    run_ctx: RunContext,
    shutdown: GracefulShutdown,
) -> int:
    """Phase 4: Generate audit pages for qualifying leads without audits."""
    if not config.outreach.enabled:
        logger.info("Outreach disabled, skipping audit generation")
        return 0

    leads = db.get_leads_without_audits(
        min_score=config.outreach.min_score_for_outreach
    )
    if not leads:
        logger.info("No leads need audit pages")
        return 0

    logger.info(f"Generating audits for {len(leads)} leads")
    generated = 0

    for lead in leads:
        if shutdown.check():
            logger.warning("Shutdown requested, stopping audit generation")
            break

        audit_url, file_path = generate_audit_page(lead, config)
        if audit_url:
            issues_json = get_issues_json(lead)
            db.record_audit(lead["place_id"], audit_url, file_path, issues_json)
            generated += 1

    logger.info(f"Generated {generated} audit pages")
    run_ctx.increment("audits_generated", generated)
    return generated


def run_contact_finding_phase(
    config: Config,
    db: Database,
    run_ctx: RunContext,
    shutdown: GracefulShutdown,
) -> int:
    """Phase 5: Find contact emails for leads without contacts."""
    if not config.outreach.enabled:
        logger.info("Outreach disabled, skipping contact finding")
        return 0

    leads = db.get_leads_without_contacts()
    if not leads:
        logger.info("No leads need contact discovery")
        return 0

    logger.info(f"Finding contacts for {len(leads)} leads")
    found = 0

    for lead in leads:
        if shutdown.check():
            logger.warning("Shutdown requested, stopping contact finding")
            break

        website = lead.get("website")
        if not website:
            continue

        contact, error = find_contact_with_isolation(website)
        if contact:
            db.record_contact(
                lead["place_id"], contact.email, contact.source, contact.confidence
            )
            found += 1

    logger.info(f"Found contacts for {found} leads")
    run_ctx.increment("contacts_found", found)
    return found


def run_outreach_phase(
    config: Config,
    db: Database,
    run_ctx: RunContext,
    shutdown: GracefulShutdown,
    dry_run: bool = False,
) -> int:
    """Phase 6: Send outreach emails to qualifying leads."""
    if not config.outreach.enabled:
        logger.info("Outreach disabled, skipping")
        return 0

    leads = db.get_leads_ready_for_outreach(
        min_score=config.outreach.min_score_for_outreach,
        min_confidence=config.outreach.min_contact_confidence,
    )
    if leads:
        logger.info(f"Found {len(leads)} leads ready for outreach")
    else:
        logger.info("No leads ready for outreach")

    if dry_run:
        if leads:
            logger.info(f"[DRY-RUN] Would send outreach to {len(leads)} leads")
        followups = db.get_leads_for_followup(min_days_since_sent=3)
        if followups:
            logger.info(f"[DRY-RUN] Would send {len(followups)} follow-up emails")
        return 0

    sent = 0
    if leads:
        sent = run_outreach(leads, db, config.smtp, config.outreach, shutdown)

    remaining = max(config.outreach.max_emails_per_day - sent, 0)
    if remaining > 0:
        followups = run_followups(
            db=db,
            smtp_config=config.smtp,
            outreach_config=config.outreach,
            shutdown=shutdown,
            max_to_send=remaining,
        )
        run_ctx.increment("followups_sent", followups)
        sent += followups

    run_ctx.increment("outreach_sent", sent)
    return sent


def run_warm_delivery_phase(
    config: Config,
    db: Database,
    run_ctx: RunContext,
    dry_run: bool = False,
) -> bool:
    """Phase 7: Deliver warm (engaged) leads to subscribers."""
    if not config.outreach.enabled:
        return True

    warm_leads = db.get_warm_leads(
        min_engagement_score=config.delivery.warm_lead_min_engagement
    )
    if not warm_leads:
        logger.info("No warm leads to deliver")
        return True

    logger.info(f"Found {len(warm_leads)} warm leads")

    if dry_run:
        logger.info(f"[DRY-RUN] Would deliver {len(warm_leads)} warm leads")
        return True

    subscribers, sub_error = get_subscribers_with_isolation(
        config=config.gumroad, retry_config=config.retry
    )
    if sub_error:
        logger.error(f"Failed to get subscribers for warm delivery: {sub_error}")
        return False

    if not subscribers:
        logger.warning("No subscribers for warm lead delivery")
        return True

    pro_subs = [s for s in subscribers if s.tier == "pro"]
    if not pro_subs:
        logger.warning("No pro subscribers for warm lead delivery")
        return True

    portal_config = config.portal
    if portal_config and not portal_config.base_url:
        portal_config.base_url = config.outreach.tracking_base_url

    results, error = deliver_warm_leads_with_isolation(
        pro_subs, warm_leads, config, portal_config=portal_config
    )
    if error:
        logger.error(f"Warm lead delivery error: {error}")
        return False

    success_count = sum(1 for r in results if r.get("success"))
    logger.info(f"Delivered warm leads to {success_count}/{len(pro_subs)} subscribers")

    # Record exports for warm leads
    for result in results:
        if result.get("success"):
            db.record_export(
                run_id=run_ctx.run_id,
                subscriber_email=result.get("subscriber_email"),
                lead_count=len(warm_leads),
                csv_path=result.get("csv_path", ""),
                tier="pro",
                export_type="warm",
            )
    return success_count > 0


def run_weekly(
    config: Config = None,
    skip_scrape: bool = False,
    skip_delivery: bool = False,
    skip_outreach: bool = False,
    dry_run: bool = False,
    export_csv: bool = False,
):
    """
    Main weekly run entry point.

    Args:
        config: Configuration (loads from env if not provided)
        skip_scrape: Skip scraping phase (use existing DB leads)
        skip_delivery: Skip delivery phase (scrape only)
        skip_outreach: Skip outreach phases (audit gen, contact find, send)
        dry_run: Skip all database writes and email sends (for testing)
        export_csv: Generate local CSV report(s) without emailing or marking exported
    """
    if dry_run:
        logger.info("=== DRY-RUN MODE: No database writes or emails will be sent ===")
    # Setup
    setup_logging()
    shutdown = GracefulShutdown()

    if config is None:
        config = load_config()

    # Validate configuration
    validation_requirements = _build_validation_requirements(
        config,
        skip_delivery=skip_delivery,
        skip_outreach=skip_outreach,
        dry_run=dry_run,
    )
    errors = validate_config(config, **validation_requirements)
    if errors:
        for error in errors:
            logger.error(f"Config error: {error}")
        logger.error("Cannot proceed with invalid configuration")
        sys.exit(1)

    # Initialize database
    db = Database(config.database)

    # Run with context tracking
    with RunContext(logger) as run_ctx:
        if not dry_run:
            db.start_run(run_ctx.run_id)

        try:
            scraped_qualifying_leads: Optional[list[Lead]] = None

            # Phase 1: Scraping
            if not skip_scrape:
                logger.info("=== Phase 1: Scraping ===")
                run_ctx.start_phase("scraping")
                scraped_qualifying_leads = run_scraping_phase(
                    config, db, run_ctx, shutdown, dry_run=dry_run
                )
                duration = run_ctx.end_phase("scraping")
                logger.info("Phase 'scraping' completed in %.2fs", duration)

                if shutdown.check():
                    logger.warning("Shutdown during scrape phase")
                    _emit_run_kpis(run_ctx, dry_run=dry_run)
                    if not dry_run:
                        db.complete_run(run_ctx.run_id, run_ctx.stats, "shutdown_requested")
                    return

                # Phase 1b: Competitor Analysis
                if config.scraper.competitor_analysis_enabled and scraped_qualifying_leads:
                    logger.info("=== Phase 1b: Competitor Analysis ===")
                    run_ctx.start_phase("competitor_analysis")
                    for lead in scraped_qualifying_leads:
                        if shutdown.check():
                            break
                        try:
                            competitors_json = analyze_competitors_for_lead(lead, config)
                            if competitors_json and not dry_run:
                                db.update_lead_competitors(lead.place_id, competitors_json)
                                lead.competitors_json = competitors_json
                        except Exception as e:
                            logger.warning(f"Competitor analysis failed for {lead.name}: {e}")
                    duration = run_ctx.end_phase("competitor_analysis")
                    logger.info("Phase 'competitor_analysis' completed in %.2fs", duration)

            # Optional: Local CSV export (no emails, no export marking)
            if export_csv:
                logger.info("=== Local CSV Export ===")
                run_ctx.start_phase("export_csv")
                run_export_csv_phase(
                    config=config,
                    db=db,
                    run_ctx=run_ctx,
                    scraped_qualifying_leads=scraped_qualifying_leads,
                    label="local",
                )
                duration = run_ctx.end_phase("export_csv")
                logger.info("Phase 'export_csv' completed in %.2fs", duration)

            # Phase 2: Delivery (cold leads CSV)
            if not skip_delivery:
                logger.info("=== Phase 2: Delivery ===")
                run_ctx.start_phase("delivery")
                run_delivery_phase(config, db, run_ctx, dry_run=dry_run)
                duration = run_ctx.end_phase("delivery")
                logger.info("Phase 'delivery' completed in %.2fs", duration)

            # Phase 3: Manual review export (skip in dry-run)
            if config.scoring.manual_review_enabled and not dry_run:
                logger.info("=== Phase 3: Manual Review Export ===")
                run_ctx.start_phase("manual_review")
                run_manual_review_phase(config, db, run_ctx)
                duration = run_ctx.end_phase("manual_review")
                logger.info("Phase 'manual_review' completed in %.2fs", duration)

            # Phase 4: Generate audit pages
            if not skip_outreach:
                logger.info("=== Phase 4: Generate Audits ===")
                run_ctx.start_phase("audit_generation")
                run_audit_generation_phase(config, db, run_ctx, shutdown)
                duration = run_ctx.end_phase("audit_generation")
                logger.info("Phase 'audit_generation' completed in %.2fs", duration)

                if shutdown.check():
                    logger.warning("Shutdown during audit phase")
                    _emit_run_kpis(run_ctx, dry_run=dry_run)
                    if not dry_run:
                        db.complete_run(run_ctx.run_id, run_ctx.stats, "shutdown_requested")
                    return

            # Phase 5: Find contacts
            if not skip_outreach:
                logger.info("=== Phase 5: Find Contacts ===")
                run_ctx.start_phase("contact_finding")
                run_contact_finding_phase(config, db, run_ctx, shutdown)
                duration = run_ctx.end_phase("contact_finding")
                logger.info("Phase 'contact_finding' completed in %.2fs", duration)

                if shutdown.check():
                    logger.warning("Shutdown during contact phase")
                    _emit_run_kpis(run_ctx, dry_run=dry_run)
                    if not dry_run:
                        db.complete_run(run_ctx.run_id, run_ctx.stats, "shutdown_requested")
                    return

            # Phase 6: Send outreach
            if not skip_outreach:
                logger.info("=== Phase 6: Send Outreach ===")
                run_ctx.start_phase("outreach")
                run_outreach_phase(config, db, run_ctx, shutdown, dry_run=dry_run)
                duration = run_ctx.end_phase("outreach")
                logger.info("Phase 'outreach' completed in %.2fs", duration)

            # Phase 7: Deliver warm leads
            if not skip_outreach and not skip_delivery:
                logger.info("=== Phase 7: Deliver Warm Leads ===")
                run_ctx.start_phase("warm_delivery")
                run_warm_delivery_phase(config, db, run_ctx, dry_run=dry_run)
                duration = run_ctx.end_phase("warm_delivery")
                logger.info("Phase 'warm_delivery' completed in %.2fs", duration)

            _emit_run_kpis(run_ctx, dry_run=dry_run)

            # Complete run (skip in dry-run)
            if not dry_run:
                db.complete_run(run_ctx.run_id, run_ctx.stats)

                # Periodic cleanup
                db.cleanup_old_leads(days=180)

            logger.info("Weekly run completed successfully")

        except Exception as e:
            logger.exception(f"Fatal error in weekly run: {e}")
            _emit_run_kpis(run_ctx, dry_run=dry_run)
            if not dry_run:
                db.complete_run(run_ctx.run_id, run_ctx.stats, str(e))
            raise


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="BrokenSite Weekly Lead Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.run_weekly                  # Full run: scrape + deliver
  python -m src.run_weekly --scrape-only    # Scrape only, no delivery
  python -m src.run_weekly --deliver-only   # Deliver existing leads only
  python -m src.run_weekly --dry-run        # Test run with no side effects
  python -m src.run_weekly --scrape-only --dry-run  # Test scraping only
  python -m src.run_weekly --export-csv --dry-run    # Local CSV report, no emails
  python -m src.run_weekly --stats          # Show database stats
  python -m src.run_weekly --validate       # Check configuration
        """
    )

    parser.add_argument(
        "--scrape-only",
        action="store_true",
        help="Only run scraping phase, skip delivery",
    )
    parser.add_argument(
        "--deliver-only",
        action="store_true",
        help="Only run delivery phase, skip scraping",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show database statistics and exit",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate configuration for the selected run mode and exit",
    )
    parser.add_argument(
        "--outreach-only",
        action="store_true",
        help="Only run outreach phases (audit gen, contact find, send, warm delivery)",
    )
    parser.add_argument(
        "--no-outreach",
        action="store_true",
        help="Skip outreach phases (original pipeline only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without database writes or email sends (for testing)",
    )
    parser.add_argument(
        "--export-csv",
        action="store_true",
        help="Write local CSV report(s) without emailing or marking leads exported",
    )

    args = parser.parse_args()

    setup_logging()
    config = load_config()

    if args.stats:
        db = Database(config.database)
        stats = db.get_stats()
        print("Database Statistics:")
        print(f"  Total leads: {stats['total_leads']}")
        print(f"  Unique websites: {stats['unique_websites']}")
        print(f"  Total runs: {stats['total_runs']}")
        print(f"  Total exports: {stats['total_exports']}")
        if stats['last_run']:
            print(f"  Last run: {stats['last_run']['run_id']} ({stats['last_run']['status']})")

        top_combos = db.get_top_yield_city_categories(limit=5)
        if top_combos:
            print("  Top city/category yield (quality-first):")
            for combo in top_combos:
                print(
                    "    - "
                    f"{combo['city']} / {combo['category']}: "
                    f"{combo['lead_count']} leads, "
                    f"{combo['quality_lead_count']} quality (score>=60), "
                    f"avg score {combo['avg_score']}"
                )

        if stats.get("last_completed_run"):
            last_completed = stats["last_completed_run"]
            completed_at = last_completed.get("completed_at")
            completed_dt = completed_at
            if isinstance(completed_at, str):
                try:
                    completed_dt = datetime.fromisoformat(completed_at)
                except ValueError:
                    completed_dt = None
            if completed_dt:
                now = datetime.now(completed_dt.tzinfo) if getattr(completed_dt, "tzinfo", None) else datetime.now()
                days_since = (now - completed_dt).days
                print(
                    "  Last completed run: "
                    f"{last_completed['run_id']} ({days_since} days ago) | "
                    f"queries={last_completed['queries_attempted']} "
                    f"businesses={last_completed['businesses_found']} "
                    f"exported={last_completed['leads_exported']} "
                    f"emails={last_completed['emails_sent']}"
                )
                if days_since > 8:
                    print("  WARNING: Last completed run is older than 8 days.")
        return

    if args.validate:
        skip_delivery = args.scrape_only or args.outreach_only or args.export_csv
        skip_outreach = args.no_outreach or args.export_csv
        requirements = _build_validation_requirements(
            config,
            skip_delivery=skip_delivery,
            skip_outreach=skip_outreach,
            dry_run=args.dry_run,
        )
        errors = validate_config(config, **requirements)
        if errors:
            print("Configuration errors:")
            for error in errors:
                print(f"  - {error}")
            sys.exit(1)
        else:
            print("Configuration valid")
            return

    run_weekly(
        config=config,
        skip_scrape=args.deliver_only or args.outreach_only,
        skip_delivery=args.scrape_only or args.outreach_only or args.export_csv,
        skip_outreach=args.no_outreach or args.export_csv,
        dry_run=args.dry_run,
        export_csv=args.export_csv,
    )


if __name__ == "__main__":
    main()
