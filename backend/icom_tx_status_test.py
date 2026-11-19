"""Icom front-panel TX detection: the CI-V handler reads the radio's TX status (1C 00) and the TX
arbiter makes TX the app did not start a FRONT PANEL owner, as it always did for the Yaesu radios.
run: python -m backend.icom_tx_status_test

In-process, on the Icom simulator (SimTransport): nothing is keyed, the simulated TX is a number.
`front_tx` stands in for a hand on the radio's own PTT.

  1. the simulator answers 1C 00 reads like the radio: what CI-V keyed, or the front panel
  2. the poll reads 1C 00 about 3x a second; state["ptt"] follows the radio (front panel included),
     a state frame goes out on each change, our own echoed 1C 00 xx is not the radio's answer
  3. TeleRig's own key reads back as TX (no flicker); the PTT failsafe unkeys at its deadline even
     when the last read said RX (a lost or stale read never keeps a carrier up)
  4. the IC-728 (no CI-V TX command) is never asked
  5. the arbiter: front-panel TX on an Icom becomes the FRONT PANEL owner; a browser's or a bridge
     program's key is refused (busy FRONT PANEL); the session ends when the radio returns to RX
"""
import time

from backend import civ, policy, profiles, txarb
from backend.hub import Conn
from backend.radio import Radio
from backend.transport import SimTransport

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got)[:200], "" if good else f"(want {want!r})")


