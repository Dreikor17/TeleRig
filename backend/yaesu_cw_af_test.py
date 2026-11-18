"""CW on the audio (AF) scope: the profile data and Yaesu menu reads behind it.
run: python -m backend.yaesu_cw_af_test

The AF scope (frontend waterfall.js AfMap, tests/af_scope_test.js) places CW by the radio's pitch, its
CW BFO side and its CW FREQ DISPLAY setting. This checks the backend half:

  1. the profiles ship cw_sides / cw_pitch_hz to the UI, and each menu read names the real menu item
     (FT-891 07-07 CW BFO + 07-11 CW FREQ DISPLAY, FT-991A 059 CW FREQ DISPLAY);
  2. a YaesuRadio on the CAT simulator reads them on connect into state cw_bfo / cw_disp (the
     simulator answers like a factory radio: CW BFO USB, display PITCH) and KP into key_pitch;
  3. a SET-menu write of either item updates the state (its confirm-read takes the same path);
  4. the ATAS TUNER SELECT read still lands in tuner_select (the FT-991A path this generalised).

Only the built-in simulator is ever connected, in this process; no serial port, socket, sound card
or file is touched, and nothing is keyed.
"""
import time

from backend import profiles
from backend.transport import YaesuSimTransport
from backend.yaesu import YaesuRadio

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


def wait_for(r, keys, timeout=4.0):
    """Wait until the connect priming has answered every state key (the sim answers each read)."""
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end and any(r.state.get(k) is None for k in keys):
        time.sleep(0.05)


FT891, FT991A, IC728 = (profiles.PROFILES[k] for k in ("ft891", "ft991a", "ic728"))

# -- 1. profiles -----------------------------------------------------------------------------------
j891, j728 = FT891.to_json(), IC728.to_json()
C("FT-891 JSON cw_sides", j891["cw_sides"], {"CW-USB": 1, "CW-LSB": -1})
C("FT-891 JSON cw_pitch_hz: 0 = the radio reports KP", j891["cw_pitch_hz"], 0)
C("FT-991A JSON cw_sides", FT991A.to_json()["cw_sides"], {"CW-USB": 1, "CW-LSB": -1})
C("IC-728 JSON: LSB-side CW at a fixed 800 Hz", (j728["cw_sides"], j728["cw_pitch_hz"]),
  ({"CW": -1, "CW-NAR": -1}, 800))
for pid in ("ic9700", "ic7300mk2"):
    C(f"{pid} has a real scope: no CW map", profiles.PROFILES[pid].to_json()["cw_sides"], {})
m891 = {it.num: it for it in FT891.menu}
C("FT-891 cw_bfo_read = menu 07-07 CW BFO (USB/LSB/AUTO)",
  (FT891.cw_bfo_read, m891[707].name, m891[707].options), ("EX0707;", "CW BFO", ["USB", "LSB", "AUTO"]))
C("FT-891 cw_disp_read = menu 07-11 CW FREQ DISPLAY (FREQ/PITCH)",
  (FT891.cw_disp_read, m891[711].name, m891[711].options), ("EX0711;", "CW FREQ DISPLAY", ["FREQ", "PITCH"]))
m991 = {it.num: it for it in FT991A.menu}
C("FT-991A cw_disp_read = menu 059 CW FREQ DISPLAY (DIRECT FREQ/PITCH OFFSET)",
  (FT991A.cw_disp_read, m991[59].name, m991[59].options),
  ("EX059;", "CW FREQ DISPLAY", ["DIRECT FREQ", "PITCH OFFSET"]))
C("FT-991A: no CW BFO read (CW-U / CW-L are modes)", FT991A.cw_bfo_read, "")
C("the IC-728 has no CAT menu reads", (IC728.cw_bfo_read, IC728.cw_disp_read), ("", ""))

# -- 2. + 3. FT-891 on the simulator ---------------------------------------------------------------
r = YaesuRadio()
r.connect(YaesuSimTransport(FT891), FT891)
wait_for(r, ("cw_bfo", "cw_disp", "key_pitch"))
C("FT-891 connect reads CW BFO (factory USB = 0) and CW FREQ DISPLAY (factory PITCH = 1)",
  (r.state.get("cw_bfo"), r.state.get("cw_disp")), (0, 1))
C("... and the pitch code (KP40 = 700 Hz)", r.state.get("key_pitch"), 40)
C("the CW menu reads ride the panel round-robin", ("EX0707;" in r._panel_cmds, "EX0711;" in r._panel_cmds),
  (True, True))
frames = []
r.on_state = lambda s: frames.append(dict(s))
r.set_menu(707, 2)                                   # CW BFO = AUTO (LSB on 7 MHz and below)
r.set_menu(711, 0)                                   # CW FREQ DISPLAY = FREQ (no pitch offset)
time.sleep(0.4)
C("a SET-menu write updates the state (CW BFO AUTO, display FREQ)",
  (r.state.get("cw_bfo"), r.state.get("cw_disp")), (2, 0))
C("... and each change emitted a state frame", any(f.get("cw_bfo") == 2 for f in frames)
  and any(f.get("cw_disp") == 0 for f in frames), True)
r.disconnect()

# -- 2. + 4. FT-991A on the simulator ------------------------------------------------------------------
r = YaesuRadio()
r.connect(YaesuSimTransport(FT991A), FT991A)
wait_for(r, ("cw_disp", "tuner_select"))
C("FT-991A connect reads CW FREQ DISPLAY (factory PITCH OFFSET = 1)", r.state.get("cw_disp"), 1)
C("FT-991A has no cw_bfo in state", r.state.get("cw_bfo"), None)
C("the ATAS TUNER SELECT read still lands in tuner_select", r.state.get("tuner_select"), 0)
r.set_menu(59, 0)                                    # DIRECT FREQ
time.sleep(0.4)
C("FT-991A menu 059 -> DIRECT FREQ updates the state", r.state.get("cw_disp"), 0)
r.disconnect()

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
