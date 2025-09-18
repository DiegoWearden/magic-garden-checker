#!/usr/bin/env python3
# Simple editor server for item_rarities.json — no external dependencies
import json, os, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
ITEM_PATH = ROOT / 'item_rarities.json'
# Port can be provided as first CLI arg or via RARITY_EDITOR_PORT env var; default 8000
DEFAULT_PORT = int(os.getenv('RARITY_EDITOR_PORT', '8000'))
try:
    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
except Exception:
    PORT = DEFAULT_PORT

HTML = '''<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Item Rarities Editor</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body{font-family:system-ui,Segoe UI,Arial;margin:16px}
    .section{margin-bottom:20px}
    .item{display:flex;align-items:center;gap:12px;padding:6px 0;border-bottom:1px solid #eee}
    .item .name{flex:1}
    .radios{display:flex;gap:8px;align-items:center}
    label{font-size:13px}
    .controls{margin:8px 0 16px}
    .small{font-size:13px;color:#666}
    .add-row{display:flex;gap:8px;margin-top:8px}
    .cat-select{width:140px}
  </style>
</head>
<body>
  <h2>Item Rarities Editor</h2>
  <div class="controls">
    <button id="load">Load</button>
    <button id="save">Save</button>
    <button id="download">Download JSON</button>
    <input id="filein" type="file" style="display:none">
    <button id="upload">Upload JSON</button>
    <span id="msg" style="margin-left:10px;color:green"></span>
  </div>

  <div id="editor">
    <div class="section" id="seeds">
      <h3>Seeds</h3>
      <div class="small">Choose one rarity per item.</div>
      <div class="list"></div>
    </div>
    <div class="section" id="eggs">
      <h3>Eggs</h3>
      <div class="list"></div>
    </div>
    <div class="section" id="tools">
      <h3>Tools</h3>
      <div class="list"></div>
    </div>
    <div class="section" id="decor">
      <h3>Decor</h3>
      <div class="list"></div>
    </div>

    <div class="add-row">
      <input id="new-name" placeholder="New item id or display name" style="flex:1">
      <select id="new-cat" class="cat-select"><option value="seed">Seed</option><option value="egg">Egg</option><option value="tool">Tool</option><option value="decor">Decor</option></select>
      <button id="add-new">Add</button>
    </div>
  </div>

  <script>
    // rarities and canonical item lists (keeps GUI aligned with shop_snapshot)
    const RARITIES = ['common','uncommon','rare','epic','legendary','mythic','divine','celestial'];
    const SEEDS = [
      "Carrot Seed","Strawberry Seed","Aloe Seed","Blueberry Seed","Apple Seed","Tulip Seed",
      "Tomato Seed","Daffodil Seed","Corn Kernel","Watermelon Seed","Pumpkin Seed",
      "Echeveria Cutting","Coconut Seed","Banana Seed","Lily Seed","Burro's Tail Cutting",
      "Mushroom Spore","Cactus Seed","Bamboo Seed","Grape Seed","Pepper Seed","Lemon Seed",
      "Passion Fruit Seed","Dragon Fruit Seed","Lychee Pit","Sunflower Seed","Starweaver Pod"
    ];
    const EGGS = [
      "Common Egg","Uncommon Egg","Rare Egg","Legendary Egg","Mythical Egg"
    ];
    const TOOLS = ["Watering Can","Planter Pot","Shovel"];
    const DECOR = [
      "Small Rock","Medium Rock","Large Rock","Wood Bench","Wood Arch","Wood Bridge","Wood Lamp Post","Wood Owl",
      "Stone Bench","Stone Arch","Stone Bridge","Stone Lamp Post","Stone Gnome",
      "Marble Bench","Marble Arch","Marble Bridge","Marble Lamp Post"
    ];

    // helper: normalize keys as the bot expects
    function norm(k){ return (k||'').toString().trim().toLowerCase(); }

    // render a single item row into container; radio inputs named by category+index
    function renderItem(container, category, key, selected){
      const idx = container.children.length;
      const div = document.createElement('div'); div.className='item';
      const name = document.createElement('div'); name.className='name'; name.textContent = key; div.appendChild(name);
      const radios = document.createElement('div'); radios.className='radios';
      RARITIES.forEach(r=>{
        const id = `r_${category}_${idx}_${r}`;
        const lab = document.createElement('label');
        const inp = document.createElement('input'); inp.type='radio'; inp.name = `rar_${category}_${key}`; inp.value = r; inp.id = id;
        if(selected === r) inp.checked = true;
        lab.appendChild(inp); lab.appendChild(document.createTextNode(' ' + r));
        radios.appendChild(lab);
      });
      div.appendChild(radios);
      const del = document.createElement('button'); del.textContent='Delete'; del.onclick=()=>div.remove(); del.style.marginLeft='8px';
      div.appendChild(del);
      container.appendChild(div);
    }

    async function load(){
      try{
        const r = await fetch('/api/load');
        const data = r.ok ? await r.json() : {};
        document.getElementById('msg').textContent='Loaded'; document.getElementById('msg').style.color='green';
        // clear lists
        document.querySelector('#seeds .list').innerHTML='';
        document.querySelector('#eggs .list').innerHTML='';
        document.querySelector('#tools .list').innerHTML='';
        document.querySelector('#decor .list').innerHTML='';
        // helper to get rarity from loaded data, default 'common'
        const getR = (name) => (data[norm(name)] || data[name.toLowerCase()] || 'common');
        // render canonical lists
        SEEDS.forEach(s => renderItem(document.querySelector('#seeds .list'), 'seed', s, getR(s)));
        EGGS.forEach(e => renderItem(document.querySelector('#eggs .list'), 'egg', e, getR(e)));
        TOOLS.forEach(t => renderItem(document.querySelector('#tools .list'), 'tool', t, getR(t)));
        DECOR.forEach(d => renderItem(document.querySelector('#decor .list'), 'decor', d, getR(d)));
        // render any extra keys present in data that weren't in canonical lists
        const seen = new Set();
        [...SEEDS, ...EGGS, ...TOOLS, ...DECOR].forEach(x=>seen.add(norm(x)));
        Object.keys(data).forEach(k => {
          if(seen.has(k)) return;
          const raw = k; const val = data[k];
          // attempt to categorize by substring
          if(k.includes('egg')) renderItem(document.querySelector('#eggs .list'), 'egg', raw, val);
          else if(k.includes('can') || k.includes('shovel') || k.includes('pot') || k.includes('tool')) renderItem(document.querySelector('#tools .list'), 'tool', raw, val);
          else if(k.includes('seed')) renderItem(document.querySelector('#seeds .list'), 'seed', raw, val);
          else renderItem(document.querySelector('#decor .list'), 'decor', raw, val);
        });
      }catch(e){ document.getElementById('msg').textContent='Load failed'; document.getElementById('msg').style.color='red'; }
    }

    function collect(){
      const out = {};
      ['seeds','eggs','tools','decor'].forEach(sec => {
        const ct = document.querySelector(`#${sec} .list`);
        if(!ct) return;
        for(const child of ct.children){
          const name = child.querySelector('.name').textContent.trim();
          const radios = child.querySelectorAll('input[type=radio]');
          let chosen = null;
          radios.forEach(r=>{ if(r.checked) chosen = r.value; });
          if(!chosen) chosen = 'common';
          out[norm(name)] = chosen; // normalized key
        }
      });
      return out;
    }

    document.getElementById('save').onclick = async ()=>{
      const obj = collect();
      try{
        const r = await fetch('/api/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(obj)});
        if(!r.ok) throw new Error('save failed');
        document.getElementById('msg').textContent='Saved'; document.getElementById('msg').style.color='green';
      }catch(e){ document.getElementById('msg').textContent='Save failed'; document.getElementById('msg').style.color='red'; }
    };

    document.getElementById('download').onclick = ()=>{
      const blob = new Blob([JSON.stringify(collect(), null, 2)], {type:'application/json'});
      const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href=url; a.download='item_rarities.json'; a.click(); URL.revokeObjectURL(url);
    };

    document.getElementById('upload').onclick = ()=> document.getElementById('filein').click();
    document.getElementById('filein').onchange = async (ev)=>{
      const f = ev.target.files[0]; if(!f) return; const txt = await f.text();
      try{ const obj = JSON.parse(txt); // write to server directly
        const r = await fetch('/api/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(obj)});
        if(r.ok){ await load(); document.getElementById('msg').textContent='Uploaded and loaded'; document.getElementById('msg').style.color='green'; }
        else { document.getElementById('msg').textContent='Upload failed'; document.getElementById('msg').style.color='red'; }
      }catch(e){ document.getElementById('msg').textContent='Invalid JSON'; document.getElementById('msg').style.color='red'; }
    };

    document.getElementById('add-new').onclick = ()=>{
      const name = document.getElementById('new-name').value.trim();
      const cat = document.getElementById('new-cat').value;
      if(!name) return;
      const mapping = { 'seed':'#seeds .list', 'egg':'#eggs .list', 'tool':'#tools .list', 'decor':'#decor .list' };
      renderItem(document.querySelector(mapping[cat]), cat, name, 'common');
      document.getElementById('new-name').value='';
    };

    // auto-load
    window.addEventListener('load', ()=>load());
  </script>
</body>
</html>
'''

