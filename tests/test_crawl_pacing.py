"""PoliteCrawler.fetch must honour HANDOFF rule 4 for its own robots.txt request too:
robots.txt counts as a request, so the page fetch waits a full delay after it."""

from types import SimpleNamespace

import pytest

from scanner import crawl
from scanner.crawl import DOMAIN_DELAY_SECONDS, PoliteCrawler


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeSession:
    def __init__(self, clock, robots_text="User-agent: *\nAllow: /\n"):
        self.clock = clock
        self.headers = {}
        self.robots_text = robots_text
        self.calls = []  # (kind, url, fake_time)

    def get(self, url, timeout=None):
        self.calls.append(("robots", url, self.clock.now))
        return SimpleNamespace(status_code=200, text=self.robots_text, headers={})

    def request(self, method, url, timeout=None, allow_redirects=True):
        self.calls.append(("page", url, self.clock.now))
        return SimpleNamespace(status_code=200, text="<html></html>", encoding=None, url=url,
                               headers={"content-type": "text/html"})


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(crawl, "time", c)
    return c


def make(clock, **kw):
    return PoliteCrawler(session=FakeSession(clock, **kw))


def test_page_fetch_waits_a_full_delay_after_the_robots_fetch(clock):
    crawler = make(clock)
    result = crawler.fetch("https://example.com/")
    assert result.status_code == 200
    (kind1, _, t1), (kind2, _, t2) = crawler.session.calls
    assert (kind1, kind2) == ("robots", "page")
    assert t2 - t1 >= DOMAIN_DELAY_SECONDS


def test_second_fetch_on_same_domain_uses_cached_robots_and_still_waits(clock):
    crawler = make(clock)
    crawler.fetch("https://example.com/")
    crawler.fetch("https://example.com/about")
    kinds = [c[0] for c in crawler.session.calls]
    assert kinds == ["robots", "page", "page"]
    t_page1, t_page2 = crawler.session.calls[1][2], crawler.session.calls[2][2]
    assert t_page2 - t_page1 >= DOMAIN_DELAY_SECONDS


def test_disallowed_url_makes_no_page_request(clock):
    crawler = make(clock, robots_text="User-agent: *\nDisallow: /\n")
    result = crawler.fetch("https://example.com/")
    assert result.blocked and result.error == "robots_disallow"
    assert [c[0] for c in crawler.session.calls] == ["robots"]


def test_different_domains_are_not_delayed_by_each_other(clock):
    crawler = make(clock)
    crawler.fetch("https://a.example/")
    start = clock.now
    crawler.fetch("https://b.example/")
    robots_b = [c for c in crawler.session.calls if c[0] == "robots" and "b.example" in c[1]][0]
    assert robots_b[2] == start  # first request to b.example is immediate
