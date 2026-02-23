"""SIP engine using pjsua2: TLS support, no SRTP."""

import os
import sys
import threading

# Call state constants (PJSIP_INV_STATE_*)
CALL_STATE_NULL = 0
CALL_STATE_CALLING = 1
CALL_STATE_INCOMING = 2
CALL_STATE_EARLY = 3
CALL_STATE_CONNECTING = 4
CALL_STATE_CONFIRMED = 5
CALL_STATE_DISCONNECTED = 6

try:
    import pjsua2 as pj
    PJSIP_TRANSPORT_TLS = getattr(pj, "PJSIP_TRANSPORT_TLS", 3)
    PJSIP_TRANSPORT_UDP = getattr(pj, "PJSIP_TRANSPORT_UDP", 17)
except ImportError:
    pj = None
    PJSIP_TRANSPORT_TLS = 3
    PJSIP_TRANSPORT_UDP = 17


def _call_handler_base():
    return pj.Call if pj else object


def _account_handler_base():
    return pj.Account if pj else object


def _buddy_handler_base():
    return pj.Buddy if pj else object


class BLFBuddyHandler(_buddy_handler_base()):
    """Buddy for BLF (dialog event) subscription; notifies engine on state change."""

    def __init__(self, engine=None, blf_uri=""):
        if pj:
            pj.Buddy.__init__(self)
        self._engine = engine
        self._blf_uri = blf_uri

    def _report_state(self):
        if not pj or not self._engine or not self._engine.on_blf_state:
            return
        try:
            info = self.getInfo()
            state_str = ""
            if info and getattr(info, "presStatus", None):
                ps = info.presStatus
                state_str = (getattr(ps, "statusText", None) or getattr(ps, "note", None) or "").strip()
            if not state_str:
                state_str = getattr(info, "subStateName", None) or ""
            state_str = state_str.strip()
            # Subscription state (Active, Pending) or "?" is not dialog state; show as idle/unknown
            if state_str in ("?", "Active", "Pending", "Terminated", ""):
                state_str = "—"
            if self._engine.on_log:
                self._engine.on_log("BLF state %s: %s" % (self._blf_uri, state_str))
            self._engine.on_blf_state(self._blf_uri, state_str)
        except Exception:
            pass

    def onBuddyDlgEventState(self):
        self._report_state()

    def onBuddyEvSubDlgEventState(self, prm):
        self._report_state()


class CallHandler(_call_handler_base()):
    """Handle call events; holds ref so Call is not GC'd."""

    def __init__(self, acc, call_id=None, engine=None):
        if pj:
            pj.Call.__init__(self, acc, call_id if call_id is not None else pj.PJSUA_INVALID_ID)
        self._engine = engine

    def onCallState(self, prm):
        if not pj or not self._engine or not self._engine.on_call_state:
            return
        ci = self.getInfo()
        reason = getattr(ci, "lastReason", None) or ""
        self._engine.on_call_state(self, ci.state, ci.remoteUri, ci.lastStatusCode, reason)

    def onCallMediaState(self, prm):
        if not pj or not self._engine:
            return
        ci = self.getInfo()
        for mi in ci.media:
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                try:
                    aud_med = self.getAudioMedia(mi.index)
                    if aud_med:
                        self._engine._connect_slots(aud_med)
                except Exception:
                    pass
                if getattr(self._engine, "on_media_active", None):
                    self._engine.on_media_active(self, ci.remoteUri)
                break


class AccountHandler(_account_handler_base()):
    """Account handler for registration and incoming calls."""

    def __init__(self, engine=None):
        if pj:
            pj.Account.__init__(self)
        self._engine = engine

    def onRegState(self, prm):
        if not pj or not self._engine or not self._engine.on_reg_state:
            return
        ai = self.getInfo()
        self._engine.on_reg_state(ai.regStatus, ai.uri)

    def onIncomingCall(self, prm):
        if not pj or not self._engine:
            return
        c = CallHandler(self, prm.callId, self._engine)
        ci = c.getInfo()
        if self._engine.on_incoming_call:
            self._engine.on_incoming_call(c, ci.remoteUri)


