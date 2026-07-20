import asyncio

from src.config import Config, PortalConfig, SMTPConfig
from src.portal_auth import generate_portal_token
from src.rate_limit import SlidingWindowLimiter
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


def _make_request(ip: str = "127.0.0.1"):
    """Minimal ASGI-style request stub for handlers under test."""
    class _Client:
        host = ip

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


# ============================================================
# Rate limiting: engagement event dedupe + CTA submission throttle
# ============================================================


def test_record_event_dedupes_repeat_within_window(test_database, monkeypatch):
    """Repeated hits of the same (ip, place_id, event_type) within the
    dedupe window must only write one engagement_events row, so a
    refresh-spam script can't trivially inflate get_engagement_score()."""
    monkeypatch.setattr(tracking, "_get_db", lambda: test_database)
    monkeypatch.setattr(tracking, "_engagement_event_limiter", SlidingWindowLimiter())

    request = _make_request()
    tracking._record_event("place1", "page_view", request)
    tracking._record_event("place1", "page_view", request)
    tracking._record_event("place1", "page_view", request)

    events = test_database.get_events_for_lead("place1")
    assert len(events) == 1


def test_record_event_dedupe_is_scoped_per_place_and_event_type(
    test_database, monkeypatch
):
    """Dedupe must not cross-contaminate different place_ids, event types,
    or source IPs — only exact repeats within the window are collapsed."""
    monkeypatch.setattr(tracking, "_get_db", lambda: test_database)
    monkeypatch.setattr(tracking, "_engagement_event_limiter", SlidingWindowLimiter())

    tracking._record_event("place1", "page_view", _make_request("1.1.1.1"))
    tracking._record_event("place2", "page_view", _make_request("1.1.1.1"))
    tracking._record_event("place1", "cta_click", _make_request("1.1.1.1"))
    tracking._record_event("place1", "page_view", _make_request("2.2.2.2"))

    # place1: page_view@1.1.1.1, cta_click@1.1.1.1, page_view@2.2.2.2 — 3 distinct keys.
    assert len(test_database.get_events_for_lead("place1")) == 3
    assert len(test_database.get_events_for_lead("place2")) == 1


def test_cta_submit_within_limit_records_inquiry(test_database, monkeypatch):
    config = Config(smtp=SMTPConfig(from_email="support@example.com"))
    monkeypatch.setattr(tracking, "_get_db", lambda: test_database)
    monkeypatch.setattr(tracking, "load_config", lambda: config)
    monkeypatch.setattr(tracking, "_engagement_event_limiter", SlidingWindowLimiter())
    monkeypatch.setattr(tracking, "_cta_submit_limiter", SlidingWindowLimiter())
    monkeypatch.setattr(tracking, "_send_inquiry_notifications", lambda *a, **k: None)

    response = asyncio.run(
        tracking.track_cta_submit(
            "place1",
            _make_request(),
            name="Jane Owner",
            email="jane@biz.com",
            phone="555-0100",
            notes="Interested in a rebuild.",
        )
    )

    assert response.status_code == 200
    with test_database._connect() as conn:
        rows = conn.execute(
            "SELECT name, email FROM lead_inquiries WHERE place_id = 'place1'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["email"] == "jane@biz.com"


def test_cta_submit_blocks_after_limit_exceeded(test_database, monkeypatch):
    """Beyond CTA_SUBMIT_MAX_PER_WINDOW submissions from one IP, further
    submissions must be rejected (429) and not write an inquiry row or
    trigger the pro-subscriber notification fan-out."""
    config = Config(smtp=SMTPConfig(from_email="support@example.com"))
    monkeypatch.setattr(tracking, "_get_db", lambda: test_database)
    monkeypatch.setattr(tracking, "load_config", lambda: config)
    monkeypatch.setattr(tracking, "_engagement_event_limiter", SlidingWindowLimiter())
    monkeypatch.setattr(tracking, "_cta_submit_limiter", SlidingWindowLimiter())

    notify_calls = []
    monkeypatch.setattr(
        tracking, "_send_inquiry_notifications",
        lambda *a, **k: notify_calls.append(a),
    )

    request = _make_request()
    for _ in range(tracking.CTA_SUBMIT_MAX_PER_WINDOW):
        response = asyncio.run(
            tracking.track_cta_submit(
                "place1", request, name="A", email="a@biz.com", phone="", notes=""
            )
        )
        assert response.status_code == 200

    blocked_response = asyncio.run(
        tracking.track_cta_submit(
            "place1", request, name="B", email="b@biz.com", phone="", notes=""
        )
    )
    assert blocked_response.status_code == 429

    with test_database._connect() as conn:
        rows = conn.execute(
            "SELECT email FROM lead_inquiries WHERE place_id = 'place1'"
        ).fetchall()
    assert len(rows) == tracking.CTA_SUBMIT_MAX_PER_WINDOW
    assert len(notify_calls) == tracking.CTA_SUBMIT_MAX_PER_WINDOW
    assert all(row["email"] != "b@biz.com" for row in rows)


def test_cta_submit_truncates_overlong_fields(test_database, monkeypatch):
    config = Config(smtp=SMTPConfig(from_email="support@example.com"))
    monkeypatch.setattr(tracking, "_get_db", lambda: test_database)
    monkeypatch.setattr(tracking, "load_config", lambda: config)
    monkeypatch.setattr(tracking, "_engagement_event_limiter", SlidingWindowLimiter())
    monkeypatch.setattr(tracking, "_cta_submit_limiter", SlidingWindowLimiter())
    monkeypatch.setattr(tracking, "_send_inquiry_notifications", lambda *a, **k: None)

    huge_name = "x" * 5000
    huge_notes = "y" * 10000

    asyncio.run(
        tracking.track_cta_submit(
            "place1", _make_request(), name=huge_name, email="", phone="", notes=huge_notes
        )
    )

    with test_database._connect() as conn:
        row = conn.execute(
            "SELECT name, notes FROM lead_inquiries WHERE place_id = 'place1'"
        ).fetchone()
    assert len(row["name"]) == tracking.CTA_FIELD_MAX_LENGTH
    assert len(row["notes"]) == tracking.CTA_NOTES_MAX_LENGTH
