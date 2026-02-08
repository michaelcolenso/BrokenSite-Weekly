"""
SQLite database layer for BrokenSite-Weekly.
Handles lead storage, deduplication, and run history.
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any, Iterable
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
    reasons: List[str] | str
    first_seen: datetime
    last_seen: datetime
    review_count: Optional[int] = None
    cid: Optional[str] = None  # Google CID (alternative ID)
    exclusive_until: Optional[datetime] = None
    exclusive_tier: Optional[str] = None
    lead_tier: Optional[str] = None


SCHEMA = """
-- Leads table: stores all discovered businesses
CREATE TABLE IF NOT EXISTS leads (
    place_id TEXT PRIMARY KEY,
    cid TEXT,
    name TEXT NOT NULL,
    website TEXT,
    address TEXT,
    phone TEXT,
    review_count INTEGER,
    city TEXT NOT NULL,
    category TEXT NOT NULL,
    score INTEGER NOT NULL,
    reasons TEXT,
    first_seen TIMESTAMP NOT NULL,
    last_seen TIMESTAMP NOT NULL,
    exported_count INTEGER DEFAULT 0,
    last_exported TIMESTAMP,
    exported_basic_at TIMESTAMP,
    exported_pro_at TIMESTAMP,
    exclusive_until TIMESTAMP,
    exclusive_tier TEXT,
    lead_tier TEXT
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
    tier TEXT,
    export_type TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_exports_subscriber ON exports(subscriber_email);

-- Audit pages generated for leads
CREATE TABLE IF NOT EXISTS audits (
    place_id TEXT PRIMARY KEY,
    audit_url TEXT,
    audit_html_path TEXT,
    generated_at TIMESTAMP,
    issues_json TEXT
);

-- Contact information found for leads
CREATE TABLE IF NOT EXISTS contacts (
    place_id TEXT PRIMARY KEY,
    email TEXT,
    source TEXT,
    confidence REAL,
    found_at TIMESTAMP
);

-- Outreach attempts to leads
CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id TEXT,
    email TEXT,
    audit_url TEXT,
    sent_at TIMESTAMP,
    success BOOLEAN,
    error TEXT,
    followup_sent_at TIMESTAMP,
    followup_success BOOLEAN,
    followup_error TEXT,
    UNIQUE(place_id, email)
);

CREATE INDEX IF NOT EXISTS idx_outreach_place_id ON outreach(place_id);

-- Engagement tracking events
CREATE TABLE IF NOT EXISTS engagement_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id TEXT,
    event_type TEXT,
    ip_address TEXT,
    user_agent TEXT,
    timestamp TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_engagement_place_id ON engagement_events(place_id);
CREATE INDEX IF NOT EXISTS idx_engagement_type ON engagement_events(event_type);

-- Unsubscribe list
CREATE TABLE IF NOT EXISTS unsubscribes (
    place_id TEXT PRIMARY KEY,
    email TEXT,
    unsubscribed_at TIMESTAMP
);

-- Suppression list for bad emails
CREATE TABLE IF NOT EXISTS suppression (
    email TEXT PRIMARY KEY,
    reason TEXT,
    suppressed_at TIMESTAMP
);

