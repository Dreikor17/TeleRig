"""TX arbiter unit tests — run: python -m backend.txarb_test

In-process, with a fake clock and a fake (recording) radio: the arbiter state machine (§2.13),
T41 (idempotent claims against a REAL Radio + SimTransport: the radio deadline is never pushed
back), T46 (EXTERNAL debounce, flap, CW grace, reattach only after a radio-ended ptt / cw claim, never
after a lapsed tune claim), T61 (session clock and caps), T62 (phantom claim dropped by
reconciliation), T63 (release grace on every IDLE transition + audio flush), T68 (lag-aware watchdog,
a raising tick keeps the loop alive), and force_release always unkeying an app-keyed claim even when
the radio's last poll is stale (a fake radio and the real Yaesu CAT parser).

Only the built-in simulators are ever connected (T41: Icom CI-V; the stale-poll case: Yaesu CAT), in
this process; no server, socket, sound card or file is touched.
"""
import asyncio
import threading
import time

from backend import policy, profiles, txarb
from backend.audio_router import AudioRouter
from backend.hostaudio import HostAudio
from backend.hub import CTL_MAX, Conn, Hub
from backend.lan import LanTransport
from backend.radio import Radio
from backend.transport import SimTransport

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeRadio:
    """Records the start/stop primitives; `state` is what the arbiter reads."""

    def __init__(self, pid="ic9700", cw_works=True):
        self.profile = profiles.PROFILES[pid]
        self.state = {"connected": True, "radio": pid, "freq": 145500000, "mode_name": "FM",
                      "ptt": False, "cw_tx": False, "vox": 0, "cw_tx_ready": True}
        self.calls = []
        self.cw_works = cw_works

    def set_ptt(self, tx, hold=None):
        self.calls.append(("set_ptt", tx) if hold is None else ("set_ptt", tx, hold))
        self.state["ptt"] = bool(tx)

    def send_cw(self, text, wpm):
        self.calls.append(("send_cw", text, wpm))
        if self.cw_works:
            self.state["cw_tx"] = True

    def stop_cw(self):
        self.calls.append(("stop_cw",))
        self.state["cw_tx"] = False

    def set_rx_func(self, name, on):
        self.calls.append(("set_rx_func", name, on))
        self.state[name] = 1 if on else 0

    def tune_atu(self):
        self.calls.append(("tune_atu",))


class Env:
    def __init__(self, radio=None, clock=None):
        self.clock = clock or Clock()
        self.radio = radio or FakeRadio()
        self.sent, self.bcast, self.audits, self.flushes = [], [], [], [0]
        self.arb = txarb.TxArbiter(lambda: self.radio, send_to=lambda c, f: self.sent.append((c.id, f)),
                                   broadcast=lambda f: self.bcast.append(f),
                                   audit=lambda ev, **kw: self.audits.append((ev, kw)),
                                   flush_audio=lambda: self.flushes.__setitem__(0, self.flushes[0] + 1),
                                   clock=self.clock)

    def conn(self, cid, user=None, role="operator"):
        c = Conn(cid, None, user=user or f"OP{cid}", role=role)
        return c

    def tick(self, dt=0.5, gap=0.5):
        self.clock.t += dt
        self.arb.tick(gap=gap)

    def evs(self, name):
        return [kw for ev, kw in self.audits if ev == name]


# -- basics: claim, idempotent re-claim, release ----------------------------------------------------
e = Env()
a, b = e.conn(1), e.conn(2)
C("claim from IDLE -> started", e.arb.claim(a, "ptt", seq=5), "started")
C("... one set_ptt(True)", e.radio.calls, [("set_ptt", True)])
C("... tx frame names the owner and echoes req", (e.bcast[-1]["owner"]["user"], e.bcast[-1]["owner"]["claims"],
                                                 e.bcast[-1]["req"]), ("OP1", ["ptt"], {"conn": 1, "seq": 5}))
C("... audit tx_start with user/freq/mode/kind", [(k["user"], k["freq"], k["mode"], k["kind"]) for k in e.evs("tx_start")],
  [("OP1", 145500000, "FM", "ptt")])
C("... the owner's heartbeat starts now", a.last_hb, e.clock.t)
e.radio.calls.clear()
C("re-claim of a held claim -> noop (F19)", e.arb.claim(a, "ptt", seq=6), "noop")
C("... no radio call (I17)", e.radio.calls, [])
C("... the tx frame goes to the claimant only, echoing req", e.sent[-1], (1, {**e.arb.frame(), "req": {"conn": 1, "seq": 6}}))
C("another connection's claim -> busy (I3)", e.arb.claim(b, "ptt", seq=1), "busy")
C("... busy frame names the owner", (e.sent[-1][0], e.sent[-1][1]["type"], e.sent[-1][1]["owner"]), (2, "busy", "OP1"))
C("tx_hb from a non-owner is a no-op", (e.arb.on_tx_hb(b), b.last_hb), (None, 0.0))
e.clock.t += 3
e.arb.on_tx_hb(a)
C("tx_hb from the owner refreshes last_hb, no radio call (I16)", (a.last_hb, e.radio.calls), (e.clock.t, []))
e.radio.calls.clear()
C("owner releases ptt -> True", e.arb.release(a, "ptt"), True)
C("... one set_ptt(False)", e.radio.calls, [("set_ptt", False)])
C("... IDLE, release grace 2 s", (e.arb.owner, e.arb.release_grace_until), (None, e.clock.t + 2.0))
C("... tx_end reason owner with dur_s", [(k["reason"], k["dur_s"]) for k in e.evs("tx_end")], [("owner", 3.0)])
C("... buffered TX audio flushed (AR-18)", e.flushes[0] >= 1, True)
C("release by a non-owner does nothing", e.arb.release(b, "ptt"), False)

# -- stop / stop_idle / force_release ------------------------------------------------------------------
e = Env()
a, b = e.conn(1), e.conn(2)
e.radio.calls.clear()
C("explicit STOP with no owner -> set_ptt(False) + stop_cw (T46)", (e.arb.stop(b), e.radio.calls),
  (False, [("set_ptt", False), ("stop_cw",)]))
e.arb.claim(a, "ptt")
e.radio.calls.clear()
C("tx_stop from a non-owner force-releases", e.arb.stop(b), True)
C("... unkeyed", e.radio.calls, [("set_ptt", False)])
C("... the owner gets tx_revoked{reason:stopped, by:OP2}", [f for cid, f in e.sent if cid == 1 and f["type"] == "tx_revoked"],
  [{"type": "tx_revoked", "reason": "stopped", "by": "OP2"}])
C("... audit tx_stop_forced {by, owner}", [(k["by"], k["owner"]) for k in e.evs("tx_stop_forced")], [("OP2", "OP1")])
C("... tx_end reason stopped", e.evs("tx_end")[-1]["reason"], "stopped")
e.arb.claim(a, "ptt")
e.arb.claim(a, "vox")
e.radio.calls.clear()
C("tx_stop from the owner releases every claim", (e.arb.stop(a), sorted(e.radio.calls)),
  (True, [("set_ptt", False), ("set_rx_func", "vox", False)]))

class Principal:                                    # an HTTP principal: no connection id
    user = "OP2"
