import os
import io
from pathlib import Path
from typing import Optional
import asyncio
import re
import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# If you installed Playwright browsers in a local cache, prefer that before importing Playwright
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path(__file__).resolve().parent / ".playwright-cache"),
)

import discord
from discord.ext import commands
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CDP_DEFAULT = os.getenv("CDP_DEFAULT", "http://127.0.0.1:9222")

# Parse command-line arguments (optional periodic start time HH:MM:SS or 12-hour like 9:10:30pm)
parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--periodic-start', type=str, default=None,
                    help='Optional time of day to auto-start periodic checks, format HH:MM[:SS] (24h) or h[:mm[:ss]]am/pm e.g. 9:10pm or 9:10:30pm')
# parse known args so this won't interfere when invoked by other tooling
_args, _unknown = parser.parse_known_args()
PERIODIC_START_STR = _args.periodic_start
PERIODIC_START_TIME = None
if PERIODIC_START_STR:
    s = PERIODIC_START_STR.strip().lower()
    # Accept formats like '9:10:30pm', '9:10 pm', '09:10:30', '21:10:30', '9pm', '9:05am', '9:05:07am'
    import re
    # groups: 1=hour, 2=minute (optional), 3=second (optional), 4=am/pm (optional)
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2})(?::(\d{2}))?)?\s*(am|pm)?\s*$", s)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        ss = int(m.group(3) or 0)
        ampm = m.group(4)
        if ampm:
            # 12-hour -> 24-hour conversion
            if hh == 12 and ampm == 'am':
                hh = 0
            elif hh != 12 and ampm == 'pm':
                hh = hh + 12
        # If no am/pm provided, assume 24-hour input; validate ranges
        if 0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60:
            PERIODIC_START_TIME = (hh, mm, ss)
    else:
        PERIODIC_START_TIME = None

# Remove RESTOCK_TRIGGER_TIME env usage; disable the exact timer-trigger unless set elsewhere
TRIGGER_MIN = None
TRIGGER_SEC = None

# Minimal bot that attaches to an external headless Chromium and returns the current page HTML
class SimpleCDPBot(commands.Bot):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.playwright = None
        # Previously cdp_browser for connect_over_cdp; now we launch our own browser
        self.browser = None
        self.cdp_pages = []

    async def setup_hook(self):
        # Do not launch an internal browser. The bot will connect to an external browser over CDP
        # when needed via _ensure_attached. This keeps the browser independent and manual.
        self.playwright = None
        self.browser = None
        self.cdp_pages = []

    async def close(self):
        # On close, stop any local Playwright client state but do not attempt to launch/close
        # an external browser that the user manages.
        try:
            if getattr(self, 'playwright', None):
                try:
                    await self.playwright.stop()
                except Exception:
                    pass
        finally:
            await super().close()

intents = discord.Intents.default()
intents.message_content = True
# Disable the default built-in help command so we can register our own custom !help
bot = SimpleCDPBot(command_prefix="!", intents=intents, help_command=None)

# Track which pages we've already notified to avoid duplicate alerts
_last_restock_notified = {}
# New: per-page timer notification dedupe
_last_timer_notified = {}
# New: per-page set of names already notified for rarity-threshold scans
_last_threshold_notified = {}

RARITY_PRIORITY = {
    'common': 0,
    'uncommon': 1,
    'rare': 2,
    'epic': 3,
    'legendary': 4,
    'mythic': 5,
}

# Rarity threshold configuration
# - Default is read from RESTOCK_RARITY_THRESHOLD env var (e.g. "mythic").
# - Can be changed at runtime via set_rarity_threshold() or the !set_threshold command (bot owner only).
RARITY_THRESHOLD_NAME = os.getenv("RESTOCK_RARITY_THRESHOLD", "mythic").lower()
RARITY_THRESHOLD_VALUE = RARITY_PRIORITY.get(RARITY_THRESHOLD_NAME, max(RARITY_PRIORITY.values()))

