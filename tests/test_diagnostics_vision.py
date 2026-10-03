"""Phase 2: diagnostic collector + vision evaluator, with failure containment.

Collector tests drive real headless Chromium against a local HTTP server.
Vision tests mock the Anthropic API; no network or key required.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from scanner.crawl import PoliteCrawler
from scanner.diagnostics import collector
from scanner.diagnostics.collector import collect_diagnostics
from scanner.diagnostics.schema import DiagnosticReport
from scanner.evaluators import vision
from scanner.evaluators.vision import evaluate_screenshot, parse_verdict, validate_vision_env

GOOD = """<!doctype html><html><head><meta name=viewport content="width=device-width">
<link rel=stylesheet href="/missing.css"></head><body><h1>Acme Plumbing</h1>
<a href="tel:+15551234567">Call</a><a href="mailto:a@b.co">Mail</a>
<form><input name=n><textarea name=m></textarea></form>
<img src="/missing.png"></body></html>"""
OVERFLOW = ("<!doctype html><html><head><meta name=viewport content='width=device-width'></head>"
            "<body><div style='width:900px;height:20px;background:red'>wide</div></body></html>")
# No viewport tag: a real phone lays out at 980px, so a 600px element does NOT overflow.
NO_VIEWPORT = "<!doctype html><html><body><div style='width:600px;height:20px;background:red'>x</div></body></html>"
OTHER_PORT = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path
        if path == "/robots.txt":
            body, code = b"User-agent: *\nAllow: /\n", 200
        elif path == "/overflow":
            body, code = OVERFLOW.encode(), 200
        elif path == "/noviewport":
            body, code = NO_VIEWPORT.encode(), 200
        elif path == "/jsnav":
            body = f"<html><body><script>location.href='http://127.0.0.1:{OTHER_PORT['p']}/secret'</script></body></html>".encode()
            code = 200
        elif path == "/xredir":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{OTHER_PORT['p']}/secret")
            self.end_headers()
            return
        elif path == "/404":
            body, code = b"<html><body>Not found</body></html>", 404
        elif path == "/500":
            body, code = b"<html><body>boom</body></html>", 500
        elif path == "/slow":
            time.sleep(3)
            body, code = b"<html>late</html>", 200
        elif path == "/hop1":
            self.send_response(301)
            self.send_header("Location", "/redir")
            self.end_headers()
            return
        elif path == "/redir":
            self.send_response(301)
            self.send_header("Location", "/")
            self.end_headers()
            return
        elif path in ("/missing.css", "/missing.png"):
            body, code = b"nope", 404
        else:
            body, code = GOOD.encode(), 200
        self.send_response(code)
        self.send_header("Content-Type", "text/html" if path not in ("/missing.png",) else "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(autouse=True, scope="module")
def _chromium_override():
    # Sandbox ships a Chromium that may not match the installed Playwright pin.
    path = Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    mp = pytest.MonkeyPatch()
    if path.exists() and "BSW_CHROMIUM_PATH" not in __import__("os").environ:
        mp.setenv("BSW_CHROMIUM_PATH", str(path))
    yield
    mp.undo()


class DisallowAllHandler(BaseHTTPRequestHandler):
    hits = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        DisallowAllHandler.hits.append(self.path)
        body = b"User-agent: *\nDisallow: /\n" if self.path == "/robots.txt" else b"<html>secret</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    other = ThreadingHTTPServer(("127.0.0.1", 0), DisallowAllHandler)
    OTHER_PORT["p"] = other.server_port
    for s_ in (srv, other):
        threading.Thread(target=s_.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{srv.server_port}"
    srv.shutdown()
    other.shutdown()


class FastCrawler(PoliteCrawler):
    """No 10s pacing in tests."""

    def wait_for_domain(self, domain):
        pass


def collect(url, tmp_path, **kw):
    return collect_diagnostics(url, tmp_path, crawler=FastCrawler(), sleep=lambda s: None, **kw)


# ---- collector -------------------------------------------------------------

def test_healthy_page_reports_contacts_assets_screenshots(server, tmp_path):
    r = collect(f"http://{server}/", tmp_path)
    assert r.status == "ok" and r.reachable and r.http_status == 200
    assert set(r.contact_methods_found) == {"phone", "email", "form"}
    assert r.broken_asset_count == 2  # missing.css + missing.png
    assert not r.mobile_viewport_overflow
    assert Path(r.screenshot_path).stat().st_size > 0
    assert Path(r.mobile_screenshot_path).stat().st_size > 0
    assert r.ssl_error == "no_https" and r.ssl_valid is False
    DiagnosticReport.model_validate(r.model_dump())


def test_mobile_overflow_detected(server, tmp_path):
    assert collect(f"http://{server}/overflow", tmp_path).mobile_viewport_overflow is True


def test_mobile_capture_uses_real_mobile_viewport_semantics(server, tmp_path):
    # Without a viewport tag a phone lays out at 980px: a 600px element is not overflow.
    # (A plain narrowed desktop window would wrongly report overflow here.)
    r = collect(f"http://{server}/noviewport", tmp_path)
    assert r.status == "ok" and r.mobile_viewport_overflow is False
    # Screenshot is phone-sized: PNG IHDR width/height.
    import struct
    w, h = struct.unpack(">II", Path(r.mobile_screenshot_path).read_bytes()[16:24])
    assert (w, h) == (390, 844)


def test_robots_rechecked_on_cross_origin_redirect(server, tmp_path):
    DisallowAllHandler.hits.clear()
    r = collect(f"http://{server}/xredir", tmp_path)
    assert r.status == "blocked" and "robots_disallow" in r.error
    assert r.screenshot_path is None
    assert "/secret" not in DisallowAllHandler.hits  # blocked page was never fetched


def test_multi_hop_chain_every_hop_vetted_and_paced(server, tmp_path):
    waits, vetted = [], []
    res = collector.browse(f"http://{server}/hop1", tmp_path, "chain",
                           lambda u: (vetted.append(u), True)[1], waits.append, lambda h: None)
    assert res.http_status == 200 and len(res.redirect_chain) == 2
    assert len(vetted) == 3 and len(waits) == 3  # /hop1, /redir, / each vetted and paced


def test_robots_rechecked_on_client_side_navigation(server, tmp_path):
    DisallowAllHandler.hits.clear()
    r = collect(f"http://{server}/jsnav", tmp_path)
    assert r.status == "blocked" and "robots_disallow" in r.error
    assert "/secret" not in DisallowAllHandler.hits


def test_every_navigation_hop_is_paced_and_recorded(server, tmp_path):
    waits, records = [], []
    browse = collector.browse
    res = browse(f"http://{server}/redir", tmp_path, "pace", lambda u: True, waits.append, records.append)
    assert res.http_status == 200 and len(res.redirect_chain) == 1
    assert waits == records == [server, server]  # initial request + redirect hop


def test_redirect_chain_recorded(server, tmp_path):
    r = collect(f"http://{server}/redir", tmp_path)
    assert r.http_status == 200 and len(r.redirect_chain) == 1


@pytest.mark.parametrize("path,code", [("404", 404), ("500", 500)])
def test_http_errors_are_unreachable(server, tmp_path, path, code):
    r = collect(f"http://{server}/{path}", tmp_path)
    assert r.status == "ok" and r.http_status == code and r.reachable is False


def test_timeout_is_contained_after_two_attempts(server, tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "NAV_TIMEOUT_MS", 800)
    calls = []
    real = collector.browse
    monkeypatch.setattr(collector, "browse", lambda *a: (calls.append(1), real(*a))[1])
    r = collect_diagnostics(f"http://{server}/slow", tmp_path, crawler=FastCrawler(),
                            browse_fn=collector.browse, sleep=lambda s: None)
    assert r.status == "error" and "Timeout" in r.error and r.reachable is False
    assert len(calls) == collector.MAX_ATTEMPTS


def test_connection_refused_is_contained(tmp_path):
    r = collect("http://127.0.0.1:1/", tmp_path)
    assert r.status == "error" and r.reachable is False and r.error


def test_ssl_error_is_contained(server, tmp_path):
    # TLS handshake against a plain-HTTP port fails both our check and the browser.
    r = collect(f"https://{server}/", tmp_path)
    assert r.status == "error" and r.ssl_valid is False and r.ssl_error
    assert r.screenshot_path is None


def test_ssl_checked_on_final_https_destination_after_http_redirect(tmp_path):
    seen = []
    browsed = collector.BrowseResult(http_status=200, final_url="https://example.com/")
    r = collect_diagnostics("http://example.com", tmp_path, crawler=FastCrawler(),
                            browse_fn=lambda *a: browsed,
                            ssl_fn=lambda h, p: (seen.append((h, p)), (True, "2030-01-01T00:00:00+00:00", None))[1])
    assert seen == [("example.com", 443)]
    assert r.ssl_valid is True and r.ssl_error is None


def test_ssl_not_rechecked_when_https_input_stays_on_same_host(tmp_path):
    seen = []
    browsed = collector.BrowseResult(http_status=200, final_url="https://example.com/x")
    collect_diagnostics("https://example.com", tmp_path, crawler=FastCrawler(), browse_fn=lambda *a: browsed,
                        ssl_fn=lambda h, p: (seen.append(h), (True, None, None))[1])
    assert seen == ["example.com"]


def test_ssl_false_when_https_input_downgrades_to_http(tmp_path):
    browsed = collector.BrowseResult(http_status=200, final_url="http://example.com/")
    r = collect_diagnostics("https://example.com", tmp_path, crawler=FastCrawler(), browse_fn=lambda *a: browsed,
                            ssl_fn=lambda h, p: (True, None, None))
    assert r.ssl_valid is False and r.ssl_error == "no_https"


def test_invalid_url_is_contained(tmp_path):
    r = collect("", tmp_path)
    assert r.status == "error" and r.error == "invalid_url"


def test_robots_disallow_skips_fetch(tmp_path):
    crawler = FastCrawler()
    crawler.allowed = lambda url: False
    browse = Mock()
    r = collect_diagnostics("example.com", tmp_path, crawler=crawler, browse_fn=browse)
    assert r.status == "blocked" and browse.call_count == 0


def test_unexpected_crash_never_raises(tmp_path):
    crawler = FastCrawler()
    crawler.allowed = Mock(side_effect=RuntimeError("kaboom"))
    r = collect_diagnostics("example.com", tmp_path, crawler=crawler)
    assert r.status == "error" and "kaboom" in r.error


# ---- vision ----------------------------------------------------------------

VERDICT = {
    "is_broken_or_neglected": True, "confidence_score": 0.9,
    "primary_defect_category": "layout_overflow",
    "observable_evidence": "Nav overlaps the logo on mobile.",
    "quick_fix_headline": "Fix mobile navigation overlap",
}


def ok_report(tmp_path, **kw):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"\x89PNG fake")
    base = dict(domain="x.com", url="https://x.com", status="ok", reachable=True,
                http_status=200, screenshot_path=str(shot))
    base.update(kw)
    return DiagnosticReport(**base)


def api_response(code=200, text=None):
    resp = Mock(status_code=code)
    resp.json.return_value = {"content": [{"type": "text", "text": text if text is not None else json.dumps(VERDICT)}]}
    return resp


@pytest.mark.parametrize("kw,reason", [
    (dict(reachable=False, http_status=404), "unreachable_http_404"),
    (dict(status="error", reachable=False), "diagnostics_error"),
    (dict(status="blocked", reachable=False), "diagnostics_blocked"),
    (dict(screenshot_path=None), "no_screenshot"),
])
def test_vision_skips_without_calling_api(tmp_path, kw, reason):
    session = Mock()
    res = evaluate_screenshot(ok_report(tmp_path, **kw), "plumber", api_key="k", session=session)
    assert res.status == "skipped" and res.reason == reason
    assert session.post.call_count == 0


def test_vision_happy_path_and_fenced_json(tmp_path):
    session = Mock()
    session.post.return_value = api_response(text="```json\n" + json.dumps(VERDICT) + "\n```")
    res = evaluate_screenshot(ok_report(tmp_path), "plumber", api_key="k", session=session)
    assert res.status == "ok" and res.verdict.actionable()
    kwargs = session.post.call_args.kwargs
    assert kwargs["timeout"] <= 15 and kwargs["headers"]["x-api-key"] == "k"
    assert kwargs["json"]["messages"][0]["content"][1]["type"] == "image"


def test_vision_retries_once_on_429_then_succeeds(tmp_path):
    session = Mock()
    session.post.side_effect = [api_response(429), api_response()]
    res = evaluate_screenshot(ok_report(tmp_path), "plumber", api_key="k", session=session, sleep=lambda s: None)
    assert res.status == "ok" and session.post.call_count == 2


def test_vision_gives_up_after_two_attempts(tmp_path):
    session = Mock()
    session.post.side_effect = requests.Timeout("slow")
    res = evaluate_screenshot(ok_report(tmp_path), "plumber", api_key="k", session=session, sleep=lambda s: None)
    assert res.status == "error" and "Timeout" in res.reason and session.post.call_count == 2


def test_vision_auth_error_not_retried(tmp_path):
    session = Mock()
    session.post.return_value = api_response(401)
    res = evaluate_screenshot(ok_report(tmp_path), "plumber", api_key="k", session=session)
    assert res.status == "error" and res.reason == "http_401" and session.post.call_count == 1


def test_vision_malformed_reply_is_contained(tmp_path):
    session = Mock()
    session.post.return_value = api_response(text="I think it looks fine!")
    res = evaluate_screenshot(ok_report(tmp_path), "plumber", api_key="k", session=session, sleep=lambda s: None)
    assert res.status == "error" and res.verdict is None and session.post.call_count == 2


@pytest.mark.parametrize("payload", [[], {"content": None}, {"content": [None, 5]}, {"content": "text"}, {}])
def test_vision_unexpected_200_payload_is_contained(tmp_path, payload):
    session = Mock()
    resp = Mock(status_code=200)
    resp.json.return_value = payload
    session.post.return_value = resp
    res = evaluate_screenshot(ok_report(tmp_path), "plumber", api_key="k", session=session, sleep=lambda s: None)
    assert res.status == "error" and res.verdict is None


def test_vision_schema_violation_rejected():
    bad = dict(VERDICT, confidence_score=1.7)
    with pytest.raises(Exception):
        parse_verdict(json.dumps(bad))
    with pytest.raises(Exception):
        parse_verdict(json.dumps(dict(VERDICT, primary_defect_category="vibes")))


def test_vision_missing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    res = evaluate_screenshot(ok_report(tmp_path), "plumber", session=Mock())
    assert res.status == "error" and res.reason == "missing_api_key"


def test_validate_vision_env():
    assert validate_vision_env({"ANTHROPIC_API_KEY": "k"}) == []
    assert validate_vision_env({}) == ["ANTHROPIC_API_KEY is not set"]
    assert len(validate_vision_env({"ANTHROPIC_API_KEY": "k", "BSW_VISION_MODEL": " "})) == 1
