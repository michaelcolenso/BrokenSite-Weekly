"""Guarded main-frame navigation for headless-browser page loads.

HANDOFF hard rules 3 and 4 apply to every *document* load a browser makes: the initial URL, each
redirect hop and any client-side (script / meta-refresh) navigation must be robots-checked, paced
on its own host and counted. Chromium would follow redirects internally without telling us, so
this class takes over main-frame navigations: each one is fetched with redirects off, vetted, and
handed to the browser; redirects are followed here, one routed navigation per hop.

Only top-level (main-frame) navigations are guarded. Every other request the page makes while it
renders (sub-resources of any type, XHR/fetch, media, iframe documents) is the screenshot
exception in HANDOFF hard rule 4 and is not paced or robots-checked.

Used by the diagnostics collector and by scanner.screenshot. Callers must create their browser
context with service_workers="block": requests handled by a service worker never reach the route
handler, so a worker could serve pages past these checks.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse


# A doctype may follow a BOM and any whitespace/comments; a start tag placed before it would put the
# document in quirks mode.
_DOCTYPE = re.compile(rb"(?:\xef\xbb\xbf)?(?:\s|<!--.*?-->|<\?xml[^>]*>)*<!doctype[^>]*>", re.I | re.S)
_BOM = b"\xef\xbb\xbf"
_HREF_ATTR = re.compile(rb"((?<=[\s\"'/])href\s*=\s*)(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.I)


class _BaseFinder(HTMLParser):
    """Finds the first *active* <base href>: not inside a comment, script/style, <template> or
    <noscript>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.inert_depth = 0  # inside <template> or <noscript> (inert with scripting enabled)
        self.found = None  # (line, column, raw tag text)

    def handle_starttag(self, tag, attrs):
        if tag in ("template", "noscript"):
            self.inert_depth += 1
        elif tag == "base" and self.found is None and self.inert_depth == 0:
            if any(k == "href" and v is not None for k, v in attrs):
                self.found = (*self.getpos(), self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag in ("template", "noscript") and self.inert_depth:
            self.inert_depth -= 1


def _find_active_base(body: bytes):
    """Return (offset, raw tag bytes) of the document's active <base href>, or None.
    The body is parsed as latin-1 so every character is exactly one byte and offsets map back."""
    text = body.decode("latin-1")
    finder = _BaseFinder()
    try:
        finder.feed(text)
        finder.close()
    except Exception:  # noqa: BLE001 - malformed markup: treat as having no base
        return None
    if finder.found is None:
        return None
    line, col, raw = finder.found
    offset = sum(len(l) + 1 for l in text.split("\n")[: line - 1]) + col
    raw_bytes = raw.encode("latin-1")
    if body[offset: offset + len(raw_bytes)] != raw_bytes:
        return None
    return offset, raw_bytes


def _with_base(body: bytes, url: str, ignore_existing: bool = False) -> bytes:
    """Make an HTML document that is committed under a different URL than the one it was fetched
    from resolve relative URLs against `url`. An active <base href> is resolved against `url` (it
    was written relative to where the page really lives); otherwise <base href=url> is injected
    after any doctype, so quirks mode is not triggered. With `ignore_existing`, an existing
    <base href> is neutralised (its href removed) and ours is injected instead; used when the
    response's own policy restricts <base>, which it would natively enforce on the page's base."""
    hit = _find_active_base(body)
    if hit and ignore_existing:
        offset, raw = hit
        m = _HREF_ATTR.search(raw)
        if m:
            body = body[:offset] + raw[:m.start()] + raw[m.end():] + body[offset + len(raw):]
    elif hit:
        offset, raw = hit
        m = _HREF_ATTR.search(raw)
        if m:
            original = (m.group(2) or m.group(3) or m.group(4) or b"").decode("utf-8", "replace")
            resolved = html.escape(urljoin(url, html.unescape(original)), quote=True).encode("utf-8")
            new_raw = raw[:m.start()] + m.group(1) + b'"' + resolved + b'"' + raw[m.end():]
            return body[:offset] + new_raw + body[offset + len(raw):]
    tag = b'<base href="' + html.escape(url, quote=True).encode("utf-8") + b'">'
    d = _DOCTYPE.match(body)
    at = d.end() if d else (len(_BOM) if body.startswith(_BOM) else 0)
    return body[:at] + tag + body[at:]


def _is_html(headers: dict) -> bool:
    """text/html only: an HTML <base> would break an XHTML (XML) document."""
    return headers.get("content-type", "").lower().split(";")[0].strip() == "text/html"


def _has_base_uri(csp: str) -> bool:
    """True if any policy in the header has a base-uri directive (by directive name)."""
    return any(d.split(None, 1)[0].lower() == "base-uri"
               for policy in csp.split(",") for d in policy.split(";") if d.strip())


def _relax_base_uri(csp: str) -> str:
    """Drop the base-uri directive from a Content-Security-Policy header value. Chromium would
    otherwise reject the <base> element _with_base() injects; the directive governs nothing else.
    Several policies may be comma-joined in one value; each keeps its other directives."""
    policies = []
    for policy in csp.split(","):
        kept = [d.strip() for d in policy.split(";")
                if d.strip() and not d.strip().lower().startswith("base-uri")]
        if kept:
            policies.append("; ".join(kept))
    return ", ".join(policies)


_SELF = re.compile(r"'self'", re.I)


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _rewrite_self(csp: str, origin: str) -> str:
    """Replace the 'self' source with a concrete origin."""
    return _SELF.sub(origin, csp)


def _rebase_headers(headers: dict, original_url: str, final_url: str):
    """Headers for a response committed under `original_url` that was really served from
    `final_url` (after redirects). Drops hop-by-hop/length headers (the body is re-sent), removes
    the CSP base-uri directive (it would make Chromium reject our injected <base>), and, if the
    redirect crossed origins, rewrites 'self' to the final origin so the policy still allows the
    resources the final document actually loads. Returns (headers, had_base_uri)."""
    out = {k: v for k, v in headers.items()
           if k.lower() not in ("content-length", "content-encoding", "transfer-encoding")}
    had_base_uri = False
    cross_origin = _origin(original_url) != _origin(final_url)
    for key in [k for k in out if k.lower() == "content-security-policy"]:
        value = out[key]
        had_base_uri = had_base_uri or _has_base_uri(value)
        value = _relax_base_uri(value)
        if value and cross_origin:
            value = _rewrite_self(value, _origin(final_url))
        if value:
            out[key] = value
        else:
            del out[key]
    return out, had_base_uri


class RobotsBlocked(Exception):
    """A navigation (initial or redirect hop) targets a URL robots.txt disallows."""


class FormPostBlocked(Exception):
    """The page tried to submit a form (a top-level non-GET navigation). We never send those."""


class GuardedNavigator:
    """Installs a route handler on `context` and drives `page` through guarded navigations.

    `allow_fn(url) -> bool` is the robots.txt check (it must pace its own robots.txt fetches, as
    PoliteCrawler.allowed does). `wait_fn(netloc)` reserves a request slot on, and
    `record_fn(netloc)` stamps, each navigation's own host.
    """

    def __init__(self, context, page, *, allow_fn, wait_fn=None, record_fn=None,
                 nav_timeout_ms=15_000, settle_ms=500, max_redirects=10, wait_until="load"):
        self.page = page
        self.allow_fn = allow_fn
        self.wait_fn = wait_fn
        self.record_fn = record_fn
        self.nav_timeout_ms = nav_timeout_ms
        self.settle_ms = settle_ms
        self.max_redirects = max_redirects
        self.wait_until = wait_until
        self.blocked: list[str] = []          # navigations vetoed by robots.txt
        self.route_errors: list[str] = []     # fetch failures inside the handler
        self.pending: list[tuple[str, str]] = []  # (redirecting URL, redirect target)
        self.frozen = False
        self.frozen_hits: list[str] = []      # navigations attempted after freeze() (vetoed)
        self.popup_hits: list[str] = []       # navigations of other pages (popups) (vetoed)
        self.iframe_post_hits: list[str] = []  # non-GET iframe navigations (vetoed, capture continues)
        self.post_hits: list[str] = []        # top-level non-GET navigations (form posts) (vetoed)
        context.route("**/*", self._on_route)
        # The captured page is the only page that may navigate: close anything it opens (popups).
        context.on("page", lambda popup: self._close_popup(popup))

    @staticmethod
    def _close_popup(popup) -> None:
        try:
            popup.close()
        except Exception:  # noqa: BLE001 - already closed
            pass

    def freeze(self) -> None:
        """Veto every further main-frame navigation (late timers, resize handlers)."""
        self.frozen = True

    def _on_route(self, route) -> None:
        req = route.request
        try:
            is_nav = req.is_navigation_request()
        except Exception:  # noqa: BLE001
            route.continue_()
            return
        try:
            frame = req.frame
        except Exception:  # noqa: BLE001 - Playwright: no frame yet (a popup's first navigation) or a worker
            frame = None
        if frame is None:
            if is_nav:
                # A popup's initial navigation: nothing outside the captured page needs fetching
                # for a screenshot, and its redirect state must not leak into the captured page's.
                self.popup_hits.append(req.url)
                route.abort("aborted")
            else:
                route.continue_()  # e.g. a service-worker-owned request
            return
        main_frame_nav = is_nav and frame.parent_frame is None
        if is_nav and frame.page is not self.page:
            self.popup_hits.append(req.url)  # another page's navigation: veto, same reasoning
            route.abort("aborted")
            return
        if self.frozen and main_frame_nav:
            # After the settle window every capture must describe one document. ERR_ABORTED keeps
            # the current document; the default error code would commit an error page.
            # A late auto-submitted form is recorded as a POST so the collector reports
            # form_post_blocked instead of a screenshot of the pre-submit document.
            if req.method != "GET":
                self.post_hits.append(req.url)
            else:
                self.frozen_hits.append(req.url)
            route.abort("aborted")
            return
        if main_frame_nav and req.method != "GET":
            # A page that submits a form on its own. A screenshot never submits forms (and a
            # 307/308 redirect of a POST could not be followed faithfully by a GET), so the request
            # is never sent. It is answered with a stub document rather than aborted or 204'd,
            # because either of those leaves a pending goto() hanging until its timeout; goto()
            # then raises FormPostBlocked so the stub is never mistaken for the page.
            self.post_hits.append(req.url)
            route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>blocked</title>")
            return
        # Child-frame (iframe) documents are navigations too, but the screenshot exception in
        # HANDOFF rule 4 treats them as embedded content, so only the main frame is guarded.
        if is_nav and not main_frame_nav and req.method != "GET":
            # Same "never submit a form" policy as the top level, but a hidden-iframe post (ad or
            # analytics frame) shouldn't void the whole capture: the frame gets a blank document.
            self.iframe_post_hits.append(req.url)
            route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>blocked</title>")
            return
        if self.allow_fn and is_nav and not main_frame_nav:
            self._pass_through(route)
            return
        if not (self.allow_fn and main_frame_nav):
            route.continue_()
            return
        # route.continue_() follows redirects inside Chromium without calling this handler again.
        # Fetch navigations with redirects off and hand the response to the browser.
        host = urlparse(req.url).netloc.lower()
        try:
            if not self.allow_fn(req.url):
                self.blocked.append(req.url)
                route.abort()
                return
            if self.wait_fn:
                self.wait_fn(host)
            try:
                resp = route.fetch(max_redirects=0)
            finally:
                if self.record_fn:
                    self.record_fn(host)
            location = resp.headers.get("location")
            if 300 <= resp.status < 400 and location:
                # Chromium would follow further hops without calling this handler, so hand it a
                # blank page and let goto() issue the next hop as its own routed navigation
                # (aborting instead leaves an error page that races it).
                self.pending.append((req.url, urljoin(req.url, location)))
                route.fulfill(status=200, content_type="text/html", body="")
                return
            route.fulfill(response=resp)
        except Exception as exc:  # noqa: BLE001 - fail closed, surface via the goto error
            self.route_errors.append(f"{type(exc).__name__}: {exc}")
            route.abort()

    def _pass_through(self, route) -> None:
        """Serve an embedded (iframe) document unguarded, but through route.fetch/fulfill.

        route.continue_() is not an option: once the top-level document has been fulfilled by this
        handler, Chromium never finishes a cross-origin iframe navigation that is continued, and
        every later screenshot times out. Fulfilling a 3xx fails the same way (Chromium follows it
        natively), and neither a script nor a meta refresh can drive a sandboxed iframe. So
        redirects are followed inside route.fetch() (capped at max_redirects, so a loop makes a
        bounded number of requests). The body is then committed under the original iframe URL;
        to keep relative URLs resolving against the real page, a <base href> for the final URL is
        injected into HTML responses. The frame's own URL and origin stay those of the original
        request, which is acceptable for an embedded frame in a screenshot.
        """
        req = route.request
        try:
            resp = route.fetch(max_redirects=self.max_redirects)
            if resp.url != req.url and _is_html(resp.headers):
                headers, had_base_uri = _rebase_headers(resp.headers, req.url, resp.url)
                body = _with_base(resp.body(), resp.url, ignore_existing=had_base_uri)
                route.fulfill(status=resp.status, headers=headers, body=body)
            else:
                route.fulfill(response=resp)
        except Exception:  # noqa: BLE001 - a frame that can't load stays blank; the page still captures
            route.abort()

    def goto(self, url: str) -> list[str]:
        """Load `url`, following redirects hop by hop and letting immediate client-side
        navigations land. Returns the redirect chain. Raises RobotsBlocked, FormPostBlocked, or RuntimeError for
        a handler failure or too many redirects; other navigation errors propagate unchanged."""
        chain: list[str] = []
        current = url
        for _ in range(self.max_redirects + 1):
            self.pending.clear()
            try:
                self.page.goto(current, wait_until=self.wait_until, timeout=self.nav_timeout_ms)
            except Exception:
                if self.blocked:
                    raise RobotsBlocked(self.blocked[0]) from None
                if self.route_errors:
                    raise RuntimeError(self.route_errors[0]) from None
                raise
            if not self.pending:
                # Let immediate client-side navigations (script / meta refresh) land; each is
                # robots-checked and paced by the handler, and may itself be a redirect.
                self.page.wait_for_timeout(self.settle_ms)
            if self.pending and not self.blocked:
                src, current = self.pending[0]
                chain.append(src)
                continue
            break
        else:
            raise RuntimeError("too_many_redirects")
        if self.blocked:  # a script/meta-refresh navigation was vetoed during load
            raise RobotsBlocked(self.blocked[0])
        if self.post_hits:
            raise FormPostBlocked(self.post_hits[0])
        return chain
