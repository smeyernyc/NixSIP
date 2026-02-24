# NixSIP 1.0

Compact Linux SIP client (User-Agent: NixSIP/1.0). Tested on Linux Mint. Multi-account support, dialpad, mute, hold, transfer (attended and unattended), merge (three-way+), and **TLS (SIPS)**. Media is **not** SRTP — TLS for signaling only.

## Features

- **Multiple accounts** — Add several SIP accounts and switch from the dropdown. Switching accounts unregisters the previous one and registers the selected account.
- **Speed dials & BLF** — Menu → **Edit speed dials & BLF…** to add speed-dial and BLF entries. BLF shows a lamp per extension: green = idle, red = in call (see [BLF](#blf-busy-lamp-field) below).
- **TLS** — Use SIPS (port 5061) per account; optional TLS per account.
- **Call controls** — Dialpad (scales to window width), Call, Hang up, Answer, Reject, Mute, Hold, Transfer (unattended and attended), Merge (add call for three-way+).
- **In-call display** — During an active call the app shows a **call timer** (M:SS), **latency** (RTT in ms from RTCP), and an estimated **MOS** (1–5) derived from RTT and packet loss.
- **Call history** — Menu → **Call history…** shows the last 20 numbers (incoming and outgoing). Click a row to place a call. Stored in `call_history.json`.
- **No SRTP** — RTP is sent in the clear; only SIP signaling can use TLS.
- **Close to tray** — Closing the window (X) hides it to the system tray (if AppIndicator is available). Right‑click the tray icon for **Show** or **Quit**. Without the tray library, X closes the app.
- **App icon** — Window and tray use `icons/nixsip.png` or the theme icon "phone".

## Requirements

- Python 3.6+
- GTK 3 and PyGObject
- pjproject (PJSIP) with Python pjsua2 bindings
- **Optional (for system tray):** `gir1.2-appindicator3-0.1` (e.g. `sudo apt install gir1.2-appindicator3-0.1`)

## Install (Debian/Ubuntu)

### Quick install

```bash
# 1. GTK dependencies
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0

# 2. Build and install pjsua2 (from project directory)
cd /path/to/SIP
./install_pjsua2.sh
```

The install script installs build deps, clones pjproject into `pjproject_build`, builds it, and installs the Python bindings (sudo required for system install).

### Manual install

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0
sudo apt install build-essential libssl-dev python3-dev swig \
    libasound2-dev libportaudio2 portaudio19-dev git

git clone https://github.com/pjsip/pjproject.git pjproject_build
cd pjproject_build
./configure --enable-shared CFLAGS='-fPIC'
make dep && make
cd pjsip-apps/src/swig/python && make && sudo make install
```

## Run

If pjsua2 is installed system-wide:

```bash
python3 main.py
```

If you use the local build only (or see "pjsua2 not installed"):

```bash
./run.sh
```

`run.sh` sets `LD_LIBRARY_PATH` and `PYTHONPATH` to the local `pjproject_build` and exports `PJSIP_DISABLE_SECURE_DLG_CHECK=1` for servers that send `sip:` in Contact for SIPS dialogs.

## Usage

1. **+** — Add account (Display name, User ID, Domain, Password, Use TLS).
2. Select an account from the dropdown to register.
3. Enter a number or `sip:user@host` and click **Call** (or Enter).
4. **Answer** / **Reject** — Shown only when there is an incoming call; **Hang up** — End current call; **Mute** — Mute mic; **Hold** — Hold/Unhold (applies to the "current" leg); **Transfer** — Unattended or Attended (dial then Complete transfer); **Add call** — While in a call, add a second party (dial number; both legs stay in the call for three-way).
5. **Three-way (Add call)** — Be on a call, click **Add call**, enter the second number. When the second party answers, both legs are active and the app mixes audio (via pjproject). The "current" call (used for Hold, Transfer, DTMF) stays the first leg; Hang up ends the current leg only.
6. Dialpad scales with window width and keeps button aspect ratio; use it for DTMF during a call.
7. **In-call stats** — While a call is connected, a line under the status shows call duration (e.g. 1:23), latency in ms, and MOS (Mean Opinion Score) quality estimate. Values appear after RTP is flowing; "—" is shown until data is available.
8. **Speed dials & BLF** — Each account has its own list. Add BLF entries with label + URI (e.g. `998` or `sip:998@pbx.example.com`). Click a BLF row to call that extension.
9. **Call history** — **Menu → Call history…** opens a list of the last 20 calls (↑ outgoing, ↓ incoming). Click a row to dial that number. Shown as "No call history" when empty.

**Config files:** `~/.config/sipclient/accounts.json`, `audio.json`, `prefs.json` (last-used account). Speed dials and BLF are stored per account as `speeddials_<key>.json` and `blf_<key>.json` (one key per account). Call history is in `call_history.json`.

## BLF (Busy Lamp Field)

BLF subscribes to dialog state for each configured extension. The lamp shows **green** when idle and **red** when the extension is in a call. State is derived from NOTIFY bodies in `~/.config/sipclient/sip_debug.log` (the pjsua2 Python callback does not expose the NOTIFY body in this build). A short delay and a periodic refresh from the log keep the lamp in sync when calls start and end. Requires SIP debug logging (on by default).

## Debugging

- **Show debug log** — Check "Show debug log" in the log area to show or hide the in-app log panel.
- **SIP/RTP trace** — Written to `~/.config/sipclient/sip_debug.log`. Use **Menu → Open SIP debug log…** to open it. Used for registration, calls, BLF NOTIFYs, and message flow.

## SIPS and 480 SIPS Required

If calls over TLS drop after answer with **480 SIPS Required**, the server likely sent a 200 OK with `sip:` in Contact. **Fix:** configure the server to use `sips:` in Contact. **Workaround:** `run.sh` sets `PJSIP_DISABLE_SECURE_DLG_CHECK=1`; the local pjproject build includes this support.

## TLS only, no SRTP

TLS is used for SIP when **Use TLS** is checked (SIPS, typically 5061). RTP/audio is not encrypted; only signaling can use TLS.
