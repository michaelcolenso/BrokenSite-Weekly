"""
SQLite database layer for BrokenSite-Weekly.
Handles lead storage, deduplication, and run history.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any
from contextlib import contextmanager
from dataclasses import dataclass

from .config import DatabaseConfig, DATA_DIR
from .logging_setup import get_logger

logger = get_logger("db")


@dataclass
class Lead:
    """Represents a business lead."""
    place_id: str  # Google Place ID - stable identifier
    name: str
    website: Optional[str]
    address: Optional[str]
    phone: Optional[str]
    city: str
    category: str
    score: int
    reasons: str
    first_seen: datetime
    last_seen: datetime
    cid: Optional[str] = None  # Google CID (alternative ID)


SCHEMA = """
-- Leads table: stores all discovered businesses
CREATE TABLE IF NOT EXISTS leads (
    place_id TEXT PRIMARY KEY,
    cid TEXT,
    name TEXT NOT NULL,
    website TEXT,
    address TEXT,
    phone TEXT,
    city TEXT NOT NULL,
    category TEXT NOT NULL,
    score INTEGER NOT NULL,
    reasons TEXT,
    first_seen TIMESTAMP NOT NULL,
    last_seen TIMESTAMP NOT NULL,
    exported_count INTEGER DEFAULT 0,
    last_exported TIMESTAMP
);

-- Index for deduplication lookups
CREATE INDEX IF NOT EXISTS idx_leads_website ON leads(website);
CREATE INDEX IF NOT EXISTS idx_leads_last_seen ON leads(last_seen);
CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score);

-- Run history: tracks each weekly run
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    status TEXT NOT NULL,  -- 'running', 'completed', 'failed'
    queries_attempted INTEGER DEFAULT 0,
    businesses_found INTEGER DEFAULT 0,
    leads_exported INTEGER DEFAULT 0,
    emails_sent INTEGER DEFAULT 0,
    error_message TEXT
);

-- Export history: tracks what was sent to whom
CREATE TABLE IF NOT EXISTS exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    subscriber_email TEXT NOT NULL,
    lead_count INTEGER NOT NULL,
    csv_path TEXT,
    sent_at TIMESTAMP NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_exports_subscriber ON exports(subscriber_email);
