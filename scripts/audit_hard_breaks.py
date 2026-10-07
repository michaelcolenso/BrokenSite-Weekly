#!/usr/bin/env python3
"""Read-only hard-break audit of data/leads.db (no Maps, no writes)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.lead_utils import is_hard_break

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "leads.db"


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT name, website, city, category, score, reasons FROM leads"))
    total = len(rows)
    ge40 = sum(1 for r in rows if r["score"] >= 40)
    hard = [r for r in rows if is_hard_break(r["reasons"])]
    high_soft = [r for r in rows if r["score"] >= 80 and not is_hard_break(r["reasons"])]
    print(f"total={total} score>=40={ge40} hard_break={len(hard)} high_score_not_hard={len(high_soft)}")
    print("--- hard-break (top 20 by score) ---")
    for row in sorted(hard, key=lambda r: -r["score"])[:20]:
        print(f"  {row['score']:3d} | {row['city']} | {row['category']} | {row['name']}")
    print("--- high score NOT hard-break ---")
    for row in sorted(high_soft, key=lambda r: -r["score"])[:20]:
        print(f"  {row['score']:3d} | {row['name']} | {row['reasons'][:80]}")


if __name__ == "__main__":
    main()
