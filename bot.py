#!/usr/bin/env python3
"""
Discord-integrated Magic Garden shop monitor + watchlist UI.

Now reads items, kinds, and rarities directly from item_rarities.json (the GUI output).
"""

import os, io, json, time, threading, re
from typing import Any, Dict, List, Optional, Set, Tuple
from pathlib import Path
from collections import OrderedDict, defaultdict

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import discord
from discord import app_commands

# ---------- Load env ----------
ROOT = Path(__file__).resolve().parent
load_dotenv(dotenv_path=ROOT / '.env')

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
ROOM_URL = os.getenv('MAGIC_ROOM_URL', 'https://magiccircle.gg/r/LDQK')
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

# Auto-reload interval for item files
FILE_WATCH_INTERVAL_SEC = float(os.getenv('FILE_WATCH_INTERVAL_SEC', '2.0'))

# ---------- State (shared across threads; guard with state_lock) ----------
state_lock = threading.Lock()

full_state: Dict[str, Any] = {}
last_normalized: Optional[Dict[str, Any]] = None

# Countdown state per kind
restock_timers: Dict[str, Dict[str, float]] = {}

refresh_requested = threading.Event()
force_refresh_requested = threading.Event()
last_refresh_at = 0.0
pending_print_kinds: Set[str] = set()

# ---------- Files ----------
GUILD_SETTINGS_PATH = ROOT / 'guild_settings.json'
ITEM_RARITIES_PATH  = ROOT / 'item_rarities.json'   # <<— source of truth now
WATCHLIST_PATH      = ROOT / 'guild_watchlist.json'
OUT_PATH            = ROOT / 'shop_snapshot.json'
ITEM_ALIASES_PATH   = ROOT / 'item_aliases.json'

# ---------- Utilities ----------
def _dprint(msg: str): print(msg, flush=True)
def _dbg(msg: str): 
    if DEBUG: _dprint(f"[DEBUG] {msg}")

def _norm_item_key(name: Optional[str]) -> str:
    return (name or "").strip().lower()

def _sk(x: str) -> str:
    """squish key: lowercase, alnum only"""
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())

def _split_camel(name: str) -> str:
    # "OrangeTulip" -> "Orange Tulip", "BurrosTail" -> "Burros Tail", "WoodLampPost" -> "Wood Lamp Post"
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name or "")
    s = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", s)
    s = re.sub(r"(?<=[0-9])(?=[A-Za-z])", " ", s)
    return s.strip()

def _humanize(raw: str) -> str:
    # also handle snake/kebab/etc.
    if not raw: return "unknown"
    s = raw.replace("_", " ").replace("-", " ")
    if re.search(r"[A-Z]", s) and not " " in raw:
        s = _split_camel(raw)
    return " ".join(w.capitalize() for w in s.split())

# Suffixes the shop may use for seeds/eggs/etc
SEED_SUFFIXES = [" seed", " seeds", " kernel", " cutting", " cuttings", " spore", " pit", " pod", " pods"]
EGG_SUFFIXES  = [" egg", " eggs"]

# ---------- Load item definitions from item_rarities.json ----------
# These are populated at import time by _load_items()
ITEM_RARITIES: Dict[str, str] = {}    # canonical_key -> rarity (lower)
KIND_MAP: Dict[str, str] = {}         # canonical_key -> kind (seed/egg/tool/decor/other)
PRETTY_MAP: Dict[str, str] = {}       # canonical_key -> Pretty Name ("Orange Tulip")
VALUE_TO_CANON: Dict[str, str] = {}   # watchlist value (lower pretty) -> canonical_key
KIND_BY_VALUE: Dict[str, str] = {}    # watchlist value -> kind
CANON_MAP: Dict[str, str] = {}        # alias squished -> canonical_key (for matching incoming shop names)
ITEM_OPTIONS: List[Tuple[str, str, str]] = []  # (label, value, rarity) for watchlist UI
RARITIES_SET = {"common","uncommon","rare","legendary","mythic","divine","celestial"}
ALIAS_MAP: Dict[str, str] = {}        # canonical_key -> custom display override

# Track file mtimes to support auto-reload
_rarities_mtime: Optional[float] = None
_aliases_mtime: Optional[float] = None

def _add_alias(canon: str, alias_text: str):
    CANON_MAP[_sk(alias_text)] = canon

