# Magic Garden Checker

A small Discord bot that attaches to a user-managed Chromium via CDP and watches the Magic Garden shop page for items and restocks. It can run one-off checks, periodic scans, and send automatic alerts when items matching configured filters are detected.

---

## Features

- Attach to an external Chromium / Chrome / Chromium-based browser via CDP (no embedded browser launched automatically).
- One-off checks: fetch page HTML, take screenshots, list in-stock items.
- Periodic scans with optional schedule (interpreted in `America/Chicago` timezone).
- Automatic notifications for items that are in-stock and meet a rarity threshold (deduped so alerts are sent only for new items).
- Owner-only control commands for starting/stopping scans and changing thresholds.

---

## Prerequisites

- Python 3.9+ (uses `zoneinfo`)
- An external Chromium/Chrome running with remote debugging enabled (or Playwright browsers with a CDP endpoint)
- A Discord bot token and the bot invited to your server with `send_messages` permission

---

## Install

1. Create and activate a virtual environment:

```bash
python -m venv venv
source venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. (Optional) If you installed Playwright and want Playwright browsers:

```bash
playwright install
```

---

## Run a Chromium instance for CDP

Example using Google Chrome / Chromium:

```bash
chromium-browser --headless=new \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir=/home/diego/chrome-profile \
  --disable-gpu --no-first-run --no-default-browser-check \
  https://magiccircle.gg/r/LDQK
```

This exposes a CDP endpoint at `http://127.0.0.1:9222` (the default used by the bot).

---

## Configuration

Create a `.env` file in the project directory with at minimum:

```
DISCORD_TOKEN=your_bot_token_here
```

Optional overrides:

```
CDP_DEFAULT=http://127.0.0.1:9222
RESTOCK_RARITY_THRESHOLD=mythic
```

### Key settings

- `DISCORD_TOKEN` — required. Bot token used to log in.
- `CDP_DEFAULT` — CDP endpoint to attach to (default: `http://127.0.0.1:9222`).
- `RESTOCK_RARITY_THRESHOLD` — default rarity threshold for automatic alerts. Valid values: `common`, `uncommon`, `rare`, `epic`, `legendary`, `mythic`.

---

## Usage

Start the bot:

```bash
python bot.py
```

Start the bot and schedule the periodic checker to start at a Chicago time (interpreted as `America/Chicago`):

```bash
python bot.py --periodic-start 10:37:01pm
```

> Note: the scheduled input is interpreted in `America/Chicago` and converted to the host clock.

---

## Discord commands

General:

- `!help` — Show help (lists commands and usage).

Owner-only commands:

- `!set_threshold <rarity>` — Set the restock rarity threshold. Valid: `common`, `uncommon`, `rare`, `epic`, `legendary`, `mythic`.
- `!start_periodic_check [minutes]` — Start periodic scans every X minutes (default: 5).
- `!stop_periodic_check` — Stop the periodic scanner.
- `!run_seed_check` — Run a one-off immediate scan and (if matches) send alerts.

Utility commands (available to anyone):

- `!current_html [index] [endpoint]` — Return HTML of the attached page (index defaults to `0`). Useful for debugging selectors.
- `!screenshot [index] [full] [endpoint]` — Take a screenshot. Use `true` for full-page capture.
- `!in_stock [index] [endpoint]` — List items currently detected as in-stock on the selected page.

---

## Behavior notes

- The bot attaches to an external browser tab. Make sure the shop page is open in that browser.
- Notifications are deduped per-page; the bot remembers which item names have already triggered alerts and will only notify again when new names appear. Restarting the bot clears in-memory state.
- If you need the bot to always notify (even for the same items), request an additional command to clear the dedupe cache or to disable dedupe.

---

## Troubleshooting

- If the scheduled start appears to be many hours away, check the host clock/timezone. The scheduled input is interpreted in `America/Chicago`. Run `date` or `timedatectl status` on the host to verify.
- If no items are detected, run `!current_html` or `!screenshot` and inspect the output; selectors are based on `button.chakra-button` and `p.chakra-text.css-swfl2y`.
- If the bot cannot attach to CDP, ensure Chrome is running with `--remote-debugging-port` and `CDP_DEFAULT` matches the endpoint.

---

## Development notes

- The bot uses the Playwright CDP connector to attach to an external browser. You can also use a Playwright-launched browser and expose its CDP endpoint.

---

## License

This repository contains user-specific scripts and is provided as-is. Modify at your own risk.