# BrokenSite-Weekly Setup Guide

Complete setup instructions for Ubuntu VPS deployment.

## Prerequisites

- Ubuntu 22.04 LTS VPS (2GB RAM minimum, 4GB recommended)
- Root/sudo access
- Existing Gumroad subscription product
- SMTP credentials (Gmail with App Password recommended)

## 1. System Preparation

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.11+ and dependencies
sudo apt install -y python3.11 python3.11-venv python3-pip

# Install Playwright system dependencies
sudo apt install -y \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libcairo2 libatspi2.0-0

# Create system user (no login shell)
sudo useradd -r -s /bin/false -d /opt/brokensite-weekly brokensite
```

## 2. Application Installation

```bash
# Create application directory
sudo mkdir -p /opt/brokensite-weekly
sudo chown brokensite:brokensite /opt/brokensite-weekly

# Clone repository (or copy files)
sudo -u brokensite git clone https://github.com/YOUR_REPO/BrokenSite-Weekly.git /opt/brokensite-weekly

# Create virtual environment + install deps (uv)
# Install uv first: https://docs.astral.sh/uv/
cd /opt/brokensite-weekly
sudo -u brokensite uv venv venv --python 3.11
# If `venv/` already exists and this errors, delete/recreate it.
sudo -u brokensite uv pip install -r requirements.txt --python /opt/brokensite-weekly/venv/bin/python

# Install Playwright browsers
sudo -u brokensite /opt/brokensite-weekly/venv/bin/playwright install chromium
```

## 3. Configuration

```bash
# Copy environment template
sudo -u brokensite cp .env.example .env

# Edit with your credentials
sudo -u brokensite nano .env
```

### Required Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `GUMROAD_ACCESS_TOKEN` | Gumroad API token | `abc123...` |
| `GUMROAD_PRODUCT_ID` | Your subscription product ID | `xyz789` |
| `SMTP_HOST` | SMTP server | `smtp.gmail.com` |
| `SMTP_PORT` | SMTP port | `587` |
| `SMTP_USERNAME` | SMTP login | `you@gmail.com` |
| `SMTP_PASSWORD` | SMTP password/app password | `abcd efgh ijkl mnop` |
| `SMTP_FROM_EMAIL` | Sender email | `you@gmail.com` |
| `SMTP_FROM_NAME` | Sender display name | `BrokenSite Weekly` |

### Getting Gumroad Credentials

1. Go to https://app.gumroad.com/settings/advanced
2. Scroll to "Application Form" and create an app
3. Copy the Access Token
4. For Product ID: go to your product page, the ID is in the URL or use the API

### Gmail App Password Setup

1. Enable 2FA on your Google account
2. Go to https://myaccount.google.com/apppasswords
3. Generate an "App Password" for "Mail"
4. Use the 16-character password (spaces OK)

## 4. Create Data Directories

```bash
sudo -u brokensite mkdir -p /opt/brokensite-weekly/{data,logs,output,debug}
```

## 5. Test the Installation

```bash
# Validate configuration
sudo -u brokensite /opt/brokensite-weekly/venv/bin/python -m src.run_weekly --validate

# Run a test scrape (no delivery)
sudo -u brokensite /opt/brokensite-weekly/venv/bin/python -m src.run_weekly --scrape-only --dry-run --no-outreach

# Check results
sudo -u brokensite /opt/brokensite-weekly/venv/bin/python -m src.run_weekly --stats
```

## 6. Install systemd Service

```bash
# Copy service files
sudo cp /opt/brokensite-weekly/systemd/brokensite-weekly.service /etc/systemd/system/
sudo cp /opt/brokensite-weekly/systemd/brokensite-weekly.timer /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable timer (starts on boot)
sudo systemctl enable brokensite-weekly.timer

# Start timer now
sudo systemctl start brokensite-weekly.timer

# Verify timer is active
sudo systemctl list-timers | grep brokensite
```

## 7. Verify Setup

```bash
# Check timer status
sudo systemctl status brokensite-weekly.timer

# Manually trigger a run (for testing)
sudo systemctl start brokensite-weekly.service

# Watch logs
sudo journalctl -u brokensite-weekly.service -f

# Check application logs
tail -f /opt/brokensite-weekly/logs/brokensite-weekly.log
```

## Updating

```bash
cd /opt/brokensite-weekly
sudo -u brokensite git pull
sudo -u brokensite ./venv/bin/pip install -r requirements.txt
sudo systemctl daemon-reload
```

## Uninstalling

```bash
sudo systemctl stop brokensite-weekly.timer
sudo systemctl disable brokensite-weekly.timer
sudo rm /etc/systemd/system/brokensite-weekly.*
sudo systemctl daemon-reload
sudo userdel brokensite
sudo rm -rf /opt/brokensite-weekly
```