e.arb.claim(a, "ptt")
C("HTTP tx_stop (principal) stops another user's TX", (e.arb.stop(Principal()), e.arb.owner), (True, None))

# heartbeat / disconnect release of an owner WITHOUT a ptt claim never unkeys a front-panel key
e = Env()
a = e.conn(1)
e.arb.claim(a, "vox")
e.radio.state["ptt"] = True                          # keyed by the physical mic (VOX / front panel)
e.radio.calls.clear()
e.clock.t += 7
e.arb.tick(gap=0.5)
C("T46 heartbeat release of a vox-only owner: VOX off, NO set_ptt (F6)", e.radio.calls, [("set_rx_func", "vox", False)])
C("... audit tx_hb_timeout + tx_end reason heartbeat", (len(e.evs("tx_hb_timeout")), e.evs("tx_end")[-1]["reason"]), (1, "heartbeat"))
e = Env()
a = e.conn(1)
e.radio.state["cw_tx_ready"] = True
e.arb.claim(a, "cw", args={"text": "CQ", "wpm": 20})
e.radio.state["ptt"] = True
e.radio.calls.clear()
e.arb.on_conn_closed(a)
C("T46 disconnect release of a cw-only owner: stop_cw, NO set_ptt", e.radio.calls, [("stop_cw",)])
C("... tx_end reason disconnect", e.evs("tx_end")[-1]["reason"], "disconnect")
e = Env()
a, b = e.conn(1), e.conn(2)
e.arb.claim(a, "ptt")
e.radio.calls.clear()
e.arb.on_conn_closed(b)
C("T20 another client's drop never unkeys", (e.arb.owner, e.radio.calls), (1, []))
e.arb.on_conn_closed(a)
C("T19 the owner's drop unkeys immediately", (e.arb.owner, e.radio.calls), (None, [("set_ptt", False)]))

# A ptt / cw claim the APP keyed is always unkeyed by force_release, whatever the radio's last poll said
# (final review round 2). On a Yaesu, a TX; answer already in flight when TX1; went out is parsed after the
# key and reads TX0 for one poll period: a STOP, the owner's drop or kick, a heartbeat stall, an admin
# switch, a radio change, a locked server or shutdown in that window must still send the unkey (I4, I5).
def _stale_release(fn, k="ptt"):
    env = Env(FakeRadio("ft991a"))
    c, other = env.conn(1), env.conn(2)
    env.arb.claim(c, k, args={"text": "CQ", "wpm": 20} if k == "cw" else None)
    env.radio.state["ptt" if k == "ptt" else "cw_tx"] = False      # the stale poll answer
    env.radio.calls.clear()
    fn(env, c, other)
    return env.arb.owner, env.radio.calls


def _stale_kicked(env, c, other):
    c.closing = True
    env.tick(0.1, 0.1)


def _stale_hb(env, c, other):
    env.clock.t += 7
    env.arb.tick(gap=0.5)


_STALE_CASES = {"STOP by another operator": lambda env, c, o: env.arb.stop(o),
                "HTTP STOP": lambda env, c, o: env.arb.stop(Principal()),
                "owner's socket drop": lambda env, c, o: env.arb.on_conn_closed(c),
                "owner kicked (watchdog backstop)": _stale_kicked,
                "heartbeat stall": _stale_hb,
                "admin_switch": lambda env, c, o: env.arb.force_release("admin_switch", by="ADM", explicit=True),
                "radio_change": lambda env, c, o: env.arb.force_release("radio_change", by="OP2"),
                "radio_disconnect": lambda env, c, o: env.arb.force_release("radio_disconnect", by="OP2"),
                "server_locked": lambda env, c, o: env.arb.force_release("server_locked"),
                "shutdown": lambda env, c, o: env.arb.force_release("shutdown", explicit=True)}
C("stale TX0 right after the app keyed: every forced release still sends set_ptt(False)",
  {k: _stale_release(f) for k, f in _STALE_CASES.items()}, {k: (None, [("set_ptt", False)]) for k in _STALE_CASES})
C("... and a cw claim whose cw_tx reads False is still stopped (stop_cw is harmless)",
  {k: _stale_release(f, "cw") for k, f in _STALE_CASES.items()}, {k: (None, [("stop_cw",)]) for k in _STALE_CASES})
e = Env(FakeRadio("ft991a"))
a = e.conn(1)
e.arb.claim(a, "vox")
e.radio.state["ptt"] = False
e.radio.calls.clear()
e.arb.force_release("heartbeat")
C("... a claim the app did NOT key is still never unkeyed by a non-explicit release (F6)", e.radio.calls,
  [("set_rx_func", "vox", False)])

# The same with the REAL Yaesu CAT parser on the built-in simulator (no port, socket or radio; the
# simulator's TX is a string field). The poll is stopped so the stale answer can be placed exactly: a
# "TX0;" delivered after TX1; (as a reply already in flight is) makes state.ptt False. A STOP or the
# owner's drop right then must leave the simulator unkeyed (before the fix it stayed TX1, and 2.6 s later
# the arbiter called TeleRig's own carrier FRONT PANEL).
from backend.transport import YaesuSimTransport  # noqa: E402
from backend.yaesu import YaesuRadio  # noqa: E402

_yprof = profiles.PROFILES["ft991a"]
_ytp = YaesuSimTransport(_yprof)
_yr = YaesuRadio(_yprof)
_yr.connect(_ytp, _yprof)
try:
    C("Yaesu stale TX0: the in-process radio is the CAT simulator", type(_yr._tp).__name__, "YaesuSimTransport")
    _yr._poll_stop.set()
    _yr._poll_thread.join(timeout=2.0)
    for how in ("STOP by another operator", "owner's socket drop", "heartbeat stall"):
        env = Env(radio=_yr)
        c, other = env.conn(1), env.conn(2)
        C(f"Yaesu stale TX0 ({how}): OP1 keys", (env.arb.claim(c, "ptt"), _ytp.tx), ("started", "1"))
        _yr._on_bytes(b"TX0;")                     # the answer to a TX; query sent before the key
        C("... the real parser now reads receive (state.ptt False) while the simulator is keyed",
          (_yr.state["ptt"], _ytp.tx), (False, "1"))
        {"STOP by another operator": lambda: env.arb.stop(other),
         "owner's socket drop": lambda: env.arb.on_conn_closed(c),
         "heartbeat stall": lambda: _stale_hb(env, c, other)}[how]()
        C(f"... {how}: IDLE and the simulator is unkeyed (TX0;)", (env.arb.owner, _ytp.tx), (None, "0"))
        for _ in range(8):                         # 4 s of watchdog, a TX; read before each tick (the poll)
            _yr._send("TX;")                       # the simulator answers at once: state.ptt = its TX
            env.arb.on_radio_state(dict(_yr.state))
            env.tick()
        C("... 4 s later still IDLE: TeleRig's own carrier is not left up as FRONT PANEL",
          (env.arb.owner, _ytp.tx, _yr.state["ptt"]), (None, "0", False))
finally:
    _yr.disconnect()
C("Yaesu stale TX0: simulator disconnected, unkeyed", (_yr.state["connected"], _ytp.tx), (False, "0"))

