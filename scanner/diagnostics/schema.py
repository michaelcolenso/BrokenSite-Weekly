"""Structured output for the diagnostic collector."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

ContactMethod = Literal["phone", "email", "form"]

# status values:
#   ok        page loaded (HTTP status may still be 4xx/5xx; see reachable)
#   blocked   robots.txt disallows the URL; nothing was fetched
#   error     navigation failed (DNS, TLS, timeout, ...); see `error`
DiagnosticStatus = Literal["ok", "blocked", "error"]


class DiagnosticReport(BaseModel):
    domain: str
    url: str
    final_url: Optional[str] = None
    status: DiagnosticStatus
    error: Optional[str] = None

    reachable: bool = False
    http_status: int = 0  # 0 = no HTTP response received
    redirect_chain: list[str] = Field(default_factory=list)

    ssl_valid: bool = False
    ssl_expires_at: Optional[str] = None  # ISO-8601 UTC
    ssl_error: Optional[str] = None

    mixed_content_count: int = 0
    mobile_viewport_overflow: bool = False
    broken_asset_count: int = 0
    broken_assets: list[str] = Field(default_factory=list)  # capped sample
    contact_methods_found: list[ContactMethod] = Field(default_factory=list)

    screenshot_path: Optional[str] = None  # desktop 1440x900, above the fold
    mobile_screenshot_path: Optional[str] = None  # mobile 390x844
    mobile_redirect_url: Optional[str] = None  # navigation the site attempted on resize (vetoed)
    execution_time_ms: int = 0
