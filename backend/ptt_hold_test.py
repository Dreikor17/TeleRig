"""One SSTV picture's keying (the hold) — run: python -m backend.ptt_hold_test

A voice over is held at most PTT_TIMEOUT (120 s); one SSTV picture is held for its own length (the TX
arbiter's limit, backend/sstv.py), never past the radio's longest keying: 3 s inside the 3-minute TX
time-out the handler sets on connect (the backstop if the control link fails, 47 CFR 97.213(b)).

  1. the profiles: every radio that can key sets a 3-minute TOT on connect, and `tot_s` is exactly
     what its command sets (decoded through the radio's own TOT menu item); the IC-728 has none;
  2. ptt_hold_max / ptt_hold_s: 177 s for those radios, 120 s without a TOT; a hold is clamped to it and
     anything that isn't a sane positive number is a voice over's 120 s;
  3. sstv.fits / cap_s: the modes that fit 177 s and 120 s, and each picture's limit;
  4. the real Icom Radio and the real YaesuRadio on their simulators: set_ptt(True, hold) arms that
     deadline (clamped) and shows it in state["ptt_tot"]; a plain set_ptt(True) is 120 s; unkey and
     disconnect put ptt_tot back to 120; the Icom failsafe still unkeys at the deadline.

Only the built-in simulators are ever connected, in this process; no serial port, socket, sound card
or file is touched. "Keyed" is a field in the simulator objects.
"""
import math
import re
import time

from backend import menu_engine as me
from backend import profiles, sstv
from backend.radio import PTT_TIMEOUT, Radio, ptt_hold_max, ptt_hold_s
from backend.transport import SimTransport, YaesuSimTransport
from backend.yaesu import YaesuRadio

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


P = profiles.PROFILES
KEYING = [pid for pid, p in P.items() if p.capabilities.ptt]


# -- 1. the profiles' TOT --------------------------------------------------------------------------------
def tot_from_command(p) -> int:
    """The seconds the profile's TOT command sets, decoded through the radio's own TX TOT menu item."""
    items = {it.num: it for it in (p.menu or [])}
    if p.tot_civ:
        num = int(f"{p.tot_civ[0]:02x}{p.tot_civ[1]:02x}")        # the 1A 05 data number is BCD: 00 41 = 41
        it = items.get(num)
        val = me.civ_decode(it, bytes(p.tot_civ)) if it and it.tx_tot else None
        m = re.match(r"(\d+)\s*min", str(val or ""))
        return int(m.group(1)) * 60 if m else -1
    if p.tot_cat:
        it = next((i for i in items.values() if i.tx_tot), None)
        val = me.yaesu_decode(it, p.tot_cat) if it else None
        return int(val) * 60 if isinstance(val, int) else -1
    return 0


C("radios that can key", sorted(KEYING), ["ft891", "ft991a", "ic7300mk2", "ic9700"])
for pid in sorted(P):
    p = P[pid]
    C(f"{pid}: tot_s is what its TOT command sets", (p.tot_s, tot_from_command(p)),
      (p.tot_s, p.tot_s))
for pid in KEYING:
    C(f"{pid}: a 3-minute TOT on connect (never longer)", P[pid].tot_s, 180)
C("FT-991A sets menu 036 TX TOT = 3 min", P["ft991a"].tot_cat, "EX03603;")
C("FT-891 sets menu 05-14 TX TOT = 03 min", P["ft891"].tot_cat, "EX051403;")
C("IC-728: no TOT, no TX", (P["ic728"].tot_s, P["ic728"].tot_civ, P["ic728"].tot_cat, P["ic728"].capabilities.ptt),
  (0, (), "", False))

# -- 2. ptt_hold_max / ptt_hold_s -------------------------------------------------------------------------
for pid in KEYING:
    C(f"{pid}: the longest keying is 3 s inside its TOT", ptt_hold_max(P[pid]), 177)
C("no TOT (IC-728): a voice over's 120 s", ptt_hold_max(P["ic728"]), PTT_TIMEOUT)
C("no profile: 120 s", ptt_hold_max(None), PTT_TIMEOUT)
fake = type("F", (), {"tot_s": 60})()
C("a 1-minute TOT: never less than 120 s (the voice-over limit stays)", ptt_hold_max(fake), PTT_TIMEOUT)
for bad in (type("F", (), {"tot_s": True})(), type("F", (), {"tot_s": "180"})(), type("F", (), {"tot_s": -5})()):
    C(f"a nonsense tot_s ({bad.tot_s!r}): 120 s", ptt_hold_max(bad), PTT_TIMEOUT)
