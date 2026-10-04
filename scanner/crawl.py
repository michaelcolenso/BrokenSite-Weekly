"""Polite fetch logic for the BSW scanner.

Rules implemented here are intentionally conservative:
- honest User-Agent
- robots.txt checked and cached per domain for 24 hours
- one request per domain every ten seconds
- four concurrent domains globally
- crawler budgets are constants used by orchestration code
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from threading import BoundedSemaphore, Lock
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

USER_AGENT = "BSW-Scanner/1.0 (+https://brokensiteweekly.com/bot)"
ROBOTS_CACHE_SECONDS = 24 * 60 * 60
DOMAIN_DELAY_SECONDS = 10.0
MAX_CONCURRENT_DOMAINS = 4
MAX_REQUESTS_PER_PAGE = 2
MAX_PAGES_PER_SITE = 3
REQUEST_TIMEOUT_SECONDS = 15
MAX_ROBOTS_REDIRECTS = 5


@dataclass
class FetchResult:
    url: str
    status_code: Optional[int]
    text: str = ""
    content_type: str = ""
    final_url: str = ""
    error: Optional[str] = None
    blocked: bool = False


@dataclass
class RobotsCacheEntry:
    parser: RobotFileParser
    fetched_at: float


@dataclass
class PoliteCrawler:
    session: requests.Session = field(default_factory=requests.Session)
    _robots: dict[str, RobotsCacheEntry] = field(default_factory=dict)
    _last_request_at: dict[str, float] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)
    _domain_locks: dict[str, Lock] = field(default_factory=dict)
    _last_sent_at: dict[str, float] = field(default_factory=dict)
    _domain_slots: BoundedSemaphore = field(default_factory=lambda: BoundedSemaphore(MAX_CONCURRENT_DOMAINS))

    def __post_init__(self) -> None:
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _domain_lock(self, domain: str) -> Lock:
        with self._lock:
            return self._domain_locks.setdefault(domain, Lock())

    def fetch(self, url: str, *, allow_binary: bool = False, method: str = "GET") -> FetchResult:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if not domain:
            return FetchResult(url=url, status_code=None, error="invalid_url")

        # One in-flight fetch per domain: serializes the robots-cache check, pacing wait, request
        # and record, so concurrent threads can't both fetch robots.txt or both skip the delay.
        # Taken before the slot so threads queued on one domain don't hold global slots.
        with self._domain_lock(domain), self._domain_slots:
            # robots.txt is a request to the domain too (HANDOFF rule 4); _fetch_robots paces and
            # records it, so the page fetch below waits a full delay after it.
            allowed = self.allowed(url)
            if not allowed:
                return FetchResult(url=url, status_code=None, error="robots_disallow", blocked=True)

            self._wait_for_domain(domain)
            try:
                response = self.session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
            except requests.RequestException as exc:
                return FetchResult(url=url, status_code=None, error=str(exc))
            finally:
                self._touch(domain)

        content_type = response.headers.get("content-type", "")
        text = ""
        if allow_binary or "text" in content_type or "html" in content_type or not content_type:
            response.encoding = response.encoding or "utf-8"
            text = response.text
        return FetchResult(
            url=url,
            status_code=response.status_code,
            text=text,
            content_type=content_type,
            final_url=response.url,
        )

    def allowed(self, url: str) -> bool:
        parser = self._get_robots(url)
        return parser.can_fetch(USER_AGENT, url)

    def _get_robots(self, url: str) -> RobotFileParser:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        now = time.time()
        entry = self._robots.get(base)
        if entry and now - entry.fetched_at < ROBOTS_CACHE_SECONDS:
            return entry.parser

        parser = RobotFileParser()
        robots_url = f"{base}/robots.txt"
        parser.set_url(robots_url)
        try:
            response = self._fetch_robots(robots_url)
            if response.status_code >= 400:
                parser.parse([])
            else:
                parser.parse(response.text.splitlines())
                if response.headers.get("last-modified"):
                    try:
                        parser.modified()
                    except Exception:
                        pass
        except requests.RequestException:
            parser.parse([])
        self._robots[base] = RobotsCacheEntry(parser=parser, fetched_at=now)
        return parser

    def _fetch_robots(self, robots_url: str):
        """GET robots.txt, following redirects by hand so every hop is paced and recorded on the
        host it actually hits (requests would send the whole chain back-to-back, and a redirect to
        another host, e.g. apex -> www, would never be paced at all)."""
        for _ in range(MAX_ROBOTS_REDIRECTS + 1):
            host = urlparse(robots_url).netloc.lower()
            self._wait_for_domain(host)
            try:
                response = self.session.get(robots_url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=False)
            finally:
                self.record_request(host)
            location = response.headers.get("location")
            if 300 <= response.status_code < 400 and location:
                robots_url = urljoin(robots_url, location)
                if urlparse(robots_url).scheme not in ("http", "https"):
                    raise requests.RequestException("robots.txt redirect to non-http(s) URL")
                continue
            return response
        raise requests.TooManyRedirects("robots.txt exceeded redirect limit")

    def wait_for_domain(self, domain: str) -> None:
        """Public pacing hook for auxiliary requests (see DomainThrottledSession)."""
        self._wait_for_domain(domain)

    def record_request(self, domain: str) -> None:
        """Record that a request to `domain` just completed (auxiliary requests)."""
        self._touch(domain)

    def _touch(self, domain: str) -> None:
        # max(): never move the timestamp back past a slot another caller has reserved.
        with self._lock:
            self._last_request_at[domain] = max(self._last_request_at.get(domain, 0.0), time.monotonic())

    def _wait_for_domain(self, domain: str) -> None:
        """Wait for this caller's request slot on `domain`, then claim it.

        Reservation (under the lock) queues concurrent callers one delay apart, including ones
        arriving from another domain's robots.txt redirect. Waking up is re-validated against the
        last request actually released (`_last_sent_at`): if a later reservation fired first because
        this waiter was delayed, it waits out that request's delay too. Call once per request,
        immediately before sending it; a second call without a request in between waits again.
        """
        self._await_slot(domain, self._reserve_slot(domain))

    def _reserve_slot(self, domain: str) -> float:
        with self._lock:
            now = time.monotonic()
            last = self._last_request_at.get(domain)
            start = now if last is None else max(now, last + DOMAIN_DELAY_SECONDS)
            self._last_request_at[domain] = start
        return start

    def _await_slot(self, domain: str, start: float) -> None:
        while True:
            delay = start - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            with self._lock:
                now = time.monotonic()
                sent = self._last_sent_at.get(domain)
                if sent is None or now - sent >= DOMAIN_DELAY_SECONDS:
                    self._last_sent_at[domain] = now
                    return
                start = sent + DOMAIN_DELAY_SECONDS  # an earlier-woken request got in first


class DomainThrottledSession(requests.Session):
    """requests.Session that enforces the same per-domain politeness delay as the crawler.

    Auxiliary check requests (form-action probes, image HEADs) bypass
    PoliteCrawler.fetch(), so checks receive this session instead of a bare
    requests.Session. Every outgoing request waits for the shared per-domain
    10-second pacing window and carries the honest scanner User-Agent.
    """

    def __init__(self, crawler: PoliteCrawler) -> None:
        super().__init__()
        self.headers.update({"User-Agent": USER_AGENT})
        self._crawler = crawler

    def request(self, method, url, **kwargs):  # noqa: D102 - pacing wrapper
        domain = urlparse(url).netloc.lower()
        if domain:
            self._crawler.wait_for_domain(domain)
        try:
            return super().request(method, url, **kwargs)
        finally:
            if domain:
                self._crawler.record_request(domain)
