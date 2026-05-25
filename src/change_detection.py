"""
Week-over-week change detection for BrokenSite-Weekly.

Compares the current run's leads against the previous run to surface:
  - New leads (first seen this run)
  - Score deltas (leads whose score changed significantly)
  - Disappeared leads (were in previous run, not in current run)
  - Tier changes (upgraded or downgraded between runs)

Results are stored in the DB and optionally included in CSV exports.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict

from .db import Database
from .logging_setup import get_logger

logger = get_logger("change_detection")

# Score delta threshold for flagging a "significant" change
DEFAULT_DELTA_THRESHOLD = 15

# DB table schema for change records
CHANGE_DETECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_deltas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    previous_run_id TEXT,
    place_id TEXT NOT NULL,
    change_type TEXT NOT NULL,  -- 'new', 'score_up', 'score_down', 'tier_up', 'tier_down', 'disappeared'
    current_score INTEGER,
    previous_score INTEGER,
    score_delta INTEGER,
    previous_tier TEXT,
    current_tier TEXT,
    reasons_current TEXT,
    reasons_previous TEXT,
    detected_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_deltas_run ON run_deltas(run_id);
CREATE INDEX IF NOT EXISTS idx_run_deltas_place ON run_deltas(place_id);
CREATE INDEX IF NOT EXISTS idx_run_deltas_type ON run_deltas(change_type);
"""


def _ensure_delta_schema(db: Database) -> None:
    """Create delta table if it doesn't exist."""
    try:
        with db._connect() as conn:
            conn.executescript(CHANGE_DETECTION_SCHEMA)
    except Exception as e:
        logger.warning(f"Failed to ensure delta schema: {e}")


def take_snapshot(db: Database) -> Dict[str, Dict[str, Any]]:
    """
    Take a snapshot of all current leads for later comparison.

    Returns a dict keyed by place_id with {score, reasons, lead_tier}.
    Call this BEFORE a scraping run, then pass to detect_changes after.
    """
    try:
        with db._connect() as conn:
            rows = conn.execute("""
                SELECT place_id, name, score, reasons, lead_tier, last_seen
                FROM leads
                WHERE website IS NOT NULL
                ORDER BY score DESC
            """).fetchall()
            return {row["place_id"]: dict(row) for row in rows}
    except Exception as e:
        logger.error(f"Failed to take snapshot: {e}")
        return {}


