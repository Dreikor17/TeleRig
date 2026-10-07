"""TX arbiter unit tests — run: python -m backend.txarb_test

In-process, with a fake clock and a fake (recording) radio: the arbiter state machine (§2.13),
T41 (idempotent claims against a REAL Radio + SimTransport: the radio deadline is never pushed
back), T46 (EXTERNAL debounce, flap, CW grace, reattach), T61 (session clock and caps), T62
(phantom claim dropped by reconciliation), T63 (release grace on every IDLE transition + audio
flush), T68 (lag-aware watchdog, a raising tick keeps the loop alive).

Only the built-in simulator is ever connected (T41), in this process; no server, socket, sound card
or file is touched.
"""
import asyncio
import threading
import time

from backend import policy, profiles, txarb
from backend.audio_router import AudioRouter
from backend.hostaudio import HostAudio
from backend.hub import Conn
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

    def set_ptt(self, tx):
        self.calls.append(("set_ptt", tx))
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
e.tick()
C("T61 cw cap reached -> timeout", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "timeout"))
e = Env(FakeRadio("ft991a"))
a = e.conn(1)
e.arb.claim(a, "tune")
C("T61 tune cap 20 s", e.arb.claim_cap["tune"] - e.clock.t, 20.0)
for _ in range(39):
    e.tick()
    e.arb.on_tx_hb(a)
C("... still owned at 19.5 s (tune is not observable: no reconciliation)", e.arb.owner, 1)
e.tick()
C("... released at 20 s", (e.arb.owner, e.evs("tx_end")[-1]["reason"]), (None, "timeout"))
long = "E" * 160
e = Env(FakeRadio("ft991a"))
a = e.conn(1)
e.arb.claim(a, "cw", args={"text": "0" * 80, "wpm": 4})
C("cw cap is at most 300 s", e.arb.claim_cap["cw"] - e.clock.t <= 300.0, True)

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

    def set_ptt(self, tx):
        self.calls.append(("set_ptt", tx))
        self.r.set_ptt(tx)

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
