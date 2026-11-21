"""Per-user memories: frequencies an operator saved under a name, one list per radio.

One file per user: <config_dir>/memories/<hex>.json, where <hex> is the lowercase hex of the UTF-8
canonical user name, as for the layouts (layout_store.py).

    {"v":1, "rev":12, "next":9,
     "radios":{"ft991a":[{"id":7,"hz":146520000,"mode":"FM","name":"Simplex","narrow":false,"width":0}, ...],
               "ic9700":[{"id":8,"hz":145500000,"mode":"FM","name":"Calling","data":false,"filter":1}]}}

`rev` counts every write. `next` is the id the next memory gets: ids start at 1, one counter for the
whole file, and an id is never used twice in a file, so a list another device still shows can never
delete a newer memory by an old id.

An item is what the radio was on: `hz` (inside the radio's receive coverage: the profile's
rx_ranges, else its bands' edges, as frontend/freqentry.js), `mode` (one of the profile's modes),
`name` (1..NAME_MAX characters, stored stripped, no control characters) and, optionally, how its
filter was set: `data` and `filter` (an Icom's DATA switch and FIL slot), `narrow` and `width` (a
Yaesu's NAR and WIDTH code). validate_item() rejects every shape, type and range error and any
other key (MemoryInvalid -> 400 {error:"invalid", field}); nothing is clamped. A radio holds at most
MAX_PER_RADIO memories (MemoryFull -> 409 {error:"full"}).

Files are written atomically under the OS lock (store.JsonStore). A read never changes the file: a
radio that is not in PROFILES any more and an item that no longer validates are left out of what is
served, and stay in the file. A file that cannot be read is reported (`memories_error`) and moved
aside by the user's next save; it is the user's own data, so it never locks the server (only
auth.json / config.json do, I6).

Standard library only; no file I/O at import time (I12).
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

from .profiles import PROFILES
from .store import JsonStore, StoreCorrupt, StoreMissing, console

MAX_PER_RADIO = 100                  # memories of one radio per user (409 above)
NAME_MAX = 32                        # characters of a name, after stripping
BODY_MAX = 2048                      # bytes of a POST body (413 above)
FILE_VERSION = 1
FLAGS = ("data", "narrow")                       # optional bools
RANGES = {"filter": (1, 3), "width": (0, 99)}    # optional ints, both ends included
ITEM_KEYS = ("hz", "mode", "name", "data", "filter", "narrow", "width")   # in the order an item is stored


class MemoryInvalid(ValueError):
    """A shape, type or range error (-> 400 {error:"invalid", field}); `message` is for the user."""

    def __init__(self, field: str, message: str = ""):
        super().__init__(message or f"invalid {field}")
        self.field = field
        self.message = message or f"Invalid memory: {field}."


class MemoryFull(Exception):
    """The radio already holds MAX_PER_RADIO memories (-> 409 {error:"full"}); nothing is written."""

    message = f"This radio already has {MAX_PER_RADIO} memories. Delete one to save another."

    def __init__(self):
        super().__init__(self.message)


class _Unchanged(Exception):
    """Raised inside a mutate() to leave the file exactly as it is (there was nothing to delete)."""


# -- validation ------------------------------------------------------------------------------------
def _is_int(v: Any) -> bool:
    return type(v) is int                      # a bool is NOT an int here


def key_name(k: Any) -> str:
    """An unknown key as an error names it: its first 32 characters, safe to send back."""
    return str(k)[:32].encode("utf-8", "replace").decode("utf-8")


def _covers(profile: Any, hz: int) -> bool:
    """hz is inside the radio's receive coverage: its rx_ranges, else its bands' edges."""
    ranges = profile.rx_ranges or [(b.lo, b.hi) for b in profile.bands]
    return any(lo <= hz <= hi for lo, hi in ranges)


def validate_item(radio: Any, item: Any) -> dict:
    """The item as it is stored (without its id), or MemoryInvalid(field).

    radio is a profile id. item is {"hz", "mode", "name"} plus, optionally, "data", "filter",
    "narrow" and "width", and nothing else. ints are non-bool ints, flags are bools, and a number
    out of range is an error like any other. The name is stripped; its length and its characters
    are checked on what is left."""
    prof = PROFILES.get(radio) if isinstance(radio, str) else None
    if prof is None:
        raise MemoryInvalid("radio", "Unknown radio.")
    if not isinstance(item, dict):
        raise MemoryInvalid("item", "A memory needs a frequency, a mode and a name.")
    for k in item:
        if k not in ITEM_KEYS:
            raise MemoryInvalid(f"item.{key_name(k)}", f"Unknown field {key_name(k)!r}.")
    hz = item.get("hz")
    if not _is_int(hz):
        raise MemoryInvalid("item.hz", "The frequency must be a whole number of hertz.")
    if not _covers(prof, hz):
        raise MemoryInvalid("item.hz", f"That frequency is outside what the {prof.name} receives.")
    mode = item.get("mode")
    if not isinstance(mode, str) or mode not in prof.modes:
        raise MemoryInvalid("item.mode", f"The {prof.name} has no such mode.")
    name = item.get("name")
    if not isinstance(name, str):
        raise MemoryInvalid("item.name", "The name must be text.")
    name = name.strip()
    if not name:
        raise MemoryInvalid("item.name", "Give the memory a name.")
    if len(name) > NAME_MAX:
        raise MemoryInvalid("item.name", f"The name can be {NAME_MAX} characters at most.")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise MemoryInvalid("item.name", "The name can't hold line breaks or other control characters.")
    try:
        name.encode("utf-8")                   # half of a character pair: it could not be sent back
    except UnicodeEncodeError:
        raise MemoryInvalid("item.name", "The name contains characters that can't be used.") from None
    out = {"hz": hz, "mode": mode, "name": name}
    for k in ITEM_KEYS[3:]:
        if k not in item:
            continue
        v = item[k]
        if k in FLAGS:
            if type(v) is not bool:
                raise MemoryInvalid(f"item.{k}", f"{k} must be true or false.")
        else:
            lo, hi = RANGES[k]
            if not _is_int(v) or not lo <= v <= hi:
                raise MemoryInvalid(f"item.{k}", f"{k} must be a whole number from {lo} to {hi}.")
        out[k] = v
    return out


def _stored(radio: str, it: Any) -> Optional[dict]:
    """A stored item as it is served ({"id", ...the item}), or None when it does not validate."""
    if not isinstance(it, dict) or not _is_int(it.get("id")) or it["id"] < 1:
        return None
    try:
        return {"id": it["id"], **validate_item(radio, {k: v for k, v in it.items() if k != "id"})}
    except MemoryInvalid:
        return None


def _items(radio: str, lst: Any) -> list:
    """What is served of one radio's stored list: the items that validate, in id order (an id that
    a hand-edited file holds twice is served once)."""
    out: dict = {}
    for it in (lst if isinstance(lst, list) else ()):
        ok = _stored(radio, it)
        if ok is not None:
            out.setdefault(ok["id"], ok)
    return [out[i] for i in sorted(out)]


def _validate_file(d: dict) -> None:
    """The stored file's top level (a JsonStore validator: a violation = unreadable file)."""
    if d.get("v", FILE_VERSION) != FILE_VERSION:
        raise ValueError("unknown memories file version")
    rev = d.get("rev", 0)
    if not _is_int(rev) or rev < 0:
        raise ValueError("rev must be an int >= 0")
    nxt = d.get("next", 1)
    if not _is_int(nxt) or nxt < 1:
        raise ValueError("next must be an int >= 1")
    if not isinstance(d.get("radios", {}), dict):
        raise ValueError("radios must be an object")