def wait(fn, timeout=2.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return bool(fn())


IC9700 = profiles.PROFILES["ic9700"]

# -- 1. the simulator alone --------------------------------------------------------------------------
tp = SimTransport(IC9700)
got = []
tp.start(lambda b: got.extend(civ.FrameReader().feed(b)))
try:
    def ask(cmd, sub, data=b""):
        got.clear()
        tp.write(civ.build(cmd, sub, data, radio_addr=IC9700.civ_addr))
        return [(f.cmd, f.sub, bytes(f.data)) for f in got]

    C("sim: 1C 00 read -> RX", ask(0x1C, 0x00), [(0x1C, 0x00, b"\x00")])
    C("sim: 1C 00 01 (CI-V key) -> OK", ask(0x1C, 0x00, b"\x01"), [(civ.OK, None, b"")])
    C("sim: ... then the read says TX", ask(0x1C, 0x00), [(0x1C, 0x00, b"\x01")])
    ask(0x1C, 0x00, b"\x00")
    C("sim: 1C 00 00 -> RX again", ask(0x1C, 0x00), [(0x1C, 0x00, b"\x00")])
    tp.front_tx = True
    C("sim: a front-panel key reads TX with no CI-V key", ask(0x1C, 0x00), [(0x1C, 0x00, b"\x01")])
    C("sim: ... and the reply comes from the radio's address", got[0].from_addr, IC9700.civ_addr)
finally:
    tp.stop()


# -- 2.-3. the Icom handler on the simulator ---------------------------------------------------------
class Spy(SimTransport):
    """The simulator, counting the 1C 00 reads the handler sends; `lose_reads` drops them unanswered
    (a radio that stopped answering, a lost reply)."""

    def __init__(self, prof):
        super().__init__(prof)
        self.tx_reads = []
        self.lose_reads = False

    def write(self, data):
        for fr in civ.FrameReader().feed(data):
            if fr.cmd == 0x1C and fr.sub == 0x00 and not fr.data:
                self.tx_reads.append(time.monotonic())
                if self.lose_reads:
                    return
        super().write(data)


r = Radio(IC9700)
sim = Spy(IC9700)
frames = []
r.on_state = lambda st: frames.append(bool(st.get("ptt")))
r.connect(sim, IC9700)
try:
    time.sleep(1.6)
    n = len(sim.tx_reads)
    span = sim.tx_reads[-1] - sim.tx_reads[0] if n > 1 else 0
    C("the poll reads 1C 00 about 3x a second", 3 <= n and span > 0 and 2.0 <= (n - 1) / span <= 5.0,
      True)
    C("the radio receiving: ptt false", r.state["ptt"], False)
    frames.clear()
    sim.front_tx = True
    C("front-panel key: ptt true within a second", wait(lambda: r.state["ptt"] is True, 1.0), True)
    C("... a state frame says so", True in frames, True)
    sim.front_tx = False
    C("front panel released: ptt false again", wait(lambda: r.state["ptt"] is False, 1.0), True)
    # our own frame echoed back (CI-V echo, a shared bus): 1C 00 01 from the controller is not TX
    r._on_bytes(civ.build(0x1C, 0x00, b"\x01", radio_addr=IC9700.civ_addr))
    C("an echo of our own 1C 00 01 is not the radio's answer", r.state["ptt"], False)
    # TeleRig's own key reads back as TX, with no flicker while it is held
    r.set_ptt(True)
    seen = set()
    for _ in range(40):
        seen.add(r.state["ptt"])
        time.sleep(0.025)
    C("TeleRig's key: ptt stays true through the reads", seen, {True})
    C("... the radio (simulated) is keyed", sim.tx, 1)
    # the failsafe unkeys at its deadline whatever the last read said: here the reads go unanswered
    # and the last one said RX, while the radio is still keyed
    sim.lose_reads = True
    r.state["ptt"] = False
    r._ptt_deadline = time.monotonic() - 0.01
    C("failsafe: at the deadline the radio is unkeyed anyway", wait(lambda: sim.tx == 0, 1.0), True)
    C("... and the deadline is gone", r._ptt_deadline, None)
    sim.lose_reads = False
    C("... the radio reads RX", wait(lambda: r.state["ptt"] is False, 1.0), True)
finally:
    r.disconnect()

# -- 4. the IC-728: no CI-V TX command, never asked --------------------------------------------------
IC728 = profiles.PROFILES["ic728"]
r728, sim728 = Radio(IC728), Spy(IC728)
r728.connect(sim728, IC728)
try:
    time.sleep(1.2)
    C("IC-728: no 1C 00 read is ever sent", len(sim728.tx_reads), 0)
finally:
    r728.disconnect()


# -- 5. the arbiter: an Icom front-panel key is the FRONT PANEL owner ---------------------------------
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


clk = Clock()
r = Radio(IC9700)
sim = SimTransport(IC9700)
r.connect(sim, IC9700)
try:
    arb = txarb.TxArbiter(lambda: r, clock=clk)
    op = Conn(1, None, user="OP1", role="operator")
    bridge = Conn(2, None, user="WSJT-X", role="operator")
    ctx = type("X", (), {"arb": arb, "radio": r, "transition": False})()
    arb.tick(clk.t)
    C("arbiter: idle while the radio receives", arb.owner, None)
    sim.front_tx = True
    wait(lambda: r.state["ptt"] is True, 1.0)
    arb.tick(clk.t)
    clk.t += txarb.EXT_CONFIRM + 0.1
    arb.tick(clk.t)
    C("arbiter: front-panel TX on an Icom -> FRONT PANEL owner", (arb.owner, arb.label()),
      (txarb.EXTERNAL, txarb.FRONT_PANEL))
    for who, c in (("a browser", op), ("a bridge program", bridge)):
        v = policy.evaluate(policy.WS_POLICY["ptt"], c, {"action": "ptt", "tx": True, "seq": 1}, ctx)
        C(f"arbiter: {who} keying over it is refused", (v.kind, v.extra.get("owner")), ("busy", "FRONT PANEL"))
    C("... nothing keyed by the app", sim.tx, 0)
    sim.front_tx = False
    wait(lambda: r.state["ptt"] is False, 1.0)
    arb.tick(clk.t)
    clk.t += txarb.EXT_END + 0.1
    arb.tick(clk.t)
    C("arbiter: the radio back on RX ends the FRONT PANEL session", arb.owner, None)
finally:
    r.disconnect()

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
