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
from scanner.diagnostics.guard import (
    GuardedNavigator, RobotsBlocked, _has_base_uri, _relax_base_uri, _rebase_headers, _rewrite_self, _with_base,
)
from scanner.screenshot import capture_homepage
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
    post_hits = []   # POSTs that reached the server
    popup_hits = []  # requests for the popup targets
    final_hits = []  # requests for the redirect target document (/sub/framefinal)
    pic_hits = []   # paths requested for the relative image in the redirected iframe
    loop_hits = []  # requests for /frameloop
    sw_hits = []  # requests for /sw.js: a registration attempt means service workers weren't blocked

    def log_message(self, *a):
        pass

    def do_POST(self):
        Handler.post_hits.append(self.path)
        self.send_response(307)
        self.send_header("Location", "/clean")
        self.end_headers()

    def do_GET(self):
        path = self.path
        if path == "/robots.txt":
            body, code = b"User-agent: *\nAllow: /\n", 200
        elif path == "/overflow":
            body, code = OVERFLOW.encode(), 200
        elif path == "/noviewport":
            body, code = NO_VIEWPORT.encode(), 200
        elif path == "/settleredir":  # client-side nav whose target is itself a server redirect
            body = b"<html><body><script>setTimeout(()=>location.href='/redir',100)</script></body></html>"
            code = 200
        elif path == "/crossframe":  # iframe whose document is on an origin that disallows crawling
            body = f"<html><body><iframe src='http://127.0.0.1:{OTHER_PORT['p']}/framedoc'></iframe></body></html>".encode()
            code = 200
        elif path in ("/framehop", "/frameloop"):
            if path == "/frameloop":
                Handler.loop_hits.append(1)
            self.send_response(302)
            self.send_header("Location", "/framepage" if path == "/framehop" else "/frameloop")
            self.end_headers()
            return
        elif path == "/framehop2":  # redirects into a sub-directory so relative URLs expose a wrong base
            self.send_response(302)
            self.send_header("Location", "/sub/framefinal")
            self.end_headers()
            return
        elif path == "/sub/framefinal":
            Handler.final_hits.append(1)
            body, code = b"<html><body><img src='pic.png'></body></html>", 200
        elif path in ("/pic.png", "/sub/pic.png"):
            Handler.pic_hits.append(path)
            body, code = b"nope", 404
        elif path == "/postpage":  # auto-submits a form: POST /postsink, which 307-redirects
            body = (b"<html><body><h1>interstitial</h1><form method=post action='/postsink'>"
                    b"<input name=a value=1></form><script>document.forms[0].submit()</script></body></html>")
            code = 200
        elif path == "/iframepost":  # form that targets a hidden iframe
            body = (b"<html><body><h1>real page</h1><iframe name=f></iframe>"
                    b"<form method=post action='/postsink' target=f><input name=a value=1></form>"
                    b"<script>document.forms[0].submit()</script></body></html>")
            code = 200
        elif path == "/latepost":  # submits a form well after the settle window
            body = (b"<html><body><h1>real page</h1><form method=post action='/postsink'>"
                    b"<input name=a value=1></form><script>setTimeout(function(){document.forms[0].submit()},650)"
                    b"</script></body></html>")
            code = 200
        elif path == "/popuppage":  # opens a popup whose initial document redirects
            body = b"<html><body><h1>home</h1><script>window.open('/popupredir')</script></body></html>"
            code = 200
        elif path == "/popupredir":
            Handler.popup_hits.append(1)
            self.send_response(302)
            self.send_header("Location", "/clean")
            self.end_headers()
            return
        elif path == "/sandboxhop":  # sandboxed iframe, no allow-scripts: injected JS could not run
            body, code = b"<html><body><iframe sandbox src='/framehop2'></iframe></body></html>", 200
        elif path == "/framehop3":  # redirects to a document whose own CSP forbids <base>
            self.send_response(302)
            self.send_header("Location", "/sub/cspframe")
            self.end_headers()
            return
        elif path == "/sub/cspframe":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Security-Policy", "base-uri 'none'; img-src 'self'")
            body = b"<html><body><img src='pic.png'></body></html>"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif path == "/framehop5":  # final doc has base-uri 'none' AND its own <base href>
            self.send_response(302)
            self.send_header("Location", "/sub/cspbase")
            self.end_headers()
            return
        elif path == "/sub/cspbase":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Security-Policy", "base-uri 'none'")
            body = b"<html><head><base href='/evil/'></head><body><img src='pic.png'></body></html>"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif path == "/hopframe5":
            body, code = b"<html><body><iframe src='/framehop5'></iframe></body></html>", 200
        elif path == "/hopframe3":
            body, code = b"<html><body><iframe src='/framehop3'></iframe></body></html>", 200
        elif path == "/hopframe2":
            body, code = b"<html><body><iframe src='/framehop2'></iframe></body></html>", 200
        elif path == "/hopframe":
            body, code = b"<html><body><iframe src='/framehop'></iframe></body></html>", 200
        elif path == "/loopframe":
            body, code = b"<html><body><iframe src='/frameloop'></iframe></body></html>", 200
        elif path == "/swpage":
            body = (b"<html><body><script>navigator.serviceWorker && "
                    b"navigator.serviceWorker.register('/sw.js').catch(()=>{})</script></body></html>")
            code = 200
        elif path == "/sw.js":
            Handler.sw_hits.append(1)
            body, code = b"self.addEventListener('fetch', () => {});", 200
        elif path == "/withframe":
            body, code = b"<html><body><iframe src='/framepage'></iframe></body></html>", 200
        elif path == "/framepage":
            body, code = b"<html><body><img src='/missing.png'></body></html>", 200
        elif path == "/latenav":  # lands inside the settle window -> followed
            body = b"<html><body><script>setTimeout(()=>location.href='/clean',150)</script></body></html>"
            code = 200
        elif path == "/verylatenav":  # lands after the freeze -> vetoed
            body = b"<html><body><h1>stay</h1><script>setTimeout(()=>location.href='/clean',3000)</script></body></html>"
            code = 200
        elif path == "/resizenav":
            body = (b"<html><body><h1>desktop</h1><script>"
                    b"window.addEventListener('resize',()=>{location.href='/clean'})</script></body></html>")
            code = 200
        elif path == "/prenav":
            body = b"<html><body><img src='/missing.png'><script>location.href='/clean'</script></body></html>"
            code = 200
        elif path == "/clean":
            body, code = b"<html><body><h1>clean</h1></body></html>", 200
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
        self.send_header("Content-Type", "image/png" if path == "/missing.png"
                         else "application/javascript" if path == "/sw.js" else "text/html")
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

    def _wait_for_domain(self, domain):
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


