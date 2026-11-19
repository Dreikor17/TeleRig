"""Per-user panel layouts (spec §3.7, §3.14, I22).

One file per user: <config_dir>/layouts/<hex>.json, where <hex> is the lowercase hex of the UTF-8
canonical user name (ADMIN -> 41444d494e). Callsigns may contain "/" and NTFS ignores case, so the
name itself is never used as a file name.

    {"v":1, "gs":"14.0.0", "rev":7, "updated":"...",
     "desktop":{"column":12, "items":[{"id":"cw","x":9,"y":9,"w":3,"h":15,"hidden":false,
                                       "collapsed":false,"room":{"from":"scope","dw":3}}, ...]},
     "phone":{"column":1, "items":[{"id":"vfo","x":0,"y":0,"w":1,"h":5,"hidden":false,"collapsed":false}, ...]},
     "scope_split":{"desktop":0.38, "phone":0.42}}

ONE bounds table (BOUNDS) is shipped to the browser in the bootstrap, so the client and the server
can never disagree (AR-26). validate() rejects only SHAPE and TYPE errors (LayoutInvalid ->
400 {error:"invalid", field}); numbers are CLAMPED, never rejected, and the stored (clamped) layout is
echoed so the client adopts it (I22). Overlap is not checked: it is resolved when the layout is applied.

A PUT replaces one device's layout and merges only that device's scope_split fraction: the other
device's layout and fraction are never touched (AR-54). Last write wins per device; `rev` counts
every write. Files are written atomically under the OS lock (store.JsonStore). A layout file that
cannot be read is reported (`layout_error`) and moved aside by the user's next save; it is the
user's own cosmetic data, so it never locks the server (only auth.json / config.json do, I6).

Standard library only; no file I/O at import time (I12).
"""
from __future__ import annotations

import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .store import JsonStore, StoreCorrupt, StoreMissing, console

TILE_IDS = ("vfo", "ptt", "audio", "meter", "scope", "controls", "cw", "recorder", "spots", "sstv")   # == layout.js TILES (§3.1)
DEVICES = ("desktop", "phone")
COLUMNS = {"desktop": 12, "phone": 1}
BOUNDS = {"desktop": {"cols": 12, "y_max": 199, "h_max": 96, "bottom_max": 200, "dw_max": 11},
          "phone": {"rank_max": 15, "h_max": 60},
          "split": [0.15, 0.85]}
BODY_MAX = 16 * 1024                 # bytes of a PUT body (413 above)
ITEMS_MAX = 16
GS_VERSION = "14.0.0"                # the vendored GridStack the layouts were made with (§3.4)
FILE_VERSION = 1
ITEM_KEYS = frozenset({"id", "x", "y", "w", "h", "hidden", "collapsed", "room"})
DATA_KEYS = frozenset({"column", "items"})


class LayoutInvalid(ValueError):
    """A shape or type error (-> 400 {error:"invalid", field}). Never raised for a number that is
    merely out of range: those are clamped."""

    def __init__(self, field: str, message: str = ""):
        super().__init__(message or f"invalid {field}")
        self.field = field
        self.message = message or f"Invalid layout: {field}."


# -- validation + clamping (I22) ----------------------------------------------------------------
def _is_int(v: Any) -> bool:
    return type(v) is int                      # a bool is NOT an int here


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


