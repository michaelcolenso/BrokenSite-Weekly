"""
Tests for the database module.
"""

import pytest
from datetime import datetime, timedelta

from src.db import Database, Lead


class TestDatabaseInitialization:
    """Tests for database initialization."""

    def test_creates_database_file(self, test_database, test_db_path):
        """Database file should be created."""
        assert test_db_path.exists()

    def test_creates_leads_table(self, test_database):
        """Leads table should exist."""
        with test_database._connect() as conn:
            result = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='leads'"
            ).fetchone()
            assert result is not None

    def test_creates_runs_table(self, test_database):
        """Runs table should exist."""
        with test_database._connect() as conn:
            result = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
            ).fetchone()
            assert result is not None

    def test_creates_exports_table(self, test_database):
        """Exports table should exist."""
        with test_database._connect() as conn:
            result = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='exports'"
            ).fetchone()
            assert result is not None

    def test_creates_suppression_and_inquiries_tables(self, test_database):
        """Suppression and lead inquiries tables should exist."""
        with test_database._connect() as conn:
            suppression = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='suppression'"
            ).fetchone()
            inquiries = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='lead_inquiries'"
            ).fetchone()
            assert suppression is not None
            assert inquiries is not None

    def test_leads_has_exclusive_columns(self, test_database):
        """Leads table should include exclusive and tier columns."""
        with test_database._connect() as conn:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
            assert "exclusive_until" in cols
            assert "exclusive_tier" in cols
            assert "lead_tier" in cols
            assert "exported_basic_at" in cols
            assert "exported_pro_at" in cols


class TestLeadUpsert:
    """Tests for lead upsert operations."""

    def test_inserts_new_lead(self, test_database):
        """Should insert a new lead and return True."""
        lead = Lead(
            place_id="test_place_123",
            cid="12345",
            name="Test Business",
            website="https://test.com",
            address="123 Test St",
            phone="555-1234",
            city="Test City, TX",
            category="plumber",
            score=75,
            reasons="parked_domain",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
        )

        is_new = test_database.upsert_lead(lead)

        assert is_new is True

    def test_updates_existing_lead_outside_window(self, test_database, database_config):
        """Should update lead if last_seen is outside dedupe window."""
        old_time = datetime.utcnow() - timedelta(days=database_config.dedupe_window_days + 1)

        # Insert old lead
        lead1 = Lead(
            place_id="test_place_123",
            cid="12345",
            name="Old Name",
            website="https://test.com",
            address="123 Test St",
            phone="555-1234",
            city="Test City, TX",
            category="plumber",
            score=50,
            reasons="old_reason",
            first_seen=old_time,
            last_seen=old_time,
        )
        with test_database._connect() as conn:
            conn.execute("""
                INSERT INTO leads (
                    place_id, cid, name, website, address, phone,
                    city, category, score, reasons, first_seen, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                lead1.place_id, lead1.cid, lead1.name, lead1.website,
                lead1.address, lead1.phone, lead1.city, lead1.category,
                lead1.score, lead1.reasons, lead1.first_seen, lead1.last_seen,
            ))

        # Upsert with new data
        lead2 = Lead(
            place_id="test_place_123",
            cid="12345",
            name="New Name",
            website="https://test.com",
            address="123 Test St",
            phone="555-1234",
            city="Test City, TX",
            category="plumber",
            score=75,
            reasons="parked_domain",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
        )
        is_new = test_database.upsert_lead(lead2)

        assert is_new is True

        # Verify update
        with test_database._connect() as conn:
            row = conn.execute(
                "SELECT name, score FROM leads WHERE place_id = ?",
                (lead2.place_id,)
            ).fetchone()
            assert row["name"] == "New Name"
            assert row["score"] == 75

    def test_does_not_update_within_window(self, test_database, database_config):
        """Should not update lead if last_seen is within dedupe window."""
        recent_time = datetime.utcnow() - timedelta(days=1)

        # Insert recent lead directly
        with test_database._connect() as conn:
            conn.execute("""
                INSERT INTO leads (
                    place_id, cid, name, website, address, phone,
                    city, category, score, reasons, first_seen, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "test_place_123", "12345", "Original Name", "https://test.com",
                "123 Test St", "555-1234", "Test City, TX", "plumber",
                50, "original_reason", recent_time, recent_time,
            ))

        # Try to upsert - should not update
        lead = Lead(
            place_id="test_place_123",
            cid="12345",
            name="New Name",
            website="https://test.com",
            address="123 Test St",
            phone="555-1234",
            city="Test City, TX",
            category="plumber",
            score=75,
            reasons="new_reason",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
        )
        is_new = test_database.upsert_lead(lead)

        assert is_new is False

        # Verify not updated
        with test_database._connect() as conn:
            row = conn.execute(
                "SELECT name, score FROM leads WHERE place_id = ?",
                (lead.place_id,)
            ).fetchone()
            assert row["name"] == "Original Name"
            assert row["score"] == 50


