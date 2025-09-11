import os
import io
from pathlib import Path
from typing import Optional, Dict, Any
import asyncio
import re
import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json

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

# Path for persistent per-guild settings
GUILD_SETTINGS_PATH = Path(__file__).resolve().parent / "guild_settings.json"
# In-memory cache of guild settings loaded from disk
GUILD_SETTINGS: Dict[str, Dict[str, Any]] = {}

def _load_guild_settings() -> None:
    """Load guild settings from JSON on startup (silently ignore missing/invalid file)."""
    global GUILD_SETTINGS
    try:
        if GUILD_SETTINGS_PATH.exists():
            with open(GUILD_SETTINGS_PATH, "r", encoding="utf-8") as f:
                GUILD_SETTINGS = json.load(f)
                # ensure string keys
                GUILD_SETTINGS = {str(k): dict(v or {}) for k, v in GUILD_SETTINGS.items()}
        else:
            GUILD_SETTINGS = {}
    except Exception:
        GUILD_SETTINGS = {}

def _save_guild_settings() -> None:
    """Persist current guild settings to disk (best-effort)."""
    try:
        with open(GUILD_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(GUILD_SETTINGS, f, indent=2, sort_keys=True)
    except Exception:
        pass

def _get_guild_config_for(guild_id: int) -> Dict[str, Any]:
    """Return the effective per-guild config dict with defaults applied."""
    g = GUILD_SETTINGS.get(str(guild_id), {})
    return {
        # Do NOT fall back to the old global RARITY_THRESHOLD_NAME here — prefer per-guild config.
        'rarity': g.get('rarity', None),
        'plant': g.get('plant', None),
        'notify_on_new_only': g.get('notify_on_new_only', NOTIFY_ON_NEW_ONLY),
        'channel_id': g.get('channel_id', None),
    }

# Load settings at module import
_load_guild_settings()

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
    'divine': 6,
    'celestial': 7,
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

# Plant-based threshold (if set, use the rarity of this plant as the threshold)
PLANT_THRESHOLD_NAME = None

# Print configured threshold on startup for visibility
print(f"Restock rarity threshold: {RARITY_THRESHOLD_NAME} ({RARITY_THRESHOLD_VALUE})")

# Canonical full ordering of items (lowest -> highest). Use this for position-based plant thresholds.
FULL_ORDER = [
    "Carrot Seed",
    "Strawberry Seed",
    "Aloe Seed",
    "Blueberry Seed",
    "Apple Seed",
    "Tulip Seed",
    "Tomato Seed",
    "Daffodil Seed",
    "Corn Kernel",
    "Watermelon Seed",
    "Pumpkin Seed",
    "Echeveria Cutting",
    "Coconut Seed",
    "Banana Seed",
    "Lily Seed",
    "Burro's Tail Cutting",
    "Mushroom Spore",
    "Cactus Seed",
    "Bamboo Seed",
    "Grape Seed",
    "Pepper Seed",
    "Lemon Seed",
    "Passion Fruit Seed",
    "Dragon Fruit Seed",
    "Lychee Pit",
    "Sunflower Seed",
    "Starweaver Pod",
]

# Friendly rarity labels for known items (used by list_plants and for logging)
RARITY_MAP = {
    "Carrot Seed": "Common",
    "Strawberry Seed": "Common",
    "Aloe Seed": "Common",
    "Blueberry Seed": "Uncommon",
    "Apple Seed": "Uncommon",
    "Tulip Seed": "Uncommon",
    "Tomato Seed": "Uncommon",
    "Daffodil Seed": "Rare",
    "Corn Kernel": "Rare",
    "Watermelon Seed": "Rare",
    "Pumpkin Seed": "Rare",
    "Echeveria Cutting": "Legendary",
    "Coconut Seed": "Legendary",
    "Banana Seed": "Legendary",
    "Lily Seed": "Legendary",
    "Burro's Tail Cutting": "Legendary",
    "Mushroom Spore": "Mythical",
    "Cactus Seed": "Mythical",
    "Bamboo Seed": "Mythical",
    "Grape Seed": "Mythical",
    "Pepper Seed": "Divine",
    "Lemon Seed": "Divine",
    "Passion Fruit Seed": "Divine",
    "Dragon Fruit Seed": "Divine",
    "Lychee Pit": "Divine",
    "Sunflower Seed": "Divine",
    "Starweaver Pod": "Celestial",
}

def set_rarity_threshold(name: str) -> bool:
    """Set the runtime rarity threshold. Returns True if accepted."""
    global RARITY_THRESHOLD_NAME, RARITY_THRESHOLD_VALUE
    n = (name or "").lower()
    if n in RARITY_PRIORITY:
        RARITY_THRESHOLD_NAME = n
        RARITY_THRESHOLD_VALUE = RARITY_PRIORITY[n]
        return True
    return False

@bot.command(name="set_server_threshold")
@commands.has_permissions(administrator=True)
async def cmd_set_server_threshold(ctx, rarity: str):
    """Server administrators: set this server's rarity threshold. Example: !set_server_threshold divine"""
    if not ctx.guild:
        await ctx.send("This command must be used inside a server/guild channel.")
        return
    n = (rarity or "").lower()
    if n not in RARITY_PRIORITY:
        await ctx.send(f"Unknown rarity '{rarity}'. Valid options: {', '.join(RARITY_PRIORITY.keys())}")
        return
    gid = str(ctx.guild.id)
    cfg = GUILD_SETTINGS.get(gid, {})
    cfg['rarity'] = n
    GUILD_SETTINGS[gid] = cfg
    _save_guild_settings()
    await ctx.send(f"This server's rarity threshold set to {n}.")

@bot.command(name="set_server_plant")
@commands.has_permissions(administrator=True)
async def cmd_set_server_plant(ctx, *, plant: str = None):
    """Server administrators: set a plant name to use as this server's threshold. Example: !set_server_plant "Mushroom Spore""" 
    if not ctx.guild:
        await ctx.send("This command must be used inside a server/guild channel.")
        return
    if not plant:
        await ctx.send("Usage: !set_server_plant <plant name>\nExample: !set_server_plant Mushroom Spore")
        return
    gid = str(ctx.guild.id)
    cfg = GUILD_SETTINGS.get(gid, {})
    cfg['plant'] = plant.strip()
    GUILD_SETTINGS[gid] = cfg
    _save_guild_settings()
    await ctx.send(f"This server's plant-based threshold set to '{plant.strip()}'.")

@bot.command(name="clear_server_plant")
@commands.has_permissions(administrator=True)
async def cmd_clear_server_plant(ctx):
    """Server administrators: clear this server's plant-based threshold so the server uses the rarity threshold instead."""
    if not ctx.guild:
        await ctx.send("This command must be used inside a server/guild channel.")
        return
    gid = str(ctx.guild.id)
    cfg = GUILD_SETTINGS.get(gid, {})
    cfg.pop('plant', None)
    GUILD_SETTINGS[gid] = cfg
    _save_guild_settings()
    await ctx.send("Cleared this server's plant-based threshold; using rarity threshold again.")

@bot.command(name="set_server_notify_new_only")
@commands.has_permissions(administrator=True)
async def cmd_set_server_notify_new_only(ctx, flag: str = 'true'):
    """Server administrators: toggle whether this server only receives notifications for new items (true/false)."""
    if not ctx.guild:
        await ctx.send("This command must be used inside a server/guild channel.")
        return
    val = flag.strip().lower() in ('1','true','yes','y')
    gid = str(ctx.guild.id)
    cfg = GUILD_SETTINGS.get(gid, {})
    cfg['notify_on_new_only'] = val
    GUILD_SETTINGS[gid] = cfg
    _save_guild_settings()
    await ctx.send(f"This server's notify_on_new_only set to {val}.")

@bot.command(name="set_server_channel")
@commands.has_permissions(administrator=True)
async def cmd_set_server_channel(ctx, channel_id: Optional[int] = None):
    """Server administrators: set a channel ID where notifications will be sent for this server. If omitted, the bot falls back to the first writable text channel."""
    if not ctx.guild:
        await ctx.send("This command must be used inside a server/guild channel.")
        return
    gid = str(ctx.guild.id)
    cfg = GUILD_SETTINGS.get(gid, {})
    if channel_id is None:
        cfg.pop('channel_id', None)
        GUILD_SETTINGS[gid] = cfg
        _save_guild_settings()
        await ctx.send("Cleared configured channel; the bot will use the first writable text channel instead.")
        return
    cfg['channel_id'] = int(channel_id)
    GUILD_SETTINGS[gid] = cfg
    _save_guild_settings()
    await ctx.send(f"This server's notification channel set to {channel_id}.")

@bot.command(name="show_server_settings")
@commands.has_permissions(administrator=True)
async def cmd_show_server_settings(ctx):
    """Show the current server-specific configuration (if any)."""
    if not ctx.guild:
        await ctx.send("This command must be used inside a server/guild channel.")
        return
    cfg = _get_guild_config_for(ctx.guild.id)
    lines = [f"Server settings for {ctx.guild.name} (id {ctx.guild.id}):"]
    rarity_val = cfg.get('rarity') if cfg.get('rarity') is not None else 'not configured'
    lines.append(f"- rarity: {rarity_val}")
    lines.append(f"- plant: {cfg.get('plant')}")
    lines.append(f"- notify_on_new_only: {cfg.get('notify_on_new_only')}")
    lines.append(f"- channel_id: {cfg.get('channel_id')}")
    await ctx.send("\n".join(lines))

@bot.command(name="set_plant_threshold")
@commands.is_owner()
async def cmd_set_plant_threshold(ctx, *, plant: str = None):
    """Owner-only: set a plant name whose rarity will be used as the notification threshold.
    Example: !set_plant_threshold "Mushroom Spore" -> use the rarity of Mushroom Spore as the threshold.
    """
    global PLANT_THRESHOLD_NAME
    if not plant:
        await ctx.send("Usage: !set_plant_threshold <plant name>\nExample: !set_plant_threshold Mushroom Spore")
        return
    PLANT_THRESHOLD_NAME = plant.strip()
    await ctx.send(f"Plant threshold set to '{PLANT_THRESHOLD_NAME}'. The bot will use that plant's rarity as the filter when it can find the plant on a page.")

@bot.command(name="clear_plant_threshold")
@commands.is_owner()
async def cmd_clear_plant_threshold(ctx):
    """Owner-only: clear any plant-based threshold so the bot uses the rarity threshold instead."""
    global PLANT_THRESHOLD_NAME
    PLANT_THRESHOLD_NAME = None
    await ctx.send("Cleared plant-based threshold; using rarity threshold again.")

@bot.command(name="help")
async def cmd_help(ctx):
    """Show available commands and basic usage."""
    embed = discord.Embed(title="Magic Garden Checker — Commands", color=0x3498db)
    embed.description = (
        "Quick reference for available commands. Commands marked (owner) require the bot owner; server-admin commands require Administrator permissions."
    )

    quick_start = (
        "• The bot connects to an external Chromium instance over CDP (CDP_DEFAULT).\n"
        "• Start the browser and open the shop page(s) you want the bot to inspect.\n"
        "• Use `!set_server_channel <channel_id>` to pin notifications to a specific channel (recommended).\n"
        "• Optionally schedule automatic periodic checks with `--periodic-start HH:MM[:SS]` when launching the bot."
    )

    owner_cmds_lines = [
        "`!start_periodic_check [minutes]` — Start periodic scans (owner only).",
        "`!stop_periodic_check` — Stop the periodic scanner (owner only).",
        "`!run_seed_check` — Run an immediate one-off scan (owner only).",
        "`!set_plant_threshold <plant name>` — Use the specified plant's rarity as the global threshold (owner only).",
        "`!clear_plant_threshold` — Clear the global plant-based threshold (owner only).",
    ]

    admin_cmds_lines = [
        "`!set_server_threshold <rarity>` — Set this server's rarity threshold (administrator).",
        "`!set_server_plant <plant name>` — Use a specific plant on pages as this server's threshold (administrator).",
        "`!clear_server_plant` — Clear the server's plant-based threshold (administrator).",
        "`!set_server_notify_new_only <true|false>` — Send notifications only for newly seen items (administrator).",
        "`!set_server_channel <channel_id>` — Configure which channel this server receives notifications in (administrator).",
        "`!show_server_settings` — Display this server's current configuration (administrator).",
    ]

    utility_cmds_lines = [
        "`!check_threshold [index] [endpoint] [\"Plant Name\"]` — Show configured thresholds and optionally inspect a plant on a tab.",
        "`!current_html [index] [endpoint]` — Return page HTML for debugging.",
        "`!screenshot [index] [full] [endpoint]` — Capture a screenshot; use `true` for full-page.",
        "`!in_stock [index] [endpoint]` — List items detected as in-stock on the selected tab.",
        "`!list_plants` — Fetch current plant/item names from the open page (useful for `!set_plant_threshold`).",
        "`!help` — Show this help message.",
    ]

    embed.add_field(name="Quick start", value=quick_start, inline=False)
    embed.add_field(name="Owner-only commands", value="\n".join(owner_cmds_lines), inline=False)
    embed.add_field(name="Server administrator commands", value="\n".join(admin_cmds_lines), inline=False)
    embed.add_field(name="Utility commands", value="\n".join(utility_cmds_lines), inline=False)

    notes = (
        "Notes:\n"
        "• If a server has neither a rarity nor plant threshold configured, it will not receive automatic notifications.\n"
        "• Plant thresholds are matched against names discovered on the page; use `!list_plants` to obtain exact names.\n"
        "• The bot attaches to an external browser — ensure the browser is running and accessible at the CDP endpoint."
    )
    embed.add_field(name="Notes", value=notes, inline=False)

    await ctx.send(embed=embed)

@bot.command(name="check_threshold")
async def check_threshold(ctx, index: Optional[int] = None, endpoint: Optional[str] = None, *, plant: Optional[str] = None):
    """Show current threshold configuration. If attached to a page, optionally check a plant name on the page.

    Usage:
      !check_threshold                     -> show configured thresholds
      !check_threshold 0                   -> show thresholds and inspect tab 0
      !check_threshold 0 http://... "Plant Name"  -> inspect plant on tab 0 (plant name may contain spaces)
    """
    endpoint = endpoint or CDP_DEFAULT

    # Prefer per-guild settings when invoked from inside a guild; otherwise show global defaults.
    guild_cfg = None
    if ctx.guild:
        guild_cfg = _get_guild_config_for(ctx.guild.id)

    lines = []
    if guild_cfg:
        # show whether this server has a configured rarity threshold
        if guild_cfg.get('rarity') is not None:
            lines.append(f"Server rarity threshold: {guild_cfg.get('rarity')} ({RARITY_PRIORITY.get(guild_cfg.get('rarity'), RARITY_THRESHOLD_VALUE)})")
        else:
            lines.append("Server rarity threshold: not configured")
        if guild_cfg.get('plant'):
            lines.append(f"Server plant-based threshold configured: '{guild_cfg.get('plant')}'")
        else:
            lines.append("Server plant-based threshold: not configured")
        lines.append(f"Server notify_on_new_only: {guild_cfg.get('notify_on_new_only')}")
        if guild_cfg.get('channel_id'):
            lines.append(f"Server configured channel_id: {guild_cfg.get('channel_id')}")
    else:
        lines.append(f"Global rarity threshold: {RARITY_THRESHOLD_NAME} ({RARITY_THRESHOLD_VALUE})")
        if PLANT_THRESHOLD_NAME:
            lines.append(f"Global plant-based threshold configured: '{PLANT_THRESHOLD_NAME}'")
        else:
            lines.append("Global plant-based threshold: not configured")
        lines.append(f"Global notify_on_new_only: {NOTIFY_ON_NEW_ONLY}")

    # If user only wanted configuration, and no page inspection requested, send short reply
    if plant is None and index is None:
        await ctx.send("\n".join(lines))
        return

    # When inspecting a page, use guild-specific plant if present, otherwise the global PLANT_THRESHOLD_NAME
    effective_plant_threshold = None
    if guild_cfg and guild_cfg.get('plant'):
        effective_plant_threshold = guild_cfg.get('plant')
    else:
        effective_plant_threshold = PLANT_THRESHOLD_NAME

    # Use guild-specific rarity threshold value for comparisons when invoked from a guild
    effective_global_threshold_value = RARITY_THRESHOLD_VALUE
    if guild_cfg:
        effective_global_threshold_value = RARITY_PRIORITY.get(guild_cfg.get('rarity', RARITY_THRESHOLD_NAME), RARITY_THRESHOLD_VALUE)

    # Try to attach to a browser to inspect the page and plant if requested
    attached = await _ensure_attached(endpoint)
    if not attached:
        await ctx.send("\n".join(lines) + f"\nNote: Failed to attach to CDP endpoint: {endpoint}")
        return

    pages = await _get_pages()
    if not pages:
        await ctx.send("\n".join(lines) + "\nNote: No open pages found in the attached browser.")
        return

    sel = 0 if index is None else int(index)
    if sel < 0 or sel >= len(pages):
        await ctx.send(f"Invalid index {sel}. Must be between 0 and {len(pages)-1}.")
        return

    page = pages[sel]
    try:
        # Scrape items with heuristic rarity from the page (same heuristics used elsewhere)
        items = await page.evaluate(r"""
            () => {
                const out = [];
                const cards = Array.from(document.querySelectorAll('button.chakra-button'));
                for (const card of cards) {
                    const nameEl = card.querySelector('p.chakra-text.css-swfl2y') || card.querySelector('p.chakra-text');
                    const itemName = (nameEl && (nameEl.textContent || '').trim()) || '';
                    if (!itemName) continue;
                    const rarityCandidate = Array.from(card.querySelectorAll('p, span, div')).map(n => (n.textContent||'').toLowerCase()).join(' ');
                    let rarity = 'common';
                    if (/celestial/i.test(rarityCandidate)) rarity = 'celestial';
                    else if (/divine/i.test(rarityCandidate)) rarity = 'divine';
                    else if (/mythic|mythical/i.test(rarityCandidate)) rarity = 'mythic';
                    else if (/legendary/i.test(rarityCandidate)) rarity = 'legendary';
                    else if (/epic/i.test(rarityCandidate)) rarity = 'epic';
                    else if (/rare/i.test(rarityCandidate)) rarity = 'rare';
                    else if (/uncommon/i.test(rarityCandidate)) rarity = 'uncommon';
                    out.push({name: itemName, rarity});
                }
                // Deduplicate preserving first-seen order
                const seen = new Set();
                return out.filter(it => {
                    if (seen.has(it.name)) return false;
                    seen.add(it.name);
                    return true;
                });
            }
        """)

        if not items:
            await ctx.send("No item names were found on the page.")
            return

        # Build name->rarity map for quick lookup
        heur = {it.get('name'): it.get('rarity') for it in items}

        # If a specific plant name was requested, try to find it on the page
        if plant:
            pname = plant.strip().lower()
            matched = None
            for it in items:
                iname = (it.get('name') or '').lower()
                if iname == pname or pname in iname:
                    matched = it
                    break

            if not matched:
                lines.append(f"Plant '{plant}' not found on the selected page (tab {sel}).")
                await ctx.send("\n".join(lines))
                return

            name = matched.get('name')
            rarity = matched.get('rarity')
            lines.append(f"Found plant on page: {name} — rarity: {rarity}")

            # Canonical position info if available
            if name in FULL_ORDER:
                idx = FULL_ORDER.index(name)
                lines.append(f"Position in canonical FULL_ORDER: {idx} (0 = lowest rarity -> higher index = rarer)")
            else:
                lines.append("Plant not present in canonical FULL_ORDER; position-based comparisons unavailable.")

            # Determine whether this plant would meet the effective threshold
            # Compute effective threshold according to current configuration and presence of a plant-based threshold
            effective_threshold_value = effective_global_threshold_value
            threshold_source = f"rarity >= {guild_cfg.get('rarity') if guild_cfg else RARITY_THRESHOLD_NAME}"
            # If a plant-based threshold is configured, attempt to locate that plant on the same page
            position_mode = False
            plant_idx = None
            plant_rarity = None
            if effective_plant_threshold:
                pname_cfg = effective_plant_threshold.strip().lower()
                matched_cfg = None
                for it in items:
                    if (it.get('name') or '').lower() == pname_cfg or pname_cfg in (it.get('name') or '').lower():
                        matched_cfg = it
                        break
                if matched_cfg:
                    plant_name_cfg = matched_cfg.get('name')
                    plant_rarity = matched_cfg.get('rarity', 'common')
                    if plant_name_cfg in FULL_ORDER:
                        position_mode = True
                        plant_idx = FULL_ORDER.index(plant_name_cfg)
                        threshold_source = f"plant position '{effective_plant_threshold}' (index={plant_idx})"
                    else:
                        effective_threshold_value = RARITY_PRIORITY.get(plant_rarity, effective_threshold_value)
                        threshold_source = f"plant '{effective_plant_threshold}' (rarity={plant_rarity})"
                else:
                    threshold_source = f"rarity >= {guild_cfg.get('rarity') if guild_cfg else RARITY_THRESHOLD_NAME} (plant threshold '{effective_plant_threshold}' not found on page)"

            # Decide if the inspected plant meets the effective threshold
            meets = False
            if position_mode and plant_idx is not None:
                if name in FULL_ORDER:
                    meets = FULL_ORDER.index(name) >= plant_idx
                else:
                    compare_value = RARITY_PRIORITY.get(plant_rarity, effective_threshold_value)
                    meets = RARITY_PRIORITY.get(rarity, 0) >= compare_value
            else:
                meets = RARITY_PRIORITY.get(rarity, 0) >= effective_threshold_value

            lines.append(f"Effective threshold used for comparison: {threshold_source}")
            lines.append(f"Does '{name}' meet the effective threshold? {'YES' if meets else 'NO'}")
            await ctx.send("\n".join(lines))
            return

        # If no specific plant requested but plant threshold is configured, try to show what plant is being used as threshold
        if effective_plant_threshold:
            pname_cfg = effective_plant_threshold.strip().lower()
            matched_cfg = None
            for it in items:
                if (it.get('name') or '').lower() == pname_cfg or pname_cfg in (it.get('name') or '').lower():
                    matched_cfg = it
                    break
            if matched_cfg:
                p_name = matched_cfg.get('name')
                p_rarity = matched_cfg.get('rarity')
                lines.append(f"Configured plant threshold '{effective_plant_threshold}' found on page as '{p_name}' with rarity {p_rarity}.")
                if p_name in FULL_ORDER:
                    lines.append(f"Using position-based threshold: items at or after index {FULL_ORDER.index(p_name)} in FULL_ORDER will qualify.")
                else:
                    lines.append(f"Plant not in canonical FULL_ORDER; falling back to rarity-based threshold using rarity '{p_rarity}'.")
            else:
                lines.append(f"Configured plant threshold '{effective_plant_threshold}' was not found on the current page (tab {sel}).")

        await ctx.send("\n".join(lines))
    except Exception as e:
        await ctx.send(f"Error while inspecting page: {e}")

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

async def _notify_guilds_with_items(page_url: str, items: list, threshold_source: str) -> int:
    """Notify each guild according to its own configured threshold/plant; return number of guilds messaged.
    items should be the list of in-stock item dicts extracted from the page (name, stockText, count, rarity).
    This function applies per-guild filtering and per-guild dedupe state.
    """
    sent = 0
    for guild in bot.guilds:
        cfg = _get_guild_config_for(guild.id)
        # Determine effective threshold for this guild
        # If the guild has not configured either a rarity or plant threshold, skip notifying it.
        if cfg.get('rarity') is None and not cfg.get('plant'):
            # clear any previous dedupe state for this guild+page so admins can enable later
            _last_threshold_notified.pop(f"{page_url}::{guild.id}", None)
            continue

        effective_value = RARITY_PRIORITY.get(cfg.get('rarity'), RARITY_THRESHOLD_VALUE) if cfg.get('rarity') is not None else RARITY_THRESHOLD_VALUE
        position_mode = False
        plant_idx = None
        plant_rarity = None
        # If guild configured a plant-based threshold, try to find it in the items
        if cfg.get('plant'):
            pname = cfg.get('plant').strip().lower()
            matched = None
            for it in items:
                if (it.get('name') or '').lower() == pname or pname in (it.get('name') or '').lower():
                    matched = it
                    break
            if matched:
                plant_rarity = matched.get('rarity', 'common')
                if matched.get('name') in FULL_ORDER:
                    position_mode = True
                    plant_idx = FULL_ORDER.index(matched.get('name'))
                else:
                    effective_value = RARITY_PRIORITY.get(plant_rarity, effective_value)

        # Filter items per guild using position_mode or rarity
        filtered = []
        if position_mode and plant_idx is not None:
            for it in items:
                name = it.get('name')
                if name in FULL_ORDER:
                    if FULL_ORDER.index(name) >= plant_idx:
                        filtered.append(it)
                else:
                    compare_value = RARITY_PRIORITY.get(plant_rarity, effective_value)
                    if RARITY_PRIORITY.get(it.get('rarity','common'), 0) >= compare_value:
                        filtered.append(it)
        else:
            filtered = [it for it in items if RARITY_PRIORITY.get(it.get('rarity','common'), 0) >= effective_value]

        if not filtered:
            # no items meet this guild's threshold
            # clear per-guild dedupe state for this page so future finds will notify
            _last_threshold_notified.pop(f"{page_url}::{guild.id}", None)
            continue

        # Dedupe: check previous names for this guild+page
        current_names = {it.get('name') for it in filtered}
        key = f"{page_url}::{guild.id}"
        prev_names = _last_threshold_notified.get(key, set())
        notify_on_new = cfg.get('notify_on_new_only', NOTIFY_ON_NEW_ONLY)
        if notify_on_new:
            new_names = current_names - prev_names
            if not new_names and prev_names:
                # nothing new to notify for this guild
                _last_threshold_notified[key] = current_names
                continue

        # Choose channel: configured channel_id takes precedence
        channel = None
        cid = cfg.get('channel_id')
        if cid:
            try:
                channel = guild.get_channel(int(cid))
            except Exception:
                channel = None
        if not channel:
            # fallback to first writable text channel
            for c in guild.text_channels:
                perms = c.permissions_for(guild.me or bot.user)
                if perms.send_messages:
                    channel = c
                    break
        if not channel:
            continue

        # Build message and send
        lines = [f"@everyone MAGIC GARDEN ALERT for {guild.name}: Items found matching threshold ({threshold_source}):"]
        for it in filtered:
            count = it.get('count')
            stock = it.get('stockText') or ''
            if count is not None:
                lines.append(f"- {it.get('name')} — {stock} — {it.get('rarity')}")
            else:
                lines.append(f"- {it.get('name')} — {stock} — {it.get('rarity')}")
        try:
            await channel.send("\n".join(lines))
            sent += 1
            _last_threshold_notified[key] = current_names
        except Exception:
            pass
    print(f"_notify_guilds_with_items: notified {sent} guild(s)")
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

                    // rarity heuristics: look for words like Celestial/Divine/Mythic/Legendary/Epic/Rare near the card
                    const rarityCandidate = Array.from(card.querySelectorAll('p, span, div')).map(n => (n.textContent||'').toLowerCase()).join(' ');
                    let rarity = 'common';
                    if (/celestial/i.test(rarityCandidate)) rarity = 'celestial';
                    else if (/divine/i.test(rarityCandidate)) rarity = 'divine';
                    else if (/mythic|mythical/i.test(rarityCandidate)) rarity = 'mythic';
                    else if (/legendary/i.test(rarityCandidate)) rarity = 'legendary';
                    else if (/epic/i.test(rarityCandidate)) rarity = 'epic';
                    else if (/rare/i.test(rarityCandidate)) rarity = 'rare';
                    else if (/uncommon/i.test(rarityCandidate)) rarity = 'uncommon';

                    out.push({name: itemName, stockText, count, rarity});
                }
                return out;
            }
        """)

        if not items:
            _last_restock_notified[page.url] = True
            return False

        # Filter for mythic-and-above by priority
        mythic_and_above = [it for it in items if RARITY_PRIORITY.get(it.get('rarity','common'),0) >= RARITY_THRESHOLD_VALUE]
        if not mythic_and_above:
            _last_restock_notified[page.url] = True
            return False

        # Instead of sending a separate restock-specific message, defer to the unified
        # threshold scanner which will send notifications according to the configured threshold.
        try:
            sent = await _scan_and_notify_threshold(page)
        except Exception:
            sent = False
        # Mark that we've processed this restock banner so we don't repeatedly handle it
        _last_restock_notified[page.url] = True
        return bool(sent)
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
                    if (/celestial/i.test(rarityCandidate)) rarity = 'celestial';
                    else if (/divine/i.test(rarityCandidate)) rarity = 'divine';
                    else if (/mythic|mythical/i.test(rarityCandidate)) rarity = 'mythic';
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

        # Filter items that appear to be in stock (exclude those with explicit NO/NO LOCAL STOCK labels)
        in_stock = []
        no_stock_re = re.compile(r"no\s+local\s+stock|no\s+local|no\s+stock", re.I)
        for it in items:
            stock_text = (it.get('stockText') or '')
            if no_stock_re.search(stock_text):
                continue
            if it.get('count') is not None or re.search(r"X\s*\d+", stock_text or '', re.I):
                in_stock.append(it)

        if not in_stock:
            _last_timer_notified[page.url] = key
            return False

        # Instead of broadcasting one raw message to all guilds, use per-guild filtering and notify
        sent = await _notify_guilds_with_items(page.url, in_stock, f"timer {TRIGGER_MIN}:{TRIGGER_SEC:02d}")

        _last_timer_notified[page.url] = key
        return bool(sent)
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
                    if (/celestial/i.test(rarityCandidate)) rarity = 'celestial';
                    else if (/divine/i.test(rarityCandidate)) rarity = 'divine';
                    else if (/mythic|mythical/i.test(rarityCandidate)) rarity = 'mythic';
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
            # clear per-page per-guild state
            keys = [k for k in _last_threshold_notified.keys() if k.startswith(f"{page.url}::")]
            for k in keys:
                _last_threshold_notified.pop(k, None)
            return False

        # Filter to items that appear to be in stock (exclude those with explicit NO/NO LOCAL STOCK labels)
        in_stock = []
        no_stock_re = re.compile(r"no\s+local\s+stock|no\s+local|no\s+stock", re.I)
        for it in items:
            stock_text = (it.get('stockText') or '')
            if no_stock_re.search(stock_text):
                continue
            if it.get('count') is not None or re.search(r"X\s*\d+", stock_text or '', re.I):
                in_stock.append(it)

        if not in_stock:
            # clear per-page per-guild state
            keys = [k for k in _last_threshold_notified.keys() if k.startswith(f"{page.url}::")]
            for k in keys:
                _last_threshold_notified.pop(k, None)
            return False

        # Use the new per-guild notifier which will apply each guild's config and dedupe separately.
        sent = await _notify_guilds_with_items(page.url, in_stock, "server-configured threshold")
        print(f"_scan_and_notify_threshold: notification sent={bool(sent)} to {sent} guild(s)")
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
                    # If the restock path sends notifications it returns True; capture that so
                    # we don't run the threshold scanner in the same iteration and duplicate messages.
                    restock_sent = False
                    try:
                        restock_sent = await _parse_page_for_restock_and_alert(page)
                    except Exception as e:
                        print(f"_periodic_seed_check: restock parse error for {getattr(page,'url','unknown')}: {e}")

                    try:
                        await _check_timer_and_alert(page)
                    except Exception as e:
                        print(f"_periodic_seed_check: timer check error for {getattr(page,'url','unknown')}: {e}")

                    if restock_sent:
                        print(f"_periodic_seed_check: restock handler already sent notification for {getattr(page,'url','unknown')}, skipping threshold scan")
                    else:
                        # New: scan for items meeting rarity threshold and notify automatically
                        try:
                            await _scan_and_notify_threshold(page)
                        except Exception as e:
                            print(f"_periodic_seed_check: threshold scan error for {getattr(page,'url','unknown')}: {e}")
                except Exception as e:
                    print(f"_periodic_seed_check: per-page top-level error for {getattr(page,'url','unknown')}: {e}")
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

@bot.command(name="list_plants")
async def list_plants(ctx, index: Optional[int] = None, endpoint: Optional[str] = None):
    """List all item names currently scraped from the page.

    Usage:
      !list_plants                -> list names from first open tab
      !list_plants 1              -> list names from tab index 1
      !list_plants 0 http://...   -> specify CDP endpoint
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
        # Scrape name + heuristic rarity from the page, then sort using a supplied
        # authoritative ordering and rarity map (falling back to the heuristic when
        # an item is not in the map).
        items = await page.evaluate(r"""
            () => {
                const out = [];
                const cards = Array.from(document.querySelectorAll('button.chakra-button'));
                for (const card of cards) {
                    const nameEl = card.querySelector('p.chakra-text.css-swfl2y') || card.querySelector('p.chakra-text');
                    const itemName = (nameEl && (nameEl.textContent || '').trim()) || '';
                    if (!itemName) continue;
                    const rarityCandidate = Array.from(card.querySelectorAll('p, span, div')).map(n => (n.textContent||'').toLowerCase()).join(' ');
                    let rarity = 'common';
                    if (/celestial/i.test(rarityCandidate)) rarity = 'celestial';
                    else if (/divine/i.test(rarityCandidate)) rarity = 'divine';
                    else if (/mythic|mythical/i.test(rarityCandidate)) rarity = 'mythic';
                    else if (/legendary/i.test(rarityCandidate)) rarity = 'legendary';
                    else if (/epic/i.test(rarityCandidate)) rarity = 'epic';
                    else if (/rare/i.test(rarityCandidate)) rarity = 'rare';
                    else if (/uncommon/i.test(rarityCandidate)) rarity = 'uncommon';
                    out.push({name: itemName, rarity});
                }
                // Deduplicate by name while preserving first-seen order
                const seen = new Set();
                return out.filter(it => {
                    if (seen.has(it.name)) return false;
                    seen.add(it.name);
                    return true;
                });
            }
        """)

        # Filter out spurious names that are just numeric badges (e.g. "1") or empty strings
        items = [it for it in items if (it.get('name') or '').strip() and not re.match(r'^\d+$', (it.get('name') or '').strip())]

        if not items:
            await ctx.send("No item names were found on the page.")
            return

        # Authoritative ordering & rarities provided by the user. Items present in this
        # list will be shown in this exact order and with the mapped rarity label.
        FULL_ORDER = [
            "Carrot Seed",
            "Strawberry Seed",
            "Aloe Seed",
            "Blueberry Seed",
            "Apple Seed",
            "Tulip Seed",
            "Tomato Seed",
            "Daffodil Seed",
            "Corn Kernel",
            "Watermelon Seed",
            "Pumpkin Seed",
            "Echeveria Cutting",
            "Coconut Seed",
            "Banana Seed",
            "Lily Seed",
            "Burro's Tail Cutting",
            "Mushroom Spore",
            "Cactus Seed",
            "Bamboo Seed",
            "Grape Seed",
            "Pepper Seed",
            "Lemon Seed",
            "Passion Fruit Seed",
            "Dragon Fruit Seed",
            "Lychee Pit",
            "Sunflower Seed",
            "Starweaver Pod",
        ]

        RARITY_MAP = {
            "Carrot Seed": "Common",
            "Strawberry Seed": "Common",
            "Aloe Seed": "Common",
            "Blueberry Seed": "Uncommon",
            "Apple Seed": "Uncommon",
            "Tulip Seed": "Uncommon",
            "Tomato Seed": "Uncommon",
            "Daffodil Seed": "Rare",
            "Corn Kernel": "Rare",
            "Watermelon Seed": "Rare",
            "Pumpkin Seed": "Rare",
            "Echeveria Cutting": "Legendary",
            "Coconut Seed": "Legendary",
            "Banana Seed": "Legendary",
            "Lily Seed": "Legendary",
            "Burro's Tail Cutting": "Legendary",
            "Mushroom Spore": "Mythical",
            "Cactus Seed": "Mythical",
            "Bamboo Seed": "Mythical",
            "Grape Seed": "Mythical",
            "Pepper Seed": "Divine",
            "Lemon Seed": "Divine",
            "Passion Fruit Seed": "Divine",
            "Dragon Fruit Seed": "Divine",
            "Lychee Pit": "Divine",
            "Sunflower Seed": "Divine",
            "Starweaver Pod": "Celestial",
        }

        # Build a name->heuristic_rarity map from the scraped items for fallback
        heur = {it.get('name'): it.get('rarity') for it in items}

        def sort_key(it):
            name = it.get('name')
            if name in FULL_ORDER:
                return (0, FULL_ORDER.index(name))
            # unknown items go after known ones, sorted alphabetically
            return (1, name.lower())

        items_sorted = sorted(items, key=sort_key)

        lines = []
        for it in items_sorted:
            name = it.get('name')
            label = RARITY_MAP.get(name)
            if not label:
                # fallback: use scraped heuristic rarity and title-case it
                label = (heur.get(name) or 'common').capitalize()
            lines.append(f"{name} — {label}")

        text = "\n".join(lines)

        # We no longer save/export to plants.json; the list is generated live from the page.

        # Send the list; use a file if too long
        if len(text) <= 1900:
            await ctx.send("```\n" + text + "\n```")
        else:
            await ctx.send(file=discord.File(io.BytesIO(text.encode('utf-8')), filename='plants.txt'))
    except Exception as e:
        await ctx.send(f"Error while scraping names: {e}")

if not DISCORD_TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN in .env")

bot.run(DISCORD_TOKEN)
