# BrokenSite-Weekly - Agent Guide

> **For AI coding agents:** This document contains everything you need to know to work effectively with this codebase.

## Project Overview

**BrokenSite-Weekly** is a fully automated lead-generation system that finds local businesses with broken, outdated, or neglected websites and delivers them weekly to paying subscribers (web developers and agencies).

**Business Model:**
- Subscribers pay ~$50/month via Gumroad
- System runs weekly on a VPS via systemd timer
- Zero marginal cost per additional subscriber
- Current target: ~20 subscribers generating ~$1,000/month

**Core Workflow:**
1. **Scrape** Google Maps for local businesses (plumbers, dentists, etc.)
2. **Check** each website for broken/outdated signals
3. **Score** sites (higher = more broken)
4. **Dedupe** using Google Place IDs (90-day window)
5. **Email** weekly CSV to active Gumroad subscribers

---

## Technology Stack

| Component | Technology |
|-----------|------------|
| **Language** | Python 3.11+ |
| **Browser Automation** | Playwright (Chromium) |
| **Database** | SQLite (stdlib) |
| **Email** | smtplib (stdlib) |
| **HTTP Requests** | requests |
| **HTML Parsing** | BeautifulSoup4 |
| **Templating** | Jinja2 |
| **Process Runner** | systemd timer/service |
| **Testing** | pytest |

**Key Dependencies:**
```
playwright>=1.40.0
requests>=2.31.0
beautifulsoup4>=4.12.0
Jinja2>=3.1.0
fastapi>=0.104.0  # For tracking endpoints
uvicorn>=0.24.0
```

---

## Project Structure

```
/home/user/BrokenSite-Weekly/
├── src/                          # Main source code
│   ├── __init__.py
│   ├── config.py                 # Environment-based configuration (dataclasses)
│   ├── db.py                     # SQLite persistence layer
│   ├── retry.py                  # Exponential backoff with jitter
│   ├── logging_setup.py          # Rotating file + stderr logging
│   ├── maps_scraper.py           # Playwright Google Maps scraper
│   ├── scoring.py                # Website health scoring engine
│   ├── gumroad.py                # Subscriber retrieval API
│   ├── delivery.py               # SMTP email with CSV attachments
│   ├── run_weekly.py             # Main orchestrator (entry point)
│   ├── contact_finder.py         # Email extraction from websites
│   ├── audit_generator.py        # HTML audit page generation
│   ├── outreach.py               # Direct outreach email campaigns
│   ├── warm_delivery.py          # Warm lead delivery to pro subscribers
│   ├── tracking.py               # FastAPI tracking endpoints
│   ├── portal_auth.py            # Subscriber portal authentication
│   └── lead_utils.py             # Lead tiering and utility functions
│
├── tests/                        # Test suite
│   ├── conftest.py               # Shared pytest fixtures
│   ├── test_scoring.py           # Scoring module tests
│   ├── test_db.py                # Database tests
│   ├── test_config_validation.py # Config validation tests
│   └── ...
│
├── systemd/                      # Systemd service files
│   ├── brokensite-weekly.service # Oneshot service definition
│   └── brokensite-weekly.timer   # Weekly timer (Sunday 3am UTC)
│
├── scripts/                      # Utility scripts
│   ├── backup_db.sh              # Database backup script
│   └── quick_eval.py             # Quick evaluation script
│
├── templates/                    # Jinja2 templates
│   └── audit.html                # Audit page template
│
├── data/                         # SQLite database storage
├── logs/                         # Application logs (rotated at 10MB)
├── output/                       # CSV exports and KPI snapshots
├── debug/                        # Screenshots/HTML dumps on failures
│
├── requirements.txt              # Production dependencies
├── requirements-dev.txt          # Development dependencies
├── .env.example                  # Environment variable template
├── README.md                     # User-facing documentation
├── SETUP.md                      # VPS deployment guide
├── RUNBOOK.md                    # Operations and troubleshooting
├── ROADMAP.md                    # Feature roadmap and improvements
├── HANDOFF.md                    # Mission brief for contributors
└── Claude.md                     # Architecture overview
```

---

## Build and Run Commands

### Environment Setup

