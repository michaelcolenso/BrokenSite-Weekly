#!/usr/bin/env python3
"""
Weekly orchestrator for BrokenSite-Weekly.
Coordinates scraping, scoring, deduplication, and delivery.

Designed for unattended VPS operation via systemd timer.
"""

import sys
import signal
import argparse
from datetime import datetime
from typing import Optional

from .config import load_config, validate_config, Config
from .logging_setup import setup_logging, RunContext, get_logger
from .db import Database, Lead
from .maps_scraper import scrape_with_isolation, Business
from .scoring import evaluate_with_isolation, ScoringResult
from .gumroad import get_subscribers_with_isolation
from .delivery import deliver_with_isolation, generate_csv

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
) -> Optional[Lead]:
    """
    Process a single business: check website, score, store.
    Returns Lead if it qualifies, None otherwise.
    Fully isolated - never raises exceptions.
    """
    try:
        # Skip if no website
        if not business.website:
            logger.debug(f"Skipping {business.name}: no website")
            return None

        # Check for duplicate
        if db.is_duplicate(business.place_id, business.website):
            logger.debug(f"Skipping {business.name}: duplicate")
            return None

        run_ctx.increment("websites_checked")

        # Score the website
        result: ScoringResult = evaluate_with_isolation(
            url=business.website,
            config=config.scoring,
            retry_config=config.retry,
        )

        # Create lead record
        lead = Lead(
            place_id=business.place_id,
            cid=business.cid,
            name=business.name,
            website=business.website,
            address=business.address,
            phone=business.phone,
            city=business.city,
            category=business.category,
            score=result.score,
            reasons=",".join(result.reasons),
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
        )

        # Store in database
        is_new = db.upsert_lead(lead)

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
) -> list:
    """
    Phase 1: Scrape Google Maps and score websites.
    Returns list of qualifying leads.
    """
    qualifying_leads = []

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

                lead = process_business(business, db, config, run_ctx)
                if lead:
                    qualifying_leads.append(lead)

    return qualifying_leads


def run_delivery_phase(
    config: Config,
    db: Database,
    run_ctx: RunContext,
) -> bool:
    """
    Phase 2: Get subscribers and deliver CSV.
    Returns True if delivery succeeded.
    """
    # Get unexported leads from database
    leads_data = db.get_unexported_leads(
        min_score=config.scoring.min_score_to_include,
        limit=500,
    )

    if not leads_data:
        logger.info("No new leads to deliver")
        return True

    logger.info(f"Found {len(leads_data)} unexported leads")

    # Get active subscribers
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

    logger.info(f"Delivering to {len(subscribers)} subscribers")

    # Deliver to subscribers
    results, delivery_error = deliver_with_isolation(
        subscribers=subscribers,
        leads=leads_data,
        config=config.smtp,
        retry_config=config.retry,
    )

    if delivery_error:
        logger.error(f"Delivery system error: {delivery_error}")
        run_ctx.increment("errors")
        return False

    # Count successes
    success_count = sum(1 for r in results if r.success)
    run_ctx.stats["emails_sent"] = success_count
    run_ctx.stats["leads_exported"] = len(leads_data) if success_count > 0 else 0

    # Mark leads as exported if at least one delivery succeeded
    if success_count > 0:
        place_ids = [lead["place_id"] for lead in leads_data]
        db.mark_exported(place_ids)
        logger.info(f"Marked {len(place_ids)} leads as exported")

    # Record exports in database
    for result in results:
        if result.success:
            db.record_export(
                run_id=run_ctx.run_id,
                subscriber_email=result.subscriber_email,
                lead_count=len(leads_data),
                csv_path=str(config.database.db_path.parent / "output"),
            )

    return success_count > 0 or len(subscribers) == 0


def run_weekly(config: Config = None, skip_scrape: bool = False, skip_delivery: bool = False):
    """
    Main weekly run entry point.

    Args:
        config: Configuration (loads from env if not provided)
        skip_scrape: Skip scraping phase (use existing DB leads)
        skip_delivery: Skip delivery phase (scrape only)
    """
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
        db.start_run(run_ctx.run_id)

        try:
            # Phase 1: Scraping
            if not skip_scrape:
                logger.info("=== Phase 1: Scraping ===")
                run_scraping_phase(config, db, run_ctx, shutdown)

                if shutdown.check():
                    logger.warning("Shutdown during scrape phase")
                    db.complete_run(run_ctx.run_id, run_ctx.stats, "shutdown_requested")
                    return

            # Phase 2: Delivery
            if not skip_delivery:
                logger.info("=== Phase 2: Delivery ===")
                run_delivery_phase(config, db, run_ctx)

            # Complete run
            db.complete_run(run_ctx.run_id, run_ctx.stats)

            # Periodic cleanup
            db.cleanup_old_leads(days=180)

            logger.info("Weekly run completed successfully")

        except Exception as e:
            logger.exception(f"Fatal error in weekly run: {e}")
            db.complete_run(run_ctx.run_id, run_ctx.stats, str(e))
            raise


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="BrokenSite Weekly Lead Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.run_weekly              # Full run: scrape + deliver
  python -m src.run_weekly --scrape-only    # Scrape only, no delivery
  python -m src.run_weekly --deliver-only   # Deliver existing leads only
  python -m src.run_weekly --stats          # Show database stats
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
        skip_scrape=args.deliver_only,
        skip_delivery=args.scrape_only,
    )


if __name__ == "__main__":
    main()
