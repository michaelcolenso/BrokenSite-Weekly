"""Run diagnostics (and optional vision evaluation) for one site.

Usage:
    python -m scanner.diagnose --url example.com --category plumber
    python -m scanner.diagnose --url example.com --no-vision --output-dir output/diag

Prints one JSON object: {"diagnostics": {...}, "vision": {...} | null}.
Exit codes: 0 ok, 2 invalid configuration.
"""

from __future__ import annotations

import argparse
import json
import sys

from scanner.diagnostics import collect_diagnostics
from scanner.evaluators.vision import evaluate_screenshot, validate_vision_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="site URL or bare domain")
    parser.add_argument("--category", default="", help="business category (context for the vision model)")
    parser.add_argument("--output-dir", default="output/diagnostics", help="where screenshots are written")
    parser.add_argument("--no-vision", action="store_true", help="deterministic diagnostics only; no API call")
    args = parser.parse_args(argv)

    if not args.no_vision:
        problems = validate_vision_env()
        if problems:
            for problem in problems:
                print(f"config error: {problem}", file=sys.stderr)
            print("hint: set the variables or pass --no-vision", file=sys.stderr)
            return 2

    report = collect_diagnostics(args.url, args.output_dir)
    vision = None if args.no_vision else evaluate_screenshot(report, args.category)
    print(json.dumps({
        "diagnostics": report.model_dump(),
        "vision": vision.model_dump() if vision else None,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
