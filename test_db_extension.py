#!/usr/bin/env python3
"""
Quick validation test for database extension.
Tests that all new tables and methods are present and functional.
"""

import sys
import os
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.db import Database
from src.config import DatabaseConfig
import tempfile

def test_database_extension():
    """Test all new database functionality."""

    # Use a temporary database
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # Initialize database
        config = DatabaseConfig()
        config.db_path = Path(tmp_path)
        db = Database(config)
        print("✓ Database initialized successfully")

        # Test table creation by checking if we can query them
        with db._connect() as conn:
            # Check all new tables exist
            tables = conn.execute("""
                SELECT name FROM sqlite_master
                WHERE type='table' AND name IN (
                    'audits', 'contacts', 'outreach', 'engagement_events',
                    'unsubscribes', 'suppression', 'lead_inquiries'
                )
            """).fetchall()

            table_names = [t['name'] for t in tables]
            expected_tables = ['audits', 'contacts', 'outreach', 'engagement_events', 'unsubscribes', 'suppression', 'lead_inquiries']

            for table in expected_tables:
                if table in table_names:
                    print(f"✓ Table '{table}' created")
                else:
                    print(f"✗ Table '{table}' missing")
                    return False

            # Check indexes
            indexes = conn.execute("""
                SELECT name FROM sqlite_master
                WHERE type='index' AND name IN (
                    'idx_outreach_place_id',
                    'idx_engagement_place_id',
                    'idx_engagement_type'
                )
            """).fetchall()

            index_names = [i['name'] for i in indexes]
            expected_indexes = ['idx_outreach_place_id', 'idx_engagement_place_id', 'idx_engagement_type']

            for index in expected_indexes:
                if index in index_names:
                    print(f"✓ Index '{index}' created")
                else:
                    print(f"✗ Index '{index}' missing")
                    return False

        # Test audit methods
        print("\nTesting audit methods...")
        db.record_audit("test_place_1", "https://example.com/audit/1", "/path/to/audit.html", '{"issues": []}')
        print("✓ record_audit()")

        url = db.get_audit_url("test_place_1")
        assert url == "https://example.com/audit/1", f"Expected audit URL, got {url}"
        print("✓ get_audit_url()")

        leads_without_audits = db.get_leads_without_audits(min_score=40)
        assert isinstance(leads_without_audits, list), "Expected list from get_leads_without_audits"
        print("✓ get_leads_without_audits()")

        # Test contact methods
        print("\nTesting contact methods...")
        db.record_contact("test_place_1", "test@example.com", "json-ld", 0.95)
        print("✓ record_contact()")

        contact = db.get_contact("test_place_1")
        assert contact is not None, "Expected contact info"
        assert contact["email"] == "test@example.com", f"Expected test@example.com, got {contact['email']}"
        print("✓ get_contact()")

        leads_without_contacts = db.get_leads_without_contacts()
        assert isinstance(leads_without_contacts, list), "Expected list from get_leads_without_contacts"
        print("✓ get_leads_without_contacts()")

        # Test outreach methods
        print("\nTesting outreach methods...")
        db.record_outreach("test_place_1", "test@example.com", "https://example.com/audit/1", True)
        print("✓ record_outreach()")

        has_contact = db.has_been_contacted("test_place_1")
        assert has_contact is True, "Expected has_been_contacted to be True"
        print("✓ has_been_contacted()")

        ready_for_outreach = db.get_leads_ready_for_outreach(min_score=40)
        assert isinstance(ready_for_outreach, list), "Expected list from get_leads_ready_for_outreach"
        print("✓ get_leads_ready_for_outreach()")

        # Test engagement methods
        print("\nTesting engagement methods...")
        db.record_event("test_place_1", "page_view", "127.0.0.1", "Mozilla/5.0")
        print("✓ record_event()")

        events = db.get_events_for_lead("test_place_1")
        assert isinstance(events, list), "Expected list from get_events_for_lead"
        assert len(events) > 0, "Expected at least one event"
        print("✓ get_events_for_lead()")

        score = db.get_engagement_score("test_place_1")
        assert isinstance(score, int), f"Expected int score, got {type(score)}"
        assert score == 25, f"Expected score of 25 (one page_view), got {score}"
        print(f"✓ get_engagement_score() = {score}")

        # Test unsubscribe methods
        print("\nTesting unsubscribe methods...")
        db.add_unsubscribe("test_place_2", "unsubscribe@example.com")
        print("✓ add_unsubscribe()")

        is_unsub = db.is_unsubscribed("test_place_2")
        assert is_unsub is True, "Expected is_unsubscribed to be True"
        print("✓ is_unsubscribed()")

        # Test engagement score with unsubscribe
        db.record_event("test_place_2", "page_view", "127.0.0.1", "Mozilla/5.0")
        score_unsub = db.get_engagement_score("test_place_2")
        assert score_unsub == -100, f"Expected score of -100 for unsubscribed lead, got {score_unsub}"
        print("✓ get_engagement_score() returns -100 for unsubscribed")

        # Test warm leads query
        print("\nTesting warm leads query...")
        warm_leads = db.get_warm_leads(min_engagement_score=25)
        assert isinstance(warm_leads, list), "Expected list from get_warm_leads"
        print("✓ get_warm_leads()")

        print("\n" + "="*50)
        print("All tests passed! Database extension is working correctly.")
        print("="*50)
        return True

    finally:
        # Clean up temp file
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

if __name__ == "__main__":
    success = test_database_extension()
    sys.exit(0 if success else 1)
