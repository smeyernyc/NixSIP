"""Compact GTK UI: account switch, dialpad, mute, end, answer."""

import os
import time
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, Gio, Pango
try:
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
except (ValueError, ImportError):
    GdkPixbuf = None

# Optional: system tray (Linux AppIndicator)
try:
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3
    _HAS_APP_INDICATOR = True
except (ValueError, ImportError):
    _HAS_APP_INDICATOR = False

try:
    import cairo
except ImportError:
    cairo = None

from accounts import load_accounts, save_accounts, account_label, add_account, update_account, CONFIG_DIR
from audio_config import load_audio_settings, save_audio_settings
from sip_engine import SipEngine, pjsua2_available
from speeddials_blf import load_speeddials, save_speeddials, load_blf, save_blf, set_blf_debug_log
from call_history import load_history, add_entry


def _glib_idle(f):
    def w(*a, **k):
        GLib.idle_add(lambda: f(*a, **k))
    return w


def _parse_uri_to_user_domain(uri):
    """Parse sip:user@domain or sips:user@domain into (user_id, domain)."""
    if not uri:
        return "", ""
    s = uri.strip()
    for prefix in ("sips://", "sip://", "sips:", "sip:"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if "@" in s:
        return s.split("@", 1)[0].strip(), s.split("@", 1)[1].strip()
    return s.strip(), ""


class AccountDialog(Gtk.Dialog):
    """Add/edit SIP account."""

    def __init__(self, parent, existing=None):
        title = "Edit account" if existing else "Add account"
        super().__init__(title=title, transient_for=parent, modal=True)
        self.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK)
        self.set_default_size(360, 260)
        box = self.get_content_area()
        grid = Gtk.Grid(column_spacing=8, row_spacing=8, margin=12)
        box.add(grid)
        self.entries = {}
        user_id, domain = _parse_uri_to_user_domain(existing.get("uri") if existing else None)
        row = 0
        for label_text, key, placeholder in [
            ("Display Name", "display_name", "My Phone"),
            ("User ID", "user_id", "1001"),
            ("Domain", "domain", "sip.example.com"),
            ("Password", "password", ""),
        ]:
            l = Gtk.Label(label=label_text + ":", xalign=0)
            grid.attach(l, 0, row, 1, 1)
            e = Gtk.Entry()
            e.set_placeholder_text(placeholder)
            if key == "password":
                e.set_visibility(False)
            if existing:
                if key == "display_name":
                    e.set_text(str(existing.get("label") or ""))
                elif key == "user_id":
                    e.set_text(user_id)
                elif key == "domain":
                    e.set_text(domain)
                elif key == "password":
                    e.set_text(str(existing.get("password") or ""))
            grid.attach(e, 1, row, 1, 1)
            self.entries[key] = e
            row += 1
        cb = Gtk.CheckButton(label="Use TLS (SIPS)")
        cb.set_active(True if not existing else existing.get("use_tls", True))
        grid.attach(cb, 1, row, 1, 1)
        self.entries["use_tls"] = cb
        self.show_all()

    def get_values(self):
        display_name = self.entries["display_name"].get_text().strip()
        user_id = self.entries["user_id"].get_text().strip()
        domain = self.entries["domain"].get_text().strip()
        password = self.entries["password"].get_text()
        use_tls = self.entries["use_tls"].get_active()
        if not user_id or not domain:
            return None
        scheme = "sips" if use_tls else "sip"
        uri = "%s:%s@%s" % (scheme, user_id, domain)
        return {
            "label": display_name or uri,
            "uri": uri,
            "password": password,
            "registrar": None,  # derived from domain in engine
            "use_tls": use_tls,
        }


class AudioOptionsDialog(Gtk.Dialog):
    """Audio device selection and speaker/mic test."""

    def __init__(self, parent, engine, on_log):
        super().__init__(title="Audio options", transient_for=parent, modal=False)
        self.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        self.set_default_size(380, 280)
        self._engine = engine
        self._on_log = on_log
        box = self.get_content_area()
        grid = Gtk.Grid(column_spacing=8, row_spacing=8, margin=12)
        box.add(grid)
        row = 0
        grid.attach(Gtk.Label(label="Speaker (playback):", xalign=0), 0, row, 1, 1)
        self._playback_combo = Gtk.ComboBoxText()
        grid.attach(self._playback_combo, 1, row, 1, 1)
        row += 1
        grid.attach(Gtk.Label(label="Microphone (capture):", xalign=0), 0, row, 1, 1)
        self._capture_combo = Gtk.ComboBoxText()
        grid.attach(self._capture_combo, 1, row, 1, 1)
        row += 1
        btn_row = Gtk.Box(spacing=8)
        self._btn_test_speaker = Gtk.Button(label="Test speaker")
        self._btn_test_speaker.connect("clicked", self._on_test_speaker)
        btn_row.pack_start(self._btn_test_speaker, False, False, 0)
        self._btn_test_mic = Gtk.Button(label="Test mic")
        self._btn_test_mic.connect("clicked", self._on_test_mic)
        btn_row.pack_start(self._btn_test_mic, False, False, 0)
        grid.attach(btn_row, 1, row, 1, 1)
        row += 1
        self._test_status = Gtk.Label(label="", xalign=0, wrap=True)
        grid.attach(self._test_status, 0, row, 2, 1)
        self._playback_devs = []
        self._capture_devs = []
        self._fill_devices()
        self.connect("response", self._on_response)
        self.show_all()

    def _on_response(self, d, response_id):
        # Save current selection when closing (Apply on Close)
        self._apply_devices()
        d.destroy()

    def _fill_devices(self):
        self._playback_devs, self._capture_devs = self._engine.get_audio_devices() if self._engine else ([], [])
        saved = load_audio_settings()
        self._playback_combo.remove_all()
        self._capture_combo.remove_all()
        for _id, name in self._playback_devs:
            self._playback_combo.append_text(name[:60])
        for _id, name in self._capture_devs:
            self._capture_combo.append_text(name[:60])
        playback_id = saved.get("playback_dev_id")
        capture_id = saved.get("capture_dev_id")
        for i, (dev_id, _) in enumerate(self._playback_devs):
            if dev_id == playback_id:
                self._playback_combo.set_active(i)
                break
        else:
            if self._playback_devs:
                self._playback_combo.set_active(0)
        for i, (dev_id, _) in enumerate(self._capture_devs):
            if dev_id == capture_id:
                self._capture_combo.set_active(i)
                break
        else:
            if self._capture_devs:
                self._capture_combo.set_active(0)

    def _on_test_speaker(self, btn):
        self._apply_devices()
        self._test_status.set_text("Playing test tone (3 s)...")
        self._btn_test_speaker.set_sensitive(False)
        def done():
            GLib.idle_add(lambda: self._test_status.set_text("Speaker test finished."))
            GLib.idle_add(lambda: self._btn_test_speaker.set_sensitive(True))
        self._engine.speaker_test(3, done)
        self._on_log("Speaker test started")

    def _on_test_mic(self, btn):
        self._apply_devices()
        self._test_status.set_text("Listening to mic (3 s)...")
        self._btn_test_mic.set_sensitive(False)
        def done():
            GLib.idle_add(lambda: self._test_status.set_text("Mic test finished."))
            GLib.idle_add(lambda: self._btn_test_mic.set_sensitive(True))
        self._engine.mic_test(3, done)
        self._on_log("Mic test started")

    def _apply_devices(self):
        playback_id = None
        capture_id = None
        i = self._playback_combo.get_active()
        if i >= 0 and i < len(self._playback_devs):
            playback_id = self._playback_devs[i][0]
            if self._engine.set_playback_dev(playback_id):
                self._on_log("Playback device set to %s" % playback_id)
        i = self._capture_combo.get_active()
        if i >= 0 and i < len(self._capture_devs):
            capture_id = self._capture_devs[i][0]
            if self._engine.set_capture_dev(capture_id):
                self._on_log("Capture device set to %s" % capture_id)
        if playback_id is not None or capture_id is not None:
            save_audio_settings(playback_dev_id=playback_id, capture_dev_id=capture_id)
            self._on_log("Audio devices saved")


