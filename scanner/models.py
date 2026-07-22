"""Business and scan models for the v1 scanner."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Business:
    business_name: str
    vertical: str
    phone: str
    address: str
    domain: str


@dataclass(frozen=True)
class PageFetch:
    url: str
    status_code: int | None
    html: str = ""
    blocked: bool = False
    error: str | None = None
