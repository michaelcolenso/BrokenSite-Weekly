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

import json
from urllib.parse import urljoin, urlparse


class RobotsBlocked(Exception):
    """A navigation (initial or redirect hop) targets a URL robots.txt disallows."""


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
        self._child_hops: dict = {}           # iframe -> redirect hops served so far (loop cap)
        context.route("**/*", self._on_route)

    def freeze(self) -> None:
        """Veto every further main-frame navigation (late timers, resize handlers)."""
        self.frozen = True

    def _on_route(self, route) -> None:
        req = route.request
        try:
            is_nav = req.is_navigation_request()
            main_frame_nav = is_nav and req.frame.parent_frame is None
        except Exception:  # noqa: BLE001 - e.g. a service-worker-owned request has no frame
            route.continue_()
            return
        # Child-frame (iframe) documents are navigations too, but the screenshot exception in
        # HANDOFF rule 4 treats them as embedded content, so only the main frame is guarded.
        if self.allow_fn and is_nav and not main_frame_nav:
            self._pass_through(route)
            return
        if self.frozen and main_frame_nav:
            # After the settle window every capture must describe one document. ERR_ABORTED keeps
            # the current document; the default error code would commit an error page.
            self.frozen_hits.append(req.url)
            route.abort("aborted")
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
        every later screenshot times out. The same happens if a 3xx is simply fulfilled (Chromium
        follows it natively). Following redirects inside route.fetch() would load the right body
        but commit it under the original URL. So a 3xx is answered with a tiny page that navigates
        the frame to the target: the next hop is a fresh, routed navigation and commits under its
        real URL. Hops per frame are capped so a redirect loop cannot hammer a site.
        """
        req = route.request
        try:
            resp = route.fetch(max_redirects=0)
            location = resp.headers.get("location")
            if 300 <= resp.status < 400 and location:
                target = urljoin(req.url, location)
                hops = self._child_hops.get(req.frame, 0) + 1
                if urlparse(target).scheme not in ("http", "https") or hops > self.max_redirects:
                    route.abort()
                    return
                self._child_hops[req.frame] = hops
                literal = json.dumps(target).replace("<", "\\u003c")  # safe inside <script>
                route.fulfill(status=200, content_type="text/html",
                              body=f"<script>location.replace({literal})</script>")
                return
            route.fulfill(response=resp)
        except Exception:  # noqa: BLE001 - a frame that can't load stays blank; the page still captures
            route.abort()

    def goto(self, url: str) -> list[str]:
        """Load `url`, following redirects hop by hop and letting immediate client-side
        navigations land. Returns the redirect chain. Raises RobotsBlocked, or RuntimeError for
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
        return chain