class SipEngine:
    """Single SIP endpoint: TLS transport, multiple accounts, no SRTP."""

    def __init__(self):
        self._ep = None
        self._accounts = {}  # uri -> (AccountHandler, AccountConfig)
        self._calls = {}  # call_id -> CallHandler
        self._blf_buddies = []  # list of BLFBuddyHandler (keep refs so not GC'd)
        self._lock = threading.Lock()
        self.on_reg_state = None
        self.on_incoming_call = None
        self.on_call_state = None
        self.on_blf_state = None
        self.on_log = None
        self._tls_transport_id = None
        self._udp_transport_id = None
        self._worker_thread = None
        self._running = False

    def _run_worker(self):
        """Run pj worker in this thread (must be called from a dedicated thread)."""
        if not pj:
            raise RuntimeError("pjsua2 not available. Install pjproject and build Python bindings.")
        self._ep = pj.Endpoint()
        self._ep.libCreate()

        ep_cfg = pj.EpConfig()
        ep_cfg.uaConfig.threadCnt = 0
        ep_cfg.uaConfig.userAgent = "NixSIP/1.0"
        try:
            ep_cfg.mediaConfig.srtpUse = getattr(pj, "PJMEDIA_SRTP_DISABLED", 0)
        except Exception:
            pass
        # SIP/RTP debug log to file for diagnosing call drops (e.g. after answer)
        try:
            from accounts import CONFIG_DIR, _ensure_config_dir
            _ensure_config_dir()
            ep_cfg.logConfig.msgLogging = 1
            ep_cfg.logConfig.level = 5
            ep_cfg.logConfig.consoleLevel = 4
            self._sip_log_path = os.path.join(CONFIG_DIR, "sip_debug.log")
            ep_cfg.logConfig.filename = self._sip_log_path
        except Exception:
            self._sip_log_path = None
        self._ep.libInit(ep_cfg)

        # TLS transport (5061)
        tcfg = pj.TransportConfig()
        tcfg.port = 5061
        try:
            self._tls_transport_id = self._ep.transportCreate(PJSIP_TRANSPORT_TLS, tcfg)
        except Exception:
            self._tls_transport_id = None

        # UDP for non-TLS (5060)
        tcfg_udp = pj.TransportConfig()
        tcfg_udp.port = 5060
        try:
            self._udp_transport_id = self._ep.transportCreate(PJSIP_TRANSPORT_UDP, tcfg_udp)
        except Exception:
            self._udp_transport_id = None

        self._ep.libStart()
        # Register this thread for PJSIP
        try:
            self._ep.libRegisterThread("main")
        except Exception:
            pass
        if getattr(self, "_sip_log_path", None) and self.on_log:
            self.on_log("SIP/RTP debug log: %s" % self._sip_log_path)
        # If no audio devices at all, use null device so calls don't fail
        try:
            adm = self._ep.audDevManager()
            if adm.getDevCount() == 0:
                adm.setNullDev()
                if self.on_log:
                    self.on_log("No audio devices - using null audio (no sound)")
            else:
                # Apply saved audio device selection so it persists across restarts
                try:
                    from audio_config import load_audio_settings
                    s = load_audio_settings()
                    if s.get("playback_dev_id") is not None:
                        adm.setPlaybackDev(s["playback_dev_id"])
                        if self.on_log:
                            self.on_log("Restored playback device %s" % s["playback_dev_id"])
                    if s.get("capture_dev_id") is not None:
                        adm.setCaptureDev(s["capture_dev_id"])
                        if self.on_log:
                            self.on_log("Restored capture device %s" % s["capture_dev_id"])
                except Exception:
                    pass
        except Exception:
            pass

    def _event_worker(self):
        """Worker thread: poll for SIP events."""
        if not pj:
            return
        try:
            self._ep.libRegisterThread("worker")
        except Exception:
            pass
        while self._running:
            try:
                if self._ep:
                    self._ep.libHandleEvents(10)  # 10ms timeout
            except Exception:
                break
            import time
            time.sleep(0.01)  # Small sleep to avoid busy-wait

    def _connect_slots(self, aud_med):
        """Connect call audio to sound device. Must succeed or the call may drop when answered."""
        try:
            adm = self._ep.audDevManager()
            # Always try to connect (null device still provides media ports). Skipping this can cause the call to drop when the other party answers.
            cap = adm.getCaptureDevMedia()
            if cap:
                aud_med.startTransmit(cap)
            pb = adm.getPlaybackDevMedia()
            if pb:
                pb.startTransmit(aud_med)
            if self.on_log:
                self.on_log("Audio connected (call ↔ sound device)")
        except Exception as e:
            err = str(e).strip() or getattr(e, "reason", None) or repr(e)
            sys.stderr.write("Audio connection error: %s\n" % err)
            if self.on_log:
                self.on_log("Audio connect failed: %s" % err)

    def start(self):
        """Start engine (call from main thread; worker runs in background)."""
        if not pj:
            return False
        try:
            self._run_worker()
            self._running = True
            self._worker_thread = threading.Thread(target=self._event_worker, daemon=True)
            self._worker_thread.start()
            return True
        except Exception as e:
            sys.stderr.write("SIP engine start error: %s\n" % e)
            return False

    def stop(self):
        """Shutdown endpoint."""
        self._running = False
        if self._worker_thread:
            self._worker_thread.join(timeout=1.0)
        if self._ep:
            try:
                self._ep.libDestroy()
            except Exception:
                pass
            self._ep = None
        self._accounts.clear()
        self._calls.clear()

    def set_account(self, acc_config):
        """Use a single account: unregister others and register this one.
        acc_config: dict with uri, password, registrar, use_tls.
        """
        uri = acc_config.get("uri") or ""
        if not uri:
            return False
        
        # Clear BLF buddies before account shutdown (must delete before account)
        with self._lock:
            self._blf_buddies.clear()
        # Unregister and remove all existing accounts before adding the new one
        with self._lock:
            for u, (ah, _) in list(self._accounts.items()):
                try:
                    ah.setRegistration(False)  # Send REGISTER Expires: 0
                    ah.delAccount()
                except Exception:
                    pass
            self._accounts.clear()
            # Clear calls when switching accounts
            for c in list(self._calls.values()):
                try:
                    c.hangup(pj.CallOpParam())
                except Exception:
                    pass
            self._calls.clear()

        cfg = pj.AccountConfig()
        # Normalize URI: remove existing scheme, add appropriate one
        uri_clean = uri.strip()
        if uri_clean.startswith("sips://"):
            uri_clean = uri_clean[7:]
        elif uri_clean.startswith("sip://"):
            uri_clean = uri_clean[6:]
        elif uri_clean.startswith("sips:"):
            uri_clean = uri_clean[5:]
        elif uri_clean.startswith("sip:"):
            uri_clean = uri_clean[4:]
        
        # Set account URI with appropriate scheme
        use_tls = acc_config.get("use_tls", False)
        cfg.idUri = ("sips:" if use_tls else "sip:") + uri_clean
        
        # Set registrar URI
        registrar = acc_config.get("registrar") or ""
        if registrar:
            # Normalize registrar URI
            reg_clean = registrar.strip()
            if reg_clean.startswith("sips://"):
                reg_clean = reg_clean[7:]
            elif reg_clean.startswith("sip://"):
                reg_clean = reg_clean[6:]
            elif reg_clean.startswith("sips:"):
                reg_clean = reg_clean[5:]
            elif reg_clean.startswith("sip:"):
                reg_clean = reg_clean[4:]
            cfg.regConfig.registrarUri = ("sips:" if use_tls else "sip:") + reg_clean
        else:
            # Derive registrar from account URI (domain part)
            if "@" in uri_clean:
                domain = uri_clean.split("@", 1)[1]
                cfg.regConfig.registrarUri = ("sips:" if use_tls else "sip:") + domain
            else:
                cfg.regConfig.registrarUri = ""
        # Extract username for auth (before @)
        username = uri_clean.split("@", 1)[0] if "@" in uri_clean else uri_clean
        
        cred = pj.AuthCredInfo()
        cred.scheme = "digest"
        cred.realm = "*"  # Will be updated by server challenge
        cred.username = username
        cred.dataType = 0  # Plain text password
        cred.data = acc_config.get("password") or ""
        cfg.sipConfig.authCreds.append(cred)

        # Bind TLS accounts to the TLS transport so ACK and all dialog traffic use TLS
        # (avoids 480 SIPS Required when server requires the whole dialog on SIPS)
        if use_tls and self._tls_transport_id is not None:
            cfg.sipConfig.transportId = self._tls_transport_id

        acc = AccountHandler(self)
        try:
            acc.create(cfg)
            with self._lock:
                self._accounts[uri] = (acc, acc_config)
        except Exception as e:
            if self.on_reg_state:
                self.on_reg_state(False, uri)
            sys.stderr.write("Account add error: %s\n" % e)
            return False
        return True

    def unregister(self):
        """Unregister current account."""
        with self._lock:
            self._blf_buddies.clear()
            for u, (ah, _) in list(self._accounts.items()):
                try:
                    ah.delAccount()
                except Exception:
                    pass
                self._accounts.clear()
            self._calls.clear()

    def set_blf(self, entries):
        """Set BLF (Busy Lamp Field) list: subscribe to dialog state for each URI.
        entries: list of dicts with 'uri' (and optionally 'label').
        Must be called when an account is registered; buddies are created on current account.
        """
        if not pj:
            return
        with self._lock:
            self._blf_buddies.clear()
            if not self._accounts:
                return
            acc = list(self._accounts.values())[0][0]
        for e in entries:
            uri = (e.get("uri") or "").strip()
            if not uri:
                continue
            try:
                cfg = pj.BuddyConfig()
                cfg.uri = uri
                cfg.subscribe = False
                cfg.subscribe_dlg_event = True
                buddy = BLFBuddyHandler(self, uri)
                buddy.create(acc, cfg)
                with self._lock:
                    self._blf_buddies.append(buddy)
                if self.on_log:
                    self.on_log("BLF: subscribed to %s" % uri)
            except Exception as err:
                if self.on_log:
                    self.on_log("BLF: subscribe failed for %s: %s" % (uri, err))

    def make_call(self, to_uri):
        """Place outgoing call from current account. to_uri e.g. sip:user@host.
        Returns (call, None) on success or (None, error_message) on failure.
        """
        with self._lock:
            if not self._accounts:
                return None, "No account selected"
            acc = list(self._accounts.values())[0][0]
        try:
            c = CallHandler(acc, pj.PJSUA_INVALID_ID, self)
            # Use default call settings (audio only, no video)
            prm = pj.CallOpParam(True)
            c.makeCall(to_uri, prm)
            with self._lock:
                self._calls[self._call_id(c)] = c
            return c, None
        except Exception as e:
            err = str(e).strip() or getattr(e, "reason", None) or repr(e)
            sys.stderr.write("Make call error: %s\n" % err)
            # If audio driver fails, switch to null device so next call can complete (no sound)
            if self._ep and ("EAUD_SYSERR" in err or "audio driver" in err.lower() or "PJMEDIA_EAUD" in err):
                try:
                    self._ep.audDevManager().setNullDev()
                    if self.on_log:
                        self.on_log("Audio driver error - switched to null audio. Try calling again (no sound).")
                    err = "Audio driver error. Switched to null audio - try calling again (no sound)."
                except Exception:
                    pass
            if self.on_log:
                self.on_log("make_call error: %s" % err)
            return None, err

    def answer_call(self, call):
        """Answer incoming call."""
        try:
            call.answer(200)
        except Exception as e:
            sys.stderr.write("Answer error: %s\n" % e)

    def _call_id(self, call):
        try:
            return call.getId()
        except Exception:
            return id(call)

    def hangup_call(self, call):
        """Hang up call."""
        try:
            call.hangup(pj.CallOpParam())
            with self._lock:
                self._calls.pop(self._call_id(call), None)
        except Exception:
            with self._lock:
                self._calls.pop(self._call_id(call), None)

    def set_mute(self, call, mute):
        """Mute/unmute call."""
        try:
            call.setMute(mute)
        except Exception:
            pass

    def get_current_call(self):
        """Return one active/ringing call if any."""
        with self._lock:
            for c in self._calls.values():
                try:
                    ci = c.getInfo()
                    if ci.state not in (CALL_STATE_NULL, CALL_STATE_DISCONNECTED):
                        return c
                except Exception:
                    pass
        return None

    def get_all_calls(self):
        """Return list of active call handlers."""
        with self._lock:
            return list(self._calls.values())

    def get_call_stats(self, call):
        """Get RTP stats for the call: rtt_ms, loss_pct, jitter_ms, mos (estimated). Returns None if unavailable."""
        if not pj or not call:
            return None
        try:
            ci = call.getInfo()
            med_idx = None
            for i, mi in enumerate(ci.media):
                if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                    med_idx = i
                    break
            if med_idx is None and ci.media:
                for i, mi in enumerate(ci.media):
                    if mi.type == pj.PJMEDIA_TYPE_AUDIO:
                        med_idx = i
                        break
            if med_idx is None:
                return None
            st = call.getStreamStat(med_idx)
            rtcp = st.rtcp
            rtt_stat = getattr(rtcp, "rttUsec", None)
            rtt_us = getattr(rtt_stat, "last", 0) or getattr(rtt_stat, "mean", 0) if rtt_stat else 0
            rtt_ms = rtt_us / 1000.0
            rx = rtcp.rxStat
            pkt = getattr(rx, "pkt", 0) or 0
            loss = getattr(rx, "loss", 0) or 0
            total = pkt + loss
            loss_pct = (100.0 * loss / total) if total > 0 else 0.0
            jitter_stat = getattr(rx, "jitterUsec", None)
            jitter_us = getattr(jitter_stat, "last", 0) or getattr(jitter_stat, "mean", 0) if jitter_stat else 0
            jitter_ms = jitter_us / 1000.0
            # Simple MOS estimate: 4.5 - delay penalty - loss penalty, clamp 1-5
            mos = 4.5 - (rtt_ms / 100.0) - (loss_pct * 0.05)
            mos = max(1.0, min(5.0, round(mos * 10) / 10.0))
            return {"rtt_ms": round(rtt_ms, 0), "loss_pct": round(loss_pct, 1), "jitter_ms": round(jitter_ms, 1), "mos": mos}
        except Exception:
            return None

    def dtmf(self, call, digit):
        """Send DTMF digit (0-9, *, #)."""
        try:
            call.dialDtmf(digit)
        except Exception:
            pass

    def hold_call(self, call):
        """Put the call on hold (re-INVITE with hold SDP)."""
        try:
            prm = pj.CallOpParam(False)
            call.setHold(prm)
        except Exception as e:
            if self.on_log:
                self.on_log("Hold error: %s" % (str(e).strip() or repr(e)))

    def unhold_call(self, call):
        """Take the call off hold (re-INVITE with UNHOLD)."""
        try:
            prm = pj.CallOpParam(False)
            prm.opt = pj.CallSetting(False)
            prm.opt.flag = getattr(pj, "PJSUA_CALL_UNHOLD", 2)
            call.reinvite(prm)
        except Exception as e:
            if self.on_log:
                self.on_log("Unhold error: %s" % (str(e).strip() or repr(e)))

    def transfer_call(self, call, dest_uri):
        """Blind (unattended) transfer: send REFER to dest_uri."""
        try:
            prm = pj.CallOpParam(False)
            call.xfer(dest_uri, prm)
        except Exception as e:
            if self.on_log:
                self.on_log("Transfer error: %s" % (str(e).strip() or repr(e)))
            return str(e).strip() or repr(e)
        return None

    def transfer_attended(self, call_to_transfer, replace_with_call):
        """Attended transfer: refer the first call to replace with the second (REFER with Replaces)."""
        try:
            prm = pj.CallOpParam(False)
            call_to_transfer.xferReplaces(replace_with_call, prm)
        except Exception as e:
            if self.on_log:
                self.on_log("Attended transfer error: %s" % (str(e).strip() or repr(e)))
            return str(e).strip() or repr(e)
        return None

    def get_audio_devices(self):
        """Return (playback_list, capture_list). Each is [(device_id, name), ...]."""
        if not pj or not self._ep:
            return [], []
        try:
            adm = self._ep.audDevManager()
            n = adm.getDevCount()
            playback, capture = [], []
            for i in range(n):
                try:
                    info = adm.getDevInfo(i)
                    name = getattr(info, "name", "") or "Device %s" % i
                    if getattr(info, "outputCount", 0) > 0:
                        playback.append((i, name))
                    if getattr(info, "inputCount", 0) > 0:
                        capture.append((i, name))
                except Exception:
                    pass
            return playback, capture
        except Exception:
            return [], []

    def set_playback_dev(self, dev_id):
        """Set default playback (speaker) device by ID."""
        if not pj or not self._ep:
            return False
        try:
            self._ep.audDevManager().setPlaybackDev(dev_id)
            return True
        except Exception:
            return False

    def set_capture_dev(self, dev_id):
        """Set default capture (mic) device by ID."""
        if not pj or not self._ep:
            return False
        try:
            self._ep.audDevManager().setCaptureDev(dev_id)
            return True
        except Exception:
            return False

    def speaker_test(self, duration_sec=3, on_done=None):
        """Play a test tone to the speaker for duration_sec. on_done() called when finished."""
        def run():
            try:
                if self._ep:
                    self._ep.libRegisterThread("speaker_test")
                adm = self._ep.audDevManager()
                spk = adm.getPlaybackDevMedia()
                tonegen = pj.ToneGenerator()
                tonegen.createToneGenerator()
                tone = pj.ToneDesc()
                tone.freq1 = 440
                tone.freq2 = 0
                tone.on_msec = 200
                tone.off_msec = 100
                tones = pj.ToneDescVector()
                tones.append(tone)
                tonegen.play(tones, True)
                tonegen.startTransmit(spk)
                import time
                time.sleep(min(duration_sec, 10))
                tonegen.stop()
            except Exception as e:
                err = str(e).strip() or getattr(e, "reason", None) or repr(e)
                if self.on_log:
                    self.on_log("Speaker test error: %s" % err)
            finally:
                if on_done:
                    on_done()

        t = threading.Thread(target=run, daemon=True)
        t.start()

    def mic_test(self, duration_sec=3, on_done=None):
        """Loop mic to speaker for duration_sec. on_done() called when finished."""
        def run():
            try:
                if self._ep:
                    self._ep.libRegisterThread("mic_test")
                adm = self._ep.audDevManager()
                cap = adm.getCaptureDevMedia()
                spk = adm.getPlaybackDevMedia()
                cap.startTransmit(spk)
                import time
                time.sleep(min(duration_sec, 10))
                cap.stopTransmit(spk)
            except Exception as e:
                err = str(e).strip() or getattr(e, "reason", None) or repr(e)
                if self.on_log:
                    self.on_log("Mic test error: %s" % err)
            finally:
                if on_done:
                    on_done()

        t = threading.Thread(target=run, daemon=True)
        t.start()


def pjsua2_available():
    return pj is not None
