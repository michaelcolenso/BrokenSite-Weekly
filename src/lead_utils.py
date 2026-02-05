"""
Lead utility helpers for tiering, marketing signals, and outreach pitch.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional


MARKETING_SIGNAL_REASONS = {"has_gtm", "has_fb_pixel", "has_gclid"}


def compute_lead_tier(score: int) -> str:
    """Convert numeric score into a lead tier label."""
    if score >= 80:
        return "hot"
    if score >= 60:
        return "warm"
    if score >= 40:
        return "cool"
    return "skip"


def parse_reasons(reasons: str | Iterable[str] | None) -> list[str]:
    if not reasons:
        return []
    if isinstance(reasons, str):
        return [r.strip() for r in reasons.split(",") if r.strip()]
    return [str(r).strip() for r in reasons if str(r).strip()]


def has_marketing_pixel(reasons: str | Iterable[str] | None) -> bool:
    tokens = parse_reasons(reasons)
    return any(token in MARKETING_SIGNAL_REASONS for token in tokens)


def primary_reason(reasons: str | Iterable[str] | None) -> Optional[str]:
    """Return the first non-marketing reason for suggested pitch."""
    for token in parse_reasons(reasons):
        if token in MARKETING_SIGNAL_REASONS:
            continue
        if token == "unverified":
            continue
        return token
    return None


def suggested_pitch_from_reasons(reasons: str | Iterable[str] | None) -> str:
    reason = primary_reason(reasons)
    if not reason:
        return "I spotted a few issues on your website that may be hurting conversions."

    if reason in ("ssl_error", "no_https"):
        return "I noticed your site shows as not secure — that warning drives customers away."
    if reason in ("no_viewport", "not_responsive"):
        return "I checked your site on mobile and it’s difficult to use — most customers are on phones."
    if reason.startswith("timeout") or reason.startswith("slow_response"):
        return "Your site takes too long to load, which hurts rankings and conversions."
    if reason.startswith("server_error_") or reason.startswith("http_") or reason in ("unreachable", "dns_failed"):
        return "Your site is returning an error for visitors, which means lost leads right now."
    if reason in ("parked_domain", "under_construction"):
        return "Your domain is showing a placeholder page instead of your business."
    if reason.startswith("copyright_"):
        return "Your site footer shows an outdated copyright year, which signals neglect to visitors."
    if reason in ("missing_meta_description", "missing_h1", "generic_title"):
        return "Your site is missing basic SEO elements that help customers find you."
    if reason.startswith("outdated_"):
        return "Your site uses outdated technology that can break on modern browsers."
    if reason.startswith("diy_"):
        return "Your site is on a template builder that often limits SEO and performance."

    return "I noticed a few website issues that could be costing you customers."


def compute_exclusive_until(days: int = 7) -> datetime:
    return datetime.utcnow() + timedelta(days=days)