# Default periodic check interval (change this value in code to adjust how often the threshold checker runs)
# Value is in seconds. Default = 5 minutes.
PERIODIC_INTERVAL_SECONDS = 300

# Control dedupe behavior for automatic notifications:
# - If True, notify ONLY when new threshold-passing item names appear (previous behavior).
# - If False, notify every scan as long as threshold-passing items are present.
NOTIFY_ON_NEW_ONLY = False

# Print configured threshold on startup for visibility
print(f"Restock rarity threshold: {RARITY_THRESHOLD_NAME} ({RARITY_THRESHOLD_VALUE})")

def set_rarity_threshold(name: str) -> bool:
    """Set the runtime rarity threshold. Returns True if accepted."""
    global RARITY_THRESHOLD_NAME, RARITY_THRESHOLD_VALUE
    n = (name or "").lower()
    if n in RARITY_PRIORITY:
        RARITY_THRESHOLD_NAME = n
        RARITY_THRESHOLD_VALUE = RARITY_PRIORITY[n]
        return True
    return False

@bot.command(name="set_threshold")
@commands.is_owner()
async def cmd_set_threshold(ctx, rarity: str):
    """Bot owner only: set the restock rarity threshold (e.g. common, rare, mythic)."""
    if set_rarity_threshold(rarity):
        await ctx.send(f"Rarity threshold set to {RARITY_THRESHOLD_NAME} ({RARITY_THRESHOLD_VALUE})")
    else:
        await ctx.send(f"Unknown rarity '{rarity}'. Valid options: {', '.join(RARITY_PRIORITY.keys())}")

@bot.command(name="help")
async def cmd_help(ctx):
    """Show available commands and basic usage."""
    embed = discord.Embed(title="Magic Garden Checker — Commands", color=0x2ecc71)
    embed.description = "Use these commands to inspect the shop page, control the periodic checker, and change the rarity threshold. Owner-only commands require the bot owner."

    owner_cmds = (
        "!set_threshold <rarity> — Set notification threshold (common, uncommon, rare, epic, legendary, mythic)\n"
        "!start_periodic_check [minutes] — Start periodic scans (default uses built-in interval)\n"
        "!stop_periodic_check — Stop the periodic scanner\n"
        "!run_seed_check — Run an immediate one-off scan"
    )

    utility_cmds = (
        "!current_html [index] [endpoint] — Return page HTML for debugging\n"
        "!screenshot [index] [full] [endpoint] — Capture a screenshot (use 'true' for full)\n"
        "!in_stock [index] [endpoint] — List items detected as in-stock"
    )

    notes = (
        "• The bot attaches to an external Chromium via CDP (CDP_DEFAULT).\n"
        "• Schedule periodic start with --periodic-start (interpreted as America/Chicago).\n"
        "• Use !set_threshold to change the runtime threshold.\n"
        "• Owner commands require the bot owner to run them."
    )

    embed.add_field(name="Owner-only commands", value=f"```\n{owner_cmds}\n```", inline=False)
    embed.add_field(name="Utility commands", value=f"```\n{utility_cmds}\n```", inline=False)
    embed.add_field(name="Notes", value=notes, inline=False)

    await ctx.send(embed=embed)

async def _notify_all_guilds(message: str):
    """Send message to the first writable text channel in each guild. Returns number of guilds messaged."""
    sent = 0
    for guild in bot.guilds:
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me or bot.user)
            if perms.send_messages:
                try:
                    await channel.send(message)
                    sent += 1
                except Exception:
                    pass
                break
    print(f"_notify_all_guilds: attempted to send message to {sent} guild(s)")
    return sent