def _rebuild_item_options():
    """Rebuild ITEM_OPTIONS using current PRETTY_MAP, ALIAS_MAP, and ITEM_RARITIES.
    Keeps option values stable (lower pretty name), only label changes with aliases.
    """
    global ITEM_OPTIONS
    ITEM_OPTIONS = []
    try:
        for canon, pretty in PRETTY_MAP.items():
            rarity = ITEM_RARITIES.get(canon, "")
            display = ALIAS_MAP.get(canon) or pretty
            label = f"{display}" + (f" — {rarity.capitalize()}" if rarity else "")
            value = pretty.lower()
            ITEM_OPTIONS.append((label, value, rarity))
    except Exception:
        pass


def _load_items():
    global ITEM_RARITIES, KIND_MAP, PRETTY_MAP, VALUE_TO_CANON, KIND_BY_VALUE, CANON_MAP, ITEM_OPTIONS
    ITEM_RARITIES = {}
    KIND_MAP = {}
    PRETTY_MAP = {}
    VALUE_TO_CANON = {}
    KIND_BY_VALUE = {}
    CANON_MAP = {}
    ITEM_OPTIONS = []

    try:
        raw = json.load(open(ITEM_RARITIES_PATH, "r", encoding="utf-8"), object_pairs_hook=OrderedDict)
    except FileNotFoundError:
        _dprint(f"[WARN] {ITEM_RARITIES_PATH.name} not found. No items loaded.")
        return
    except Exception as e:
        _dprint(f"[WARN] failed to parse {ITEM_RARITIES_PATH.name}: {e}")
        return

    # Preserve top-level order (kinds) and per-kind item order from file
    for kind_raw, items in raw.items():
        kind = (kind_raw or "other").strip().lower()
        if not isinstance(items, dict): 
            continue
        for item_name_raw, rarity_raw in items.items():
            pretty = _humanize(str(item_name_raw))
            canon = _sk(pretty)
            rarity = (str(rarity_raw).lower() if rarity_raw else "")
            if rarity and rarity not in RARITIES_SET:
                _dbg(f"Unknown rarity '{rarity_raw}' for {pretty}; keeping as text.")
            # maps
            ITEM_RARITIES[canon] = rarity
            KIND_MAP[canon] = kind if kind in ("seed","egg","tool","decor") else "other"
            PRETTY_MAP[canon] = pretty
            value = pretty.lower()       # watchlist value
            VALUE_TO_CANON[value] = canon
            KIND_BY_VALUE[value] = KIND_MAP[canon]

            # aliases for matching incoming shop names
            _add_alias(canon, pretty)
            _add_alias(canon, item_name_raw)  # "OrangeTulip" style
            # seeds often appear with suffixes; add both with and without
            if KIND_MAP[canon] == "seed":
                for suf in SEED_SUFFIXES:
                    _add_alias(canon, pretty + suf)
            # eggs may appear with "Egg"
            if KIND_MAP[canon] == "egg":
                for suf in EGG_SUFFIXES:
                    _add_alias(canon, pretty + suf)

            # watchlist options are rebuilt after aliases are loaded

    _dprint(f"[ITEMS] Loaded {len(ITEM_RARITIES)} items from {ITEM_RARITIES_PATH.name}.")
    _rebuild_item_options()

def _load_aliases():
    global ALIAS_MAP
    ALIAS_MAP = {}
    try:
        data = json.loads(ITEM_ALIASES_PATH.read_text(encoding='utf-8') or '{}')
        if isinstance(data, dict):
            for k, v in data.items():
                try:
                    ck = _sk(str(k))
                    disp = str(v).strip()
                    if ck and disp:
                        ALIAS_MAP[ck] = disp
                except Exception:
                    continue
        _dprint(f"[ALIASES] Loaded {len(ALIAS_MAP)} alias(es) from {ITEM_ALIASES_PATH.name}.")
    except FileNotFoundError:
        _dprint(f"[ALIASES] {ITEM_ALIASES_PATH.name} not found; using no overrides.")
    except Exception as e:
        _dprint(f"[ALIASES] Failed to load {ITEM_ALIASES_PATH.name}: {e}")
    finally:
        _rebuild_item_options()

def _get_mtime(p: Path) -> Optional[float]:
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return None
    except Exception:
        return None