# -- T46: EXTERNAL debounce, busy, end; flap; CW grace; reattach -----------------------------------------
e = Env(FakeRadio("ft991a"))
a, b = e.conn(1), e.conn(2)
e.radio.state["ptt"] = True
e.arb.on_radio_state({"ptt": True})
e.tick(0.3, 0.3)
C("T46 radio ptt=True for 0.3 s -> still IDLE", e.arb.owner, None)
e.tick(0.4, 0.4)
C("T46 ... for 0.7 s -> EXTERNAL (FRONT PANEL)", (e.arb.owner, e.arb.label()), (txarb.EXTERNAL, "FRONT PANEL"))
C("... audit tx_start kind=external", e.evs("tx_start")[-1]["kind"], "external")
C("T46 K from anyone while EXTERNAL -> busy", (e.arb.claim(a, "ptt"), e.arb.claim(b, "ptt")), ("busy", "busy"))
ctx = type("X", (), {"arb": e.arb, "radio": e.radio, "transition": False})()
C("T46 policy: K while EXTERNAL -> busy FRONT PANEL",
  (lambda v: (v.kind, v.extra.get("owner")))(policy.evaluate(policy.WS_POLICY["ptt"], a, {"action": "ptt", "tx": True}, ctx)),
  ("busy", "FRONT PANEL"))
e.radio.calls.clear()
e.arb.force_release("heartbeat")                   # a non-explicit release never unkeys the front panel
C("non-explicit force_release of EXTERNAL never unkeys (F6)", e.radio.calls, [])
e.radio.state["ptt"] = True
e.arb.on_radio_state({"ptt": True})
e.clock.t += 3                                     # past the release grace
e.arb.on_radio_state({"ptt": True})
e.tick(0.7, 0.7)
C("EXTERNAL again", e.arb.owner, txarb.EXTERNAL)
e.radio.state["ptt"] = False
e.arb.on_radio_state({"ptt": False})
e.tick(0.5)
C("EXTERNAL: radio ptt False 0.5 s -> still EXTERNAL", e.arb.owner, txarb.EXTERNAL)
e.tick(0.6)
C("EXTERNAL: radio ptt False >= 1.0 s -> IDLE, tx_end reason radio", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "radio"))
C("... release grace on the EXTERNAL end too", e.arb.release_grace_until, e.clock.t + 2.0)
e.radio.state["ptt"] = True
e.clock.t += 2.5                                   # past the release grace
e.arb.on_radio_state({"ptt": True})
e.tick(0.7, 0.7)
C("a front-panel key again -> EXTERNAL", e.arb.owner, txarb.EXTERNAL)
e.radio.calls.clear()
C("explicit STOP of a FRONT PANEL key unkeys it", (e.arb.stop(a), e.radio.calls[:1], e.arb.owner),
  (True, [("set_ptt", False)], None))
C("... audited tx_stop_forced owner FRONT PANEL", e.evs("tx_stop_forced")[-1]["owner"], "FRONT PANEL")

# flap during an owned PTT
e = Env(FakeRadio("ft991a"))
a = e.conn(1)
e.arb.claim(a, "ptt")
for _ in range(4):
    e.tick()
    e.arb.on_tx_hb(a)
e.radio.state["ptt"] = False                       # a misread poll ...
e.arb.on_radio_state({"ptt": False})
e.tick(0.4, 0.4)
e.radio.state["ptt"] = True                        # ... and back
e.arb.on_radio_state({"ptt": True})
e.tick()
e.tick()
C("T46 radio flap during an owned PTT (False 0.4 s then True) -> claim kept", (e.arb.owner, sorted(e.arb.claims)), (1, ["ptt"]))

# a cw session ending, then radio ptt True within 3.5 s -> no EXTERNAL
e = Env(FakeRadio("ft991a"))
a = e.conn(1)
e.arb.claim(a, "cw", args={"text": "CQ", "wpm": 20})
e.radio.state["cw_tx"] = False                     # the message ends
e.arb.on_radio_state(dict(e.radio.state))
for _ in range(6):
    e.tick()
C("cw finished -> dropped by reconciliation, reason done", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "done"))
C("... release grace 3.5 s after a session that held cw (AR-47)", e.arb.release_grace_until - e.clock.t > 2.0, True)
end_t = e.clock.t
e.radio.state["ptt"] = True                        # break-in delay: the radio still reports TX
for _ in range(4):
    e.arb.on_radio_state({"ptt": True})
    e.tick()
e.radio.state["ptt"] = False
e.arb.on_radio_state({"ptt": False})
e.tick()
C("T46 radio ptt True within the 3.5 s CW grace -> no EXTERNAL", (e.arb.owner, e.clock.t - end_t < 3.5), (None, True))

# a radio-ended session then TX within 5 s -> reattach to the same conn
e = Env(FakeRadio("ft991a"))
a = e.conn(1)
e.arb.claim(a, "ptt")
start = e.arb.session_start
e.radio.state["ptt"] = False                       # the radio unkeyed itself (e.g. its own failsafe)
e.arb.on_radio_state({"ptt": False})
for _ in range(6):
    e.tick()
    e.arb.on_tx_hb(a)
C("radio-ended session -> IDLE reason radio", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "radio"))
ended = e.arb.last_session["ended_at"]
e.clock.t = ended + 2.2                            # past the release grace, within 5 s
e.radio.state["ptt"] = True
e.arb.on_radio_state({"ptt": True})
e.tick(0.7, 0.7)
C("T46 ... TX again within 5 s -> reattached to the same connection", (e.arb.owner, sorted(e.arb.claims)), (1, ["ptt"]))
C("... the session clock continues from the first start", e.arb.session_start, start)
C("... audit tx_reattach", len(e.evs("tx_reattach")), 1)
e.arb.on_conn_closed(a)
C("... and the drop rule applies again (owner close unkeys)", (e.arb.owner, e.radio.calls[-1]), (None, ("set_ptt", False)))

# T46 (final review round 2): only a session whose ptt or cw claim the RADIO ended is reattached (AR-47:
# a misread poll, the Yaesu CW break-in hang). A tune claim that lapses at its 20 s cap is not one: TX the
# radio shows after it (a front-panel or physical-mic over keyed during the tune claim, in the release
# grace, or just after it) is FRONT PANEL. The TUNE owner's drop, ptt false, PTT claim, silence or session
# clock can never unkey it, and its mic never reaches it (F6, I10); the audit never names OP1 (I21).
class _MicRadio(FakeRadio):
    def __init__(self, pid):
        super().__init__(pid)
        self.audio = 0

    def write_audio(self, data):
        self.audio += len(data)


class _HubStub:
    conns = {}

    def send_to(self, c, f):
        pass


def _run_until(env, c, t_end, key_at=None, key=True):
    """Watchdog ticks every 0.5 s to t_end; OP1's browser heartbeats every 2 s (a no-op unless it owns
    TX); from key_at on the radio reports TX that TeleRig did not key (`key` False: receive)."""
    n = 0
    while env.clock.t < t_end - 1e-9:
        env.clock.t += 0.5
        n += 1
        if key_at is not None and env.clock.t >= key_at:
            env.radio.state["ptt"] = key
        if n % 4 == 0:
            env.arb.on_tx_hb(c)
        env.arb.on_radio_state(dict(env.radio.state))
        env.arb.tick(gap=0.5)