async def _parse_page_for_restock_and_alert(page):
    """Returns True if restock banner found and notifications sent for new mythic+ items."""
    try:
        # Quick check: is the restock banner present on the page?
        has_restock = await page.evaluate(r"""
            () => {
                try {
                    const text = document.body.innerText || '';
                    return /Seeds\s+Restocked!/i.test(text);
                } catch (e) { return false; }
            }
        """)
        if not has_restock:
            # clear previous flag for this page so future restocks notify again
            _last_restock_notified.pop(page.url, None)
            return False

        # If we've already notified for this page while banner still present, skip
        if _last_restock_notified.get(page.url):
            return False

        # Extract items with name, stockText, count and rarity (simple heuristics)
        items = await page.evaluate(r"""
            () => {
                const out = [];
                const cards = Array.from(document.querySelectorAll('button.chakra-button'));
                for (const card of cards) {
                    const nameEl = card.querySelector('p.chakra-text.css-swfl2y') || card.querySelector('p.chakra-text');
                    const itemNameEl = nameEl;
                    const itemName = (itemNameEl && itemNameEl.textContent || '').trim();
                    if (!itemName) continue;

                    // stock text
                    let stockEl = card.querySelector('.McFlex.css-n6egec p.chakra-text') ||
                                  Array.from(card.querySelectorAll('p.chakra-text, span, div')).find(n => /X\s*\d+|no\s+local|no\s+stock/i.test(n.textContent||''));
                    const stockText = (stockEl && stockEl.textContent || '').trim();
                    const m = stockText.match(/X\s*(\d+)/i);
                    const count = m ? parseInt(m[1], 10) : null;

                    // rarity heuristics: look for words like Mythic/Legendary/Epic/Rare near the card
                    const rarityCandidate = Array.from(card.querySelectorAll('p, span, div')).map(n => (n.textContent||'').toLowerCase()).join(' ');
                    let rarity = '';
                    if (/mythic/i.test(rarityCandidate)) rarity = 'mythic';
                    else if (/legendary/i.test(rarityCandidate)) rarity = 'legendary';
                    else if (/epic/i.test(rarityCandidate)) rarity = 'epic';
                    else if (/rare/i.test(rarityCandidate)) rarity = 'rare';
                    else if (/uncommon/i.test(rarityCandidate)) rarity = 'uncommon';
                    else rarity = 'common';

                    out.push({name: itemName, stockText, count, rarity});
                }
                return out;
            }
        """)

        if not items:
            _last_restock_notified[page.url] = True
            return False

        # Filter for mythic-or-above by priority
        mythic_and_above = [it for it in items if RARITY_PRIORITY.get(it.get('rarity','common'),0) >= RARITY_THRESHOLD_VALUE]
        if not mythic_and_above:
            _last_restock_notified[page.url] = True
            return False

        # Build message
        lines = [f"Restock detected on {page.url} - Mythic+ items:"]
        for it in mythic_and_above:
            count = it.get('count')
            stock = it.get('stockText') or ''
            if count is not None:
                lines.append(f"- {it.get('name')} — {stock} ({count} units) — {it.get('rarity')}")
            else:
                lines.append(f"- {it.get('name')} — {stock} — {it.get('rarity')}")
        msg = '\n'.join(lines)

        await _notify_all_guilds(msg)
        _last_restock_notified[page.url] = True
        return True
    except Exception:
        return False