def test_asset_observations_reset_on_client_side_navigation(server, tmp_path):
    # /prenav loads a 404 image, then JS-navigates to a clean page: only the final document counts.
    r = collect(f"http://{server}/prenav", tmp_path)
    assert r.status == "ok" and r.final_url.endswith("/clean")
    assert r.broken_asset_count == 0 and r.mixed_content_count == 0


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


def test_navigation_during_mobile_pass_is_vetoed(server, tmp_path):
    r = collect(f"http://{server}/resizenav", tmp_path)
    assert r.status == "ok" and r.final_url.endswith("/resizenav")
    assert r.blocked_navigation_url and r.blocked_navigation_url.endswith("/clean")
    assert Path(r.mobile_screenshot_path).stat().st_size > 0


def test_redirect_triggered_inside_settle_window_is_followed(server, tmp_path):
    r = collect(f"http://{server}/settleredir", tmp_path)
    assert r.status == "ok" and r.http_status == 200
    assert r.final_url == f"http://{server}/" and r.screenshot_path


def test_requests_inside_iframes_are_attributed_to_the_top_level_document(server, tmp_path):
    r = collect(f"http://{server}/withframe", tmp_path)
    assert r.status == "ok" and r.broken_asset_count == 1  # the 404 image inside the iframe


def test_client_navigation_inside_settle_window_is_followed(server, tmp_path):
    r = collect(f"http://{server}/latenav", tmp_path)
    assert r.status == "ok" and r.http_status == 200 and r.final_url.endswith("/clean")


