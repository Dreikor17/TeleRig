"""FT-991A simulator TX tracking — run: python -m backend.yaesu_sim_test

The Yaesu CAT simulator (transport.YaesuSimTransport) answers the TX; poll with the keyed state the
app last wrote (TX1; CAT key, TX0; unkey, TX2; radio-side key), like the real radio. Before this it
always answered TX0;, so the TX arbiter's reconciliation dropped every PTT claim on the FT-991A
profile about 2.5 s after keying.

  1. the transport alone: TX; answers follow TX0/TX1/TX2 writes; junk is ignored; stop() resets;
  2. a real YaesuRadio on the simulator: state["ptt"] stays True across several TX; polls while
     keyed, and goes False after set_ptt(False);
  3. the real arbiter (txarb) on that radio with heartbeats: a PTT claim survives the reconciliation
     window (the bug), and an owner release ends the session.

Only the built-in simulator is ever connected, in this process; no serial port, socket, sound card
or file is touched. The "keying" here is a field in the simulator object.
"""
import time

from backend import profiles, txarb
from backend.hub import Conn
from backend.transport import YaesuSimTransport
from backend.yaesu import YaesuRadio

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


FT991A = profiles.PROFILES["ft991a"]

# -- 1. the transport alone ------------------------------------------------------------------------
out = []
tp = YaesuSimTransport(FT991A)
tp.start(lambda b: out.append(b.decode("ascii")))


def ask(cmd):
    out.clear()
    tp.write(cmd.encode("ascii"))
    return list(out)


C("fresh sim: TX; -> TX0;", ask("TX;"), ["TX0;"])
C("TX1; (CAT key) answers nothing", ask("TX1;"), [])
C("... TX; -> TX1;", ask("TX;"), ["TX1;"])
C("... still TX1; on the next poll", ask("TX;"), ["TX1;"])
C("TX0; (unkey) answers nothing", ask("TX0;"), [])
C("... TX; -> TX0;", ask("TX;"), ["TX0;"])
ask("TX2;")
C("TX2; (radio-side key) -> TX; -> TX2;", ask("TX;"), ["TX2;"])
ask("TX0;")
for junk in ("TX9;", "TXA;", "TX11;", "TX;;"):
    ask(junk)
C("junk TX writes are ignored (still TX0)", ask("TX;"), ["TX0;"])
C("several commands in one write", ask("TX1;TX;FA;")[:1], ["TX1;"])
tp.stop()
C("stop() resets the simulated TX state", tp.tx, "0")

# -- 2. a real YaesuRadio on the simulator ---------------------------------------------------------
r = YaesuRadio()
r.connect(YaesuSimTransport(FT991A), FT991A)
time.sleep(0.6)
C("connected to the simulator, not keyed", (r.state.get("connected"), r.state.get("ptt")), (True, False))
r.set_ptt(True)
samples = []
t_end = time.monotonic() + 3.2                      # TX; is polled ~3x/s: about 9 polls
while time.monotonic() < t_end:
    samples.append(bool(r.state.get("ptt")))
    time.sleep(0.1)
C("keyed: state.ptt stays True across the TX; polls (was dropped to False by TX0;)", all(samples), True)
r.set_ptt(False)
time.sleep(0.8)
C("set_ptt(False): state.ptt False and the sim answers TX0", (r.state.get("ptt"), r._tp.tx), (False, "0"))
r.disconnect()

# -- 3. the real arbiter: a PTT claim survives reconciliation ----------------------------------------
r = YaesuRadio()
r.connect(YaesuSimTransport(FT991A), FT991A)
time.sleep(0.6)
audits = []
arb = txarb.TxArbiter(lambda: r, send_to=lambda c, f: None, broadcast=lambda f: None,
                      audit=lambda ev, **kw: audits.append((ev, kw)), flush_audio=lambda: None)
owner = Conn(1, None, user="OP1", role="operator")
C("claim ptt on the FT-991A sim -> started", arb.claim(owner, "ptt", seq=1), "started")
t_end = time.monotonic() + 4.5                      # key grace 1.5 s + absent 1.0 s = the old drop at ~2.5 s
while time.monotonic() < t_end:
    arb.on_radio_state({k: r.state.get(k) for k in ("ptt", "cw_tx", "vox", "tuner", "connected")})
    arb.on_tx_hb(owner)
    arb.tick(gap=0.25)
    time.sleep(0.25)
C("after 4.5 s of heartbeats the claim is still held (no reconciliation drop)",
  (arb.owner, sorted(arb.claims), bool(r.state.get("ptt"))), (1, ["ptt"], True))
C("no tx_end yet", [kw.get("reason") for ev, kw in audits if ev == "tx_end"], [])
C("owner release -> True", arb.release(owner, "ptt"), True)
time.sleep(0.8)
C("... unkeyed, TX free, tx_end reason owner",
  (bool(r.state.get("ptt")), arb.owner, [kw.get("reason") for ev, kw in audits if ev == "tx_end"]),
  (False, None, ["owner"]))
r.disconnect()

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
