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
from threading import BoundedSemaphore, Lock
from typing import Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

USER_AGENT = "BSW-Scanner/1.0 (+https://brokensiteweekly.com/bot)"
ROBOTS_CACHE_SECONDS = 24 * 60 * 60
DOMAIN_DELAY_SECONDS = 10.0
MAX_CONCURRENT_DOMAINS = 4
MAX_REQUESTS_PER_PAGE = 2
MAX_PAGES_PER_SITE = 3
REQUEST_TIMEOUT_SECONDS = 15


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
    _domain_slots: BoundedSemaphore = field(default_factory=lambda: BoundedSemaphore(MAX_CONCURRENT_DOMAINS))

    def __post_init__(self) -> None:
        self.session.headers.update({"User-Agent": USER_AGENT})

    def fetch(self, url: str, *, allow_binary: bool = False, method: str = "GET") -> FetchResult:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if not domain:
            return FetchResult(url=url, status_code=None, error="invalid_url")

        if not self.allowed(url):
            return FetchResult(url=url, status_code=None, error="robots_disallow", blocked=True)

        with self._domain_slots:
            self._wait_for_domain(domain)
            try:
                response = self.session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
            except requests.RequestException as exc:
                return FetchResult(url=url, status_code=None, error=str(exc))
            finally:
                with self._lock:
                    self._last_request_at[domain] = time.monotonic()

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
            response = self.session.get(robots_url, timeout=REQUEST_TIMEOUT_SECONDS)
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

    def _wait_for_domain(self, domain: str) -> None:
        with self._lock:
            last = self._last_request_at.get(domain)
        if last is None:
            return
        delay = DOMAIN_DELAY_SECONDS - (time.monotonic() - last)
        if delay > 0:
            time.sleep(delay)
