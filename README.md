# Magic Garden Checker

A small Discord bot that attaches to an external Chromium/Chrome (via CDP) and watches the Magic Garden shop page for items and restocks. It supports one-off checks, periodic scans, and automatic alerts when items matching configured filters are detected.

---

## Quick overview
- The bot attaches to a browser's CDP endpoint (it will not launch a browser for you).
- Use the Discord commands below to run checks, take screenshots, and configure server thresholds.
- Keep a Chromium tab open with the shop page(s) you want the bot to inspect.

---

## Prerequisites
- Python 3.9+
- An external Chromium/Chrome running with remote debugging enabled, or Playwright with an exposed CDP endpoint
- A Discord bot token (bot invited to your server with send_messages permission)

---

## Install
Create and activate a virtualenv, then install dependencies:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

If you installed Playwright and want to use Playwright browsers:

```bash
playwright install
```

---

## Run a Chromium instance for CDP
Example using Google Chrome / Chromium (adjust paths/URL):

```bash
chromium-browser --headless=new \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir=/home/diego/chrome-profile \
  --disable-gpu --no-first-run --no-default-browser-check \
  https://magiccircle.gg/r/LDQK
```

This exposes a CDP endpoint at: http://127.0.0.1:9222

---

## Configuration (.env)
Create a `.env` file in the project directory. Minimal required setting:

```env
DISCORD_TOKEN=your_bot_token_here
```

Optional overrides (examples):

```env
# CDP endpoint (default used by the bot)
CDP_DEFAULT=http://127.0.0.1:9222

# Default rarity threshold for automatic alerts
RESTOCK_RARITY_THRESHOLD=mythic
```

Valid rarities: `common`, `uncommon`, `rare`, `epic`, `legendary`, `mythic`.

---

## Start the bot
Run the bot normally:

```bash
python bot.py
```

Start the bot and schedule the periodic checker to start at a Chicago time (interprets input as America/Chicago):

```bash
python bot.py --periodic-start 10:37:01pm
```

---

## Discord commands (copyable)
Each command is listed on its own line so you can copy/paste directly into Discord.

General

```
!help
```

Owner-only commands

```
!start_periodic_check [minutes]
```

```
!stop_periodic_check
```

```
!run_seed_check
```

```
!set_plant_threshold <plant name>
```

```
!clear_plant_threshold
```

Server administrator commands

```
!set_server_threshold <rarity>
```

```
!set_server_plant <plant name>
```

```
!clear_server_plant
```

```
!set_server_notify_new_only <true|false>
```

```
!set_server_channel <channel_id>
```

```
!show_server_settings
```

Utility / Debugging commands

```
!check_threshold [index] [endpoint] ["Plant Name"]
```

```
!current_html [index] [endpoint]
```

```
!screenshot [index] [full] [endpoint]
```

```
!in_stock [index] [endpoint]
```

```
!list_plants
```

---

## Behavior notes
- The bot attaches to an external browser tab. Make sure the shop page is open in that browser instance.
- Notifications are deduped per-page; the bot remembers which item names already triggered alerts and will only notify again when new names appear. Restarting the bot clears in-memory state.
- If a server has neither a rarity nor a plant threshold configured, it will not receive automatic notifications.
- Use `!list_plants` to get exact plant/item names for `!set_plant_threshold` or `!set_server_plant`.

---

## Troubleshooting
- Scheduled start time appears many hours away: the input is interpreted in America/Chicago; verify the host clock/timezone (`date` or `timedatectl status`).
- No items detected: run `!current_html` or `!screenshot` to inspect the page and confirm selectors.
- Bot cannot attach to CDP: ensure Chrome is running with `--remote-debugging-port` and that `CDP_DEFAULT` matches the endpoint.

---

## Development notes
- The bot uses a Playwright CDP connector to attach to external browsers but can also use Playwright-launched browsers with an exposed CDP endpoint.

---

## License
Provided as-is. Modify at your own risk.