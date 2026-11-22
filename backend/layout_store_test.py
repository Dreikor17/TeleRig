"""Layout store tests — run: python -m backend.layout_store_test

§3.7 / §3.14 / I22: one bounds table; shape and type errors are rejected (LayoutInvalid -> 400
{error:"invalid", field}); numbers are clamped, never rejected; a PUT replaces one device and merges
only that device's split fraction (AR-54); `rev` counts writes; files are layouts/<hex>.json; an
unreadable file is reported and replaced by the next save, never locking anything.

Everything happens in the scratch folder of $TELERIG_CONFIG (the runner sets it); the test refuses
to run against a real ~/.telerig or ~/.radio-webop. No server, radio or socket is involved.
"""
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
    raise SystemExit("REFUSING: layout_store_test needs a scratch TELERIG_CONFIG")

REPO = Path(__file__).resolve().parent.parent

# I12: importing the module (in a fresh child) touches no file: the layouts folder is not created
before = sorted(p.name for p in Path(cfg).parent.iterdir()) if Path(cfg).parent.is_dir() else []
r = subprocess.run([sys.executable, "-c", "import backend.layout_store"], cwd=str(REPO), capture_output=True, text=True)
after = sorted(p.name for p in Path(cfg).parent.iterdir()) if Path(cfg).parent.is_dir() else []
C("I12: import does no file I/O", (r.returncode, before == after), (0, True))

from backend import layout_store as L  # noqa: E402
from backend.layout_store import LayoutInvalid  # noqa: E402


def field_of(fn, *a):
    try:
        fn(*a)
    except LayoutInvalid as e:
        return e.field
    return None


# ---- the bounds table (shipped in the bootstrap, AR-26) ------------------------------------------
C("BOUNDS is the spec's single table", L.BOUNDS,
  {"desktop": {"cols": 12, "y_max": 199, "h_max": 96, "bottom_max": 200, "dw_max": 11},
   "phone": {"rank_max": 15, "h_max": 60}, "split": [0.15, 0.85]})
C("PANEL_IDS (== layout.js TILE_IDS)", L.PANEL_IDS, ("vfo", "audio", "scope", "controls", "recorder", "spots", "sstv"))
C("TILE_IDS = the panels + the retired cw, meter and ptt (a stored layout's CW, Meter or Transmit panel stays valid)",
  L.TILE_IDS, L.PANEL_IDS + ("cw", "meter", "ptt"))
C("BOUNDS is JSON (bootstrap)", json.loads(json.dumps(L.BOUNDS)), L.BOUNDS)

# ---- desktop: valid input passes through unchanged -------------------------------------------------
DESK = {"column": 12, "items": [
    {"id": "controls", "x": 0, "y": 0, "w": 3, "h": 24, "hidden": False, "collapsed": False},
    {"id": "vfo", "x": 3, "y": 0, "w": 4, "h": 5, "hidden": False, "collapsed": False},
    {"id": "cw", "x": 9, "y": 9, "w": 3, "h": 15, "hidden": True, "collapsed": False, "room": {"from": "scope", "dw": 3}}]}
C("a valid desktop layout is kept as is", L.validate("desktop", DESK), DESK)

# ---- clamping (I22): numbers are never rejected -----------------------------------------------------
c = L.validate("desktop", {"column": 12, "items": [
    {"id": "vfo", "x": 10, "y": 250, "w": 4, "h": 5},          # x+w > 12, y beyond the bottom
    {"id": "scope", "x": -3, "y": -1, "w": 20, "h": 500},       # w > 12, h > 96, negatives
    {"id": "cw", "x": 0, "y": 0, "w": 0, "h": 0, "room": {"from": "scope", "dw": 50}}]})
it = {i["id"]: i for i in c["items"]}
C("x+w>12 -> x pulled left", (it["vfo"]["x"], it["vfo"]["w"]), (8, 4))
C("y:250 -> y = 200-h", it["vfo"]["y"], 195)
C("w:20 -> 12, x -> 0; h:500 -> 96; y -> 0", (it["scope"]["w"], it["scope"]["x"], it["scope"]["h"], it["scope"]["y"]),
  (12, 0, 96, 0))
C("w:0/h:0 -> 1; room.dw:50 -> 11", (it["cw"]["w"], it["cw"]["h"], it["cw"]["room"]), (1, 1, {"from": "scope", "dw": 11}))
C("missing flags default to false", (it["vfo"]["hidden"], it["vfo"]["collapsed"]), (False, False))
p = L.validate("phone", {"column": 1, "items": [
    {"id": "vfo", "x": 3, "y": 99, "w": 2, "h": 70}, {"id": "scope", "y": 2, "h": 0}, {"id": "ptt", "y": -4}]})
