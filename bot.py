#!/usr/bin/env python3
"""
Discord-integrated Magic Garden shop monitor + watchlist UI.

Additions:
- /shop_watch: ephemeral multi-page checklist to choose watched items (labels "Name — Rarity")
- /shop_watch_view: view current watched items for this guild
- Per-guild watchlist stored in guild_watchlist.json
- Restock notifications filtered by watchlist (if non-empty)

Env (.env) variables:
  DISCORD_TOKEN (required)
  MAGIC_ROOM_URL, SHOP_SNAPSHOT_PATH (optional path used only if WRITE_SNAPSHOT=1)
  SEED_PERIOD_SEC, EGG_PERIOD_SEC, TOOL_PERIOD_SEC, DECOR_PERIOD_SEC (optional overrides)
  DEBUG=1 (optional; more verbose logging)
  WRITE_SNAPSHOT=1 (optional; default 0 → disable file writes)
  SNAPSHOT_LOG=1   (optional; default 0 → suppress "Wrote ..." logs)
"""

import os, io, json, time, threading, re
from typing import Any, Dict, List, Optional, Set, Tuple
from pathlib import Path
from collections import defaultdict

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import discord
from discord import app_commands

# ---------- Load env ----------
ROOT = Path(__file__).resolve().parent
load_dotenv(dotenv_path=ROOT / '.env')

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
ROOM_URL = os.getenv('MAGIC_ROOM_URL', 'https://magiccircle.gg/r/LDQK')
OUT_PATH = os.getenv('SHOP_SNAPSHOT_PATH', str(ROOT / 'shop_snapshot.json'))
DEBUG = os.getenv('DEBUG', '0') == '1'

# Disable file writes by default; can enable via WRITE_SNAPSHOT=1
WRITE_SNAPSHOT = os.getenv('WRITE_SNAPSHOT', '0') == '1'
SNAPSHOT_LOG   = os.getenv('SNAPSHOT_LOG', '0') == '1'

DEFAULT_PERIODS = {
    "seed":  int(os.getenv("SEED_PERIOD_SEC",  300)),   # 5 min
    "egg":   int(os.getenv("EGG_PERIOD_SEC",   600)),   # 10 min
    "tool":  int(os.getenv("TOOL_PERIOD_SEC",  300)),   # 5 min
    "decor": int(os.getenv("DECOR_PERIOD_SEC", 3000)),  # 50 min
}

PRINT_ONLY_AVAILABLE = True
LOCAL_SOFT_RESET_AT_ZERO = True
MAX_WAIT_SEC = 60
MIN_REFRESH_COOLDOWN_SEC = 8.0

# ---------- State (shared across threads; guard with state_lock) ----------
state_lock = threading.Lock()
written_first_snapshot = threading.Event()
snapshot_updated = threading.Event()

full_state: Dict[str, Any] = {}
last_normalized: Optional[Dict[str, Any]] = None

# Countdown state per kind:
#   secs: current server baseline
#   t0: baseline monotonic()
#   period: stable period for local reset at zero
restock_timers: Dict[str, Dict[str, float]] = {}

# Refresh orchestration (performed on the monitor thread)
refresh_requested = threading.Event()
force_refresh_requested = threading.Event()
last_refresh_at = 0.0  # guarded by state_lock
pending_print_kinds: Set[str] = set()  # guarded by state_lock

# In-memory snapshot buffer
last_snapshot_bytes: bytes = b""

# ---------- Files ----------
GUILD_SETTINGS_PATH = ROOT / 'guild_settings.json'        # existing general config (optional)
ITEM_RARITIES_PATH  = ROOT / 'item_rarities.json'
WATCHLIST_PATH      = ROOT / 'guild_watchlist.json'       # NEW: where selections live

# ---------- Utilities ----------
def _dprint(msg: str):
    print(msg, flush=True)

def _dbg(msg: str):
    if DEBUG:
        _dprint(f"[DEBUG] {msg}")

def _norm_item_key(name: Optional[str]) -> str:
    return (name or "").strip().lower()

