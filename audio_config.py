"""Persist selected audio device IDs."""

import json
import os

from accounts import CONFIG_DIR

AUDIO_FILE = os.path.join(CONFIG_DIR, "audio.json")


def load_audio_settings():
    """Return dict with capture_dev_id, playback_dev_id (int or None), debug_log (bool)."""
    if not os.path.isfile(AUDIO_FILE):
        return {}
    try:
        with open(AUDIO_FILE, "r") as f:
            data = json.load(f)
        out = {}
        if "capture_dev_id" in data:
            try:
                out["capture_dev_id"] = int(data["capture_dev_id"])
            except (TypeError, ValueError):
                pass
        if "playback_dev_id" in data:
            try:
                out["playback_dev_id"] = int(data["playback_dev_id"])
            except (TypeError, ValueError):
                pass
        if "debug_log" in data:
            out["debug_log"] = bool(data["debug_log"])
        return out
    except (json.JSONDecodeError, IOError):
        return {}


def save_audio_settings(capture_dev_id=None, playback_dev_id=None, debug_log=None):
    """Save device IDs and optional debug_log. Pass None to leave unchanged."""
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    data = load_audio_settings()
    if capture_dev_id is not None:
        data["capture_dev_id"] = capture_dev_id
    if playback_dev_id is not None:
        data["playback_dev_id"] = playback_dev_id
    if debug_log is not None:
        data["debug_log"] = bool(debug_log)
    with open(AUDIO_FILE, "w") as f:
        json.dump(data, f, indent=2)
