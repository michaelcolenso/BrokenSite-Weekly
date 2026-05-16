"""
Logging configuration for BrokenSite-Weekly.
Provides structured logging with rotation for unattended operation.
"""

import logging
import logging.handlers
import sys
from pathlib import Path
from datetime import datetime
from time import perf_counter
import threading

from .config import LOG_DIR


def setup_logging(
    name: str = "brokensite",
    level: int = logging.INFO,
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5,
) -> logging.Logger:
    """
    Configure logging with both file and console output.

    File logs rotate at max_bytes, keeping backup_count old files.
    Console output goes to stderr for systemd journal capture.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Avoid duplicate handlers on repeated calls
    if logger.handlers:
        return logger

    # Format: timestamp, level, module, message
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s.%(funcName)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Rotating file handler
    log_file = LOG_DIR / "brokensite-weekly.log"
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Console handler (stderr for systemd)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


def get_logger(module_name: str) -> logging.Logger:
    """Get a child logger for a specific module."""
    return logging.getLogger(f"brokensite.{module_name}")


class RunContext:
    """
    Context manager for tracking a weekly run.
    Logs start/end times and provides a run_id for correlation.
    """

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        self.start_time = None
        self._lock = threading.Lock()
        self._active_phase_start: dict[str, float] = {}
        self.stats = {
            "queries_attempted": 0,
            "queries_succeeded": 0,
            "businesses_found": 0,
            "websites_checked": 0,
            "qualifying_leads": 0,
            "leads_exported": 0,
            "emails_sent": 0,
            "manual_review_leads": 0,
            "audits_generated": 0,
            "contacts_found": 0,
            "outreach_sent": 0,
            "followups_sent": 0,
            "errors": 0,
            "phase_durations_seconds": {},
            "reason_counts": {},
        }

    def __enter__(self):
        self.start_time = datetime.utcnow()
        self.logger.info(f"=== Weekly run started: {self.run_id} ===")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = datetime.utcnow() - self.start_time
        self.logger.info(
            f"=== Weekly run completed: {self.run_id} | "
            f"Duration: {duration} | "
            f"Stats: {self.stats} ==="
        )
        if exc_type:
            self.logger.error(f"Run failed with exception: {exc_type.__name__}: {exc_val}")
        return False  # Don't suppress exceptions

    def increment(self, stat: str, amount: int = 1):
        """Increment a stat counter."""
        with self._lock:
            if stat in self.stats:
                self.stats[stat] += amount

    def start_phase(self, phase_name: str):
        """Start a phase timer."""
        with self._lock:
            self._active_phase_start[phase_name] = perf_counter()

    def end_phase(self, phase_name: str) -> float:
        """Stop a phase timer and store cumulative duration in stats."""
        with self._lock:
            start = self._active_phase_start.pop(phase_name, None)
            if start is None:
                return 0.0
            duration = perf_counter() - start
            phase_durations = self.stats.setdefault("phase_durations_seconds", {})
            phase_durations[phase_name] = phase_durations.get(phase_name, 0.0) + duration
            return duration

    def count_reasons(self, reasons: list[str]):
        """Track frequency histogram for scoring reasons."""
        if not reasons:
            return
        with self._lock:
            reason_counts = self.stats.setdefault("reason_counts", {})
            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
