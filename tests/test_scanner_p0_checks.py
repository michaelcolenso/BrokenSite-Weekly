from unittest.mock import Mock

from scanner.checks.p0 import (
    broken_images,
    broken_pages,
    dead_cms,
    dead_form,
    no_https,
    not_mobile,
    stale_copyright,
)
from scanner.score import lead_severity, lead_tier
from scanner.checks import CheckResult


class DummyResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def session_with_statuses(*statuses):
    session = Mock()
    session.head.side_effect = [DummyResponse(status) for status in statuses]
    return session


def test_no_https_triggers_when_http_serves_and_https_unavailable():
    result = no_https("example.com", http_status=200, https_status=None)
    assert result.triggered is True
    assert result.severity == 4


def test_no_https_negative_when_https_serves():
    assert no_https("example.com", http_status=200, https_status=200).triggered is False


def test_dead_form_triggers_for_broken_action():
    html = '<form action="/missing"></form>'
    result = dead_form("https://example.com", html, session=session_with_statuses(404))
    assert result.triggered is True


def test_dead_form_negative_for_ok_action():
    html = '<form action="/contact"></form>'
    assert dead_form("https://example.com", html, session=session_with_statuses(200)).triggered is False


def test_broken_pages_triggers_for_homepage_failure():
    assert broken_pages([500, 200, 200]).triggered is True


def test_broken_pages_negative_for_one_internal_failure():
    assert broken_pages([200, 404, 200]).triggered is False


def test_not_mobile_triggers_without_viewport():
    assert not_mobile("<html><head></head></html>").triggered is True


def test_not_mobile_negative_with_viewport():
    html = '<meta name="viewport" content="width=device-width, initial-scale=1">'
    assert not_mobile(html).triggered is False


def test_stale_copyright_triggers_for_2021_or_older():
    assert stale_copyright("<footer>© 2020</footer>").triggered is True


def test_stale_copyright_negative_for_current_year():
    assert stale_copyright("<footer>&copy; 2026</footer>").triggered is False


def test_broken_images_triggers_for_three_broken_images():
    html = ''.join(f'<img src="/{i}.jpg">' for i in range(3))
    assert broken_images("https://example.com", html, session=session_with_statuses(404, 404, 404)).triggered is True


def test_broken_images_negative_for_two_broken_images():
    html = ''.join(f'<img src="/{i}.jpg">' for i in range(3))
    assert broken_images("https://example.com", html, session=session_with_statuses(404, 404, 200)).triggered is False


def test_dead_cms_triggers_old_wordpress():
    html = '<meta name="generator" content="WordPress 4.9">'
    assert dead_cms(html).triggered is True


def test_dead_cms_negative_current_wordpress():
    html = '<meta name="generator" content="WordPress 6.4">'
    assert dead_cms(html).triggered is False


def test_scoring_adds_bonus_for_three_checks_and_caps_at_five():
    checks = [
        CheckResult("a", True, "", 3),
        CheckResult("b", True, "", 4),
        CheckResult("c", True, "", 5),
    ]
    assert lead_severity(checks) == 5
    assert lead_tier(5) == "A"
    assert lead_tier(4) == "B"
    assert lead_tier(3) == "C"


def test_ssl_expired_triggers_for_cert_verification_error(monkeypatch):
    from scanner.checks import p0

    class FakeContext:
        def wrap_socket(self, sock, server_hostname):
            raise p0.ssl.SSLCertVerificationError("expired")

    class FakeSocket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(p0.ssl, "create_default_context", lambda: FakeContext())
    monkeypatch.setattr(p0.socket, "create_connection", lambda *args, **kwargs: FakeSocket())

    assert p0.ssl_expired("example.com").triggered is True


def test_ssl_expired_negative_for_valid_cert(monkeypatch):
    from scanner.checks import p0

    class FakeWrapped:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    class FakeContext:
        def wrap_socket(self, sock, server_hostname):
            return FakeWrapped()

    class FakeSocket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(p0.ssl, "create_default_context", lambda: FakeContext())
    monkeypatch.setattr(p0.socket, "create_connection", lambda *args, **kwargs: FakeSocket())

    assert p0.ssl_expired("example.com").triggered is False
