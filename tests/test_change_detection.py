"""
Tests for week-over-week change detection.
"""

import pytest
from datetime import datetime

from src.change_detection import (
    take_snapshot,
    detect_changes,
    get_deltas_for_run,
    get_lead_change_history,
    _get_all_leads,
    _get_previous_run_id,
)
from src.db import Database, Lead
from src.config import DatabaseConfig


def _seed_leads(db: Database, leads_data: list[dict]) -> None:
    """Insert leads directly into the DB for testing."""
    now = datetime.utcnow()
    with db._connect() as conn:
        for ld in leads_data:
            conn.execute("""
                INSERT OR REPLACE INTO leads (
                    place_id, name, website, city, category, score, reasons,
                    lead_tier, first_seen, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ld["place_id"], ld.get("name", "Test"), ld.get("website", "https://test.com"),
                ld.get("city", "Austin, TX"), ld.get("category", "plumber"),
                ld.get("score", 50), ld.get("reasons", "[]"),
                ld.get("lead_tier", "warm"), now, now,
            ))


class TestTakeSnapshot:
    def test_empty_db(self, tmp_path):
        db = Database(DatabaseConfig(db_path=tmp_path / "test.db"))
        snapshot = take_snapshot(db)
        assert snapshot == {}

    def test_captures_leads(self, tmp_path):
        db = Database(DatabaseConfig(db_path=tmp_path / "test.db"))
        _seed_leads(db, [
            {"place_id": "p1", "score": 75, "lead_tier": "warm"},
            {"place_id": "p2", "score": 45, "lead_tier": "cool"},
        ])
        snapshot = take_snapshot(db)
        assert len(snapshot) == 2
        assert snapshot["p1"]["score"] == 75
        assert snapshot["p2"]["lead_tier"] == "cool"


class TestDetectChanges:
    def test_new_leads_detected(self, tmp_path):
        db = Database(DatabaseConfig(db_path=tmp_path / "test.db"))
        # Seed with snapshot leads
        snapshot = {
            "p1": {"place_id": "p1", "score": 50, "lead_tier": "cool", "reasons": "[]"},
        }
        _seed_leads(db, [
            {"place_id": "p1", "score": 50, "lead_tier": "cool"},
            {"place_id": "p2", "score": 80, "lead_tier": "hot"},  # new
        ])

        # First, record a previous run
        with db._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, started_at, completed_at, status) VALUES (?, ?, ?, ?)",
                ("prev_run", datetime.utcnow(), datetime.utcnow(), "completed"),
            )

        result = detect_changes(
            db, current_run_id="curr_run",
            previous_snapshot=snapshot,
            previous_run_id="prev_run",
        )
        assert result["new"] == 1
        assert result["total_deltas"] >= 1

    def test_disappeared_leads_detected(self, tmp_path):
        db = Database(DatabaseConfig(db_path=tmp_path / "test.db"))
        snapshot = {
            "p1": {"place_id": "p1", "score": 50, "lead_tier": "cool", "reasons": "[]"},
            "p2": {"place_id": "p2", "score": 60, "lead_tier": "warm", "reasons": "[]"},
        }
        # Only p1 remains after the run
        _seed_leads(db, [
            {"place_id": "p1", "score": 50, "lead_tier": "cool"},
        ])

        with db._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, started_at, completed_at, status) VALUES (?, ?, ?, ?)",
                ("prev_run", datetime.utcnow(), datetime.utcnow(), "completed"),
            )

        result = detect_changes(
            db, current_run_id="curr_run",
            previous_snapshot=snapshot,
            previous_run_id="prev_run",
        )
        assert result["disappeared"] == 1

    def test_score_up_detected(self, tmp_path):
        db = Database(DatabaseConfig(db_path=tmp_path / "test.db"))
        snapshot = {
            "p1": {"place_id": "p1", "score": 40, "lead_tier": "cool", "reasons": "[]"},
        }
        _seed_leads(db, [
            {"place_id": "p1", "score": 80, "lead_tier": "hot"},
        ])

        with db._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, started_at, completed_at, status) VALUES (?, ?, ?, ?)",
                ("prev_run", datetime.utcnow(), datetime.utcnow(), "completed"),
            )

        result = detect_changes(
            db, current_run_id="curr_run",
            previous_snapshot=snapshot,
            previous_run_id="prev_run",
        )
        assert result["score_up"] == 1

    def test_score_down_detected(self, tmp_path):
        db = Database(DatabaseConfig(db_path=tmp_path / "test.db"))
        snapshot = {
            "p1": {"place_id": "p1", "score": 85, "lead_tier": "hot", "reasons": "[]"},
        }
        _seed_leads(db, [
            {"place_id": "p1", "score": 50, "lead_tier": "cool"},
        ])

        with db._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, started_at, completed_at, status) VALUES (?, ?, ?, ?)",
                ("prev_run", datetime.utcnow(), datetime.utcnow(), "completed"),
            )

        result = detect_changes(
            db, current_run_id="curr_run",
            previous_snapshot=snapshot,
            previous_run_id="prev_run",
        )
        assert result["score_down"] == 1

    def test_tier_change_detected(self, tmp_path):
        db = Database(DatabaseConfig(db_path=tmp_path / "test.db"))
        snapshot = {
            "p1": {"place_id": "p1", "score": 65, "lead_tier": "warm", "reasons": "[]"},
        }
        _seed_leads(db, [
            {"place_id": "p1", "score": 65, "lead_tier": "hot"},  # tier changed
        ])

        with db._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, started_at, completed_at, status) VALUES (?, ?, ?, ?)",
                ("prev_run", datetime.utcnow(), datetime.utcnow(), "completed"),
            )

        result = detect_changes(
            db, current_run_id="curr_run",
            previous_snapshot=snapshot,
            previous_run_id="prev_run",
            delta_threshold=30,  # high threshold so score delta doesn't fire
        )
        assert result["tier_up"] == 1

    def test_no_previous_run_skips(self, tmp_path):
        db = Database(DatabaseConfig(db_path=tmp_path / "test.db"))
        result = detect_changes(db, current_run_id="first_run")
        assert result.get("skipped") is True

    def test_small_score_delta_not_flagged(self, tmp_path):
        db = Database(DatabaseConfig(db_path=tmp_path / "test.db"))
        snapshot = {
            "p1": {"place_id": "p1", "score": 65, "lead_tier": "warm", "reasons": "[]"},
        }
        _seed_leads(db, [
            {"place_id": "p1", "score": 70, "lead_tier": "warm"},  # only +5
        ])

        with db._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, started_at, completed_at, status) VALUES (?, ?, ?, ?)",
                ("prev_run", datetime.utcnow(), datetime.utcnow(), "completed"),
            )

        result = detect_changes(
            db, current_run_id="curr_run",
            previous_snapshot=snapshot,
            previous_run_id="prev_run",
            delta_threshold=15,
        )
        assert result["score_up"] == 0
        assert result["score_down"] == 0


class TestGetDeltas:
    def test_empty_for_unknown_run(self, tmp_path):
        db = Database(DatabaseConfig(db_path=tmp_path / "test.db"))
        deltas = get_deltas_for_run(db, "nonexistent")
        assert deltas == []


class TestLeadHistory:
    def test_empty_for_unknown_lead(self, tmp_path):
        db = Database(DatabaseConfig(db_path=tmp_path / "test.db"))
        history = get_lead_change_history(db, "unknown_place")
        assert history == []
