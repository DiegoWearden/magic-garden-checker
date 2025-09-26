#!/usr/bin/env python3
"""
Simple GUI to edit item_aliases.json used by the bot for display name overrides.

Usage:
  python alias_editor.py

Opens http://127.0.0.1:5001 with a form to edit aliases for items present in item_rarities.json.
Only non-empty aliases are saved; empty fields mean "use default name".
"""

import json
from pathlib import Path
from typing import Dict, List, Tuple

from flask import Flask, request, redirect, url_for, render_template_string

ROOT = Path(__file__).resolve().parent
RARITIES_PATH = ROOT / "item_rarities.json"
ALIASES_PATH = ROOT / "item_aliases.json"

app = Flask(__name__)


def _load_items() -> List[str]:
    """Return a sorted list of item names (pretty) from item_rarities.json."""
    items: List[str] = []
    try:
        raw = json.loads(RARITIES_PATH.read_text(encoding="utf-8") or "{}")
        if isinstance(raw, dict):
            for _kind, mapping in raw.items():
                if isinstance(mapping, dict):
                    items.extend(list(mapping.keys()))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    # Dedupe and sort
    seen = set()
    out: List[str] = []
    for name in items:
        if not name:
            continue
        k = str(name)
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    out.sort(key=lambda s: s.lower())
    return out


def _load_aliases() -> Dict[str, str]:
    try:
        data = json.loads(ALIASES_PATH.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


@app.get("/")
def index():
    q = (request.args.get("q") or "").strip().lower()
    items = _load_items()
    aliases = _load_aliases()
    rows: List[Tuple[str, str]] = []  # (item, alias)
    for item in items:
        if q and q not in item.lower():
            continue
        rows.append((item, aliases.get(item, "")))
    return render_template_string(
        TEMPLATE,
        rows=rows,
        total=len(items),
        shown=len(rows),
        q=q,
    )


@app.post("/save")
def save():
    items = request.form.getlist("item")
    values = request.form.getlist("alias")
    out: Dict[str, str] = {}
    for item, alias in zip(items, values):
        alias = (alias or "").strip()
        item = (item or "").strip()
        if alias:
            out[item] = alias
    try:
        ALIASES_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    # Back to index with saved=1 flag
    return redirect(url_for("index", saved=1))


TEMPLATE = r"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Item Alias Editor</title>
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 24px; }
      table { border-collapse: collapse; width: 100%; max-width: 1100px; }
      th, td { border-bottom: 1px solid #ddd; padding: 8px; text-align: left; }
      th { background: #f7f7f7; position: sticky; top: 0; }
      input[type=text] { width: 100%; padding: 6px 8px; }
      .meta { color: #666; font-size: 14px; margin-bottom: 12px; }
      .row-idx { color: #999; width: 36px; }
      .topbar { display: flex; gap: 12px; align-items: center; margin-bottom: 12px; }
      .btn { padding: 8px 12px; background: #1976d2; color: white; border: none; border-radius: 4px; cursor: pointer; }
      .btn:disabled { opacity: .6; cursor: default; }
      .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #eee; margin-left: 8px; font-size: 12px; }
      .saved { color: #2e7d32; }
    </style>
  </head>
  <body>
    <h2>Item Alias Editor</h2>
    <div class="meta">Editing <code>item_aliases.json</code>. Leave a field empty to use the default item name.</div>
    <div class="topbar">
      <form method="get" action="/">
        <input type="text" name="q" placeholder="Filter items…" value="{{ q }}" />
        <button class="btn" type="submit">Filter</button>
        <span class="pill">Total: {{ total }}</span>
        <span class="pill">Shown: {{ shown }}</span>
        {% if request.args.get('saved') %}<span class="pill saved">Saved</span>{% endif %}
      </form>
    </div>

    <form method="post" action="/save">
      <table>
        <thead>
          <tr>
            <th class="row-idx">#</th>
            <th>Item (default display)</th>
            <th>Custom alias (optional)</th>
          </tr>
        </thead>
        <tbody>
          {% for item, alias in rows %}
          <tr>
            <td class="row-idx">{{ loop.index }}</td>
            <td><code>{{ item }}</code><input type="hidden" name="item" value="{{ item }}" /></td>
            <td><input type="text" name="alias" value="{{ alias }}" placeholder="Leave empty to use default" /></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      <div style="margin-top:16px;">
        <button class="btn" type="submit">Save</button>
        <span class="meta">After saving, run the Discord slash command <code>/shop_alias_reload</code> to apply changes.</span>
      </div>
    </form>
  </body>
  </html>
"""


if __name__ == "__main__":
    print("Input:", RARITIES_PATH)
    print("Output:", ALIASES_PATH)
    app.run(host="127.0.0.1", port=5001)


