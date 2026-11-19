"""AF (the volume of the radio's own speaker, in the room with the radio) starts at 0 on every connect.
run: python -m backend.af_zero_test

In-process, on the simulators (nothing is keyed; the levels are numbers):

  1. Icom (IC-9700, IC-7300MK2): the AF set (CI-V 14 01 0000) goes out before the levels are read back,
     so the read confirms 0; the state says 0; a radio turned up since is turned down at the next connect
  2. Yaesu (FT-991A, FT-891): AG0000; before the panel reads; the state says 0
  3. the IC-728 (no level commands) is never sent one
"""
import time

from backend import civ, profiles
from backend.radio import Radio
from backend.transport import SimTransport, YaesuSimTransport
from backend.yaesu import YaesuRadio

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got)[:200], "" if good else f"(want {want!r})")


class IcomSpy(SimTransport):
    def __init__(self, prof):
        super().__init__(prof)
        self.cmds = []

    def write(self, data):
        for fr in civ.FrameReader().feed(data):
            self.cmds.append((fr.cmd, fr.sub, bytes(fr.data)))
        super().write(data)


class YaesuSpy(YaesuSimTransport):
    def __init__(self, prof):
        super().__init__(prof)
        self.cmds = []

    def write(self, data):
        self.cmds.extend(c + ";" for c in data.decode("ascii", "replace").split(";") if c)
        super().write(data)


for pid in ("ic9700", "ic7300mk2"):
    p = profiles.PROFILES[pid]
    sim = IcomSpy(p)
    sim.levels[0x01] = 180                                   # someone turned the speaker up
    r = Radio(p)
    r.connect(sim, p)
    try:
        time.sleep(0.3)
        C(f"{p.name}: AF set to 0 on connect", sim.levels[0x01], 0)
        C(f"{p.name}: ... and the state says 0", r.state["af"], 0)
        sets = [i for i, c in enumerate(sim.cmds) if c[:2] == (0x14, 0x01) and c[2]]
        reads = [i for i, c in enumerate(sim.cmds) if c[:2] == (0x14, 0x01) and not c[2]]
        C(f"{p.name}: the set goes out before the level is read back", bool(sets and reads and sets[0] < reads[0]), True)
        C(f"{p.name}: ... as 0000", sim.cmds[sets[0]][2] if sets else None, civ.level_to_bcd(0))
    finally:
        r.disconnect()

for pid in ("ft991a", "ft891"):
    p = profiles.PROFILES[pid]
    sim = YaesuSpy(p)
    r = YaesuRadio(p)
    r.connect(sim, p)
    try:
        time.sleep(0.3)
        C(f"{p.name}: AG0000; on connect", sim._levels["AG0"], "000")
        C(f"{p.name}: ... and the state says 0", r.state["af"], 0)
        C(f"{p.name}: ... before the panel reads it back", sim.cmds.index("AG0000;") < sim.cmds.index("AG0;"), True)
    finally:
        r.disconnect()

p = profiles.PROFILES["ic728"]
sim = IcomSpy(p)
r = Radio(p)
r.connect(sim, p)
try:
    time.sleep(0.3)
    C("IC-728: no level command is ever sent", [c for c in sim.cmds if c[0] == 0x14], [])
finally:
    r.disconnect()

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