async def _check_timer_and_alert(page):
    """Check the page for the timer element (.css-1srsqcm). If TRIGGER_MIN/TRIGGER_SEC are not
    configured, this check is skipped. Returns True if notifications were sent."""
    # If no trigger configured, skip this check
    if TRIGGER_MIN is None or TRIGGER_SEC is None:
        return False
    try:
        # grab the visible text of the timer container
        txt = await page.evaluate(r"""
            () => {
                const el = document.querySelector('.css-1srsqcm');
                return el ? (el.innerText || '').trim() : null;
            }
        """)
        if not txt:
            _last_timer_notified.pop(page.url, None)
            return False

        # find the first two numeric groups (minutes, seconds)
        found = re.findall(r"(\d{1,2})", txt)
        if len(found) < 2:
            return False
        minutes = int(found[0])
        seconds = int(found[1])

        if (minutes, seconds) != (TRIGGER_MIN, TRIGGER_SEC):
            # clear previous flag so a future match will notify again
            _last_timer_notified.pop(page.url, None)
            return False

        # dedupe: avoid notifying repeatedly while timer stays at the same value
        key = f"{TRIGGER_MIN}:{TRIGGER_SEC}"
        if _last_timer_notified.get(page.url) == key:
            return False

        # Reuse the same DOM extraction as the restock path to collect items with rarity
        items = await page.evaluate(r"""
            () => {
                const out = [];
                const cards = Array.from(document.querySelectorAll('button.chakra-button'));
                for (const card of cards) {
                    const nameEl = card.querySelector('p.chakra-text.css-swfl2y') || card.querySelector('p.chakra-text');
                    const itemNameEl = nameEl;
                    const itemName = (itemNameEl && itemNameEl.textContent || '').trim();
                    if (!itemName) continue;

                    let stockEl = card.querySelector('.McFlex.css-n6egec p.chakra-text') ||
                                  Array.from(card.querySelectorAll('p.chakra-text, span, div')).find(n => /X\s*\d+|no\s+local|no\s+stock/i.test(n.textContent||''));
                    const stockText = (stockEl && stockEl.textContent || '').trim();
                    const m = stockText.match(/X\s*(\d+)/i);
                    const count = m ? parseInt(m[1], 10) : null;

                    const rarityCandidate = Array.from(card.querySelectorAll('p, span, div')).map(n => (n.textContent||'').toLowerCase()).join(' ');
                    let rarity = '';
                    if (/mythic/i.test(rarityCandidate)) rarity = 'mythic';
                    else if (/legendary/i.test(rarityCandidate)) rarity = 'legendary';
                    else if (/epic/i.test(rarityCandidate)) rarity = 'epic';
                    else if (/rare/i.test(rarityCandidate)) rarity = 'rare';
                    else if (/uncommon/i.test(rarityCandidate)) rarity = 'uncommon';
                    else rarity = 'common';

                    out.push({name: itemName, stockText, count, rarity});
                }
                return out;
            }
        """)

        if not items:
            _last_timer_notified[page.url] = key
            return False

        # Filter items that meet the configured threshold
        candidates = [it for it in items if RARITY_PRIORITY.get(it.get('rarity','common'),0) >= RARITY_THRESHOLD_VALUE]
        if not candidates:
            _last_timer_notified[page.url] = key
            return False

        # Build and send message
        trigger_str = f"{TRIGGER_MIN}:{TRIGGER_SEC:02d}"
        lines = [f"Timer trigger {trigger_str} detected on {page.url} - items at or above {RARITY_THRESHOLD_NAME}:"]
        for it in candidates:
            count = it.get('count')
            stock = it.get('stockText') or ''
            if count is not None:
                lines.append(f"- {it.get('name')} — {stock} ({count} units) — {it.get('rarity')}")
            else:
                lines.append(f"- {it.get('name')} — {stock} — {it.get('rarity')}")
        msg = '\n'.join(lines)

        await _notify_all_guilds(msg)
        _last_timer_notified[page.url] = key
        return True
    except Exception:
        return False