ic = P["ic9700"]
C("ptt_hold_s(no hold): 120 s", ptt_hold_s(ic), 120.0)
C("ptt_hold_s(42.81): a short picture is held only for its own limit", ptt_hold_s(ic, 42.81), 42.81)
C("ptt_hold_s(167.7): PD-160's limit", ptt_hold_s(ic, 167.7), 167.7)
C("ptt_hold_s(999): clamped to the radio's 177 s", ptt_hold_s(ic, 999), 177.0)
C("ptt_hold_s(170) on a radio without a TOT: clamped to 120 s", ptt_hold_s(P["ic728"], 170), 120.0)
for bad in (0, -1, math.nan, math.inf, True, "60", None, [60]):
    C(f"ptt_hold_s({bad!r}): fails safe to 120 s", ptt_hold_s(ic, bad), 120.0)

# -- 3. sstv.fits / cap_s ---------------------------------------------------------------------------------
C("modes that fit 177 s", [m for m in sstv.ON_AIR_S if sstv.fits(m, 177)],
  ["r36", "r72", "m1", "m2", "s1", "s2", "pd50", "pd90", "pd120", "pd160"])
C("modes that fit 120 s (no TOT)", [m for m in sstv.ON_AIR_S if sstv.fits(m, 120)],
  ["r36", "r72", "m1", "m2", "s1", "s2", "pd50", "pd90"])
C("an unknown mode, a non-string: never fit", (sstv.fits("pd999", 177), sstv.fits(None, 177), sstv.fits(160, 177)),
  (False, False, False))
C("cap_s(pd160, 177): its time on the air + 5 s", sstv.cap_s("pd160", 177), 162.70 + 5.0)
C("cap_s(r36, 177): a short picture's limit is short", sstv.cap_s("r36", 177), 37.81 + 5.0)
C("cap_s(m1, 120): at most the radio's longest keying", sstv.cap_s("m1", 120), 120.0)
C("cap_s(pd180, 177), cap_s(pd120, 120): don't fit -> None", (sstv.cap_s("pd180", 177), sstv.cap_s("pd120", 120)),
  (None, None))
C("every picture's limit ends before the 3-minute TOT", max(sstv.cap_s(m, 177) for m in sstv.ON_AIR_S if sstv.fits(m, 177)) < 180,
  True)


# -- 4. the real handlers on their simulators ----------------------------------------------------------------
def check_handler(name, r, tp, prof):
    r.connect(tp, prof)
    try:
        C(f"{name}: connected to the simulator", (r.state["connected"], type(r._tp).__name__), (True, type(tp).__name__))
        C(f"{name}: state shows its longest keying", r.state["ptt_hold_max"], 177)
        C(f"{name}: ptt_tot is 120 at rest", r.state["ptt_tot"], 120)
        t = time.monotonic()
        r.set_ptt(True, hold=42.81)
        C(f"{name}: a picture's hold arms its own deadline", round(r._ptt_deadline - t, 1), 42.8)
        C(f"{name}: ... shown as ptt_tot (whole seconds)", (r.state["ptt"], r.state["ptt_tot"]), (True, 42))
        r.set_ptt(False)
        C(f"{name}: unkey: no deadline, ptt_tot back to 120", (r.state["ptt"], r._ptt_deadline, r.state["ptt_tot"]),
          (False, None, 120))
        t = time.monotonic()
        r.set_ptt(True)
        C(f"{name}: a plain key is a voice over's 120 s", (round(r._ptt_deadline - t), r.state["ptt_tot"]), (120, 120))
        r.set_ptt(False)
        t = time.monotonic()
        r.set_ptt(True, hold=999)
        C(f"{name}: a hold past the radio's limit is clamped to 177 s", (round(r._ptt_deadline - t), r.state["ptt_tot"]),
          (177, 177))
        r.set_ptt(False)
        r.set_ptt(True, hold=-3)
        C(f"{name}: a nonsense hold is 120 s", r.state["ptt_tot"], 120)
        r.set_ptt(False)
        r.set_ptt(True, hold=167.7)
    finally:
        r.disconnect()
    C(f"{name}: disconnect unkeys and puts ptt_tot back to 120", (r.state["ptt"], r._ptt_deadline, r.state["ptt_tot"]),
      (False, None, 120))


check_handler("IC-9700", Radio(ic), SimTransport(ic), ic)
check_handler("FT-991A", YaesuRadio(P["ft991a"]), YaesuSimTransport(P["ft991a"]), P["ft991a"])

# the Icom failsafe unkeys a held picture at its deadline, whatever the radio last said
r = Radio(ic)
r.connect(SimTransport(ic), ic)
try:
    r.set_ptt(True, hold=60.0)
    r._ptt_deadline = time.monotonic() - 0.01
    for _ in range(100):
        if not r.state["ptt"]:
            break
        time.sleep(0.02)
    C("IC-9700: the failsafe unkeys a held picture at its deadline", (r.state["ptt"], r._ptt_deadline, r.state["ptt_tot"]),
      (False, None, 120))
finally:
    r.disconnect()

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
