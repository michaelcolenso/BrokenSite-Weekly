#!/usr/bin/env python3
"""
Health check module for BrokenSite-Weekly.

Provides health status for container orchestration and monitoring.
Can be run as a standalone script or imported for programmatic use.
"""

import sys
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List
from dataclasses import dataclass

from .config import load_config, DATA_DIR, LOG_DIR, OUTPUT_DIR


@dataclass
class HealthCheckResult:
    """Result of a health check."""
    name: str
    healthy: bool
    message: str
    details: Dict[str, Any] = None


def check_database() -> HealthCheckResult:
    """Check database connectivity and basic integrity."""
    db_path = DATA_DIR / "leads.db"

    if not db_path.exists():
        return HealthCheckResult(
            name="database",
            healthy=True,
            message="Database not yet created (first run pending)",
        )

    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row

        # Test basic query
        result = conn.execute("SELECT COUNT(*) as count FROM leads").fetchone()
        lead_count = result["count"]

        # Check last run status
        last_run = conn.execute("""
            SELECT run_id, status, completed_at
            FROM runs
            ORDER BY started_at DESC
            LIMIT 1
        """).fetchone()

        conn.close()

        details = {"lead_count": lead_count}
        if last_run:
            details["last_run_id"] = last_run["run_id"]
            details["last_run_status"] = last_run["status"]
            details["last_run_completed"] = last_run["completed_at"]

        return HealthCheckResult(
            name="database",
            healthy=True,
            message=f"Database OK, {lead_count} leads stored",
            details=details,
        )

    except sqlite3.Error as e:
        return HealthCheckResult(
            name="database",
            healthy=False,
            message=f"Database error: {e}",
        )
    except Exception as e:
        return HealthCheckResult(
            name="database",
            healthy=False,
            message=f"Unexpected error: {e}",
        )


def check_directories() -> HealthCheckResult:
    """Check required directories exist and are writable."""
    dirs_to_check = [
        ("data", DATA_DIR),
        ("logs", LOG_DIR),
        ("output", OUTPUT_DIR),
    ]

    issues = []
    for name, path in dirs_to_check:
        if not path.exists():
            try:
                path.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                issues.append(f"{name}: cannot create ({e})")
                continue

        # Check writable
        test_file = path / ".write_test"
        try:
            test_file.touch()
            test_file.unlink()
        except Exception as e:
            issues.append(f"{name}: not writable ({e})")

    if issues:
        return HealthCheckResult(
            name="directories",
            healthy=False,
            message=f"Directory issues: {'; '.join(issues)}",
        )

    return HealthCheckResult(
        name="directories",
        healthy=True,
        message="All directories OK and writable",
    )


def check_config() -> HealthCheckResult:
    """Check configuration validity (without exposing secrets)."""
    try:
        from .config import validate_config

        config = load_config()
        errors = validate_config(config)

        if errors:
            return HealthCheckResult(
                name="config",
                healthy=False,
                message=f"Config errors: {len(errors)} issues",
                details={"errors": errors},
            )

        return HealthCheckResult(
            name="config",
            healthy=True,
            message="Configuration valid",
            details={
                "target_cities": len(config.target_cities),
                "search_queries": len(config.search_queries),
            },
        )

    except Exception as e:
        return HealthCheckResult(
            name="config",
            healthy=False,
            message=f"Config load error: {e}",
        )


def check_last_run_age() -> HealthCheckResult:
    """Check if the last successful run was within expected window."""
    db_path = DATA_DIR / "leads.db"

    if not db_path.exists():
        return HealthCheckResult(
            name="last_run_age",
            healthy=True,
            message="No runs yet (first run pending)",
        )

    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row

        # Get last successful run
        last_run = conn.execute("""
            SELECT run_id, completed_at
            FROM runs
            WHERE status = 'completed'
            ORDER BY completed_at DESC
            LIMIT 1
        """).fetchone()
        conn.close()

        if not last_run:
            return HealthCheckResult(
                name="last_run_age",
                healthy=True,
                message="No completed runs yet",
            )

        # Parse completion time
        completed_str = last_run["completed_at"]
        if isinstance(completed_str, str):
            completed_at = datetime.fromisoformat(completed_str.replace("Z", "+00:00"))
        else:
            completed_at = completed_str

        age = datetime.utcnow() - completed_at.replace(tzinfo=None)

        # Warn if last run was more than 8 days ago (should run weekly)
        if age > timedelta(days=8):
            return HealthCheckResult(
                name="last_run_age",
                healthy=False,
                message=f"Last run was {age.days} days ago (expected weekly)",
                details={"last_run_id": last_run["run_id"], "age_days": age.days},
            )

        return HealthCheckResult(
            name="last_run_age",
            healthy=True,
            message=f"Last run was {age.days} days ago",
            details={"last_run_id": last_run["run_id"], "age_days": age.days},
        )

    except Exception as e:
        return HealthCheckResult(
            name="last_run_age",
            healthy=False,
            message=f"Error checking run age: {e}",
        )


def run_all_checks() -> tuple[bool, List[HealthCheckResult]]:
    """
    Run all health checks.

    Returns:
        tuple[bool, List[HealthCheckResult]]: (all_healthy, results)
    """
    checks = [
        check_directories,
        check_database,
        check_config,
        check_last_run_age,
    ]

    results = []
    for check_fn in checks:
        try:
            result = check_fn()
            results.append(result)
        except Exception as e:
            results.append(HealthCheckResult(
                name=check_fn.__name__.replace("check_", ""),
                healthy=False,
                message=f"Check failed: {e}",
            ))

    all_healthy = all(r.healthy for r in results)
    return all_healthy, results


def main():
    """CLI entry point for health checks."""
    all_healthy, results = run_all_checks()

    for result in results:
        status = "OK" if result.healthy else "FAIL"
        print(f"[{status}] {result.name}: {result.message}")

    if all_healthy:
        print("\nOverall: Healthy")
        sys.exit(0)
    else:
        print("\nOverall: Unhealthy")
        sys.exit(1)


if __name__ == "__main__":
    main()