async def _scan_and_notify_threshold(page):
    """Scan a page for items at or above the configured rarity threshold.
    If any qualifying items are found and at least one is new since the last scan for this page,
    send a message to all guilds listing the currently available qualifying items.
    Returns True if a notification was sent.
    """
    try:
        print(f"_scan_and_notify_threshold: scanning page {getattr(page, 'url', 'unknown')}")
        items = await page.evaluate(r"""
            () => {
                const out = [];
                const cards = Array.from(document.querySelectorAll('button.chakra-button'));
                for (const card of cards) {
                    const nameEl = card.querySelector('p.chakra-text.css-swfl2y') || card.querySelector('p.chakra-text');
                    const itemNameEl = nameEl;
                    const itemName = (itemNameEl && itemNameEl.textContent || '').trim();
                    if (!itemName) continue;

                    let stockEl = card.querySelector('.McFlex.css-n6egec p.chakra-text') ||
                                  Array.from(card.querySelectorAll('p.chakra-text, span, div')).find(n => /X\s*\d+|no\s+local|no\s+stock/i.test(n.textContent||''));
                    const stockText = (stockEl && stockEl.textContent || '').trim();
                    const m = stockText.match(/X\s*(\d+)/i);
                    const count = m ? parseInt(m[1], 10) : null;

                    const rarityCandidate = Array.from(card.querySelectorAll('p, span, div')).map(n => (n.textContent||'').toLowerCase()).join(' ');
                    let rarity = '';
                    if (/mythic/i.test(rarityCandidate)) rarity = 'mythic';
                    else if (/legendary/i.test(rarityCandidate)) rarity = 'legendary';
                    else if (/epic/i.test(rarityCandidate)) rarity = 'epic';
                    else if (/rare/i.test(rarityCandidate)) rarity = 'rare';
                    else if (/uncommon/i.test(rarityCandidate)) rarity = 'uncommon';
                    else rarity = 'common';

                    out.push({name: itemName, stockText, count, rarity});
                }
                return out;
            }
        """)

        if not items:
            print(f"_scan_and_notify_threshold: no items found on {getattr(page, 'url', 'unknown')}")
            _last_threshold_notified.pop(page.url, None)
            return False

        # Filter to items that appear to be in stock (exclude those with explicit NO/NO LOCAL STOCK labels)
        in_stock = []
        no_stock_re = re.compile(r"no\s+local\s+stock|no\s+local|no\s+stock", re.I)
        for it in items:
            stock_text = (it.get('stockText') or '')
            # If the stock label explicitly says no stock, treat as out of stock
            if no_stock_re.search(stock_text):
                continue
            # Otherwise, consider it in-stock (count present or an X## label)
            if it.get('count') is not None or re.search(r"X\s*\d+", stock_text or '', re.I):
                in_stock.append(it)

        # Now apply the configured rarity threshold to the in-stock list
        in_stock_filtered = [it for it in in_stock if RARITY_PRIORITY.get(it.get('rarity','common'), 0) >= RARITY_THRESHOLD_VALUE]
        print(f"_scan_and_notify_threshold: found {len(items)} total items, {len(in_stock)} in-stock, {len(in_stock_filtered)} in-stock meeting threshold {RARITY_THRESHOLD_NAME}")

        # If no in-stock items meet the threshold, clear stored state and skip
        if not in_stock_filtered:
            _last_threshold_notified[page.url] = set()
            return False

        # Dedupe on the filtered in-stock names so we only notify when new threshold-passing items appear
        current_names = {it.get('name') for it in in_stock_filtered}
        if NOTIFY_ON_NEW_ONLY:
            prev_names = _last_threshold_notified.get(page.url, set())
            new_names = current_names - prev_names
            if not new_names and prev_names:
                print(f"_scan_and_notify_threshold: no new threshold-passing in-stock items (prev={len(prev_names)}, curr={len(current_names)})")
                _last_threshold_notified[page.url] = current_names
                return False
        else:
            # Always notify when threshold-passing items are present; still update dedupe map afterwards
            print(f"_scan_and_notify_threshold: NOTIFY_ON_NEW_ONLY=False, will notify every scan if items present")

        # Build message: include only the filtered in-stock section (threshold-passing items)
        lines = [f"@everyone MAGIC GARDEN ALERT: Items found with rarity >= {RARITY_THRESHOLD_NAME}):"]
        for it in in_stock_filtered:
            count = it.get('count')
            stock = it.get('stockText') or ''
            if count is not None:
                lines.append(f"- {it.get('name')} — {stock} — {it.get('rarity')}")
            else:
                lines.append(f"- {it.get('name')} — {stock} — {it.get('rarity')}")

        msg = "\n".join(lines)

        sent = await _notify_all_guilds(msg)
        print(f"_scan_and_notify_threshold: notification sent={bool(sent)} to {sent} guild(s)")
        _last_threshold_notified[page.url] = current_names
        return bool(sent)
    except Exception as e:
        print(f"_scan_and_notify_threshold: error: {e}")
        return False

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    # NOTE: Do not auto-start the periodic watcher here unless --periodic-start was provided.
    if PERIODIC_START_TIME:
        async def _schedule_start():
            hh, mm, ss = PERIODIC_START_TIME
            # Interpret the provided time as America/Chicago local time regardless of host timezone.
            chicago_tz = ZoneInfo("America/Chicago")
            now_chi = datetime.now(tz=chicago_tz)
            target_chi = now_chi.replace(hour=hh, minute=mm, second=ss, microsecond=0)
            if target_chi <= now_chi:
                target_chi += timedelta(days=1)
            # Convert to UTC and compute delay relative to host clock (UTC-aware) to get correct sleep seconds
            now_utc = datetime.now(tz=ZoneInfo("UTC"))
            target_utc = target_chi.astimezone(ZoneInfo("UTC"))
            delay = (target_utc - now_utc).total_seconds()
            print(f"Scheduled periodic seed check to start at {hh:02d}:{mm:02d}:{ss:02d} America/Chicago (in {int(delay)}s)")
            await asyncio.sleep(delay)
            # start the periodic check at the scheduled time (default defined by PERIODIC_INTERVAL_SECONDS)
            if getattr(bot, '_periodic_task', None) and not bot._periodic_task.done():
                print('Periodic task already running at scheduled start')
                return
            bot._periodic_task = bot.loop.create_task(_periodic_seed_check(PERIODIC_INTERVAL_SECONDS))
            print('Periodic seed check started by schedule')
        bot.loop.create_task(_schedule_start())