-- Lead inquiry submissions from CTA form
CREATE TABLE IF NOT EXISTS lead_inquiries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id TEXT,
    name TEXT,
    email TEXT,
    phone TEXT,
    notes TEXT,
    created_at TIMESTAMP
);
"""


class Database:
    """SQLite database manager for lead storage and deduplication."""

    def __init__(self, config: DatabaseConfig = None):
        self.config = config or DatabaseConfig()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = self.config.db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        """Initialize database schema."""
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._ensure_columns(conn)
            logger.info(f"Database initialized at {self.db_path}")

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        """Ensure new columns exist for backward compatibility."""
        lead_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(leads)").fetchall()
        }
        if "review_count" not in lead_columns:
            conn.execute("ALTER TABLE leads ADD COLUMN review_count INTEGER")
        if "exported_basic_at" not in lead_columns:
            conn.execute("ALTER TABLE leads ADD COLUMN exported_basic_at TIMESTAMP")
        if "exported_pro_at" not in lead_columns:
            conn.execute("ALTER TABLE leads ADD COLUMN exported_pro_at TIMESTAMP")
        if "exclusive_until" not in lead_columns:
            conn.execute("ALTER TABLE leads ADD COLUMN exclusive_until TIMESTAMP")
        if "exclusive_tier" not in lead_columns:
            conn.execute("ALTER TABLE leads ADD COLUMN exclusive_tier TEXT")
        if "lead_tier" not in lead_columns:
            conn.execute("ALTER TABLE leads ADD COLUMN lead_tier TEXT")

        export_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(exports)").fetchall()
        }
        if "tier" not in export_columns:
            conn.execute("ALTER TABLE exports ADD COLUMN tier TEXT")
        if "export_type" not in export_columns:
            conn.execute("ALTER TABLE exports ADD COLUMN export_type TEXT")

        outreach_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(outreach)").fetchall()
        }
        if "followup_sent_at" not in outreach_columns:
            conn.execute("ALTER TABLE outreach ADD COLUMN followup_sent_at TIMESTAMP")
        if "followup_success" not in outreach_columns:
            conn.execute("ALTER TABLE outreach ADD COLUMN followup_success BOOLEAN")
        if "followup_error" not in outreach_columns:
            conn.execute("ALTER TABLE outreach ADD COLUMN followup_error TEXT")

    @contextmanager
    def _connect(self):
        """Context manager for database connections."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        """Close the shared database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _serialize_reasons(self, reasons: Optional[Iterable[str] | str]) -> str:
        if not reasons:
            return json.dumps([])
        if isinstance(reasons, str):
            stripped = reasons.strip()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        return json.dumps([str(r) for r in parsed if str(r).strip()])
                except json.JSONDecodeError:
                    pass
            items = [r.strip() for r in reasons.split(",") if r.strip()]
            return json.dumps(items)
        return json.dumps([str(r).strip() for r in reasons if str(r).strip()])

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
        Insert or update a lead. Returns True if this is a new (non-duplicate) lead.
        """
        now = datetime.utcnow()
        cutoff = now - timedelta(days=self.config.dedupe_window_days)
        reasons_json = self._serialize_reasons(lead.reasons)

        with self._connect() as conn:
            row = conn.execute("""
                INSERT INTO leads (
                    place_id, cid, name, website, address, phone,
                    review_count, city, category, score, reasons, first_seen, last_seen,
                    exclusive_until, exclusive_tier, lead_tier
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(place_id) DO UPDATE SET
                    name = excluded.name,
                    website = excluded.website,
                    address = excluded.address,
                    phone = excluded.phone,
                    review_count = excluded.review_count,
                    city = excluded.city,
                    category = excluded.category,
                    score = excluded.score,
                    reasons = excluded.reasons,
                    last_seen = excluded.last_seen,
                    cid = excluded.cid,
                    exclusive_until = excluded.exclusive_until,
                    exclusive_tier = excluded.exclusive_tier,
                    lead_tier = excluded.lead_tier
                WHERE leads.last_seen <= ?
                RETURNING place_id
            """, (
                lead.place_id, lead.cid, lead.name, lead.website,
                lead.address, lead.phone, lead.review_count, lead.city, lead.category,
                lead.score, reasons_json, now, now,
                lead.exclusive_until, lead.exclusive_tier, lead.lead_tier,
                cutoff
            )).fetchone()
            return row is not None

    def get_unexported_leads(
        self,
        min_score: int,
        limit: int = 500,
        tier: str = "basic",
    ) -> List[Dict[str, Any]]:
        """Get leads that haven't been exported for a given tier, above minimum score."""
        return self.get_unexported_leads_for_tier(min_score=min_score, tier=tier, limit=limit)

    def get_unexported_leads_for_tier(
        self,
        min_score: int,
        tier: str,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Get unexported leads for a specific tier with exclusivity rules."""
        now = datetime.utcnow()
        tier = (tier or "basic").lower()

        with self._connect() as conn:
            if tier == "pro":
                rows = conn.execute("""
                    SELECT place_id, cid, name, website, address, phone, review_count,
                           city, category, score, reasons, first_seen, lead_tier,
                           exclusive_until, exclusive_tier
                    FROM leads
                    WHERE score >= ? AND website IS NOT NULL
                      AND exported_pro_at IS NULL
                    ORDER BY score DESC, first_seen ASC
                    LIMIT ?
                """, (min_score, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT place_id, cid, name, website, address, phone, review_count,
                           city, category, score, reasons, first_seen, lead_tier,
                           exclusive_until, exclusive_tier
                    FROM leads
                    WHERE score >= ? AND website IS NOT NULL
                      AND exported_basic_at IS NULL
                      AND (
                        exclusive_until IS NULL
                        OR exclusive_until < ?
                        OR exclusive_tier IS NULL
                        OR exclusive_tier != 'pro'
                      )
                    ORDER BY score DESC, first_seen ASC
                    LIMIT ?
                """, (min_score, now, limit)).fetchall()

            return [dict(row) for row in rows]

    def get_unverified_leads(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Get unverified leads for manual review."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT place_id, cid, name, website, address, phone, review_count,
                       city, category, score, reasons, first_seen
                FROM leads
                WHERE reasons LIKE '%unverified%' AND exported_count = 0
                ORDER BY score DESC, first_seen ASC
                LIMIT ?
            """, (limit,)).fetchall()

            return [dict(row) for row in rows]

    def get_lead_summary(self, place_id: str) -> Optional[Dict[str, Any]]:
        """Get basic lead info with audit URL for notifications."""
        with self._connect() as conn:
            row = conn.execute("""
                SELECT l.place_id, l.name, l.website, l.city, l.category, l.score, l.reasons,
                       a.audit_url
                FROM leads l
                LEFT JOIN audits a ON l.place_id = a.place_id
                WHERE l.place_id = ?
            """, (place_id,)).fetchone()
            return dict(row) if row else None

    def mark_exported(self, place_ids: List[str], tier: str = "basic"):
        """Mark leads as exported for a given tier."""
        now = datetime.utcnow()
        column = "exported_pro_at" if (tier or "").lower() == "pro" else "exported_basic_at"
        with self._connect() as conn:
            conn.executemany(
                f"UPDATE leads SET exported_count = exported_count + 1, last_exported = ?, {column} = ? WHERE place_id = ?",
                [(now, now, pid) for pid in place_ids]
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

    def record_export(
        self,
        run_id: str,
        subscriber_email: str,
        lead_count: int,
        csv_path: str,
        tier: str = None,
        export_type: str = None,
    ):
        """Record an export to a subscriber."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO exports (run_id, subscriber_email, lead_count, csv_path, sent_at, tier, export_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (run_id, subscriber_email, lead_count, csv_path, datetime.utcnow(), tier, export_type))

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

    # ---- Audit Methods ----

    def record_audit(self, place_id: str, audit_url: str, audit_html_path: str, issues_json: str):
        """Record a generated audit for a lead."""
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO audits (place_id, audit_url, audit_html_path, generated_at, issues_json)
                VALUES (?, ?, ?, ?, ?)
            """, (place_id, audit_url, audit_html_path, datetime.utcnow(), issues_json))

    def get_leads_without_audits(self, min_score: int) -> List[Dict[str, Any]]:
        """Get leads with score >= min_score that don't have an audit yet."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT l.place_id, l.name, l.website, l.score, l.reasons
                FROM leads l
                LEFT JOIN audits a ON l.place_id = a.place_id
                WHERE l.score >= ? AND l.website IS NOT NULL AND a.place_id IS NULL
                ORDER BY l.score DESC
            """, (min_score,)).fetchall()

            return [dict(row) for row in rows]

    def get_audit_url(self, place_id: str) -> Optional[str]:
        """Get the audit URL for a lead."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT audit_url FROM audits WHERE place_id = ?",
                (place_id,)
            ).fetchone()
            return row["audit_url"] if row else None

    # ---- Contact Methods ----

    def record_contact(self, place_id: str, email: str, source: str, confidence: float):
        """Record contact information for a lead."""
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO contacts (place_id, email, source, confidence, found_at)
                VALUES (?, ?, ?, ?, ?)
            """, (place_id, email, source, confidence, datetime.utcnow()))

    def get_contact(self, place_id: str) -> Optional[Dict[str, Any]]:
        """Get contact information for a lead."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT place_id, email, source, confidence, found_at FROM contacts WHERE place_id = ?",
                (place_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_leads_without_contacts(self) -> List[Dict[str, Any]]:
        """Get leads that have a website but no contact record."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT l.place_id, l.name, l.website, l.score
                FROM leads l
                LEFT JOIN contacts c ON l.place_id = c.place_id
                WHERE l.website IS NOT NULL AND c.place_id IS NULL
                ORDER BY l.score DESC
            """).fetchall()

            return [dict(row) for row in rows]

    # ---- Outreach Methods ----

    def record_outreach(self, place_id: str, email: str, audit_url: str, success: bool, error: str = None):
        """Record an outreach attempt."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO outreach (place_id, email, audit_url, sent_at, success, error)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (place_id, email, audit_url, datetime.utcnow(), success, error))

    def record_followup(self, place_id: str, success: bool, error: str = None):
        """Record follow-up attempt for an outreach row."""
        with self._connect() as conn:
            conn.execute("""
                UPDATE outreach SET followup_sent_at = ?, followup_success = ?, followup_error = ?
                WHERE place_id = ?
            """, (datetime.utcnow(), success, error, place_id))

    def get_leads_ready_for_outreach(self, min_score: int) -> List[Dict[str, Any]]:
        """
        Get leads that have audit + contact but haven't been contacted and aren't unsubscribed.
        Only returns leads with confidence >= 0.7.
        """
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT l.place_id, l.name, l.website, l.score,
                       c.email, c.confidence,
                       a.audit_url
                FROM leads l
                INNER JOIN audits a ON l.place_id = a.place_id
                INNER JOIN contacts c ON l.place_id = c.place_id
                LEFT JOIN outreach o ON l.place_id = o.place_id
                LEFT JOIN unsubscribes u ON l.place_id = u.place_id
                WHERE l.score >= ?
                  AND c.confidence >= 0.7
                  AND o.place_id IS NULL
                  AND u.place_id IS NULL
                ORDER BY l.score DESC, c.confidence DESC
            """, (min_score,)).fetchall()

            return [dict(row) for row in rows]

    def get_leads_for_followup(self, min_days_since_sent: int = 3) -> List[Dict[str, Any]]:
        """Get leads eligible for follow-up (no engagement, no unsubscribe)."""
        cutoff = datetime.utcnow() - timedelta(days=min_days_since_sent)
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT l.place_id, l.name, l.website, l.score,
                       c.email, c.confidence,
                       a.audit_url, o.sent_at
                FROM leads l
                INNER JOIN outreach o ON l.place_id = o.place_id
                INNER JOIN contacts c ON l.place_id = c.place_id
                INNER JOIN audits a ON l.place_id = a.place_id
                LEFT JOIN unsubscribes u ON l.place_id = u.place_id
                LEFT JOIN engagement_events e ON l.place_id = e.place_id
                WHERE o.success = 1
                  AND o.followup_sent_at IS NULL
                  AND o.sent_at <= ?
                  AND u.place_id IS NULL
                  AND e.place_id IS NULL
            """, (cutoff,)).fetchall()
            return [dict(row) for row in rows]

    def has_been_contacted(self, place_id: str) -> bool:
        """Check if a lead has been contacted."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM outreach WHERE place_id = ?",
                (place_id,)
            ).fetchone()
            return row is not None

    # ---- Engagement Methods ----

    def record_event(self, place_id: str, event_type: str, ip_address: str = None, user_agent: str = None):
        """Record an engagement event."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO engagement_events (place_id, event_type, ip_address, user_agent, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (place_id, event_type, ip_address, user_agent, datetime.utcnow()))

    def get_events_for_lead(self, place_id: str) -> List[Dict[str, Any]]:
        """Get all engagement events for a lead."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT event_type, ip_address, user_agent, timestamp
                FROM engagement_events
                WHERE place_id = ?
                ORDER BY timestamp DESC
            """, (place_id,)).fetchall()

            return [dict(row) for row in rows]

    def get_engagement_score(self, place_id: str) -> int:
        """
        Calculate engagement score for a lead.
        Weights: email_opened=5, page_view=25, cta_click=50, unsubscribe=-100
        """
        with self._connect() as conn:
            # Check if unsubscribed first
            unsubscribed = conn.execute(
                "SELECT 1 FROM unsubscribes WHERE place_id = ?",
                (place_id,)
            ).fetchone()
            if unsubscribed:
                return -100

            # Count event types
            rows = conn.execute("""
                SELECT event_type, COUNT(*) as count
                FROM engagement_events
                WHERE place_id = ?
                GROUP BY event_type
            """, (place_id,)).fetchall()

            score = 0
            for row in rows:
                event_type = row["event_type"]
                count = row["count"]
                if event_type == "email_opened":
                    score += 5 * count
                elif event_type == "page_view":
                    score += 25 * count
                elif event_type == "cta_click":
                    score += 50 * count

            return score

    # ---- Unsubscribe Methods ----

    def add_unsubscribe(self, place_id: str, email: str):
        """Add a lead to the unsubscribe list."""
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO unsubscribes (place_id, email, unsubscribed_at)
                VALUES (?, ?, ?)
            """, (place_id, email, datetime.utcnow()))

    def is_unsubscribed(self, place_id: str) -> bool:
        """Check if a lead is unsubscribed."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM unsubscribes WHERE place_id = ?",
                (place_id,)
            ).fetchone()
            return row is not None

    # ---- Suppression Methods ----

    def add_suppression(self, email: str, reason: str):
        """Add an email to suppression list."""
        if not email:
            return
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO suppression (email, reason, suppressed_at)
                VALUES (?, ?, ?)
            """, (email, reason, datetime.utcnow()))

    def is_suppressed(self, email: str) -> bool:
        """Check if an email is suppressed."""
        if not email:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM suppression WHERE email = ?",
                (email,)
            ).fetchone()
            return row is not None

    # ---- Inquiry Methods ----

    def record_lead_inquiry(
        self,
        place_id: str,
        name: str,
        email: str,
        phone: str,
        notes: str,
    ):
        """Record CTA form submission."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO lead_inquiries (place_id, name, email, phone, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (place_id, name, email, phone, notes, datetime.utcnow()))

    # ---- Export Query Methods ----

    def get_recent_exports(self, subscriber_email: str, limit: int = 4) -> List[Dict[str, Any]]:
        """Get recent exports for a subscriber."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT subscriber_email, lead_count, csv_path, sent_at, tier, export_type
                FROM exports
                WHERE subscriber_email = ?
                ORDER BY sent_at DESC
                LIMIT ?
            """, (subscriber_email, limit)).fetchall()
            return [dict(row) for row in rows]

    def get_latest_warm_export(self, subscriber_email: str) -> Optional[Dict[str, Any]]:
        """Get the most recent warm export for a subscriber."""
        with self._connect() as conn:
            row = conn.execute("""
                SELECT lead_count, sent_at
                FROM exports
                WHERE subscriber_email = ? AND export_type = 'warm'
                ORDER BY sent_at DESC
                LIMIT 1
            """, (subscriber_email,)).fetchone()
            return dict(row) if row else None

    # ---- Warm Lead Query ----

    def get_warm_leads(self, min_engagement_score: int = 25) -> List[Dict[str, Any]]:
        """
        Get leads with engagement score >= threshold, excluding unsubscribed.
        Returns leads with their engagement scores.
        """
        with self._connect() as conn:
            # Get all leads that have been contacted and have events
            rows = conn.execute("""
                SELECT DISTINCT l.place_id, l.name, l.website, l.address, l.phone,
                       l.review_count, l.city, l.category, l.score, l.reasons, l.lead_tier,
                       l.exclusive_until,
                       c.email, a.audit_url
                FROM leads l
                INNER JOIN outreach o ON l.place_id = o.place_id
                INNER JOIN contacts c ON l.place_id = c.place_id
                INNER JOIN audits a ON l.place_id = a.place_id
                LEFT JOIN unsubscribes u ON l.place_id = u.place_id
                WHERE o.success = 1
                  AND u.place_id IS NULL
            """).fetchall()

            # Calculate engagement scores and filter
            warm_leads = []
            for row in rows:
                place_id = row["place_id"]
                engagement_score = self.get_engagement_score(place_id)
                if engagement_score >= min_engagement_score:
                    lead_dict = dict(row)
                    lead_dict["engagement_score"] = engagement_score
                    warm_leads.append(lead_dict)

            # Sort by engagement score descending
            warm_leads.sort(key=lambda x: x["engagement_score"], reverse=True)
            return warm_leads
