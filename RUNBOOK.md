# BrokenSite-Weekly Operations Runbook

## System Overview

```
Weekly Timer (systemd)
    └── run_weekly.py (orchestrator)
            ├── Phase 1: Scraping
            │       ├── maps_scraper.py → Google Maps
            │       ├── scoring.py → Website checks
            │       └── db.py → SQLite storage
            │
            └── Phase 2: Delivery
                    ├── gumroad.py → Get subscribers
                    └── delivery.py → Send emails
```

## File Locations

| Path | Purpose |
|------|---------|
| `/opt/brokensite-weekly/` | Application root |
| `/opt/brokensite-weekly/.env` | Credentials (600 permissions) |
| `/opt/brokensite-weekly/data/leads.db` | SQLite database |
| `/opt/brokensite-weekly/logs/brokensite-weekly.log` | Application logs |
| `/opt/brokensite-weekly/output/leads_YYYY-MM-DD.csv` | Weekly CSV exports |
| `/opt/brokensite-weekly/debug/` | Screenshots/HTML on failures |

## Database Schema

```sql
-- Main leads table
leads (
    place_id TEXT PRIMARY KEY,  -- Google Place ID (stable)
    cid TEXT,                   -- Google CID (alternative ID)
    name TEXT,
    website TEXT,
    address TEXT,
    phone TEXT,
    city TEXT,
    category TEXT,
    score INTEGER,              -- Higher = more broken
    reasons TEXT,               -- Comma-separated flags
    first_seen TIMESTAMP,
    last_seen TIMESTAMP,
    exported_count INTEGER,
    last_exported TIMESTAMP
)

-- Run history
runs (
    run_id TEXT PRIMARY KEY,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    status TEXT,  -- 'running', 'completed', 'failed'
    queries_attempted INTEGER,
    businesses_found INTEGER,
    leads_exported INTEGER,
    emails_sent INTEGER,
    error_message TEXT
)

-- Export tracking
exports (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    subscriber_email TEXT,
    lead_count INTEGER,
    csv_path TEXT,
    sent_at TIMESTAMP
)
```

## Common Operations

### Check System Status

```bash
# Timer status
sudo systemctl status brokensite-weekly.timer

# Last run status
sudo systemctl status brokensite-weekly.service

# Recent logs
sudo journalctl -u brokensite-weekly.service --since "1 hour ago"

# Database stats
sudo -u brokensite /opt/brokensite-weekly/venv/bin/python -m src.run_weekly --stats
```

### Manual Run

```bash
# Full run
sudo systemctl start brokensite-weekly.service

# Scrape only (no emails)
sudo -u brokensite /opt/brokensite-weekly/venv/bin/python -m src.run_weekly --scrape-only

# Deliver only (use existing leads)
sudo -u brokensite /opt/brokensite-weekly/venv/bin/python -m src.run_weekly --deliver-only
```

### View Logs

```bash
# Application log (rotates at 10MB)
tail -f /opt/brokensite-weekly/logs/brokensite-weekly.log

# Systemd journal
sudo journalctl -u brokensite-weekly.service -f

# Last run's complete log
sudo journalctl -u brokensite-weekly.service --since "last Sunday"
```

### Database Queries

```bash
# Connect to database
sqlite3 /opt/brokensite-weekly/data/leads.db

# Useful queries:
.mode column
.headers on

-- Recent leads
SELECT name, website, score, reasons FROM leads ORDER BY last_seen DESC LIMIT 20;

-- High-score leads
SELECT name, website, score, reasons FROM leads WHERE score >= 75 ORDER BY score DESC;

-- Run history
SELECT run_id, status, leads_exported, emails_sent FROM runs ORDER BY started_at DESC LIMIT 10;

-- Subscriber delivery history
SELECT subscriber_email, COUNT(*) as deliveries, MAX(sent_at) as last_delivery
FROM exports GROUP BY subscriber_email;
```

## Troubleshooting

### Scraper Failures

**Symptom**: No businesses found, debug screenshots show blank/error pages

**Check**:
```bash
# Look at debug dumps
ls -la /opt/brokensite-weekly/debug/

# View screenshot
# (copy to local machine and open)
```

**Common causes**:
1. Google consent dialog changed → check consent button selectors
2. Rate limiting → wait 24h, consider reducing queries
3. Playwright browser issue → `playwright install chromium`

### Email Delivery Failures

**Symptom**: `emails_sent: 0` in logs

**Check**:
```bash
# Test SMTP manually
python3 -c "
import smtplib
with smtplib.SMTP('smtp.gmail.com', 587) as s:
    s.starttls()
    s.login('your_email', 'your_app_password')
    print('SMTP OK')
"
```

**Common causes**:
1. App password expired → regenerate in Google account
2. Gmail "less secure apps" blocked → must use App Password
3. Sending limit reached → wait 24h

### Gumroad API Failures

**Symptom**: "Failed to get subscribers" in logs

**Check**:
```bash
# Test API
curl -H "Authorization: Bearer YOUR_TOKEN" \
     https://api.gumroad.com/v2/user
```

**Common causes**:
1. Token expired → regenerate in Gumroad settings
2. Product ID wrong → verify in Gumroad dashboard
3. No active subscribers → check Gumroad dashboard

### High Memory Usage

**Symptom**: OOM kills or slow performance

**Fix**:
```bash
# Reduce concurrent operations in config
# Edit src/config.py:
#   max_results_per_query = 30  (was 50)
#   max_scrolls = 10  (was 15)
```

## Scoring Reference

| Score Range | Meaning | Examples |
|-------------|---------|----------|
| 75-100 | Hard failure | Unreachable, timeout, 5xx error, SSL error, parked domain |
| 40-74 | Medium signals | No HTTPS, old copyright, missing viewport, empty page |
| 10-39 | Weak signals | DIY builder, old jQuery |
| 0-9 | Probably fine | Minor issues only |

**Leads with score ≥ 40 are exported.**

## Business Model Reference

### Revenue Math
- 20 subscribers × $50/month = $1,000/month
- Gumroad takes ~10% = $900 net
- VPS cost ~$20/month = $880 profit

### Subscriber Value
- Weekly CSV of 100-300 scored leads
- Focus on niches (plumbers, roofers, dentists, etc.)
- High-score leads (75+) are "warm" prospects for web dev services

### Scaling
- Add more cities → more leads
- Add more categories → more leads
- More subscribers → same leads, more revenue (zero marginal cost)

## Weekly Checklist

1. ✅ Check timer fired: `systemctl status brokensite-weekly.timer`
2. ✅ Check run completed: `journalctl -u brokensite-weekly.service --since "last Sunday"`
3. ✅ Verify emails sent: check `emails_sent` in logs
4. ✅ Monitor Gumroad: check subscriber count hasn't dropped
5. ✅ Check disk space: `df -h /opt/brokensite-weekly`

## Emergency Contacts

- Gumroad support: support@gumroad.com
- Gmail SMTP issues: Check Google Workspace Status Dashboard
