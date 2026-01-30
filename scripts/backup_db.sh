#!/bin/bash
# Database backup script for BrokenSite-Weekly
# Creates timestamped backups before each run
#
# Usage:
#   ./scripts/backup_db.sh
#   ./scripts/backup_db.sh --list    # List existing backups
#   ./scripts/backup_db.sh --prune 7 # Remove backups older than 7 days

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DATA_DIR="$PROJECT_ROOT/data"
DB_FILE="$DATA_DIR/leads.db"
BACKUP_DIR="$DATA_DIR/backups"

# Parse arguments
ACTION="${1:-backup}"
PRUNE_DAYS="${2:-30}"

list_backups() {
    if [[ -d "$BACKUP_DIR" ]]; then
        echo "Existing backups:"
        ls -lh "$BACKUP_DIR"/*.db 2>/dev/null || echo "  (none)"
    else
        echo "No backup directory found."
    fi
}

prune_backups() {
    local days="$1"
    if [[ -d "$BACKUP_DIR" ]]; then
        echo "Removing backups older than $days days..."
        find "$BACKUP_DIR" -name "leads.db.backup.*" -type f -mtime "+$days" -delete -print
    fi
}

create_backup() {
    # Ensure backup directory exists
    mkdir -p "$BACKUP_DIR"

    # Check if database exists
    if [[ ! -f "$DB_FILE" ]]; then
        echo "No database file found at $DB_FILE - nothing to backup"
        exit 0
    fi

    # Create timestamped backup
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_FILE="$BACKUP_DIR/leads.db.backup.$TIMESTAMP"

    # Use SQLite backup command for consistency (handles write locks)
    if command -v sqlite3 &> /dev/null; then
        sqlite3 "$DB_FILE" ".backup '$BACKUP_FILE'"
    else
        # Fallback to cp if sqlite3 not available
        cp "$DB_FILE" "$BACKUP_FILE"
    fi

    echo "Backup created: $BACKUP_FILE"

    # Show backup size
    if [[ -f "$BACKUP_FILE" ]]; then
        ls -lh "$BACKUP_FILE"
    fi
}

case "$ACTION" in
    --list|-l)
        list_backups
        ;;
    --prune|-p)
        prune_backups "$PRUNE_DAYS"
        ;;
    backup|*)
        create_backup
        ;;
esac
