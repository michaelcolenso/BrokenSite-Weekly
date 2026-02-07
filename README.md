# BrokenSite-Weekly

Fully unattended weekly lead-generation system for local businesses with broken/outdated websites. Runs on a VPS via systemd timer, delivers CSV to Gumroad subscribers.

## How It Works

1. **Scrapes** Google Maps for local businesses (plumbers, roofers, dentists, etc.)
2. **Checks** each website for broken/outdated signals
3. **Scores** sites (higher = more broken)
4. **Dedupes** using Google Place IDs
5. **Emails** weekly CSV to active Gumroad subscribers

## Quick Start

```bash
# Clone and setup
git clone https://github.com/YOUR_REPO/BrokenSite-Weekly.git
cd BrokenSite-Weekly

# Python env + deps (uv)
# Install uv first: https://docs.astral.sh/uv/
uv venv venv --python 3.11
# If `venv/` already exists and this errors, delete/recreate it.
uv pip install -r requirements.txt --python ./venv/bin/python
./venv/bin/playwright install chromium

# Configure (optional for scrape-only tests; required for delivery/outreach)
cp .env.example .env
nano .env  # Add your Gumroad + SMTP credentials

# Load env for this shell (run_weekly does not auto-load .env)
set -a; source ./.env; set +a

# Test
./venv/bin/python -m src.run_weekly --validate
./venv/bin/python -m src.run_weekly --export-csv --dry-run
```

See [SETUP.md](SETUP.md) for full VPS deployment instructions.

## Architecture

```
src/
├── config.py          # Environment-based configuration
├── db.py              # SQLite: leads, runs, exports
├── retry.py           # Exponential backoff utilities
├── logging_setup.py   # Rotating file + stderr logging
├── maps_scraper.py    # Playwright Google Maps scraper
├── scoring.py         # Website health scoring
├── gumroad.py         # Subscriber retrieval (read-only)
├── delivery.py        # SMTP with CSV attachment
└── run_weekly.py      # Main orchestrator
```

## Scoring

| Score | Signal | Weight |
|-------|--------|--------|
| 75-100 | Hard failures (unreachable, 5xx, SSL error, parked) | High |
| 40-74 | Medium (no HTTPS, old copyright, no viewport) | Medium |
| 5-15 | Weak (DIY builders like Wix, Squarespace) | Low |

Only leads scoring **≥40** are exported.

## Business Model

- ~20 Gumroad subscribers at $50/month = ~$1,000/month
- Subscribers receive weekly CSV of scored leads
- Zero marginal cost per subscriber (same scrape, same email)

## Documentation

- [SETUP.md](SETUP.md) - Ubuntu VPS installation
- [RUNBOOK.md](RUNBOOK.md) - Operations guide, troubleshooting, DB schema

## License

MIT
