"""Audit log (spec §2.19, I21).

Two JSON Lines files in config_dir(), UTF-8, ISO-8601 UTC timestamps with milliseconds:
  audit-tx.log  TX accountability ONLY (tx_start, tx_end, tx_reattach, tx_stop_forced,
                tx_hb_timeout, tot_off, menu_critical, radio_connect, radio_disconnect,
                radio_transport_pinned, and the WSJT-X bridge's bridge_config, bridge_tx_paused,
                bridge_tx_resumed). 10 MiB per file, 10 files (current + .1 ... .9). Never
                coalesced or rate-limited, and nothing else writes here, so a user cannot flood
                their own TX records out.
  audit.log     security events (login_ok, login_fail, pw_changed, store_init, reset_admin,
                tx_denied, denied, panel_lock, rate_limited, ...). 2 MiB per file, current + .1 + .2.

One daemon writer thread with a 10,000-entry queue: log() never blocks and never raises; dropped
entries are counted and reported in the next written entry. Denial-type events (login_fail,
denied, tx_denied, panel_lock, rate_limited) are coalesced per (key, event, action/reason) in
60 s windows into one entry {ev, count, first, last, ...}; the key is the connection (or the
login source). After coalescing, at most one security entry per second is written per key; the
excess is counted into that key's next entry ("suppressed"). Secrets are never passed in.

Test hook: TELERIG_TEST_AUDIT_WINDOW=<s> shortens the coalescing window, honoured ONLY when
TELERIG_SIM_ONLY=1 (like TELERIG_TEST_SUDO_TTL), so the harness can read coalesced entries.

The target folder is resolved when an event is logged (never at import, I12).
"""
from __future__ import annotations

import atexit
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SEC_FILE, TX_FILE = "audit.log", "audit-tx.log"
SEC_MAX, SEC_KEEP = 2 * 1024 * 1024, 2
TX_MAX, TX_KEEP = 10 * 1024 * 1024, 9
QUEUE_MAX = 10000
COALESCE_WINDOW = 60.0
TX_EVENTS = frozenset({"tx_start", "tx_end", "tx_reattach", "tx_stop_forced", "tx_hb_timeout",
                       "tot_off", "menu_critical", "radio_connect", "radio_disconnect",
                       "radio_transport_pinned",
                       "bridge_config", "bridge_tx_paused", "bridge_tx_resumed"})
# The bridge's per-connection events are coalesced per peer (key "bridge:<ip>"): a program that
# reconnects for every poll must not push the sign-in history out of audit.log.
COALESCED = frozenset({"login_fail", "denied", "tx_denied", "panel_lock", "rate_limited",
                       "bridge_connect", "bridge_disconnect", "bridge_refused", "bridge_tx_refused"})
KEY_MIN_INTERVAL = 1.0           # at most one security entry per second per key (connection/source)
DEFER_MAX = 32                   # entries a key may have waiting for the cap; beyond: counted


def _env_on(name: str) -> bool:
    v = os.environ.get(name)
    return v is not None and v.strip().lower() not in ("", "0", "false", "no", "off")


def coalesce_window() -> float:
    """60 s; TELERIG_TEST_AUDIT_WINDOW overrides it ONLY when TELERIG_SIM_ONLY=1 (tests)."""
    v = os.environ.get("TELERIG_TEST_AUDIT_WINDOW")
    if v and _env_on("TELERIG_SIM_ONLY"):
        try:
            t = float(v)
            if 0 < t <= COALESCE_WINDOW:
                return t
        except ValueError:
            pass
    return COALESCE_WINDOW


