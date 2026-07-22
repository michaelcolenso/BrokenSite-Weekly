"""Emit scanner results and verification samples."""

from __future__ import annotations

import csv
import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

from scanner.checks import CheckResult


@dataclass
class LeadRecord:
    domain: str
    business_name: str
    vertical: str
    phone: str
    address: str
    checks: list[CheckResult]
    tier: str
    screenshot_key: str | None = None


@dataclass
class ResultsPayload:
    metro: str
    week: str
    scanned: int
    leads: list[LeadRecord] = field(default_factory=list)


def write_results(payload: ResultsPayload, path: str | Path) -> None:
    data = asdict(payload)
    for lead in data["leads"]:
        lead["checks"] = [check for check in lead["checks"] if check["triggered"]]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def write_verification_sample(leads: list[LeadRecord], path: str | Path, *, sample_size: int = 50, seed: int = 42) -> None:
    sample = list(leads)
    rng = random.Random(seed)
    rng.shuffle(sample)
    sample = sample[:sample_size]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["domain", "business_name", "vertical", "tier", "triggered_checks"])
        writer.writeheader()
        for lead in sample:
            writer.writerow({
                "domain": lead.domain,
                "business_name": lead.business_name,
                "vertical": lead.vertical,
                "tier": lead.tier,
                "triggered_checks": ";".join(check.check_id for check in lead.checks if check.triggered),
            })