def _front_panel_is_safe(env, c, alive, tag):
    C(tag + ": the radio's TX is FRONT PANEL, not OP1's",
      (env.arb.owner, env.arb.label(), sorted(env.arb.claims)), (txarb.EXTERNAL, "FRONT PANEL", ["ptt"]))
    C("... no tx_reattach; tx_start kind external; nothing in the audit names OP1 for this TX",
      (len(env.evs("tx_reattach")), [k["kind"] for k in env.evs("tx_start")][1:], env.evs("tx_start")[-1]["user"]),
      (0, ["external"], "FRONT PANEL"))
    router = AudioRouter(_HubStub(), HostAudio(lambda pcm: None), lambda: env.radio, lambda: "lan")
    router.arb = env.arb
    c.tx_audio = True
    C("... OP1's mic frame is not passed to the radio (I10)", (router.mic_in(c, b"\x00" * 640), env.radio.audio), (False, 0))
    env.radio.calls.clear()
    C("... OP1's ptt false (a PTT click, a hidden-tab autoRelease) -> not the owner, nothing sent",
      (env.arb.release(c, "ptt"), env.radio.calls), (False, []))
    C("... OP1's PTT / TUNE -> busy, nothing sent", (env.arb.claim(c, "ptt", seq=9), env.arb.claim(c, "tune", seq=10),
                                                     env.radio.calls), ("busy", "busy", []))
    alive[c.id] = False
    env.arb.on_conn_closed(c)                       # OP1 closes the tab / its socket drops
    _run_until(env, c, env.clock.t + 130.0)         # no heartbeat and past any 120 s session clock
    C("... OP1's drop and 130 s of silence never unkey the local operator",
      (env.radio.calls, env.radio.state["ptt"], env.arb.owner), ([], True, txarb.EXTERNAL))
    env.radio.calls.clear()
    C("... only an explicit STOP does", (env.arb.stop(env.conn(2)), env.radio.calls[:1], env.arb.owner),
      (True, [("set_ptt", False)], None))


for key_at in (8.0, 21.0, 23.0):                    # during the tune claim / in the release grace / after it
    e = Env(_MicRadio("ft991a"))
    a = e.conn(1)
    alive = {1: True}
    e.arb.conn_alive = lambda x, alive=alive: alive.get(getattr(x, "id", None), False)
    t0 = e.clock.t
    e.arb.claim(a, "tune", seq=1)
    _run_until(e, a, t0 + 20.5, key_at=t0 + key_at)
    C(f"T46 TUNE at t0, a front-panel key at t+{key_at:g}: the tune claim lapsed at t20, reason done",
      ([k["reason"] for k in e.evs("tx_end")], e.arb.last_session["reason"], e.radio.calls), (["done"], "done", [("tune_atu",)]))
    _run_until(e, a, t0 + 30.0, key_at=t0 + key_at)
    _front_panel_is_safe(e, a, alive, f"T46 TUNE then a front-panel key at t+{key_at:g}")

# TUNE + PTT, the owner unkeys, the tune claim lapses at its cap: the session held ptt once, but the RADIO
# did not end it, so a front-panel key 3 s later is FRONT PANEL too.
e = Env(_MicRadio("ft991a"))
a = e.conn(1)
alive = {1: True}
e.arb.conn_alive = lambda x: alive.get(getattr(x, "id", None), False)
t0 = e.clock.t
e.arb.claim(a, "tune", seq=1)
_run_until(e, a, t0 + 2.0)
e.arb.claim(a, "ptt", seq=2)
e.radio.state["ptt"] = True                         # (the fake radio reports the app's key)
_run_until(e, a, t0 + 6.0)
e.arb.release(a, "ptt")
_run_until(e, a, t0 + 20.5)
C("T46 TUNE+PTT, the owner unkeyed, tune cap -> done", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "done"))
_run_until(e, a, t0 + 30.0, key_at=t0 + 23.0)
_front_panel_is_safe(e, a, alive, "T46 TUNE+PTT (released), then a front-panel key")

# A vox session that the radio ended (state.vox reads off) is not reattached either: the TX that follows
# is FRONT PANEL (a reattach would hand it a ptt claim, and with it the drop unkey and the mic gate).
e = Env(_MicRadio("ft991a"))
a = e.conn(1)
alive = {1: True}
e.arb.conn_alive = lambda x: alive.get(getattr(x, "id", None), False)
t0 = e.clock.t
e.arb.claim(a, "vox", seq=1)
e.radio.state["vox"] = 0                            # VOX turned off on the radio
_run_until(e, a, t0 + 3.5)
C("T46 vox session ended by the radio -> reason radio", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "radio"))
_run_until(e, a, t0 + 10.0, key_at=t0 + 6.0)
_front_panel_is_safe(e, a, alive, "T46 vox ended by the radio, then a front-panel key")

# Positive control: the Yaesu CW break-in hang (AR-47) still reattaches. A CW message ends (cw_tx False,
# reconciliation, reason done) and the radio still shows TX 3.6 s later (past the 3.5 s CW grace,
# within 5 s): that TX is the CW owner's, so the owner's drop unkeys it.
e = Env(FakeRadio("ft991a"))
a = e.conn(1)
t0 = e.clock.t
e.arb.claim(a, "cw", args={"text": "CQ", "wpm": 20})
e.radio.state["cw_tx"] = False
_run_until(e, a, t0 + 3.0)
C("T46 CW finished on its own -> reason done", (e.arb.owner, e.evs("tx_end")[-1]["reason"],
                                                sorted(e.arb.last_session.get("radio_ended") or [])), (None, "done", ["cw"]))
ended = e.arb.last_session["ended_at"]
e.clock.t = ended + 3.6                             # past the 3.5 s CW release grace: a state push shows TX
e.radio.state["ptt"] = True
e.arb.on_radio_state(dict(e.radio.state))
e.clock.t = ended + 4.3                             # 0.7 s of persistent TX, inside the 5 s window
e.arb.tick(gap=0.5)
C("T46 TX 3.6 s after the CW ended -> reattached to the CW owner (AR-47)", (e.arb.owner, sorted(e.arb.claims),
                                                                          len(e.evs("tx_reattach"))), (1, ["ptt"], 1))
e.radio.calls.clear()
e.arb.on_conn_closed(a)
C("... and the CW owner's drop unkeys it", (e.arb.owner, e.radio.calls), (None, [("set_ptt", False)]))

# -- T61: the arbiter's own session clock and caps (I18) -----------------------------------------------
e = Env()
a = e.conn(1)
t0 = e.clock.t
e.arb.claim(a, "ptt")
for i in range(200):                               # 100 s of heartbeats
    e.clock.t = t0 + (i + 1) * 0.5
    e.arb.on_tx_hb(a)
    e.arb.tick(gap=0.5)
e.arb.claim(a, "vox")
C("T61 vox added at t100: the session clock is NOT restarted", e.arb.session_start, t0)
while e.clock.t < t0 + 119.5:
    e.clock.t += 0.5
    e.arb.on_tx_hb(a)
    e.arb.tick(gap=0.5)