def _ts(t: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(time.time() if t is None else t, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _dir() -> Path:
    from .auth import config_dir
    return config_dir()


class _Writer:
    def __init__(self) -> None:
        self._q: deque = deque()
        self._cv = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._dropped = 0
        self.dropped_total = 0           # since start (Admin -> Activity shows it)
        self._busy = False
        self._agg: dict = {}             # (dir, key, ev, sub) -> entry being coalesced
        self._key_last: dict = {}        # key -> time of its last written security entry
        self._key_suppressed: dict = {}  # key -> entries counted into the key's next entry
        self._deferred: dict = {}        # key -> deque of entries waiting for the 1/s cap

    # -- producer side (any thread) -------------------------------------------------------------
    def put(self, d: Path, rec: dict) -> None:
        with self._cv:
            if len(self._q) >= QUEUE_MAX:
                self._dropped += 1
                self.dropped_total += 1
                return
            self._q.append((d, rec))
            self._start()
            self._cv.notify()

    def coalesce(self, d: Path, key: str, ev: str, sub: str, fields: dict) -> None:
        now = time.time()
        with self._cv:
            k = (str(d), key, ev, sub)
            a = self._agg.get(k)
            if a is None:
                self._agg[k] = {"dir": d, "t0": now, "key": key,
                                "rec": {"ev": ev, **fields, "count": 1, "first": _ts(now), "last": _ts(now)}}
                self._start()
                self._cv.notify()
            else:
                a["rec"]["count"] += 1
                a["rec"]["last"] = _ts(now)
                for f in ("users",):
                    if f in fields and isinstance(a["rec"].get(f), list):
                        for x in fields[f]:
                            if x not in a["rec"][f] and len(a["rec"][f]) < 8:
                                a["rec"][f].append(x)

    def _start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True, name="tr-audit")
            self._thread.start()

    # -- consumer side ------------------------------------------------------------------------
    def _due(self, force: bool) -> list:
        now = time.time()
        win = coalesce_window()
        out = []
        for k in [k for k, a in self._agg.items() if force or now - a["t0"] >= win]:
            a = self._agg.pop(k)
            rec = a["rec"]
            if a.get("key"):
                rec["_key"] = str(a["key"])
            out.append((a["dir"], rec))
        return out

    def _run(self) -> None:
        while True:
            with self._cv:
                if not self._q:
                    self._cv.wait(timeout=0.5 if self._deferred else 1.0)
                batch = list(self._q)
                self._q.clear()
                batch += self._due(force=False)
                batch += self._release_deferred(force=False)
                dropped, self._dropped = self._dropped, 0
                self._busy = bool(batch)
            if batch:
                if dropped:
                    batch[0][1]["dropped_before"] = dropped
                self._write(batch)
            with self._cv:
                self._busy = False
                self._cv.notify_all()

    def _release_deferred(self, force: bool) -> list:
        """Entries held back by the per-key 1/s cap whose key may write again (all when forced)."""
        now = time.monotonic()
        out = []
        for key in list(self._deferred):
            dq = self._deferred[key]
            while dq and (force or now - self._key_last.get(key, -1e9) >= KEY_MIN_INTERVAL):
                d, rec = dq.popleft()
                rec["_key"] = key
                rec["_forced"] = force
                out.append((d, rec))
                if not force:
                    break
            if not dq:
                del self._deferred[key]
        return out

    def _cap(self, d, rec: dict, now: float) -> bool:
        """The per-key cap: True = write now; False = held back (deferred, or counted into the
        key's next entry when its backlog is full)."""
        key = rec.pop("_key", None)
        forced = rec.pop("_forced", False)
        if not key:
            return True
        if not forced and now - self._key_last.get(key, -1e9) < KEY_MIN_INTERVAL:
            dq = self._deferred.setdefault(key, deque())
            sub = (rec.get("ev"), rec.get("action"), rec.get("reason"))
            for _, w in dq:                       # same event already waiting: merge the counts
                if (w.get("ev"), w.get("action"), w.get("reason")) == sub and "count" in w and "count" in rec:
                    w["count"] += int(rec.get("count", 1) or 1)
                    w["last"] = rec.get("last", w.get("last"))
                    return False
            if len(dq) < DEFER_MAX:
                rec["_key"] = key
                dq.append((d, rec))
            else:
                self._key_suppressed[key] = self._key_suppressed.get(key, 0) + int(rec.get("count", 1) or 1)
            return False
        self._key_last[key] = now
        held = self._key_suppressed.pop(key, 0)
        if held:
            rec["suppressed"] = held
        if len(self._key_last) > 4096:
            for k in [k for k, t in self._key_last.items() if now - t > 120 and k not in self._deferred]:
                self._key_last.pop(k, None)
        return True

    def _write(self, batch: list) -> None:
        groups: dict = {}
        now = time.monotonic()
        for d, rec in batch:
            rec.setdefault("ts", _ts())
            name = TX_FILE if rec.get("ev") in TX_EVENTS else SEC_FILE
            if name == SEC_FILE:                  # <= 1 security entry / s per connection or source
                with self._cv:
                    if not self._cap(d, rec, now):
                        continue
            else:
                rec.pop("_key", None)
                rec.pop("_forced", None)
            groups.setdefault((str(d), name), []).append(rec)
        for (d, name), recs in groups.items():
            try:
                _append(Path(d) / name, recs, TX_MAX if name == TX_FILE else SEC_MAX,
                        TX_KEEP if name == TX_FILE else SEC_KEEP)
            except Exception:
                pass                                          # never raise; the server goes on

    def flush(self, timeout: float = 2.0) -> bool:
        """Write everything queued, including open coalescing windows. True when drained."""
        end = time.monotonic() + timeout
        with self._cv:
            for d, rec in self._due(force=True):
                rec["_forced"] = True
                self._q.append((d, rec))
            for d, rec in self._release_deferred(force=True):
                self._q.append((d, rec))
            if self._q:
                self._start()
                self._cv.notify()
            while (self._q or self._busy) and time.monotonic() < end:
                self._cv.wait(timeout=max(0.01, end - time.monotonic()))
            return not self._q and not self._busy


def _append(path: Path, recs: list, max_bytes: int, keep: int) -> None:
    lines = "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in recs).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size and size + len(lines) > max_bytes:
        _rotate(path, keep)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, lines)
    finally:
        os.close(fd)