# New: periodic seed check task (default 5 minutes) and control commands
async def _periodic_seed_check(interval_seconds: int = PERIODIC_INTERVAL_SECONDS):
    """Periodically run a single seed check across attached pages every interval_seconds."""
    while True:
        try:
            attached = await _ensure_attached(CDP_DEFAULT)
            print(f"_periodic_seed_check: _ensure_attached -> {attached}")
            if not attached:
                # wait and retry next loop
                await asyncio.sleep(interval_seconds)
                continue

            pages = await _get_pages()
            print(f"_periodic_seed_check: checking {len(pages)} page(s)")
            for page in pages:
                print(f"_periodic_seed_check: scanning page {getattr(page,'url', 'unknown')}")
                # Reuse existing parsing/alert functions
                try:
                    await _parse_page_for_restock_and_alert(page)
                except Exception as e:
                    print(f"_periodic_seed_check: restock parse error for {getattr(page,'url','unknown')}: {e}")
                try:
                    await _check_timer_and_alert(page)
                except Exception as e:
                    print(f"_periodic_seed_check: timer check error for {getattr(page,'url','unknown')}: {e}")
                # New: scan for items meeting rarity threshold and notify automatically
                try:
                    await _scan_and_notify_threshold(page)
                except Exception as e:
                    print(f"_periodic_seed_check: threshold scan error for {getattr(page,'url','unknown')}: {e}")
        except Exception as e:
            print(f"_periodic_seed_check: top-level error: {e}")
        await asyncio.sleep(interval_seconds)

@bot.command(name="start_periodic_check")
@commands.is_owner()
async def start_periodic_check(ctx, minutes: Optional[int] = None):
    """Owner-only: start the periodic seed check every <minutes> (default uses PERIODIC_INTERVAL_SECONDS).
    If minutes is omitted the bot uses the global PERIODIC_INTERVAL_SECONDS value defined in the source.
    """
    if getattr(bot, '_periodic_task', None) and not bot._periodic_task.done():
        await ctx.send("Periodic seed check already running.")
        return
    if minutes is None:
        interval = PERIODIC_INTERVAL_SECONDS
    else:
        interval = int(minutes) * 60
    bot._periodic_task = bot.loop.create_task(_periodic_seed_check(interval))
    # Respond with minutes used for readability
    await ctx.send(f"Started periodic seed check every {interval//60} minutes.")

