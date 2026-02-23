"""SIP account storage and management."""

import json
import os

CONFIG_DIR = os.path.expanduser("~/.config/sipclient")
ACCOUNTS_FILE = os.path.join(CONFIG_DIR, "accounts.json")
PREFS_FILE = os.path.join(CONFIG_DIR, "prefs.json")


def _ensure_config_dir():
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)


def load_accounts():
    """Load accounts from disk. Returns list of account dicts."""
    _ensure_config_dir()
    path = ACCOUNTS_FILE
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data.get("accounts", [])
    except (json.JSONDecodeError, IOError):
        return []


def save_accounts(accounts):
    """Save list of account dicts to disk."""
    _ensure_config_dir()
    path = ACCOUNTS_FILE
    with open(path, "w") as f:
        json.dump({"accounts": accounts}, f, indent=2)


def account_label(acc):
    """Display label for an account (uri or name)."""
    return acc.get("label") or acc.get("uri") or "Unknown"


def add_account(uri, password, registrar=None, use_tls=True, label=None):
    """Add an account. uri should be like sip:user@domain."""
    accounts = load_accounts()
    entry = {
        "uri": uri,
        "password": password,
        "registrar": registrar or "",
        "use_tls": bool(use_tls),
        "label": label or uri,
    }
    # Avoid duplicate URIs
    accounts = [a for a in accounts if a.get("uri") != uri]
    accounts.append(entry)
    save_accounts(accounts)
    return entry


def update_account(old_uri, uri, password, registrar=None, use_tls=True, label=None):
    """Update an existing account identified by old_uri. Replaces with new data."""
    accounts = load_accounts()
    entry = {
        "uri": uri,
        "password": password,
        "registrar": registrar or "",
        "use_tls": bool(use_tls),
        "label": label or uri,
    }
    found = False
    for i, a in enumerate(accounts):
        if a.get("uri") == old_uri:
            accounts[i] = entry
            found = True
            break
    if not found:
        accounts.append(entry)
    save_accounts(accounts)
    return entry


def remove_account(uri):
    """Remove account by URI."""
    accounts = [a for a in load_accounts() if a.get("uri") != uri]
    save_accounts(accounts)


def get_account(uri):
    """Get one account by URI."""
    for a in load_accounts():
        if a.get("uri") == uri:
            return a
    return None


def get_last_account_uri():
    """Return the URI of the last selected account, or None."""
    if not os.path.isfile(PREFS_FILE):
        return None
    try:
        with open(PREFS_FILE, "r") as f:
            data = json.load(f)
        return data.get("last_account_uri")
    except (json.JSONDecodeError, IOError):
        return None


def set_last_account_uri(uri):
    """Remember the given account URI as last used."""
    _ensure_config_dir()
    data = {}
    if os.path.isfile(PREFS_FILE):
        try:
            with open(PREFS_FILE, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    data["last_account_uri"] = uri
    with open(PREFS_FILE, "w") as f:
        json.dump(data, f, indent=2)