C("T61 still owned at t119.5", e.arb.owner, 1)
e.clock.t = t0 + 120.0
e.arb.on_tx_hb(a)
e.arb.tick(gap=0.5)
C("T61 force_release('timeout') at t120 regardless of the radio deadline", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "timeout"))
e.clock.t += 10
e.arb.claim(a, "ptt")
e.clock.t += 60
e.arb.on_tx_hb(a)
e.arb.tick(gap=30.0)                               # even a huge loop stall never pauses the clock ...
e.clock.t += 61
e.arb.tick(gap=61.0)
C("T61 the session clock is never suspended by a stall", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "timeout"))
e = Env()
a = e.conn(1)
from backend.radio import cw_duration  # noqa: E402
dur = cw_duration("CQ CQ", 20)
e.arb.claim(a, "cw", args={"text": "CQ CQ", "wpm": 20})
C("T61 cw cap = duration + 5 s", round(e.arb.claim_cap["cw"] - e.clock.t, 3), round(dur + 5.0, 3))
e.arb.claim_cap["cw"] = e.clock.t + 0.4
e.arb.on_tx_hb(a)
e.radio.calls.clear()
e.tick()
C("T61 cw cap reached -> timeout", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "timeout"))
C("... the CW still sending is stopped", e.radio.calls, [("stop_cw",)])
C("... tx_revoked names the cw cap (not the 120 s session clock)",
  [f for cid, f in e.sent if f.get("type") == "tx_revoked"], [{"type": "tx_revoked", "reason": "timeout", "by": None, "claim": "cw"}])
e = Env(FakeRadio("ft991a"))
a = e.conn(1)
e.arb.claim(a, "tune")
C("T61 tune cap 20 s", e.arb.claim_cap["tune"] - e.clock.t, 20.0)
for _ in range(39):
    e.tick()
    e.arb.on_tx_hb(a)
C("... still owned at 19.5 s (tune is not observable: no reconciliation)", e.arb.owner, 1)
e.radio.calls.clear()
e.tick()
C("... the tune claim ends at 20 s: session over, reason done", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "done"))
C("... nothing sent to the radio, no tx_revoked (the cap is the normal end of a tune)",
  (e.radio.calls, [f for _, f in e.sent if f.get("type") == "tx_revoked"]), ([], []))
C("... release grace set like every IDLE transition", e.arb.release_grace_until, e.clock.t + 2.0)

# T61 (final review): the tune cap ends the tune claim ONLY. TUNE at t0 then PTT at t5: the over goes on
# past t20 and ends at the 120 s session clock, which runs from the session start (t0).
e = Env(FakeRadio("ic7300mk2"))
a = e.conn(1)
t0 = e.clock.t
C("T61 tune+ptt: TUNE at t0 -> started", e.arb.claim(a, "tune", seq=1), "started")
for _ in range(10):                                 # 5 s of heartbeats
    e.tick()
    e.arb.on_tx_hb(a)
C("T61 tune+ptt: PTT at t5 -> added", e.arb.claim(a, "ptt", seq=2), "added")
e.radio.calls.clear()
while e.clock.t < t0 + 20.0:
    e.tick()
    e.arb.on_tx_hb(a)
C("T61 tune+ptt: at t20 the tune claim lapsed, ptt still held by OP1", (e.arb.owner, sorted(e.arb.claims)), (1, ["ptt"]))
C("... no unkey, no tx_revoked, no tx_end", (e.radio.calls, [f for _, f in e.sent if f.get("type") == "tx_revoked"],
                                              e.evs("tx_end")), ([], [], []))
C("... the tx frame now shows only ptt", (e.bcast[-1].get("owner") or {}).get("claims"), ["ptt"])
C("... the radio is still keyed", e.radio.state["ptt"], True)
C("... TUNE can be pressed again (the tune claim is free)", e.arb.claim(a, "tune", seq=3), "added")
C("... ... one tune_atu, no unkey", e.radio.calls, [("tune_atu",)])
while e.clock.t < t0 + 119.5:
    e.tick()
    e.arb.on_tx_hb(a)
C("T61 tune+ptt: still on the air at t119.5", (e.arb.owner, "ptt" in e.arb.claims, e.radio.state["ptt"]), (1, True, True))
e.tick()
C("T61 tune+ptt: the 120 s session clock (from t0) ends it at t120",
  (e.arb.owner, e.radio.state["ptt"], e.evs("tx_end")[-1]["reason"], e.evs("tx_end")[-1]["dur_s"]), (None, False, "timeout", 120.0))
C("... tx_revoked{timeout} without a claim name (the session clock)",
  [f for _, f in e.sent if f.get("type") == "tx_revoked"], [{"type": "tx_revoked", "reason": "timeout", "by": None}])

# The owner releases ptt while the tune claim still runs: the session lasts until the tune cap (done).
e = Env(FakeRadio("ic7300mk2"))
a = e.conn(1)
t0 = e.clock.t
e.arb.claim(a, "tune")
e.tick()
e.arb.claim(a, "ptt")
e.tick()
e.arb.on_tx_hb(a)
C("T61 tune+ptt: the owner unkeys -> the tune claim keeps the session", (e.arb.release(a, "ptt"), sorted(e.arb.claims)), (True, ["tune"]))
while e.clock.t < t0 + 20.0:
    e.tick()
    e.arb.on_tx_hb(a)
C("... which ends at the tune cap, reason done", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "done"))

# A cw cap with ptt also held: CW stopped and dropped, the ptt over goes on.
e = Env(FakeRadio("ic7300mk2"))
a = e.conn(1)
e.arb.claim(a, "ptt")
e.arb.claim(a, "cw", args={"text": "CQ", "wpm": 20})
e.arb.claim_cap["cw"] = e.clock.t + 0.4
e.arb.on_tx_hb(a)
e.radio.calls.clear()
e.tick()
C("T61 cw cap with ptt held: stop_cw only, ptt kept", (e.radio.calls, e.arb.owner, sorted(e.arb.claims)),
  ([("stop_cw",)], 1, ["ptt"]))
C("... no tx_revoked, no tx_end", ([f for _, f in e.sent if f.get("type") == "tx_revoked"], e.evs("tx_end")), ([], []))

# A tune cap with cw still running: only tune ends.
e = Env(FakeRadio("ft991a"))
a = e.conn(1)
t0 = e.clock.t
e.arb.claim(a, "tune")
e.clock.t += 15
e.arb.on_tx_hb(a)
e.arb.claim(a, "cw", args={"text": "CQ CQ CQ DE TEST TEST", "wpm": 10})
while e.clock.t < t0 + 20.0:
    e.tick()
    e.arb.on_tx_hb(a)
C("T61 tune cap with cw running: cw kept", (e.arb.owner, sorted(e.arb.claims), e.radio.state["cw_tx"]), (1, ["cw"], True))
long = "E" * 160
e = Env(FakeRadio("ft991a"))
a = e.conn(1)
e.arb.claim(a, "cw", args={"text": "0" * 80, "wpm": 4})
C("cw cap is at most 300 s", e.arb.claim_cap["cw"] - e.clock.t <= 300.0, True)


