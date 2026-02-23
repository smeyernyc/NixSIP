"""Persist speed dials and BLF (Busy Lamp Field) entries (per account)."""

import hashlib
import json
import os

from accounts import CONFIG_DIR


def _account_key(account_uri):
    """Stable filename-safe key for an account URI (None -> legacy single file)."""
    if not account_uri or not str(account_uri).strip():
        return None
    return hashlib.sha1(account_uri.encode()).hexdigest()[:16]


def _speeddials_path(account_uri):
    if _account_key(account_uri) is None:
        return os.path.join(CONFIG_DIR, "speeddials.json")
    return os.path.join(CONFIG_DIR, "speeddials_%s.json" % _account_key(account_uri))


def _blf_path(account_uri):
    if _account_key(account_uri) is None:
        return os.path.join(CONFIG_DIR, "blf.json")
    return os.path.join(CONFIG_DIR, "blf_%s.json" % _account_key(account_uri))

# Optional: set from GUI to send BLF debug lines to the debug log
_blf_debug_log = None


def set_blf_debug_log(callback):
    """Set a callback(msg) for BLF load/save debug messages (e.g. main window _log)."""
    global _blf_debug_log
    _blf_debug_log = callback


def _ensure_dir():
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)


def load_speeddials(account_uri=None):
    """Return list of {label, number} for the given account (None = legacy global file)."""
    path = _speeddials_path(account_uri)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data.get("speeddials", [])
    except (json.JSONDecodeError, IOError):
        return []


def save_speeddials(entries, account_uri=None):
    """Save list of {label, number} for the given account."""
    _ensure_dir()
    path = _speeddials_path(account_uri)
    with open(path, "w") as f:
        json.dump({"speeddials": entries}, f, indent=2)


def load_blf(account_uri=None):
    """Return list of {label, uri} for BLF subscriptions for the given account."""
    path = _blf_path(account_uri)
    if _blf_debug_log:
        _blf_debug_log("BLF: loading from %s" % path)
    if not os.path.isfile(path):
        if _blf_debug_log:
            _blf_debug_log("BLF: no config file, using empty list")
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
        entries = data.get("blf", [])
        if _blf_debug_log:
            _blf_debug_log("BLF: loaded %d entries from config" % len(entries))
        return entries
    except (json.JSONDecodeError, IOError) as e:
        if _blf_debug_log:
            _blf_debug_log("BLF: load error: %s" % e)
        return []


def save_blf(entries, account_uri=None):
    """Save list of {label, uri} for the given account."""
    path = _blf_path(account_uri)
    if _blf_debug_log:
        _blf_debug_log("BLF: saving %d entries to %s" % (len(entries), path))
    _ensure_dir()
    with open(path, "w") as f:
        json.dump({"blf": entries}, f, indent=2)
