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
        # Check for duplicate (skip in dry-run to show what would be processed)
        if not dry_run and db.is_duplicate(business.place_id, business.website):
            logger.debug(f"Skipping {business.name}: duplicate")
            return None

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
                reasons="no_website",
                first_seen=datetime.utcnow(),
                last_seen=datetime.utcnow(),
                lead_tier=compute_lead_tier(config.scoring.weight_no_website),
            )
            if not dry_run:
                db.upsert_lead(lead)

            if lead.score >= config.scoring.min_score_to_include:
                logger.info(
                    f"Lead: {business.name} | no website | "
                    f"Score: {lead.score} | Reasons: {lead.reasons}"
                )
                return lead

            return None

        run_ctx.increment("websites_checked")

        # Score the website
        from .scoring import evaluate_with_isolation

        result = evaluate_with_isolation(
            url=business.website,
            config=config.scoring,
            retry_config=config.retry,
        )

        reasons_str = ",".join(result.reasons)
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
            reasons=reasons_str,
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
            logger.info(
                f"Lead: {business.name} | {business.website} | "
                f"Score: {result.score} | Reasons: {result.reasons}"
            )
            return lead
        else:
            logger.debug(
                f"Low score: {business.name} | Score: {result.score}"
            )
            return None

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

            # Process each business
            for business in businesses:
                if shutdown.check():
                    break

                lead = process_business(business, db, config, run_ctx, dry_run=dry_run)
                if lead:
                    qualifying_leads.append(lead)

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
        min_score=config.outreach.min_score_for_outreach
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
    errors = validate_config(config)
    if errors and not skip_delivery:
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
                scraped_qualifying_leads = run_scraping_phase(
                    config, db, run_ctx, shutdown, dry_run=dry_run
                )

                if shutdown.check():
                    logger.warning("Shutdown during scrape phase")
                    if not dry_run:
                        db.complete_run(run_ctx.run_id, run_ctx.stats, "shutdown_requested")
                    return

            # Optional: Local CSV export (no emails, no export marking)
            if export_csv:
                logger.info("=== Local CSV Export ===")
                run_export_csv_phase(
                    config=config,
                    db=db,
                    run_ctx=run_ctx,
                    scraped_qualifying_leads=scraped_qualifying_leads,
                    label="local",
                )

            # Phase 2: Delivery (cold leads CSV)
            if not skip_delivery:
                logger.info("=== Phase 2: Delivery ===")
                run_delivery_phase(config, db, run_ctx, dry_run=dry_run)

            # Phase 3: Manual review export (skip in dry-run)
            if config.scoring.manual_review_enabled and not dry_run:
                logger.info("=== Phase 3: Manual Review Export ===")
                run_manual_review_phase(config, db, run_ctx)

            # Phase 4: Generate audit pages
            if not skip_outreach:
                logger.info("=== Phase 4: Generate Audits ===")
                run_audit_generation_phase(config, db, run_ctx, shutdown)

                if shutdown.check():
                    logger.warning("Shutdown during audit phase")
                    if not dry_run:
                        db.complete_run(run_ctx.run_id, run_ctx.stats, "shutdown_requested")
                    return

            # Phase 5: Find contacts
            if not skip_outreach:
                logger.info("=== Phase 5: Find Contacts ===")
                run_contact_finding_phase(config, db, run_ctx, shutdown)

                if shutdown.check():
                    logger.warning("Shutdown during contact phase")
                    if not dry_run:
                        db.complete_run(run_ctx.run_id, run_ctx.stats, "shutdown_requested")
                    return

            # Phase 6: Send outreach
            if not skip_outreach:
                logger.info("=== Phase 6: Send Outreach ===")
                run_outreach_phase(config, db, run_ctx, shutdown, dry_run=dry_run)

            # Phase 7: Deliver warm leads
            if not skip_outreach and not skip_delivery:
                logger.info("=== Phase 7: Deliver Warm Leads ===")
                run_warm_delivery_phase(config, db, run_ctx, dry_run=dry_run)

            # Complete run (skip in dry-run)
            if not dry_run:
                db.complete_run(run_ctx.run_id, run_ctx.stats)

                # Periodic cleanup
                db.cleanup_old_leads(days=180)

            logger.info("Weekly run completed successfully")

        except Exception as e:
            logger.exception(f"Fatal error in weekly run: {e}")
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
        help="Validate configuration and exit",
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
        if stats['last_run']:
            print(f"  Last run: {stats['last_run']['run_id']} ({stats['last_run']['status']})")
        return

    if args.validate:
        errors = validate_config(config)
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
