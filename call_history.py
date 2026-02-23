"""Call history: last 20 numbers (in/out), persisted."""

import json
import os
from datetime import datetime

from accounts import CONFIG_DIR

HISTORY_FILE = os.path.join(CONFIG_DIR, "call_history.json")
MAX_ENTRIES = 20


def _ensure_dir():
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)


def load_history():
    """Return list of {uri, direction, time}, newest first, max 20."""
    if not os.path.isfile(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r") as f:
            data = json.load(f)
        entries = data.get("entries", [])[:MAX_ENTRIES]
        return entries
    except (json.JSONDecodeError, IOError):
        return []


def add_entry(uri, direction="out"):
    """Append a call to history (direction 'in' or 'out'), keep max 20."""
    uri = (uri or "").strip()
    if not uri:
        return
    entry = {
        "uri": uri,
        "direction": "in" if direction == "in" else "out",
        "time": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    entries = load_history()
    # Prepend and dedupe by uri (keep newest)
    entries = [entry] + [e for e in entries if e.get("uri") != uri]
    entries = entries[:MAX_ENTRIES]
    _ensure_dir()
    with open(HISTORY_FILE, "w") as f:
        json.dump({"entries": entries}, f, indent=2)
