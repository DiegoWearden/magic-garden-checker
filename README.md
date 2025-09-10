# Meeting Handler Bot

Lightweight Discord bot that fetches and returns rendered HTML for a target webpage (default: https://magiccircle.gg/r/LDQK).

## Requirements

- Python 3.8+
- A Discord bot token (set in .env as `DISCORD_TOKEN`)
- Debian/Ubuntu/Raspberry Pi OS: system libraries required by Playwright (see Troubleshooting)

## Setup

1. Create and activate a virtual environment:

   python -m venv venv
   source venv/bin/activate

2. Install Python dependencies:

   pip install -r requirements.txt

3. Install Playwright system dependencies and browsers:

   # Install OS-level dependencies recommended by Playwright
   sudo playwright install-deps

   # Install browser binaries into the project cache
   PLAYWRIGHT_BROWSERS_PATH=./.playwright-cache playwright install chromium

   If you prefer the default cache location, run `playwright install` without `PLAYWRIGHT_BROWSERS_PATH`.

4. Create a `.env` file with your Discord token:

   DISCORD_TOKEN=your_bot_token_here

5. Ensure the headless binary is executable (if using project cache):

   chmod +x ./.playwright-cache/chromium_headless_shell-*/chrome-linux/headless_shell

## Run

With the virtual environment active:

   python bot.py

## Bot commands

- `!fetch_html [url]` - Renders the page (default URL is defined by `TARGET_URL` or the built-in default) and returns HTML. If the page is large, it sends a `page.html` file.
- `!ping` - Replies `pong`.

## Configuration

- Set `TARGET_URL` in `.env` to change the default URL.
- The code sets the environment variable `PLAYWRIGHT_BROWSERS_PATH` to `./.playwright-cache` so Playwright uses the local cache when available.

## Troubleshooting

- If Playwright complains about missing libraries, run:

  sudo playwright install-deps

  or install specific packages reported by the installer using `apt`.

- If Playwright cannot find the executable, ensure the environment variable is set before starting the bot:

  export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-cache"
  python bot.py

- If you get permission errors when installing browsers globally with `sudo`, use the project cache method shown above.

- Make sure the bot has the "Message Content Intent" enabled in the Discord Developer Portal and that the bot has permissions to read and send messages in the server.

## License

No license specified.