```bash
# Install uv first: https://docs.astral.sh/uv/
uv venv venv --python 3.11
uv pip install -r requirements.txt --python ./venv/bin/python
./venv/bin/playwright install chromium

# Configure environment
cp .env.example .env
# Edit .env with your credentials
```

### Main Commands

```bash
# Full weekly run (scrape + deliver + outreach)
./venv/bin/python -m src.run_weekly

# Scrape only (no delivery, no emails)
./venv/bin/python -m src.run_weekly --scrape-only

# Deliver only (use existing leads in database)
./venv/bin/python -m src.run_weekly --deliver-only

# Outreach only (audit gen, contact find, send emails)
./venv/bin/python -m src.run_weekly --outreach-only

# Skip outreach phases
./venv/bin/python -m src.run_weekly --no-outreach

# Dry run (no database writes or emails - for testing)
./venv/bin/python -m src.run_weekly --dry-run
./venv/bin/python -m src.run_weekly --scrape-only --dry-run

# Export local CSV without emailing
./venv/bin/python -m src.run_weekly --export-csv --dry-run

# Validate configuration
./venv/bin/python -m src.run_weekly --validate
./venv/bin/python -m src.run_weekly --validate --scrape-only --no-outreach

# Show database statistics
./venv/bin/python -m src.run_weekly --stats
```

### Testing

```bash
# Run all tests
./venv/bin/python -m pytest

# Run with coverage
./venv/bin/python -m pytest --cov=src --cov-report=term-missing

# Run specific test file
./venv/bin/python -m pytest tests/test_scoring.py -v
```

---

## Code Style Guidelines

### General Principles

1. **Fail gracefully** - One bad business shouldn't crash the whole run
2. **Log everything** - When running unattended on a VPS, logs are your eyes
3. **Make it configurable** - Scoring weights in config, not hardcoded
4. **Isolate errors** - Use `*_with_isolation` wrappers that never raise exceptions
5. **Don't over-engineer** - This runs once a week, not microservices

### Error Isolation Pattern

Every external operation uses an isolation wrapper that returns `(result, error)`:

```python
def operation_with_isolation(*args) -> tuple[ResultType, Optional[str]]:
    """Execute operation with full error isolation. Never raises."""
    try:
        result = operation(*args)
        return result, None
    except Exception as e:
        logger.error(f"Operation failed: {e}")
        return default_result, str(e)
```

Examples in codebase:
- `scrape_with_isolation()` in `maps_scraper.py`
- `evaluate_with_isolation()` in `scoring.py`
- `get_subscribers_with_isolation()` in `gumroad.py`
- `deliver_with_isolation()` in `delivery.py`

### Logging

Use the structured logger from `logging_setup.py`:

```python
from .logging_setup import get_logger

logger = get_logger("module_name")

logger.info("Informative message")
logger.warning("Something unexpected")
logger.error("Operation failed: %s", error)
```

### Configuration

All configuration is in `src/config.py` using dataclasses:

```python
@dataclass
class ScoringConfig:
    weight_unreachable: int = 100
    weight_timeout: int = 90
    # ... etc
```

Load from environment with defaults:
```python
from .config import load_config

config = load_config()
score = config.scoring.weight_ssl_error
```

---

## Testing Strategy

### Test Organization

- **Unit tests** for individual functions (`test_scoring.py`)
- **Integration tests** for database operations (`test_db.py`)
- **Fixtures** in `conftest.py` for shared test data

### Key Fixtures

```python
# From conftest.py
@pytest.fixture
def scoring_config() -> ScoringConfig:
    return ScoringConfig()

@pytest.fixture
def test_database(database_config: DatabaseConfig) -> Generator[Database, None, None]:
    db = Database(database_config)
    yield db
    # Cleanup automatic via tmp_path

@pytest.fixture
def sample_html_modern() -> str:
    return """<!DOCTYPE html>..."""
```

### Mocking External Calls

Always mock external HTTP calls in tests:

```python
from unittest.mock import Mock, patch

@patch("src.scoring.fetch_website")
def test_scores_ssl_error(mock_fetch, scoring_config):
    mock_fetch.return_value = (None, "ssl_error: certificate verify failed")
    result = evaluate_website("https://example.com", config=scoring_config)
    assert "ssl_error" in result.reasons
```

