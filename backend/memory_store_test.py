"""Memory store tests — run: python -m backend.memory_store_test

One memories file per user, a list per radio. Every field of an item is validated and nothing is
clamped: a shape, type or range error names its field, the frequency is checked against the radio's
own receive coverage and the mode against its own modes (the real profiles), and the repeater
settings (shift, offset, tone, CTCSS tone, DCS code) against what the radio can set and the modes
they go with; an item without them is what it always was. Ids start at 1 and are
never used twice in a file; `rev` counts writes; a radio holds at most MAX_PER_RADIO; deleting an id
that is not there writes nothing; a read never changes the file; files are memories/<hex>.json; an
unreadable file is reported and replaced by the next save, never locking anything.

Everything happens in the scratch folder of $TELERIG_CONFIG (the runner sets it); the test refuses
to run against a real ~/.telerig or ~/.radio-webop. No server, radio or socket is involved.
"""
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    line = " ".join([("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})"])
    print(line.encode("ascii", "backslashreplace").decode("ascii"))


cfg = os.environ.get("TELERIG_CONFIG", "")
real = [os.path.join(os.path.expanduser("~"), d) for d in (".telerig", ".radio-webop")]
if not cfg or any(os.path.normcase(os.path.abspath(cfg)).startswith(os.path.normcase(r)) for r in real):
    raise SystemExit("REFUSING: memory_store_test needs a scratch TELERIG_CONFIG")

REPO = Path(__file__).resolve().parent.parent
DIR = Path(cfg).parent / "memories"

# I12: importing the module (in a fresh child) touches no file: the memories folder is not created
before = sorted(p.name for p in Path(cfg).parent.iterdir()) if Path(cfg).parent.is_dir() else []
r = subprocess.run([sys.executable, "-c", "import backend.memory_store"], cwd=str(REPO), capture_output=True, text=True)
after = sorted(p.name for p in Path(cfg).parent.iterdir()) if Path(cfg).parent.is_dir() else []
C("I12: import does no file I/O", (r.returncode, before == after), (0, True))

from backend import civ  # noqa: E402
from backend import memory_store as M  # noqa: E402
from backend.memory_store import MemoryFull, MemoryInvalid  # noqa: E402
from backend.profiles import PROFILES  # noqa: E402


def field_of(fn, *a):
    try:
        fn(*a)
    except MemoryInvalid as e:
        return e.field
    return None


def message_of(fn, *a):
    try:
        fn(*a)
    except (MemoryInvalid, MemoryFull) as e:
        return e.message
    return None


def stored(user):
    return json.loads(M.path_for(user).read_text())


def written(p):
    """The file's bytes and when it was last written: equal before and after = nothing was written."""
    return p.read_bytes(), p.stat().st_mtime_ns


def moved_aside(p):
    return {x.name for x in p.parent.iterdir() if x.name.startswith(p.name + ".corrupt-")}


V = M.validate_item
ITEM = {"hz": 146520000, "mode": "FM", "name": "Simplex"}
YAESU = {"hz": 146520000, "mode": "FM", "name": "Simplex", "narrow": False, "width": 0}     # NAR, WIDTH
ICOM = {"hz": 145500000, "mode": "FM", "name": "Calling", "data": False, "filter": 1}       # DATA, FIL
# repeater memories: the IC-9700's + shift, 600 kHz offset, TSQL 100.0 Hz; the FT-991A's - shift and DCS 023
RPT_ICOM = {"hz": 147240000, "mode": "FM", "name": "Club", "shift": "+", "offset": 600000, "tone": "tsql", "ctcss": 1000}
RPT_YAESU = {"hz": 146940000, "mode": "FM", "name": "Hilltop", "shift": "-", "tone": "dcs", "dcs": 23}
NONE = ({"rev": 0, "radios": {}, "seeded": []}, False)
UNREADABLE = ({"rev": 0, "radios": {}, "seeded": []}, True)

C("the limits: 100 a radio, a name of 32, a body of 2048 bytes", (M.MAX_PER_RADIO, M.NAME_MAX, M.BODY_MAX), (100, 32, 2048))

# ---- a valid item is stored as it is ------------------------------------------------------------------
C("a valid item is kept as is", V("ft991a", ITEM), ITEM)
C("the optional fields ride along (a Yaesu's NAR and WIDTH, an Icom's DATA and FIL)",
  (V("ft991a", YAESU), V("ic9700", ICOM)), (YAESU, ICOM))
C("one stored order, whatever the request's",
  list(V("ic9700", {"width": 5, "name": "N", "filter": 2, "mode": "USB", "narrow": True, "hz": 144200000, "data": True})),
  ["hz", "mode", "name", "data", "filter", "narrow", "width"])
C("... the repeater settings after those, in their own order (shift, offset, tone, ctcss, dcs)",
  [list(V("ic9700", {"ctcss": 885, "tone": "tone", "offset": 600000, "shift": "-", "width": 5, "name": "N", "filter": 2,
                     "mode": "FM", "narrow": True, "hz": 146940000, "data": True})),
   list(V("ft991a", {"dcs": 754, "tone": "dcs-enc", "shift": "+", "name": "N", "mode": "C4FM", "hz": 145000000}))],
  [["hz", "mode", "name", "data", "filter", "narrow", "width", "shift", "offset", "tone", "ctcss"],
   ["hz", "mode", "name", "shift", "tone", "dcs"]])
C("an item without repeater settings is what it always was, on every radio (old files read the same)",
  [V(rid, {"hz": p.default_freq, "mode": p.modes[0], "name": "x"}) for rid, p in PROFILES.items()],
  [{"hz": p.default_freq, "mode": p.modes[0], "name": "x"} for p in PROFILES.values()])
C("the name is stored stripped", V("ft991a", {**ITEM, "name": "  Simplex \n"})["name"], "Simplex")
C("the result is a new dict (the caller's is not kept)", V("ft991a", ITEM) is ITEM, False)
fine = [{**ITEM, "filter": 1}, {**ITEM, "filter": 3}, {**ITEM, "width": 0}, {**ITEM, "width": 99}, {**ITEM, "data": True},
        {**ITEM, "data": False}, {**ITEM, "narrow": True}, {**ITEM, "narrow": False}, {**ITEM, "name": "x"}, {**ITEM, "name": "x" * 32},
        {**ITEM, "name": " " + "x" * 32 + " "}, {**ITEM, "name": "Zürich 70 cm ↑"}, {**ITEM, "name": "\U0001f4fb" * 32}]
C("the bounds themselves are fine: filter 1 and 3, width 0 and 99, a name of 1 and of 32 characters",
  [field_of(V, "ft991a", it) for it in fine], [None] * len(fine))

# ---- the frequency: the radio's own receive coverage (the real profiles) ---------------------------------
def hz_ok(radio, hz, mode="FM"):
    return field_of(V, radio, {"hz": hz, "mode": mode, "name": "x"}) is None


C("146.520 MHz: fine on the FT-991A and the IC-9700, refused on the IC-7300MK2",
  [hz_ok(rid, 146520000) for rid in ("ft991a", "ic9700", "ic7300mk2")], [True, True, False])
C("... the refusal names item.hz", field_of(V, "ic7300mk2", ITEM), "item.hz")
C("both edges of a range are inside (IC-9700, 144-148 MHz), one hertz further is not",
  [hz_ok("ic9700", h) for h in (143_999_999, 144_000_000, 148_000_000, 148_000_001)], [False, True, True, False])
C("anywhere the radio receives, not only its ham bands (the FT-991A's airband, 5 MHz on the IC-7300MK2)",
  [hz_ok("ft991a", 121_500_000, "AM"), hz_ok("ic7300mk2", 5_000_000, "AM"), hz_ok("ic9700", 440_000_000)], [True, True, True])
C("between two ranges is outside (FT-991A: 56-118 MHz)", hz_ok("ft991a", 100_000_000), False)
C("every profile: its default frequency is inside; 0, a negative and 10 GHz are not",
  [(hz_ok(rid, p.default_freq, p.modes[0]), hz_ok(rid, 0, p.modes[0]), hz_ok(rid, -p.default_freq, p.modes[0]),
    hz_ok(rid, 10_000_000_000, p.modes[0])) for rid, p in PROFILES.items()], [(True, False, False, False)] * len(PROFILES))
C("every profile here states its coverage", [rid for rid, p in PROFILES.items() if not p.rx_ranges], [])
PROFILES["bandsonly"] = dataclasses.replace(PROFILES["ic728"], id="bandsonly", rx_ranges=[])
C("a profile without rx_ranges: its bands' edges (40 m: 7.0-7.3 MHz; 5 MHz is no band)",
  [hz_ok("bandsonly", h, "USB") for h in (6_999_999, 7_000_000, 7_300_000, 7_300_001, 5_000_000)],
  [False, True, True, False, False])

# ---- the mode: one of the radio's own ----------------------------------------------------------------
C("every mode of a radio's own list",
  [m for rid, p in PROFILES.items() for m in p.modes if field_of(V, rid, {"hz": p.default_freq, "mode": m, "name": "x"})], [])
C("a mode of another radio is refused (DV is the IC-9700's, C4FM the FT-991A's)",
  [field_of(V, "ft991a", {**ITEM, "mode": "DV"}), field_of(V, "ic9700", {**ICOM, "mode": "C4FM"}),
   field_of(V, "ic9700", {**ICOM, "mode": "DV"}), field_of(V, "ft991a", {**ITEM, "mode": "C4FM"})],
  ["item.mode", "item.mode", None, None])
C("a mode the radio reports but has no key for (a Yaesu's FM-N) is not in its list", field_of(V, "ft991a", {**ITEM, "mode": "FM-N"}),
  "item.mode")


# ---- the repeater settings: only what the radio can set, in a mode that has them ---------------------------
def rpt(radio, hz, mode="FM", **settings):
    """The field a repeater memory is refused for, None when it is kept."""
    return field_of(V, radio, {"hz": hz, "mode": mode, "name": "x", **settings})


FM_AT = {"ic9700": 147_240_000, "ic7300mk2": 29_620_000, "ft991a": 146_940_000, "ft891": 29_620_000, "ic728": 29_620_000}
C("a repeater memory is kept as is (IC-9700: + shift, 600 kHz, TSQL 100.0 Hz; FT-991A: - shift, DCS 023)",
  (V("ic9700", RPT_ICOM), V("ft991a", RPT_YAESU)), (RPT_ICOM, RPT_YAESU))
C("what each radio's memory keeps in FM: a + shift, a - shift with its offset, TSQL with its tone, DCS with its code",
  {rid: [rpt(rid, hz, shift="+"), rpt(rid, hz, shift="-", offset=600_000), rpt(rid, hz, tone="tsql", ctcss=1000),
         rpt(rid, hz, tone="dcs", dcs=23)] for rid, hz in FM_AT.items()},
  {"ic9700": [None, None, None, "item.tone"],                     # shift, offset (CI-V 0D), TONE/TSQL; no DCS
   "ic7300mk2": [None, "item.offset", None, "item.tone"],         # shift (duplex), TONE/TSQL; no offset, no DCS
   "ft991a": [None, "item.offset", None, None],                   # shift (the FM panel's OS), every tone; the
   "ft891": [None, "item.offset", None, None],                    # offset is a menu item on a Yaesu
   "ic728": ["item.shift", "item.shift", "item.tone", "item.tone"]})
C("the IC-728 keeps none of them (each one alone)",
  [rpt("ic728", 29_620_000, **s) for s in ({"shift": "simplex"}, {"offset": 600_000}, {"tone": "off"}, {"ctcss": 885}, {"dcs": 23})],
  ["item.shift", "item.offset", "item.tone", "item.ctcss", "item.dcs"])
C("... the refusals say why, naming the radio",
  [message_of(V, rid, {"hz": FM_AT[rid], "mode": "FM", "name": "x", **s}) for rid, s in (
      ("ic728", {"shift": "+"}), ("ic7300mk2", {"shift": "+", "offset": 600_000}), ("ft891", {"shift": "-", "offset": 600_000}),
      ("ic728", {"tone": "off"}), ("ic9700", {"tone": "dcs", "dcs": 23}))],
  ["A repeater shift can't be saved for the IC-728.", "An offset can't be saved for the IC-7300MK2.",
   "An offset can't be saved for the FT-891.", "A tone setting can't be saved for the IC-728.", "DCS can't be saved for the IC-9700."])
C("simplex, + and -: on every radio that has a shift",
  [rpt(rid, FM_AT[rid], shift=s) for rid in ("ic9700", "ic7300mk2", "ft991a", "ft891") for s in M.SHIFTS], [None] * 12)
FIVE = [{"tone": "off"}, {"tone": "tone", "ctcss": 670}, {"tone": "tsql", "ctcss": 2541}, {"tone": "dcs", "dcs": 23},
        {"tone": "dcs-enc", "dcs": 754}]
C("FT-991A and FT-891: all five tone settings (off, TONE, TSQL, DCS, DCS-ENC)",
  [rpt(rid, FM_AT[rid], **t) for rid in ("ft991a", "ft891") for t in FIVE], [None] * 10)
C("IC-9700 and IC-7300MK2: off, TONE and TSQL; DCS and DCS-ENC refused (TeleRig drives no DTCS)",
  [rpt(rid, FM_AT[rid], **t) for rid in ("ic9700", "ic7300mk2") for t in FIVE], [None, None, None, "item.tone", "item.tone"] * 2)
C("every standard CTCSS tone, in tenths of a hertz (67.0 Hz = 670 ... 254.1 Hz = 2541)",
  ([t for t in (round(x * 10) for x in civ.CTCSS_TONES) if rpt("ft991a", 146_940_000, tone="tone", ctcss=t)], sorted(M.CTCSS)[:3],
   len(M.CTCSS)), ([], [670, 693, 719], 50))
C("every standard DCS code, its three digits read as a decimal number (code 023 = 23 ... 754 = 754)",
  ([c for c in civ.DCS_CODES if rpt("ft991a", 146_940_000, tone="dcs", dcs=int(c))], sorted(M.DCS)[:3], len(M.DCS)),
  ([], [23, 25, 26], 104))

# the modes: a shift with FM, DATA-FM, C4FM and DV (the radio's own of them), a tone setting with FM, DATA-FM and C4FM
C("a shift in the FM modes and DV: IC-9700 FM and DV, FT-991A FM, DATA-FM and C4FM",
  [rpt("ic9700", 145_000_000, m, shift="-") for m in ("FM", "DV")] + [rpt("ft991a", 145_000_000, m, shift="+") for m in ("FM", "DATA-FM", "C4FM")],
  [None] * 5)
C("... and in no other mode: no shift in USB, AM, CW, DATA-USB",
  [rpt("ic9700", 144_200_000, "USB", shift="+"), rpt("ft991a", 145_000_000, "AM", shift="-"), rpt("ic7300mk2", 29_620_000, "CW", shift="+"),
   rpt("ft991a", 145_000_000, "DATA-USB", shift="simplex")], ["item.shift"] * 4)
C("... saying which mode", message_of(V, "ic9700", {"hz": 144_200_000, "mode": "USB", "name": "x", "shift": "+"}),
  "There is no repeater shift in USB.")
C("a shift and its offset in DV on the IC-9700, but no tone setting there",
  [rpt("ic9700", 145_000_000, "DV", shift="-", offset=600_000), rpt("ic9700", 145_000_000, "DV", tone="off"),
   rpt("ic9700", 145_000_000, "DV", shift="-", tone="tone", ctcss=885)], [None, "item.tone", "item.tone"])
C("... saying which mode", message_of(V, "ic9700", {"hz": 145_000_000, "mode": "DV", "name": "x", "tone": "off"}),
  "There is no tone setting in DV.")
C("a tone setting in FM, DATA-FM and C4FM (FT-991A), in no other mode",
  [rpt("ft991a", 145_000_000, m, tone="tsql", ctcss=885) for m in ("FM", "DATA-FM", "C4FM", "USB", "AM", "DATA-USB", "CW-USB")],
  [None, None, None, "item.tone", "item.tone", "item.tone", "item.tone"])

# a CTCSS tone with TONE and TSQL only, and needed there; a DCS code with DCS and DCS-ENC only, and needed there
tones = [
    ({"tone": "tone"}, "item.ctcss"), ({"tone": "tsql"}, "item.ctcss"), ({"tone": "dcs"}, "item.dcs"), ({"tone": "dcs-enc"}, "item.dcs"),
    ({"tone": "off", "ctcss": 885}, "item.ctcss"), ({"tone": "off", "dcs": 23}, "item.dcs"),
    ({"ctcss": 885}, "item.ctcss"), ({"dcs": 23}, "item.dcs"),                          # no tone setting at all
    ({"tone": "tone", "ctcss": 885, "dcs": 23}, "item.dcs"), ({"tone": "dcs", "dcs": 23, "ctcss": 885}, "item.ctcss"),
    ({"tone": "tsql", "dcs": 23}, "item.ctcss"),                                        # its own one missing: named first
    ({"tone": "dcs-enc", "ctcss": 885}, "item.ctcss"),
]
C("a CTCSS tone is needed by TONE and TSQL and refused with anything else; a DCS code likewise for DCS and DCS-ENC",
  [rpt("ft991a", 146_940_000, **t) for t, _ in tones], [f for _, f in tones])
C("... the refusals say which", [message_of(V, "ft991a", {**ITEM, **t}) for t in ({"tone": "tsql"}, {"tone": "dcs-enc"}, {"ctcss": 885},
                                                                                  {"tone": "tone", "ctcss": 885, "dcs": 23})],
  ["TSQL needs a CTCSS tone.", "DCS-ENC needs a DCS code.", "A CTCSS tone goes with tone or tsql only.",
   "A DCS code goes with dcs or dcs-enc only."])

# the offset: with a + or - shift; 100 Hz to 99.9999 MHz in 100 Hz steps; 10 MHz and up on the 1200 MHz band only
C("an offset needs a + or - shift (no shift, simplex: refused); a shift needs no offset (the radio keeps its own)",
  [rpt("ic9700", 147_240_000, offset=600_000), rpt("ic9700", 147_240_000, shift="simplex", offset=600_000),
   rpt("ic9700", 147_240_000, shift="+", offset=600_000), rpt("ic9700", 147_240_000, shift="-", offset=600_000),
   rpt("ic9700", 147_240_000, shift="+")], ["item.offset", "item.offset", None, None, None])
C("... saying so", message_of(V, "ic9700", {**ITEM, "shift": "simplex", "offset": 600_000}), "An offset needs a + or - shift.")
C("the offset's bounds on the 1200 MHz band: 100 Hz, 600 kHz, 9.9999 MHz, 10 MHz, 12 MHz, 99.9999 MHz",
  [rpt("ic9700", 1_282_000_000, shift="-", offset=o) for o in (100, 600_000, 9_999_900, 10_000_000, 12_000_000, 99_999_900)], [None] * 6)
C("10 MHz and up only from 1 GHz (the IC-9700 takes that digit on its 1200 MHz band only)",
  [rpt("ic9700", hz, shift="-", offset=o) for hz, o in ((145_000_000, 9_999_900), (145_000_000, 10_000_000), (440_000_000, 12_000_000),
                                                         (440_000_000, 99_999_900), (1_240_000_000, 10_000_000))],
  [None, "item.offset", "item.offset", "item.offset", None])
C("... saying so", message_of(V, "ic9700", {**ITEM, "shift": "-", "offset": 10_000_000}),
  "An offset of 10 MHz or more is for the 1200 MHz band only.")
C("past 99.9999 MHz there is no offset, also on the 1200 MHz band (CI-V 0D has no 100 MHz digit)",
  [rpt("ic9700", 1_282_000_000, shift="-", offset=o) for o in (100_000_000, 120_000_000)], ["item.offset", "item.offset"])
C("... saying so", message_of(V, "ic9700", {**ITEM, "hz": 1_282_000_000, "shift": "-", "offset": 100_000_000}),
  "The offset must be 100 Hz to 99.9999 MHz, in steps of 100 Hz.")

# ---- shape, type and range errors -> MemoryInvalid(field) ---------------------------------------------
bad = [
    (("nope", ITEM), "radio"),
    (("FT991A", ITEM), "radio"),
    ((None, ITEM), "radio"),
    ((5, ITEM), "radio"),
    ((["ft991a"], ITEM), "radio"),
    (("ft991a", None), "item"),
    (("ft991a", []), "item"),
    (("ft991a", "Simplex"), "item"),
    (("ft991a", {**ITEM, "tone": 100}), "item.tone"),
    (("ft991a", {**ITEM, "id": 7}), "item.id"),                    # an id is the server's to give
    (("ft991a", {**ITEM, "k" * 50: 1}), "item." + "k" * 32),
    (("ft991a", {**ITEM, "a\ud800": 1}), "item.a?"),               # named with what can be sent back
    (("ft991a", {}), "item.hz"),
    (("ft991a", {"mode": "FM", "name": "x"}), "item.hz"),
    (("ft991a", {**ITEM, "hz": True}), "item.hz"),                 # a bool is not an int
    (("ft991a", {**ITEM, "hz": 146520000.0}), "item.hz"),
    (("ft991a", {**ITEM, "hz": "146520000"}), "item.hz"),
    (("ft991a", {**ITEM, "hz": None}), "item.hz"),
    (("ft991a", {**ITEM, "hz": [146520000]}), "item.hz"),
    (("ft991a", {**ITEM, "hz": 0}), "item.hz"),
    (("ft991a", {"hz": 146520000, "name": "x"}), "item.mode"),
    (("ft991a", {**ITEM, "mode": 4}), "item.mode"),
    (("ft991a", {**ITEM, "mode": None}), "item.mode"),
    (("ft991a", {**ITEM, "mode": "fm"}), "item.mode"),
    (("ft991a", {**ITEM, "mode": ["FM"]}), "item.mode"),
    (("ft991a", {**ITEM, "mode": ""}), "item.mode"),
    (("ft991a", {"hz": 146520000, "mode": "FM"}), "item.name"),
    (("ft991a", {**ITEM, "name": 5}), "item.name"),
    (("ft991a", {**ITEM, "name": None}), "item.name"),
    (("ft991a", {**ITEM, "name": True}), "item.name"),
    (("ft991a", {**ITEM, "name": ["Simplex"]}), "item.name"),
    (("ft991a", {**ITEM, "name": ""}), "item.name"),
    (("ft991a", {**ITEM, "name": "  \t \n"}), "item.name"),
    (("ft991a", {**ITEM, "name": "x" * 33}), "item.name"),
    (("ft991a", {**ITEM, "name": "a\tb"}), "item.name"),
    (("ft991a", {**ITEM, "name": "a\nb"}), "item.name"),
    (("ft991a", {**ITEM, "name": "a\x00b"}), "item.name"),
    (("ft991a", {**ITEM, "name": "a\x1bb"}), "item.name"),
    (("ft991a", {**ITEM, "name": "a\x7fb"}), "item.name"),
    (("ft991a", {**ITEM, "name": "half a pair \ud83d"}), "item.name"),
    (("ft991a", {**ITEM, "data": 1}), "item.data"),
    (("ft991a", {**ITEM, "data": "true"}), "item.data"),
    (("ft991a", {**ITEM, "data": None}), "item.data"),
    (("ft991a", {**ITEM, "narrow": 0}), "item.narrow"),
    (("ft991a", {**ITEM, "narrow": "no"}), "item.narrow"),
    (("ft991a", {**ITEM, "narrow": None}), "item.narrow"),
    (("ft991a", {**ITEM, "filter": 0}), "item.filter"),
    (("ft991a", {**ITEM, "filter": 4}), "item.filter"),
    (("ft991a", {**ITEM, "filter": True}), "item.filter"),        # a bool is not an int
    (("ft991a", {**ITEM, "filter": 2.0}), "item.filter"),
    (("ft991a", {**ITEM, "filter": "2"}), "item.filter"),
    (("ft991a", {**ITEM, "filter": None}), "item.filter"),
    (("ft991a", {**ITEM, "width": -1}), "item.width"),
    (("ft991a", {**ITEM, "width": 100}), "item.width"),
    (("ft991a", {**ITEM, "width": False}), "item.width"),         # a bool is not an int
    (("ft991a", {**ITEM, "width": 5.5}), "item.width"),
    (("ft991a", {**ITEM, "width": "5"}), "item.width"),
    # the repeater settings: types, unknown strings, values that are not standard ones (what each radio can set: above)
    (("ft991a", {**ITEM, "shift": True}), "item.shift"),          # a bool is not text
    (("ft991a", {**ITEM, "shift": None}), "item.shift"),
    (("ft991a", {**ITEM, "shift": 1}), "item.shift"),
    (("ft991a", {**ITEM, "shift": ["+"]}), "item.shift"),
    (("ft991a", {**ITEM, "shift": "plus"}), "item.shift"),
    (("ft991a", {**ITEM, "shift": ""}), "item.shift"),
    (("ft991a", {**ITEM, "shift": "SIMPLEX"}), "item.shift"),
    (("ft991a", {**ITEM, "shift": "+600"}), "item.shift"),
    (("ic9700", {**ITEM, "shift": "+", "offset": True}), "item.offset"),    # a bool is not an int
    (("ic9700", {**ITEM, "shift": "+", "offset": 600000.0}), "item.offset"),
    (("ic9700", {**ITEM, "shift": "+", "offset": "600000"}), "item.offset"),
    (("ic9700", {**ITEM, "shift": "+", "offset": None}), "item.offset"),
    (("ic9700", {**ITEM, "shift": "+", "offset": 0}), "item.offset"),
    (("ic9700", {**ITEM, "shift": "+", "offset": 99}), "item.offset"),
    (("ic9700", {**ITEM, "shift": "+", "offset": 150}), "item.offset"),     # not in steps of 100 Hz
    (("ic9700", {**ITEM, "shift": "+", "offset": 600_050}), "item.offset"),
    (("ic9700", {**ITEM, "shift": "+", "offset": 100_000_000}), "item.offset"),
    (("ic9700", {**ITEM, "shift": "+", "offset": -600_000}), "item.offset"),
    (("ft991a", {**ITEM, "tone": True}), "item.tone"),
    (("ft991a", {**ITEM, "tone": None}), "item.tone"),
    (("ft991a", {**ITEM, "tone": "TSQL"}), "item.tone"),
    (("ft991a", {**ITEM, "tone": "ctcss"}), "item.tone"),
    (("ft991a", {**ITEM, "tone": "dtcs"}), "item.tone"),
    (("ft991a", {**ITEM, "tone": ""}), "item.tone"),
    (("ft991a", {**ITEM, "tone": "tsql", "ctcss": True}), "item.ctcss"),   # a bool is not an int
    (("ft991a", {**ITEM, "tone": "tsql", "ctcss": 885.0}), "item.ctcss"),
    (("ft991a", {**ITEM, "tone": "tsql", "ctcss": "885"}), "item.ctcss"),
    (("ft991a", {**ITEM, "tone": "tsql", "ctcss": None}), "item.ctcss"),
    (("ft991a", {**ITEM, "tone": "tsql", "ctcss": 88.5}), "item.ctcss"),   # tenths of a hertz, not hertz
    (("ft991a", {**ITEM, "tone": "tsql", "ctcss": 886}), "item.ctcss"),    # 88.6 Hz: not a standard tone
    (("ft991a", {**ITEM, "tone": "tsql", "ctcss": 0}), "item.ctcss"),
    (("ft991a", {**ITEM, "tone": "tsql", "ctcss": 2542}), "item.ctcss"),
    (("ft991a", {**ITEM, "tone": "dcs", "dcs": True}), "item.dcs"),        # a bool is not an int
    (("ft991a", {**ITEM, "tone": "dcs", "dcs": "023"}), "item.dcs"),
    (("ft991a", {**ITEM, "tone": "dcs", "dcs": 23.0}), "item.dcs"),
    (("ft991a", {**ITEM, "tone": "dcs", "dcs": None}), "item.dcs"),
    (("ft991a", {**ITEM, "tone": "dcs", "dcs": 19}), "item.dcs"),          # 023 read as octal: the digits count as decimal
    (("ft991a", {**ITEM, "tone": "dcs", "dcs": 24}), "item.dcs"),          # 024: no such code
    (("ft991a", {**ITEM, "tone": "dcs", "dcs": 0}), "item.dcs"),
    (("ft991a", {**ITEM, "tone": "dcs", "dcs": 755}), "item.dcs"),
]
C("every shape, type and range error names its field", [field_of(V, *a) for a, _ in bad], [f for _, f in bad])
msgs = [message_of(V, *a) for a, _ in bad]
C("every refusal has a short sentence for the user",
  [m for m in msgs if not (isinstance(m, str) and 8 <= len(m) <= 80 and m.endswith("."))], [])
C("... that can always be sent back (UTF-8)",
  [m for m in msgs if isinstance(m, str) and m.encode("utf-8", "replace").decode("utf-8") != m], [])
C("... and names the radio where that helps", (message_of(V, "ic7300mk2", ITEM), message_of(V, "ft991a", {**ITEM, "mode": "DV"})),
  ("That frequency is outside what the IC-7300MK2 receives.", "The FT-991A has no such mode."))

# ---- files: add / get / delete ---------------------------------------------------------------------
C("file name = hex of the canonical name (ADMIN, W1AW/P)",
  (M.path_for("ADMIN").name, M.path_for("W1AW/P").name), ("41444d494e.json", "573141572f50.json"))
C("path is <config>/memories", M.path_for("OP1").parent, DIR)
for u in ("OP1", "OP2", "OP3", "FULL", "W1AW/P", "RPT"):
    M.delete_file(u)
nf = M.path_for("NOFILE")
C("no memories yet -> rev 0, no radios, no error", M.get("NOFILE"), NONE)
C("a delete with no file -> rev 0, an empty list", M.delete("NOFILE", "ft991a", 1), {"rev": 0, "items": []})
C("... neither created a file or a lock file", (nf.exists(), nf.with_name(nf.name + ".lock").exists()), (False, False))
a1 = M.add("OP1", "ft991a", YAESU)
C("first add: rev 1, id 1, the radio's list", a1, {"rev": 1, "id": 1, "items": [{"id": 1, **YAESU}]})
FT8 = {"hz": 7074000, "mode": "DATA-USB", "name": "FT8 40 m"}
a2 = M.add("OP1", "ft991a", {**FT8, "name": "  FT8 40 m "})
C("second add: rev 2, id 2, the whole list in id order, the name stripped",
  a2, {"rev": 2, "id": 2, "items": [{"id": 1, **YAESU}, {"id": 2, **FT8}]})
a3 = M.add("OP1", "ic9700", ICOM)
C("another radio: a list of its own, the file's one id counter", a3, {"rev": 3, "id": 3, "items": [{"id": 3, **ICOM}]})
C("get: every radio's list, items as stored with their ids",
  M.get("OP1"), ({"rev": 3, "radios": {"ft991a": [{"id": 1, **YAESU}, {"id": 2, **FT8}], "ic9700": [{"id": 3, **ICOM}]}, "seeded": []}, False))
p1 = M.path_for("OP1")
C("the file: v, rev, next and a list per radio",
  stored("OP1"), {"v": 1, "rev": 3, "next": 4, "radios": {"ft991a": [{"id": 1, **YAESU}, {"id": 2, **FT8}],
                                                          "ic9700": [{"id": 3, **ICOM}]}})
w1 = written(p1)
C("an item that does not validate is not added, and nothing is written",
  (field_of(M.add, "OP1", "ft991a", {**ITEM, "hz": 1}), field_of(M.add, "OP1", "nope", ITEM), written(p1) == w1), ("item.hz", "radio", True))

d1 = M.delete("OP1", "ft991a", 1)
C("delete: rev+1, the radio's list afterwards", d1, {"rev": 4, "items": [{"id": 2, **FT8}]})
w1 = written(p1)
C("delete of an id that is not there (any more): the same answer, rev unchanged", M.delete("OP1", "ft991a", 1), d1)
C("... an id that never was, and one of another radio's list: the same", (M.delete("OP1", "ft991a", 77), M.delete("OP1", "ft991a", 3)),
  (d1, d1))
C("... nothing was written", written(p1) == w1, True)
C("... and the other radio still has its own", M.get("OP1")[0]["radios"]["ic9700"], [{"id": 3, **ICOM}])
C("delete of an unknown radio names the radio", [field_of(M.delete, "OP1", rid, 1) for rid in ("nope", None, "")], ["radio"] * 3)
a4 = M.add("OP1", "ft991a", ITEM)
C("an id is never used twice: after a delete the next add gets a new one",
  (a4["rev"], a4["id"], [i["id"] for i in a4["items"]], stored("OP1")["next"]), (5, 4, [2, 4], 5))
d2 = M.delete("OP1", "ic9700", 3)
C("the last memory of a radio: an empty list, and the radio leaves the file and the answer",
  (d2, sorted(stored("OP1")["radios"]), sorted(M.get("OP1")[0]["radios"])), ({"rev": 6, "items": []}, ["ft991a"], ["ft991a"]))
C("... the id counter stays", (stored("OP1")["next"], M.add("OP1", "ic9700", ICOM)["id"]), (5, 5))

# ---- a repeater memory round trip: add() / get() / the file ---------------------------------------------
C("add: an IC-9700 repeater memory, as given", M.add("RPT", "ic9700", RPT_ICOM), {"rev": 1, "id": 1, "items": [{"id": 1, **RPT_ICOM}]})
C("add: an FT-991A one, as given", M.add("RPT", "ft991a", RPT_YAESU), {"rev": 2, "id": 2, "items": [{"id": 2, **RPT_YAESU}]})
C("get: both back, as they were saved",
  M.get("RPT"), ({"rev": 2, "radios": {"ic9700": [{"id": 1, **RPT_ICOM}], "ft991a": [{"id": 2, **RPT_YAESU}]}, "seeded": []}, False))
d = stored("RPT")["radios"]
C("the file keeps them in the stored order", (list(d["ic9700"][0]), list(d["ft991a"][0])),
  (["id", "hz", "mode", "name", "shift", "offset", "tone", "ctcss"], ["id", "hz", "mode", "name", "shift", "tone", "dcs"]))
wr = written(M.path_for("RPT"))
C("a repeater memory the radio cannot keep is not added, and nothing is written",
  (field_of(M.add, "RPT", "ic9700", {**RPT_ICOM, "tone": "dcs", "dcs": 23}), field_of(M.add, "RPT", "ft991a", {**RPT_YAESU, "offset": 600000}),
   written(M.path_for("RPT")) == wr), ("item.tone", "item.offset", True))

# ---- one file per user ------------------------------------------------------------------------------
op2 = M.path_for("OP2")
C("only the caller's file was written", (p1.exists(), op2.exists()), (True, False))
C("OP2 has none of OP1's", M.get("OP2"), NONE)
C("OP2's own first add: rev 1, id 1", M.add("OP2", "ft991a", FT8), {"rev": 1, "id": 1, "items": [{"id": 1, **FT8}]})
w2 = written(op2)
M.add("OP1", "ft991a", {**ITEM, "name": "Another"})
M.delete("OP1", "ft991a", 2)
C("OP1's saves leave OP2's file as it was", written(op2) == w2, True)
C("OP1 has none of OP2's", [i["id"] for i in M.get("OP1")[0]["radios"]["ft991a"]], [4, 6])
M.add("W1AW/P", "ft991a", ITEM)
C("W1AW/P -> memories/573141572f50.json", (DIR / "573141572f50.json").exists(), True)

# ---- the limit: MAX_PER_RADIO per radio -------------------------------------------------------------
for n in range(M.MAX_PER_RADIO):
    last = M.add("FULL", "ft991a", {"hz": 144_000_000 + n * 12_500, "mode": "FM", "name": f"Ch {n + 1}"})
C("100 memories on one radio", (last["rev"], last["id"], len(last["items"])), (100, 100, 100))
pf = M.path_for("FULL")
wf = written(pf)
C("the 101st is refused: MemoryFull with a sentence for the user",
  message_of(M.add, "FULL", "ft991a", ITEM), "This radio already has 100 memories. Delete one to save another.")
C("... nothing was written", written(pf) == wf, True)
C("another radio of the same user still takes one", M.add("FULL", "ic9700", ICOM)["id"], 101)
C("delete one and there is room again: 99, then 100 with a new id",
  (len(M.delete("FULL", "ft991a", 50)["items"]), M.add("FULL", "ft991a", ITEM)["id"], len(M.get("FULL")[0]["radios"]["ft991a"])),
  (99, 102, 100))
C("... and full again", message_of(M.add, "FULL", "ft991a", ITEM) is not None, True)

# ---- a read never changes the file: what no longer validates is left out, not deleted -----------------
M.add("OP3", "ft991a", ITEM)
M.add("OP3", "bandsonly", {"hz": 7_100_000, "mode": "USB", "name": "40 m"})
del PROFILES["bandsonly"]                       # a radio this version does not have (any more)
p3 = M.path_for("OP3")
d = stored("OP3")
d["radios"]["ft991a"] += [
    {"id": 3, "hz": 146520000, "mode": "DV", "name": "a mode this radio does not have"},
    {"id": 4, "hz": 1296000000, "mode": "FM", "name": "outside its coverage"},
    {"id": "5", "hz": 146520000, "mode": "FM", "name": "an id that is not a number"},
    {"id": True, "hz": 146520000, "mode": "FM", "name": "a bool for an id"},
    {"id": 0, "hz": 146520000, "mode": "FM", "name": "ids start at 1"},
    {"hz": 146520000, "mode": "FM", "name": "no id"},
    {"id": 6, "hz": 146520000, "mode": "FM", "name": "an extra key", "squelch": 3},
    {"id": 8, "hz": 146520000, "mode": "FM", "name": "an offset the FT-991A can't set", "shift": "-", "offset": 600000},
    "not an object",
    {"id": 1, "hz": 145000000, "mode": "FM", "name": "the id 1 a second time"},
    {"id": 9, "hz": 145000000, "mode": "FM", "name": "  fine  ", "narrow": True},
]
d["radios"]["ic9700"] = "not a list"
del d["next"]                                   # a file written by hand
p3.write_text(json.dumps(d))
M._reset_for_tests()
w3 = written(p3)
C("hand-edited file: a radio that is not in PROFILES and items that do not validate are left out",
  M.get("OP3"), ({"rev": 2, "radios": {"ft991a": [{"id": 1, **ITEM}, {"id": 9, "hz": 145000000, "mode": "FM", "name": "fine",
                                                                 "narrow": True}]}, "seeded": []}, False))
C("... and the read left the file as it was", written(p3) == w3, True)
a = M.add("OP3", "ft991a", FT8)
C("a file without `next`: the new id is past every id in the file",
  (a["id"], [i["id"] for i in a["items"]], stored("OP3")["next"]), (10, [1, 9, 10], 11))
d = stored("OP3")
C("a save keeps what a read leaves out (the other radio, the items that do not validate)",
  (len(d["radios"]["ft991a"]), d["radios"]["bandsonly"], d["radios"]["ic9700"]),
  (13, [{"id": 2, "hz": 7_100_000, "mode": "USB", "name": "40 m"}], "not a list"))
x = M.delete("OP3", "ft991a", 3)
C("an item that is left out can still be deleted by its id",
  (x["rev"], [i["id"] for i in x["items"]], len(stored("OP3")["radios"]["ft991a"])), (4, [1, 9, 10], 12))
C("a radio whose list is not a list starts one at its next save", M.add("OP3", "ic9700", ICOM)["items"], [{"id": 11, **ICOM}])
p3.write_text(json.dumps({"radios": {"ft991a": [{"id": n, **ITEM} for n in (1, 2, 3)]}}))     # by hand: no v, rev or next
M._reset_for_tests()
x = M.delete("OP3", "ft991a", 3)
C("a file without `next`: a delete records it, so the id that went is not handed out again",
  (x, stored("OP3")["next"], M.add("OP3", "ft991a", FT8)["id"]), ({"rev": 1, "items": [{"id": 1, **ITEM}, {"id": 2, **ITEM}]}, 4, 4))

# ---- an unreadable or damaged file --------------------------------------------------------------------
p1.write_text("{not json")
M._reset_for_tests()
old = moved_aside(p1)
C("unreadable file -> rev 0, no radios, memories_error=True, nothing raised", M.get("OP1"), UNREADABLE)
C("a delete on it answers rev 0 and leaves it alone", (M.delete("OP1", "ft991a", 4), p1.read_text()), ({"rev": 0, "items": []}, "{not json"))
a = M.add("OP1", "ft991a", ITEM)
new = sorted(moved_aside(p1) - old)
C("the next add moves the unreadable file aside and starts a fresh one",
  (a, len(new), M.get("OP1")), ({"rev": 1, "id": 1, "items": [{"id": 1, **ITEM}]}, 1, ({"rev": 1, "radios": {"ft991a": [{"id": 1, **ITEM}]}, "seeded": []}, False)))
C("... the moved file holds what was there", (p1.parent / new[0]).read_text() if new else None, "{not json")
tops = [{"v": 2}, {"v": 1, "rev": -5}, {"rev": True}, {"rev": "3"}, {"next": 0}, {"next": "3"}, {"next": True}, {"radios": []},
        {"radios": None}, ["a", "list"]]
got = []
for top in tops:
    p1.write_text(json.dumps(top))
    M._reset_for_tests()
    got.append(M.get("OP1"))
C("a file with a bad top level is unreadable, not fatal", got, [UNREADABLE] * len(tops))
p1.write_text("{}")
M._reset_for_tests()
C("an empty object is a file with no memories", (M.get("OP1"), M.add("OP1", "ft991a", ITEM)["id"], stored("OP1")),
  (NONE, 1, {"v": 1, "rev": 1, "next": 2, "radios": {"ft991a": [{"id": 1, **ITEM}]}}))

# ---- presets: popular SSB frequencies, once per radio ----------------------------------------------
M._reset_for_tests()
M.delete_file("SEED")
pre = {r: M.preset_items(r) for r in ("ft991a", "ft891", "ic7300mk2", "ic9700", "ic728")}
C("preset_items: the ten presets on each HF radio, none on the IC-9700 (VHF / UHF)",
  {r: len(v) for r, v in pre.items()}, {"ft991a": 10, "ft891": 10, "ic7300mk2": 10, "ic9700": 0, "ic728": 10})
C("... the one the operator named first: 7.200 LSB, 40 m SSB 7.200",
  [i for i in pre["ft991a"] if i["hz"] == 7_200_000], [{"hz": 7_200_000, "mode": "LSB", "name": "40 m SSB 7.200", "narrow": False, "width": 14}])
C("... SSB at 2.4 kHz as each radio sets it: a Yaesu NAR off and WIDTH 14, an Icom FIL2 with DATA off, the IC-728 nothing",
  [{k: v for k, v in pre[r][0].items() if k not in ("hz", "mode", "name")} for r in ("ft891", "ic7300mk2", "ic728")],
  [{"narrow": False, "width": 14}, {"data": False, "filter": 2}, {}])
C("... the sideband by band: LSB below 10 MHz, USB above",
  sorted({(i["hz"] < 10_000_000, i["mode"]) for i in pre["ic7300mk2"]}), [(False, "USB"), (True, "LSB")])
C("... every preset is a valid item, its name within NAME_MAX, lowest first",
  [all(M.validate_item("ft991a", i) == i for i in pre["ft991a"]), max(len(i["name"]) for i in pre["ft991a"]) <= M.NAME_MAX,
   [i["hz"] for i in pre["ft991a"]] == sorted(i["hz"] for i in pre["ft991a"])], [True, True, True])
s1 = M.seed("SEED", "ft991a")
C("seed: all of them, rev 1, ids from 1, the radio noted",
  (s1["added"], s1["rev"], [i["id"] for i in s1["items"]], [i["name"] for i in s1["items"]] == [i["name"] for i in pre["ft991a"]],
   stored("SEED")["seeded"], M.get("SEED")[0]["seeded"]), (10, 1, list(range(1, 11)), True, ["ft991a"], ["ft991a"]))
ws = written(M.path_for("SEED"))
C("seed again: nothing added, the same answer, nothing written",
  (M.seed("SEED", "ft991a"), written(M.path_for("SEED")) == ws), ({"rev": 1, "items": s1["items"], "added": 0}, True))
M.delete("SEED", "ft991a", 3)
C("a preset deleted never comes back", ([i["hz"] for i in M.seed("SEED", "ft991a")["items"]].count(7_200_000), M.seed("SEED", "ft991a")["added"]), (0, 0))
own = M.add("SEED", "ft891", {"hz": 14_336_000, "mode": "USB", "name": "My county hunters"})
s2 = M.seed("SEED", "ft891")
C("a radio with a memory on a preset's frequency and mode: that preset left out, the others added after it",
  (s2["added"], [i["name"] for i in s2["items"]].count("County hunters 20 m"), s2["items"][0]["name"], stored("SEED")["seeded"]),
  (9, 0, "My county hunters", ["ft991a", "ft891"]))
s3 = M.seed("SEED", "ic9700")
C("a radio with no presets (the IC-9700): nothing added, but noted (the page asks once)",
  (s3["added"], s3["items"], "ic9700" in stored("SEED")["seeded"], "ic9700" in stored("SEED")["radios"]), (0, [], True, False))
C("seed of an unknown radio names the radio", [field_of(M.seed, "SEED", r) for r in ("nope", None, "")], ["radio"] * 3)
for k in range(95):
    M.add("NEAR", "ic7300mk2", {"hz": 14_000_000 + 100 * k, "mode": "USB", "name": f"m{k}"})
s4 = M.seed("NEAR", "ic7300mk2")
C("near the limit: only as many as fit (95 + 5 = MAX_PER_RADIO)", (s4["added"], len(s4["items"])), (5, M.MAX_PER_RADIO))
C("a file with a bad seeded list is read as none seeded", (M._seeded({"seeded": "ft991a"}), M._seeded({"seeded": ["ft991a", 3]})), ([], ["ft991a"]))
for u in ("SEED", "NEAR"):
    M.delete_file(u)

# ---- delete (user deleted) -----------------------------------------------------------------------
C("delete_file removes the file", (M.delete_file("W1AW/P"), M.path_for("W1AW/P").exists()), (True, False))
C("delete_file of a missing file -> False", M.delete_file("W1AW/P"), False)
C("after delete: no memories, and a user of that name starts again at id 1",
  (M.get("W1AW/P"), M.add("W1AW/P", "ft991a", ITEM)["id"]), (NONE, 1))
C("delete_file leaves the other users' files", (M.delete_file("OP2"), p1.exists(), p3.exists()), (True, True, True))
for u in ("OP1", "OP3", "FULL", "W1AW/P", "RPT"):
    M.delete_file(u)

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
