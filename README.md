# Magic Garden Checker — Quick Usage

A small set of commands to get the bot and utilities running.

Prerequisites
- Python 3.9+

Setup

```bash
# create and activate a virtual environment, then install requirements
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Run the bot

```bash
python bot.py
```

Run the rarity editor

```bash
python rarity_editor.py
```

Edit item display aliases (GUI)

```bash
python alias_editor.py
# opens http://127.0.0.1:5001
# after saving, run /shop_alias_reload in Discord to apply without restart
```

Run the websocket scanner and write discovered items to a file

```bash
python ws_scan_items.py --timeout 30 --debug --headless --out discovered_items.json
```

That's all — use these commands to run the main programs in this repository.