import asyncio

from src.config import Config, PortalConfig, SMTPConfig
from src.portal_auth import generate_portal_token
from src import tracking


def test_portal_download_rejects_export_owned_by_other_subscriber(
    tmp_path,
    test_database,
    monkeypatch,
):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    alice_csv = output_dir / "alice.csv"
    bob_csv = output_dir / "bob.csv"
    alice_csv.write_text("name\nalice\n", encoding="utf-8")
    bob_csv.write_text("name\nbob\n", encoding="utf-8")

    test_database.record_export(
        "run-1",
        "alice@example.com",
        1,
        str(alice_csv),
        tier="basic",
    )
    test_database.record_export(
        "run-1",
        "bob@example.com",
        1,
        str(bob_csv),
        tier="basic",
    )

    config = Config(
        portal=PortalConfig(secret="portal-secret", base_url="https://portal.example"),
        smtp=SMTPConfig(from_email="support@example.com"),
    )
    monkeypatch.setattr(tracking, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(tracking, "_get_db", lambda: test_database)
    monkeypatch.setattr(tracking, "load_config", lambda: config)

    token = generate_portal_token("alice@example.com", config.portal.secret)
    response = asyncio.run(
        tracking.portal_download(bob_csv.name, token=token)
    )

    assert response.status_code == 404


def test_portal_download_rejects_sibling_path_with_output_prefix(
    tmp_path,
    test_database,
    monkeypatch,
):
    output_dir = tmp_path / "output"
    sibling_dir = tmp_path / "output-archive"
    output_dir.mkdir()
    sibling_dir.mkdir()
    sibling_csv = sibling_dir / "alice.csv"
    sibling_csv.write_text("name\nalice\n", encoding="utf-8")

    test_database.record_export(
        "run-1",
        "alice@example.com",
        1,
        str(sibling_csv),
        tier="basic",
    )

    config = Config(
        portal=PortalConfig(secret="portal-secret", base_url="https://portal.example"),
        smtp=SMTPConfig(from_email="support@example.com"),
    )
    monkeypatch.setattr(tracking, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(tracking, "_get_db", lambda: test_database)
    monkeypatch.setattr(tracking, "load_config", lambda: config)

    token = generate_portal_token("alice@example.com", config.portal.secret)
    response = asyncio.run(
        tracking.portal_download("../output-archive/alice.csv", token=token)
    )

    assert response.status_code == 400


def _make_request():
    """Minimal ASGI-style request stub for dashboard handler."""
    class _Client:
        host = "127.0.0.1"

    class _Request:
        client = _Client()
        headers = {}

    return _Request()


def test_dashboard_disabled_when_no_token_configured(
    tmp_path,
    test_database,
    monkeypatch,
):
    config = Config(
        portal=PortalConfig(dashboard_token=""),
        smtp=SMTPConfig(from_email="support@example.com"),
    )
    monkeypatch.setattr(tracking, "_get_db", lambda: test_database)
    monkeypatch.setattr(tracking, "load_config", lambda: config)

    response = asyncio.run(tracking.dashboard(_make_request(), token="anything"))
    assert response.status_code == 403


def test_dashboard_rejects_wrong_token(
    tmp_path,
    test_database,
    monkeypatch,
):
    config = Config(
        portal=PortalConfig(dashboard_token="s3cret"),
        smtp=SMTPConfig(from_email="support@example.com"),
    )
    monkeypatch.setattr(tracking, "_get_db", lambda: test_database)
    monkeypatch.setattr(tracking, "load_config", lambda: config)

    response = asyncio.run(tracking.dashboard(_make_request(), token="wrong"))
    assert response.status_code == 403


def test_dashboard_allows_correct_token(
    tmp_path,
    test_database,
    monkeypatch,
):
    config = Config(
        portal=PortalConfig(dashboard_token="s3cret"),
        smtp=SMTPConfig(from_email="support@example.com"),
    )
    monkeypatch.setattr(tracking, "_get_db", lambda: test_database)
    monkeypatch.setattr(tracking, "load_config", lambda: config)

    response = asyncio.run(tracking.dashboard(_make_request(), token="s3cret"))
    assert response.status_code == 200


def test_unsubscribe_endpoint_suppresses_contact_email(
    test_database,
    monkeypatch,
):
    """The /unsubscribe/{place_id} handler must suppress the contact's email
    (not just the place_id), so a shared email across multiple place_ids
    (e.g. multi-location businesses) is fully suppressed after one click."""
    test_database.record_contact("place1", "owner@biz.com", "mailto", 0.9)

    config = Config(smtp=SMTPConfig(from_email="support@example.com"))
    monkeypatch.setattr(tracking, "_get_db", lambda: test_database)
    monkeypatch.setattr(tracking, "load_config", lambda: config)

    response = asyncio.run(tracking.unsubscribe("place1", _make_request()))

    assert response.status_code == 200
    assert test_database.is_unsubscribed("place1") is True
    assert test_database.is_suppressed("owner@biz.com") is True