"""


class Database:
    """SQLite database manager for lead storage and deduplication."""

    def __init__(self, config: DatabaseConfig = None):
        self.config = config or DatabaseConfig()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = self.config.db_path
        self._init_schema()

    def _init_schema(self):
        """Initialize database schema."""
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            logger.info(f"Database initialized at {self.db_path}")

    @contextmanager
    def _connect(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(
            self.db_path,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
        )
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def is_duplicate(self, place_id: str, website: Optional[str] = None) -> bool:
        """
        Check if a lead is a duplicate within the dedupe window.
        Primary key is place_id, but also checks website for fallback matching.
        """
        cutoff = datetime.utcnow() - timedelta(days=self.config.dedupe_window_days)

        with self._connect() as conn:
            # Check by place_id first (most reliable)
            row = conn.execute(
                "SELECT 1 FROM leads WHERE place_id = ? AND last_seen > ?",
                (place_id, cutoff)
            ).fetchone()
            if row:
                return True

            # Fallback: check by website if provided
            if website:
                row = conn.execute(
                    "SELECT 1 FROM leads WHERE website = ? AND last_seen > ?",
                    (website, cutoff)
                ).fetchone()
                if row:
                    return True

        return False

    def upsert_lead(self, lead: Lead) -> bool:
        """
        Insert or update a lead. Returns True if this is a new lead.
        """
        now = datetime.utcnow()

        with self._connect() as conn:
            # Check if exists
            existing = conn.execute(
                "SELECT place_id, first_seen FROM leads WHERE place_id = ?",
                (lead.place_id,)
            ).fetchone()

            if existing:
                # Update existing
                conn.execute("""
                    UPDATE leads SET
                        name = ?, website = ?, address = ?, phone = ?,
                        city = ?, category = ?, score = ?, reasons = ?,
                        last_seen = ?, cid = ?
                    WHERE place_id = ?
                """, (
                    lead.name, lead.website, lead.address, lead.phone,
                    lead.city, lead.category, lead.score, lead.reasons,
                    now, lead.cid, lead.place_id
                ))
                return False
            else:
                # Insert new
                conn.execute("""
                    INSERT INTO leads (
                        place_id, cid, name, website, address, phone,
                        city, category, score, reasons, first_seen, last_seen
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    lead.place_id, lead.cid, lead.name, lead.website,
                    lead.address, lead.phone, lead.city, lead.category,
                    lead.score, lead.reasons, now, now
                ))
                return True

    def get_unexported_leads(self, min_score: int, limit: int = 500) -> List[Dict[str, Any]]:
        """Get leads that haven't been exported yet, above minimum score."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT place_id, cid, name, website, address, phone,
                       city, category, score, reasons, first_seen
                FROM leads
                WHERE score >= ? AND exported_count = 0 AND website IS NOT NULL
                ORDER BY score DESC, first_seen ASC
                LIMIT ?
            """, (min_score, limit)).fetchall()

            return [dict(row) for row in rows]

    def get_unverified_leads(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Get unverified leads for manual review."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT place_id, cid, name, website, address, phone,
                       city, category, score, reasons, first_seen
                FROM leads
                WHERE reasons LIKE '%unverified%' AND exported_count = 0
                ORDER BY score DESC, first_seen ASC
                LIMIT ?
            """, (limit,)).fetchall()

            return [dict(row) for row in rows]

    def mark_exported(self, place_ids: List[str]):
        """Mark leads as exported."""
        now = datetime.utcnow()
        with self._connect() as conn:
            conn.executemany(
                "UPDATE leads SET exported_count = exported_count + 1, last_exported = ? WHERE place_id = ?",
                [(now, pid) for pid in place_ids]
            )

    def start_run(self, run_id: str):
        """Record start of a weekly run."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, started_at, status) VALUES (?, ?, 'running')",
                (run_id, datetime.utcnow())
            )

    def complete_run(self, run_id: str, stats: Dict[str, int], error: str = None):
        """Record completion of a weekly run."""
        status = "failed" if error else "completed"
        with self._connect() as conn:
            conn.execute("""
                UPDATE runs SET
                    completed_at = ?,
                    status = ?,
                    queries_attempted = ?,
                    businesses_found = ?,
                    leads_exported = ?,
                    emails_sent = ?,
                    error_message = ?
                WHERE run_id = ?
            """, (
                datetime.utcnow(), status,
                stats.get("queries_attempted", 0),
                stats.get("businesses_found", 0),
                stats.get("leads_exported", 0),
                stats.get("emails_sent", 0),
                error, run_id
            ))

    def record_export(self, run_id: str, subscriber_email: str, lead_count: int, csv_path: str):
        """Record an export to a subscriber."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO exports (run_id, subscriber_email, lead_count, csv_path, sent_at)
                VALUES (?, ?, ?, ?, ?)
            """, (run_id, subscriber_email, lead_count, csv_path, datetime.utcnow()))

    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics."""
        with self._connect() as conn:
            total_leads = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            unique_websites = conn.execute(
                "SELECT COUNT(DISTINCT website) FROM leads WHERE website IS NOT NULL"
            ).fetchone()[0]
            total_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            last_run = conn.execute(
                "SELECT run_id, completed_at, status FROM runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()

            return {
                "total_leads": total_leads,
                "unique_websites": unique_websites,
                "total_runs": total_runs,
                "last_run": dict(last_run) if last_run else None,
            }

    def cleanup_old_leads(self, days: int = 180):
        """Remove leads older than specified days that were never exported."""
        cutoff = datetime.utcnow() - timedelta(days=days)
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM leads WHERE last_seen < ? AND exported_count = 0",
                (cutoff,)
            )
            logger.info(f"Cleaned up {result.rowcount} old unexported leads")