# -- the files ----------------------------------------------------------------------------------
_mu = threading.Lock()
_stores: dict = {}                     # path -> JsonStore (one per file in this process)


def uid(user: str) -> str:
    return user.encode("utf-8").hex()


def memories_dir() -> Path:
    from .auth import config_dir
    return config_dir() / "memories"


def path_for(user: str) -> Path:
    return memories_dir() / f"{uid(user)}.json"


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


def _radios(d: dict) -> dict:
    return d["radios"] if isinstance(d.get("radios"), dict) else {}


def _list(radios: dict, radio: str) -> list:
    return radios[radio] if isinstance(radios.get(radio), list) else []


def _next_id(d: dict, radios: dict) -> int:
    """The id the next memory gets: the file's `next`, moved past every id the file holds (a
    hand-written file may carry no `next`, or a low one), so an id is never used twice."""
    nxt = d["next"] if _is_int(d.get("next")) and d["next"] >= 1 else 1
    for lst in radios.values():
        for it in (lst if isinstance(lst, list) else ()):
            if isinstance(it, dict) and _is_int(it.get("id")) and it["id"] >= nxt:
                nxt = it["id"] + 1
    return nxt


def _rev(d: dict) -> int:
    return d["rev"] if _is_int(d.get("rev")) else 0