def _reload_aliases_if_changed() -> bool:
    global _aliases_mtime
    cur = _get_mtime(ITEM_ALIASES_PATH)
    if cur != _aliases_mtime:
        _load_aliases()
        _aliases_mtime = cur
        return True
    return False

def _reload_items_if_changed() -> bool:
    global _rarities_mtime
    cur = _get_mtime(ITEM_RARITIES_PATH)
    if cur != _rarities_mtime:
        _load_items()
        # Re-apply aliases overlay after items load
        _reload_aliases_if_changed()
        _rarities_mtime = cur
        return True
    return False

def start_items_file_watcher():
    """Start a background thread that reloads items/aliases when files change."""
    def loop():
        global _rarities_mtime, _aliases_mtime
        # Initialize mtimes
        _rarities_mtime = _get_mtime(ITEM_RARITIES_PATH)
        _aliases_mtime = _get_mtime(ITEM_ALIASES_PATH)
        while True:
            try:
                changed_items = _reload_items_if_changed()
                changed_alias = _reload_aliases_if_changed() if not changed_items else False
                if changed_items or changed_alias:
                    which = []
                    if changed_items: which.append('items')
                    if changed_alias: which.append('aliases')
                    _dprint(f"[HOT-RELOAD] Updated {', '.join(which)} from disk.")
            except Exception as e:
                _dprint(f"[HOT-RELOAD] Error while checking files: {e}")
            finally:
                time.sleep(max(0.2, FILE_WATCH_INTERVAL_SEC))
    t = threading.Thread(target=loop, name='items_file_watcher', daemon=True)
    t.start()

def _canonical_key_for_name(name: Optional[str]) -> Optional[str]:
    if not name: return None
    s = _sk(name)
    if s in CANON_MAP:
        return CANON_MAP[s]
    # try to strip seed/egg suffix heuristically
    for suf in SEED_SUFFIXES + EGG_SUFFIXES:
        if s.endswith(_sk(suf)):
            s2 = s[: -len(_sk(suf))]
            if s2 in CANON_MAP:
                return CANON_MAP[s2]
    return CANON_MAP.get(s)

def _pretty_from_raw(name: Optional[str]) -> str:
    ck = _canonical_key_for_name(name)
    if ck:
        if ck in ALIAS_MAP and ALIAS_MAP[ck]:
            return ALIAS_MAP[ck]
        return PRETTY_MAP.get(ck, _humanize(name or "unknown"))
    return _humanize(name or "unknown")

def _rarity_from_raw(name: Optional[str]) -> str:
    ck = _canonical_key_for_name(name)
    r = ITEM_RARITIES.get(ck or "", "")
    return r.capitalize() if r else ""

# Initialize from file
_load_items()
_load_aliases()

# ---------- Rarity order (kept for reference/sorting if needed) ----------
RARITY_ORDER = {
    'celestial': 0, 'divine': 1, 'mythic': 2, 'legendary': 3, 'rare': 4, 'uncommon': 5, 'common': 6,
}

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

# Filter inventory by watchlist using squished keys/seed variants
def _base(x: str) -> str:
    x = (x or "").strip().lower()
    for suf in (" seed", " seeds", " kernel", " cutting", " cuttings", " pod", " pods", " spore", " pit", " egg", " eggs"):
        if x.endswith(suf):
            x = x[: -len(suf)]
            break
    return _sk(x)

def filter_inventory_by_watch(inv: List[Dict[str, Any]], guild_id: int) -> List[Dict[str, Any]]:
    watched = get_guild_watch(guild_id)
    if not watched:
        return inv
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

# ---------- JSON patch helpers (unchanged) ----------
def _ptr_decode(seg: str) -> str: return seg.replace("~1", "/").replace("~0", "~")

def _get_parent_and_key(root: Any, pointer: str):
    if pointer == "" or pointer == "/": return None, None
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
    if parent is None: return value
    if isinstance(parent, list): parent[int(key)] = value
    else: parent[key] = value
    return root

def _op_add(root: Any, pointer: str, value: Any):
    parent, key = _get_parent_and_key(root, pointer)
    if parent is None: return value
    if isinstance(parent, list):
        k = int(key)
        if k == len(parent): parent.append(value)
        else: parent.insert(k, value)
    else:
        parent[key] = value
    return root