def clamp_split(v: Any, field: str = "scope_split") -> float:
    """A finite number (not a bool) clamped into BOUNDS["split"]; anything else is a type error.
    Clamped only, never rounded: the client compares the echoed value with its own clamped number."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise LayoutInvalid(field)
    f = float(v)
    if not math.isfinite(f):
        raise LayoutInvalid(field)
    lo, hi = BOUNDS["split"]
    return min(float(hi), max(float(lo), f))


def validate(device: Any, data: Any) -> dict:
    """The clamped, normalised layout of one device, or LayoutInvalid(field).

    Shape/type rules: device is desktop|phone; data is {"column", "items"} and nothing else; column
    is 12 (desktop) or 1 (phone); at most ITEMS_MAX items with unique ids from TILE_IDS; item keys
    from ITEM_KEYS only; ints are non-bool ints and flags are bools; `room {from, dw}` only on
    desktop. Desktop items need x, y, w, h; phone items need y (the RANK, AR-29), and h is optional.

    Clamping: desktop w into [1,12], then x into [0,12-w], h into [1,96], y into [0,200-h], room.dw
    into [1,11]; phone x=0, w=1, rank y into [0,15], h into [1,60]."""
    if device not in DEVICES:
        raise LayoutInvalid("device")
    if not isinstance(data, dict):
        raise LayoutInvalid("data")
    for k in data:
        if k not in DATA_KEYS:
            raise LayoutInvalid(str(k)[:32] if isinstance(k, str) else "data")
    col = data.get("column")
    if not _is_int(col) or col != COLUMNS[device]:
        raise LayoutInvalid("column")
    items = data.get("items")
    if not isinstance(items, list) or len(items) > ITEMS_MAX:
        raise LayoutInvalid("items")
    out, seen = [], set()
    for i, it in enumerate(items):
        f = f"items[{i}]"
        if not isinstance(it, dict):
            raise LayoutInvalid(f)
        for k in it:
            if k not in ITEM_KEYS:
                raise LayoutInvalid(f"{f}.{k}"[:48] if isinstance(k, str) else f)
        tid = it.get("id")
        if not isinstance(tid, str) or tid not in TILE_IDS or tid in seen:
            raise LayoutInvalid(f"{f}.id")
        seen.add(tid)
        for k in ("hidden", "collapsed"):
            if k in it and type(it[k]) is not bool:
                raise LayoutInvalid(f"{f}.{k}")
        need = ("x", "y", "w", "h") if device == "desktop" else ("y",)
        for k in ("x", "y", "w", "h"):
            if k in it:
                if not _is_int(it[k]):
                    raise LayoutInvalid(f"{f}.{k}")
            elif k in need:
                raise LayoutInvalid(f"{f}.{k}")
        o = {"id": tid}
        if device == "desktop":
            B = BOUNDS["desktop"]
            w = _clamp(it["w"], 1, B["cols"])
            x = _clamp(it["x"], 0, B["cols"] - w)
            h = _clamp(it["h"], 1, B["h_max"])
            y = _clamp(it["y"], 0, B["bottom_max"] - h)
            o.update(x=x, y=y, w=w, h=h)
        else:
            B = BOUNDS["phone"]
            o.update(x=0, y=_clamp(it["y"], 0, B["rank_max"]), w=1)
            if "h" in it:
                o["h"] = _clamp(it["h"], 1, B["h_max"])
        o["hidden"] = bool(it.get("hidden", False))
        o["collapsed"] = bool(it.get("collapsed", False))
        if "room" in it:
            rm = it["room"]
            if device != "desktop" or not isinstance(rm, dict) or set(rm) != {"from", "dw"}:
                raise LayoutInvalid(f"{f}.room")
            if not isinstance(rm["from"], str) or rm["from"] not in TILE_IDS:
                raise LayoutInvalid(f"{f}.room.from")
            if not _is_int(rm["dw"]):
                raise LayoutInvalid(f"{f}.room.dw")
            o["room"] = {"from": rm["from"], "dw": _clamp(rm["dw"], 1, BOUNDS["desktop"]["dw_max"])}
        out.append(o)
    return {"column": COLUMNS[device], "items": out}


def _validate_file(d: dict) -> None:
    """The stored file's top level (a JsonStore validator: a violation = unreadable layout)."""
    if d.get("v", FILE_VERSION) != FILE_VERSION:
        raise ValueError("unknown layout file version")
    rev = d.get("rev", 0)
    if not _is_int(rev) or rev < 0:
        raise ValueError("rev must be an int >= 0")
    for dev in DEVICES:
        if dev in d and not isinstance(d[dev], dict):
            raise ValueError(f"{dev} must be an object")
    ss = d.get("scope_split", {})
    if not isinstance(ss, dict):
        raise ValueError("scope_split must be an object")


# -- the files ----------------------------------------------------------------------------------
_mu = threading.Lock()
_stores: dict = {}                     # path -> JsonStore (one per file in this process)


def uid(user: str) -> str:
    return user.encode("utf-8").hex()


def layouts_dir() -> Path:
    from .auth import config_dir
    return config_dir() / "layouts"


def path_for(user: str) -> Path:
    return layouts_dir() / f"{uid(user)}.json"