def test_navigation_after_settle_window_is_vetoed_and_metadata_consistent(server, tmp_path):
    r = collect(f"http://{server}/verylatenav", tmp_path)
    assert r.status == "ok" and r.final_url.endswith("/verylatenav")
    assert r.blocked_navigation_url is None or r.blocked_navigation_url.endswith("/clean")


def test_http_iframe_counts_as_mixed_but_main_frame_navigation_does_not():
    f = collector.is_mixed_content_request
    assert f("http://x/img.png", False, True)        # subresource
    assert f("http://x/frame", True, False)          # child-frame document
    assert not f("http://x/", True, True)            # main-frame navigation (redirect hop)
    assert not f("https://x/a.png", False, True)


def test_cert_error_after_http_redirect_is_not_reported_as_no_https(tmp_path):
    def boom(*a):
        raise RuntimeError("Page.goto: net::ERR_CERT_DATE_INVALID")
    r = collect_diagnostics("http://example.com", tmp_path, crawler=FastCrawler(), browse_fn=boom,
                            sleep=lambda s: None)
    assert r.status == "error" and r.ssl_valid is False and "ERR_CERT" in r.ssl_error


@pytest.mark.parametrize("bad", ["https://[bad", "http://[::1", "https://host:notaport/"])
def test_malformed_urls_never_raise(tmp_path, bad):
    r = collect(bad, tmp_path)
    assert r.status == "error" and r.reachable is False


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


# ---- v1 screenshot path (scanner.screenshot.capture_homepage) ---------------------------------

class RecordingCrawler(FastCrawler):
    def __init__(self):
        super().__init__()
        self.waits, self.records = [], []

    def wait_for_domain(self, domain):
        self.waits.append(domain)

    def record_request(self, domain):
        self.records.append(domain)


def test_capture_homepage_writes_a_jpeg_and_paces_every_document_navigation(server, tmp_path):
    crawler = RecordingCrawler()
    out = tmp_path / "shots" / "site.jpg"
    capture_homepage(f"http://{server}/redir", out, crawler=crawler)  # /redir -> / (one redirect hop)
    assert out.read_bytes()[:2] == b"\xff\xd8"  # JPEG magic
    assert crawler.waits == [server, server]  # initial request + the redirect hop
    assert len(crawler.records) >= 2


def test_capture_homepage_obeys_robots_on_a_redirect_target(server, tmp_path):
    DisallowAllHandler.hits.clear()
    out = tmp_path / "blocked.jpg"
    with pytest.raises(RobotsBlocked):
        capture_homepage(f"http://{server}/xredir", out, crawler=RecordingCrawler())
    assert not out.exists()
    assert "/secret" not in DisallowAllHandler.hits  # the disallowed page was never fetched


def test_capture_homepage_vetoes_a_late_navigation_and_still_captures(server, tmp_path):
    out = tmp_path / "late.jpg"
    capture_homepage(f"http://{server}/verylatenav", out, crawler=RecordingCrawler())
    assert out.stat().st_size > 0


def test_iframe_documents_are_treated_as_embedded_content_not_guarded_navigations(server, tmp_path):
    """Pins the HANDOFF rule 4 screenshot exception as written: documents embedded in iframes are
    exempt like other embedded content, so a robots-disallowed iframe origin is still rendered."""
    DisallowAllHandler.hits.clear()
    out = tmp_path / "frame.jpg"
    capture_homepage(f"http://{server}/crossframe", out, crawler=RecordingCrawler())
    assert out.stat().st_size > 0
    assert "/framedoc" in DisallowAllHandler.hits