# ---------- Load item rarities (for labels) ----------
VOCAB: Dict[str, str] = {
  "carrot seed": "common",
  "strawberry seed": "common",
  "aloe seed": "common",
  "blueberry seed": "uncommon",
  "apple seed": "uncommon",
  "tulip seed": "uncommon",
  "tomato seed": "uncommon",
  "daffodil seed": "uncommon",
  "corn kernel": "rare",
  "watermelon seed": "rare",
  "pumpkin seed": "rare",
  "echeveria cutting": "legendary",
  "coconut seed": "legendary",
  "banana seed": "legendary",
  "lily seed": "legendary",
  "burro's tail cutting": "legendary",
  "mushroom spore": "mythic",
  "cactus seed": "mythic",
  "bamboo seed": "mythic",
  "grape seed": "mythic",
  "pepper seed": "divine",
  "lemon seed": "divine",
  "passion fruit seed": "divine",
  "dragon fruit seed": "divine",
  "lychee pit": "divine",
  "sunflower seed": "divine",
  "starweaver pod": "celestial",
  "common egg": "common",
  "uncommon egg": "uncommon",
  "rare egg": "rare",
  "legendary egg": "legendary",
  "mythical egg": "mythic",
  "watering can": "common",
  "planter pot": "common",
  "shovel": "uncommon",
  "small rock": "common",
  "medium rock": "common",
  "large rock": "common",
  "wood bench": "common",
  "wood arch": "common",
  "wood bridge": "common",
  "wood lamp post": "common",
  "wood owl": "common",
  "stone bench": "uncommon",
  "stone arch": "uncommon",
  "stone bridge": "uncommon",
  "stone lamp post": "uncommon",
  "stone gnome": "uncommon",
  "marble bench": "rare",
  "marble arch": "rare",
  "marble bridge": "rare",
  "marble lamp post": "rare"
}
ITEM_RARITIES: Dict[str, str] = {k.lower(): v.lower() for k, v in VOCAB.items()}

def _sk(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())

def _base(x: str) -> str:
    x = (x or "").strip().lower()
    for suf in (" seed", " seeds", " kernel", " cutting", " cuttings", " pod", " pods", " spore", " pit"):
        if x.endswith(suf):
            x = x[: -len(suf)]
            break
    return _sk(x)

CANON_MAP: Dict[str, str] = {}
for _k in ITEM_RARITIES.keys():
    CANON_MAP[_sk(_k)] = _k
    CANON_MAP[_base(_k)] = _k

