"""
BLF (Busy Lamp Field): dialog-event subscription and state reporting.

- Engine creates BLFBuddyHandler per BLF URI and calls set_blf(entries).
- On NOTIFY, callback enqueues (buddy_id, uri, timestamp) or (buddy_id, uri, timestamp, state_str).
- Worker calls process_blf_pending(engine); after BLF_DELAY_SEC we notify the GUI.
- pjsua C API (get_blf_dialog_state) segfaults when the remote party hangs up; BLF_USE_C_API is False.
- If the callback param exposes the NOTIFY body, we parse <state> from it so the lamp can show busy/idle.
"""

import ctypes
import re
import sys
import time

try:
    import pjsua2 as pj
except ImportError:
    pj = None

BLF_DELAY_SEC = 0.5
# C API (pjsua_buddy_get_dlg_event_info) segfaults when the subscribed extension hangs up; keep False.
BLF_USE_C_API = False
# One-time debug: log what the NOTIFY callback param contains (set True to diagnose why state is always "—").
BLF_DEBUG_PRM = False

# C API: read dialog state from pjsua (only call from worker, after delay)
_get_blf_lib = None


def get_blf_dialog_state(buddy_id, on_log=None):
    """Return dialog state string (e.g. 'confirmed', 'terminated') or None. Safe only when called from worker after delay."""
    global _get_blf_lib
    try:
        class PjStr(ctypes.Structure):
            _fields_ = [("ptr", ctypes.c_void_p), ("slen", ctypes.c_long)]

        class PjDlgEventInfo(ctypes.Structure):
            _fields_ = [
                ("id", ctypes.c_int),
                ("uri", PjStr), ("dialog_id", PjStr), ("dialog_info_state", PjStr),
                ("dialog_info_entity", PjStr), ("dialog_call_id", PjStr),
                ("dialog_remote_tag", PjStr), ("dialog_local_tag", PjStr),
                ("dialog_direction", PjStr), ("dialog_state", PjStr),
                ("dialog_duration", PjStr), ("local_identity", PjStr),
                ("local_identity_display", PjStr), ("local_target_uri", PjStr),
                ("remote_identity", PjStr), ("remote_identity_display", PjStr),
                ("remote_target_uri", PjStr),
                ("sub_state", ctypes.c_int), ("sub_state_name", ctypes.c_void_p),
                ("sub_term_code", ctypes.c_uint), ("sub_term_reason", PjStr),
                ("buf_", (ctypes.c_char * 1024)),
            ]

        if _get_blf_lib is None:
            for name in ("libpjsua.so.2", "libpjsua.so", "libpjsua2.so"):
                try:
                    lib = ctypes.CDLL(name)
                    if hasattr(lib, "pjsua_buddy_get_dlg_event_info"):
                        _get_blf_lib = lib
                        break
                except OSError:
                    continue
            if _get_blf_lib is None and on_log:
                on_log("BLF: libpjsua not found; lamp may not update")

        if _get_blf_lib is None:
            return None

        lib = _get_blf_lib
        lib.pjsua_buddy_get_dlg_event_info.argtypes = [ctypes.c_int, ctypes.POINTER(PjDlgEventInfo)]
        lib.pjsua_buddy_get_dlg_event_info.restype = ctypes.c_int
        info = PjDlgEventInfo()
        if lib.pjsua_buddy_get_dlg_event_info(int(buddy_id), ctypes.byref(info)) != 0:
            return None

        def str_from(s):
            if not s.ptr or s.slen <= 0:
                return ""
            return ctypes.string_at(s.ptr, s.slen).decode("utf-8", errors="replace").strip()

        state = str_from(info.dialog_state)
        if not state:
            state = str_from(info.dialog_info_state)
        return state or None
    except Exception:
        return None


def _get_whole_msg_from_obj(obj):
    """Try to get wholeMsg/whole_msg from rdata-like object. Returns string or None."""
    if obj is None:
        return None
    for name in ("wholeMsg", "whole_msg"):
        v = getattr(obj, name, None)
        if isinstance(v, str):
            return v
    return None