def get(user: str) -> tuple:
    """({"rev", "radios"}, memories_error): what the user's file holds, as it is served. Only radios
    that are in PROFILES and have an item to show are listed, each in id order. No file: rev 0 and
    no radios. memories_error = the file exists but cannot be read (rev 0, no radios)."""
    try:
        st = _store(user)
        if st.status() == "missing":
            return {"rev": 0, "radios": {}}, False
        d = st.load()
    except (StoreCorrupt, OSError, ValueError) as e:
        console(f"TeleRig: the memories of {user} cannot be read: {e}")
        return {"rev": 0, "radios": {}}, True
    radios = {}
    for rid, lst in _radios(d).items():
        items = _items(rid, lst) if rid in PROFILES else []
        if items:
            radios[rid] = items
    return {"rev": _rev(d), "radios": radios}, False


def add(user: str, radio: Any, item: Any) -> dict:
    """Keep one more memory for `radio`. Returns {rev, id, items}: the new rev, the id it got and
    that radio's list as served. MemoryInvalid and MemoryFull leave the file as it is. An unreadable
    file is moved aside and replaced (the user's own data; nothing else depends on it).
    StoreBusy/OSError propagate."""
    it = validate_item(radio, item)              # idempotent: callers may pass validated data
    got: dict = {}

    def fn(d: dict) -> None:
        radios = _radios(d)
        lst = _list(radios, radio)
        if len(_items(radio, lst)) >= MAX_PER_RADIO:     # what the user sees is what counts
            raise MemoryFull()
        got["id"] = _next_id(d, radios)
        lst.append({"id": got["id"], **it})
        radios[radio] = lst
        d.update(v=FILE_VERSION, rev=_rev(d) + 1, next=got["id"] + 1, radios=radios)
    new = _store(user).mutate(fn, create=True, move_corrupt=True)
    return {"rev": new["rev"], "id": got["id"], "items": _items(radio, new["radios"][radio])}


def delete(user: str, radio: Any, mem_id: int) -> dict:
    """Forget the memory `mem_id` of `radio`. Returns {rev, items}: the rev and that radio's list as
    served, afterwards. An id that is not there is not an error: nothing is written and the rev
    stays as it is (0 with no file, and with one that cannot be read: its next save replaces it).
    MemoryInvalid for an unknown radio. StoreBusy/OSError propagate."""
    if not isinstance(radio, str) or radio not in PROFILES:
        raise MemoryInvalid("radio", "Unknown radio.")
    st = _store(user)
    if st.check() != "ok":                       # nothing to delete from; no lock file is left behind
        return {"rev": 0, "items": []}
    same: dict = {}

    def fn(d: dict) -> None:
        radios = _radios(d)
        lst = _list(radios, radio)
        keep = [it for it in lst
                if not (isinstance(it, dict) and _is_int(it.get("id")) and it["id"] == mem_id)]
        if len(keep) == len(lst):
            same.update(rev=_rev(d), items=_items(radio, lst))
            raise _Unchanged()
        nxt = _next_id(d, radios)                # before it goes: its id stays used
        if keep:
            radios[radio] = keep
        else:
            radios.pop(radio, None)
        d.update(v=FILE_VERSION, rev=_rev(d) + 1, next=nxt, radios=radios)
    try:
        new = st.mutate(fn)
    except _Unchanged:
        return same
    except (StoreMissing, StoreCorrupt):         # gone or damaged since the check above
        return {"rev": 0, "items": []}
    return {"rev": new["rev"], "items": _items(radio, new["radios"].get(radio))}


def delete_file(user: str) -> bool:
    """The user was deleted: remove the memories file (under the OS lock). The persistent lock file
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
    except StoreMissing:                      # no memories folder at all
        pass
    with _mu:
        _stores.pop(st.path, None)            # a re-created user starts from a fresh cache
    return removed