def _op_remove(root: Any, pointer: str):
    parent, key = _get_parent_and_key(root, pointer)
    if parent is None: return {}
    if isinstance(parent, list): parent.pop(int(key))
    else: parent.pop(key, None)
    return root

def apply_patches(root: Any, patches: List[Dict[str, Any]]) -> Any:
    for p in patches:
        op = p.get("op"); path = p.get("path", "")
        if op == "replace": root = _op_replace(root, path, p.get("value"))
        elif op == "add":   root = _op_add(root, path, p.get("value"))
        elif op == "remove":root = _op_remove(root, path)
    return root

# ---------- Normalization ----------
def _current_stock(item: Dict[str, Any]) -> int:
    for k in ("remainingStock", "currentStock", "stock", "available", "qty", "quantity"):
        if k in item:
            val = item[k]
            if isinstance(val, (int, float)): return int(val)
            if isinstance(val, str):
                try:
                    s = val.strip()
                    if s == "": continue
                    if re.match(r'^-?\d+(?:\.\d+)?$', s):
                        return int(float(s))
                except Exception:
                    pass
    if "initialStock" in item and "sold" in item:
        try: return max(int(item.get("initialStock", 0) or 0) - int(item.get("sold", 0) or 0), 0)
        except Exception: pass
    try: return int(item.get("initialStock", 0) or 0)
    except Exception: return 0

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
        child = state["child"]["data"]; shops = child["shops"]
    except Exception:
        return {}
    def norm(kind: str, item: Dict[str, Any]) -> Dict[str, Any]:
        base = {
            "name": _display_name(item),
            "itemType": item.get("itemType", kind),
            "initialStock": int(item.get("initialStock", 0) or 0),
            "currentStock": _current_stock(item),
        }
        if kind == "seed":  return {"id": item.get("species"), **base}
        if kind == "tool":  return {"id": item.get("toolId"), **base}
        if kind == "egg":   return {"id": item.get("eggId"), **base}
        if kind == "decor": return {"id": item.get("decorId"), **base}
        return {"id": item.get("id"), **base}
    out = {"captured_at": int(time.time()), "currentTime": child.get("currentTime"), "shops": {}}
    for kind in ("seed", "egg", "tool", "decor"):
        s = shops.get(kind)
        if not s: continue
        inv = s.get("inventory") or []
        out["shops"][kind] = {
            "secondsUntilRestock": s.get("secondsUntilRestock"),
            "inventory": [norm(kind, it) for it in inv],
        }
    return out

# ---------- Formatting ----------
def _fmt_secs(secs: float) -> str:
    secs = max(0, int(round(secs))); h, rem = divmod(secs, 3600); m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def _format_seconds_verbose(s):
    try: s = float(s)
    except Exception: return 'N/A'
    if s < 0: return 'now'
    s = int(s); d, s = divmod(s, 86400); h, s = divmod(s, 3600); m, s = divmod(s, 60)
    parts = []; 
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s or not parts: parts.append(f"{s}s")
    return " ".join(parts)

def _format_seconds(s): return _format_seconds_verbose(s)

# ---------- Guild settings ----------
def load_guild_settings() -> Dict[str, Any]:
    try: return json.loads(GUILD_SETTINGS_PATH.read_text(encoding='utf-8') or '{}')
    except FileNotFoundError: return {}
    except Exception as e:
        _dprint(f"[WARN] failed to load guild_settings.json: {e}")
        return {}

def save_guild_settings(data: Dict[str, Any]):
    try: GUILD_SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding='utf-8')
    except Exception as e: _dprint(f"[WARN] failed to save guild_settings.json: {e}")

