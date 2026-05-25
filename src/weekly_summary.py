"""
Weekly summary email generator for BrokenSite-Weekly.

Compiles aggregate statistics from the latest run into a human-readable
summary email that goes out alongside (or after) the CSV lead export.

Pulls data from:
  - Database stats (total leads, unique websites, etc.)
  - Market reports per city/niche
  - Run history (trending data)
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path
from collections import Counter

from .config import SMTPConfig, RetryConfig, OUTPUT_DIR
from .db import Database
from .logging_setup import get_logger

logger = get_logger("weekly_summary")


def build_summary_text(
    db: Database,
    market_report_paths: Optional[List[str]] = None,
    min_score: int = 40,
) -> str:
    """
    Build the weekly summary email body from database stats and market reports.

    Args:
        db: Database instance for querying stats.
        market_report_paths: Optional list of market report file paths.
        min_score: Minimum score threshold used for the run.

    Returns:
        Plain text email body.
    """
    stats = db.get_stats()
    last_completed = stats.get("last_completed_run") or {}
    total_leads = stats.get("total_leads", 0)
    total_runs = stats.get("total_runs", 0)

    # Top reasons from qualifying leads (approximate from top yield)
    top_combos = db.get_top_yield_city_categories(min_score_for_quality=min_score, limit=5)

    lines = [
        "BrokenSite Weekly — Run Summary",
        "=" * 40,
        "",
        f"Run completed: {_format_timestamp(last_completed.get('completed_at'))}",
        f"Total leads in database: {total_leads}",
        f"Total runs completed: {total_runs}",
        "",
    ]

    # Latest run stats
    if last_completed:
        lines.append("Latest Run Details")
        lines.append("-" * 30)
        lines.append(f"  Queries attempted: {last_completed.get('queries_attempted', 0)}")
        lines.append(f"  Businesses found: {last_completed.get('businesses_found', 0)}")
        lines.append(f"  Leads exported: {last_completed.get('leads_exported', 0)}")
        lines.append(f"  Emails sent: {last_completed.get('emails_sent', 0)}")

        started = last_completed.get("started_at")
        completed = last_completed.get("completed_at")
        if started and completed:
            try:
                start_dt = _parse_ts(started)
                end_dt = _parse_ts(completed)
                duration_min = round((end_dt - start_dt).total_seconds() / 60, 1)
                lines.append(f"  Duration: {duration_min} minutes")
            except Exception:
                pass
        lines.append("")

    # Top city/category combos
    if top_combos:
        lines.append("Top City / Niche Yield (quality-first)")
        lines.append("-" * 30)
        for combo in top_combos[:5]:
            lines.append(
                f"  {combo['city']} / {combo['category']}: "
                f"{combo['lead_count']} leads, "
                f"{combo['quality_lead_count']} quality (score>={min_score}), "
                f"avg score {combo['avg_score']}"
            )
        lines.append("")

    # Market report highlights
    if market_report_paths:
        lines.append("Market Report Highlights")
        lines.append("-" * 30)
        for report_path in market_report_paths[:5]:
            try:
                text = Path(report_path).read_text(encoding="utf-8")
                # Extract first 3 non-empty lines after the header
                report_lines = [l for l in text.split("\n") if l.strip() and not l.startswith("━")]
                for rl in report_lines[1:4]:  # skip title, show 3 key stats
                    lines.append(f"  {rl.strip()}")
                lines.append("")
            except Exception:
                pass
        if len(market_report_paths) > 5:
            lines.append(f"  ... and {len(market_report_paths) - 5} more market reports")
        lines.append("")

    # Tips
    lines.extend([
        "Quick Tips",
        "-" * 30,
        "• Focus on Hot leads (score 80+) first — sites that are down or returning errors.",
        "• Warm leads (score 60-79) have clear issues like outdated tech or no mobile support.",
        "• Leads with marketing pixels (GTM, FB) are already spending on ads — hotter prospects.",
        "• Check the 'suggested_pitch' column for an opening line tailored to each lead.",
        "",
        "Happy prospecting!",
        "",
        "---",
        "BrokenSite Weekly",
    ])

    return "\n".join(lines)


def write_summary_to_file(
    summary_text: str,
    output_dir: Optional[Path] = None,
    date_str: Optional[str] = None,
) -> Path:
    """Write summary text to a file and return the path."""
    output_dir = output_dir or OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = date_str or datetime.utcnow().strftime("%Y-%m-%d")
    path = output_dir / f"weekly_summary_{date_str}.txt"
    path.write_text(summary_text, encoding="utf-8")
    logger.info(f"Weekly summary written: {path}")
    return path


def generate_weekly_summary(
    db: Database,
    market_report_paths: Optional[List[str]] = None,
    min_score: int = 40,
) -> Tuple[str, Optional[Path]]:
    """
    Generate the weekly summary text and write it to a file.

    Returns (summary_text, file_path).
    """
    try:
        text = build_summary_text(db, market_report_paths, min_score)
        path = write_summary_to_file(text)
        return text, path
    except Exception as e:
        logger.error(f"Failed to generate weekly summary: {e}")
        return f"Weekly summary generation failed: {e}", None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _format_timestamp(ts: Any) -> str:
    if not ts:
        return "N/A"
    try:
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        elif isinstance(ts, datetime):
            dt = ts
        else:
            return str(ts)
        return dt.strftime("%B %d, %Y at %H:%M UTC")
    except Exception:
        return str(ts)


def _parse_ts(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str):
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    raise ValueError(f"Cannot parse timestamp: {ts}")
