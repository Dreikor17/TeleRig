"""Per-user memories: frequencies an operator saved under a name, one list per radio.

One file per user: <config_dir>/memories/<hex>.json, where <hex> is the lowercase hex of the UTF-8
canonical user name, as for the layouts (layout_store.py).

    {"v":1, "rev":12, "next":10,
     "radios":{"ft991a":[{"id":7,"hz":146520000,"mode":"FM","name":"Simplex","narrow":false,"width":0},
                         {"id":9,"hz":146940000,"mode":"FM","name":"Hilltop","shift":"-","tone":"dcs","dcs":23}],
               "ic9700":[{"id":8,"hz":147240000,"mode":"FM","name":"Club","data":false,"filter":1,
                          "shift":"+","offset":600000,"tone":"tsql","ctcss":1000}]}}

`rev` counts every write. `next` is the id the next memory gets: ids start at 1, one counter for the
whole file, and an id is never used twice in a file, so a list another device still shows can never
delete a newer memory by an old id.

An item is what the radio was on: `hz` (inside the radio's receive coverage: the profile's
rx_ranges, else its bands' edges, as frontend/freqentry.js), `mode` (one of the profile's modes),
`name` (1..NAME_MAX characters, stored stripped, no control characters) and, optionally, how its
filter was set: `data` and `filter` (an Icom's DATA switch and FIL slot), `narrow` and `width` (a
Yaesu's NAR and WIDTH code), and its repeater settings, each only on a radio that can set it and in
a mode that has it:

  shift   "simplex" | "+" | "-". A Yaesu with the FM panel (capabilities.fm_tone: its OS command is
          there), any other radio with capabilities.duplex; in FM, DATA-FM, C4FM or DV.
  offset  the duplex offset in Hz, 100..99_999_900 in steps of 100, with a "+" or "-" shift, on a
          radio with capabilities.duplex_offset (CI-V 0D). 10 MHz or more only from 1 GHz up (the
          IC-9700 takes the 10 MHz digit on its 1200 MHz band only).
  tone    "off" | "tone" (a CTCSS tone on transmit) | "tsql" (tone squelch) | "dcs" | "dcs-enc" (DCS
          on transmit only). capabilities.fm_tone, in FM, DATA-FM or C4FM; the two DCS ones also need
          capabilities.fm_dcs.
  ctcss   the CTCSS tone in tenths of a hertz (885 = 88.5 Hz, one of civ.CTCSS_TONES): with "tone" and
          "tsql", which need it, and with nothing else.
  dcs     the DCS code, its three digits read as a decimal number (23 = code 023, one of
          civ.DCS_CODES): with "dcs" and "dcs-enc", which need it, and with nothing else.

validate_item() rejects every shape, type and range error and any other key (MemoryInvalid -> 400
{error:"invalid", field}); nothing is clamped. An item without repeater settings validates as it
always did, so the files written before they existed read the same. A radio holds at most
MAX_PER_RADIO memories (MemoryFull -> 409 {error:"full"}).

Presets: popular SSB frequencies (PRESETS) every list starts with, once per radio. seed() adds the
ones the radio can take (preset_items) and notes the radio in the file's "seeded" list, so a preset
the operator deleted never comes back; get() serves that list, and the page asks for the presets of a
radio not in it (the Memory view, frontend/spots.js).

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

from . import civ
from .profiles import PROFILES
from .store import JsonStore, StoreCorrupt, StoreMissing, console

MAX_PER_RADIO = 100                  # memories of one radio per user (409 above)
NAME_MAX = 32                        # characters of a name, after stripping
BODY_MAX = 2048                      # bytes of a POST body (413 above)
FILE_VERSION = 1
FLAGS = ("data", "narrow")                       # optional bools
RANGES = {"filter": (1, 3), "width": (0, 99)}    # optional ints, both ends included
ITEM_KEYS = ("hz", "mode", "name", "data", "filter", "narrow", "width",
             "shift", "offset", "tone", "ctcss", "dcs")              # in the order an item is stored
FILTER_KEYS = ITEM_KEYS[3:7]                     # how the filter was set: FLAGS and RANGES
# The repeater settings (the module docstring).
SHIFTS = ("simplex", "+", "-")
TONES = ("off", "tone", "tsql", "dcs", "dcs-enc")
SHIFT_MODES = frozenset({"FM", "DATA-FM", "C4FM", "DV"})          # the modes a shift goes with
TONE_MODES = frozenset({"FM", "DATA-FM", "C4FM"})                 # the modes a tone setting goes with
OFFSET_MIN, OFFSET_MAX, OFFSET_STEP = 100, 99_999_900, 100       # Hz: CI-V 0D, 3-byte BCD, 100 Hz LSB
OFFSET_10MHZ = 10_000_000            # an offset this big or bigger ...
OFFSET_10MHZ_FROM = 1_000_000_000    # ... only from this frequency up (the IC-9700's 1200 MHz band)
CTCSS = frozenset(round(t * 10) for t in civ.CTCSS_TONES)        # the standard tones, in tenths of Hz
DCS = frozenset(int(c) for c in civ.DCS_CODES)                   # the standard codes, digits read as decimal
# The presets: (hz, sideband, name), lowest first. Sources (read 2026-11-22): 7.200, the operator's own
# example of a popular 40 m SSB frequency; 28.400, the 10 m SSB calling frequency by custom (most of 10 m's
# SSB is 28.300-28.500); hwn.org's net procedures (the Hurricane Watch Net, when activated: 14.325 USB by
# day, 7.268 LSB by night; 7.268 is also the Waterway Net's morning net, which the HWN page names);
# countyhunter.com (the county hunters' common SSB frequencies: 14.336, 7.188, 3.901); iota-world.org (the
# IOTA meeting frequencies, SSB: 14.260 the main one, 21.260, 28.460). The calling frequencies (QRP, SSTV,
# AM, FT8, W1AW, the emergency centers) are in the Spots panel's Calling list (frontend/callfreqs.js).
PRESETS = (
    (3_901_000, "LSB", "County hunters 80 m"),
    (7_188_000, "LSB", "County hunters 40 m"),
    (7_200_000, "LSB", "40 m SSB 7.200"),
    (7_268_000, "LSB", "Hurricane Watch / Waterway"),
    (14_260_000, "USB", "IOTA 20 m"),
    (14_325_000, "USB", "Hurricane Watch Net"),
    (14_336_000, "USB", "County hunters 20 m"),
    (21_260_000, "USB", "IOTA 15 m"),
    (28_400_000, "USB", "10 m SSB calling"),
    (28_460_000, "USB", "IOTA 10 m"),
)
# SSB's 2.4 kHz the way each radio sets it: an Icom's FIL2 (2.4 kHz in the IC-7300MK2 and IC-9700 manuals),
# DATA off; a Yaesu's NAR off and WIDTH code 14 (2400 Hz in frontend/waterfall.js Passband, SH.ssb)
PRESET_FIL, PRESET_WIDTH = 2, 14


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
    "narrow" and "width", and the repeater settings "shift", "offset", "tone", "ctcss" and "dcs"
    (each only where the radio can set it: _repeater()), and nothing else. ints are non-bool ints,
    flags are bools, and a number out of range is an error like any other. The name is stripped;
    its length and its characters are checked on what is left."""
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
    for k in FILTER_KEYS:
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
    _repeater(prof, item, out)
    return out