class SpeedDialsBLFDialog(Gtk.Dialog):
    """Edit speed dials and BLF entries for one account."""

    def __init__(self, parent, on_saved, on_log=None, account_uri=None):
        super().__init__(title="Speed dials & BLF", transient_for=parent, modal=True)
        self.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        self.set_default_size(420, 380)
        self._on_saved = on_saved
        self._on_log = on_log
        self._account_uri = account_uri
        box = self.get_content_area()
        notebook = Gtk.Notebook()
        # Speed dials tab
        sd_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sd_toolbar = Gtk.Box(spacing=4)
        btn_add_sd = Gtk.Button(label="Add speed dial")
        btn_add_sd.connect("clicked", self._add_speeddial)
        sd_toolbar.pack_start(btn_add_sd, False, False, 0)
        btn_remove_sd = Gtk.Button(label="Remove")
        btn_remove_sd.connect("clicked", self._remove_speeddial)
        sd_toolbar.pack_start(btn_remove_sd, False, False, 0)
        sd_box.pack_start(sd_toolbar, False, False, 0)
        self._sd_list = Gtk.ListBox()
        self._sd_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        sw_sd = Gtk.ScrolledWindow()
        sw_sd.set_min_content_height(120)
        sw_sd.add(self._sd_list)
        sd_box.pack_start(sw_sd, True, True, 0)
        notebook.append_page(sd_box, Gtk.Label(label="Speed dials"))
        # BLF tab
        blf_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        blf_toolbar = Gtk.Box(spacing=4)
        btn_add_blf = Gtk.Button(label="Add BLF")
        btn_add_blf.connect("clicked", self._add_blf)
        blf_toolbar.pack_start(btn_add_blf, False, False, 0)
        btn_remove_blf = Gtk.Button(label="Remove")
        btn_remove_blf.connect("clicked", self._remove_blf)
        blf_toolbar.pack_start(btn_remove_blf, False, False, 0)
        blf_box.pack_start(blf_toolbar, False, False, 0)
        self._blf_list = Gtk.ListBox()
        self._blf_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        sw_blf = Gtk.ScrolledWindow()
        sw_blf.set_min_content_height(120)
        sw_blf.add(self._blf_list)
        blf_box.pack_start(sw_blf, True, True, 0)
        notebook.append_page(blf_box, Gtk.Label(label="BLF"))
        box.pack_start(notebook, True, True, 0)
        self.connect("response", self._on_response)
        self._fill_speeddials()
        self._fill_blf()
        self.show_all()

    def _fill_speeddials(self):
        for w in self._sd_list.get_children():
            self._sd_list.remove(w)
        for e in load_speeddials(self._account_uri):
            row = Gtk.Box(spacing=8)
            row.entry_data = e
            row.pack_start(Gtk.Label(label=e.get("label", "")[:20], xalign=0), False, False, 0)
            row.pack_start(Gtk.Label(label=e.get("number", ""), xalign=0), True, True, 0)
            self._sd_list.add(row)
        self._sd_list.show_all()

    def _fill_blf(self):
        for w in self._blf_list.get_children():
            self._blf_list.remove(w)
        for e in load_blf(self._account_uri):
            row = Gtk.Box(spacing=8)
            row.entry_data = e
            row.pack_start(Gtk.Label(label=e.get("label", "")[:20], xalign=0), False, False, 0)
            row.pack_start(Gtk.Label(label=e.get("uri", ""), xalign=0), True, True, 0)
            self._blf_list.add(row)
        self._blf_list.show_all()

    def _add_speeddial(self, btn):
        d = Gtk.Dialog(title="Add speed dial", transient_for=self, modal=True)
        d.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK)
        d.set_default_size(280, 120)
        b = d.get_content_area()
        b.set_spacing(8)
        b.add(Gtk.Label(label="Label:"))
        entry_label = Gtk.Entry()
        entry_label.set_placeholder_text("e.g. Mom")
        b.add(entry_label)
        b.add(Gtk.Label(label="Number or URI:"))
        entry_number = Gtk.Entry()
        entry_number.set_placeholder_text("e.g. 5551234 or sip:101@domain")
        b.add(entry_number)
        d.show_all()
        if d.run() != Gtk.ResponseType.OK:
            d.destroy()
            return
        label = entry_label.get_text().strip() or "Speed dial"
        number = entry_number.get_text().strip()
        d.destroy()
        if not number:
            return
        entries = load_speeddials(self._account_uri)
        entries.append({"label": label, "number": number})
        save_speeddials(entries, self._account_uri)
        self._fill_speeddials()

    def _remove_speeddial(self, btn):
        row = self._sd_list.get_selected_row()
        if not row or not hasattr(row.get_child(), "entry_data"):
            return
        e = row.get_child().entry_data
        entries = [x for x in load_speeddials(self._account_uri) if (x.get("label"), x.get("number")) != (e.get("label"), e.get("number"))]
        save_speeddials(entries, self._account_uri)
        self._fill_speeddials()

    def _add_blf(self, btn):
        d = Gtk.Dialog(title="Add BLF", transient_for=self, modal=True)
        d.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK)
        d.set_default_size(360, 160)
        b = d.get_content_area()
        b.set_spacing(8)
        b.add(Gtk.Label(label="Label:"))
        entry_label = Gtk.Entry()
        entry_label.set_placeholder_text("e.g. Ext 100")
        b.add(entry_label)
        b.add(Gtk.Label(label="SIP URI (extension@your-server):"))
        entry_uri = Gtk.Entry()
        entry_uri.set_placeholder_text("sip:100@pbx.example.com  or  100@pbx.example.com")
        entry_uri.set_tooltip_text(
            "Full SIP address to monitor. Use the same domain as your account.\n"
            "Examples: sip:100@pbx.example.com  or just 100@pbx.example.com")
        b.add(entry_uri)
        hint = Gtk.Label(label="Use your account's domain (e.g. sip:100@your-pbx.domain)")
        hint.get_style_context().add_class("dim-label")
        hint.set_line_wrap(True)
        hint.set_xalign(0)
        b.add(hint)
        d.show_all()
        if d.run() != Gtk.ResponseType.OK:
            d.destroy()
            return
        label = entry_label.get_text().strip() or "BLF"
        uri = entry_uri.get_text().strip()
        d.destroy()
        if not uri:
            return
        if not uri.startswith("sip"):
            uri = "sip:" + uri
        entries = load_blf(self._account_uri)
        entries.append({"label": label, "uri": uri})
        save_blf(entries, self._account_uri)
        if self._on_log:
            self._on_log("BLF: added %s (%s)" % (label, uri))
        self._fill_blf()

    def _remove_blf(self, btn):
        row = self._blf_list.get_selected_row()
        if not row or not hasattr(row.get_child(), "entry_data"):
            return
        e = row.get_child().entry_data
        entries = [x for x in load_blf(self._account_uri) if (x.get("label"), x.get("uri")) != (e.get("label"), e.get("uri"))]
        save_blf(entries, self._account_uri)
        if self._on_log:
            self._on_log("BLF: removed %s (%s)" % (e.get("label") or "?", e.get("uri") or "?"))
        self._fill_blf()

    def _on_response(self, w, resp):
        if self._on_saved:
            self._on_saved()
        self.destroy()