# -- SSTV: one picture is held for its own length, inside the radio's 3-minute TOT (backend/sstv.py) ----------
def heartbeat_until(env, conn, t):
    while env.clock.t < t:
        env.clock.t = min(t, env.clock.t + 0.5)
        env.arb.on_tx_hb(conn)
        env.arb.tick(gap=0.5)


e = Env()
a, b = e.conn(1), e.conn(2)
t0 = e.clock.t
C("SSTV: a PD-160 picture from IDLE -> started", e.arb.claim(a, "ptt", args={"sstv": "pd160"}, seq=1), "started")
C("SSTV ... ONE set_ptt(True) with the picture's hold (2:43 on the air + 5 s)", e.radio.calls, [("set_ptt", True, 162.70 + 5.0)])
C("SSTV ... the session's limit is the picture's", e.arb.session_max, 162.70 + 5.0)
C("SSTV ... the audit says it was a picture", [k["kind"] for k in e.evs("tx_start")], ["sstv"])
C("SSTV ... another connection is busy (I3)", (e.arb.claim(b, "ptt", args={"sstv": "r36"}, seq=1), e.radio.calls[1:]), ("busy", []))
heartbeat_until(e, a, t0 + 150.0)
C("SSTV still on the air at 2:30 (past a voice over's 120 s)", (e.arb.owner, sorted(e.arb.claims)), (1, ["ptt"]))
heartbeat_until(e, a, t0 + 167.5)
C("SSTV still on the air at 2:47.5", e.arb.owner, 1)
e.radio.calls.clear()
heartbeat_until(e, a, t0 + 168.0)
C("SSTV at its limit (2:47.7): force_release('timeout'), unkeyed", (e.arb.owner, e.evs("tx_end")[-1]["reason"], e.radio.calls),
  (None, "timeout", [("set_ptt", False)]))
C("SSTV ... the owner is told it was the picture's limit",
  [(f["reason"], f.get("claim")) for cid, f in e.sent if f.get("type") == "tx_revoked"], [("timeout", "sstv")])
C("SSTV ... and the arbiter is back to a voice over's limit", e.arb.session_max, txarb.SESSION_MAX)
e.clock.t += 10
e.radio.calls.clear()
t1 = e.clock.t
e.arb.claim(a, "ptt", seq=2)
C("SSTV: a plain ptt claim after it: set_ptt(True), no hold", e.radio.calls, [("set_ptt", True)])
heartbeat_until(e, a, t1 + 119.5)
C("SSTV ... a voice over still ends at 120 s", e.arb.owner, 1)
heartbeat_until(e, a, t1 + 120.0)
C("SSTV ... (released at 120 s)", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "timeout"))

e = Env()
a = e.conn(1)
t0 = e.clock.t
e.arb.claim(a, "ptt", args={"sstv": "r36"})
C("SSTV: a Robot 36 picture is held only for its own limit (0:38 + 5 s)", (e.radio.calls, e.arb.session_max),
  ([("set_ptt", True, 37.81 + 5.0)], 37.81 + 5.0))
heartbeat_until(e, a, t0 + 42.5)
C("SSTV ... on the air at 0:42.5", e.arb.owner, 1)
heartbeat_until(e, a, t0 + 43.0)
C("SSTV ... released at 0:42.8, long before 120 s", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "timeout"))
e.clock.t += 5
C("SSTV: the owner releasing at the picture's end -> one unkey", (e.arb.claim(a, "ptt", args={"sstv": "r36"}),
                                                                 e.arb.release(a, "ptt"), e.radio.calls[-1]),
  ("started", True, ("set_ptt", False)))

for mid in ("pd180", "pd290", "sdx", "pd999", 7):
    e = Env()
    a = e.conn(1)
    r = e.arb.claim(a, "ptt", args={"sstv": mid}, seq=3)
    C(f"SSTV: a {mid!r} picture (doesn't fit the 3-min TOT / unknown) -> failed, nothing keyed",
      (r, e.radio.calls, e.arb.owner, [f["code"] for cid, f in e.sent if f.get("type") == "error"], e.bcast[-1].get("req")),
      ("failed", [], None, ["tx_failed"], {"conn": 1, "seq": 3}))

e = Env(FakeRadio("ft991a"))
e.radio.profile = __import__("dataclasses").replace(e.radio.profile, tot_s=0)       # a radio with no hardware TOT
a = e.conn(1)
e.arb.claim(a, "ptt", args={"sstv": "m1"})
C("SSTV: no TOT on the radio: Martin M1 is held at most 120 s", (e.radio.calls, e.arb.session_max), ([("set_ptt", True, 120.0)], 120.0))
e.arb.release(a, "ptt")
e.clock.t += 5
C("SSTV: no TOT on the radio: PD-120 starts nothing", e.arb.claim(a, "ptt", args={"sstv": "pd120"}), "failed")

e = Env(FakeRadio("ft991a"))
a = e.conn(1)
t0 = e.clock.t
e.arb.claim(a, "tune")
e.clock.t += 2
e.radio.calls.clear()
C("SSTV: a picture claim added to a session under way -> added, as a plain ptt claim",
  (e.arb.claim(a, "ptt", args={"sstv": "pd160"}), e.radio.calls, e.arb.session_max), ("added", [("set_ptt", True)], txarb.SESSION_MAX))
heartbeat_until(e, a, t0 + 120.0)
C("SSTV ... on that session's 120 s clock", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "timeout"))

# a picture the radio's poll misread as unkeyed (reconciliation ended it) and that shows TX again within
# 5 s is reattached with ITS limit, on the clock from its first start (AR-47)
e = Env(FakeRadio("ft991a"))
a = e.conn(1)
t0 = e.clock.t
e.arb.claim(a, "ptt", args={"sstv": "pd160"})
heartbeat_until(e, a, t0 + 60.0)
e.radio.state["ptt"] = False
e.arb.on_radio_state({"ptt": False})
for _ in range(6):
    e.tick()
    e.arb.on_tx_hb(a)
C("SSTV: a misread unkey ends the session (reason radio)", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "radio"))
e.clock.t = e.arb.last_session["ended_at"] + 2.2
e.radio.state["ptt"] = True
e.arb.on_radio_state({"ptt": True})
e.tick(0.7, 0.7)
C("SSTV ... TX again within 5 s: reattached, the picture's limit and start kept",
  (e.arb.owner, e.arb.session_start, e.arb.session_max), (1, t0, 162.70 + 5.0))
heartbeat_until(e, a, t0 + 167.5)
C("SSTV ... still on the air at 2:47.5", e.arb.owner, 1)
heartbeat_until(e, a, t0 + 168.0)
C("SSTV ... released at the picture's limit", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "timeout"))

# -- T62: a start primitive that silently did nothing (phantom claim) ------------------------------------
e = Env(FakeRadio("ic9700", cw_works=False))
a, b = e.conn(1), e.conn(2)
e.radio.state["mode_name"] = "CW"
C("T62 cw claim granted", e.arb.claim(a, "cw", args={"text": "CQ", "wpm": 20}), "started")
times = []
for _ in range(8):
    e.tick()
    e.arb.on_tx_hb(a)
    times.append((round(e.clock.t - 1000.0, 1), e.arb.owner))