pi = {i["id"]: i for i in p["items"]}
C("phone w:2 -> 1, x -> 0, rank 99 -> 15, h:70 -> 60", pi["vfo"], {"id": "vfo", "x": 0, "y": 15, "w": 1, "h": 60,
                                                                  "hidden": False, "collapsed": False})
C("phone h:0 -> 1; h optional (content-fit tiles)", (pi["scope"]["h"], "h" in pi["ptt"], pi["ptt"]["y"]), (1, False, 0))
C("split 0.1 -> 0.15, 0.9 -> 0.85, 0.5 kept, int ok", (L.clamp_split(0.1), L.clamp_split(0.9), L.clamp_split(0.5),
                                                       L.clamp_split(1)), (0.15, 0.85, 0.5, 0.85))
C("an in-range split is echoed exactly (clamped, never rounded)", L.clamp_split(0.382718281828), 0.382718281828)

# ---- shape and type errors -> LayoutInvalid(field) ------------------------------------------------
V = L.validate
bad = [
    (("tablet", DESK), "device"),
    (("desktop", []), "data"),
    (("desktop", {"column": 12}), "items"),
    (("desktop", {"column": 1, "items": []}), "column"),
    (("phone", {"column": 12, "items": []}), "column"),
    (("desktop", {"column": True, "items": []}), "column"),
    (("desktop", {"column": 12, "items": [], "extra": 1}), "extra"),
    (("desktop", {"column": 12, "items": [{"id": "vfo", "x": True, "y": 0, "w": 1, "h": 1}]}), "items[0].x"),
    (("desktop", {"column": 12, "items": [{"id": "vfo", "x": 1.5, "y": 0, "w": 1, "h": 1}]}), "items[0].x"),
    (("desktop", {"column": 12, "items": [{"id": "vfo", "x": "1", "y": 0, "w": 1, "h": 1}]}), "items[0].x"),
    (("desktop", {"column": 12, "items": [{"id": "vfo", "y": 0, "w": 1, "h": 1}]}), "items[0].x"),
    (("desktop", {"column": 12, "items": [{"id": "vfo", "x": 0, "y": 0, "w": 1, "h": 1, "hidden": 0}]}), "items[0].hidden"),
    (("desktop", {"column": 12, "items": [{"id": "vfo", "x": 0, "y": 0, "w": 1, "h": 1, "z": 1}]}), "items[0].z"),
    (("desktop", {"column": 12, "items": [{"id": "nope", "x": 0, "y": 0, "w": 1, "h": 1}]}), "items[0].id"),
    (("desktop", {"column": 12, "items": [{"id": "vfo", "x": 0, "y": 0, "w": 1, "h": 1}] * 2}), "items[1].id"),
    (("desktop", {"column": 12, "items": [{"id": "vfo", "x": 0, "y": 0, "w": 1, "h": 1, "room": {"from": "x", "dw": 1}}]}),
     "items[0].room.from"),
    (("desktop", {"column": 12, "items": [{"id": "vfo", "x": 0, "y": 0, "w": 1, "h": 1, "room": {"from": "cw", "dw": False}}]}),
     "items[0].room.dw"),
    (("desktop", {"column": 12, "items": [{"id": "vfo", "x": 0, "y": 0, "w": 1, "h": 1, "room": {"from": "cw"}}]}),
     "items[0].room"),
    (("phone", {"column": 1, "items": [{"id": "vfo", "y": 0, "room": {"from": "cw", "dw": 1}}]}), "items[0].room"),
    (("phone", {"column": 1, "items": [{"id": "vfo"}]}), "items[0].y"),
    (("phone", {"column": 1, "items": ["vfo"]}), "items[0]"),
    (("desktop", {"column": 12, "items": [{"id": t, "x": 0, "y": 0, "w": 1, "h": 1} for t in L.TILE_IDS] * 3}), "items"),
]
C("every shape/type error names its field", [field_of(V, *a) for a, _ in bad], [f for _, f in bad])
C("split: bool / str / NaN / inf / None are type errors",
  [field_of(L.clamp_split, v) for v in (True, "0.4", float("nan"), float("inf"), None)], ["scope_split"] * 5)

# ---- files: put / get / reset / delete --------------------------------------------------------------
C("file name = hex of the canonical name (ADMIN, W1AW/P)",
  (L.path_for("ADMIN").name, L.path_for("W1AW/P").name), ("41444d494e.json", "573141572f50.json"))
C("path is <config>/layouts", L.path_for("OP1").parent, Path(cfg).parent / "layouts")
for u in ("OP1", "OP2", "W1AW/P"):
    L.delete_file(u)