def _parse_state_from_body(prm):
    """
    Extract <state>...</state> from the NOTIFY that triggered the callback.
    OnBuddyEvSubStateParam has SipEvent e; e.body.rxMsg.rdata.wholeMsg is the full NOTIFY (headers + body).
    Pygui uses getInfo() after onBuddyState() for presence; we need NOTIFY body for dialog state (BLF).
    """
    if prm is None:
        return None
    body = None
    # Try prm.e or prm.event (SipEvent), then body.rxMsg/rx_msg, then rdata.wholeMsg/whole_msg
    for ev_attr in ("e", "event"):
        e = getattr(prm, ev_attr, None)
        if e is None:
            continue
        try:
            ev_body = getattr(e, "body", None)
            if ev_body is None:
                continue
            for rx_attr in ("rxMsg", "rx_msg"):
                rx_msg = getattr(ev_body, rx_attr, None)
                if rx_msg is None:
                    continue
                rdata = getattr(rx_msg, "rdata", None)
                if rdata is not None:
                    whole = _get_whole_msg_from_obj(rdata)
                    if whole and "<state>" in whole:
                        body = whole
                        break
            if body:
                break
        except Exception:
            pass
        if body:
            break
    # Try prm as SipEvent (some bindings may pass event directly)
    if not body:
        try:
            ev_body = getattr(prm, "body", None)
            if ev_body is not None:
                for rx_attr in ("rxMsg", "rx_msg"):
                    rx_msg = getattr(ev_body, rx_attr, None)
                    if rx_msg is not None:
                        rdata = getattr(rx_msg, "rdata", None)
                        if rdata is not None:
                            whole = _get_whole_msg_from_obj(rdata)
                            if whole and "<state>" in whole:
                                body = whole
                                break
        except Exception:
            pass
    if not body:
        for attr in ("body", "content", "reason", "msgBody", "sipBody", "data"):
            try:
                v = getattr(prm, attr, None)
                if isinstance(v, str) and "<state>" in v:
                    body = v
                    break
                if hasattr(v, "decode"):
                    v = v.decode("utf-8", errors="replace")
                if isinstance(v, str) and "<state>" in v:
                    body = v
                    break
            except Exception:
                continue
    if not body:
        return None
    m = re.search(r"<state>\s*([^<]+)\s*</state>", body, re.IGNORECASE)
    return m.group(1).strip() if m else None


_blf_debug_done = False


def _blf_debug_prm(prm, engine, parsed_state):
    """One-time log of prm structure to see how to get NOTIFY body in this pjsua2 build."""
    global _blf_debug_done
    if _blf_debug_done or not prm or not getattr(engine, "on_log", None):
        return
    _blf_debug_done = True
    try:
        attrs = []
        for name in dir(prm):
            if name.startswith("_"):
                continue
            try:
                v = getattr(prm, name)
                t = type(v).__name__
                if isinstance(v, (str, bytes)):
                    snippet = (v[:80] + "..") if len(v) > 80 else v
                    attrs.append("%s=%s(%r)" % (name, t, snippet))
                else:
                    attrs.append("%s=%s" % (name, t))
            except Exception:
                attrs.append("%s=?" % name)
        engine.on_log("BLF debug: prm attrs: %s" % " ".join(attrs))
        engine.on_log("BLF debug: parsed_state=%s" % (parsed_state,))
    except Exception as e:
        try:
            engine.on_log("BLF debug error: %s" % (e,))
        except Exception:
            pass


def normalize_blf_state(state_str):
    """Map subscription/document states to display placeholder; pass through real dialog states."""
    s = (state_str or "").strip()
    if s in ("?", "Active", "Pending", "", "full", "partial"):
        return "—"
    return s or "—"


# Fallback: parse last NOTIFY dialog-info from sip_debug.log when callback doesn't expose body
_SIP_LOG_TAIL_BYTES = 80 * 1024
# Periodically re-read log so we pick up "terminated" even if callback didn't fire or log was delayed
BLF_LOG_REFRESH_INTERVAL = 2.0
_last_log_refresh = 0.0


def _get_state_from_sip_log(sip_log_path, entity_uri):
    """
    Read the tail of the SIP debug log and return the most recent <state> for the given entity.
    entity_uri can be full (sip:998@nyoph.fractalcts.com) or short (998); XML has entity="sip:998@...".
    Returns state string (e.g. 'confirmed', 'terminated') or None.
    """
    if not sip_log_path or not entity_uri:
        return None
    try:
        with open(sip_log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - _SIP_LOG_TAIL_BYTES)
            f.seek(start)
            tail = f.read()
    except Exception:
        return None
    # Last NOTIFY for this entity: match entity="..." or entity='...' containing this URI
    # (config may store "998" but XML has entity="sip:998@nyoph.fractalcts.com")
    pos = -1
    # Exact match first (full URI)
    entity_esc = re.escape(entity_uri)
    for pattern in [r'entity="' + entity_esc + r'"', r"entity='" + entity_esc + r"'"]:
        for m in re.finditer(pattern, tail):
            if m.start() > pos:
                pos = m.start()
    # If no match, entity_uri may be short (e.g. "998"); match any entity= containing it
    if pos < 0 and entity_uri:
        key = entity_uri.replace("sip:", "").replace("sips:", "").strip()
        if key:
            for pattern in [r'entity="([^"]*)"', r"entity='([^']*)'"]:
                for m in re.finditer(pattern, tail):
                    if key in m.group(1) and m.start() > pos:
                        pos = m.start()
    if pos < 0:
        return None
    fragment = tail[pos:]
    state_m = re.search(r"<state>\s*([^<]+)\s*</state>", fragment, re.IGNORECASE)
    return state_m.group(1).strip() if state_m else None