def _repeater(prof: Any, item: dict, out: dict) -> None:
    """The item's repeater settings (the module docstring), checked one by one in ITEM_KEYS order and
    added to `out`, or MemoryInvalid: what the radio cannot set, a mode that has no such setting, a
    tone or code that is not a standard one, a CTCSS tone or DCS code missing where the tone setting
    needs it or given where it does not."""
    caps, mode = prof.capabilities, out["mode"]
    if "shift" in item:
        v = item["shift"]
        if not isinstance(v, str) or v not in SHIFTS:
            raise MemoryInvalid("item.shift", "The shift must be simplex, + or -.")
        if not (caps.fm_tone if prof.protocol == "yaesu" else caps.duplex):
            raise MemoryInvalid("item.shift", f"A repeater shift can't be saved for the {prof.name}.")
        if mode not in SHIFT_MODES:
            raise MemoryInvalid("item.shift", f"There is no repeater shift in {mode}.")
        out["shift"] = v
    if "offset" in item:
        v = item["offset"]
        if not _is_int(v) or not OFFSET_MIN <= v <= OFFSET_MAX or v % OFFSET_STEP:
            raise MemoryInvalid("item.offset", "The offset must be 100 Hz to 99.9999 MHz, in steps of 100 Hz.")
        if not caps.duplex_offset:
            raise MemoryInvalid("item.offset", f"An offset can't be saved for the {prof.name}.")
        if out.get("shift") not in ("+", "-"):
            raise MemoryInvalid("item.offset", "An offset needs a + or - shift.")
        if v >= OFFSET_10MHZ and out["hz"] < OFFSET_10MHZ_FROM:
            raise MemoryInvalid("item.offset", "An offset of 10 MHz or more is for the 1200 MHz band only.")
        out["offset"] = v
    if "tone" in item:
        v = item["tone"]
        if not isinstance(v, str) or v not in TONES:
            raise MemoryInvalid("item.tone", "The tone must be off, tone, tsql, dcs or dcs-enc.")
        if not caps.fm_tone:
            raise MemoryInvalid("item.tone", f"A tone setting can't be saved for the {prof.name}.")
        if mode not in TONE_MODES:
            raise MemoryInvalid("item.tone", f"There is no tone setting in {mode}.")
        if v in ("dcs", "dcs-enc") and not caps.fm_dcs:
            raise MemoryInvalid("item.tone", f"DCS can't be saved for the {prof.name}.")
        out["tone"] = v
    tone = out.get("tone")
    if tone in ("tone", "tsql"):
        if "ctcss" not in item:
            raise MemoryInvalid("item.ctcss", f"{tone.upper()} needs a CTCSS tone.")
        v = item["ctcss"]
        if not _is_int(v) or v not in CTCSS:
            raise MemoryInvalid("item.ctcss", "That is not a standard CTCSS tone.")
        out["ctcss"] = v
    elif "ctcss" in item:
        raise MemoryInvalid("item.ctcss", "A CTCSS tone goes with tone or tsql only.")
    if tone in ("dcs", "dcs-enc"):
        if "dcs" not in item:
            raise MemoryInvalid("item.dcs", f"{tone.upper()} needs a DCS code.")
        v = item["dcs"]
        if not _is_int(v) or v not in DCS:
            raise MemoryInvalid("item.dcs", "That is not a standard DCS code.")
        out["dcs"] = v
    elif "dcs" in item:
        raise MemoryInvalid("item.dcs", "A DCS code goes with dcs or dcs-enc only.")


