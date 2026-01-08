# BrokenSite-Weekly

## Project Overview
A fully unattended weekly lead-generation system for local businesses with broken/outdated websites. Runs on a VPS via systemd timer without manual intervention.

## Architecture
```
src/
├── config.py          # Environment-based configuration (dataclasses)
├── db.py              # SQLite: leads, runs, exports tables
├── retry.py           # Exponential backoff with jitter
├── logging_setup.py   # Rotating file + stderr logging
├── maps_scraper.py    # Playwright Google Maps scraper (hardened)
├── scoring.py         # Website health scoring (weighted signals)
├── gumroad.py         # Subscriber retrieval (read-only, single product)
├── delivery.py        # SMTP with CSV attachment
└── run_weekly.py      # Main orchestrator with graceful shutdown

systemd/
├── brokensite-weekly.service  # oneshot service
└── brokensite-weekly.timer    # Sunday 3am UTC
```

## Key Reliability Features
- **Consent handling**: Multiple selector strategies for Google cookie banners
- **Debug dumps**: Screenshot + HTML saved on scraper failures
- **Per-item isolation**: One business/subscriber failure won't crash the run
- **Graceful shutdown**: SIGTERM/SIGINT handled, partial progress preserved
- **Deduplication**: Uses Google place_id (stable) with 90-day window
- **Retry with backoff**: Configurable exponential backoff + jitter

## Scoring Weights
| Signal | Weight | Category |
|--------|--------|----------|
| Unreachable | 100 | Hard failure |
| Timeout | 90 | Hard failure |
| 5xx error | 85 | Hard failure |
| SSL error | 80 | Hard failure |
| Parked domain | 75 | Hard failure |
| No HTTPS | 30 | Medium |
| Old copyright | 25 | Medium |
| No viewport | 20 | Medium |
| Wix/Squarespace | 5 | Weak (low to avoid false positives) |

Threshold: **score >= 40** to export

## Environment Variables
```bash
GUMROAD_ACCESS_TOKEN=   # From Gumroad settings
GUMROAD_PRODUCT_ID=     # Existing subscription product
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=          # Your email
SMTP_PASSWORD=          # App password (not regular password)
SMTP_FROM_EMAIL=        # Sender address
SMTP_FROM_NAME=         # Display name
```

## CLI Commands
```bash
python -m src.run_weekly              # Full run
python -m src.run_weekly --scrape-only    # No delivery
python -m src.run_weekly --deliver-only   # No scraping
python -m src.run_weekly --stats          # DB statistics
python -m src.run_weekly --validate       # Check config
```

## File Locations
- **Database**: `data/leads.db`
- **Logs**: `logs/brokensite-weekly.log` (rotates at 10MB)
- **CSV output**: `output/leads_YYYY-MM-DD.csv`
- **Debug dumps**: `debug/*.png`, `debug/*.html`

## Database Schema
Primary tables: `leads` (place_id PK), `runs`, `exports`
See RUNBOOK.md for full schema.

## Testing Changes
1. `--validate` to check config
2. `--scrape-only` to test scraping without emails
3. `--deliver-only` to test delivery with existing leads
4. Check `logs/` and `debug/` for issues
