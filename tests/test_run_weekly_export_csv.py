import logging
from datetime import datetime

from src.db import Lead
from src.logging_setup import RunContext
from src.run_weekly import run_export_csv_phase
from src.config import load_config


def test_export_csv_phase_writes_csv_from_scraped_leads(tmp_path, monkeypatch):
    # Redirect CSV output to a temp directory to avoid polluting the repo.
    import src.run_weekly as run_weekly_mod

    monkeypatch.setattr(run_weekly_mod, "OUTPUT_DIR", tmp_path)

    cfg = load_config()
    logger = logging.getLogger("test_export_csv")
    run_ctx = RunContext(logger)

    lead = Lead(
        place_id="test_place_id",
        cid="123",
        name="Test Business",
        website="https://example.com",
        address="123 Main St",
        phone="555-0100",
        review_count=10,
        city="Austin, TX",
        category="plumber",
        score=80,
        reasons="ssl_error,timeout",
        first_seen=datetime.utcnow(),
        last_seen=datetime.utcnow(),
        lead_tier="pro",
    )

    paths = run_export_csv_phase(
        config=cfg,
        db=None,  # unused in scraped-leads branch
        run_ctx=run_ctx,
        scraped_qualifying_leads=[lead],
        label="local",
    )

    assert len(paths) == 1
    csv_path = tmp_path / csv_path_basename(paths[0])
    assert csv_path.exists()
    content = csv_path.read_text(encoding="utf-8")
    assert "Test Business" in content


def csv_path_basename(path_str: str) -> str:
    # Keep this test platform-agnostic.
    return path_str.split("/")[-1]