dropped_at = next(t for t, o in times if o is None)
C("T62 ... dropped ~1.0 s after the 1.5 s key grace (2.5-3.0 s)", 2.5 <= dropped_at <= 3.0, True)
ctx = type("X", (), {"arb": e.arb, "radio": e.radio, "transition": False})()
C("T62 ... other operators are no longer busy",
  policy.evaluate(policy.WS_POLICY["set_freq"], b, {"action": "set_freq", "hz": 7000000}, ctx).kind, "plain")

# -- T63: release grace on every IDLE transition; force_release flushes host + LAN TX buffers ------------
def grace_after(fn, cw=False):
    env = Env(FakeRadio("ft991a"))
    c = env.conn(1)
    env.arb.claim(c, "cw" if cw else "ptt", args={"text": "CQ", "wpm": 20} if cw else None)
    fn(env, c)
    return env.arb.owner, round(env.arb.release_grace_until - env.clock.t, 2)


def _reconcile(env, c):
    env.radio.state["ptt"] = False
    env.arb.on_radio_state({"ptt": False})
    for _ in range(6):
        env.tick()
        env.arb.on_tx_hb(c)
    env.clock.t = env.arb.last_session["ended_at"]


def _timeout(env, c):
    env.clock.t += 121
    c.last_hb = env.clock.t
    env.arb.tick(gap=0.5)


def _hb(env, c):
    env.clock.t += 7
    env.arb.tick(gap=0.5)


cases = {"owner release": lambda env, c: env.arb.release(c, "ptt"),
         "STOP": lambda env, c: env.arb.stop(env.conn(9)),
         "disconnect": lambda env, c: env.arb.on_conn_closed(c),
         "heartbeat": _hb, "timeout": _timeout, "reconciliation": _reconcile,
         "locked mode": lambda env, c: env.arb.force_release("server_locked")}
C("T63 release grace 2.0 s on every IDLE transition", {k: grace_after(f) for k, f in cases.items()},
  {k: (None, 2.0) for k in cases})
C("T63 ... 3.5 s after a session that held cw", grace_after(lambda env, c: env.arb.release(c, "cw"), cw=True), (None, 3.5))

ha = HostAudio(lambda pcm: None)
ha._buf.extend(b"\x01\x02" * 8000)                 # a queued tail (no device is ever opened)
lan = LanTransport("192.0.2.1")                    # never started: no socket
lan._tx_audio_buf += b"\x03" * 1000
fr = FakeRadio()
fr._tp = lan


class _Hub:
    conns = {}

    def send_to(self, c, f):
        pass


router = AudioRouter(_Hub(), ha, lambda: fr, lambda: "lan")
arb = txarb.TxArbiter(lambda: fr, flush_audio=router.flush_tx, clock=Clock())
router.arb = arb
arb.claim(Conn(1, None, user="OP1", role="operator"), "ptt")
arb.force_release("heartbeat")
C("T63 force_release -> audio.flush_tx(): host buffer and LAN buffer empty", (len(ha._buf), len(lan._tx_audio_buf)), (0, 0))
ha._buf.extend(b"\x01" * 100)
lan._tx_audio_buf += b"\x01" * 100
c1 = Conn(1, None, user="OP1", role="operator")
arb.claim(c1, "ptt")
arb.claim(c1, "cw", args={"text": "CQ", "wpm": 20})
arb.release(c1, "ptt")
C("releasing ptt while cw is still held: no ptt/vox left -> flushed", (len(ha._buf), len(lan._tx_audio_buf)), (0, 0))

# -- T41: real Radio + SimTransport(ic9700): repeated claims never reach the radio again -----------------
class Recording:
    """Wraps a real Radio; counts the TX primitives that reach it."""

    def __init__(self, r):
        self.r = r
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.r, name)

    def set_ptt(self, tx, hold=None):
        self.calls.append(("set_ptt", tx) if hold is None else ("set_ptt", tx, hold))
        self.r.set_ptt(tx, hold=hold)

    def set_rx_func(self, name, on):
        self.calls.append(("set_rx_func", name, on))
        self.r.set_rx_func(name, on)


prof = profiles.PROFILES["ic9700"]
real = Radio(prof)
real.connect(SimTransport(prof), prof)
try:
    C("T41 the in-process radio is the simulator", type(real._tp).__name__, "SimTransport")
    wrap = Recording(real)
    env = Env(radio=wrap, clock=time.monotonic)
    owner = env.conn(1)
    ctx = type("X", (), {"arb": env.arb, "radio": wrap, "transition": False})()

    def send(cmd):
        v = policy.evaluate(policy.WS_POLICY[cmd["action"]], owner, cmd, ctx)
        if v.kind == "claim":
            env.arb.claim(owner, v.extra["claim"], args=v.extra.get("args"), seq=v.extra.get("seq"))
        elif v.kind == "release":
            env.arb.release(owner, v.extra["claim"])
        return v.kind

    C("T41 owner claims ptt", send({"action": "ptt", "tx": True, "seq": 1}), "claim")
    d0 = real._ptt_deadline
    C("T41 ... the radio's failsafe deadline is armed", d0 is not None, True)
    for _ in range(20):
        env.arb.on_tx_hb(owner)
        time.sleep(0.1)
    C("T41 20 x tx_hb: deadline unchanged (I16, F2)", real._ptt_deadline, d0)
    for i in range(20):
        send({"action": "ptt", "tx": True, "seq": 2 + i})
        time.sleep(0.1)
    C("T41 20 x ptt:true: deadline unchanged (I17, F19)", real._ptt_deadline, d0)
    send({"action": "rx_func", "name": "vox", "on": True})
    d1 = real._ptt_deadline                         # the first VOX-on is a NEW claim: it arms once
    for _ in range(19):
        time.sleep(0.1)
        send({"action": "rx_func", "name": "vox", "on": True})
    C("T41 20 x vox on: deadline unchanged after the first (new) claim", real._ptt_deadline, d1)
    C("T41 exactly one set_ptt(True) and one set_rx_func('vox', True) reached the radio",
      (wrap.calls.count(("set_ptt", True)), wrap.calls.count(("set_rx_func", "vox", True))), (1, 1))
    C("T41 the radio really is keyed (sim)", (bool(real.state["ptt"]), bool(real.state["vox"])), (True, True))
    send({"action": "rx_func", "name": "vox", "on": False})
    send({"action": "ptt", "tx": False})
    C("T41 owner releases: unkeyed, VOX off, TX free", (bool(real.state["ptt"]), bool(real.state["vox"]), env.arb.owner),
      (False, False, None))
finally:
    real.disconnect()
C("T41 simulator disconnected", (real.state["connected"], real._tp), (False, None))


# -- a KICKED owner is released at once (I5, I7): tx_hb ignored, watchdog backstop, the Hub kick hook --------
e = Env()
a = e.conn(1)
e.arb.claim(a, "ptt")
a.closing = True                                     # kicked: its close frame is out, the client ignores it
e.clock.t += 1.0
hb0 = a.last_hb
e.arb.on_tx_hb(a)
C("tx_hb from a kicked (closing) owner is ignored", a.last_hb, hb0)
e.radio.calls.clear()
e.tick(0.1, 0.1)
C("watchdog backstop: a kicked owner is force-released on the next tick", (e.arb.owner, e.radio.calls),
  (None, [("set_ptt", False)]))
