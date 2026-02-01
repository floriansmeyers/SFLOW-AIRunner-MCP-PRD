#!/bin/bash
set -e

APP_DIR="/opt/mcpserver/app"
LOG_FILE="/var/log/mcpserver-deploy.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

cd "$APP_DIR"

# Fetch latest changes
git fetch origin main

# Check if there are new commits
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
    log "New commits detected. Deploying..."

    # Pull changes
    git pull origin main

    # Activate venv and update dependencies
    source venv/bin/activate
    pip install -r requirements.txt --quiet

    # Install Playwright browsers if playwright is in requirements
    if grep -q "playwright" requirements.txt; then
        playwright install chromium --with-deps 2>/dev/null || true
    fi

    # Restart the service
    systemctl restart mcpserver

    log "Deploy complete. New HEAD: $(git rev-parse --short HEAD)"
else
    log "No changes detected."
fi