def _rotate(path: Path, keep: int) -> None:
    for i in range(keep, 0, -1):
        src = path if i == 1 else path.with_name(f"{path.name}.{i - 1}")
        dst = path.with_name(f"{path.name}.{i}")
        try:
            if dst.exists() and i == keep:
                dst.unlink()
            if src.exists():
                os.replace(src, dst)
        except OSError:
            pass


_w = _Writer()
atexit.register(lambda: _w.flush(1.0))


def log(ev: str, *, key: Optional[str] = None, **fields) -> None:
    """Queue one event. Coalesced events need a `key` (connection id or source). Never raises."""
    try:
        d = _dir()
        clean = {k: v for k, v in fields.items() if v is not None or k in ("by",)}
        if ev in COALESCED:
            sub = str(fields.get("action") or fields.get("reason") or "")
            _w.coalesce(d, str(key or ""), ev, sub, clean)
        else:
            _w.put(d, {"ts": _ts(), "ev": ev, **clean})
    except Exception:
        pass


def flush(timeout: float = 2.0) -> bool:
    try:
        return _w.flush(timeout)
    except Exception:
        return False


def stats() -> dict:
    """{dropped: entries lost to a full queue since start} (Admin -> Activity)."""
    return {"dropped": int(getattr(_w, "dropped_total", 0))}


def tail(limit: int = 200, kind: str = "all") -> list:
    """Newest first, from the current and rotated files of kind tx | security | all. Files are read
    newest first (current, .1, .2, ...) and reading stops once `limit` entries of that kind are in
    hand, so a short tail never parses the whole rotated history."""
    names = {"tx": [TX_FILE], "security": [SEC_FILE]}.get(kind, [TX_FILE, SEC_FILE])
    limit = max(0, int(limit))
    out = []
    if not limit:
        return out
    try:
        d = _dir()
    except Exception:
        return out
    for name in names:
        keep = TX_KEEP if name == TX_FILE else SEC_KEEP
        got = 0
        for i in range(0, keep + 1):
            p = d / (name if i == 0 else f"{name}.{i}")
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
                    got += 1
            if got >= limit:
                break
    out.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
    return out[:limit]
