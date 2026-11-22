"""Repeater settings: the tone and code tables, what each radio sets, the duplex offset over CI-V —
run: python -m backend.repeater_test

  1. the tables are the page's: civ.CTCSS_TONES and civ.DCS_CODES equal frontend/app.js's CTCSS_TONES
     and DCS_CODES item for item (an index is the same tone or code on the radio, in the FM panel and in
     the tone_freq / dcs_code actions, which take every index and no more); 50 tones, 104 codes;
  2. what each radio sets (fm_tone, fm_dcs, duplex, duplex_offset): the IC-9700 has the FM tone panel
     (TONE / TSQL, no DCS), the shift and its offset (CI-V 0D); only the IC-9700 sets the offset, and
     /api/radios says so;
  3. the offset on the wire: 3-byte BCD with a 100 Hz LSB (600 kHz = 00 60 00), both ways;
  4. the IC-9700 handler on its simulator: connect reads the radio's offset (0C); set_offset writes 0D,
     the simulator keeps it and a 0C read brings it back into state["offset"]; the value is rounded down
     to 100 Hz and kept within 0..99.9999 MHz; the server's dispatcher hands an `offset` action to it;
  5. nothing is sent where TeleRig does not set the offset: the IC-7300MK2 and the IC-728 (the Icom
     handler) and the Yaesu handler (the offset is a menu item there).

In-process, on the Icom simulator (SimTransport, its scope off) and transport stand-ins: no port,
socket or radio, and nothing is keyed. frontend/app.js is only read.
"""
import re
import time
from pathlib import Path

from backend import civ, policy, profiles
from backend.radio import Radio
from backend.transport import SimTransport
from backend.yaesu import YaesuRadio

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


P = profiles.PROFILES
IC9700 = P["ic9700"]

# -- 1. the tables are the page's ------------------------------------------------------------------------
JS = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text(encoding="utf-8")


def js_list(name):
    """The strings of `const <name> = [...]` in app.js, or None when it is not there."""
    m = re.search(r"\bconst " + name + r" = \[(.*?)\];", JS, re.S)
    return re.findall(r'"([^"]*)"', m.group(1)) if m else None


C("app.js CTCSS_TONES = civ.CTCSS_TONES, tone for tone", js_list("CTCSS_TONES"), [f"{t:.1f}" for t in civ.CTCSS_TONES])
C("app.js DCS_CODES = civ.DCS_CODES, code for code", js_list("DCS_CODES"), civ.DCS_CODES)
C("50 tones and 104 codes, none twice",
  (len(civ.CTCSS_TONES), len(set(civ.CTCSS_TONES)), len(civ.DCS_CODES), len(set(civ.DCS_CODES))), (50, 50, 104, 104))
C("the tones rise; every code is three octal digits, and they rise",
  (all(a < b for a, b in zip(civ.CTCSS_TONES, civ.CTCSS_TONES[1:])), all(re.fullmatch(r"[0-7]{3}", c) for c in civ.DCS_CODES),
   civ.DCS_CODES == sorted(civ.DCS_CODES)), (True, True, True))
C("the first and the last: 67.0 and 254.1 Hz, 023 and 754",
  (civ.CTCSS_TONES[0], civ.CTCSS_TONES[-1], civ.DCS_CODES[0], civ.DCS_CODES[-1]), (67.0, 254.1, "023", "754"))


def takes(action, key, v):
    try:
        policy.WS_POLICY[action].validate({key: v}, None)
        return True
    except policy.Invalid:
        return False


C("tone_freq and dcs_code take every index of the tables, and no more",
  [takes("tone_freq", "idx", 0), takes("tone_freq", "idx", len(civ.CTCSS_TONES) - 1), takes("tone_freq", "idx", len(civ.CTCSS_TONES)),
   takes("dcs_code", "idx", 0), takes("dcs_code", "idx", len(civ.DCS_CODES) - 1), takes("dcs_code", "idx", len(civ.DCS_CODES))],
  [True, True, False, True, True, False])

# -- 2. what each radio sets ------------------------------------------------------------------------------
C("each radio: (fm_tone, fm_dcs, duplex, duplex_offset)",
  {pid: (p.capabilities.fm_tone, p.capabilities.fm_dcs, p.capabilities.duplex, p.capabilities.duplex_offset) for pid, p in P.items()},
  {"ic9700": (True, False, True, True),           # TONE / TSQL (16 42 / 16 43, 1B 00 / 01), no DTCS; 0D offset
   "ic7300mk2": (True, False, True, False),
   "ft991a": (True, True, True, False),           # the FM panel: CT / CN / OS; the offset is a menu item
   "ft891": (True, True, True, False),
   "ic728": (False, False, False, False)})
C("/api/radios carries duplex_offset for the page",
  {pid: p.to_json()["capabilities"]["duplex_offset"] for pid, p in P.items()},
  {"ic9700": True, "ic7300mk2": False, "ft991a": False, "ft891": False, "ic728": False})
C("a radio sets no offset unless its profile says so", profiles.Capabilities().duplex_offset, False)