def test_collector_handles_a_page_with_a_cross_origin_iframe(server, tmp_path):
    """Regression: a guarded top-level document plus a continued cross-origin iframe request used
    to hang screenshots until the timeout (YouTube/Maps-style embeds)."""
    r = collect(f"http://{server}/crossframe", tmp_path)
    assert r.status == "ok" and r.http_status == 200
    assert Path(r.screenshot_path).stat().st_size > 0 and Path(r.mobile_screenshot_path).stat().st_size > 0
    assert r.execution_time_ms < 15000


def test_service_workers_are_blocked_so_they_cannot_serve_pages_past_the_guard(server, tmp_path):
    Handler.sw_hits.clear()
    r = collect(f"http://{server}/swpage", tmp_path)
    assert r.status == "ok"
    capture_homepage(f"http://{server}/swpage", tmp_path / "sw.jpg", crawler=RecordingCrawler())
    assert Handler.sw_hits == []  # the worker script was never even requested


def test_redirecting_iframe_commits_under_its_final_url(server, tmp_path):
    # /hopframe embeds /framehop, which 302s to /framepage; only the *final* document loads the
    # 404 image, so a count of 1 proves the redirect was followed and the final page rendered.
    r = collect(f"http://{server}/hopframe", tmp_path)
    assert r.status == "ok" and r.broken_asset_count == 1


def test_iframe_redirect_loop_is_capped(server, tmp_path):
    Handler.loop_hits.clear()
    r = collect(f"http://{server}/loopframe", tmp_path)
    assert r.status == "ok"
    assert 1 <= len(Handler.loop_hits) <= collector.MAX_REDIRECTS + 2  # bounded, not endless


def test_redirected_iframe_resolves_relative_urls_against_its_final_url(server, tmp_path):
    Handler.pic_hits.clear()
    r = collect(f"http://{server}/hopframe2", tmp_path)
    assert r.status == "ok"
    # Committed under /sub/framefinal, 'pic.png' is /sub/pic.png; committed under the original
    # /framehop2 it would wrongly be /pic.png.
    assert Handler.pic_hits == ["/sub/pic.png"]


def test_redirecting_sandboxed_iframe_without_allow_scripts_still_loads_its_target(server, tmp_path):
    # Neither a script nor a meta refresh can drive a sandboxed iframe, so the redirect has to be
    # followed outside the frame. (The frame's own image is not asserted: with loopback test
    # servers Chromium's private-network-access rule blocks subresources of a fulfilled document in
    # an opaque-origin frame; that does not apply to sites on public addresses.)
    Handler.final_hits.clear()
    r = collect(f"http://{server}/sandboxhop", tmp_path)
    assert r.status == "ok"
    assert Handler.final_hits  # the redirect target's document was fetched and delivered


def test_popup_navigation_cannot_replace_the_captured_page(server, tmp_path):
    Handler.popup_hits.clear()
    r = collect(f"http://{server}/popuppage", tmp_path)
    assert r.status == "ok" and r.final_url.endswith("/popuppage")  # not the popup's /clean
    assert Handler.popup_hits == []  # the popup's document was never even fetched


def test_with_base_injects_after_doctype():
    out = _with_base(b"<!DOCTYPE html><html><head></head><body></body></html>", "http://h/sub/final")
    assert out.startswith(b'<!DOCTYPE html><base href="http://h/sub/final">')


def test_with_base_without_doctype_prepends():
    assert _with_base(b"<html></html>", "http://h/x").startswith(b'<base href="http://h/x"><html>')


def test_with_base_resolves_an_existing_relative_base_against_the_final_url():
    body = b'<html><head><base href="assets/"></head><body></body></html>'
    out = _with_base(body, "http://h/sub/final")
    assert b'<base href="http://h/sub/assets/">' in out and out.count(b"<base") == 1