C("no layout yet -> (None, False)", L.get("OP1"), (None, False))
PH = {"column": 1, "items": [{"id": "vfo", "x": 0, "y": 0, "w": 1, "h": 5, "hidden": False, "collapsed": False}]}
r1 = L.put("OP1", "desktop", DESK, 0.38)
C("first put: rev 1, echoes the stored layout and split", (r1["rev"], r1["layout"], r1["scope_split"]), (1, DESK, 0.38))
r2 = L.put("OP1", "phone", PH, 0.1)
C("second put (phone): rev 2, split clamped 0.1 -> 0.15", (r2["rev"], r2["scope_split"]), (2, 0.15))
lay, err = L.get("OP1")
C("get: both devices, both splits (merged per device, AR-54)",
  (err, lay["rev"], lay["desktop"], lay["phone"]["items"][0]["id"], lay["scope_split"]),
  (False, 2, DESK, "vfo", {"desktop": 0.38, "phone": 0.15}))
r3 = L.put("OP1", "desktop", DESK)                         # no split: the stored ones are untouched
C("put without a split keeps both stored splits", (r3["rev"], r3["scope_split"], L.get("OP1")[0]["scope_split"]),
  (3, 0.38, {"desktop": 0.38, "phone": 0.15}))
r4 = L.put("OP1", "desktop", DESK, 0.6)
C("a desktop split never touches the phone split", L.get("OP1")[0]["scope_split"], {"desktop": 0.6, "phone": 0.15})
op2 = L.path_for("OP2")
C("only the caller's file was written", (L.path_for("OP1").exists(), op2.exists()), (True, False))
L.put("OP2", "desktop", DESK)
b2 = op2.read_bytes()
L.put("OP1", "phone", PH)
C("OP1's save leaves OP2's file byte-identical", op2.read_bytes(), b2)
L.put("W1AW/P", "desktop", DESK)
C("W1AW/P -> layouts/573141572f50.json", (Path(cfg).parent / "layouts" / "573141572f50.json").exists(), True)

rev = L.reset("OP1", "phone")
lay, _ = L.get("OP1")
C("reset(phone): rev+1, phone layout and split gone, desktop kept",
  (rev, "phone" in lay, lay["scope_split"], lay["desktop"] == DESK), (r4["rev"] + 2, False, {"desktop": 0.6}, True))
rev2 = L.reset("OP1")
lay, _ = L.get("OP1")
C("reset(all): rev keeps counting, no device left", (rev2, "desktop" in lay, "phone" in lay), (rev + 1, False, False))
C("reset of a user with no file -> rev 0, nothing created", (L.reset("NOFILE"), L.path_for("NOFILE").exists()), (0, False))
C("reset of an unknown device", field_of(L.reset, "OP1", "tablet"), "device")

# ---- a hand-edited or corrupt file -------------------------------------------------------------------
p1 = L.path_for("OP1")
d = json.loads(p1.read_text())
d["desktop"] = {"column": 12, "items": [{"id": "vfo", "x": "zero"}]}     # a device with a shape error
d["phone"] = {"column": 1, "items": [{"id": "vfo", "y": 40, "h": 900}]}  # out of range: clamped on read
d["scope_split"] = {"desktop": 7, "phone": "x"}
p1.write_text(json.dumps(d))
L._reset_for_tests()
lay, err = L.get("OP1")
C("hand-edited file: bad device dropped, numbers clamped on read",
  (err, "desktop" in lay, lay["phone"]["items"][0]["y"], lay["phone"]["items"][0]["h"], lay["scope_split"]),
  (False, False, 15, 60, {"desktop": 0.85}))
p1.write_text("{not json")
L._reset_for_tests()
C("unreadable file -> (None, layout_error=True), nothing raised", L.get("OP1"), (None, True))
r5 = L.put("OP1", "desktop", DESK, 0.4)
moved = sorted(x.name for x in p1.parent.iterdir() if x.name.startswith(p1.name + ".corrupt-"))
C("the next save moves the unreadable file aside and writes a fresh one",
  (r5["rev"], len(moved) == 1, L.get("OP1")[0]["desktop"] == DESK), (1, True, True))
p1.write_text(json.dumps({"v": 1, "rev": -5}))                          # top-level shape error
L._reset_for_tests()
C("a file with a bad top level is unreadable, not fatal", L.get("OP1"), (None, True))

# ---- delete (user deleted) -----------------------------------------------------------------------
C("delete_file removes the file", (L.delete_file("W1AW/P"), L.path_for("W1AW/P").exists()), (True, False))
C("delete_file of a missing file -> False", L.delete_file("W1AW/P"), False)
C("after delete: no layout", L.get("W1AW/P"), (None, False))
for u in ("OP1", "OP2"):
    L.delete_file(u)

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