# ---------- Discord notifier ----------
class DiscordNotifier:
    def __init__(self, client: discord.Client):
        self.client = client

    def _find_channel_for_guild(self, guild: discord.Guild, cfg: Dict[str, Any]):
        cid = cfg.get('channel_id') if cfg else None
        if cid:
            try:
                ch = self.client.get_channel(int(cid))
                if ch and isinstance(ch, discord.TextChannel):
                    if ch.permissions_for(guild.me).send_messages:
                        return ch
            except Exception:
                pass
        try:
            if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
                return guild.system_channel
        except Exception:
            pass
        for ch in getattr(guild, 'text_channels', []):
            try:
                if ch.permissions_for(guild.me).send_messages:
                    return ch
            except Exception:
                continue
        return None

    def notify_snapshot(self, snapshot: Dict[str, Any]):
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
                        inv = filter_inventory_by_watch(inv_all, guild.id)
                        if not inv: continue

                        restock_str = _format_seconds(kind_shop.get('secondsUntilRestock'))
                        lines = [f"Captured: {time.ctime(captured)}", f"Restock in: {restock_str}", f"Items: {len(inv)} available (filtered)"]
                        for it in inv[:20]:
                            raw = it.get('name')
                            disp = _pretty_from_raw(raw)
                            cur = int(it.get('currentStock', 0))
                            init = int(it.get('initialStock', 0))
                            rc = _rarity_from_raw(raw)
                            lines.append(f"{disp} — {cur}/{init}" + (f" — {rc}" if rc else ""))
                        if len(inv) > 20:
                            lines.append(f"...and {len(inv)-20} more items")
                        msg = "\n".join(lines)
                        if len(msg) > 1900:
                            msg = msg[:1900] + '\n...truncated'
                        ch = self._find_channel_for_guild(guild, cfg)
                        if ch:
                            try: await ch.send(f"**Shop {kind} update**\n{msg}")
                            except Exception:
                                try:
                                    owner = guild.owner
                                    if owner: await owner.send(f"**Shop {kind} update**\n{msg}")
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
                if not ch: continue
                with state_lock:
                    cur = None if last_normalized is None else last_normalized
                kind_shop_now = cur['shops'].get(kind) if cur and cur.get('shops') and cur['shops'].get(kind) else (kind_shop or {})
                inv_all = (kind_shop_now or {}).get('inventory', []) or []
                inv_all = [i for i in inv_all if int(i.get('currentStock', 0)) > 0] if PRINT_ONLY_AVAILABLE else inv_all
                inv = filter_inventory_by_watch(inv_all, guild.id)
                if not inv: continue

                header = f"@everyone\n**MAGIC GARDEN ALERT, {kind.capitalize()} restocked:**\n\n"
                item_lines = []
                for it in inv[:50]:
                    raw = it.get('name') or _display_name(it)
                    disp = _pretty_from_raw(raw)
                    try:
                        cur_stock = int(it.get('currentStock', 0))
                    except Exception:
                        try: cur_stock = int(float(str(it.get('currentStock', 0)).strip()))
                        except Exception: cur_stock = 0
                    rc = _rarity_from_raw(raw)
                    item_lines.append(f"{disp} — X {cur_stock}" + (f" — {rc}" if rc else ""))
                msg = header + "\n".join(item_lines)
                if len(msg) > 1900:
                    msg = msg[:1900] + '\n...truncated'
                try:
                    await ch.send(msg)
                except Exception:
                    try:
                        owner = guild.owner
                        if owner: await owner.send(msg)
                    except Exception:
                        pass
        loop.create_task(_do())