def test_with_base_handles_single_quoted_unquoted_and_absolute_bases():
    assert b"http://h/sub/a/" in _with_base(b"<base href='a/'>", "http://h/sub/final")
    assert b"http://h/sub/a/" in _with_base(b"<base href=a/>", "http://h/sub/final")
    absolute = _with_base(b'<base href="http://other/x/">', "http://h/sub/final")
    assert b'href="http://other/x/"' in absolute


def test_with_base_injects_when_the_existing_base_has_no_href():
    out = _with_base(b'<head><base target="_blank"></head>', "http://h/sub/final")
    assert b'<base href="http://h/sub/final">' in out and b'target="_blank"' in out


def test_redirected_iframe_with_a_base_uri_csp_still_resolves_against_its_final_url(server, tmp_path):
    Handler.pic_hits.clear()
    r = collect(f"http://{server}/hopframe3", tmp_path)
    assert r.status == "ok"
    # With `base-uri 'none'` kept, Chromium would ignore the injected <base> and request /pic.png.
    assert Handler.pic_hits == ["/sub/pic.png"]


def test_relax_base_uri_drops_only_that_directive():
    assert _relax_base_uri("base-uri 'none'; img-src 'self'") == "img-src 'self'"
    assert _relax_base_uri("default-src 'self'; BASE-URI 'self'") == "default-src 'self'"
    assert _relax_base_uri("default-src 'self'") == "default-src 'self'"
    assert _relax_base_uri("base-uri 'none'") == ""


def test_a_page_that_auto_submits_a_form_does_not_get_its_post_sent(server, tmp_path):
    Handler.post_hits.clear()
    r = collect(f"http://{server}/postpage", tmp_path)
    assert r.status == "blocked" and "form_post_blocked" in r.error
    assert Handler.post_hits == []  # the POST never reached the server
    assert r.screenshot_path is None  # and the stub page was never captured as if it were the site


def test_a_form_submitted_after_the_settle_window_is_still_reported_as_a_post(server, tmp_path):
    Handler.post_hits.clear()
    r = collect(f"http://{server}/latepost", tmp_path)
    assert r.status == "blocked" and "form_post_blocked" in r.error
    assert Handler.post_hits == []


def test_a_form_posted_into_an_iframe_is_not_sent_and_does_not_void_the_capture(server, tmp_path):
    Handler.post_hits.clear()
    r = collect(f"http://{server}/iframepost", tmp_path)
    assert r.status == "ok" and r.screenshot_path
    assert Handler.post_hits == []


def test_capture_homepage_refuses_a_page_that_auto_submits_a_form(server, tmp_path):
    from scanner.diagnostics.guard import FormPostBlocked
    Handler.post_hits.clear()
    out = tmp_path / "post.jpg"
    with pytest.raises(FormPostBlocked):
        capture_homepage(f"http://{server}/postpage", out, crawler=RecordingCrawler())
    assert not out.exists() and Handler.post_hits == []


def test_relax_base_uri_keeps_every_other_comma_joined_policy():
    assert _relax_base_uri("base-uri 'none', script-src 'none'") == "script-src 'none'"
    assert _relax_base_uri("script-src 'none'; base-uri 'self', img-src 'self'") == "script-src 'none', img-src 'self'"
    assert _relax_base_uri("base-uri 'none', base-uri 'self'") == ""


def test_with_base_ignores_inert_base_text_in_comments_scripts_and_templates():
    for inert in (b'<!-- <base href="/old/"> -->',
                  b'<script>var s = \'<base href="/old/">\';</script>',
                  b'<template><base href="/old/"></template>'):
        out = _with_base(b"<html><head>" + inert + b"</head></html>", "http://h/sub/final")
        assert b'<base href="http://h/sub/final">' in out      # a real base was injected
        assert b'href="/old/"' in out                          # and the inert text was left alone