# -- 3. the offset on the wire ----------------------------------------------------------------------------
C("0C / 0D: 3-byte BCD, 100 Hz LSB (600 kHz, 5 MHz, 12.3456 MHz, 99.9999 MHz)",
  [civ.offset_to_bcd(h).hex(" ") for h in (600_000, 5_000_000, 12_345_600, 99_999_900)],
  ["00 60 00", "00 00 05", "56 34 12", "99 99 99"])
C("... and back", [civ.offset_from_bcd(civ.offset_to_bcd(h)) for h in (100, 600_000, 5_000_000, 12_345_600, 99_999_900)],
  [100, 600_000, 5_000_000, 12_345_600, 99_999_900])


# -- 4. the IC-9700 handler on its simulator ---------------------------------------------------------------
class Spy(SimTransport):
    """The IC-9700 simulator with its scope off (nothing here needs it), keeping the payload of every
    0D (set the offset) frame the handler writes."""

    def __init__(self, prof):
        super().__init__(prof)
        self.sets = []

    def write(self, data):
        for fr in civ.FrameReader().feed(data):
            if fr.cmd == 0x0D:
                self.sets.append((bytes([fr.sub]) if fr.sub is not None else b"") + fr.data)
        super().write(data)

    def _emit_scope(self, center, span):
        return


def read_back(r, want, timeout=2.0):
    """Ask the radio for its offset (0C) until state["offset"] reads `want`; a reply lost to the
    simulator's threads is asked for again."""
    end = time.time() + timeout
    while time.time() < end:
        r._write(r._b(0x0C))
        if wait(lambda: r.state["offset"] == want, 0.2):
            return True
    return r.state["offset"] == want


r = Radio(IC9700)
sim = Spy(IC9700)
sim.offset = 1_600_000                      # what the radio holds before TeleRig connects
offsets = []
r.on_state = lambda st: offsets.append(st.get("offset"))
r.connect(sim, IC9700)
try:
    C("connect reads the radio's own offset (0C)", wait(lambda: r.state["offset"] == 1_600_000), True)
    offsets.clear()
    r.set_offset(5_000_000)
    C("set_offset(5 MHz): the state at once", r.state["offset"], 5_000_000)
    C("... one 0D frame: 00 00 05", sim.sets, [b"\x00\x00\x05"])
    C("... the radio (simulated) keeps 5 MHz", sim.offset, 5_000_000)
    C("... and a state frame says so", 5_000_000 in offsets, True)
    r.state["offset"] = 0                   # forgotten here: only the radio's own answer can bring it back
    C("a 0C read brings 5 MHz back into the state", read_back(r, 5_000_000), True)
    cases = ((600_050, 600_000), (99, 0), (-5, 0), (123_456_789, 99_999_900), (12_000_000, 12_000_000))
    got = []
    for hz, want in cases:
        sim.sets.clear()
        r.set_offset(hz)
        got.append((r.state["offset"], list(sim.sets), sim.offset))
    C("rounded down to 100 Hz, kept within 0..99.9999 MHz (the state, the 0D payload, what the radio keeps)",
      got, [(w, [civ.offset_to_bcd(w)], w) for _, w in cases])
    r.state["offset"] = 0
    C("... and read back: 12 MHz", read_back(r, 12_000_000), True)

    from backend import server              # import only: no lifespan; this test's radio stands in for a moment
    saved = server.radio
    server.radio = r
    try:
        sim.sets.clear()
        server._handle_cmd({"action": "offset", "hz": 7_600_000})
        C("the dispatcher: an `offset` action reaches the IC-9700 (0D, the state, the radio)",
          (sim.sets, r.state["offset"], sim.offset), ([civ.offset_to_bcd(7_600_000)], 7_600_000, 7_600_000))
        server._handle_cmd({"action": "offset"})          # malformed (the policy refuses it first): dropped
        C("... one without hz writes nothing", (sim.sets, r.state["offset"]), ([civ.offset_to_bcd(7_600_000)], 7_600_000))
    finally:
        server.radio = saved
finally:
    r.disconnect()


# -- 5. nothing is sent where TeleRig does not set the offset -------------------------------------------------
class Tap:
    """A transport stand-in that keeps what is written to it (never started: no port, no thread)."""

    name = "tap"
    supports_audio = False

    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(bytes(data))


for pid in ("ic7300mk2", "ic728"):
    rr = Radio(P[pid])
    rr._tp = tap = Tap()
    before = rr.state["offset"]
    rr.set_offset(600_000)
    C(f"{pid}: set_offset sends nothing and changes nothing (no duplex_offset)", (tap.frames, rr.state["offset"]), ([], before))
rr = Radio(IC9700)
rr._tp = tap = Tap()
rr.set_offset(600_000)
C("... the IC-9700's writes exactly one 0D frame", tap.frames, [civ.build(0x0D, None, b"\x00\x60\x00", radio_addr=IC9700.civ_addr)])
for pid in ("ft991a", "ft891"):
    y = YaesuRadio(P[pid])
    sent = []
    y._send = sent.append
    before = dict(y.state)
    y.set_offset(600_000)
    C(f"{pid}: set_offset sends nothing and changes nothing (the offset is a menu item)", (sent, y.state == before), ([], True))

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
