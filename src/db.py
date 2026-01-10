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
    """
    SQLite database manager for lead storage and deduplication.

    Optimized for deployment with:
    - Connection caching (reuses single connection per instance)
    - WAL mode for better concurrent access
    - Optimized pragmas for performance
    - Proper timeout handling
    """

    # Connection timeout in seconds
    CONNECTION_TIMEOUT = 30.0

    def __init__(self, config: DatabaseConfig = None):
        self.config = config or DatabaseConfig()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = self.config.db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_schema()

    def _get_connection(self) -> sqlite3.Connection:
        """Get or create a database connection with optimized settings."""
        if self._conn is not None:
            try:
                # Test connection is still valid
                self._conn.execute("SELECT 1")
                return self._conn
            except sqlite3.Error:
                # Connection is stale, create new one
                self._conn = None

        conn = sqlite3.connect(
            self.db_path,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            timeout=self.CONNECTION_TIMEOUT,
            isolation_level="DEFERRED",  # Better for mixed read/write workloads
        )
        conn.row_factory = sqlite3.Row

        # Apply performance pragmas
        conn.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging for concurrency
        conn.execute("PRAGMA synchronous=NORMAL")  # Good balance of safety/speed
        conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        conn.execute("PRAGMA temp_store=MEMORY")  # Keep temp tables in memory
        conn.execute("PRAGMA mmap_size=268435456")  # 256MB memory-mapped I/O
        conn.execute("PRAGMA busy_timeout=30000")  # 30 second busy timeout

        self._conn = conn
        return conn

    def _init_schema(self):
        """Initialize database schema."""
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            logger.info(f"Database initialized at {self.db_path}")

    @contextmanager
    def _connect(self):
        """
        Context manager for database transactions.

        Uses cached connection and handles transaction lifecycle.
        """
        conn = self._get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self):
        """Close the database connection. Call when done with database operations."""
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def __del__(self):
        """Ensure connection is closed on garbage collection."""
        self.close()

    def is_duplicate(self, place_id: str, website: Optional[str] = None) -> bool:
        """
        Check if a lead is a duplicate within the dedupe window.
        Primary key is place_id, but also checks website for fallback matching.

        Note: For atomic duplicate-check-and-insert, use upsert_lead_atomic() instead.
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

        This method is atomic - the check and insert/update happen in a single transaction.
        """
        now = datetime.utcnow()

        with self._connect() as conn:
            # Use a single atomic transaction with INSERT ... ON CONFLICT
            # First, try to get the existing first_seen value
            existing = conn.execute(
                "SELECT first_seen FROM leads WHERE place_id = ?",
                (lead.place_id,)
            ).fetchone()

            if existing:
                # Update existing - preserve first_seen
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

    def check_and_upsert_lead(self, lead: Lead) -> tuple[bool, bool]:
        """
        Atomically check for duplicates and upsert a lead.

        Returns:
            tuple[bool, bool]: (is_duplicate_in_window, is_new_lead)
            - is_duplicate_in_window: True if lead was seen within dedupe window
            - is_new_lead: True if this is a brand new lead (not seen before at all)

        This method eliminates the race condition between is_duplicate() and upsert_lead()
        by performing both operations in a single transaction.
        """
        now = datetime.utcnow()
        cutoff = now - timedelta(days=self.config.dedupe_window_days)

        with self._connect() as conn:
            # Check if exists and if within dedupe window - all in one query
            existing = conn.execute("""
                SELECT place_id, last_seen, first_seen
                FROM leads
                WHERE place_id = ? OR (website = ? AND website IS NOT NULL)
                ORDER BY
                    CASE WHEN place_id = ? THEN 0 ELSE 1 END,
                    last_seen DESC
                LIMIT 1
            """, (lead.place_id, lead.website, lead.place_id)).fetchone()

            if existing:
                is_within_window = existing["last_seen"] > cutoff
                is_same_place = existing["place_id"] == lead.place_id

                if is_same_place:
                    # Update existing record
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
                    return (is_within_window, False)
                else:
                    # Different place_id but same website - treat as duplicate if in window
                    if is_within_window:
                        return (True, False)
                    # Otherwise, insert as new record
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
                    return (False, True)
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
                return (False, True)

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
