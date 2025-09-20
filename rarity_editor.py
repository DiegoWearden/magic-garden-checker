# rarity_gui.py
import os
import json
import argparse
from pathlib import Path
from collections import OrderedDict
from flask import Flask, request, redirect, url_for, render_template_string

# --- CLI / config ---
parser = argparse.ArgumentParser(description="Web GUI to tag rarities for items in a JSON file.")
parser.add_argument("--input", "-i", default=os.getenv("INPUT_FILE", "discovered_items.json"),
                    help="Path to the discovered-items JSON (default: discovered_items.json)")
parser.add_argument("--output", "-o", default=os.getenv("OUTPUT_FILE", "item_rarities.json"),
                    help="Path to write the rarity mapping JSON (default: item_rarities.json)")
parser.add_argument("--port", "-p", type=int, default=int(os.getenv("PORT", "5000")),
                    help="Port to run the server on (default: 5000)")
args = parser.parse_args()

INPUT_FILE = Path(args.input)
OUTPUT_FILE = Path(args.output)

RARITIES = ["common", "uncommon", "rare", "legendary", "mythic", "divine", "celestial"]

app = Flask(__name__)

# --- helpers ---
def load_discovered() -> OrderedDict:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Input file not found: {INPUT_FILE.resolve()}")
    with INPUT_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f, object_pairs_hook=OrderedDict)
    normalized = OrderedDict()
    for kind, items in data.items():
        if isinstance(items, list):
            normalized[kind] = [str(x) for x in items]
    return normalized

def load_existing_mapping() -> dict:
    if not OUTPUT_FILE.exists():
        return {}
    try:
        with OUTPUT_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def build_default_mapping(discovered: OrderedDict, existing: dict) -> OrderedDict:
    out = OrderedDict()
    for kind, items in discovered.items():
        out[kind] = OrderedDict()
        existing_kind = existing.get(kind, {})
        for item in items:
            val = existing_kind.get(item)
            out[kind][item] = val if val in RARITIES else None
    return out

# --- routes ---
@app.route("/", methods=["GET"])
def index():
    discovered = load_discovered()
    existing = load_existing_mapping()
    mapping = build_default_mapping(discovered, existing)

    saved = request.args.get("saved")
    missing = request.args.get("missing")
    message = None
    if saved is not None:
        if missing and missing.isdigit() and int(missing) > 0:
            message = f"Saved to {OUTPUT_FILE.name}. {missing} item(s) still unassigned."
        else:
            message = f"Saved to {OUTPUT_FILE.name}. All items assigned! 🎉"

    return render_template_string(
        TEMPLATE,
        discovered=discovered,
        mapping=mapping,
        rarities=RARITIES,
        message=message,
        output_filename=str(OUTPUT_FILE.name),
        total_items=sum(len(v) for v in discovered.values())
    )

@app.route("/save", methods=["POST"])
def save():
    discovered = load_discovered()

    # Gather selections. Each item has checkbox group named "rarity::<kind>::<item>"
    selections = {}
    for kind, items in discovered.items():
        for item in items:
            key = f"rarity::{kind}::{item}"
            vals = request.form.getlist(key)
            # Fallback for any old template version that used "rarity::<item>"
            if not vals:
                vals = request.form.getlist(f"rarity::{item}")
            rarity = vals[-1].strip().lower() if vals else None
            if rarity in RARITIES:
                selections[(kind, item)] = rarity

    # Reassemble grouped mapping; unselected items saved as null
    out = OrderedDict()
    missing = 0
    for kind, items in discovered.items():
        out[kind] = OrderedDict()
        for item in items:
            rarity = selections.get((kind, item))
            if rarity:
                out[kind][item] = rarity
            else:
                out[kind][item] = None
                missing += 1

    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    return redirect(url_for("index", saved=1, missing=missing))