C("... audit tx_end reason disconnect", [k["reason"] for k in e.evs("tx_end")], ["disconnect"])
C("... no tx_revoked is queued to the closing socket", [f for _, f in e.sent if f.get("type") == "tx_revoked"], [])
e = Env()
a = e.conn(1)
e.arb.claim(a, "ptt")
e.arb.conn_alive = lambda c: (_ for _ in ()).throw(RuntimeError("boom"))
e.tick(0.1, 0.1)
C("a conn_alive that raises counts as gone (fail safe): released", (e.arb.owner, e.radio.state["ptt"]), (None, False))


class _KickWS:
    def __init__(self, block=False):
        self.closed = None
        self.gate = asyncio.Event()
        if not block:
            self.gate.set()

    async def send_text(self, t):
        await self.gate.wait()

    async def send_bytes(self, b):
        await self.gate.wait()

    async def close(self, code=1000, reason=None):
        self.closed = (code, reason)


async def _settle(n=30):
    for _ in range(n):
        await asyncio.sleep(0)


async def kicked_owner():
    """The server's wiring (server.py): hub.on_kick -> arb.on_conn_closed, conn_alive = open and
    registered. The owner's client ignores the close frame and keeps sending tx_hb."""
    hub = Hub()
    hub.loop = asyncio.get_running_loop()
    radio = FakeRadio()
    audits = []
    arb = txarb.TxArbiter(lambda: radio, send_to=lambda c, f: hub.send_to(c, f),
                          broadcast=lambda f: hub.broadcast_control(f),
                          audit=lambda ev, **kw: audits.append((ev, kw)),
                          conn_alive=lambda c: c is not None and not c.closing and hub.conns.get(c.id) is c)
    hub.on_kick = lambda c, code, reason: arb.on_conn_closed(c)
    ws1, ws2 = _KickWS(), _KickWS()
    op1, op2 = hub.add(ws1, user="OP1", role="operator"), hub.add(ws2, user="OP2", role="operator")
    hub.start_sender(op1)
    hub.start_sender(op2)
    C("kick hook: OP1 keys", arb.claim(op1, "ptt"), "started")
    hub.kick_user("OP2", 4401, "session_revoked")       # another user's kick never touches OP1's TX
    await _settle()
    C("kick hook: kicking a non-owner leaves the owner keyed", (arb.owner, radio.state["ptt"]), (op1.id, True))
    hb_before = op1.last_hb
    hub.kick_user("OP1", 4401, "session_revoked")      # gen bump / delete / role change
    arb.on_tx_hb(op1)                                   # the client ignores the close and heartbeats
    C("... a kicked owner's tx_hb does not refresh its heartbeat", op1.last_hb, hb_before)
    await _settle()
    arb.on_tx_hb(op1)
    C("kick hook: the kicked owner is unkeyed at once (no wait for the receive loop or 6 s)",
      (arb.owner, radio.state["ptt"], ws1.closed), (None, False, (4401, "session_revoked")))
    C("... one tx_end, reason disconnect", [kw["reason"] for ev, kw in audits if ev == "tx_end"], ["disconnect"])
    C("... its later tx_hb revives nothing", (arb.owner, op1.last_hb), (None, hb_before))
    arb.on_conn_closed(op1)                             # the receive loop's finally, later: a no-op
    C("... the later socket close is a no-op (no second tx_end)",
      [ev for ev, kw in audits].count("tx_end"), 1)

    # 1013 INSIDE an arbiter call: the tx frame of OP3's own claim overflows its control queue. The kick
    # hook is deferred (call_soon), so the arbiter is never re-entered mid-claim: tx_start, then tx_end.
    audits.clear()
    radio.calls.clear()
    ws3 = _KickWS(block=True)
    op3 = hub.add(ws3, user="OP3", role="operator")
    hub.start_sender(op3)
    await _settle()
    while len(op3.ctl) < CTL_MAX:
        hub.send_to(op3, {"type": "busy", "action": "ptt"})
    C("1013 in a claim: the claim itself completes", arb.claim(op3, "ptt"), "started")
    C("... and the hook did not run inside it", (arb.owner, ws3.closed is None), (op3.id, True))
    await _settle()
    C("... then the slow consumer is closed 1013 and released",
      (ws3.closed, arb.owner, radio.calls), ((1013, "slow_consumer"), None, [("set_ptt", True), ("set_ptt", False)]))
    C("... audit order tx_start then tx_end(disconnect), once each",
      [(ev, kw.get("reason")) for ev, kw in audits if ev in ("tx_start", "tx_end")],
      [("tx_start", None), ("tx_end", "disconnect")])
    for c in list(hub.conns.values()):
        hub.remove(c)
        if c.sender:
            c.sender.cancel()
    await _settle()

asyncio.run(kicked_owner())


# -- T68: the watchdog survives stalls (lag-aware) and failing ticks ----------------------------------------
async def t68():
    radio = FakeRadio()
    arb = txarb.TxArbiter(lambda: radio, clock=time.monotonic)
    c = Conn(1, None, user="OP1", role="operator")
    arb.claim(c, "ptt")
    loop = asyncio.get_running_loop()
    logs = []
    extra_calls = [0]

    def extra():
        extra_calls[0] += 1
        if extra_calls[0] == 2:
            raise RuntimeError("boom")              # a tick that raises: logged, loop keeps running

    task = loop.create_task(txarb.watchdog_loop(arb, extra, interval=0.5, log=logs.append))
    t0 = time.monotonic()

    async def client():
        # The client heartbeats every 2 s. A frame that arrived during a stall is processed only
        # after the loop resumes, and only after a few task hops (socket read -> protocol -> the
        # WS handler task), i.e. AFTER the overdue watchdog tick has run.
        for k in range(1, 7):
            await asyncio.sleep(max(0.0, t0 + 2.0 * k - time.monotonic()))
            for _ in range(3):
                await asyncio.sleep(0)
            arb.on_tx_hb(c)
    ct = loop.create_task(client())
    await asyncio.sleep(2.2)
    time.sleep(7.0)                                 # a 7 s synchronous stall on the loop thread
    await asyncio.sleep(1.5)
    C("T68 a 7 s loop stall while the owner heartbeats -> no heartbeat release", (arb.owner, radio.calls),
      (1, [("set_ptt", True)]))
    time.sleep(3.0)                                 # the spec's 3 s stall
    await asyncio.sleep(1.0)
    C("T68 a 3 s stall -> still keyed", arb.owner, 1)
    C("T68 a watchdog tick that raises is logged and the task keeps running",
      (any("boom" in m for m in logs), not task.done(), extra_calls[0] > 3), (True, True, True))
    await asyncio.sleep(max(0.0, 21.5 - (time.monotonic() - t0)))   # last heartbeat ~13.7 s
    C("T68 heartbeats stop -> released by the heartbeat timeout after all", arb.owner, None)
    ct.cancel()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

asyncio.run(t68())

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