@bot.command(name="stop_periodic_check")
@commands.is_owner()
async def stop_periodic_check(ctx):
    """Owner-only: stop the periodic seed check."""
    task = getattr(bot, '_periodic_task', None)
    if not task:
        await ctx.send("No periodic seed check is running.")
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    bot._periodic_task = None
    await ctx.send("Stopped periodic seed check.")

@bot.command(name="run_seed_check")
@commands.is_owner()
async def run_seed_check(ctx):
    """Owner-only: run a one-off seed check immediately."""
    await ctx.send("Running seed check now...")
    if not getattr(bot, 'browser', None):
        await _ensure_attached(CDP_DEFAULT)
    pages = await _get_pages()
    for page in pages:
        await _parse_page_for_restock_and_alert(page)
        await _check_timer_and_alert(page)
    await ctx.send("Seed check complete.")

# Helper: send as inline code block if short, otherwise as file
def _chunk_or_file_payload(text: str, fname: str = "page.html") -> dict:
    if len(text) <= 1900:
        return {"content": f"```html\n{text}\n```"}
    return {"file": discord.File(io.BytesIO(text.encode("utf-8")), filename=fname)}

async def _ensure_attached(endpoint: str = CDP_DEFAULT):
    """Ensure the bot is connected to an external browser via CDP at endpoint.
    Returns True on success. The bot will not launch its own browser; it will attach to the
    user-managed browser so you can prepare tabs/pages manually."""
    # If we already have a connected browser, we're good
    if getattr(bot, 'browser', None):
        return True
    try:
        if not getattr(bot, 'playwright', None):
            bot.playwright = await async_playwright().start()
        # Connect to external browser over CDP
        bot.browser = await bot.playwright.chromium.connect_over_cdp(endpoint)
        # Mark that this is an external connection so close() doesn't try to shut it down
        bot._connected_over_cdp = True
        return True
    except Exception as e:
        print(f"Failed to connect to external CDP endpoint {endpoint}: {e}")
        return False

async def _get_pages() -> list:
    if not getattr(bot, 'browser', None):
        return []
    # refresh pages list across all contexts
    bot.cdp_pages = [p for c in bot.browser.contexts for p in c.pages]
    return bot.cdp_pages

@bot.command(name="current_html")
async def current_html(ctx, index: Optional[int] = None, endpoint: Optional[str] = None):
    """Return the current HTML of the headless browser's page.

    Usage:
      !current_html              -> returns HTML of the first open tab
      !current_html 1            -> returns HTML of tab index 1
      !current_html 0 http://127.0.0.1:9222 -> specify CDP endpoint explicitly
    """
    endpoint = endpoint or CDP_DEFAULT
    attached = await _ensure_attached(endpoint)
    if not attached:
        await ctx.send(f"Failed to attach to CDP endpoint: {endpoint}")
        return

    pages = await _get_pages()
    if not pages:
        await ctx.send("No open pages found in the attached browser.")
        return

    sel = 0 if index is None else int(index)
    if sel < 0 or sel >= len(pages):
        await ctx.send(f"Invalid index {sel}. Must be between 0 and {len(pages)-1}.")
        return

    page = pages[sel]
    try:
        content = await page.content()
        payload = _chunk_or_file_payload(content, fname="current_page.html")
        if "content" in payload:
            await ctx.send(payload["content"])
        else:
            await ctx.send(file=payload["file"])
    except Exception as e:
        await ctx.send(f"Error retrieving page content: {e}")

