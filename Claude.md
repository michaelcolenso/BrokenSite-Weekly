# BrokenSite-Weekly

## Project Overview
A fully unattended weekly lead-generation system for local businesses with broken/outdated websites. Runs on a VPS via systemd timer without manual intervention.

## Architecture
```
src/
├── config.py          # Environment-based configuration
├── db.py              # SQLite database layer (leads, dedupe, subscribers)
├── retry.py           # Exponential backoff utilities
├── logging_setup.py   # Structured logging configuration
├── maps_scraper.py    # Playwright-based Google Maps scraper
├── scoring.py         # Website health/broken scoring
├── gumroad.py         # Gumroad subscriber retrieval (single product)
├── delivery.py        # SMTP email delivery with CSV attachment
└── run_weekly.py      # Main orchestrator script
```

## Key Design Decisions
- **Playwright for Maps**: Browser automation for Google Maps scraping (handles consent, dynamic loading)
- **SQLite for storage**: Simple, file-based, no external dependencies
- **Per-item isolation**: Failures in one lead/subscriber don't crash the entire run
- **Stable IDs**: Store Google place_id/CID for proper deduplication
- **Conservative scoring**: Hard failures (5xx, unreachable) weighted high; DIY builders weighted low to minimize false positives

## Environment Variables Required
```bash
GUMROAD_ACCESS_TOKEN=   # Gumroad API token
GUMROAD_PRODUCT_ID=     # Single subscription product ID
SMTP_HOST=              # SMTP server (default: smtp.gmail.com)
SMTP_PORT=              # SMTP port (default: 587)
SMTP_USERNAME=          # SMTP login
SMTP_PASSWORD=          # SMTP password or app password
SMTP_FROM_EMAIL=        # Sender email address
SMTP_FROM_NAME=         # Sender display name
```

## Running
```bash
# Manual run
python -m src.run_weekly

# Via systemd (production)
systemctl start brokensite-weekly.service
```

## File Locations
- **Database**: `data/leads.db`
- **Logs**: `logs/brokensite-weekly.log`
- **Weekly CSVs**: `output/leads_YYYY-MM-DD.csv`
- **Debug dumps**: `debug/` (screenshots, HTML on scraper failures)

## Business Model
~20 Gumroad subscribers at $50/month = ~$1,000/month. Subscribers receive weekly CSV of scored broken-website leads in their target niches.
