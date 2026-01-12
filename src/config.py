"""
Configuration management for BrokenSite-Weekly.
Loads from environment variables with sensible defaults.
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import List

# Base paths
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
OUTPUT_DIR = PROJECT_ROOT / "output"
DEBUG_DIR = PROJECT_ROOT / "debug"


@dataclass
class ScraperConfig:
    """Playwright Maps scraper configuration."""
    headless: bool = True
    timeout_ms: int = 30000
    scroll_pause_ms: int = 1500
    max_scrolls: int = 15
    max_results_per_query: int = 50
    screenshot_on_failure: bool = True
    html_dump_on_failure: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )


@dataclass
class ScoringConfig:
    """Website scoring thresholds and weights."""
    # Hard failures (unreachable, server errors, parked)
    weight_unreachable: int = 100
    weight_timeout: int = 90
    weight_5xx_error: int = 85
    weight_ssl_error: int = 80
    weight_parked_domain: int = 75
    weight_4xx_error: int = 40
    weight_403_404: int = 50

    # Medium signals (outdated tech, poor mobile)
    weight_http_only: int = 30
    weight_outdated_copyright: int = 25  # Copyright year > 2 years old
    weight_flash_detected: int = 40
    weight_missing_viewport: int = 20
    weight_missing_responsive: int = 15

    # Weak signals (DIY builders - low weight to avoid false positives)
    weight_wix: int = 5
    weight_squarespace: int = 5
    weight_weebly: int = 8
    weight_godaddy_builder: int = 10

    # Thresholds
    min_score_to_include: int = 40  # Only include leads scoring >= this
    request_timeout_seconds: int = 15
    max_redirects: int = 5


@dataclass
class GumroadConfig:
    """Gumroad API configuration."""
    access_token: str = field(default_factory=lambda: os.environ.get("GUMROAD_ACCESS_TOKEN", ""))
    product_id: str = field(default_factory=lambda: os.environ.get("GUMROAD_PRODUCT_ID", ""))
    api_base_url: str = "https://api.gumroad.com/v2"


@dataclass
class SMTPConfig:
    """SMTP delivery configuration."""
    host: str = field(default_factory=lambda: os.environ.get("SMTP_HOST", "smtp.gmail.com"))
    port: int = field(default_factory=lambda: int(os.environ.get("SMTP_PORT", "587")))
    username: str = field(default_factory=lambda: os.environ.get("SMTP_USERNAME", ""))
    password: str = field(default_factory=lambda: os.environ.get("SMTP_PASSWORD", ""))
    from_email: str = field(default_factory=lambda: os.environ.get("SMTP_FROM_EMAIL", ""))
    from_name: str = field(default_factory=lambda: os.environ.get("SMTP_FROM_NAME", "BrokenSite Weekly"))
    use_tls: bool = True


@dataclass
class RetryConfig:
    """Retry and backoff configuration."""
    max_retries: int = 3
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True


@dataclass
class DatabaseConfig:
    """SQLite database configuration."""
    db_path: Path = field(default_factory=lambda: DATA_DIR / "leads.db")
    dedupe_window_days: int = 90  # Don't re-contact leads within this window


@dataclass
class Config:
    """Main configuration container."""
    scraper: ScraperConfig = field(default_factory=ScraperConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    gumroad: GumroadConfig = field(default_factory=GumroadConfig)
    smtp: SMTPConfig = field(default_factory=SMTPConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)

    # Search queries - niches to target
    search_queries: List[str] = field(default_factory=lambda: [
        "plumber near me",
        "electrician near me",
        "hvac repair near me",
        "roofing contractor near me",
        "landscaping service near me",
        "auto repair shop near me",
        "dentist near me",
        "chiropractor near me",
        "hair salon near me",
        "restaurant near me",
    ])

    # Target cities for geographic rotation
    target_cities: List[str] = field(default_factory=lambda: [
        "Austin, TX",
        "Denver, CO",
        "Phoenix, AZ",
        "Nashville, TN",
        "Charlotte, NC",
        "Portland, OR",
        "San Antonio, TX",
        "Jacksonville, FL",
        "Columbus, OH",
        "Indianapolis, IN",
    ])


def load_config() -> Config:
    """Load configuration from environment variables."""
    # Ensure directories exist
    for directory in [DATA_DIR, LOG_DIR, OUTPUT_DIR, DEBUG_DIR]:
        directory.mkdir(parents=True, exist_ok=True)

    return Config()


def validate_config(config: Config) -> List[str]:
    """Validate configuration and return list of errors."""
    errors = []

    if not config.gumroad.access_token:
        errors.append("GUMROAD_ACCESS_TOKEN environment variable not set")
    if not config.gumroad.product_id:
        errors.append("GUMROAD_PRODUCT_ID environment variable not set")
    if not config.smtp.username:
        errors.append("SMTP_USERNAME environment variable not set")
    if not config.smtp.password:
        errors.append("SMTP_PASSWORD environment variable not set")
    if not config.smtp.from_email:
        errors.append("SMTP_FROM_EMAIL environment variable not set")

    return errors
