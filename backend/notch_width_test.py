"""A Yaesu's manual notch width and frequency — run: python -m backend.notch_width_test

The FT-991A and FT-891 set the manual (IF) notch's width with a SET-menu item, IF NOTCH WIDTH, NARROW or
WIDE (FT-991A 116, FT-891 12-04; profile.notch_width_menu), not with the Icom's three widths (16 57). The
DSP tab's keys are 0 WIDE / 1 MID / 2 NAR; a Yaesu shows WIDE and NAR (capabilities.mnotch_widths).

  1. connect reads the item; NARROW / WIDE -> state mnotch_w 2 / 0 and mnotch_w_ok; a key writes the
     item (EX1161 / EX1160, then reads it back) and the state follows the radio's answer; MID writes
     nothing;
  2. until the radio has answered the item as NARROW / WIDE, or when it answers something else (a radio
     that numbers its menu otherwise: another item, of another width), the keys write nothing;
  3. the notch frequency (BP01) is read back with the panel, so the NOTCH slider shows the radio's own;
  4. the FT-891's item is 12-04; the Icoms keep their three widths.

Only the built-in simulators are ever connected, in this process; no serial port, socket, sound card or
file is touched, and nothing is keyed.
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
    """The simulator, with every command TeleRig writes kept in order."""

    def __init__(self, profile):
        super().__init__(profile)
        self.sent = []

    def write(self, data: bytes) -> None:
        self.sent.extend(c + ";" for c in data.decode("ascii", "ignore").split(";") if c)
        super().write(data)


C("profiles: the item and the widths", [(P[p].notch_width_menu, P[p].capabilities.mnotch_widths) for p in ("ft991a", "ft891", "ic9700", "ic7300mk2")],
  [(116, [0, 2]), (1204, [0, 2]), (0, [0, 1, 2]), (0, [0, 1, 2])])
C("... each Yaesu item is its menu's IF NOTCH WIDTH, NARROW / WIDE",
  [(it.name, it.options) for p in (FT991A, FT891) for it in p.menu if it.num == p.notch_width_menu],
  [("IF NOTCH WIDTH", ["NARROW", "WIDE"])] * 2)

# -- 1. the FT-991A ---------------------------------------------------------------------------------
r = YaesuRadio(FT991A)
frames = []
r.on_state = lambda st: frames.append((st.get("mnotch_w"), st.get("mnotch_w_ok")))
sim = SpySim(FT991A)
r.connect(sim, FT991A)
try:
    C("connect reads the item", "EX116;" in sim.sent, True)
    C("the radio answers WIDE (its factory default): mnotch_w 0, the keys may write", wait(lambda: r.state.get("mnotch_w_ok")) and r.state["mnotch_w"], 0)
    sent = len(sim.sent)
    r.set_mnotch_w(2)
    C("NAR: the item set to NARROW, then read back", sim.sent[sent:sent + 2], ["EX1160;", "EX116;"])
    C("... the state follows the radio's answer, in a full state frame", wait(lambda: r.state["mnotch_w"] == 2) and wait(lambda: (2, True) in frames), True)
    sent = len(sim.sent)
    r.set_mnotch_w(0)
    C("WIDE: EX1161, read back", sim.sent[sent:sent + 2], ["EX1161;", "EX116;"])
    C("... mnotch_w 0", wait(lambda: r.state["mnotch_w"] == 0), True)
    del frames[:]
    r._parse("EX1160")                                  # the radio's answer, as the reader hands it over
    C("... an answer rides a full state frame at once (a quiet radio may send no other)", frames[-1:], [(2, True)])
    r._parse("EX1161")
    sent = len(sim.sent)
    r.set_mnotch_w(1)
    r.set_mnotch_w(7)
    C("MID (it has none) and a width out of range write nothing", [c for c in sim.sent[sent:] if c.startswith("EX116")], [])
    # 2. an answer that isn't NARROW / WIDE: the radio numbers its menu otherwise (116 is another item there)
    r._parse("EX11604")
    C("an answer of another width: mnotch_w_ok off", r.state["mnotch_w_ok"], False)
    sent = len(sim.sent)
    r.set_mnotch_w(2)
    C("... and the keys write nothing", [c for c in sim.sent[sent:] if c.startswith("EX116")], [])
    r._parse("EX1160")
    C("the item answered NARROW again: on, mnotch_w 2", (r.state["mnotch_w_ok"], r.state["mnotch_w"]), (True, 2))
    # 3. the notch frequency is read back with the panel
    C("the panel reads the notch frequency (BP01)", wait(lambda: "BP01;" in sim.sent, 6.0), True)
    C("... into mnotch_pos (the sim's 1000 Hz: 100 of 320)", wait(lambda: r.state.get("mnotch_pos") == round(100 / 320 * 255), 6.0), True)
finally:
    r.disconnect()

y = YaesuRadio(FT991A)
y._tp = type("Tap", (), {"sent": [], "write": lambda self, b: self.sent.append(b), "name": "tap"})()
y.state["mnotch_w_ok"] = False
y.set_mnotch_w(2)
C("before the radio has answered: nothing written", y._tp.sent, [])

# -- 4. the FT-891 ------------------------------------------------------------------------------------
r = YaesuRadio(FT891)
sim = SpySim(FT891)
r.connect(sim, FT891)
try:
    C("FT-891: connect reads 12-04", "EX1204;" in sim.sent, True)
    wait(lambda: r.state.get("mnotch_w_ok"))
    sent = len(sim.sent)
    r.set_mnotch_w(2)
    C("FT-891 NAR: EX12040, read back", sim.sent[sent:sent + 2], ["EX12040;", "EX1204;"])
    C("... mnotch_w 2", wait(lambda: r.state["mnotch_w"] == 2), True)
finally:
    r.disconnect()

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
