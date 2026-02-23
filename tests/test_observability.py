import logging
import time
from datetime import datetime

from src.db import Lead
from src.logging_setup import RunContext


def test_run_context_tracks_phase_duration_and_reason_histogram():
    run_ctx = RunContext(logging.getLogger("test_obs"))

    run_ctx.start_phase("scraping")
    time.sleep(0.01)
    duration = run_ctx.end_phase("scraping")

    assert duration > 0
    assert run_ctx.stats["phase_durations_seconds"]["scraping"] > 0

    run_ctx.count_reasons(["ssl_error", "timeout", "ssl_error"])
    assert run_ctx.stats["reason_counts"]["ssl_error"] == 2
    assert run_ctx.stats["reason_counts"]["timeout"] == 1


def test_top_yield_city_category_report_ranks_quality_first(test_database):
    leads = [
        Lead(
            place_id="a1",
            cid="1",
            name="A1",
            website="https://a1.example.com",
            address="Addr",
            phone="555-0001",
            review_count=10,
            city="Austin, TX",
            category="plumber",
            score=85,
            reasons="ssl_error",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
            lead_tier="pro",
        ),
        Lead(
            place_id="a2",
            cid="2",
            name="A2",
            website="https://a2.example.com",
            address="Addr",
            phone="555-0002",
            review_count=5,
            city="Austin, TX",
            category="plumber",
            score=70,
            reasons="timeout",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
            lead_tier="pro",
        ),
        Lead(
            place_id="b1",
            cid="3",
            name="B1",
            website="https://b1.example.com",
            address="Addr",
            phone="555-0003",
            review_count=3,
            city="Boston, MA",
            category="electrician",
            score=61,
            reasons="missing_viewport",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
            lead_tier="basic",
        ),
        Lead(
            place_id="b2",
            cid="4",
            name="B2",
            website="https://b2.example.com",
            address="Addr",
            phone="555-0004",
            review_count=3,
            city="Boston, MA",
            category="electrician",
            score=50,
            reasons="none",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
            lead_tier="basic",
        ),
    ]

    for lead in leads:
        assert test_database.upsert_lead(lead)

    combos = test_database.get_top_yield_city_categories(limit=5)

    assert combos
    assert combos[0]["city"] == "Austin, TX"
    assert combos[0]["category"] == "plumber"
    assert combos[0]["quality_lead_count"] == 2