def _canonical_vocab_key(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    for cand in {_sk(name), _base(name)}:
        if cand in CANON_MAP:
            return CANON_MAP[cand]
    return None

def _pretty_label_for_name(name: Optional[str]) -> str:
    ck = _canonical_vocab_key(name)
    if ck:
        r = ITEM_RARITIES.get(ck, "")
        return f"{ck.title()} — {r.capitalize()}" if r else ck.title()
    return name or "unknown"

RARITY_ORDER = {
    'celestial': 0,
    'divine': 1,
    'mythic': 2,
    'legendary': 3,
    'rare': 4,
    'uncommon': 5,
    'common': 6,
}
def _rarity_for_name(name: Optional[str]) -> str:
    if not name:
        return ''
    try:
        return (ITEM_RARITIES.get(_norm_item_key(name)) or '').capitalize()
    except Exception:
        return ''

# Build options list from item_rarities: (label, value, rarity)
def _kind_for_name(name: str) -> str:
    n = (name or "").lower()
    if "egg" in n:
        return "egg"
    tools = {"watering can", "planter pot", "shovel"}
    if any(t in n for t in tools):
        return "tool"
    decor_kws = ("bench", "arch", "bridge", "lamp", "gnome", "rock", "owl")
    if any(k in n for k in decor_kws):
        return "decor"
    return "seed"

def _build_item_options() -> List[Tuple[str,str,str]]:
    opts = []
    for raw_name, rarity in ITEM_RARITIES.items():
        name = raw_name.strip()
        r = str(rarity).strip().lower()
        label = f"{name.title()} — {r.capitalize()}" if r else name.title()
        opts.append((label, _norm_item_key(name), r))
    # Keep the exact order from item_rarities.json (do not sort)
    return opts

ITEM_OPTIONS = _build_item_options()

# Paginate items in the exact order they appear (no grouping by kind) so dropdown pages preserve JSON order
def _build_pages_by_kind(page_size: int = 25):
    pages = []
    total = len(ITEM_OPTIONS)
    if total == 0:
        return pages
    kind_total = (total + page_size - 1) // page_size
    for i in range(0, total, page_size):
        page_items = ITEM_OPTIONS[i:i+page_size]
        pages.append({'kind': 'all', 'items': page_items, 'kind_page': i//page_size + 1, 'kind_total': kind_total})
    return pages

ITEM_PAGES = _build_pages_by_kind()

# ---------- Watchlist storage ----------
def load_watchlist() -> Dict[str, List[str]]:
    try:
        return json.loads(WATCHLIST_PATH.read_text(encoding='utf-8') or '{}')
    except FileNotFoundError:
        return {}
    except Exception as e:
        _dprint(f"[WARN] failed to load {WATCHLIST_PATH.name}: {e}")
        return {}

def save_watchlist(data: Dict[str, List[str]]):
    try:
        WATCHLIST_PATH.write_text(json.dumps(data, indent=2), encoding='utf-8')
    except Exception as e:
        _dprint(f"[WARN] failed to save {WATCHLIST_PATH.name}: {e}")

def get_guild_watch(guild_id: int) -> Set[str]:
    wl = load_watchlist()
    items = wl.get(str(guild_id)) or []
    return set(_norm_item_key(x) for x in items)

def set_guild_watch(guild_id: int, items: List[str]):
    wl = load_watchlist()
    wl[str(guild_id)] = sorted(set(_norm_item_key(x) for x in items))
    save_watchlist(wl)

# If guild has a non-empty watchlist, filter notifications to ONLY those items.
def filter_inventory_by_watch(inv: List[Dict[str, Any]], guild_id: int) -> List[Dict[str, Any]]:
    watched = get_guild_watch(guild_id)
    if not watched:
        return []
    watched_keys: Set[str] = set()
    for w in watched:
        watched_keys.add(_sk(w))
        watched_keys.add(_base(w))
    out = []
    for it in inv:
        name = it.get('name') or ''
        if {_sk(name), _base(name)} & watched_keys:
            out.append(it)
    return out

# ---------- JSON patch helpers ----------
def _ptr_decode(seg: str) -> str:
    return seg.replace("~1", "/").replace("~0", "~")

def _get_parent_and_key(root: Any, pointer: str):
    if pointer == "" or pointer == "/":
        return None, None
    parts = [p for p in pointer.split("/") if p != ""]
    cur = root
    for raw in parts[:-1]:
        seg = _ptr_decode(raw)
        if isinstance(cur, list):
            cur = cur[int(seg)]
        else:
            cur = cur[seg]
    last = _ptr_decode(parts[-1])
    return cur, last

def _op_replace(root: Any, pointer: str, value: Any):
    parent, key = _get_parent_and_key(root, pointer)
    if parent is None:
        return value
    if isinstance(parent, list):
        parent[int(key)] = value
    else:
        parent[key] = value
    return root

def _op_add(root: Any, pointer: str, value: Any):
    parent, key = _get_parent_and_key(root, pointer)
    if parent is None:
        return value
    if isinstance(parent, list):
        k = int(key)
        if k == len(parent):
            parent.append(value)
        else:
            parent.insert(k, value)
    else:
        parent[key] = value
    return root

def _op_remove(root: Any, pointer: str):
    parent, key = _get_parent_and_key(root, pointer)
    if parent is None:
        return {}
    if isinstance(parent, list):
        parent.pop(int(key))
    else:
        parent.pop(key, None)
    return root

def apply_patches(root: Any, patches: List[Dict[str, Any]]) -> Any:
    for p in patches:
        op = p.get("op")
        path = p.get("path", "")
        if op == "replace":
            root = _op_replace(root, path, p.get("value"))
        elif op == "add":
            root = _op_add(root, path, p.get("value"))
        elif op == "remove":
            root = _op_remove(root, path)
    return root

# ---------- Normalization ----------
def _current_stock(item: Dict[str, Any]) -> int:
    for k in ("remainingStock", "currentStock", "stock", "available", "qty", "quantity"):
        if k in item and isinstance(item[k], (int, float)):
            return int(item[k])
    if "initialStock" in item and "sold" in item:
        try:
            return max(int(item["initialStock"]) - int(item["sold"]), 0)
        except Exception:
            pass
    return int(item.get("initialStock", 0) or 0)

def _display_name(item: Dict[str, Any]) -> str:
    return (item.get("displayName")
            or item.get("name")
            or item.get("species")
            or item.get("toolId")
            or item.get("eggId")
            or item.get("decorId")
            or item.get("id")
            or "unknown")

def normalize_shops(state: Dict[str, Any]) -> Dict[str, Any]:
    try:
        child = state["child"]["data"]
        shops = child["shops"]
    except Exception:
        return {}

    def norm(kind: str, item: Dict[str, Any]) -> Dict[str, Any]:
        base = {
            "name": _display_name(item),
            "itemType": item.get("itemType", kind),
            "initialStock": int(item.get("initialStock", 0) or 0),
            "currentStock": _current_stock(item),
        }
        if kind == "seed":
            return {"id": item.get("species"), **base}
        if kind == "tool":
            return {"id": item.get("toolId"), **base}
        if kind == "egg":
            return {"id": item.get("eggId"), **base}
        if kind == "decor":
            return {"id": item.get("decorId"), **base}
        return {"id": item.get("id"), **base}

    out = {"captured_at": int(time.time()), "currentTime": child.get("currentTime"), "shops": {}}
    for kind in ("seed", "egg", "tool", "decor"):
        s = shops.get(kind)
        if not s:
            continue
        inv = s.get("inventory") or []
        out["shops"][kind] = {
            "secondsUntilRestock": s.get("secondsUntilRestock"),
            "inventory": [norm(kind, it) for it in inv],
        }
    return out

# ---------- Formatting / printing ----------
def _fmt_secs(secs: float) -> str:
    secs = max(0, int(round(secs)))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def _print_available(kind: str, kind_shop: Dict[str, Any]):
    inv = (kind_shop or {}).get("inventory", [])
    if PRINT_ONLY_AVAILABLE:
        inv = [i for i in inv if int(i.get("currentStock", 0)) > 0]
    if not inv:
        _dprint(f"[RESTOCK] {kind}: (no items with stock > 0)")
        return
    items = ", ".join(f"{i['name']} (stock {i['currentStock']})" for i in inv)
    _dprint(f"[RESTOCK] {kind}: {items}")

def _format_seconds_verbose(s):
    try:
        s = float(s)
    except Exception:
        return 'N/A'
    if s < 0:
        return 'now'
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s or not parts: parts.append(f"{s}s")
    return " ".join(parts)

def _format_seconds(s):
    return _format_seconds_verbose(s)

# ---------- Load optional guild settings ----------
def load_guild_settings() -> Dict[str, Any]:
    try:
        text = GUILD_SETTINGS_PATH.read_text(encoding='utf-8')
        return json.loads(text or '{}')
    except FileNotFoundError:
        return {}
    except Exception as e:
        _dprint(f"[WARN] failed to load guild_settings.json: {e}")
        return {}

def save_guild_settings(data: Dict[str, Any]):
    try:
        GUILD_SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding='utf-8')
    except Exception as e:
        _dprint(f"[WARN] failed to save guild_settings.json: {e}")
    except Exception as e:
        _dprint(f"[WARN] failed to load guild_settings.json: {e}")
        return {}

# ---------- Discord notifier ----------
class DiscordNotifier:
    def __init__(self, client: discord.Client):
        self.client = client

    def _find_channel_for_guild(self, guild: discord.Guild, cfg: Dict[str, Any]):
        # prefer explicit configured channel_id
        cid = cfg.get('channel_id') if cfg else None
        if cid:
            try:
                ch = self.client.get_channel(int(cid))
                if ch and isinstance(ch, discord.TextChannel):
                    if ch.permissions_for(guild.me).send_messages:
                        return ch
            except Exception:
                pass
        # try system channel
        try:
            if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
                return guild.system_channel
        except Exception:
            pass
        # fallback: first text channel we can send to
        for ch in getattr(guild, 'text_channels', []):
            try:
                if ch.permissions_for(guild.me).send_messages:
                    return ch
            except Exception:
                continue
        return None

    def notify_snapshot(self, snapshot: Dict[str, Any]):
        # Called from monitor thread; schedule coroutine on bot loop
        loop = self.client.loop
        async def _do():
            guilds_cfg = load_guild_settings()
            captured = snapshot.get('captured_at')
            shops = snapshot.get('shops', {})
            for guild in list(self.client.guilds):
                cfg = guilds_cfg.get(str(guild.id), {})
                if cfg.get('snapshot_enabled', True) is False:
                    continue
                kinds_enabled = cfg.get('kinds_enabled') if cfg else None
                for kind, kind_shop in shops.items():
                    try:
                        if kinds_enabled is not None and not kinds_enabled.get(kind, True):
                            continue

                        inv_all = kind_shop.get('inventory', []) or []
                        inv_all = [i for i in inv_all if int(i.get('currentStock', 0)) > 0] if PRINT_ONLY_AVAILABLE else inv_all
                        # Filter by guild watchlist if present
                        inv = filter_inventory_by_watch(inv_all, guild.id)
                        if not inv:
                            continue

                        restock_str = _format_seconds(kind_shop.get('secondsUntilRestock'))
                        title = f"Shop {kind} update"
                        lines = [f"Captured: {time.ctime(captured)}", f"Restock in: {restock_str}", f"Items: {len(inv)} available (filtered)"]
                        for it in inv[:20]:
                            raw = it.get('name')
                            ck = _canonical_vocab_key(raw)
                            disp = ck.title() if ck else (raw or 'unknown')
                            cur = int(it.get('currentStock', 0))
                            init = int(it.get('initialStock', 0))
                            rr = ITEM_RARITIES.get(ck or '', '')
                            rc = rr.capitalize() if rr else ''
                            lines.append(f"{disp} — {cur}/{init}" + (f" — {rc}" if rc else ""))
                        if len(inv) > 20:
                            lines.append(f"...and {len(inv)-20} more items")
                        msg = "\n".join(lines)
                        if len(msg) > 1900:
                            msg = msg[:1900] + '\n...truncated'
                        ch = self._find_channel_for_guild(guild, cfg)
                        if ch:
                            try:
                                await ch.send(f"**{title}**\n{msg}")
                            except Exception:
                                try:
                                    owner = guild.owner
                                    if owner:
                                        await owner.send(f"**{title}**\n{msg}")
                                except Exception:
                                    pass
                    except Exception:
                        continue
        loop.create_task(_do())

    def notify_restock(self, kind: str, kind_shop: Dict[str, Any]):
        loop = self.client.loop
        async def _do():
            guilds_cfg = load_guild_settings()
            for guild in list(self.client.guilds):
                cfg = guilds_cfg.get(str(guild.id), {})
                ch = self._find_channel_for_guild(guild, cfg)
                if not ch:
                    continue
                inv_all = (kind_shop or {}).get('inventory', []) or []
                inv_all = [i for i in inv_all if int(i.get('currentStock', 0)) > 0] if PRINT_ONLY_AVAILABLE else inv_all
                inv = filter_inventory_by_watch(inv_all, guild.id)
                if not inv:
                    continue
                header = f"@everyone\n**MAGIC GARDEN ALERT, {kind.capitalize()} restocked:**\n\n"
                item_lines = []
                for it in inv[:50]:
                    raw = it.get('name') or _display_name(it)
                    ck = _canonical_vocab_key(raw)
                    disp = ck.title() if ck else (raw or 'unknown')
                    cur = int(it.get('currentStock', 0))
                    rr = ITEM_RARITIES.get(ck or '', '')
                    rc = rr.capitalize() if rr else ''
                    item_lines.append(f"{disp} — X {cur}" + (f" — {rc}" if rc else ""))
                msg = header + "\n".join(item_lines)
                if len(msg) > 1900:
                    msg = msg[:1900] + '\n...truncated'
                try:
                    await ch.send(msg)
                except Exception:
                    try:
                        owner = guild.owner
                        if owner:
                            await owner.send(msg)
                    except Exception:
                        pass
        loop.create_task(_do())

# ---------- Monitor (Playwright) ----------
def monitor_loop(notifier: DiscordNotifier):
    """Runs on its own thread. All Playwright actions happen here."""
    global full_state, last_normalized, restock_timers, last_refresh_at, last_snapshot_bytes

    def write_snapshot_if_changed(snapshot: Dict[str, Any], reason: str = ""):
        """Keep snapshot in memory; optionally write to disk and/or log."""
        global last_snapshot_bytes
        try:
            # Keep the snapshot in-memory so the administrative command can still return it,
            # but do not perform any disk writes or noisy logging/notifications here.
            data = json.dumps(snapshot, indent=2).encode("utf-8")
            last_snapshot_bytes = data
            # Signal that we have at least one snapshot available (so monitor can proceed)
            written_first_snapshot.set()
            # Signal that a fresh snapshot was captured (used by countdown to await refresh)
            snapshot_updated.set()
        except Exception as e:
            _dprint(f"[ERROR] Failed to capture snapshot: {e}")

    # ---- countdown thread (no Playwright here) ----
    def countdown_loop():
        kinds = ("seed", "egg", "tool", "decor")
        while True:
            now_mono = time.monotonic()
            parts = []
            with state_lock:
                kinds_to_report = []
                for k in kinds:
                    rt = restock_timers.get(k)
                    if rt and "secs" in rt and "t0" in rt:
                        remain = float(rt["secs"]) - (now_mono - float(rt["t0"]))
                        if remain <= 0:
                            kinds_to_report.append(k)
                            if LOCAL_SOFT_RESET_AT_ZERO:
                                period = float(rt.get("period") or DEFAULT_PERIODS.get(k, 60.0))
                                # reset using current monotonic baseline
                                restock_timers[k] = {"secs": period, "t0": now_mono, "period": period}
                                remain = period
                        parts.append(f"{k} {_fmt_secs(remain)}")
                    else:
                        parts.append(f"{k} —")
            _dprint("[COUNTDOWN] " + " | ".join(parts))

            # Local report without forced reload
            if kinds_to_report:
                try:
                    # Mark these kinds so the monitor thread can print/notify them if a fresh snapshot arrives.
                    with state_lock:
                        for k in kinds_to_report:
                            pending_print_kinds.add(k)
                    # clear previous snapshot marker and ask monitor thread to reload (force bypasses cooldown)
                    snapshot_updated.clear()
                    force_refresh_requested.set()
                    waited = snapshot_updated.wait(timeout=8.0)
                except Exception:
                    waited = False

                with state_lock:
                    cur = last_normalized
                    # For each kind, if the monitor already handled it (pending_print_kinds cleared), skip local notify.
                    for k in kinds_to_report:
                        try:
                            already_handled = (k not in pending_print_kinds)
                            if already_handled:
                                continue
                            # If we didn't get a fresh snapshot, skip sending (require refresh before notify)
                            if not waited:
                                _dprint(f"[LOCAL] Skipping {k} because no fresh snapshot was received after refresh.")
                                # leave the pending flag in place so the monitor may still handle it later
                                continue
                            if cur and cur.get('shops') and cur['shops'].get(k):
                                _dprint(f"[LOCAL] Reporting {k} after fresh snapshot.")
                                _print_available(k, cur['shops'].get(k, {}))
                                notifier.notify_restock(k, cur['shops'].get(k, {}))
                                # mark as handled locally
                                pending_print_kinds.discard(k)
                        except Exception:
                            pass

            time.sleep(1.0)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(service_workers="allow")
        page = ctx.new_page()
        _dprint(f"[INIT] Opening room {ROOM_URL}")

        client = ctx.new_cdp_session(page)
        client.send("Network.enable")

        def on_ws_frame(params):
            """CDP handler: updates state, baselines timers, and prints/dispatches on Welcome."""
            global full_state, last_normalized
            try:
                payload = params.get('response', {}).get('payloadData', '')
                if not payload or not (payload.startswith('{') or payload.startswith('[')):
                    return
                obj = json.loads(payload)
            except Exception:
                return

            t = obj.get('type')

            if t == 'Welcome':
                fs = obj.get('fullState') or {}
                with state_lock:
                    full_state = fs
                    cur = normalize_shops(full_state)
                    if not cur:
                        return
                    last_normalized = cur

                    now = time.monotonic()
                    for kind, s in cur.get('shops', {}).items():
                        secs = s.get('secondsUntilRestock')
                        if isinstance(secs, (int, float)):
                            period = restock_timers.get(kind, {}).get('period', DEFAULT_PERIODS.get(kind, float(secs)))
                            restock_timers[kind] = {'secs': float(secs), 't0': now, 'period': float(period)}

                    # Capture snapshot in-memory but do not notify or write files.
                    write_snapshot_if_changed(cur, reason="welcome")
                    if pending_print_kinds:
                        for kind in sorted(pending_print_kinds):
                            _dprint(f"[WELCOME] Fresh snapshot received; printing {kind}.")
                            _print_available(kind, cur['shops'].get(kind, {}))
                            notifier.notify_restock(kind, cur['shops'].get(kind, {}))
                        pending_print_kinds.clear()
                return

            if t == 'PartialState':
                patches = obj.get('patches') or []
                if not patches:
                    return
                with state_lock:
                    full_state = apply_patches(full_state, patches)
                    cur = normalize_shops(full_state)
                    if not cur:
                        return

                    now = time.monotonic()
                    for kind, s in cur.get('shops', {}).items():
                        cur_secs = s.get('secondsUntilRestock')
                        if isinstance(cur_secs, (int, float)):
                            rt = restock_timers.get(kind)
                            if not rt:
                                restock_timers[kind] = {
                                    'secs': float(cur_secs),
                                    't0': now,
                                    'period': DEFAULT_PERIODS.get(kind, float(cur_secs)),
                                }
                            else:
                                prev_secs = float(rt['secs'])
                                restock_timers[kind]['secs'] = float(cur_secs)
                                restock_timers[kind]['t0'] = now
                                if float(cur_secs) > prev_secs + 3:
                                    restock_timers[kind]['period'] = float(cur_secs)
                                    _dbg(f"{kind} period updated via server reset → {cur_secs}s")

                    last_normalized = cur
                    # Capture snapshot in-memory but do not notify or write files.
                    write_snapshot_if_changed(cur, reason="partial")

        client.on('Network.webSocketFrameReceived', on_ws_frame)

        # Navigation + open SHOP
        page.goto(ROOM_URL)
        try:
            page.get_by_role('button', name=re.compile(r"\bSHOP\b", re.I)).click(timeout=6000)
        except Exception:
            pass

        # Start countdown printer
        threading.Thread(target=countdown_loop, daemon=True).start()

        # Wait for first snapshot (best effort)
        deadline = time.time() + MAX_WAIT_SEC
        while time.time() < deadline and not written_first_snapshot.is_set():
            time.sleep(0.1)

        _dprint("[MONITOR] Running. Press Ctrl+C to stop this process.")

        # MONITOR MAIN LOOP (if you want refreshes, wire them here; currently local report only)
        try:
            while True:
                # Handle refresh requests from the countdown thread (reload page in Playwright thread)
                # Support both normal and forced refresh requests. Forced requests bypass cooldown.
                if refresh_requested.is_set() or force_refresh_requested.is_set():
                    forced = force_refresh_requested.is_set()
                    now = time.time()
                    try:
                        with state_lock:
                            can = (now - float(last_refresh_at)) >= MIN_REFRESH_COOLDOWN_SEC or forced
                        if can:
                            _dprint("[MONITOR] Refresh requested; reloading page." + (" (forced)" if forced else ""))
                            try:
                                page.reload()
                            except Exception as e:
                                _dprint(f"[MONITOR] Page reload failed: {e}")
                            with state_lock:
                                last_refresh_at = now
                        else:
                            _dprint("[MONITOR] Refresh requested but cooldown active.")
                    finally:
                        refresh_requested.clear()
                        force_refresh_requested.clear()
                 
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            browser.close()

# ---------- Discord bot setup ----------
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
notifier = DiscordNotifier(client)
monitor_thread: Optional[threading.Thread] = None
tree = app_commands.CommandTree(client)

# ---------- Watchlist UI (slash commands + components) ----------
PAGE_SIZE = 25  # Discord max options per select

def chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i+size]

# New UI: two dropdowns — one for kind, one for items (paginated)
class KindSelect(discord.ui.Select):
    def __init__(self, parent_view: "WatchlistView"):
        self.parent_view = parent_view
        kinds = ['all', 'seed', 'egg', 'tool', 'decor', 'other']
        options = [
            discord.SelectOption(label=k.title() if k != 'all' else 'All kinds', value=k, default=(k == parent_view.kind))
            for k in kinds
        ]
        super().__init__(placeholder='Choose a kind…', min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.user_id:
            await interaction.response.send_message("Only the command invoker can use this control.", ephemeral=True)
            return
        # set kind and reset page
        self.parent_view.kind = self.values[0]
        self.parent_view.page = 0
        self.parent_view.update_components()
        await interaction.response.edit_message(content=self.parent_view.render_header(), view=self.parent_view)

class ItemSelect(discord.ui.Select):
    def __init__(self, parent_view: "WatchlistView"):
        self.parent_view = parent_view
        page_items = parent_view._current_page_items()
        options = [
            discord.SelectOption(label=label, value=value, default=(value in parent_view.selection))
            for (label, value, _r) in page_items
        ]
        placeholder = f"Select items — page {parent_view.page+1}/{max(parent_view.page_count(),1)}"
        super().__init__(placeholder=placeholder, min_values=0, max_values=len(options) if options else 1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.user_id:
            await interaction.response.send_message("Only the command invoker can use this control.", ephemeral=True)
            return
        page_values = {opt.value for opt in self.options}
        chosen = set(self.values)
        # Replace selections for this page with the user's current choices
        self.parent_view.selection -= page_values
        self.parent_view.selection |= chosen
        # Persist selection immediately so it survives restarts
        try:
            set_guild_watch(self.parent_view.guild_id, list(self.parent_view.selection))
        except Exception as e:
            _dprint(f"[WARN] failed to persist watchlist for guild {self.parent_view.guild_id}: {e}")
        self.parent_view.update_components()
        await interaction.response.edit_message(content=self.parent_view.render_header(), view=self.parent_view)

class WatchlistView(discord.ui.View):
    def __init__(self, user_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.guild_id = guild_id
        self.page = 0
        self.kind = 'all'
        # working selection starts from saved watchlist
        self.selection: Set[str] = get_guild_watch(guild_id)
        self.update_components()

    def _items_for_kind(self):
        if self.kind == 'all':
            return ITEM_OPTIONS
        return [(label, val, r) for (label, val, r) in ITEM_OPTIONS if _kind_for_name(val) == self.kind]

    def page_count(self):
        total = len(self._items_for_kind()) if self.kind != 'all' else sum(len(p['items']) for p in ITEM_PAGES)
        if total == 0:
            return 0
        return (total + PAGE_SIZE - 1) // PAGE_SIZE

    def _current_page_items(self):
        items = self._items_for_kind()
        start = self.page * PAGE_SIZE
        return items[start:start + PAGE_SIZE]

    def render_header(self) -> str:
        total = len(self._items_for_kind()) if self.kind != 'all' else sum(len(p['items']) for p in ITEM_PAGES)
        selected = len(self.selection)
        kind_label = self.kind.title() if self.kind != 'all' else 'All'
        pc = self.page_count() or 1
        return f"Configure watchlist for this server (kind: {kind_label} — page {self.page+1}/{pc}):\nSelected **{selected}** of **{total}** items."

    def update_components(self):
        self.clear_items()
        # Kind selector
        self.add_item(KindSelect(self))
        # Item selector for current page (if any items exist)
        if self.page_count() > 0:
            self.add_item(ItemSelect(self))

        # Nav buttons
        prev = discord.ui.Button(label="← Prev", style=discord.ButtonStyle.secondary, disabled=(self.page <= 0))
        nextb = discord.ui.Button(label="Next →", style=discord.ButtonStyle.secondary, disabled=(self.page >= max(self.page_count()-1, 0)))
        save = discord.ui.Button(label="Save", style=discord.ButtonStyle.success)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger)

        async def on_prev(i: discord.Interaction):
            if i.user.id != self.user_id:
                await i.response.send_message("Only the command invoker can use this control.", ephemeral=True)
                return
            self.page = max(0, self.page-1)
            self.update_components()
            await i.response.edit_message(content=self.render_header(), view=self)

        async def on_next(i: discord.Interaction):
            if i.user.id != self.user_id:
                await i.response.send_message("Only the command invoker can use this control.", ephemeral=True)
                return
            self.page = min(max(self.page_count()-1, 0), self.page+1)
            self.update_components()
            await i.response.edit_message(content=self.render_header(), view=self)

        async def on_save(i: discord.Interaction):
            if i.user.id != self.user_id:
                await i.response.send_message("Only the command invoker can save.", ephemeral=True)
                return
            set_guild_watch(self.guild_id, list(self.selection))
            names = [lbl for (lbl,val,_r) in ITEM_OPTIONS if val in self.selection][:20]
            extra = "" if len(self.selection) <= 20 else f"\n...and {len(self.selection)-20} more"
            await i.response.edit_message(content=f"✅ Saved **{len(self.selection)}** watched items for this server.\n" + ("\n".join(names) + extra if names else "No items selected."), view=None)

        async def on_cancel(i: discord.Interaction):
            if i.user.id != self.user_id:
                await i.response.send_message("Only the command invoker can cancel.", ephemeral=True)
                return
            self.stop()
            await i.response.edit_message(content="❌ Canceled. No changes saved.", view=None)

        prev.callback = on_prev
        nextb.callback = on_next
        save.callback = on_save
        cancel.callback = on_cancel

        self.add_item(prev)
        self.add_item(nextb)
        self.add_item(save)
        self.add_item(cancel)

@tree.command(name="shop_watch", description="Choose which items to be notified about (checklist).")
async def shop_watch(interaction: discord.Interaction):
    if not ITEM_OPTIONS:
        await interaction.response.send_message("No items found in item_rarities.json.", ephemeral=True)
        return
    view = WatchlistView(interaction.user.id, interaction.guild.id)
    await interaction.response.send_message(view.render_header(), view=view, ephemeral=True)

@tree.command(name="shop_watch_view", description="Show the current watchlist for this server.")
async def shop_watch_view(interaction: discord.Interaction):
    sel = list(get_guild_watch(interaction.guild.id))
    if not sel:
        await interaction.response.send_message("This server has **no watchlist** set. Use `/shop_watch` to configure.", ephemeral=True)
        return
    # Map back to pretty labels with rarity
    labels = [lbl for (lbl,val,_r) in ITEM_OPTIONS if val in sel]
    labels.sort()
    preview = "\n".join(labels[:30])
    extra = f"\n...and {len(labels)-30} more" if len(labels) > 30 else ""
    await interaction.response.send_message(f"Watched items (**{len(labels)}**):\n{preview}{extra}", ephemeral=True)

# ---------- Events ----------
@client.event
async def on_ready():
    global monitor_thread
    _dprint(f"[DISCORD] Logged in as {client.user} (id={client.user.id})")
    # Load and report saved watchlist counts for visibility
    try:
        wl = load_watchlist()
        total_guilds = len(wl)
        total_items = sum(len(v) for v in wl.values())
        _dprint(f"[WATCHLIST] Loaded watchlist for {total_guilds} guild(s), {total_items} total selections.")
    except Exception:
        pass
    # Start monitor thread once
    if monitor_thread is None or not monitor_thread.is_alive():
        monitor_thread = threading.Thread(target=monitor_loop, args=(notifier,), daemon=True)
        monitor_thread.start()

    # Sync slash commands to each guild for fast availability
    try:
        for g in client.guilds:
            await tree.sync(guild=g)
        # Also do a global sync (optional, slower to propagate)
        await tree.sync()
        _dprint(f"[DISCORD] Slash commands synced to {len(client.guilds)} guild(s) + global.")
    except Exception as e:
        _dprint(f"[WARN] command sync failed: {e}")


# Optional message commands you already had
@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    cmd = message.content.strip().lower()

    if cmd == '!shop_snapshot':
        try:
            await message.channel.send('Snapshot feature is disabled in this build.')
        except Exception as e:
            await message.channel.send(f'Failed to send snapshot: {e}')

    elif cmd == '!shop_debug':
        with state_lock:
            if not restock_timers:
                await message.channel.send("No timers yet.")
                return
            lines = []
            now = time.monotonic()
            for kind, rt in restock_timers.items():
                remain = float(rt['secs']) - (now - float(rt['t0']))
                period = rt.get('period')
                lines.append(f"{kind}: remain={_fmt_secs(remain)} period={int(period) if period else 'N/A'}s")
            await message.channel.send("```\n" + "\n".join(lines) + "\n```")

# ---------- Run ----------
if __name__ == '__main__':
    if not DISCORD_TOKEN:
        print('Missing DISCORD_TOKEN in environment/.env')
        raise SystemExit(1)
    client.run(DISCORD_TOKEN)