# ---------- Monitor (Playwright) ----------
def monitor_loop(notifier: DiscordNotifier):
    global full_state, last_normalized, restock_timers, last_refresh_at

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
                                restock_timers[k] = {"secs": period, "t0": now_mono, "period": period}
                                remain = period
                        parts.append(f"{k} {_fmt_secs(remain)}")
                    else:
                        parts.append(f"{k} —")
            _dprint("[COUNTDOWN] " + " | ".join(parts))

            if kinds_to_report:
                try:
                    with state_lock:
                        prev_snapshot = last_normalized
                        for k in kinds_to_report: pending_print_kinds.add(k)
                    force_refresh_requested.set()
                    time.sleep(2.0)
                    with state_lock: cur_snapshot = last_normalized
                    waited = (cur_snapshot is not None and cur_snapshot is not prev_snapshot)
                except Exception:
                    waited = False

                with state_lock:
                    cur = last_normalized
                    for k in kinds_to_report:
                        try:
                            already_handled = (k not in pending_print_kinds)
                            if already_handled: continue
                            if not waited:
                                _dprint(f"[LOCAL] Skipping {k} because no fresh normalized state was received after refresh.")
                                continue
                            if cur and cur.get('shops') and cur['shops'].get(k):
                                _dprint(f"[LOCAL] Reporting {k} after fresh normalized state.")
                                notifier.notify_restock(k, cur['shops'].get(k, {}))
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
            global full_state, last_normalized
            try:
                payload = params.get('response', {}).get('payloadData', '')
                if not payload or not (payload.startswith('{') or payload.startswith('[')):
                    return
                obj = json.loads(payload)
            except Exception:
                return

            t = obj.get('type')
            try:
                _dprint(f"[WS_FRAME] type={t} len={len(payload)}")
                if DEBUG:
                    excerpt = payload if len(payload) <= 2000 else payload[:2000] + '...'
                    _dprint(f"[WS_PAYLOAD_EXCERPT] {excerpt}")
            except Exception:
                pass

            if t == 'Welcome':
                fs = obj.get('fullState') or {}
                with state_lock:
                    full_state = fs
                    cur = normalize_shops(full_state)
                    if not cur: return
                    last_normalized = cur
                    now = time.monotonic()
                    for kind, s in cur.get('shops', {}).items():
                        secs = s.get('secondsUntilRestock')
                        if isinstance(secs, (int, float)):
                            period = restock_timers.get(kind, {}).get('period', DEFAULT_PERIODS.get(kind, float(secs)))
                            restock_timers[kind] = {'secs': float(secs), 't0': now, 'period': float(period)}
                    if pending_print_kinds:
                        for kind in sorted(pending_print_kinds):
                            try: notifier.notify_restock(kind, cur['shops'].get(kind, {}))
                            except Exception: pass
                        pending_print_kinds.clear()
                return

            if t == 'PartialState':
                patches = obj.get('patches') or []
                if not patches: return
                with state_lock:
                    full_state = apply_patches(full_state, patches)
                    cur = normalize_shops(full_state)
                    if not cur: return
                    now = time.monotonic()
                    for kind, s in cur.get('shops', {}).items():
                        cur_secs = s.get('secondsUntilRestock')
                        if isinstance(cur_secs, (int, float)):
                            rt = restock_timers.get(kind)
                            if not rt:
                                restock_timers[kind] = {'secs': float(cur_secs), 't0': now, 'period': DEFAULT_PERIODS.get(kind, float(cur_secs))}
                            else:
                                prev_secs = float(rt['secs'])
                                restock_timers[kind]['secs'] = float(cur_secs)
                                restock_timers[kind]['t0'] = now
                                if float(cur_secs) > prev_secs + 3:
                                    restock_timers[kind]['period'] = float(cur_secs)
                                    _dbg(f"{kind} period updated via server reset → {cur_secs}s")
                    last_normalized = cur
                    if pending_print_kinds:
                        for kind in sorted(pending_print_kinds):
                            try: notifier.notify_restock(kind, cur['shops'].get(kind, {}))
                            except Exception: pass
                        pending_print_kinds.clear()

        client.on('Network.webSocketFrameReceived', on_ws_frame)

        # Navigation + open SHOP
        page.goto(ROOM_URL)
        try:
            page.get_by_role('button', name=re.compile(r"\bSHOP\b", re.I)).click(timeout=6000)
        except Exception:
            pass

        threading.Thread(target=countdown_loop, daemon=True).start()
        # Start file watcher for hot-reload of items/aliases
        start_items_file_watcher()

        # Wait for first normalized state
        deadline = time.time() + MAX_WAIT_SEC
        while time.time() < deadline:
            with state_lock:
                if last_normalized is not None: break
            time.sleep(0.1)

        _dprint("[MONITOR] Running. Press Ctrl+C to stop this process.")
        try:
            while True:
                if refresh_requested.is_set() or force_refresh_requested.is_set():
                    forced = force_refresh_requested.is_set()
                    now = time.time()
                    try:
                        with state_lock:
                            can = (now - float(last_refresh_at)) >= MIN_REFRESH_COOLDOWN_SEC or forced
                        if can:
                            _dprint("[MONITOR] Refresh requested; reloading page." + (" (forced)" if forced else ""))
                            try: page.reload()
                            except Exception as e: _dprint(f"[MONITOR] Page reload failed: {e}")
                            with state_lock: last_refresh_at = now
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

# ---------- Watchlist UI ----------
PAGE_SIZE = 25  # Discord max options per select

def chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i+size]