def test_with_base_still_rewrites_a_real_base_that_follows_a_comment():
    out = _with_base(b'<head><!-- <base href="/c/"> --><base href="assets/"></head>', "http://h/sub/final")
    assert b'<base href="http://h/sub/assets/">' in out and b'href="/c/"' in out and out.count(b"<base") == 2


def test_a_base_the_response_forbids_is_replaced_not_activated(server, tmp_path):
    Handler.pic_hits.clear()
    r = collect(f"http://{server}/hopframe5", tmp_path)
    assert r.status == "ok"
    # base-uri 'none' would natively reject the page's <base href="/evil/">; the image must load
    # relative to the real final URL (/sub/), not /evil/ (which would record nothing here).
    assert Handler.pic_hits == ["/sub/pic.png"]


def test_with_base_ignore_existing_neutralises_the_pages_base_and_injects_ours():
    out = _with_base(b'<head><base href="/evil/" target="_blank"></head>', "http://h/sub/final", ignore_existing=True)
    assert b"/evil/" not in out
    assert b'<base href="http://h/sub/final">' in out


def test_rewrite_self_replaces_only_the_self_keyword():
    out = _rewrite_self("default-src 'SELF' https://cdn.x; img-src 'self' data:", "http://b.example:8080")
    assert out == "default-src http://b.example:8080 https://cdn.x; img-src http://b.example:8080 data:"


def test_rebase_headers_across_origins_keeps_the_policy_but_points_self_at_the_final_origin():
    headers = {"Content-Type": "text/html", "Content-Length": "9", "Content-Encoding": "gzip",
               "Content-Security-Policy": "img-src 'self'; base-uri 'none'"}
    out, had_base_uri = _rebase_headers(headers, "http://a.example/x", "http://b.example/y")
    assert out == {"Content-Type": "text/html", "Content-Security-Policy": "img-src http://b.example"}
    assert had_base_uri is True


def test_rebase_headers_same_origin_leaves_self_alone():
    out, had_base_uri = _rebase_headers({"Content-Security-Policy": "img-src 'self'"},
                                        "http://a.example/x", "http://a.example/y")
    assert out == {"Content-Security-Policy": "img-src 'self'"} and had_base_uri is False


def test_base_uri_is_detected_by_directive_name_not_substring():
    assert _has_base_uri("default-src 'self'; Base-URI 'none'")
    assert _has_base_uri("img-src 'self', base-uri 'none'")
    assert not _has_base_uri("img-src https://cdn.example/base-uri")
    assert not _has_base_uri("img-src 'self'")
    headers = {"Content-Security-Policy": "img-src https://cdn.example/base-uri"}
    assert _rebase_headers(headers, "http://a/x", "http://a/y")[1] is False


def test_synthetic_base_goes_after_the_doctype_even_behind_a_bom_or_comment():
    tag = b'<base href="http://h/x">'
    assert _with_base(b"<!-- c --><!DOCTYPE html><html>", "http://h/x") == b"<!-- c --><!DOCTYPE html>" + tag + b"<html>"
    assert _with_base(b"\xef\xbb\xbf<!doctype html><html>", "http://h/x") == b"\xef\xbb\xbf<!doctype html>" + tag + b"<html>"
    assert _with_base(b"\xef\xbb\xbf<html>", "http://h/x") == b"\xef\xbb\xbf" + tag + b"<html>"


def test_synthetic_base_goes_after_an_xml_declaration_and_doctype():
    tag = b'<base href="http://h/x">'
    body = b'<?xml version="1.0"?>\n<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0//EN" "x"><html>'
    out = _with_base(body, "http://h/x")
    assert out == body.replace(b"<html>", tag + b"<html>")


def test_with_base_only_matches_the_real_href_attribute():
    out = _with_base(b'<base data-href="/tracking" href="a/">', "http://h/sub/final")
    assert out == b'<base href="http://h/sub/a/">'


