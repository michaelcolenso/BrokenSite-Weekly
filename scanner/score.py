"""Severity scoring and lead tiering."""

from scanner.checks import CheckResult


def lead_severity(checks: list[CheckResult]) -> int:
    triggered = [check for check in checks if check.triggered]
    if not triggered:
        return 0
    severity = max(check.severity for check in triggered)
    if len(triggered) >= 3:
        severity += 1
    return min(severity, 5)


def lead_tier(severity: int) -> str:
    if severity >= 5:
        return "A"
    if severity == 4:
        return "B"
    return "C"
