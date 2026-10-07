"""National-chain and junk-URL filters for lead qualification."""

from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import urlparse

from .config import DATA_DIR

_BLOCKLIST: set[str] | None = None

JUNK_HOSTS = {
    "business.google.com",
    "www.google.com",
    "maps.google.com",
    "google.com",
}


def load_blocklist(path: Path | None = None) -> set[str]:
    csv_path = path or (DATA_DIR / "national_chain_blocklist.csv")
    blocked: set[str] = set()
    if not csv_path.exists():
        return blocked
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            domain = (row.get("domain") or "").strip().lower()
            if domain.startswith("www."):
                domain = domain[4:]
            if domain:
                blocked.add(domain)
    return blocked


def get_blocklist() -> set[str]:
    global _BLOCKLIST
    if _BLOCKLIST is None:
        _BLOCKLIST = load_blocklist()
    return _BLOCKLIST


def host_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_junk_website(url: str | None) -> bool:
    if not url:
        return False
    lowered = url.lower()
    if "business.google.com/create" in lowered:
        return True
    host = host_from_url(url)
    return host in JUNK_HOSTS or host.endswith(".google.com")


def is_blocked(url: str | None, blocked: set[str] | None = None) -> bool:
    if not url:
        return False
    blocked = blocked if blocked is not None else get_blocklist()
    host = host_from_url(url)
    if not host:
        return False
    if host in blocked:
        return True
    return any(host.endswith("." + domain) for domain in blocked)