### Testing Run Modes

```bash
# 1. Validate config
python -m src.run_weekly --validate

# 2. Test scrape without side effects
python -m src.run_weekly --scrape-only --dry-run --no-outreach

# 3. Test delivery with existing leads
python -m src.run_weekly --deliver-only --dry-run

# 4. Check logs for issues
tail -f logs/brokensite-weekly.log
```

---

## Database Schema

**Primary tables:**

```sql
-- Leads table: stores all discovered businesses
leads (
    place_id TEXT PRIMARY KEY,      -- Google Place ID (stable)
    cid TEXT,                        -- Google CID (alternative ID)
    name TEXT,
    website TEXT,
    address TEXT,
    phone TEXT,
    review_count INTEGER,            -- Google review count
    city TEXT,
    category TEXT,
    score INTEGER,                   -- Higher = more broken
    reasons TEXT,                    -- Comma-separated flags
    first_seen TIMESTAMP,
    last_seen TIMESTAMP,
    exported_count INTEGER,
    last_exported TIMESTAMP,
    exported_basic_at TIMESTAMP,
    exported_pro_at TIMESTAMP,
    exclusive_until TIMESTAMP,       -- Exclusive window for pro tier
    exclusive_tier TEXT,
    lead_tier TEXT                   -- hot/warm/cool
);

-- Run history
runs (
    run_id TEXT PRIMARY KEY,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    status TEXT,                     -- 'running', 'completed', 'failed'
    queries_attempted INTEGER,
    businesses_found INTEGER,
    leads_exported INTEGER,
    emails_sent INTEGER,
    error_message TEXT
);

-- Export tracking
exports (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    subscriber_email TEXT,
    lead_count INTEGER,
    csv_path TEXT,
    sent_at TIMESTAMP,
    tier TEXT,
    export_type TEXT                 -- 'cold' or 'warm'
);

-- Audit pages generated for leads
audits (place_id, audit_url, audit_html_path, generated_at, issues_json);

-- Contact information found for leads
contacts (place_id, email, source, confidence, found_at);

-- Outreach attempts to leads
outreach (id, place_id, email, audit_url, sent_at, success, error,
          followup_sent_at, followup_success, followup_error);

-- Engagement tracking (opens, clicks, etc.)
engagement_events (id, place_id, event_type, ip_address, user_agent, timestamp);

-- Unsubscribe list
unsubscribes (place_id, email, unsubscribed_at);

-- Suppression list for bad emails
suppression (email, reason, suppressed_at);
```

---

## Scoring System

**Scoring philosophy:** Higher score = more likely to be broken/outdated.

| Score Range | Category | Examples |
|-------------|----------|----------|
| 75-100 | Hard failure | Unreachable, timeout, 5xx error, SSL error, parked domain |
| 40-74 | Medium signals | No HTTPS, old copyright, no mobile viewport |
| 10-39 | Weak signals | DIY builder (Wix, Squarespace) |
| 0-9 | Probably fine | Minor issues only |

**Key weights** (from `ScoringConfig`):
- `weight_unreachable`: 100
- `weight_timeout`: 90
- `weight_5xx_error`: 85
- `weight_ssl_error`: 80
- `weight_parked_domain`: 75
- `weight_http_only`: 30
- `weight_outdated_copyright`: 25
- `weight_missing_viewport`: 20

**Threshold:** Only leads scoring ≥40 are exported (configurable via `min_score_to_include`).

---

## Environment Variables

Required for full operation:

