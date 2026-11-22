"""Where a Yaesu's data filter sits: the profile data and the menu reads behind it.
run: python -m backend.yaesu_data_filter_test

In DATA-USB / DATA-LSB the FT-991A and FT-891 center their filter by SET menu, not on the voice band:
DATA MODE PSK on the PSK TONE, OTHERS on OTHER SHIFT (whose default 0 Hz is the carrier, so a 3 kHz
filter passes only 0-1.5 kHz of audio); OTHER DISP moves the dial. The Modem window (digital.js, with
waterfall.js Passband.dataFix, tests/passband_test.js and tests/digital_test.js) says when that cuts
what a mode needs. This checks the backend half:

  1. the profiles name the four menu items (FT-991A 062-065, FT-891 08-01..08-04) and ship them to the
     UI as data_menus; the Icoms have none;
  2. a YaesuRadio on the CAT simulator reads them on connect into state data_mode / psk_tone /
     data_disp / data_shift (the simulator answers like a factory radio: PSK, 1000 Hz, 0, 0), and they
     ride the panel round-robin;
  3. a SET-menu write of each updates the state (its confirm-read takes the same path), signed values
     both ways, with a state frame; a reply for another item, or one that is no number, changes none.

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


KEYS = ("data_mode", "psk_tone", "data_disp", "data_shift")


def wait_for(r, keys, timeout=4.0):
    """Wait until the connect priming has answered every state key (the sim answers each read)."""
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end and any(r.state.get(k) is None for k in keys):
        time.sleep(0.05)


def wait_value(r, key, want, timeout=2.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end and r.state.get(key) != want:
        time.sleep(0.05)
    return r.state.get(key)


FT891, FT991A = profiles.PROFILES["ft891"], profiles.PROFILES["ft991a"]

# -- 1. profiles -----------------------------------------------------------------------------------
m991 = {it.num: it for it in FT991A.menu}
C("FT-991A data_menus: 062 DATA MODE, 063 PSK TONE, 064 OTHER DISP, 065 OTHER SHIFT",
  {k: m991[n].name for k, n in FT991A.data_menus.items()},
  {"mode": "DATA MODE", "tone": "PSK TONE", "disp": "OTHER DISP (SSB)", "shift": "OTHER SHIFT (SSB)"})
C("FT-991A: PSK TONE 1000 / 1500 / 2000 Hz, OTHER SHIFT -3000..3000 Hz signed",
  (m991[63].options, m991[65].kind, m991[65].min, m991[65].max), (["1000 Hz", "1500 Hz", "2000 Hz"], "signed-int", -3000, 3000))
m891 = {it.num: it for it in FT891.menu}
C("FT-891 data_menus: 08-01 DATA MODE, 08-02 PSK TONE, 08-03 OTHER DISP, 08-04 OTHER SHIFT",
  {k: m891[n].name for k, n in FT891.data_menus.items()},
  {"mode": "DATA MODE", "tone": "PSK TONE", "disp": "OTHER DISP", "shift": "OTHER SHIFT"})
C("the profile JSON carries them (the Modem window's fix writes these numbers)",
  (FT991A.to_json()["data_menus"], FT891.to_json()["data_menus"]),
  ({"mode": 62, "tone": 63, "disp": 64, "shift": 65}, {"mode": 801, "tone": 802, "disp": 803, "shift": 804}))
C("the Icoms have none", [profiles.PROFILES[p].to_json()["data_menus"] for p in ("ic9700", "ic7300mk2", "ic728")], [{}, {}, {}])

# -- 2. + 3. FT-991A on the simulator --------------------------------------------------------------
r = YaesuRadio()
r.connect(YaesuSimTransport(FT991A), FT991A)
wait_for(r, KEYS)
C("FT-991A connect reads the data filter (factory: PSK, 1000 Hz, DISP 0, SHIFT 0)",
  tuple(r.state.get(k) for k in KEYS), (0, 1000, 0, 0))
C("the four reads ride the panel round-robin", [c in r._panel_cmds for c in ("EX062;", "EX063;", "EX064;", "EX065;")],
  [True, True, True, True])
frames = []
r.on_state = lambda s: frames.append(dict(s))
r.set_menu(62, "OTHER")                              # DATA MODE = OTHERS
r.set_menu(65, 1500)                                 # OTHER SHIFT = +1500 Hz
C("a write of DATA MODE OTHERS lands as 1", wait_value(r, "data_mode", 1), 1)
C("a write of OTHER SHIFT +1500 lands in Hz", wait_value(r, "data_shift", 1500), 1500)
r.set_menu(64, -300)                                 # OTHER DISP = -300 Hz
r.set_menu(63, "2000 Hz")                            # PSK TONE = 2000 Hz
C("OTHER DISP -300: signed both ways", wait_value(r, "data_disp", -300), -300)
C("PSK TONE 2000 Hz: in Hz", wait_value(r, "psk_tone", 2000), 2000)
C("... and each change emitted a state frame",
  [any(f.get(k) == v for f in frames) for k, v in (("data_mode", 1), ("data_shift", 1500), ("data_disp", -300), ("psk_tone", 2000))],
  [True, True, True, True])
before = tuple(r.state.get(k) for k in KEYS)
r._parse("EX066150")                                 # 066 DATA LCUT FREQ: not one of these
r._parse("EX065+ABCD")                               # no number
r._parse("EX06")                                     # cut short
C("another item's reply, or one that is no number, changes none of them", tuple(r.state.get(k) for k in KEYS), before)
r._parse("EX065-1200")
C("a reply heard as it is (the radio changed from its own panel): -1200", r.state.get("data_shift"), -1200)
r.disconnect()

# -- 2. + 3. FT-891 on the simulator ---------------------------------------------------------------
r = YaesuRadio()
r.connect(YaesuSimTransport(FT891), FT891)
wait_for(r, KEYS)
C("FT-891 connect reads the data filter (factory: PSK, 1000 Hz, DISP 0, SHIFT 0)",
  tuple(r.state.get(k) for k in KEYS), (0, 1000, 0, 0))
C("its four-digit reads ride the panel round-robin", [c in r._panel_cmds for c in ("EX0801;", "EX0802;", "EX0803;", "EX0804;")],
  [True, True, True, True])
r.set_menu(801, "OTHERS")
r.set_menu(804, 1500)
C("FT-891 08-01 OTHERS and 08-04 +1500", (wait_value(r, "data_mode", 1), wait_value(r, "data_shift", 1500)), (1, 1500))
r.disconnect()

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
