"""TX arbiter (spec §2.13, E6, F18, F19): at most one TX owner (I3).

The owner is a connection (a WebSocket, or a WSJT-X bridge client: backend/bridge.py) or EXTERNAL
(-1, "FRONT PANEL": a radio that reports TX was keyed outside TeleRig). An owner holds claims from
{ptt, cw, tune, vox}; the mic is NOT a claim
(F12). Runs only on the event-loop thread: every method is called from the loop (WS messages,
HTTP handlers, the watchdog, and radio-state pushes scheduled with call_soon_threadsafe).

The arbiter is the only thing that starts or stops TX. It calls the radio primitives itself:
  start: ptt -> set_ptt(True); cw -> send_cw(text, wpm); vox -> set_rx_func("vox", True);
         tune -> tune_atu()
  stop:  set_ptt(False); stop_cw(); set_rx_func("vox", False); tune has none
so a value that only looks like "off" can never key the radio (I20).

Rules (see the table in §2.13):
  * A claim the owner already holds is a no-op: no radio call, so the radio's own failsafe deadline
    is never pushed back (F19, I17). tx_hb never calls the radio (I16).
  * The arbiter keeps its OWN monotonic session clock: a session holding ptt or vox is
    force-released SESSION_MAX (120 s) after it started, whatever the radio deadline says; cw and
    tune claims have caps (I18). A cap ends ITS claim only (ptt and vox stay on the session
    clock): CW still sending at its cap is stopped, and the tune claim (tuning is not observable)
    simply ends there. The session ends at a cap only when nothing else is held.
  * One SSTV picture (a ptt claim that names its mode, args {"sstv": id}, from the page's Send
    tab) starts a session with its own limit instead of SESSION_MAX: its time on the air + 5 s
    (backend/sstv.py), at most the radio's ptt_hold_max (3 s inside its 3-minute hardware TOT).
    The handler's failsafe gets the same hold (set_ptt(True, hold=...)), clamped again by the
    handler itself. A picture that doesn't fit the radio starts nothing (`failed`); a picture claim
    added to a session already under way is a plain ptt claim on that session's clock.
  * The owner's socket closing or being kicked (the server's kick hook; the watchdog checks it
    too), or 6 s without tx_hb from the owner, releases (I5, I7). A kicked connection's tx_hb is
    ignored. A connection may carry its own limit in `hb_timeout` (a bridge client: every command
    it sends is its heartbeat, and its limit is the admin's "silence" setting); a missing or
    invalid value means the 6 s default, never "no limit".
  * Reconciliation: a claim not shown in radio state for 1 s after the key grace is dropped (the
    radio failsafe, a finished CW message, a start that silently did nothing).
  * EXTERNAL needs 0.6 s of persistent radio TX while idle; a session the radio ended is reattached
    to its still-open connection if TX shows again within 5 s (AR-47). Only a session whose ptt or
    cw claim the RADIO ended (reconciliation) qualifies: TX that shows after a tune claim lapsed at
    its cap is never the TUNE owner's, so it is FRONT PANEL (F6).
  * Every transition to IDLE sets a release grace (2 s, +1.5 s after CW) (AR-10).
  * force_release() unkeys only what the app keyed unless explicit (STOP, shutdown), each step in
    its own try (I4), and flushes buffered TX audio (AR-18). A ptt / cw claim the app keyed is
    always unkeyed, whatever the radio's last poll said (a Yaesu TX; answer in flight when TX1; went
    out reads TX0 for one poll period); the polled state only decides for what the app did NOT key.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Callable, Optional

from . import sstv
from .radio import PTT_TIMEOUT, cw_duration, ptt_hold_max

EXTERNAL = -1
FRONT_PANEL = "FRONT PANEL"
CLAIMS = ("ptt", "cw", "tune", "vox")
STATE_KEY = {"ptt": "ptt", "cw": "cw_tx", "vox": "vox"}      # tune is not observable
KEY_GRACE = 1.5
RELEASE_GRACE = 2.0
RELEASE_GRACE_CW = 1.5            # extra after a session that held cw (Yaesu break-in delay)
ABSENT_DROP = 1.0
EXT_CONFIRM = 0.6
EXT_END = 1.0
REATTACH_WINDOW = 5.0
HB_TIMEOUT = 6.0
SESSION_MAX = float(PTT_TIMEOUT)
CW_CAP_EXTRA = 5.0
CW_CAP_MAX = 300.0
TUNE_CAP = 20.0
LAG_GAP = 1.5                     # a watchdog gap above this is a loop stall
LAG_GRACE = 2.0                   # heartbeats get this long to be processed after a stall


def _noop(*a, **k) -> None:
    return None


class TxArbiter:
    def __init__(self, radio_getter: Callable[[], Any], *,
                 send_to: Callable[[Any, dict], None] = _noop,
                 broadcast: Callable[[dict], None] = _noop,
                 audit: Callable[..., None] = _noop,
                 flush_audio: Callable[[], None] = _noop,
                 conn_alive: Optional[Callable[[Any], bool]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = _noop):
        self.radio_getter = radio_getter
        self.send_to = send_to
        self.broadcast = broadcast
        self.audit = audit
        self.flush_audio = flush_audio
        self.conn_alive = conn_alive or (lambda c: c is not None and not getattr(c, "closing", False))
        self.clock = clock
        self.log = log
        # state
        self.owner: Optional[int] = None          # conn id | EXTERNAL | None
        self.owner_conn: Any = None
        self.owner_user: Optional[str] = None
        self.claims: set = set()
        self.ever: set = set()                    # claims held at any time in this session
        self.session_start = 0.0
        self.session_wall = 0.0
        self.session_max = SESSION_MAX            # this session's limit (one SSTV picture: its own)
        self.claim_start: dict = {}
        self.claim_cap: dict = {}
        self.key_grace_until = 0.0
        self.release_grace_until = 0.0
        self.absent_since: dict = {}
        self.ext_pending_since: Optional[float] = None
        self.ext_false_since: Optional[float] = None
        self.hb_grace_until = 0.0
        self.last_session: Optional[dict] = None
        self.tx_session: Optional[dict] = None

    # -- helpers ----------------------------------------------------------------------------------
    def radio(self):
        return self.radio_getter()

    def _state(self) -> dict:
        try:
            return self.radio().state or {}
        except Exception:
            return {}

    def _alive(self, conn) -> bool:
        try:
            return bool(self.conn_alive(conn))
        except Exception:
            return False                              # fail safe: an unknown connection is gone

    def holds(self, conn_id, claim: str) -> bool:
        return self.owner is not None and self.owner == conn_id and claim in self.claims

    def label(self) -> Optional[str]:
        if self.owner is None:
            return None
        return FRONT_PANEL if self.owner == EXTERNAL else self.owner_user

    def snapshot(self, now: Optional[float] = None) -> dict:
        if self.owner is None:
            return {"owner": None}
        now = self.clock() if now is None else now
        return {"owner": {"conn": self.owner, "user": self.label(), "claims": sorted(self.claims),
                          "elapsed_ms": max(0, int((now - self.session_start) * 1000)),
                          "external": self.owner == EXTERNAL}}

    def frame(self, req: Optional[dict] = None) -> dict:
        f = {"type": "tx", **self.snapshot()}
        if req is not None:
            f["req"] = req
        return f

    def _bcast(self, req: Optional[dict] = None) -> None:
        try:
            self.broadcast(self.frame(req))
        except Exception as e:
            self.log(f"TeleRig: tx broadcast failed: {e!r}")

    def _audit(self, ev: str, **fields) -> None:
        try:
            self.audit(ev, **fields)
        except Exception:
            pass

    def _cw_cap(self, args: Optional[dict]) -> float:
        """cw_duration(the text the radio really sends, its wpm) + 5 s, at most 300 s."""
        from .radio import CW_CIV_CHARS, CW_YAESU_CHARS
        text = str((args or {}).get("text", ""))
        wpm = (args or {}).get("wpm", 18)
        try:
            wpm = int(wpm)
        except (TypeError, ValueError):
            wpm = 18
        prof = getattr(self.radio(), "profile", None)
        if getattr(prof, "cw_send", "") == "line":
            msg = "".join(c for c in text.upper() if c in CW_YAESU_CHARS)[:80]
            wpm = max(4, min(60, wpm))
        else:
            msg = "".join(c for c in text if c in CW_CIV_CHARS)[:30]
            wpm = max(6, min(48, wpm))
        return min(CW_CAP_MAX, cw_duration(msg, wpm) + CW_CAP_EXTRA)

    def _hold_max(self) -> float:
        """The radio's longest keying (radio.ptt_hold_max); SESSION_MAX when it can't be read."""
        try:
            return float(ptt_hold_max(getattr(self.radio(), "profile", None)))
        except Exception:
            return SESSION_MAX

    def _ptt_hold(self, args: Optional[dict]) -> Optional[float]:
        """One SSTV picture's TX limit (args {"sstv": mode id}): sstv.cap_s against the radio's
        ptt_hold_max. None for a plain ptt claim, or a picture that doesn't fit the radio."""
        mode = (args or {}).get("sstv")
        if mode is None:
            return None
        try:
            return sstv.cap_s(mode, self._hold_max())
        except Exception:
            return None

    def _start_primitive(self, k: str, args: Optional[dict], hold: Optional[float] = None) -> None:
        r = self.radio()
        if k == "ptt":
            if hold is None:
                r.set_ptt(True)
            else:
                r.set_ptt(True, hold=hold)
        elif k == "cw":
            r.send_cw(str(args["text"]), int(args["wpm"]))
        elif k == "vox":
            r.set_rx_func("vox", True)
        elif k == "tune":
            r.tune_atu()

    def _stop_primitive(self, k: str) -> None:
        r = self.radio()
        if k == "ptt":
            r.set_ptt(False)
        elif k == "cw":
            r.stop_cw()
        elif k == "vox":
            r.set_rx_func("vox", False)
        # tune: nothing reliable to send

    def _flush(self) -> None:
        try:
            self.flush_audio()
        except Exception:
            pass

    def _session_fields(self) -> dict:
        s = self.tx_session or {}
        return {k: s.get(k) for k in ("user", "conn", "ip", "kind", "radio", "freq", "mode")}

    def _set_cap(self, k: str, now: float, args: Optional[dict]) -> None:
        if k == "cw":
            self.claim_cap[k] = now + self._cw_cap(args)
        elif k == "tune":
            self.claim_cap[k] = now + TUNE_CAP
        else:
            self.claim_cap.pop(k, None)

    # -- claims ----------------------------------------------------------------------------------
    def claim(self, conn, k: str, args: Optional[dict] = None, seq: Optional[int] = None) -> str:
        """K(conn, k). Returns started | added | noop | busy | failed."""
        now = self.clock()
        req = {"conn": conn.id, "seq": seq} if seq is not None else None
        if self.owner is None:
            hold = self._ptt_hold(args) if k == "ptt" else None
            if k == "ptt" and (args or {}).get("sstv") is not None and hold is None:
                # A picture this radio can't stay keyed for to its end starts nothing (the policy
                # refuses it first; this is the arbiter's own check).
                self.send_to(conn, {"type": "error", "code": "tx_failed",
                                    "message": "this SSTV mode is longer than the radio's TX limit"})
                self._bcast(req)
                return "failed"
            st = self._state()
            self.owner, self.owner_conn, self.owner_user = conn.id, conn, conn.user
            self.claims, self.ever = {k}, {k}
            self.session_start = self.claim_start[k] = now
            self.session_wall = time.time()
            self.session_max = hold if hold is not None else SESSION_MAX
            self._set_cap(k, now, args)
            self.absent_since = {}
            self.ext_pending_since = self.ext_false_since = None
            conn.last_hb = now
            self.tx_session = {"user": conn.user, "conn": conn.id, "ip": getattr(conn, "ip", None),
                               "kind": "sstv" if hold is not None else k, "radio": st.get("radio"),
                               "freq": st.get("freq"), "mode": st.get("mode_name")}
            try:
                self._start_primitive(k, args, hold)
            except Exception as e:
                self.log(f"TeleRig: TX start ({k}) failed: {e!r}")
                self._reset_owner()
                self.send_to(conn, {"type": "error", "code": "tx_failed", "message": f"{k} could not start"})
                self._bcast(req)
                return "failed"
            self.key_grace_until = now + KEY_GRACE
            self._bcast(req)
            self._audit("tx_start", **self._session_fields())
            return "started"
        if self.owner == conn.id:
            if k in self.claims:                         # F19 / I17: no radio call, deadline untouched
                self.send_to(conn, self.frame(req))
                return "noop"
            self.claims.add(k)
            self.ever.add(k)
            self.claim_start[k] = now
            self._set_cap(k, now, args)
            self.absent_since.pop(k, None)
            try:
                self._start_primitive(k, args)
            except Exception as e:
                self.log(f"TeleRig: TX start ({k}) failed: {e!r}")
                self.claims.discard(k)
                self.claim_cap.pop(k, None)
                self._bcast(req)
                return "failed"
            self.key_grace_until = now + KEY_GRACE        # the session clock is NOT restarted
            self._bcast(req)
            return "added"
        busy = {"type": "busy", "action": {"ptt": "ptt", "cw": "cw_tx", "tune": "tune_atu",
                                           "vox": "rx_func"}.get(k, k), "owner": self.label()}
        if req is not None:
            busy["req"] = req
        self.send_to(conn, busy)
        return "busy"

    def release(self, conn, k: str) -> bool:
        """The owner's own unkey of claim k (ptt false / cw_stop / vox off, auto or not)."""
        if self.owner is None or self.owner != conn.id:
            return False
        if k not in self.claims:
            # The owner stops something it did not claim: run the stop primitive only if the radio
            # shows it active (never a state change of the session).
            key = STATE_KEY.get(k)
            if key and self._state().get(key):
                try:
                    self._stop_primitive(k)
                except Exception:
                    pass
            return False
        try:
            self._stop_primitive(k)
        except Exception as e:
            self.log(f"TeleRig: TX stop ({k}) failed: {e!r}")
        self._drop(k)
        if not self.claims:
            self._end_session("owner", None)
        else:
            self._bcast()
        return True

    def _drop(self, k: str) -> None:
        self.claims.discard(k)
        self.claim_start.pop(k, None)
        self.claim_cap.pop(k, None)
        self.absent_since.pop(k, None)
        if not (self.claims & {"ptt", "vox"}):
            self._flush()                                  # I10 / AR-18

    def stop(self, by, explicit: bool = True) -> bool:
        """tx_stop (WS) or POST /api/tx_stop from `by` (a connection or an HTTP principal).
        The owner stops its own claims; anyone else (operator+) force-releases the owner, including
        the FRONT PANEL (explicit: unkeys what the radio shows); with no owner the radio's ptt and
        cw stop primitives run. Returns True when something was stopped."""
        by_id = getattr(by, "id", None)              # a Conn has an id; an HTTP Principal has none
        by_user = getattr(by, "user", None)
        if self.owner is None:
            st = self._state()
            was = bool(st.get("ptt") or st.get("cw_tx"))
            self.stop_idle(("ptt", "cw"))
            return was
        if by_id is not None and self.owner == by_id:
            for k in list(self.claims):
                try:
                    self._stop_primitive(k)
                except Exception:
                    pass
                self._drop(k)
            self._end_session("owner", None)
            return True
        owner = self.label()
        self.force_release("stopped", by=by_user, explicit=explicit)
        self._audit("tx_stop_forced", by=by_user, owner=owner, conn=by_id)
        return True

    def stop_idle(self, claims) -> None:
        """noowner_stop: nobody owns TX, so run the radio's stop primitive(s) as v1 did."""
        for k in claims:
            try:
                self._stop_primitive(k)
            except Exception:
                pass
        if set(claims) & {"ptt", "vox"}:
            self._flush()

    def on_tx_hb(self, conn) -> None:
        """tx_hb: refresh the owner's heartbeat. Never calls the radio (I16); a no-op otherwise.
        A kicked connection (`closing`: its close frame is out) never keeps a session alive, even
        when its client ignores the close and keeps sending tx_hb."""
        if getattr(conn, "closing", False):
            return
        if self.owner is not None and self.owner == getattr(conn, "id", None):
            conn.last_hb = self.clock()

    def on_conn_closed(self, conn) -> None:
        """The connection's socket closed, or the server kicked it (Hub.on_kick): if it owns TX,
        release now (§2.13 "c's socket closes -> force_release immediately", I5, I7). Connection
        ids are never reused, so this can never end another connection's session."""
        if self.owner is not None and self.owner == getattr(conn, "id", None):
            self.force_release("disconnect")

    # -- force release -------------------------------------------------------------------------------
    def force_release(self, reason: str, by: Optional[str] = None, explicit: bool = False,
                      claim: Optional[str] = None) -> None:
        """Release everything. Unkeys only what the app keyed unless explicit; each step on its own
        so nothing can skip an unkey (I4). `claim` names the claim whose cap ended the session (cw);
        it rides on tx_revoked so the owner is told which limit was reached.
        A ptt / cw claim the app holds is ALWAYS stopped (like release() and stop()): the polled
        state can lag the app's own key (a Yaesu TX; answer already in flight when TX1; went out
        reads TX0 for one poll period), and trusting it here would leave the radio keyed while the
        arbiter goes IDLE (I4, I5). The polled state decides only for an explicit stop of something
        the app did not claim (a FRONT PANEL key, a stray CW)."""
        # A FRONT PANEL session's ptt was not keyed by the app: only an explicit STOP unkeys it.
        claims = set(self.claims) if self.owner != EXTERNAL else set()
        r = None
        try:
            r = self.radio()
        except Exception:
            pass
        st = getattr(r, "state", None) or {}
        try:
            if "ptt" in claims or (explicit and st.get("ptt")):
                r.set_ptt(False)
        except Exception as e:
            self.log(f"TeleRig: unkey failed: {e!r}")
        try:
            if "cw" in claims or (explicit and st.get("cw_tx")):
                r.stop_cw()
        except Exception as e:
            self.log(f"TeleRig: CW stop failed: {e!r}")
        try:
            if "vox" in claims or (explicit and st.get("vox")):
                r.set_rx_func("vox", False)
        except Exception as e:
            self.log(f"TeleRig: VOX off failed: {e!r}")
        self._flush()
        if self.owner is None:
            return
        try:
            oc = self.owner_conn
            if self.owner != EXTERNAL and oc is not None and self.conn_alive(oc):
                rv = {"type": "tx_revoked", "reason": reason, "by": by}
                if claim:
                    rv["claim"] = claim
                self.send_to(oc, rv)
        except Exception:
            pass
        self.claims = set()
        self._end_session(reason, by)

    def _reset_owner(self) -> None:
        self.owner = None
        self.owner_conn = None
        self.owner_user = None
        self.claims = set()
        self.ever = set()
        self.session_max = SESSION_MAX
        self.claim_start = {}
        self.claim_cap = {}
        self.absent_since = {}
        self.tx_session = None

    def _end_session(self, reason: str, by: Optional[str], radio_ended=()) -> None:
        """`radio_ended`: the claims whose end the RADIO reported (reconciliation) and that ended the
        session. Only a session that ended with ptt or cw among them may be reattached (AR-47)."""
        now = self.clock()
        had_cw = "cw" in self.ever
        st = self._state()
        fields = self._session_fields()
        if self.owner == EXTERNAL:
            fields = {"user": FRONT_PANEL, "conn": None, "ip": None, "kind": "external",
                      "radio": (self.tx_session or {}).get("radio"), "freq": (self.tx_session or {}).get("freq"),
                      "mode": (self.tx_session or {}).get("mode")}
        dur = round(max(0.0, now - self.session_start), 2)
        self._audit("tx_end", **fields, freq_end=st.get("freq"),
                    start=_iso(self.session_wall), dur_s=dur, reason=reason, by=by,
                    claims=sorted(self.ever))
        self.last_session = {"conn": self.owner_conn if self.owner != EXTERNAL else None,
                             "claims": set(self.ever), "start": self.session_start,
                             "session_max": self.session_max,
                             "start_wall": self.session_wall, "ended_at": now, "reason": reason,
                             "radio_ended": set(radio_ended or ()),
                             "tx_session": dict(self.tx_session or {})}
        self._reset_owner()
        self.release_grace_until = now + RELEASE_GRACE + (RELEASE_GRACE_CW if had_cw else 0.0)
        self.ext_pending_since = None
        self.ext_false_since = None
        self._flush()
        self._bcast()

    # -- radio state + watchdog ------------------------------------------------------------------
    def on_radio_state(self, snap: dict) -> None:
        """A radio state push (scheduled onto the loop by the hub)."""
        try:
            self._observe(self.clock(), snap or {})
        except Exception as e:
            self.log(f"TeleRig: arbiter state update failed: {e!r}")

    def _observe(self, now: float, st: dict) -> None:
        ptt = bool(st.get("ptt"))
        if self.owner is None:
            if not ptt:
                self.ext_pending_since = None
            elif self.ext_pending_since is None and now > self.release_grace_until:
                self.ext_pending_since = now
        elif self.owner == EXTERNAL:
            if ptt:
                self.ext_false_since = None
            elif self.ext_false_since is None:
                self.ext_false_since = now
        else:
            for k in list(self.claims):
                key = STATE_KEY.get(k)
                if key is None:
                    continue
                if st.get(key):
                    self.absent_since.pop(k, None)
                elif now >= self.key_grace_until and self.absent_since.get(k) is None:
                    self.absent_since[k] = now

    def tick(self, now: Optional[float] = None, gap: Optional[float] = None) -> None:
        """One watchdog tick (every 0.5 s). `gap` is the time since the previous tick: above
        LAG_GAP the loop stalled, so heartbeats buffered in the sockets get LAG_GRACE to be
        processed first. The session clock and the claim caps are NEVER suspended."""
        now = self.clock() if now is None else now
        lag = gap is not None and gap > LAG_GAP
        if lag:
            self.hb_grace_until = now + LAG_GRACE
        st = self._state()
        self._observe(now, st)
        if self.owner is None:
            if self.ext_pending_since is not None and st.get("ptt") \
                    and now - self.ext_pending_since >= EXT_CONFIRM:
                self._external_or_reattach(now, st)
            return
        if self.owner == EXTERNAL:
            if self.ext_false_since is not None and not st.get("ptt") \
                    and now - self.ext_false_since >= EXT_END:
                self._end_session("radio", None)
            return
        # owned by a connection
        oc = self.owner_conn
        if oc is not None and not self._alive(oc):
            # Backstop for the kick hook: the owner's socket was kicked or is gone (I5, I7).
            self.force_release("disconnect")
            return
        if (self.claims & {"ptt", "vox"}) and now >= self.session_start + self.session_max:
            picture = (self.tx_session or {}).get("kind") == "sstv"
            self.force_release("timeout", claim="sstv" if picture else None)   # tx_revoked names the limit
            return
        expired = [k for k in ("cw", "tune")
                   if k in self.claims and now >= self.claim_cap.get(k, float("inf"))]
        if expired and self._caps_expired(expired, st):
            return
        if not lag and now > self.hb_grace_until and oc is not None \
                and now - getattr(oc, "last_hb", 0.0) > hb_limit(oc):
            self._audit("tx_hb_timeout", user=self.owner_user, conn=self.owner,
                        silent_s=round(now - getattr(oc, "last_hb", 0.0), 1))
            self.force_release("heartbeat")
            return
        dropped = []
        for k in list(self.claims):
            since = self.absent_since.get(k)
            if since is not None and now - since >= ABSENT_DROP:
                self._drop(k)
                dropped.append(k)
        if dropped:
            if not self.claims:
                # reason `done` when a CW message finished on its own, else `radio`
                self._end_session("done" if dropped == ["cw"] else "radio", None, radio_ended=dropped)
            else:
                self._bcast()

    def _caps_expired(self, expired: list, st: dict) -> bool:
        """Per-claim caps (I18, F19): a cw or tune cap ends THAT claim only; ptt and vox stay on the
        120 s session clock, so a TUNE pressed before PTT can never cut the over that follows it.
          * cw: a message still sending at duration + 5 s is a fault, so CW is stopped (when the
            radio shows it) and the claim dropped;
          * tune: tuning is not observable and has no stop primitive, so the cap is the normal end of
            the claim: it is dropped, nothing is sent to the radio.
        With nothing else held the session ends: `timeout` (tx_revoked naming cw) when the CW cap
        ended it, `done` when only the tune claim lapsed. A lapsed tune claim is not a radio-ended
        claim, so TX the radio shows afterwards is FRONT PANEL, never reattached to the TUNE owner
        (F6: a front-panel or physical-mic over is never handed to a browser that could unkey it).
        Returns True when the session ended."""
        if not (self.claims - set(expired)):
            if "cw" in expired:
                self.force_release("timeout", claim="cw")
            else:
                self._drop("tune")
                self._end_session("done", None)
            return True
        for k in expired:
            if k == "cw":
                try:
                    if st.get("cw_tx"):
                        self.radio().stop_cw()
                except Exception as e:
                    self.log(f"TeleRig: CW stop at its cap failed: {e!r}")
            self._drop(k)
        self._bcast()
        return False

    def _external_or_reattach(self, now: float, st: dict) -> None:
        """Persistent radio TX while IDLE: reattach (AR-47) or FRONT PANEL. Reattach is only for the
        cases AR-47 covers, a misread poll that ended the app's own ptt and the Yaesu CW break-in
        hang: the last session must have ended because the radio stopped showing its ptt or cw
        claim (`radio_ended`), within 5 s, with its connection still open. Anything else (a tune
        claim that lapsed at its cap, a vox session, an owner release, a forced release) is FRONT
        PANEL, which no browser drop, heartbeat or session clock can unkey (F6)."""
        ls = self.last_session
        if ls and ls.get("conn") is not None and ls.get("reason") in ("radio", "done") \
                and (set(ls.get("radio_ended") or ()) & {"ptt", "cw"}) \
                and now - ls.get("ended_at", -1e9) < REATTACH_WINDOW and self.conn_alive(ls["conn"]):
            c = ls["conn"]
            self.owner, self.owner_conn, self.owner_user = c.id, c, c.user
            self.claims, self.ever = {"ptt"}, set(ls.get("claims") or ()) | {"ptt"}
            self.session_start = ls["start"]                 # the session clock continues
            self.session_max = min(self._hold_max(), float(ls.get("session_max") or SESSION_MAX))
            self.session_wall = ls.get("start_wall", time.time())
            self.claim_start = {"ptt": now}
            self.claim_cap = {}
            self.absent_since = {}
            self.key_grace_until = now + KEY_GRACE
            self.tx_session = dict(ls.get("tx_session") or {}) or {"user": c.user, "conn": c.id}
            c.last_hb = max(getattr(c, "last_hb", 0.0), now)
            self.ext_pending_since = None
            self._audit("tx_reattach", user=c.user, conn=c.id, radio=st.get("radio"), freq=st.get("freq"))
            self._bcast()
            return
        self.owner, self.owner_conn, self.owner_user = EXTERNAL, None, FRONT_PANEL
        self.claims, self.ever = {"ptt"}, {"ptt"}
        self.session_start = now
        self.session_max = SESSION_MAX
        self.session_wall = time.time()
        self.claim_start, self.claim_cap, self.absent_since = {"ptt": now}, {}, {}
        self.ext_pending_since = self.ext_false_since = None
        self.tx_session = {"user": FRONT_PANEL, "conn": None, "ip": None, "kind": "external",
                           "radio": st.get("radio"), "freq": st.get("freq"), "mode": st.get("mode_name")}
        self._audit("tx_start", **self.tx_session)
        self._bcast()


