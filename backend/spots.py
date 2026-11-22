"""Spots for the Spots panel: POTA activators, WWFF activators and DX cluster spots, one list.

The server fetches each source itself, only when a page asks (GET /api/spots, while someone has the
Spots panel open) and never more often than that source allows; every page shares the cache. Nothing
of the user's is sent: fixed URLs, no query from the page, a User-Agent naming the app.

  pota  https://api.pota.app/spot/activator          at most once a minute (no published limit)
  wwff  https://spots.wwff.co/static/spots.json      at most once a minute (their floor is 30 s;
                                                     they ask for a User-Agent naming the app)
  dx    https://www.hamqth.com/api/v2/spots          at most every 30 s (their floor is 15 s); HamQTH
                                                     asks apps to say "Data provided by HamQTH.com"

Not included: SOTA (its API terms forbid AI-written software without SOTA's prior approval and
require developer registration) and DX Summit (no published terms for its API).

Every spot becomes {src, call, hz, mode, ref, name, spotter, time, note}: hz an int, mode one of
CW / SSB / FM / AM / DIGI / "" (unknown), time ISO UTC "...Z", text fields trimmed and capped (the
page shows them as text). A source that fails keeps its last good list, marked with the error.

Standard library only; no network at import time; tests swap `fetch` (a function url -> bytes).
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional

SOURCES = {
    "pota": {"url": "https://api.pota.app/spot/activator", "every": 60.0, "label": "POTA"},
    "wwff": {"url": "https://spots.wwff.co/static/spots.json", "every": 60.0, "label": "WWFF"},
    "dx":   {"url": "https://www.hamqth.com/api/v2/spots?limit=200", "every": 30.0, "label": "DX (HamQTH)"},
}
CREDIT = {"dx": "Data provided by HamQTH.com", "dx_url": "https://www.hamqth.com/"}
MAX_BYTES = 2 * 1024 * 1024           # a source's answer is a few tens of KB; refuse anything huge
MAX_AGE = 30 * 60                     # a spot older than 30 minutes is gone (POTA drops them at ~30 min)
MAX_SPOTS = 300                       # per source, newest kept
TIMEOUT = 8.0
_TEXT = {"call": 20, "ref": 24, "name": 80, "spotter": 20, "note": 120}
DIGI = {"FT8", "FT4", "JS8", "RTTY", "PSK", "PSK31", "PSK63", "DATA", "DIGI", "DIGITAL", "OLIVIA",
        "MFSK", "JT65", "JT9", "Q65", "FST4", "VARAC", "THOR", "HELL", "WSPR"}


def _txt(v: Any, n: int) -> str:
    s = "" if v is None else str(v)
    s = " ".join(s.split())                                   # one line, no control characters
    s = "".join(ch for ch in s if ch.isprintable())
    return s[:n]


def norm_mode(m: Any) -> str:
    m = _txt(m, 16).upper()
    if not m:
        return ""
    if m in ("CW", "SSB", "FM", "AM", "SSTV"):          # SSTV: a category of its own (the page tunes it as SSB)
        return m
    if m in ("USB", "LSB"):
        return "SSB"
    if m in DIGI or m.startswith(("FT", "PSK", "DATA")):
        return "DIGI"
    return ""


def sub_mode(m: Any) -> str:
    """What the source said beyond the mode, where the page tunes by it: the sideband of an SSB spot
    ("USB" | "LSB": that sideband, not the band's usual one) and the digital mode of a data spot
    ("FT8", "RTTY", "PSK31", ...: FT8 tunes the band's FT8 dial, RTTY puts the signal on the RTTY tones).
    "" when there is nothing more to say."""
    s = _txt(m, 16).upper()
    if s in ("USB", "LSB"):
        return s
    if norm_mode(s) == "DIGI" and s not in ("DATA", "DIGI", "DIGITAL") and re.fullmatch(r"[A-Z0-9-]{2,10}", s):
        return "PSK31" if s == "PSK" else s
    return ""


def _khz_to_hz(v: Any) -> Optional[int]:
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    hz = int(round(f * 1000))
    return hz if 100_000 <= hz <= 10_000_000_000 else None


def _iso(t: Any) -> Optional[str]:
    """POTA "2026-11-19T13:55:02" (UTC, no zone), HamQTH "...Z", WWFF unix seconds -> "...Z"."""
    try:
        if isinstance(t, (int, float)) and not isinstance(t, bool):
            d = datetime.fromtimestamp(float(t), tz=timezone.utc)
        else:
            s = str(t).strip().replace(" ", "T")
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            d = datetime.fromisoformat(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _spot(src, call, hz, mode, ref, name, spotter, time_iso, note) -> Optional[dict]:
    call = _txt(call, _TEXT["call"]).upper()
    if not call or hz is None or time_iso is None:
        return None
    return {"src": src, "call": call, "hz": hz, "mode": norm_mode(mode), "sub": sub_mode(mode), "ref": _txt(ref, _TEXT["ref"]),
            "name": _txt(name, _TEXT["name"]), "spotter": _txt(spotter, _TEXT["spotter"]).upper(),
            "time": time_iso, "note": _txt(note, _TEXT["note"])}


def parse_pota(data: Any) -> list:
    out = []
    for x in data if isinstance(data, list) else []:
        if not isinstance(x, dict) or x.get("invalid"):
            continue
        name = x.get("name") or x.get("parkName") or ""
        loc = x.get("locationDesc") or ""
        s = _spot("pota", x.get("activator"), _khz_to_hz(x.get("frequency")), x.get("mode"), x.get("reference"),
                  f"{name} ({loc})" if name and loc else name or loc, x.get("spotter"), _iso(x.get("spotTime")),
                  x.get("comments"))
        if s:
            out.append(s)
    return out


def parse_wwff(data: Any) -> list:
    out = []
    for x in data if isinstance(data, list) else []:
        if not isinstance(x, dict):
            continue
        s = _spot("wwff", x.get("activator"), _khz_to_hz(x.get("frequency_khz")), x.get("mode"), x.get("reference"),
                  x.get("reference_name"), x.get("spotter"), _iso(x.get("spot_time")), x.get("remarks"))
        if s:
            out.append(s)
    return out


def parse_hamqth(data: Any) -> list:
    out = []
    rows = data.get("spots") if isinstance(data, dict) else None
    for x in rows if isinstance(rows, list) else []:
        if not isinstance(x, dict):
            continue
        s = _spot("dx", x.get("dxcall"), _khz_to_hz(x.get("freq")), x.get("mode"), "",
                  x.get("country"), x.get("spotter"), _iso(x.get("time")), x.get("comment"))
        if s:
            out.append(s)
    return out


PARSERS = {"pota": parse_pota, "wwff": parse_wwff, "dx": parse_hamqth}


def _fetch_url(url: str, user_agent: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = r.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("answer too large")
    return body


class Spots:
    """The cache. get(sources) refreshes what is due (a source being fetched by another request is
    not fetched twice) and returns every requested source's spots, newest first."""

    def __init__(self, user_agent: str, *, fetch: Optional[Callable[[str], bytes]] = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self.user_agent = user_agent
        self.fetch = fetch or (lambda url: _fetch_url(url, self.user_agent))
        self.clock, self.wall = clock, wall
        self._mu = threading.Lock()
        self._src = {k: {"spots": [], "at": None, "tried": None, "error": None, "busy": False}
                     for k in SOURCES}

    def _refresh(self, key: str) -> None:
        s = self._src[key]
        with self._mu:
            now = self.clock()
            if s["busy"] or (s["tried"] is not None and now - s["tried"] < SOURCES[key]["every"]):
                return
            s["busy"], s["tried"] = True, now
        spots, error = None, None
        try:
            spots = PARSERS[key](json.loads(self.fetch(SOURCES[key]["url"]).decode("utf-8")))
        except Exception as e:                                  # offline, blocked, junk: keep the last list
            error = type(e).__name__ if not str(e) else f"{type(e).__name__}: {str(e)[:80]}"
        with self._mu:
            s["busy"] = False
            if spots is not None:
                spots.sort(key=lambda x: x["time"], reverse=True)
                s["spots"], s["at"], s["error"] = spots[:MAX_SPOTS], self.wall(), None
            else:
                s["error"] = error

    def get(self, sources) -> dict:
        keys = [k for k in SOURCES if k in set(sources)]
        for k in keys:
            self._refresh(k)
        cutoff = datetime.fromtimestamp(self.wall() - MAX_AGE, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out, status = [], {}
        with self._mu:
            for k in keys:
                s = self._src[k]
                fresh = [x for x in s["spots"] if x["time"] >= cutoff]
                out.extend(fresh)
                status[k] = {"label": SOURCES[k]["label"], "ok": s["error"] is None and s["at"] is not None,
                             "at": s["at"], "error": s["error"], "count": len(fresh)}
        out.sort(key=lambda x: x["time"], reverse=True)
        return {"spots": out, "sources": status, "credit": CREDIT, "now": self.wall()}