class CallHistoryDialog(Gtk.Dialog):
    """Scrollable list of last 20 call numbers (in/out); click to call."""

    def __init__(self, parent, on_call):
        super().__init__(title="Call history", transient_for=parent, modal=False)
        self.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        self.set_default_size(340, 320)
        self._on_call = on_call
        box = self.get_content_area()
        sw = Gtk.ScrolledWindow()
        sw.set_min_content_height(200)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list.set_tooltip_text("Click a row to call")
        self._list.connect("row-activated", self._on_row_activated)
        sw.add(self._list)
        box.pack_start(sw, True, True, 0)
        self.connect("response", lambda w, r: self.destroy())
        self._fill()
        self.show_all()

    def _fill(self):
        for w in self._list.get_children():
            self._list.remove(w)
        entries = load_history()
        if not entries:
            row = Gtk.Box(spacing=8)
            row.entry_data = None
            row.pack_start(Gtk.Label(label="No call history", xalign=0), True, True, 0)
            self._list.add(row)
        else:
            for e in entries:
                uri = e.get("uri") or "?"
                direction = e.get("direction") or "out"
                label = (uri.replace("sips:", "").replace("sip:", "").strip())[:32]
                row = Gtk.Box(spacing=8)
                row.entry_data = e
                dir_lbl = Gtk.Label(label="↓" if direction == "in" else "↑", xalign=0)
                dir_lbl.set_tooltip_text("Incoming" if direction == "in" else "Outgoing")
                row.pack_start(dir_lbl, False, False, 0)
                row.pack_start(Gtk.Label(label=label, xalign=0), True, True, 0)
                self._list.add(row)
        self._list.show_all()

    def _on_row_activated(self, listbox, row):
        if not row or not getattr(row, "entry_data", None):
            return
        uri = row.entry_data.get("uri") if row.entry_data else None
        if uri and self._on_call:
            self._on_call(uri)


def _app_icon_path():
    """Path to nixsip.png next to the script or in icons/."""
    base = os.path.dirname(os.path.abspath(__file__))
    for name in ("icons/nixsip.png", "nixsip.png"):
        path = os.path.join(base, name)
        if os.path.isfile(path):
            return path
    return None


def _app_icon_pixbufs():
    """Load app icon at standard sizes for taskbar/window list (many WMs need small sizes)."""
    if not GdkPixbuf:
        return None
    path = _app_icon_path()
    if not path:
        return None
    try:
        pbs = []
        for size in (16, 22, 24, 32, 48, 64):
            pb = GdkPixbuf.Pixbuf.new_from_file(path)
            scaled = pb.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
            if scaled:
                pbs.append(scaled)
        return pbs if pbs else None
    except Exception:
        return None


class BLFIndicator(Gtk.DrawingArea):
    """Small dot: green=idle, green flash=ringing, red=busy."""

    SIZE = 14

    def __init__(self):
        super().__init__()
        self.set_size_request(self.SIZE, self.SIZE)
        self._state = "idle"  # idle | ringing | busy
        self._flash_on = False
        self._flash_timeout = None
        self.connect("draw", self._on_draw)

    def set_state(self, state):
        """state: 'idle', 'ringing', or 'busy'."""
        if state == self._state:
            return
        self._stop_flash()
        self._state = state
        if state == "ringing":
            self._flash_on = True
            self._flash_timeout = GLib.timeout_add(300, self._on_flash_tick)
        self.queue_draw()

    def _stop_flash(self):
        if self._flash_timeout is not None:
            GLib.source_remove(self._flash_timeout)
            self._flash_timeout = None

    def _on_flash_tick(self):
        self._flash_on = not self._flash_on
        self.queue_draw()
        return True

    def _on_draw(self, widget, cr):
        if not cairo:
            return
        w, h = self.get_allocated_width(), self.get_allocated_height()
        cx, cy = w / 2.0, h / 2.0
        r = min(w, h) / 2.0 - 1.5
        if r <= 0:
            return
        # Colors: green idle, green flash ringing, red busy
        if self._state == "busy":
            cr.set_source_rgb(0.91, 0.30, 0.24)  # red
        elif self._state == "ringing":
            if self._flash_on:
                cr.set_source_rgb(0.18, 0.80, 0.44)  # green
            else:
                cr.set_source_rgb(0.18 * 0.4, 0.80 * 0.4, 0.44 * 0.4)  # dim green
        else:
            cr.set_source_rgb(0.18, 0.80, 0.44)  # green (idle)
        cr.arc(cx, cy, r, 0, 2 * 3.14159265)
        cr.fill()


class MainWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="NixSIP 1.0")
        self.set_default_size(320, 420)
        self.set_border_width(8)
        icon_pixbufs = _app_icon_pixbufs()
        if icon_pixbufs:
            try:
                self.set_icon_list(icon_pixbufs)
            except Exception:
                pass
        if not icon_pixbufs:
            icon_path = _app_icon_path()
            if icon_path:
                try:
                    self.set_icon_from_file(icon_path)
                except Exception:
                    pass
            if not icon_path:
                try:
                    self.set_icon_name("phone")
                except Exception:
                    pass
        self._engine = None
        self._indicator = None
        self._status_icon = None  # fallback tray when AppIndicator not available
        self._current_call = None
        self._incoming_call = None
        self._call_start_time = None  # time when current call became CONFIRMED (for timer)
        self._call_stats_timeout_id = None  # GLib timeout for call stats refresh
        self._active_calls = []  # list of active (non-disconnected) calls for merge/transfer
        self._held_calls = set()  # call ids on hold
        self._attended_transfer_original = None  # for attended transfer: first call
        self._audio_dialog = None
        self._muted = False
        self._registered = False  # current account registration state
        self._accounts = load_accounts()
        self._build_ui()
        self.connect("delete-event", self._on_delete_event)
        self.connect("destroy", self._on_destroy)
        self._setup_tray()
        if not pjsua2_available():
            self._status.set_text("pjsua2 not installed — install pjproject and Python bindings")
            if hasattr(self, "_btn_audio"):
                self._btn_audio.set_sensitive(False)
            return
        self._engine = SipEngine()
        self._engine.on_reg_state = _glib_idle(self._on_reg_state)
        self._engine.on_incoming_call = _glib_idle(self._on_incoming_call)
        self._engine.on_call_state = _glib_idle(self._on_call_state)
        self._engine.on_media_active = _glib_idle(self._on_media_active)
        self._engine.on_blf_state = _glib_idle(self._on_blf_state)
        self._engine.on_log = _glib_idle(self._log)
        if not self._engine.start():
            self._status.set_text("Failed to start SIP engine")
            self._log("SIP engine failed to start")
            return
        self._log("SIP engine started")
        self._refresh_account_combo()
        # set_active(0) in _refresh_account_combo already emits "changed" and triggers _on_account_changed

    def _build_ui(self):
        # Pointer cursor for clickable BLF rows
        try:
            provider = Gtk.CssProvider()
            provider.load_from_data(b".blf-clickable { cursor: pointer; }")
            screen = Gdk.Screen.get_default()
            if screen:
                Gtk.StyleContext.add_provider_for_screen(
                    screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
        except Exception:
            pass
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add(box)
        # Menu bar
        menubar = Gtk.MenuBar()
        menu_root = Gtk.MenuItem(label="Menu")
        menubar.append(menu_root)
        submenu = Gtk.Menu()
        item_speed_blf = Gtk.MenuItem(label="Edit speed dials & BLF…")
        item_speed_blf.connect("activate", self._on_speeddials_blf)
        submenu.append(item_speed_blf)
        item_history = Gtk.MenuItem(label="Call history…")
        item_history.connect("activate", self._on_call_history)
        submenu.append(item_history)
        item_sip_log = Gtk.MenuItem(label="Open SIP debug log…")
        item_sip_log.connect("activate", self._on_open_sip_debug_log)
        submenu.append(item_sip_log)
        menu_root.set_submenu(submenu)
        box.pack_start(menubar, False, False, 0)
        # Account row
        acc_row = Gtk.Box(spacing=8)
        self._account_combo = Gtk.ComboBoxText()
        self._account_combo.connect("changed", self._on_account_changed)
        acc_row.pack_start(Gtk.Label(label="Account:"), False, False, 0)
        acc_row.pack_start(self._account_combo, True, True, 0)
        btn_edit = Gtk.Button(label="Edit")
        btn_edit.set_tooltip_text("Edit account")
        btn_edit.connect("clicked", self._on_edit_account)
        acc_row.pack_start(btn_edit, False, False, 0)
        btn_add = Gtk.Button(label="+")
        btn_add.set_tooltip_text("Add account")
        btn_add.connect("clicked", self._on_add_account)
        acc_row.pack_start(btn_add, False, False, 0)
        self._btn_audio = Gtk.Button(label="Audio")
        self._btn_audio.set_tooltip_text("Audio options and device test")
        self._btn_audio.connect("clicked", self._on_audio_options)
        acc_row.pack_start(self._btn_audio, False, False, 0)
        box.pack_start(acc_row, False, False, 0)
        # Registration indicator
        reg_row = Gtk.Box(spacing=6)
        self._reg_indicator = Gtk.Label(label="", xalign=0)
        self._reg_indicator.set_markup('<span size="small">○ Not registered</span>')
        reg_row.pack_start(self._reg_indicator, False, False, 0)
        box.pack_start(reg_row, False, False, 0)
        # Status
        self._status = Gtk.Label(label="No account", xalign=0, wrap=True)
        self._status.set_selectable(True)
        box.pack_start(self._status, False, False, 0)
        # Call timer + latency + MOS (visible during active call)
        self._call_stats_label = Gtk.Label(label="", xalign=0)
        self._call_stats_label.set_selectable(True)
        self._call_stats_label.set_size_request(-1, 24)
        box.pack_start(self._call_stats_label, False, False, 0)
        # Speed dials & BLF (edit via Menu → Edit speed dials & BLF…)
        self._speeddial_box = Gtk.Box(spacing=4)
        box.pack_start(self._speeddial_box, False, False, 0)
        self._blf_box = Gtk.Box(spacing=8)
        box.pack_start(self._blf_box, False, False, 0)
        self._refresh_speeddials_blf()
        # Dial entry + Call / Hangup
        dial_row = Gtk.Box(spacing=8)
        self._dial_entry = Gtk.Entry()
        self._dial_entry.set_placeholder_text("Number or sip:user@host")
        self._dial_entry.connect("activate", lambda e: self._do_call())
        dial_row.pack_start(self._dial_entry, True, True, 0)
        self._btn_call = Gtk.Button(label="Call")
        self._btn_call.connect("clicked", lambda b: self._do_call())
        dial_row.pack_start(self._btn_call, False, False, 0)
        box.pack_start(dial_row, False, False, 0)
        self._btn_hangup = Gtk.Button(label="Hang up")
        self._btn_hangup.connect("clicked", self._on_hangup)
        self._btn_hangup.set_sensitive(False)
        box.pack_start(self._btn_hangup, False, False, 0)
        # Answer / Reject (for incoming)
        inc_row = Gtk.Box(spacing=8)
        self._btn_answer = Gtk.Button(label="Answer")
        self._btn_answer.connect("clicked", self._on_answer)
        self._btn_answer.set_sensitive(False)
        self._btn_reject = Gtk.Button(label="Reject")
        self._btn_reject.connect("clicked", self._on_reject)
        self._btn_reject.set_sensitive(False)
        inc_row.pack_start(self._btn_answer, True, True, 0)
        inc_row.pack_start(self._btn_reject, True, True, 0)
        box.pack_start(inc_row, False, False, 0)
        # Mute
        self._btn_mute = Gtk.ToggleButton(label="Mute")
        self._btn_mute.connect("toggled", self._on_mute_toggled)
        self._btn_mute.set_sensitive(False)
        box.pack_start(self._btn_mute, False, False, 0)
        # Hold / Transfer / Merge
        ctrl_row = Gtk.Box(spacing=8)
        self._btn_hold = Gtk.ToggleButton(label="Hold")
        self._btn_hold.connect("toggled", self._on_hold_toggled)
        self._btn_hold.set_sensitive(False)
        ctrl_row.pack_start(self._btn_hold, False, False, 0)
        self._btn_transfer = Gtk.Button(label="Transfer")
        self._btn_transfer.connect("clicked", self._on_transfer)
        self._btn_transfer.set_sensitive(False)
        ctrl_row.pack_start(self._btn_transfer, False, False, 0)
        self._btn_merge = Gtk.Button(label="Merge")
        self._btn_merge.connect("clicked", self._on_merge)
        self._btn_merge.set_sensitive(False)
        ctrl_row.pack_start(self._btn_merge, False, False, 0)
        self._btn_complete_transfer = Gtk.Button(label="Complete transfer")
        self._btn_complete_transfer.connect("clicked", self._on_complete_transfer)
        self._btn_complete_transfer.set_sensitive(False)
        ctrl_row.pack_start(self._btn_complete_transfer, False, False, 0)
        box.pack_start(ctrl_row, False, False, 0)
        # Dialpad: scale to width, keep 3:4 aspect ratio (3 columns, 4 rows)
        dialpad = Gtk.Grid(column_spacing=4, row_spacing=4)
        keys = [
            ("1", "2", "3"),
            ("4", "5", "6"),
            ("7", "8", "9"),
            ("*", "0", "#"),
        ]
        for r, row in enumerate(keys):
            for c, key in enumerate(row):
                b = Gtk.Button(label=key)
                b.set_hexpand(True)
                b.set_vexpand(True)
                b.set_halign(Gtk.Align.FILL)
                b.set_valign(Gtk.Align.FILL)
                b.connect("clicked", self._on_dialpad_key, key)
                dialpad.attach(b, c, r, 1, 1)
        dialpad.set_hexpand(True)
        dialpad.set_vexpand(True)
        dialpad.set_halign(Gtk.Align.FILL)
        dialpad.set_valign(Gtk.Align.FILL)
        aspect = Gtk.AspectFrame(ratio=3.0 / 4.0, obey_child=False)  # width/height = 3/4
        aspect.add(dialpad)
        box.pack_start(aspect, True, True, 0)
        # Debug log at bottom (optional; when off, panel hidden and messages discarded)
        settings = load_audio_settings()
        self._debug_log = settings.get("debug_log", True)
        log_frame = Gtk.Frame(label=" Debug log ")
        self._log_frame = log_frame
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        log_toolbar = Gtk.Box(spacing=4)
        self._cb_debug = Gtk.CheckButton(label="Show debug log")
        self._cb_debug.set_active(self._debug_log)
        self._cb_debug.connect("toggled", self._on_debug_toggled)
        log_toolbar.pack_start(self._cb_debug, False, False, 0)
        btn_clear_log = Gtk.Button(label="Clear")
        btn_clear_log.connect("clicked", self._on_clear_log)
        log_toolbar.pack_end(btn_clear_log, False, False, 0)
        log_box.pack_start(log_toolbar, False, False, 0)
        self._log_scroll = Gtk.ScrolledWindow()
        self._log_scroll.set_min_content_height(120)
        self._log_scroll.set_max_content_height(280)
        self._log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._log_scroll.set_shadow_type(Gtk.ShadowType.IN)
        self._log_buffer = Gtk.TextBuffer()
        self._log_view = Gtk.TextView(buffer=self._log_buffer, editable=False, monospace=True)
        self._log_view.set_left_margin(4)
        self._log_view.set_right_margin(4)
        self._log_view.set_top_margin(2)
        try:
            fd = Pango.FontDescription.from_string("Monospace 9")
            self._log_view.override_font(fd)
        except Exception:
            pass
        self._log_scroll.add(self._log_view)
        log_box.pack_start(self._log_scroll, True, True, 0)
        log_frame.add(log_box)
        box.pack_end(log_frame, True, True, 0)
        # When debug off, hide only the log content so "Show debug log" checkbox stays visible
        self._log_scroll.set_visible(self._debug_log)
        # Registration URI in lower right
        self._reg_uri_label = Gtk.Label(label="", xalign=1)
        self._reg_uri_label.set_selectable(True)
        try:
            self._reg_uri_label.override_font(Pango.FontDescription.from_string("9"))
        except Exception:
            pass
        uri_bar = Gtk.Box()
        uri_bar.pack_end(self._reg_uri_label, False, False, 0)
        box.pack_end(uri_bar, False, False, 0)
        set_blf_debug_log(self._log)
        if self._debug_log:
            self._log("Debug log ready")

    def _on_debug_toggled(self, btn):
        self._debug_log = btn.get_active()
        self._log_scroll.set_visible(self._debug_log)
        save_audio_settings(debug_log=self._debug_log)
        if self._debug_log:
            self._log("Debug log enabled")

    def _log(self, msg):
        """Append a timestamped line to the debug log when enabled; otherwise no-op."""
        if not getattr(self, "_debug_log", True):
            return
        try:
            ts = time.strftime("%H:%M:%S", time.localtime())
            self._log_buffer.insert(self._log_buffer.get_end_iter(), "[%s] %s\n" % (ts, msg))
            end = self._log_buffer.get_end_iter()
            self._log_view.scroll_to_iter(end, 0, False, 0, 1)
        except Exception:
            pass

    def _on_clear_log(self, btn):
        self._log_buffer.set_text("")

    def _current_call_remote(self):
        if not self._current_call or not self._engine:
            return ""
        try:
            return self._current_call.getInfo().remoteUri or ""
        except Exception:
            return ""

    def _update_call_buttons(self):
        has_current = bool(self._current_call)
        self._btn_hangup.set_sensitive(has_current)
        self._btn_mute.set_sensitive(has_current)
        self._btn_hold.set_sensitive(has_current)
        self._btn_transfer.set_sensitive(has_current)
        self._btn_merge.set_sensitive(len(self._active_calls) >= 1)
        can_complete = (
            self._attended_transfer_original is not None
            and len(self._active_calls) == 2
            and any(self._call_id(c) != self._call_id(self._attended_transfer_original) for c in self._active_calls)
        )
        self._btn_complete_transfer.set_sensitive(can_complete)
        self._btn_complete_transfer.set_visible(can_complete)
        if has_current and self._call_id(self._current_call) in self._held_calls:
            self._btn_hold.set_active(True)
        elif has_current:
            self._btn_hold.set_active(False)

    def _refresh_account_combo(self):
        self._accounts = load_accounts()
        combo = self._account_combo
        combo.remove_all()
        for a in self._accounts:
            combo.append(account_label(a), account_label(a))
        if self._accounts:
            combo.set_active(0)

    def _on_add_account(self, btn):
        d = AccountDialog(self)
        if d.run() != Gtk.ResponseType.OK:
            d.destroy()
            return
        vals = d.get_values()
        d.destroy()
        if not vals or not vals.get("uri"):
            if vals is None:
                md = Gtk.MessageDialog(
                    transient_for=self, modal=True,
                    message_type=Gtk.MessageType.WARNING,
                    buttons=Gtk.ButtonsType.OK,
                    text="User ID and Domain are required.",
                )
                md.run()
                md.destroy()
            return
        add_account(
            vals["uri"],
            vals["password"],
            registrar=vals.get("registrar"),
            use_tls=vals.get("use_tls", True),
            label=vals.get("label"),
        )
        self._refresh_account_combo()
        self._on_account_changed(None)

    def _on_edit_account(self, btn):
        acc = self._get_selected_account()
        if not acc:
            return
        d = AccountDialog(self, existing=acc)
        if d.run() != Gtk.ResponseType.OK:
            d.destroy()
            return
        vals = d.get_values()
        d.destroy()
        if not vals or not vals.get("uri"):
            if vals is None:
                md = Gtk.MessageDialog(
                    transient_for=self, modal=True,
                    message_type=Gtk.MessageType.WARNING,
                    buttons=Gtk.ButtonsType.OK,
                    text="User ID and Domain are required.",
                )
                md.run()
                md.destroy()
            return
        old_uri = acc.get("uri")
        update_account(
            old_uri,
            vals["uri"],
            vals["password"],
            registrar=vals.get("registrar"),
            use_tls=vals.get("use_tls", True),
            label=vals.get("label"),
        )
        self._refresh_account_combo()
        # Reselect the updated account (by new URI or by index)
        for i, a in enumerate(self._accounts):
            if a.get("uri") == vals["uri"]:
                self._account_combo.set_active(i)
                break
        self._on_account_changed(None)

    def _on_audio_options(self, btn):
        if not self._engine:
            self._log("Audio options: SIP engine not available")
            return
        if self._audio_dialog is not None:
            self._audio_dialog.present()
            return
        d = AudioOptionsDialog(self, self._engine, self._log)
        d.connect("destroy", lambda w: setattr(self, "_audio_dialog", None))
        self._audio_dialog = d
        d.show_all()

    def _get_selected_account(self):
        i = self._account_combo.get_active()
        if i < 0 or i >= len(self._accounts):
            return None
        return self._accounts[i]

    def _refresh_speeddials_blf(self):
        """Reload speed-dial buttons and BLF row for the current account."""
        acc = self._get_selected_account()
        account_uri = acc.get("uri") if acc else None
        for w in self._speeddial_box.get_children():
            self._speeddial_box.remove(w)
        for e in load_speeddials(account_uri):
            btn = Gtk.Button(label=(e.get("label") or e.get("number") or "?")[:12])
            btn.set_tooltip_text(e.get("number", ""))
            btn.entry_data = e
            btn.connect("clicked", self._on_speeddial_clicked)
            self._speeddial_box.pack_start(btn, False, False, 0)
        for w in self._blf_box.get_children():
            self._blf_box.remove(w)
        self._blf_indicators = {}  # uri -> BLFIndicator (green dot / flash / red)
        blf_entries = load_blf(account_uri)
        for e in blf_entries:
            uri = e.get("uri") or ""
            h = Gtk.Box(spacing=6)
            lbl = Gtk.Label(label=(e.get("label") or uri or "?")[:16], xalign=0)
            if cairo:
                indicator = BLFIndicator()
                indicator.set_state("idle")
                h.pack_start(lbl, False, False, 0)
                h.pack_start(indicator, False, False, 0)
            else:
                status = Gtk.Label(label="—", xalign=0)
                status.get_style_context().add_class("dim-label")
                h.pack_start(lbl, False, False, 0)
                h.pack_start(status, False, False, 0)
                indicator = status
            if uri:
                ev = Gtk.EventBox()
                ev.add(h)
                ev.blf_uri = uri
                ev.connect("button-press-event", self._on_blf_clicked)
                ev.set_tooltip_text("Click to call %s — Green=idle, flashing=ringing, red=on call" % uri)
                try:
                    ev.get_style_context().add_class("blf-clickable")
                except Exception:
                    pass
                self._blf_box.pack_start(ev, False, False, 0)
                self._blf_indicators[uri] = indicator
            else:
                self._blf_box.pack_start(h, False, False, 0)
        if self._engine and blf_entries:
            self._engine.set_blf(blf_entries)
        if blf_entries:
            self._log("BLF: %d entries: %s" % (len(blf_entries), ", ".join(
                "%s (%s)" % (x.get("label") or "?", x.get("uri") or "?") for x in blf_entries)))
        else:
            self._log("BLF: no entries")
        self._speeddial_box.show_all()
        self._blf_box.show_all()

    def _on_speeddial_clicked(self, btn):
        """Speed-dial button: start a call to that number (or URI)."""
        if hasattr(btn, "entry_data") and btn.entry_data.get("number"):
            to = btn.entry_data["number"].strip()
            if to:
                self._call_uri(to)

    def _on_blf_clicked(self, widget, event):
        """Click on BLF row: call that URI (left click only)."""
        if event.button != 1:
            return False
        uri = getattr(widget, "blf_uri", None)
        if uri:
            self._call_uri(uri)
        return True

    def _call_uri(self, to):
        """Place a call to the given number or URI (speed dial, BLF click, etc.)."""
        if not to or not self._engine or not self._get_selected_account():
            if not self._get_selected_account():
                self._status.set_text("Select an account first")
            return
        to = to.strip()
        if not to.startswith("sip"):
            to = "sip:" + to
        # If no @, add account domain so the call goes to our SIP server
        acc = self._get_selected_account()
        dialed = to.replace("sips:", "").replace("sip:", "").strip()
        if acc and "@" not in dialed:
            domain = (acc.get("uri") or "").replace("sips:", "").replace("sip:", "")
            if "@" in domain:
                domain = domain.split("@", 1)[1]
                scheme = "sips" if acc.get("use_tls") else "sip"
                to = "%s:%s@%s" % (scheme, dialed, domain)
        self._log("Call to %s" % to)
        try:
            call, err = self._engine.make_call(to)
            if call is not None:
                add_entry(to, "out")
                self._current_call = call
                self._btn_call.set_sensitive(False)
                self._btn_hangup.set_sensitive(True)
                self._btn_mute.set_sensitive(True)
                self._status.set_text("Calling %s…" % to)
                self._log("Call started to %s" % to)
            else:
                self._status.set_text("Call failed: %s" % (err or "Unknown error"))
                self._log("Call failed: %s" % (err or "Unknown error"))
        except Exception as e:
            self._status.set_text("Call error: %s" % str(e))
            self._log("Call exception: %s" % str(e))

    def _on_blf_state(self, uri, state_str):
        """Update BLF indicator for the given URI (green dot / green flash / red)."""
        if getattr(self, "_blf_indicators", None) and uri not in self._blf_indicators:
            return
        if uri not in self._blf_indicators:
            return
        ind = self._blf_indicators[uri]
        raw = (state_str or "").strip()
        # Map to display state: idle -> green dot, ringing -> green flash, busy -> red
        if not raw or raw in ("?", "Active", "Pending"):
            state = "idle"
        else:
            raw_lower = raw.lower()
            if raw_lower in ("terminated", "idle", "init"):
                state = "idle"
            elif raw_lower in ("trying", "early", "proceeding"):
                state = "ringing"
            elif raw_lower in ("confirmed", "active"):
                state = "busy"
            else:
                state = "idle"
        if isinstance(ind, BLFIndicator):
            ind.set_state(state)
        else:
            # fallback text when cairo not available
            text = {"idle": "—", "ringing": "…", "busy": "●"}.get(state, "—")
            if ind.get_label() != text:
                ind.set_text(text)

    def _on_speeddials_blf(self, menuitem=None):
        acc = self._get_selected_account()
        if not acc:
            self._status.set_text("Select an account first")
            self._log("Speed dials & BLF: select an account first")
            return
        d = SpeedDialsBLFDialog(
            self,
            on_saved=self._refresh_speeddials_blf,
            on_log=self._log,
            account_uri=acc.get("uri"),
        )
        d.run()

    def _on_call_history(self, menuitem=None):
        """Open call history window (scrollable, max 20; click to call)."""
        d = CallHistoryDialog(self, on_call=self._call_uri)
        d.run()

    def _on_open_sip_debug_log(self, menuitem):
        """Open the SIP/RTP debug log file in the default app (for debugging SIP flow)."""
        path = os.path.join(CONFIG_DIR, "sip_debug.log")
        try:
            uri = Gio.File.new_for_path(path).get_uri()
            Gio.AppInfo.launch_default_for_uri(uri, None)
            self._log("Opened SIP debug log: %s" % path)
        except Exception as e:
            self._log("Could not open SIP debug log: %s" % (e or path))
            md = Gtk.MessageDialog(
                self, 0, Gtk.MessageType.INFO, Gtk.ButtonsType.OK,
                "SIP debug log path:\n%s\n\nOpen it manually in a text editor." % path
            )
            md.run()
            md.destroy()

    def _on_account_changed(self, _):
        acc = self._get_selected_account()
        self._reg_uri_label.set_text(acc.get("uri", "") if acc else "")
        self._refresh_speeddials_blf()  # load this account's speed dials and BLF
        if not acc or not self._engine:
            self._reg_indicator.set_markup('<span size="small">○ Not registered</span>')
            self._status.set_text("No account" if not acc else "Select account")
            self._log("Account changed: none selected")
            return
        self._registered = False
        self._reg_indicator.set_markup('<span size="small" color="gray">… Registering</span>')
        self._status.set_text("Registering…")
        self._log("Account changed: unregistering previous, registering %s" % account_label(acc))
        ok = self._engine.set_account(acc)
        if not ok:
            self._reg_indicator.set_markup('<span size="small" color="darkred">○ Not registered</span>')
            self._status.set_text("Failed to set account")
            self._log("set_account failed for %s" % account_label(acc))

    def _on_reg_state(self, ok, uri):
        self._registered = bool(ok)
        if uri:
            self._reg_uri_label.set_text(uri)
        if ok:
            self._reg_indicator.set_markup('<span size="small" color="green">● Registered</span>')
            acc = self._get_selected_account()
            label = account_label(acc) if acc else (uri or "OK")
            self._status.set_text("Registered: %s" % label)
            self._log("Registration OK: %s" % (uri or label))
            # Apply BLF subscriptions for this account now that we're registered
            if self._engine:
                acc = self._get_selected_account()
                account_uri = acc.get("uri") if acc else None
                self._engine.set_blf(load_blf(account_uri))
        else:
            self._reg_indicator.set_markup('<span size="small" color="darkred">○ Not registered</span>')
            self._status.set_text("Registration failed: %s" % (uri or "Unknown error"))
            self._log("Registration failed: %s" % (uri or "Unknown error"))

    def _on_incoming_call(self, call, remote_uri):
        if remote_uri:
            add_entry(remote_uri, "in")
        self._incoming_call = call
        self._status.set_text("Incoming: %s" % (remote_uri or "?"))
        self._btn_answer.set_sensitive(True)
        self._btn_reject.set_sensitive(True)
        self._btn_call.set_sensitive(False)
        self._log("Incoming call from %s" % (remote_uri or "?"))

    def _call_id(self, c):
        if not c or not self._engine:
            return None
        return self._engine._call_id(c)

    def _start_call_stats_timer(self):
        if self._call_stats_timeout_id is not None:
            return
        self._update_call_stats()  # show "Call: 0:00  Latency: — ms  MOS: —" immediately
        self._call_stats_timeout_id = GLib.timeout_add(1000, self._update_call_stats)

    def _stop_call_stats_timer(self):
        if self._call_stats_timeout_id is not None:
            GLib.source_remove(self._call_stats_timeout_id)
            self._call_stats_timeout_id = None

    def _update_call_stats(self):
        if not self._current_call or not self._engine or self._call_start_time is None:
            self._stop_call_stats_timer()
            return False
        elapsed = int(time.time() - self._call_start_time)
        m, s = elapsed // 60, elapsed % 60
        timer_str = "%d:%02d" % (m, s)
        stats = self._engine.get_call_stats(self._current_call)
        if stats:
            lat = int(stats.get("rtt_ms", 0))
            mos = stats.get("mos", 0)
            self._call_stats_label.set_text("Call: %s   Latency: %d ms   MOS: %.1f" % (timer_str, lat, mos))
        else:
            self._call_stats_label.set_text("Call: %s   Latency: — ms   MOS: —" % timer_str)
        return True

    def _on_media_active(self, call, remote_uri):
        """Fallback: when RTP media becomes active, show 'In call' and start timer (in case CONFIRMED wasn't delivered)."""
        if not call or not self._engine:
            return
        cid = self._call_id(call)
        if call not in self._active_calls:
            self._active_calls.append(call)
        self._current_call = call
        self._status.set_text("In call: %s" % (remote_uri or "") + (" (%s calls)" % len(self._active_calls) if len(self._active_calls) > 1 else ""))
        if self._call_start_time is None:
            self._call_start_time = time.time()
        self._start_call_stats_timer()

    def _on_call_state(self, call, state, remote_uri, code, reason=None):
        # Call states: 0=null, 1=calling, 2=incoming, 3=early, 4=connecting, 5=confirmed, 6=disconnected
        from sip_engine import CALL_STATE_NULL, CALL_STATE_DISCONNECTED, CALL_STATE_CONFIRMED, CALL_STATE_INCOMING
        try:
            state = int(state)
        except (TypeError, ValueError):
            state = -1
        state_names = {0: "null", 1: "calling", 2: "incoming", 3: "early", 4: "connecting", 5: "confirmed", 6: "disconnected"}
        reason_str = (reason or "").strip()
        self._log("Call state=%s (%s) remote=%s code=%s reason=%s" % (state, state_names.get(state, "?"), remote_uri or "", code or "", reason_str or "(none)"))
        cid = self._call_id(call)
        if state in (CALL_STATE_NULL, CALL_STATE_DISCONNECTED):
            self._active_calls = [c for c in self._active_calls if self._call_id(c) != cid]
            self._held_calls.discard(cid)
            if self._attended_transfer_original and self._call_id(self._attended_transfer_original) == cid:
                self._attended_transfer_original = None
            if self._current_call and self._call_id(self._current_call) == cid:
                self._current_call = self._active_calls[0] if self._active_calls else None
                if self._current_call:
                    self._call_start_time = time.time()
                    self._start_call_stats_timer()
            if not self._active_calls:
                self._current_call = None
                self._incoming_call = None
                code_int = 0
                try:
                    code_int = int(code) if code not in (None, "") else 0
                except (TypeError, ValueError):
                    pass
                if code_int >= 400 or reason_str:
                    display = ("%s %s" % (code or "", reason_str)).strip() if reason_str else {480: "480 Temporarily Unavailable (no answer)", 486: "486 Busy", 487: "487 Request Terminated/Cancelled", 603: "603 Decline"}.get(code_int, "%s" % code)
                    self._status.set_text(display or "Call ended")
                    self._log("Call ended: %s" % (display or "Call ended"))
                else:
                    self._status.set_text("Ready" if self._get_selected_account() else "No account")
                self._btn_hangup.set_sensitive(False)
                self._btn_mute.set_sensitive(False)
                self._btn_hold.set_sensitive(False)
                self._btn_transfer.set_sensitive(False)
                self._btn_merge.set_sensitive(False)
                self._btn_complete_transfer.set_sensitive(False)
                self._btn_complete_transfer.set_visible(False)
                self._btn_mute.set_active(False)
                self._muted = False
                self._btn_answer.set_sensitive(False)
                self._btn_reject.set_sensitive(False)
                self._btn_call.set_sensitive(True)
                self._call_start_time = None
                self._stop_call_stats_timer()
                self._call_stats_label.set_text("")
            else:
                self._update_call_buttons()
                self._status.set_text("In call: %s" % (self._current_call_remote() or "") + (" (%s calls)" % len(self._active_calls) if len(self._active_calls) > 1 else ""))
        else:
            if state in (CALL_STATE_CONNECTING, CALL_STATE_CONFIRMED) and call not in self._active_calls:
                self._active_calls.append(call)
            if state == CALL_STATE_CONFIRMED:
                self._current_call = call  # ensure we track the confirmed call
            self._btn_hangup.set_sensitive(True)
            self._btn_mute.set_sensitive(True)
            self._btn_hold.set_sensitive(bool(self._current_call))
            self._btn_transfer.set_sensitive(bool(self._current_call))
            self._btn_merge.set_sensitive(len(self._active_calls) >= 1)
            can_complete = (
                self._attended_transfer_original is not None
                and len(self._active_calls) == 2
                and any(self._call_id(c) != self._call_id(self._attended_transfer_original) for c in self._active_calls)
            )
            self._btn_complete_transfer.set_sensitive(can_complete)
            self._btn_complete_transfer.set_visible(can_complete)
            if state == CALL_STATE_CONFIRMED:
                self._status.set_text("In call: %s" % (remote_uri or "") + (" (%s calls)" % len(self._active_calls) if len(self._active_calls) > 1 else ""))
                self._btn_answer.set_sensitive(False)
                self._btn_reject.set_sensitive(False)
                self._btn_hold.set_active(cid in self._held_calls)
                self._call_start_time = time.time()
                self._start_call_stats_timer()
            elif state == CALL_STATE_INCOMING:
                self._status.set_text("Incoming: %s" % (remote_uri or ""))
            elif state == 4:
                self._status.set_text("Connecting…" if not remote_uri else ("Connecting %s…" % remote_uri))
            elif state in (1, 3):  # calling, early – don't overwrite "In call" if a late callback arrives
                self._status.set_text("Calling %s…" % (remote_uri or ""))

    def _do_call(self):
        to = self._dial_entry.get_text().strip()
        if not to:
            return
        if not to.startswith("sip"):
            to = "sip:" + to
        if not self._engine or not self._get_selected_account():
            self._status.set_text("Select an account first")
            return
        # If destination has no @, add account domain so the INVITE goes to our SIP server
        # (otherwise sip:9178266664 is parsed as host "9178266664" -> gethostbyname fails)
        acc = self._get_selected_account()
        dialed = to.replace("sips:", "").replace("sip:", "").strip()
        if acc and "@" not in dialed:
            domain = (acc.get("uri") or "").replace("sips:", "").replace("sip:", "")
            if "@" in domain:
                domain = domain.split("@", 1)[1]
                scheme = "sips" if acc.get("use_tls") else "sip"
                to = "%s:%s@%s" % (scheme, dialed, domain)
        self._log("Call to %s" % to)
        try:
            call, err = self._engine.make_call(to)
            if call is not None:
                self._current_call = call
                self._btn_call.set_sensitive(False)
                self._btn_hangup.set_sensitive(True)
                self._btn_mute.set_sensitive(True)
                self._status.set_text("Calling %s…" % to)
                self._log("Call started to %s" % to)
            else:
                self._status.set_text("Call failed: %s" % (err or "Unknown error"))
                self._log("Call failed: %s" % (err or "Unknown error"))
        except Exception as e:
            self._status.set_text("Call error: %s" % str(e))
            self._log("Call exception: %s" % str(e))

    def _on_hangup(self, btn):
        call = self._current_call or self._incoming_call
        if call and self._engine:
            self._engine.hangup_call(call)
            self._log("Hangup")
        if call == self._incoming_call:
            self._incoming_call = None
        # _current_call and _active_calls are updated in _on_call_state when we get DISCONNECTED

    def _on_answer(self, btn):
        if self._incoming_call and self._engine:
            self._engine.answer_call(self._incoming_call)
            self._current_call = self._incoming_call
            self._incoming_call = None
            self._log("Answered")
        self._btn_answer.set_sensitive(False)
        self._btn_reject.set_sensitive(False)

    def _on_reject(self, btn):
        if self._incoming_call and self._engine:
            self._engine.hangup_call(self._incoming_call)
            self._log("Rejected")
        self._incoming_call = None
        self._btn_answer.set_sensitive(False)
        self._btn_reject.set_sensitive(False)

    def _on_mute_toggled(self, btn):
        self._muted = btn.get_active()
        call = self._current_call or self._incoming_call
        if call and self._engine:
            self._engine.set_mute(call, self._muted)
        self._log("Mute: %s" % self._muted)

    def _on_hold_toggled(self, btn):
        call = self._current_call
        if not call or not self._engine:
            return
        hold = btn.get_active()
        cid = self._call_id(call)
        if hold:
            self._engine.hold_call(call)
            self._held_calls.add(cid)
            self._log("Hold")
        else:
            self._engine.unhold_call(call)
            self._held_calls.discard(cid)
            self._log("Unhold")

    def _normalize_dest(self, to):
        """Return sip/sips URI for destination (add scheme and domain if needed)."""
        to = (to or "").strip()
        if not to:
            return None
        if not to.startswith("sip"):
            to = "sip:" + to
        acc = self._get_selected_account()
        dialed = to.replace("sips:", "").replace("sip:", "").strip()
        if acc and "@" not in dialed:
            domain = (acc.get("uri") or "").replace("sips:", "").replace("sip:", "")
            if "@" in domain:
                domain = domain.split("@", 1)[1]
            scheme = "sips" if acc.get("use_tls") else "sip"
            to = "%s:%s@%s" % (scheme, dialed, domain)
        return to

    def _on_transfer(self, btn):
        call = self._current_call
        if not call or not self._engine or not self._get_selected_account():
            return
        d = Gtk.Dialog(title="Transfer", transient_for=self, modal=True)
        d.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, "Unattended", 100, "Attended", 101)
        d.set_default_size(320, 100)
        box = d.get_content_area()
        box.set_spacing(8)
        box.add(Gtk.Label(label="Transfer to (number or URI):"))
        entry = Gtk.Entry()
        entry.set_placeholder_text("Number or sip:user@host")
        box.add(entry)
        d.show_all()
        resp = d.run()
        dest = entry.get_text().strip()
        d.destroy()
        if resp != 100 and resp != 101:
            return
        to = self._normalize_dest(dest)
        if not to:
            self._status.set_text("Enter a destination")
            return
        if resp == 100:  # Unattended
            err = self._engine.transfer_call(call, to)
            if err:
                self._status.set_text("Transfer failed: %s" % err)
            else:
                self._log("Transfer (unattended) to %s" % to)
        else:  # Attended
            self._attended_transfer_original = call
            call2, err = self._engine.make_call(to)
            if call2 is None:
                self._attended_transfer_original = None
                self._status.set_text("Transfer call failed: %s" % (err or "Unknown"))
            else:
                self._log("Calling %s for attended transfer…" % to)
                self._status.set_text("Calling %s… Answer then click Complete transfer" % to)

    def _on_complete_transfer(self, btn):
        if not self._attended_transfer_original or len(self._active_calls) != 2 or not self._engine:
            return
        other = next((c for c in self._active_calls if self._call_id(c) != self._call_id(self._attended_transfer_original)), None)
        if not other:
            return
        err = self._engine.transfer_attended(self._attended_transfer_original, other)
        self._attended_transfer_original = None
        self._btn_complete_transfer.set_visible(False)
        self._btn_complete_transfer.set_sensitive(False)
        if err:
            self._status.set_text("Attended transfer failed: %s" % err)
        else:
            self._log("Attended transfer completed")

    def _on_merge(self, btn):
        if not self._engine or not self._get_selected_account():
            return
        d = Gtk.Dialog(title="Merge (add call)", transient_for=self, modal=True)
        d.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK)
        d.set_default_size(320, 100)
        box = d.get_content_area()
        box.set_spacing(8)
        box.add(Gtk.Label(label="Number or URI to add:"))
        entry = Gtk.Entry()
        entry.set_placeholder_text("Number or sip:user@host")
        box.add(entry)
        d.show_all()
        if d.run() != Gtk.ResponseType.OK:
            d.destroy()
            return
        to = self._normalize_dest(entry.get_text())
        d.destroy()
        if not to:
            return
        try:
            call, err = self._engine.make_call(to)
            if call is not None:
                self._log("Merge: calling %s" % to)
                self._status.set_text("Calling %s… (merge)" % to)
            else:
                self._status.set_text("Call failed: %s" % (err or "Unknown error"))
        except Exception as e:
            self._status.set_text("Call error: %s" % str(e))

    def _on_dialpad_key(self, btn, key):
        text = self._dial_entry.get_text()
        self._dial_entry.set_text(text + key)
        call = self._current_call
        if call and self._engine:
            self._engine.dtmf(call, key)
            self._log("DTMF: %s" % key)

    def _make_tray_menu(self):
        """Build tray menu with Show and Quit (unregister and exit)."""
        menu = Gtk.Menu()
        item_show = Gtk.MenuItem(label="Show")
        item_show.connect("activate", lambda _: self._tray_show())
        menu.append(item_show)
        menu.append(Gtk.SeparatorMenuItem())
        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda _: self._tray_quit())
        menu.append(item_quit)
        menu.show_all()
        return menu

    def _setup_tray(self):
        """Create system tray icon (AppIndicator preferred; StatusIcon fallback) with Show / Quit menu."""
        icon_path = _app_icon_path()
        icon_name = icon_path if icon_path else "phone"
        menu = self._make_tray_menu()

        if _HAS_APP_INDICATOR:
            self._indicator = AppIndicator3.Indicator.new(
                "nixsip",
                icon_name,
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            self._indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self._indicator.set_menu(menu)
            return

        # Fallback: Gtk.StatusIcon (deprecated but works when AppIndicator not installed)
        try:
            self._status_icon = Gtk.StatusIcon()
            if icon_path and os.path.isfile(icon_path):
                self._status_icon.set_from_file(icon_path)
            else:
                self._status_icon.set_icon_name("phone")
            self._status_icon.set_tooltip_text("NixSIP")
            self._status_icon.connect("activate", lambda _: self._tray_show())
            self._status_icon.connect("popup-menu", self._status_icon_popup)
        except Exception:
            self._status_icon = None

    def _status_icon_popup(self, icon, button, time):
        """Show Show/Quit menu for StatusIcon right-click."""
        menu = self._make_tray_menu()
        menu.popup(None, None, None, None, button, time)

    def _tray_show(self):
        self.show()
        self.present()
        self.deiconify()

    def _tray_quit(self):
        """Unregister account and quit (destroy triggers _on_destroy → main_quit)."""
        if self._engine:
            self._engine.unregister()
            self._engine.stop()
        self.destroy()

    def _on_delete_event(self, w, ev):
        """X button: hide to system tray; if no tray, allow close (quit)."""
        if self._indicator or self._status_icon:
            self.hide()
            return True  # prevent destroy
        return False  # no tray: let window close and quit

    def _on_destroy(self, w):
        if self._engine:
            self._engine.unregister()
            self._engine.stop()
        Gtk.main_quit()


def main():
    # Set default app icon at multiple sizes so taskbar/window list shows it
    icon_pixbufs = _app_icon_pixbufs()
    if icon_pixbufs:
        try:
            Gtk.Window.set_default_icon_list(icon_pixbufs)
        except Exception:
            pass
    if not icon_pixbufs:
        icon_path = _app_icon_path()
        if icon_path:
            try:
                Gtk.Window.set_default_icon_from_file(icon_path)
            except Exception:
                pass
    win = MainWindow()
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