class KindSelect(discord.ui.Select):
    def __init__(self, parent_view: "WatchlistView"):
        self.parent_view = parent_view
        # Include dynamic kinds present in file, plus All/Other
        kinds = sorted(set(list(KIND_MAP.values()) + ["seed","egg","tool","decor","other"]))
        kinds = ["all"] + [k for k in ["seed","egg","tool","decor","other"] if k in kinds]
        options = [
            discord.SelectOption(label=k.title() if k != 'all' else 'All kinds', value=k, default=(k == parent_view.kind))
            for k in kinds
        ]
        super().__init__(placeholder='Choose a kind…', min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.user_id:
            await interaction.response.send_message("Only the command invoker can use this control.", ephemeral=True)
            return
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
        self.parent_view.selection -= page_values
        self.parent_view.selection |= chosen
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
        self.selection: Set[str] = get_guild_watch(guild_id)
        self.update_components()

    def _items_for_kind(self):
        if self.kind == 'all':
            return ITEM_OPTIONS
        return [(label, val, r) for (label, val, r) in ITEM_OPTIONS if KIND_BY_VALUE.get(val, 'other') == self.kind]

    def page_count(self):
        total = len(self._items_for_kind())
        return 0 if total == 0 else (total + PAGE_SIZE - 1) // PAGE_SIZE

    def _current_page_items(self):
        items = self._items_for_kind()
        start = self.page * PAGE_SIZE
        return items[start:start + PAGE_SIZE]

    def render_header(self) -> str:
        total = len(self._items_for_kind())
        selected = len(self.selection)
        kind_label = self.kind.title() if self.kind != 'all' else 'All'
        pc = self.page_count() or 1
        return f"Configure watchlist for this server (kind: {kind_label} — page {self.page+1}/{pc}):\nSelected **{selected}** of **{total}** items."

    def update_components(self):
        self.clear_items()
        self.add_item(KindSelect(self))
        if self.page_count() > 0:
            self.add_item(ItemSelect(self))

        prev = discord.ui.Button(label="← Prev", style=discord.ButtonStyle.secondary, disabled=(self.page <= 0))
        nextb = discord.ui.Button(label="Next →", style=discord.ButtonStyle.secondary, disabled=(self.page >= max(self.page_count()-1, 0)))
        save = discord.ui.Button(label="Save", style=discord.ButtonStyle.success)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger)

        async def on_prev(i: discord.Interaction):
            if i.user.id != self.user_id:
                await i.response.send_message("Only the command invoker can use this control.", ephemeral=True); return
            self.page = max(0, self.page-1); self.update_components()
            await i.response.edit_message(content=self.render_header(), view=self)

        async def on_next(i: discord.Interaction):
            if i.user.id != self.user_id:
                await i.response.send_message("Only the command invoker can use this control.", ephemeral=True); return
            self.page = min(max(self.page_count()-1, 0), self.page+1); self.update_components()
            await i.response.edit_message(content=self.render_header(), view=self)

        async def on_save(i: discord.Interaction):
            if i.user.id != self.user_id:
                await i.response.send_message("Only the command invoker can save.", ephemeral=True); return
            set_guild_watch(self.guild_id, list(self.selection))
            names = [lbl for (lbl,val,_r) in ITEM_OPTIONS if val in self.selection][:20]
            extra = "" if len(self.selection) <= 20 else f"\n...and {len(self.selection)-20} more"
            await i.response.edit_message(content=f"✅ Saved **{len(self.selection)}** watched items for this server.\n" + ("\n".join(names) + extra if names else "No items selected."), view=None)

        async def on_cancel(i: discord.Interaction):
            if i.user.id != self.user_id:
                await i.response.send_message("Only the command invoker can cancel.", ephemeral=True); return
            self.stop(); await i.response.edit_message(content="❌ Canceled. No changes saved.", view=None)

        prev.callback = on_prev; nextb.callback = on_next; save.callback = on_save; cancel.callback = on_cancel
        self.add_item(prev); self.add_item(nextb); self.add_item(save); self.add_item(cancel)

@tree.command(name="shop_watch", description="Choose which items to be notified about (checklist).")
async def shop_watch(interaction: discord.Interaction):
    if not ITEM_OPTIONS:
        await interaction.response.send_message("No items found in item_rarities.json.", ephemeral=True); return
    view = WatchlistView(interaction.user.id, interaction.guild.id)
    await interaction.response.send_message(view.render_header(), view=view, ephemeral=True)

