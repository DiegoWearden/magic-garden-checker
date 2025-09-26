#!/bin/bash

# Mirror start_bot.sh env and run the ws scanner

(
  export XDG_CACHE_HOME="$PWD/.cache"
  export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers"
  ./venv/bin/python -m playwright install chromium && \
  ./venv/bin/python ws_scan_items.py --timeout 30 --debug --headless --out discovered_items.json
)


