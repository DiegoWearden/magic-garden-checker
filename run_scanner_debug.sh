#!/bin/bash

# Script to run ws_scan_items.py with proper Playwright browser path (non-headless for debugging)
# Usage: ./run_scanner_debug.sh [additional args]

# Set the project directory
PROJECT_DIR="/home/diego/meeting-handler-bot"

# Change to project directory
cd "$PROJECT_DIR"

# Activate virtual environment
source venv/bin/activate

# If PLAYWRIGHT_BROWSERS_PATH is not set, try common locations
if [ -z "$PLAYWRIGHT_BROWSERS_PATH" ]; then
  CANDIDATES=(
    "$PROJECT_DIR/.playwright-browsers"
    "$PROJECT_DIR/.ms-playwright"
    "$HOME/.cache/ms-playwright"
    "$HOME/.ms-playwright"
    "/usr/lib/ms-playwright"
    "/var/cache/ms-playwright"
  )
  for d in "${CANDIDATES[@]}"; do
    if [ -d "$d" ]; then
      export PLAYWRIGHT_BROWSERS_PATH="$d"
      break
    fi
  done
fi

echo "Starting scanner with debug output..."
echo "Browser path: ${PLAYWRIGHT_BROWSERS_PATH:-<not set>}"
echo "Target URL: https://magiccircle.gg/r/LDQK"
echo "Running in non-headless mode for debugging..."

# Run the scanner without headless mode to see what's happening
python ws_scan_items.py --timeout 30 --debug --out discovered_items.json "$@"
