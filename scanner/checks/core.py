"""Shared check contract."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CheckResult:
    """Result returned by every scanner check."""

    check_id: str
    triggered: bool
    evidence: str
    severity: int