class Handler(BaseHTTPRequestHandler):
    def _set_cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors()
        self.end_headers()

    def do_GET(self):
        if self.path in ('/', '/editor'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self._set_cors()
            self.end_headers()
            self.wfile.write(HTML.encode('utf-8'))
            return
        if self.path == '/api/load':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self._set_cors()
            self.end_headers()
            if ITEM_PATH.exists():
                try:
                    data = json.loads(ITEM_PATH.read_text(encoding='utf-8') or '{}')
                except Exception:
                    data = {}
            else:
                data = {}
            self.wfile.write(json.dumps(data, indent=2).encode('utf-8'))
            return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == '/api/save':
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length) if length else b''
            try:
                obj = json.loads(body.decode('utf-8') or '{}')
                # normalize keys to lower-case strings
                norm = {str(k).lower(): str(v).lower() for k,v in (obj.items() if isinstance(obj, dict) else [])}
                with open(ITEM_PATH, 'w', encoding='utf-8') as f:
                    json.dump(norm, f, indent=2, sort_keys=True)
                self.send_response(200)
                self._set_cors()
                self.end_headers()
                self.wfile.write(b'Saved')
            except Exception as e:
                self.send_response(400)
                self._set_cors()
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
            return
        self.send_response(404); self.end_headers()

if __name__ == '__main__':
    print(f"Starting editor at http://127.0.0.1:{PORT}/editor")
    # ensure file exists
    if not ITEM_PATH.exists():
        try:
            ITEM_PATH.write_text('{}', encoding='utf-8')
        except Exception:
            pass
    try:
        server = HTTPServer(('127.0.0.1', PORT), Handler)
    except OSError as e:
        print(f"Failed to bind to 127.0.0.1:{PORT}: {e}")
        print("Port already in use. Stop the process using that port or run this script with a different port:\n  python rarity_editor.py 8080\nOr set RARITY_EDITOR_PORT environment variable before running.")
        raise SystemExit(1)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('Shutting down')
        server.server_close()