```bash
# Gumroad API
GUMROAD_ACCESS_TOKEN=           # From https://app.gumroad.com/settings/advanced
GUMROAD_PRODUCT_ID=             # Your subscription product ID
GUMROAD_PRODUCTS_JSON=          # Optional: multi-product mapping
GUMROAD_PRO_SEAT_CAP=5          # Pro tier seat limit

# SMTP Email
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_email@gmail.com
SMTP_PASSWORD=your_app_password  # Use App Password, not regular password
SMTP_FROM_EMAIL=your_email@gmail.com
SMTP_FROM_NAME="BrokenSite Weekly"

# Portal (subscriber access)
PORTAL_SECRET=your_secret_key
PORTAL_BASE_URL=https://your-domain.com
PORTAL_TOKEN_TTL_DAYS=30

# Outreach settings
OUTREACH_ENABLED=true
OUTREACH_MAX_EMAILS_PER_DAY=300
OUTREACH_MAX_EMAILS_PER_HOUR=60
OUTREACH_DELAY_MIN_SECONDS=15
OUTREACH_DELAY_MAX_SECONDS=45
OUTREACH_SEND_START_HOUR=8
OUTREACH_SEND_END_HOUR=18
OUTREACH_MIN_SCORE=50
OUTREACH_MIN_CONFIDENCE=0.7
OUTREACH_PHYSICAL_ADDRESS="123 Main St"  # Required for CAN-SPAM
OUTREACH_COMPANY_NAME="BrokenSite Weekly"
TRACKING_BASE_URL=https://your-domain.com
```

---

## Security Considerations

1. **CSV Injection Prevention**
   - Values starting with `=`, `+`, `-`, `@` are prefixed with `'`
   - See `_sanitize_csv_value()` in `delivery.py`

2. **Environment File Permissions**
   - `.env` should have 600 permissions (owner read/write only)

3. **SQL Injection Protection**
   - All DB queries use parameterized statements
   - Never use f-strings for SQL

4. **CAN-SPAM Compliance**
   - Physical address required in outreach emails
   - Unsubscribe mechanism via Gumroad
   - Suppression list for bounced emails

5. **Rate Limiting**
   - Built-in delays between emails (`OUTREACH_DELAY_*`)
   - Daily/hourly sending caps
   - Business hours only sending

---

## Common Development Tasks

### Adding a New Scoring Signal

1. Add weight to `ScoringConfig` in `config.py`
2. Add detection logic to `evaluate_website()` in `scoring.py`
3. Add reason to `result.reasons` list
4. Add unit test in `tests/test_scoring.py`
5. Update this document

### Adding a New CSV Column

1. Add field to `generate_csv()` in `delivery.py`
2. Ensure data is fetched from DB query
3. Update `_sanitize_csv_value()` if needed for safety

### Modifying Database Schema

1. Update `SCHEMA` string in `db.py`
2. Add column migration in `_ensure_columns()` method
3. Update `Lead` dataclass if needed

### Adding a New CLI Flag

1. Add argument in `main()` function in `run_weekly.py`
2. Pass to `run_weekly()` function
3. Implement logic in appropriate phase

---

## Deployment

The system is designed for Ubuntu VPS deployment via systemd:

```bash
# Copy service files
sudo cp systemd/brokensite-weekly.service /etc/systemd/system/
sudo cp systemd/brokensite-weekly.timer /etc/systemd/system/

# Enable and start timer
sudo systemctl daemon-reload
sudo systemctl enable brokensite-weekly.timer
sudo systemctl start brokensite-weekly.timer

# Check status
sudo systemctl status brokensite-weekly.timer
sudo journalctl -u brokensite-weekly.service -f
```

See `SETUP.md` for complete VPS setup instructions.

---

## Troubleshooting

**Common issues:**

1. **Scraper returns no results**
   - Check debug screenshots in `debug/`
   - May need to update consent button selectors
   - Google may be rate-limiting

2. **Email delivery fails**
   - Verify SMTP credentials with manual test
   - Check app password (not regular password) for Gmail
   - Review `logs/brokensite-weekly.log`

3. **High memory usage**
   - Reduce `max_results_per_query` in config
   - Reduce `max_scrolls` in scraper config

See `RUNBOOK.md` for detailed troubleshooting steps.

---

## Reference Documents

| File | Purpose |
|------|---------|
| `README.md` | Quick start and overview |
| `SETUP.md` | Full VPS deployment guide |
| `RUNBOOK.md` | Operations, troubleshooting, DB queries |
| `ROADMAP.md` | Feature roadmap and improvement ideas |
| `HANDOFF.md` | Business context and founder mindset |
| `Claude.md` | Architecture summary |
| `.env.example` | Environment variable template |

---

*Last updated: 2026-03-07*
