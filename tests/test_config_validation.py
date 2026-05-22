"""Tests for mode-aware configuration validation."""

import json

from src.config import Config, validate_config
from src.run_weekly import _build_validation_requirements


def test_validate_config_can_skip_all_external_requirements():
    config = Config()
    errors = validate_config(
        config,
        require_gumroad=False,
        require_smtp=False,
        require_outreach=False,
        require_portal=False,
    )
    assert errors == []


def test_validate_config_requires_outreach_fields_when_enabled_for_mode():
    config = Config()
    errors = validate_config(
        config,
        require_gumroad=False,
        require_smtp=False,
        require_outreach=True,
        require_portal=False,
    )
    assert "TRACKING_BASE_URL environment variable not set (required for outreach)" in errors
    assert "OUTREACH_PHYSICAL_ADDRESS environment variable not set (required for CAN-SPAM compliance)" in errors


def test_build_validation_requirements_for_scrape_only_no_outreach_dry_run():
    config = Config()
    requirements = _build_validation_requirements(
        config,
        skip_delivery=True,
        skip_outreach=True,
        dry_run=True,
    )
    assert requirements == {
        "require_gumroad": False,
        "require_smtp": False,
        "require_outreach": False,
        "require_portal": False,
    }


def test_full_run_does_not_require_outreach_by_default(monkeypatch):
    monkeypatch.delenv("OUTREACH_ENABLED", raising=False)
    config = Config()

    requirements = _build_validation_requirements(
        config,
        skip_delivery=False,
        skip_outreach=False,
        dry_run=False,
    )

    assert config.outreach.enabled is False
    assert requirements["require_outreach"] is False


def test_build_validation_requirements_for_full_run_with_outreach_enabled():
    config = Config()
    config.outreach.enabled = True
    requirements = _build_validation_requirements(
        config,
        skip_delivery=False,
        skip_outreach=False,
        dry_run=False,
    )
    assert requirements["require_gumroad"] is True
    assert requirements["require_smtp"] is True
    assert requirements["require_outreach"] is True


def test_config_launch_defaults_bound_weekly_query_grid(monkeypatch):
    monkeypatch.delenv("SEARCH_QUERIES_JSON", raising=False)
    monkeypatch.delenv("TARGET_CITIES_JSON", raising=False)

    config = Config()

    assert len(config.search_queries) * len(config.target_cities) <= 12


def test_config_accepts_json_query_grid_overrides(monkeypatch):
    queries = ["plumber", "dentist"]
    cities = ["Austin, TX", "Denver, CO"]
    monkeypatch.setenv("SEARCH_QUERIES_JSON", json.dumps(queries))
    monkeypatch.setenv("TARGET_CITIES_JSON", json.dumps(cities))

    config = Config()

    assert config.search_queries == queries
    assert config.target_cities == cities