def process_blf_pending(engine):
    """
    Process BLF queue: after BLF_DELAY_SEC, read state via C API and call engine.on_blf_state.
    Call from the worker thread only (after libHandleEvents). Dedupes by URI (keeps latest).
    """
    if not getattr(engine, "on_blf_state", None):
        return
    try:
        with engine._blf_pending_lock:
            pending = engine._blf_pending_refresh
            engine._blf_pending_refresh = []
    except Exception:
        return

    now = time.time()
    ready = [p for p in pending if (now - p[2]) >= BLF_DELAY_SEC]
    not_ready = [p for p in pending if (now - p[2]) < BLF_DELAY_SEC]
    with engine._blf_pending_lock:
        engine._blf_pending_refresh.extend(not_ready)

    # One update per URI (latest); entries are (bid, uri, ts) or (bid, uri, ts, state_str)
    by_uri = {}
    for p in ready:
        bid, uri, ts = p[0], p[1], p[2]
        state_opt = p[3] if len(p) >= 4 else None
        by_uri[uri] = (bid, state_opt)

    sip_log_path = getattr(engine, "_sip_log_path", None)
    for uri, (bid, state_opt) in by_uri.items():
        try:
            if state_opt is not None:
                state_str = normalize_blf_state(state_opt)
            elif BLF_USE_C_API:
                state_str = get_blf_dialog_state(bid, getattr(engine, "on_log", None))
                state_str = normalize_blf_state(state_str)
            else:
                # Callback param doesn't expose NOTIFY body in this build; use sip_debug.log
                state_str = _get_state_from_sip_log(sip_log_path, uri)
                state_str = normalize_blf_state(state_str) if state_str else "—"
            if engine.on_log:
                try:
                    engine.on_log("BLF state %s: %s" % (uri or "?", state_str))
                except Exception:
                    pass
            engine.on_blf_state(uri, state_str)
        except Exception:
            pass


def refresh_blf_from_log(engine):
    """
    Re-read sip_debug.log for all BLF URIs and push latest state (e.g. terminated after call ends).
    Called from worker every loop; only does log read every BLF_LOG_REFRESH_INTERVAL seconds.
    """
    global _last_log_refresh
    if not getattr(engine, "on_blf_state", None):
        return
    now = time.time()
    if now - _last_log_refresh < BLF_LOG_REFRESH_INTERVAL:
        return
    _last_log_refresh = now
    sip_log_path = getattr(engine, "_sip_log_path", None)
    if not sip_log_path:
        return
    buddies = getattr(engine, "_blf_buddies", None)
    if not buddies:
        return
    try:
        with engine._blf_pending_lock:
            uris = [getattr(b, "_blf_uri", None) for b in buddies]
    except Exception:
        return
    for uri in uris:
        if not uri:
            continue
        try:
            state_str = _get_state_from_sip_log(sip_log_path, uri)
            if state_str is not None:
                state_str = normalize_blf_state(state_str)
                engine.on_blf_state(uri, state_str)
        except Exception:
            pass


def _buddy_base():
    return pj.Buddy if pj else object


class BLFBuddyHandler(_buddy_base()):
    """Pjsua Buddy for one BLF URI. On NOTIFY, enqueues (buddy_id, uri, time) for process_blf_pending."""

    def __init__(self, engine=None, blf_uri=""):
        if pj:
            pj.Buddy.__init__(self)
        self._engine = engine
        self._blf_uri = blf_uri

    def onBuddyDlgEventState(self):
        pass

    def onBuddyEvSubDlgEventState(self, prm):
        try:
            if not self._engine:
                return
            bid = self.getId()
            if bid is None or bid < 0:
                return
            state_str = _parse_state_from_body(prm)
            msg = "BLF: NOTIFY callback for %s (state from body: %s)" % (
                self._blf_uri, state_str if state_str else "—")
            if getattr(self._engine, "on_log", None):
                try:
                    self._engine.on_log(msg)
                except Exception:
                    pass
            sys.stderr.write("[BLF] %s\n" % msg)
            if BLF_DEBUG_PRM:
                _blf_debug_prm(prm, self._engine, state_str)
            entry = (int(bid), self._blf_uri, time.time()) if state_str is None else (int(bid), self._blf_uri, time.time(), state_str)
            with self._engine._blf_pending_lock:
                self._engine._blf_pending_refresh.append(entry)
        except Exception as e:
            sys.stderr.write("BLF callback error: %s\n" % (e,))


def pjsua_available():
    return pj is not None
