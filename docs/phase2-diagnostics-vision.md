# Phase 2: diagnostics + vision evaluation

Standalone modules; not yet wired into `python -m scanner`.

| Module | Role |
|---|---|
| `scanner/diagnostics/` | Deterministic collector (Playwright). One page load, desktop 1440x900 + mobile 390x844 PNGs. Returns `DiagnosticReport` (pydantic). |
| `scanner/evaluators/vision.py` | Sends screenshots to the Anthropic Messages API, returns `VisionVerdict` (pydantic). |
| `scanner/diagnose.py` | CLI for one site. |

## CLI

```
python -m scanner.diagnose --url example.com --category plumber
python -m scanner.diagnose --url example.com --no-vision --output-dir output/diag
```

| Flag | Meaning |
|---|---|
| `--url` (required) | URL or bare domain (https assumed) |
| `--category` | business category, passed to the model as context |
| `--output-dir` | screenshot directory (default `output/diagnostics`) |
| `--no-vision` | diagnostics only; no API call, no key needed |

Exit 2 if vision is enabled and env validation fails.

## Environment

| Var | Required | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | for vision | validated at CLI start (`validate_vision_env`) |
| `BSW_VISION_MODEL` | no | default `claude-haiku-4-5-20251001`; must not be set-but-empty |
| `BSW_CHROMIUM_PATH` | no | Chromium binary override |

## Guarantees

- Timeouts: 15s navigation, 15s API call. Two attempts max, linear backoff.
- Neither entry point raises; failures come back as `status="error"` / `"blocked"` / `"skipped"`.
- Vision is skipped (no API call) unless diagnostics `status == "ok"`, HTTP 2xx/3xx, and a screenshot exists.
- Repo hard rules honoured: robots.txt check, honest `BSW-Scanner` User-Agent, shared per-domain 10s pacing, no evasion.
- No DB writes. Outputs are files/JSON, so reruns are idempotent (screenshots overwrite by domain).

## Caveats

- Sub-resource requests the browser makes (CSS/images/scripts) are not paced by the 10s window.
- `broken_asset_count` counts failed or 4xx/5xx image, stylesheet, script, font requests.
- `ssl_valid` is `false` for http-only sites (`ssl_error="no_https"`).
- Model verdict quality is unmeasured: no labelled sample has been run. Check it against the manual verification sample before using it to filter leads.