class TestDuplicateDetection:
    """Tests for duplicate detection."""

    def test_detects_duplicate_by_place_id(self, test_database):
        """Should detect duplicate by place_id."""
        lead = Lead(
            place_id="test_place_123",
            cid="12345",
            name="Test Business",
            website="https://test.com",
            address="123 Test St",
            phone="555-1234",
            city="Test City, TX",
            category="plumber",
            score=75,
            reasons="parked_domain",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
        )
        test_database.upsert_lead(lead)

        is_dup = test_database.is_duplicate("test_place_123")

        assert is_dup is True

    def test_detects_duplicate_by_website(self, test_database):
        """Should detect duplicate by website as fallback."""
        lead = Lead(
            place_id="test_place_123",
            cid="12345",
            name="Test Business",
            website="https://test.com",
            address="123 Test St",
            phone="555-1234",
            city="Test City, TX",
            category="plumber",
            score=75,
            reasons="parked_domain",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
        )
        test_database.upsert_lead(lead)

        # Check with different place_id but same website
        is_dup = test_database.is_duplicate("different_place_id", "https://test.com")

        assert is_dup is True

    def test_no_duplicate_for_new_business(self, test_database):
        """Should return False for new business."""
        is_dup = test_database.is_duplicate("new_place_id", "https://newsite.com")

        assert is_dup is False

    def test_no_duplicate_outside_window(self, test_database, database_config):
        """Should not detect duplicate outside dedupe window."""
        old_time = datetime.utcnow() - timedelta(days=database_config.dedupe_window_days + 1)

        # Insert old lead directly
        with test_database._connect() as conn:
            conn.execute("""
                INSERT INTO leads (
                    place_id, cid, name, website, address, phone,
                    city, category, score, reasons, first_seen, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "old_place_123", "12345", "Old Business", "https://old.com",
                "123 Test St", "555-1234", "Test City, TX", "plumber",
                50, "old_reason", old_time, old_time,
            ))

        is_dup = test_database.is_duplicate("old_place_123")
        assert is_dup is False


class TestExclusivityFiltering:
    """Tests for exclusive lead window filtering."""

    def test_exclusive_lead_hidden_from_basic(self, test_database):
        lead = Lead(
            place_id="exclusive_place",
            cid="c1",
            name="Exclusive Biz",
            website="https://exclusive.com",
            address="1 Exclusive Way",
            phone="555-0000",
            review_count=25,
            city="Test City, TX",
            category="plumber",
            score=80,
            reasons="parked_domain",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
            exclusive_until=datetime.utcnow() + timedelta(days=7),
            exclusive_tier="pro",
            lead_tier="hot",
        )
        test_database.upsert_lead(lead)

        basic = test_database.get_unexported_leads_for_tier(40, tier="basic")
        pro = test_database.get_unexported_leads_for_tier(40, tier="pro")

        assert all(l["place_id"] != "exclusive_place" for l in basic)
        assert any(l["place_id"] == "exclusive_place" for l in pro)


class TestExportTracking:
    """Tests for export tracking."""

    def test_gets_unexported_leads(self, test_database):
        """Should return leads that haven't been exported."""
        lead = Lead(
            place_id="test_place_123",
            cid="12345",
            name="Test Business",
            website="https://test.com",
            address="123 Test St",
            phone="555-1234",
            city="Test City, TX",
            category="plumber",
            score=75,
            reasons="parked_domain",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
        )
        test_database.upsert_lead(lead)

        leads = test_database.get_unexported_leads(min_score=40)

        assert len(leads) == 1
        assert leads[0]["place_id"] == "test_place_123"

    def test_excludes_low_score_leads(self, test_database):
        """Should exclude leads below min_score."""
        lead = Lead(
            place_id="test_place_123",
            cid="12345",
            name="Test Business",
            website="https://test.com",
            address="123 Test St",
            phone="555-1234",
            city="Test City, TX",
            category="plumber",
            score=30,  # Below threshold
            reasons="minor_issue",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
        )
        test_database.upsert_lead(lead)

        leads = test_database.get_unexported_leads(min_score=40)

        assert len(leads) == 0

    def test_excludes_leads_without_website(self, test_database):
        """Should exclude leads without a website."""
        lead = Lead(
            place_id="test_place_123",
            cid="12345",
            name="Test Business",
            website=None,  # No website
            address="123 Test St",
            phone="555-1234",
            city="Test City, TX",
            category="plumber",
            score=75,
            reasons="no_website",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
        )
        test_database.upsert_lead(lead)

        leads = test_database.get_unexported_leads(min_score=40)

        assert len(leads) == 0

    def test_marks_leads_exported(self, test_database):
        """Should mark leads as exported."""
        lead = Lead(
            place_id="test_place_123",
            cid="12345",
            name="Test Business",
            website="https://test.com",
            address="123 Test St",
            phone="555-1234",
            city="Test City, TX",
            category="plumber",
            score=75,
            reasons="parked_domain",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
        )
        test_database.upsert_lead(lead)

        test_database.mark_exported(["test_place_123"])

        # Should not appear in unexported leads
        leads = test_database.get_unexported_leads(min_score=40)
        assert len(leads) == 0

        # Verify exported_count
        with test_database._connect() as conn:
            row = conn.execute(
                "SELECT exported_count FROM leads WHERE place_id = ?",
                ("test_place_123",)
            ).fetchone()
            assert row["exported_count"] == 1


class TestRunTracking:
    """Tests for run tracking."""

    def test_records_run_start(self, test_database):
        """Should record run start."""
        test_database.start_run("run_001")

        with test_database._connect() as conn:
            row = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?",
                ("run_001",)
            ).fetchone()
            assert row["status"] == "running"

    def test_records_run_completion(self, test_database):
        """Should record run completion."""
        test_database.start_run("run_001")

        stats = {
            "queries_attempted": 10,
            "businesses_found": 50,
            "leads_exported": 25,
            "emails_sent": 5,
        }
        test_database.complete_run("run_001", stats)

        with test_database._connect() as conn:
            row = conn.execute(
                "SELECT status, businesses_found FROM runs WHERE run_id = ?",
                ("run_001",)
            ).fetchone()
            assert row["status"] == "completed"
            assert row["businesses_found"] == 50

    def test_records_run_failure(self, test_database):
        """Should record run failure."""
        test_database.start_run("run_001")

        test_database.complete_run("run_001", {}, error="Something went wrong")

        with test_database._connect() as conn:
            row = conn.execute(
                "SELECT status, error_message FROM runs WHERE run_id = ?",
                ("run_001",)
            ).fetchone()
            assert row["status"] == "failed"
            assert row["error_message"] == "Something went wrong"


class TestStats:
    """Tests for statistics."""

    def test_returns_stats(self, test_database):
        """Should return database statistics."""
        # Add some data
        lead = Lead(
            place_id="test_place_123",
            cid="12345",
            name="Test Business",
            website="https://test.com",
            address="123 Test St",
            phone="555-1234",
            city="Test City, TX",
            category="plumber",
            score=75,
            reasons="parked_domain",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
        )
        test_database.upsert_lead(lead)
        test_database.start_run("run_001")
        test_database.complete_run("run_001", {})

        stats = test_database.get_stats()

        assert stats["total_leads"] == 1
        assert stats["unique_websites"] == 1
        assert stats["total_runs"] == 1
        assert stats["last_run"]["run_id"] == "run_001"
