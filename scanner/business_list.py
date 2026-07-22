"""Load and dedupe metro business CSVs for scanner input."""

from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import urlparse

from scanner.models import Business

REQUIRED_COLUMNS = {"business_name", "vertical", "phone", "address", "domain"}


def normalize_domain(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    domain = parsed.netloc or parsed.path
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.rstrip("/")


def load_blocklist(path: str | Path) -> set[str]:
    blocklist: set[str] = set()
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            domain = normalize_domain(row.get("domain", ""))
            if domain:
                blocklist.add(domain)
    return blocklist


def load_businesses(path: str | Path, *, blocklist_path: str | Path | None = None) -> list[Business]:
    blocklist = load_blocklist(blocklist_path) if blocklist_path else set()
    seen: set[str] = set()
    businesses: list[Business] = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"business CSV missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            domain = normalize_domain(row.get("domain", ""))
            if not domain or domain in seen or domain in blocklist:
                continue
            seen.add(domain)
            businesses.append(Business(
                business_name=(row.get("business_name") or "").strip(),
                vertical=(row.get("vertical") or "").strip(),
                phone=(row.get("phone") or "").strip(),
                address=(row.get("address") or "").strip(),
                domain=domain,
            ))
    return businesses
