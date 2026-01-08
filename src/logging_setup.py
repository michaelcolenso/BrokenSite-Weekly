"""
Logging configuration for BrokenSite-Weekly.
Provides structured logging with rotation for unattended operation.
"""

import logging
import logging.handlers
import sys
from pathlib import Path
from datetime import datetime

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
        self.stats = {
            "queries_attempted": 0,
            "queries_succeeded": 0,
            "businesses_found": 0,
            "websites_checked": 0,
            "leads_exported": 0,
            "emails_sent": 0,
            "errors": 0,
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
        if stat in self.stats:
            self.stats[stat] += amount
