"""Memory store tests — run: python -m backend.memory_store_test

One memories file per user, a list per radio. Every field of an item is validated and nothing is
clamped: a shape, type or range error names its field, the frequency is checked against the radio's
own receive coverage and the mode against its own modes (the real profiles). Ids start at 1 and are
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
NONE = ({"rev": 0, "radios": {}}, False)
UNREADABLE = ({"rev": 0, "radios": {}}, True)

C("the limits: 100 a radio, a name of 32, a body of 2048 bytes", (M.MAX_PER_RADIO, M.NAME_MAX, M.BODY_MAX), (100, 32, 2048))

# ---- a valid item is stored as it is ------------------------------------------------------------------
C("a valid item is kept as is", V("ft991a", ITEM), ITEM)
C("the optional fields ride along (a Yaesu's NAR and WIDTH, an Icom's DATA and FIL)",
  (V("ft991a", YAESU), V("ic9700", ICOM)), (YAESU, ICOM))
C("one stored order, whatever the request's",
  list(V("ic9700", {"width": 5, "name": "N", "filter": 2, "mode": "USB", "narrow": True, "hz": 144200000, "data": True})),
  ["hz", "mode", "name", "data", "filter", "narrow", "width"])
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
for u in ("OP1", "OP2", "OP3", "FULL", "W1AW/P"):
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
  M.get("OP1"), ({"rev": 3, "radios": {"ft991a": [{"id": 1, **YAESU}, {"id": 2, **FT8}], "ic9700": [{"id": 3, **ICOM}]}}, False))
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
    {"id": 6, "hz": 146520000, "mode": "FM", "name": "an extra key", "tone": 100},
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
                                                                 "narrow": True}]}}, False))
C("... and the read left the file as it was", written(p3) == w3, True)
a = M.add("OP3", "ft991a", FT8)
C("a file without `next`: the new id is past every id in the file",
  (a["id"], [i["id"] for i in a["items"]], stored("OP3")["next"]), (10, [1, 9, 10], 11))
d = stored("OP3")
C("a save keeps what a read leaves out (the other radio, the items that do not validate)",
  (len(d["radios"]["ft991a"]), d["radios"]["bandsonly"], d["radios"]["ic9700"]),
  (12, [{"id": 2, "hz": 7_100_000, "mode": "USB", "name": "40 m"}], "not a list"))
x = M.delete("OP3", "ft991a", 3)
C("an item that is left out can still be deleted by its id",
  (x["rev"], [i["id"] for i in x["items"]], len(stored("OP3")["radios"]["ft991a"])), (4, [1, 9, 10], 11))
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
  (a, len(new), M.get("OP1")), ({"rev": 1, "id": 1, "items": [{"id": 1, **ITEM}]}, 1, ({"rev": 1, "radios": {"ft991a": [{"id": 1, **ITEM}]}}, False)))
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

# ---- delete (user deleted) -----------------------------------------------------------------------
C("delete_file removes the file", (M.delete_file("W1AW/P"), M.path_for("W1AW/P").exists()), (True, False))
C("delete_file of a missing file -> False", M.delete_file("W1AW/P"), False)
C("after delete: no memories, and a user of that name starts again at id 1",
  (M.get("W1AW/P"), M.add("W1AW/P", "ft991a", ITEM)["id"]), (NONE, 1))
C("delete_file leaves the other users' files", (M.delete_file("OP2"), p1.exists(), p3.exists()), (True, True, True))
for u in ("OP1", "OP3", "FULL", "W1AW/P"):
    M.delete_file(u)

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