# --- inline template (single form + unique checkbox IDs) ---
TEMPLATE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Item Rarity Tagger</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    :root { --pad: 12px; --radius: 10px; --border:#e2e2e2; --muted:#666; --bg:#fafafa; }
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 0; background: white; color: #222; }
    header { position: sticky; top: 0; background: white; border-bottom: 1px solid var(--border); padding: var(--pad); z-index: 10; display:flex; gap:12px; align-items:center; }
    h1 { font-size: 18px; margin: 0 8px 0 0; }
    .pill { font-size: 12px; padding: 4px 8px; border: 1px solid var(--border); border-radius: 999px; background: var(--bg); color: var(--muted); }
    .bar { flex: 1; display:flex; gap:8px; }
    input[type="search"] { flex:1; padding:10px; border:1px solid var(--border); border-radius:8px; font-size:14px; }
    button { padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:white; cursor:pointer; font-size:14px; }
    button.primary { background:#111; color:white; border-color:#111; }
    main { padding: 16px; max-width: 1100px; margin: 0 auto; }
    .msg { margin: 12px 0 0 0; padding: 10px 12px; border:1px solid var(--border); border-radius:8px; background: #f4fff4; }
    details { border:1px solid var(--border); border-radius:12px; margin: 16px 0; background: var(--bg); }
    summary { list-style:none; padding: 12px 14px; font-weight:600; cursor:pointer; display:flex; align-items:center; gap:10px; }
    .count { font-weight:400; color: var(--muted); }
    table { width: 100%; border-collapse: collapse; background:white; }
    th, td { padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; }
    th { text-align:left; font-size:12px; color: var(--muted); background:#fcfcfc; position: sticky; top: 56px; z-index: 5; }
    .row.unassigned { background: #fff8e1; }
    .checks { display:flex; flex-wrap: wrap; gap: 8px; align-items:center; }
    .chip { display:inline-flex; align-items:center; gap:6px; border:1px solid var(--border); border-radius:999px; padding:6px 10px; cursor:pointer; user-select:none; }
    .chip .box { width:16px; height:16px; border:1px solid var(--border); border-radius:4px; display:inline-block; }
    input[type="checkbox"] { display:none; }
    input[type="checkbox"]:checked + label.chip { border-color:#111; background:#111; color:white; }
    input[type="checkbox"]:checked + label.chip .box { background:white; border-color:white; position:relative; }
    input[type="checkbox"]:checked + label.chip .box::after {
      content: ""; position:absolute; left:4px; top:1px; width:5px; height:9px; border-right:2px solid #111; border-bottom:2px solid #111; transform: rotate(45deg);
      background:transparent;
    }
    .muted { color: var(--muted); font-size: 12px; }
    .controls { display:flex; gap:8px; align-items:center; padding: 0 12px 12px 12px; flex-wrap:wrap; }
    .controls .chip { padding:4px 8px; }
    .clear-btn { font-size:12px; border-style:dashed; }
    .footer { display:flex; justify-content:space-between; align-items:center; margin-top:12px; }
    @media (max-width: 720px) { th:nth-child(2), td:nth-child(2) { display:none; } }
  </style>
</head>
<body>

<header>
  <h1>Item Rarity Tagger</h1>
  <span class="pill">{{ total_items }} item{{ '' if total_items == 1 else 's' }}</span>
  <div class="bar">
    <input id="search" type="search" placeholder="Search items (e.g., 'carrot', 'bench', 'egg')…"/>
  </div>
  <!-- This button submits the ONE main form below -->
  <button class="primary" type="submit" form="rarity-form">Save</button>
</header>

<main>
  {% if message %}
  <div class="msg">{{ message }}</div>
  {% endif %}

  <!-- ONE single form around everything that needs submitting -->
  <form id="rarity-form" method="post" action="{{ url_for('save') }}">
    <p class="muted">
      Click a rarity chip to “check” it for an item. Click the same chip again to clear. Saving writes <strong>{{ output_filename }}</strong>.
    </p>

    {% for kind, items in discovered.items() %}
    <details open>
      <summary>
        {{ kind }} <span class="count">({{ items|length }})</span>
      </summary>

      <div class="controls">
        <span class="muted">Bulk apply to all in <strong>{{ kind }}</strong>:</span>
        {% for r in rarities %}
          <button type="button" class="chip" data-bulk="{{ r }}" data-kind="{{ kind }}">{{ r }}</button>
        {% endfor %}
        <button type="button" class="clear-btn" data-bulk="__clear__" data-kind="{{ kind }}">Clear all</button>
        <span class="muted" style="margin-left:auto;">Tip: use the search box to filter.</span>
      </div>

      <table data-kind="{{ kind }}">
        <thead>
          <tr>
            <th style="width: 40%;">Item</th>
            <th style="width: 30%;">Kind</th>
            <th style="width: 30%;">Rarity</th>
          </tr>
        </thead>
        <tbody>
          {% for item in items %}
          {% set current = mapping[kind][item] %}
          {% set item_slug = (kind ~ '-' ~ item)|replace(' ', '_')|replace('/', '_')|replace('\\', '_')|replace('"','')|replace("'", '')|lower %}
          <tr class="row {% if not current %}unassigned{% endif %}" data-kind="{{ kind }}" data-item="{{ item|e }}">
            <td><label><strong>{{ item }}</strong></label></td>
            <td class="muted">{{ kind }}</td>
            <td>
              <div class="checks">
                {% for r in rarities %}
                  {% set cid = 'cb-' ~ item_slug ~ '-' ~ r %}
                  <input type="checkbox" id="{{ cid }}" name="rarity::{{ kind }}::{{ item }}" value="{{ r }}" {% if current == r %}checked{% endif %}>
                  <label class="chip" for="{{ cid }}"><span class="box"></span>{{ r }}</label>
                {% endfor %}
                <button type="button" class="clear-btn" data-clear="{{ kind }}::{{ item }}">Clear</button>
              </div>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </details>
    {% endfor %}

    <div class="footer">
      <button class="primary" type="submit">Save</button>
      <span class="muted">Unassigned items save as <code>null</code>.</span>
    </div>
  </form>
</main>

<script>
  // Search filter
  const search = document.getElementById('search');
  search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    document.querySelectorAll('tbody tr.row').forEach(tr => {
      const item = tr.dataset.item.toLowerCase();
      const kind = tr.dataset.kind.toLowerCase();
      tr.style.display = (item.includes(q) || kind.includes(q)) ? '' : 'none';
    });
  });

  // Enforce single selection per row (checkbox look, radio behavior)
  document.querySelectorAll('.checks').forEach(group => {
    group.addEventListener('change', (e) => {
      if (e.target.matches('input[type="checkbox"]')) {
        if (e.target.checked) {
          group.querySelectorAll('input[type="checkbox"]').forEach(cb => { if (cb !== e.target) cb.checked = false; });
        }
        const tr = group.closest('tr');
        const anyChecked = group.querySelector('input[type="checkbox"]:checked');
        tr.classList.toggle('unassigned', !anyChecked);
      }
    });
  });

  // Per-item clear
  document.querySelectorAll('button[data-clear]').forEach(btn => {
    btn.addEventListener('click', () => {
      const group = btn.closest('.checks');
      group.querySelectorAll('input[type="checkbox"]').forEach(cb => cb.checked = false);
      btn.closest('tr').classList.add('unassigned');
    });
  });

  // Bulk apply within a kind
  document.querySelectorAll('button[data-bulk]').forEach(btn => {
    btn.addEventListener('click', () => {
      const kind = btn.getAttribute('data-kind');
      const val = btn.getAttribute('data-bulk');
      document.querySelectorAll(`table[data-kind="${CSS.escape(kind)}"] tbody tr`).forEach(tr => {
        const group = tr.querySelector('.checks');
        if (val === '__clear__') {
          group.querySelectorAll('input[type="checkbox"]').forEach(cb => cb.checked = false);
          tr.classList.add('unassigned');
        } else {
          group.querySelectorAll('input[type="checkbox"]').forEach(cb => cb.checked = (cb.value === val));
          tr.classList.remove('unassigned');
        }
      });
    });
  });
</script>

</body>
</html>
"""

if __name__ == "__main__":
    print(f"Input:  {INPUT_FILE.resolve()}")
    print(f"Output: {OUTPUT_FILE.resolve()}")
    app.run(host="127.0.0.1", port=args.port, debug=False)