def detect_changes(
    db: Database,
    current_run_id: str,
    previous_snapshot: Optional[Dict[str, Dict[str, Any]]] = None,
    previous_run_id: Optional[str] = None,
    delta_threshold: int = DEFAULT_DELTA_THRESHOLD,
) -> Dict[str, Any]:
    """
    Compare current run leads against a previous snapshot and record deltas.

    Args:
        db: Database instance.
        current_run_id: ID of the current run.
        previous_snapshot: Dict of leads from before the run (from take_snapshot).
                           If None, attempts to auto-detect previous run and
                           use last_seen-based heuristics.
        previous_run_id: ID of previous run (auto-detected if None).
        delta_threshold: Minimum score change to flag.

    Returns:
        Dict with summary counts.
    """
    _ensure_delta_schema(db)

    # Auto-detect previous run ID
    if previous_run_id is None:
        prev = _get_previous_run_id(db, current_run_id)
        if prev is None:
            logger.info("No previous run found — skipping change detection")
            return {"skipped": True, "reason": "no_previous_run"}
        previous_run_id = prev

    now = datetime.utcnow()
    summary = {
        "new": 0,
        "score_up": 0,
        "score_down": 0,
        "tier_up": 0,
        "tier_down": 0,
        "disappeared": 0,
        "total_deltas": 0,
    }

    try:
        with db._connect() as conn:
            _ensure_delta_schema(db)

            # Current leads (after the run)
            current_leads = _get_all_leads(conn)
            current_by_place = {l["place_id"]: l for l in current_leads}

            # Previous leads — use snapshot if provided, else fall back to DB
            if previous_snapshot:
                previous_by_place = dict(previous_snapshot)
            else:
                logger.warning(
                    "No previous snapshot provided — change detection will have "
                    "reduced accuracy (comparing against current DB state)."
                )
                previous_by_place = dict(current_by_place)

            current_places = set(current_by_place.keys())
            previous_places = set(previous_by_place.keys())

            records: List[Tuple] = []

            # New leads (in current but not in previous)
            new_places = current_places - previous_places
            for place_id in new_places:
                lead = current_by_place[place_id]
                records.append((
                    current_run_id, previous_run_id, place_id, "new",
                    lead.get("score"), None, None,
                    None, lead.get("lead_tier"),
                    _serialize_reasons(lead.get("reasons")), None,
                    now,
                ))
                summary["new"] += 1

            # Disappeared leads (in previous but not in current)
            disappeared = previous_places - current_places
            for place_id in disappeared:
                lead = previous_by_place[place_id]
                records.append((
                    current_run_id, previous_run_id, place_id, "disappeared",
                    None, lead.get("score"), None,
                    lead.get("lead_tier"), None,
                    None, _serialize_reasons(lead.get("reasons")),
                    now,
                ))
                summary["disappeared"] += 1

            # Score and tier changes (in both runs)
            common = current_places & previous_places
            for place_id in common:
                curr = current_by_place[place_id]
                prev = previous_by_place[place_id]

                curr_score = int(curr.get("score") or 0)
                prev_score = int(prev.get("score") or 0)
                delta = curr_score - prev_score
                curr_tier = curr.get("lead_tier", "")
                prev_tier = prev.get("lead_tier", "")

                changed = False

                # Score change
                if abs(delta) >= delta_threshold:
                    change_type = "score_up" if delta > 0 else "score_down"
                    records.append((
                        current_run_id, previous_run_id, place_id, change_type,
                        curr_score, prev_score, delta,
                        prev_tier, curr_tier,
                        _serialize_reasons(curr.get("reasons")),
                        _serialize_reasons(prev.get("reasons")),
                        now,
                    ))
                    summary[change_type] += 1
                    changed = True

                # Tier change (even without large score delta)
                if curr_tier != prev_tier and not changed:
                    tier_rank = {"hot": 3, "warm": 2, "cool": 1, "skip": 0}
                    curr_rank = tier_rank.get(str(curr_tier).lower(), 0)
                    prev_rank = tier_rank.get(str(prev_tier).lower(), 0)
                    change_type = "tier_up" if curr_rank > prev_rank else "tier_down"
                    records.append((
                        current_run_id, previous_run_id, place_id, change_type,
                        curr_score, prev_score, delta,
                        prev_tier, curr_tier,
                        _serialize_reasons(curr.get("reasons")),
                        _serialize_reasons(prev.get("reasons")),
                        now,
                    ))
                    summary[change_type] += 1

            # Batch insert
            if records:
                conn.executemany("""
                    INSERT INTO run_deltas (
                        run_id, previous_run_id, place_id, change_type,
                        current_score, previous_score, score_delta,
                        previous_tier, current_tier,
                        reasons_current, reasons_previous,
                        detected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, records)

            summary["total_deltas"] = len(records)

    except Exception as e:
        logger.error(f"Change detection failed: {e}")
        summary["error"] = str(e)

    logger.info(
        f"Change detection: {summary.get('new',0)} new, "
        f"{summary.get('score_up',0)} score-up, {summary.get('score_down',0)} score-down, "
        f"{summary.get('tier_up',0)} tier-up, {summary.get('tier_down',0)} tier-down, "
        f"{summary.get('disappeared',0)} disappeared"
    )
    return summary


def get_deltas_for_run(
    db: Database,
    run_id: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Get all change records for a given run."""
    try:
        with db._connect() as conn:
            rows = conn.execute("""
                SELECT run_id, place_id, change_type, current_score, previous_score,
                       score_delta, previous_tier, current_tier,
                       reasons_current, reasons_previous, detected_at
                FROM run_deltas
                WHERE run_id = ?
                ORDER BY ABS(score_delta) DESC NULLS LAST, detected_at DESC
                LIMIT ?
            """, (run_id, limit)).fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Failed to get deltas for run {run_id}: {e}")
        return []


def get_lead_change_history(
    db: Database,
    place_id: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Get change history for a specific lead across runs."""
    try:
        with db._connect() as conn:
            rows = conn.execute("""
                SELECT run_id, change_type, current_score, previous_score,
                       score_delta, previous_tier, current_tier, detected_at
                FROM run_deltas
                WHERE place_id = ?
                ORDER BY detected_at DESC
                LIMIT ?
            """, (place_id, limit)).fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Failed to get change history for {place_id}: {e}")
        return []


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_previous_run_id(db: Database, current_run_id: str) -> Optional[str]:
    """Find the run_id of the most recent completed run before current_run_id."""
    try:
        with db._connect() as conn:
            row = conn.execute("""
                SELECT run_id FROM runs
                WHERE run_id != ? AND completed_at IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT 1
            """, (current_run_id,)).fetchone()
            return row["run_id"] if row else None
    except Exception:
        return None


def _get_all_leads(conn) -> List[Dict[str, Any]]:
    """Get all current leads from the database."""
    rows = conn.execute("""
        SELECT place_id, name, score, reasons, lead_tier, last_seen
        FROM leads
        WHERE website IS NOT NULL
        ORDER BY score DESC
    """).fetchall()
    return [dict(row) for row in rows]


def _serialize_reasons(reasons: Any) -> Optional[str]:
    if not reasons:
        return None
    if isinstance(reasons, str):
        return reasons
    if isinstance(reasons, (list, tuple)):
        return json.dumps([str(r) for r in reasons if str(r).strip()])
    return str(reasons)