def preset_items(radio: str) -> list:
    """The presets this radio can take, as items: on its amateur bands (a preset is somewhere to talk,
    not only to listen) and in a sideband it has, at SSB's 2.4 kHz the way it sets that (PRESET_FIL,
    PRESET_WIDTH; nothing on a radio without filter commands: the IC-728)."""
    prof = PROFILES.get(radio)
    if prof is None:
        return []
    caps, out = prof.capabilities, []
    for hz, sb, name in PRESETS:
        if sb not in prof.modes or not any(b.lo <= hz <= b.hi for b in prof.bands):
            continue
        it: dict = {"hz": hz, "mode": sb, "name": name}
        if prof.protocol == "yaesu":
            if caps.narrow:
                it["narrow"] = False
            if caps.ext_ops:
                it["width"] = PRESET_WIDTH
        elif caps.tab_rx:                            # an Icom's FIL slots (on its RX tab)
            if caps.data_mode:
                it["data"] = False
            it["filter"] = PRESET_FIL
        out.append(validate_item(radio, it))
    return out


def _seeded(d: dict) -> list:
    return [r for r in d["seeded"] if isinstance(r, str)] if isinstance(d.get("seeded"), list) else []


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
    """({"rev", "radios", "seeded"}, memories_error): what the user's file holds, as it is served. Only
    radios that are in PROFILES and have an item to show are listed, each in id order; "seeded" names
    the radios that have had their presets (seed()). No file: rev 0, no radios, none seeded.
    memories_error = the file exists but cannot be read (rev 0, no radios, none seeded)."""
    try:
        st = _store(user)
        if st.status() == "missing":
            return {"rev": 0, "radios": {}, "seeded": []}, False
        d = st.load()
    except (StoreCorrupt, OSError, ValueError) as e:
        console(f"TeleRig: the memories of {user} cannot be read: {e}")
        return {"rev": 0, "radios": {}, "seeded": []}, True
    radios = {}
    for rid, lst in _radios(d).items():
        items = _items(rid, lst) if rid in PROFILES else []
        if items:
            radios[rid] = items
    return {"rev": _rev(d), "radios": radios, "seeded": [r for r in _seeded(d) if r in PROFILES]}, False


def seed(user: str, radio: Any) -> dict:
    """Give `radio` its presets, once: the ones it can take (preset_items) that its list doesn't hold
    already (the same frequency in the same mode), as far as MAX_PER_RADIO allows; then the radio is
    noted in the file's "seeded" list, so a preset deleted later never comes back. Returns {rev,
    items, added}: the rev, that radio's list as served and how many presets went in (0, and the file
    as it was, for a radio seeded before). MemoryInvalid for an unknown radio. An unreadable file is
    moved aside and replaced, as add() does. StoreBusy/OSError propagate."""
    if not isinstance(radio, str) or radio not in PROFILES:
        raise MemoryInvalid("radio", "Unknown radio.")
    presets = preset_items(radio)
    got: dict = {"added": 0}

    def fn(d: dict) -> None:
        radios, seeded = _radios(d), _seeded(d)
        lst = _list(radios, radio)
        shown = _items(radio, lst)
        if radio in seeded:
            got.update(rev=_rev(d), items=shown)
            raise _Unchanged()
        have = {(it["hz"], it["mode"]) for it in shown}
        nxt, room = _next_id(d, radios), MAX_PER_RADIO - len(shown)
        for it in presets:
            if room <= 0:
                break
            if (it["hz"], it["mode"]) in have:
                continue
            lst.append({"id": nxt, **it})
            nxt += 1
            room -= 1
            got["added"] += 1
        if lst:
            radios[radio] = lst
        d.update(v=FILE_VERSION, rev=_rev(d) + 1, next=nxt, radios=radios, seeded=seeded + [radio])
    try:
        new = _store(user).mutate(fn, create=True, move_corrupt=True)
    except _Unchanged:
        return {"rev": got["rev"], "items": got["items"], "added": 0}
    return {"rev": new["rev"], "items": _items(radio, new["radios"].get(radio)), "added": got["added"]}


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