class _FakeResp:
    def __init__(self, url, body=b"<html>x</html>"):
        self.url, self.status, self.headers = url, 200, {"content-type": "text/html"}
        self._body = body

    def body(self):
        return self._body


class _FakeRoute:
    def __init__(self, req_url, resp):
        self.request = type("R", (), {"url": req_url})()
        self._resp, self.fulfilled = resp, None

    def fetch(self, **kw):
        return self._resp

    def fulfill(self, **kw):
        self.fulfilled = kw

    def abort(self, *a):
        raise AssertionError("aborted")


def _navigator():
    return GuardedNavigator(type("C", (), {"route": lambda *a: None, "on": lambda *a: None})(), None, allow_fn=None, max_redirects=3)


def test_a_cross_origin_redirected_iframe_is_not_fulfilled_under_the_original_origin():
    nav = _navigator()
    route = _FakeRoute("http://a.test/f", _FakeResp("http://b.test/f", b"<script>steal()</script>"))
    nav._pass_through(route)
    assert nav.iframe_blocked == ["http://b.test/f"]
    assert b"steal" not in route.fulfilled["body"].encode()


def test_a_same_origin_redirected_iframe_is_still_rebased():
    nav = _navigator()
    route = _FakeRoute("http://a.test/f", _FakeResp("http://a.test/sub/g"))
    nav._pass_through(route)
    assert nav.iframe_blocked == [] and b"<base" in route.fulfilled["body"]


def test_with_base_ignores_href_text_inside_another_attribute_value():
    out = _with_base(b'<base data-info="href=/tracking" href="a/">', "http://h/sub/final")
    assert out == b'<base href="http://h/sub/a/">'


def test_origin_is_canonical():
    from scanner.diagnostics.guard import _origin
    assert _origin("https://Example.com/a") == _origin("https://example.com:443/b")
    assert _origin("http://h:80/") == _origin("http://h/")
    assert _origin("http://h:8080/") != _origin("http://h/")
    assert _origin("http://h/") != _origin("https://h/")


def test_with_base_preserves_unicode_in_a_parsed_base_href():
    ref = _with_base(b'<base href="caf&eacute;/">', "http://h/sub/final")
    raw = _with_base('<base href="café/">'.encode("utf-8"), "http://h/sub/final")
    expected = '<base href="http://h/sub/café/">'.encode("utf-8")
    assert ref == raw == expected


def test_a_base_the_csp_permits_is_kept_but_a_forbidden_one_is_replaced():
    from scanner.diagnostics.guard import _base_permitted
    body = b'<base href="/assets/">'
    selfonly = _base_permitted({"Content-Security-Policy": "base-uri 'self'"}, "http://a.test/f")
    out = _with_base(body, "http://a.test/sub/g", ignore_existing=True, keep_base=selfonly)
    assert out == b'<base href="http://a.test/assets/">'
    none = _base_permitted({"Content-Security-Policy": "base-uri 'none'"}, "http://a.test/f")
    out = _with_base(body, "http://a.test/sub/g", ignore_existing=True, keep_base=none)
    assert b"/assets/" not in out and b'<base href="http://a.test/sub/g">' in out
    cross = _base_permitted({"Content-Security-Policy": "base-uri 'self'"}, "http://a.test/f")
    assert cross("http://evil.test/") is False


def test_is_html_excludes_xhtml_and_other_types():
    from scanner.diagnostics.guard import _is_html
    assert _is_html({"content-type": "text/html; charset=utf-8"})
    assert not _is_html({"content-type": "application/xhtml+xml"})
    assert not _is_html({})


def test_with_base_ignores_a_base_inside_noscript():
    out = _with_base(b'<head><noscript><base href="/old/"></noscript></head>', "http://h/sub/final")
    assert b'<base href="http://h/sub/final">' in out and b'href="/old/"' in out