@tree.command(name="shop_watch_view", description="Show the current watchlist for this server.")
async def shop_watch_view(interaction: discord.Interaction):
    sel = list(get_guild_watch(interaction.guild.id))
    if not sel:
        await interaction.response.send_message("This server has **no watchlist** set. Use `/shop_watch` to configure.", ephemeral=True); return
    labels = [lbl for (lbl,val,r) in ITEM_OPTIONS if val in sel]
    labels.sort()
    preview = "\n".join(labels[:30]); extra = f"\n...and {len(labels)-30} more" if len(labels) > 30 else ""
    await interaction.response.send_message(f"Watched items (**{len(labels)}**):\n{preview}{extra}", ephemeral=True)

@tree.command(name="shop_stock", description="Show current shop stock snapshot (ephemeral).")
async def shop_stock(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    with state_lock:
        cur = None if last_normalized is None else json.loads(json.dumps(last_normalized))
    if not cur:
        await interaction.followup.send("No snapshot available yet. Please try again shortly.", ephemeral=True); return
    captured = cur.get('captured_at'); shops = cur.get('shops', {})
    lines = []; 
    if captured: lines.append(f"Captured: {time.ctime(captured)}")
    for kind in ('seed', 'egg', 'tool', 'decor'):
        kind_shop = shops.get(kind)
        if not kind_shop:
            lines.append(f"{kind.title()}: (no data)"); continue
        inv = kind_shop.get('inventory', []) or []
        if PRINT_ONLY_AVAILABLE:
            inv = [i for i in inv if int(i.get('currentStock', 0)) > 0]
        if not inv:
            lines.append(f"{kind.title()}: (no items with stock > 0)"); continue
        item_lines = []
        for it in inv[:50]:
            raw = it.get('name') or _display_name(it)
            disp = _pretty_from_raw(raw)
            curstock = int(it.get('currentStock', 0))
            init = int(it.get('initialStock', 0))
            rc = _rarity_from_raw(raw)
            item_lines.append(f"{disp} — {curstock}/{init}" + (f" — {rc}" if rc else ""))
        header = f"{kind.title()} ({len(item_lines)} available):"
        body = "; ".join(item_lines[:10])
        if len(item_lines) > 10: body += f"; ...and {len(item_lines)-10} more"
        lines.append(header + " " + body)
    msg = "\n".join(lines)
    if len(msg) > 1900: msg = msg[:1900] + '\n...truncated'
    await interaction.followup.send(msg, ephemeral=True)

@tree.command(name="shop_alias_reload", description="Reload item display aliases from item_aliases.json")
async def shop_alias_reload(interaction: discord.Interaction):
    try:
        _load_aliases()
        await interaction.response.send_message(f"Reloaded {len(ALIAS_MAP)} alias(es) from item_aliases.json.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to reload aliases: {e}", ephemeral=True)

# ---------- Events ----------
@client.event
async def on_ready():
    global monitor_thread
    _dprint(f"[DISCORD] Logged in as {client.user} (id={client.user.id})")
    try:
        wl = load_watchlist()
        total_guilds = len(wl); total_items = sum(len(v) for v in wl.values())
        _dprint(f"[WATCHLIST] Loaded watchlist for {total_guilds} guild(s), {total_items} total selections.")
    except Exception:
        pass
    if monitor_thread is None or not monitor_thread.is_alive():
        monitor_thread = threading.Thread(target=monitor_loop, args=(notifier,), daemon=True)
        monitor_thread.start()
    try:
        for g in client.guilds: await tree.sync(guild=g)
        await tree.sync()
        _dprint(f"[DISCORD] Slash commands synced to {len(client.guilds)} guild(s) + global.")
    except Exception as e:
        _dprint(f"[WARN] command sync failed: {e}")

@client.event
async def on_message(message: discord.Message):
    if message.author.bot: return
    cmd = message.content.strip().lower()
    if cmd == '!shop_snapshot':
        await message.channel.send('Snapshot feature is disabled in this build.')
    elif cmd == '!shop_debug':
        with state_lock:
            if not restock_timers:
                await message.channel.send("No timers yet."); return
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
        print('Missing DISCORD_TOKEN in environment/.env'); raise SystemExit(1)
    client.run(DISCORD_TOKEN)
