"""
Shared pytest fixtures for BrokenSite-Weekly tests.
"""

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from src.config import (
    Config,
    ScoringConfig,
    ScraperConfig,
    DatabaseConfig,
    RetryConfig,
    GumroadConfig,
    SMTPConfig,
)
from src.db import Database


@pytest.fixture
def scoring_config() -> ScoringConfig:
    """Default scoring configuration for tests."""
    return ScoringConfig()


@pytest.fixture
def scraper_config() -> ScraperConfig:
    """Default scraper configuration for tests."""
    return ScraperConfig(
        headless=True,
        timeout_ms=5000,  # Shorter timeout for tests
        max_results_per_query=5,
    )


@pytest.fixture
def retry_config() -> RetryConfig:
    """Minimal retry configuration for tests."""
    return RetryConfig(
        max_retries=1,
        base_delay_seconds=0.1,
        max_delay_seconds=1.0,
    )


@pytest.fixture
def test_db_path(tmp_path: Path) -> Path:
    """Temporary database path for tests."""
    return tmp_path / "test_leads.db"


@pytest.fixture
def database_config(test_db_path: Path) -> DatabaseConfig:
    """Database configuration pointing to temp database."""
    return DatabaseConfig(
        db_path=test_db_path,
        dedupe_window_days=90,
    )


@pytest.fixture
def test_database(database_config: DatabaseConfig) -> Generator[Database, None, None]:
    """Initialized test database."""
    db = Database(database_config)
    yield db
    # Cleanup is automatic via tmp_path fixture


@pytest.fixture
def mock_config(
    scoring_config: ScoringConfig,
    scraper_config: ScraperConfig,
    database_config: DatabaseConfig,
    retry_config: RetryConfig,
) -> Config:
    """Full mock configuration for tests."""
    return Config(
        scraper=scraper_config,
        scoring=scoring_config,
        database=database_config,
        retry=retry_config,
        gumroad=GumroadConfig(access_token="test_token", product_id="test_product"),
        smtp=SMTPConfig(
            host="localhost",
            port=1025,
            username="test",
            password="test",
            from_email="test@example.com",
        ),
        search_queries=["plumber"],
        target_cities=["Test City, TX"],
    )


@pytest.fixture
def sample_html_unreachable() -> str:
    """Sample HTML that would indicate an unreachable site."""
    return ""


@pytest.fixture
def sample_html_parked() -> str:
    """Sample HTML for a parked domain."""
    return """
    <!DOCTYPE html>
    <html>
    <head><title>Domain For Sale</title></head>
    <body>
        <h1>This domain is for sale</h1>
        <p>Contact us to purchase this premium domain name.</p>
    </body>
    </html>
    """


@pytest.fixture
def sample_html_outdated() -> str:
    """Sample HTML for an outdated website.
    Uses literal \u00a9 character since scoring.py matches raw HTML, not decoded entities.
    """
    return """
    <!DOCTYPE html>
    <html>
    <head><title>Joe's Plumbing</title></head>
    <body>
        <h1>Welcome to Joe's Plumbing</h1>
        <p>We fix pipes!</p>
        <footer>
            <p>\u00a9 2018 Joe's Plumbing. All Rights Reserved.</p>
        </footer>
    </body>
    </html>
    """


@pytest.fixture
def sample_html_wix() -> str:
    """Sample HTML for a Wix website."""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>My Business</title>
    </head>
    <body>
        <div id="SITE_CONTAINER">
            <script src="https://static.wix.com/main.js"></script>
        </div>
        <footer>\u00a9 2024 My Business</footer>
    </body>
    </html>
    """


@pytest.fixture
def sample_html_modern() -> str:
    """Sample HTML for a modern, well-maintained website."""
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Professional Plumbing Services</title>
        <link rel="stylesheet" href="styles.css">
    </head>
    <body>
        <header>
            <nav>
                <a href="/">Home</a>
                <a href="/services">Services</a>
                <a href="/contact">Contact</a>
            </nav>
        </header>
        <main>
            <h1>Professional Plumbing Services</h1>
            <p>24/7 emergency service available.</p>
        </main>
        <footer>
            <p>\u00a9 2024 Professional Plumbing. All Rights Reserved.</p>
        </footer>
    </body>
    </html>
    """


@pytest.fixture
def sample_html_no_viewport() -> str:
    """Sample HTML without viewport meta tag."""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Old Business Site</title>
    </head>
    <body>
        <h1>Welcome</h1>
        <table width="800">
            <tr><td>Content here</td></tr>
        </table>
        <footer>\u00a9 2024 Business</footer>
    </body>
    </html>
    """


@pytest.fixture
def sample_html_ssl_error() -> str:
    """Placeholder - SSL errors are detected at fetch level, not HTML."""
    return ""


@pytest.fixture
def sample_html_flash() -> str:
    """Sample HTML with Flash content."""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Flash Site</title>
    </head>
    <body>
        <object type="application/x-shockwave-flash" data="intro.swf">
            <param name="movie" value="intro.swf">
        </object>
        <footer>\u00a9 2024 Flash Site</footer>
    </body>
    </html>
    """