def hb_limit(conn) -> float:
    """The owner's heartbeat limit in seconds: the connection's own `hb_timeout` when it is a
    sane positive number (at most SESSION_MAX), else HB_TIMEOUT. Fails safe to the default."""
    v = getattr(conn, "hb_timeout", None)
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= SESSION_MAX:
        return float(v)
    return HB_TIMEOUT


def _iso(t: float) -> str:
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(t or time.time(), timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


async def watchdog_loop(arb: TxArbiter, extra: Optional[Callable] = None, *, interval: float = 0.5,
                        clock: Callable[[], float] = time.monotonic,
                        log: Callable[[str], None] = _noop) -> None:
    """The server watchdog body: every `interval` the arbiter tick (with the measured gap, so a
    loop stall is recognised, AR-16), then `extra()` (sync or async: store reloads, locked re-check,
    session revalidation). A failing step is logged and the loop goes on."""
    prev = clock()
    while True:
        await asyncio.sleep(interval)
        now = clock()
        gap, prev = now - prev, now
        try:
            arb.tick(now, gap=gap)
        except Exception as e:
            log(f"TeleRig: watchdog TX tick failed: {e!r}")
        if extra is not None:
            try:
                r = extra()
                if inspect.isawaitable(r):
                    await r
            except Exception as e:
                log(f"TeleRig: watchdog tick failed: {e!r}")
