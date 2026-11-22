"""The repeater offset in the state (state["offset"]) — run: python -m backend.rpt_offset_test

The VFO shows where a shifted transmission goes (frontend/app.js, the line under the digits), so the
state carries the offset of the band the radio is on, as the radio itself holds it:

  1. a Yaesu: its RPT SHIFT menu items (profile.rpt_offset_menus: FT-991A 080-083, FT-891 09-04 /
     09-05, in kHz) are read on connect and with the panel round-robin; state["offset"] follows the
     band of the frequency (0 on a band without one, and until the radio answers); a value changed in
     the Settings tab follows at once; a reply for another menu item, or one that isn't a number,
     changes nothing;
  2. every radio starts with offset 0 (not known): an Icom has its own once its 0C answer is in
     (backend/repeater_test.py), nothing shows a made-up 600 kHz.

Only the built-in simulators are ever connected, in this process; no serial port, socket, sound card
or file is touched, and nothing is keyed.
"""
import time

from backend import profiles
from backend.radio import Radio
from backend.transport import YaesuSimTransport
from backend.yaesu import YaesuRadio

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


def wait(pred, secs=3.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


P = profiles.PROFILES
FT991A, FT891 = P["ft991a"], P["ft891"]


class SpySim(YaesuSimTransport):
    """The FT-991A / FT-891 simulator, with every command TeleRig writes kept in order."""

    def __init__(self, profile):
        super().__init__(profile)
        self.sent = []

    def write(self, data: bytes) -> None:
        self.sent.extend(c + ";" for c in data.decode("ascii", "ignore").split(";") if c)
        super().write(data)


# -- 1. the FT-991A on its simulator ------------------------------------------------------------------
C("profiles: FT-991A 080-083, FT-891 09-04 / 09-05; the Icoms none",
  ([n for n, _lo, _hi in FT991A.rpt_offset_menus], [n for n, _lo, _hi in FT891.rpt_offset_menus],
   P["ic9700"].rpt_offset_menus, P["ic7300mk2"].rpt_offset_menus), ([80, 81, 82, 83], [904, 905], (), ()))
for pid in ("ft991a", "ft891"):
    names = [P[pid].menu[[it.num for it in P[pid].menu].index(n)].name for n, _lo, _hi in P[pid].rpt_offset_menus]
    C(f"{pid}: each is a RPT SHIFT item of its menu", all(x.startswith("RPT SHIFT ") for x in names), True)

r = YaesuRadio(FT991A)
frames = []
r.on_state = lambda st: frames.append(st.get("offset"))
sim = SpySim(FT991A)
r.connect(sim, FT991A)
try:
    C("connect asks for the four RPT SHIFT items", [c for c in sim.sent if c.startswith("EX08") and c[4] in "0123"][:4],
      ["EX080;", "EX081;", "EX082;", "EX083;"])
    C("14 MHz: no repeater offset there", wait(lambda: r._rpt_offsets.get(82) == 600_000) and r.state["offset"], 0)
    r.set_freq(146_940_000)
    C("146.940 MHz: the 144 MHz item (600 kHz)", r.state["offset"], 600_000)
    C("... a full state frame carries it", wait(lambda: 600_000 in frames), True)
    r.set_freq(442_100_000)
    C("442.100 MHz: the 430 MHz item (5 MHz)", r.state["offset"], 5_000_000)
    r.set_freq(29_620_000)
    C("29.620 MHz: the 28 MHz item (100 kHz)", r.state["offset"], 100_000)
    r.set_freq(52_525_000)
    C("52.525 MHz: the 50 MHz item (1 MHz)", r.state["offset"], 1_000_000)
    r.set_freq(146_940_000)
    r.set_menu(82, 700)                                   # the Settings tab: 144 MHz RPT SHIFT = 700 kHz
    C("a new 144 MHz value (the Settings tab) follows at once", wait(lambda: r.state["offset"] == 700_000), True)
    C("... and rides a full state frame", wait(lambda: 700_000 in frames), True)
    del frames[:]
    r._parse("EX0820750")                               # the radio's answer, as the reader hands it over
    C("... a full state frame at once (a quiet radio may send no other)", frames[-1:], [750_000])
    before = dict(r._rpt_offsets)
    r._parse("EX0821234")                               # a reply that changes nothing it shouldn't:
    C("a reply above the item's range is still the radio's word (kHz)", r._rpt_offsets[82], 1_234_000)
    r._parse("EX082ABCD")
    C("... one that isn't a number changes nothing", r._rpt_offsets[82], 1_234_000)
    r._parse("EX0790")                                   # 079 FM PKT MODE: not an offset
    C("another menu item's reply changes nothing", {k: v for k, v in r._rpt_offsets.items() if k != 82},
      {k: v for k, v in before.items() if k != 82})
    C("_note_rpt_offset refuses what isn't a whole number of kHz",
      [r._note_rpt_offset(82, v) for v in (None, -5, True, "600", 1.5)], [False] * 5)
    r.set_freq(14_074_000)
    C("back on 14 MHz: 0 again", r.state["offset"], 0)
finally:
    r.disconnect()

# -- the FT-891: 28 and 50 MHz only ---------------------------------------------------------------------
r = YaesuRadio(FT891)
sim = SpySim(FT891)
r.connect(sim, FT891)
try:
    C("FT-891: connect asks for 09-04 and 09-05", [c for c in sim.sent if c in ("EX0904;", "EX0905;")][:2], ["EX0904;", "EX0905;"])
    wait(lambda: r._rpt_offsets.get(905) == 1_000_000)
    r.set_freq(52_525_000)
    C("FT-891 on 52.525 MHz: the 50 MHz item (1 MHz)", r.state["offset"], 1_000_000)
    r.set_freq(29_620_000)
    C("... on 29.620 MHz: the 28 MHz item (100 kHz)", r.state["offset"], 100_000)
    r.set_freq(7_074_000)
    C("... on 7.074 MHz: none", r.state["offset"], 0)
finally:
    r.disconnect()

# -- 2. nothing made up before the radio answers -----------------------------------------------------
C("a fresh Icom state: offset 0 until its 0C answer", [Radio(P[pid]).state["offset"] for pid in ("ic9700", "ic7300mk2", "ic728")], [0, 0, 0])
y = YaesuRadio(FT991A)
C("a fresh Yaesu state: 0, and set_freq before any answer leaves it 0", (y.state["offset"], y._sync_offset()), (0, False))

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