@bot.command(name="screenshot")
async def screenshot(ctx, index: Optional[int] = None, full: bool = False, endpoint: Optional[str] = None):
    """Capture a screenshot of the selected headless browser tab.

    Usage:
      !screenshot               -> screenshot of first tab (viewport)
      !screenshot 1             -> screenshot of tab index 1 (viewport)
      !screenshot 0 true        -> full-page screenshot of tab 0
      !screenshot 0 false http://127.0.0.1:9222 -> specify CDP endpoint
    """
    endpoint = endpoint or CDP_DEFAULT
    attached = await _ensure_attached(endpoint)
    if not attached:
        await ctx.send(f"Failed to attach to CDP endpoint: {endpoint}")
        return

    pages = await _get_pages()
    if not pages:
        await ctx.send("No open pages found in the attached browser.")
        return

    sel = 0 if index is None else int(index)
    if sel < 0 or sel >= len(pages):
        await ctx.send(f"Invalid index {sel}. Must be between 0 and {len(pages)-1}.")
        return

    page = pages[sel]
    try:
        png = await page.screenshot(full_page=bool(full))
        await ctx.send(file=discord.File(io.BytesIO(png), filename="screenshot.png"))
    except Exception as e:
        await ctx.send(f"Error capturing screenshot: {e}")

@bot.command(name="in_stock")
async def in_stock(ctx, index: Optional[int] = None, endpoint: Optional[str] = None):
    """List items currently in stock on the selected headless browser page.

    Usage:
      !in_stock                -> check first open tab
      !in_stock 1              -> check tab index 1
      !in_stock 0 http://...   -> specify CDP endpoint

    The command looks for shop/cards rendered as buttons (button.chakra-button) and
    reads the name and stock label (e.g. "X7 Stock" vs "NO STOCK" / "NO LOCAL STOCK").
    """
    endpoint = endpoint or CDP_DEFAULT
    attached = await _ensure_attached(endpoint)
    if not attached:
        await ctx.send(f"Failed to attach to CDP endpoint: {endpoint}")
        return

    pages = await _get_pages()
    if not pages:
        await ctx.send("No open pages found in the attached browser.")
        return

    sel = 0 if index is None else int(index)
    if sel < 0 or sel >= len(pages):
        await ctx.send(f"Invalid index {sel}. Must be between 0 and {len(pages)-1}.")
        return

    page = pages[sel]
    try:
        # JS runs in the page and returns an array of {name, stockText, count}
        results = await page.evaluate(r"""
            () => {
                const out = [];
                // Find candidate card buttons
                const cards = Array.from(document.querySelectorAll('button.chakra-button'));
                for (const card of cards) {
                    const nameEl = card.querySelector('p.chakra-text.css-swfl2y') || card.querySelector('p.chakra-text');
                    const itemNameEl = nameEl;
                    const itemName = (itemNameEl && itemNameEl.textContent || '').trim();
                    if (!itemName) continue;

                    // stock text appears in a nearby .McFlex.css-n6egec p or similar
                    let stockEl = card.querySelector('.McFlex.css-n6egec p.chakra-text') ||
                                  Array.from(card.querySelectorAll('p.chakra-text, span, div')).find(n => /stock|no\s+local|no\s+stock/i.test(n.textContent||''));
                    const stockText = (stockEl && stockEl.textContent || '').trim();

                    const noStock = /no\s+local\s+stock|no\s+local|no\s+stock/i.test(stockText);
                    const m = stockText.match(/X\s*(\d+)/i);
                    const count = m ? parseInt(m[1], 10) : null;

                    const inStock = !noStock && (count !== null || /X\d+/i.test(stockText));
                    if (inStock) out.push({name: itemName, stockText, count});
                }
                return out;
            }
        """)

        if not results:
            await ctx.send("No items currently in stock were detected on the page.")
            return

        lines = []
        for r in results:
            if r.get('count') is not None:
                lines.append(f"- {r.get('name')} — {r.get('stockText')} ({r.get('count')} units)")
            else:
                lines.append(f"- {r.get('name')} — {r.get('stockText')}")
        msg = "\n".join(lines)
        if len(msg) <= 1900:
            await ctx.send("```\n" + msg + "\n```")
        else:
            await ctx.send(file=discord.File(io.BytesIO(msg.encode('utf-8')), filename='in_stock.txt'))
    except Exception as e:
        await ctx.send(f"Error during in_stock parse: {e}")

if not DISCORD_TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN in .env")

bot.run(DISCORD_TOKEN)