def _store(user: str) -> JsonStore:
    p = path_for(user)
    with _mu:
        st = _stores.get(p)
        if st is None:
            st = _stores[p] = JsonStore(p, validate=_validate_file)
        return st


def _reset_for_tests() -> None:
    with _mu:
        _stores.clear()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(d: dict) -> dict:
    """A stored file as served (bootstrap, GET): each device re-validated (a hand-edited device
    with a shape error is dropped, so the client uses its default for it), splits clamped."""
    out = {"v": FILE_VERSION, "gs": d.get("gs") if isinstance(d.get("gs"), str) else GS_VERSION,
           "rev": d.get("rev", 0) if _is_int(d.get("rev", 0)) else 0,
           "updated": d.get("updated") if isinstance(d.get("updated"), str) else None}
    for dev in DEVICES:
        if isinstance(d.get(dev), dict):
            try:
                out[dev] = validate(dev, d[dev])
            except LayoutInvalid as e:
                console(f"TeleRig: stored {dev} layout dropped ({e.field})")
    ss = {}
    for dev, v in (d.get("scope_split") or {}).items():
        if dev in DEVICES:
            try:
                ss[dev] = clamp_split(v)
            except LayoutInvalid:
                pass
    out["scope_split"] = ss
    return out


def get(user: str) -> tuple:
    """(layout | None, layout_error). None = no saved layout (the client uses its defaults);
    layout_error = the file exists but cannot be read (the client may use its paint cache)."""
    try:
        st = _store(user)
        if st.status() == "missing":
            return None, False
        return _clean(st.load()), False
    except (StoreCorrupt, OSError, ValueError) as e:
        console(f"TeleRig: the layout of {user} cannot be read: {e}")
        return None, True


def put(user: str, device: str, data: dict, scope_split: Optional[float] = None) -> dict:
    """Store one device's (already validated) layout, merging only that device's split fraction.
    Returns {rev, layout, scope_split} as stored. An unreadable file is moved aside and replaced
    (the user's own cosmetic data; nothing else depends on it). StoreBusy/OSError propagate."""
    if device not in DEVICES:
        raise LayoutInvalid("device")
    lay = validate(device, data)                 # idempotent: callers may pass validated data
    split = None if scope_split is None else clamp_split(scope_split)

    def fn(d: dict) -> None:
        if not isinstance(d.get("scope_split"), dict):
            d["scope_split"] = {}
        d["v"] = FILE_VERSION
        d["gs"] = GS_VERSION
        d["rev"] = (d.get("rev") if _is_int(d.get("rev")) else 0) + 1
        d["updated"] = _now()
        d[device] = lay
        if split is not None:
            d["scope_split"][device] = split
    new = _store(user).mutate(fn, create=True, move_corrupt=True)
    return {"rev": new["rev"], "layout": new[device], "scope_split": (new.get("scope_split") or {}).get(device)}


def reset(user: str, device: Optional[str] = None) -> int:
    """Forget one device's layout and split (device given) or both (None): the client falls back to
    its defaults. The file is kept so `rev` keeps counting. Returns the new rev (0 if no file)."""
    if device is not None and device not in DEVICES:
        raise LayoutInvalid("device")
    st = _store(user)
    try:
        if st.status() == "missing":
            return 0
    except OSError:
        pass
    devs = DEVICES if device is None else (device,)

    def fn(d: dict) -> None:
        ss = d.get("scope_split") if isinstance(d.get("scope_split"), dict) else {}
        for dev in devs:
            d.pop(dev, None)
            ss.pop(dev, None)
        d["scope_split"] = ss
        d["v"] = FILE_VERSION
        d["gs"] = GS_VERSION
        d["rev"] = (d.get("rev") if _is_int(d.get("rev")) else 0) + 1
        d["updated"] = _now()
    return _store(user).mutate(fn, create=True, move_corrupt=True)["rev"]


def delete_file(user: str) -> bool:
    """The user was deleted: remove the layout file (under the OS lock). The persistent lock file
    stays (store.py never deletes lock files). True when a file was removed."""
    st = _store(user)
    removed = False
    try:
        with st.hold_lock(create=False):
            try:
                st.path.unlink()
                removed = True
            except FileNotFoundError:
                pass
    except StoreMissing:                      # no layouts folder at all
        pass
    with _mu:
        _stores.pop(st.path, None)            # a re-created user starts from a fresh cache
    return removed
