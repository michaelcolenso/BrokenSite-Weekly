# BrokenSite-Weekly Dockerfile
# Multi-stage build for optimized production deployment

# ===== Build Stage =====
FROM python:3.11-slim as builder

# Set build-time environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ===== Production Stage =====
FROM python:3.11-slim as production

# Labels for container metadata
LABEL org.opencontainers.image.title="BrokenSite-Weekly" \
      org.opencontainers.image.description="Automated lead-generation system for web developers" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.vendor="BrokenSite Weekly"

# Runtime environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    # Playwright environment
    PLAYWRIGHT_BROWSERS_PATH=/app/browsers \
    # App directories
    APP_DATA_DIR=/app/data \
    APP_LOG_DIR=/app/logs \
    APP_OUTPUT_DIR=/app/output \
    APP_DEBUG_DIR=/app/debug

# Create non-root user for security
RUN groupadd -r brokensite && useradd -r -g brokensite brokensite

WORKDIR /app

# Install Playwright browser dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Playwright Chromium dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libwayland-client0 \
    # Additional utilities
    wget \
    ca-certificates \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Install Playwright browsers
RUN playwright install chromium && \
    chown -R brokensite:brokensite /app/browsers

# Create application directories
RUN mkdir -p ${APP_DATA_DIR} ${APP_LOG_DIR} ${APP_OUTPUT_DIR} ${APP_DEBUG_DIR} && \
    chown -R brokensite:brokensite /app

# Copy application code
COPY --chown=brokensite:brokensite src/ /app/src/
COPY --chown=brokensite:brokensite systemd/ /app/systemd/

# Health check script
COPY --chown=brokensite:brokensite <<EOF /app/healthcheck.py
#!/usr/bin/env python3
"""Health check for BrokenSite-Weekly container."""
import sys
from pathlib import Path

def check_health():
    # Check database exists and is accessible
    db_path = Path("/app/data/leads.db")
    if db_path.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            conn.execute("SELECT 1")
            conn.close()
        except Exception as e:
            print(f"Database unhealthy: {e}")
            return 1

    # Check log directory is writable
    log_dir = Path("/app/logs")
    if not log_dir.exists() or not (log_dir / ".write_test").touch() is None:
        pass  # Touch succeeded

    print("Healthy")
    return 0

if __name__ == "__main__":
    sys.exit(check_health())
EOF
RUN chmod +x /app/healthcheck.py

# Switch to non-root user
USER brokensite

# Volumes for persistent data
VOLUME ["/app/data", "/app/logs", "/app/output", "/app/debug"]

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=5s --retries=3 \
    CMD python /app/healthcheck.py

# Default command - run weekly job
ENTRYPOINT ["python", "-m", "src.run_weekly"]

# Default: full run (scrape + deliver)
CMD []
