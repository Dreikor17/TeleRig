"""config.json accessors (spec §2.3): allowed hosts, behind_proxy, host-audio pins, radio
transports, last radio, the WSJT-X bridge settings, and a raw read of the v1 keys.

Lenient by design (AR-51): config.json is "corrupt" (locked mode) only when it is not valid JSON
or its top level is not an object. Unknown keys are ignored. A known key with the wrong type is
ignored with a console warning and its default is used. Allowlist entries are normalised on read:
names are lowercased and a bare name loses any ":port" (v1 stored entries exactly as typed);
origins get their default port made explicit; IPs, localhost, *.localhost and junk are dropped with
a warning. Nothing is ever written back by a read: only explicit writes (admin saves, the Serve
helpers and the startup reconcile() of a stale behind_proxy, §2.6) go through mutate(). The v1 keys
(session_secret, password_hash) are read once by the account migration and never deleted (R10).

Standard library only; no file I/O at import time (the path resolves lazily, §2.1).
"""
from __future__ import annotations

import ipaddress
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from .store import JsonStore, StoreCorrupt, StoreMissing, console

__all__ = ["StoreCorrupt", "View", "path", "store", "status", "load", "raw", "view", "mutate",
           "allowed_hosts", "allowed_names", "allowed_origins", "behind_proxy",
           "behind_proxy_source", "host_audio", "radio_transports", "last_radio",
           "set_behind_proxy", "clear_managed_behind_proxy", "proxy_source_managed",
           "MANAGED_PROXY_SOURCES", "add_allowed_host", "normalize_entry", "bridge", "set_bridge",
           "bridge_paused", "set_bridge_paused"]

_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
TRANSPORT_KINDS = ("sim", "serial", "lan")


# -- the store (re-resolved when the config folder changes, e.g. after the ~/.telerig copy) ----
_mu = threading.Lock()
_store: Optional[JsonStore] = None
_cache: tuple = (None, None)                 # ((path, version), View)
_warned: set = set()


def path() -> Path:
    from .auth import config_file            # lazy: auth imports this module
    return config_file()


def store() -> JsonStore:
    global _store
    p = path()
    with _mu:
        if _store is None or _store.path != p:
            _store = JsonStore(p)
        return _store


def _reset_for_tests() -> None:
    global _store, _cache
    with _mu:
        _store = None
        _cache = (None, None)
        _warned.clear()


def status() -> str:
    """ok | missing | corrupt."""
    return store().status()


def load() -> dict:
    """Raw content (deep copy; {} when missing). Raises StoreCorrupt when unparsable."""
    return store().load()


raw = load                                    # the v1 keys (session_secret, password_hash) live here


def mutate(fn: Callable[[dict], Optional[dict]], create: bool = True) -> dict:
    """Explicit write (admin save). Never called by a read path."""
    return store().mutate(fn, create=create)


# -- normalisation --------------------------------------------------------------------------
def _is_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h.strip("[]"))
        return True
    except ValueError:
        return False


def _forbidden_host(h: str) -> bool:
    return h == "localhost" or h.endswith(".localhost") or _is_ip(h)


def normalize_entry(entry: str) -> tuple:
    """("name", "radio-pc") or ("origin", "https://pc.tailnet.ts.net:443"); ValueError otherwise."""
    if not isinstance(entry, str):
        raise ValueError(f"not a string: {entry!r}")
    e = entry.strip()
    if not e:
        raise ValueError("empty entry")
    if "://" in e:
        try:
            u = urlparse(e)
            port = u.port
        except ValueError:
            raise ValueError(f"bad origin {entry!r}") from None
        scheme = (u.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"origin {entry!r} is not http(s)")
        if u.path not in ("", "/") or u.query or u.fragment or u.params or u.username or u.password:
            raise ValueError(f"origin {entry!r} must be scheme://host[:port] only")
        host = (u.hostname or "").lower()
        if host.endswith(".") and not host.endswith(".."):
            host = host[:-1]
        if not host or _forbidden_host(host):
            raise ValueError(f"origin {entry!r}: IPs and localhost cannot be allowlisted")
        if not _NAME_RE.match(host):
            raise ValueError(f"origin {entry!r}: invalid host name")
        if port is None:
            port = 443 if scheme == "https" else 80
        if not 1 <= port <= 65535:
            raise ValueError(f"origin {entry!r}: invalid port")
        return "origin", f"{scheme}://{host}:{port}"
    h = e.lower()
    if h.startswith("[") or h.count(":") > 1:
        raise ValueError(f"{entry!r}: IPs cannot be allowlisted")
    if ":" in h:
        h, _, p = h.partition(":")
        if not p.isdigit():
            raise ValueError(f"{entry!r}: bad port")
    if h.endswith(".") and not h.endswith(".."):
        h = h[:-1]
    if not h or _forbidden_host(h):
        raise ValueError(f"{entry!r}: IPs and localhost cannot be allowlisted")
    if not _NAME_RE.match(h):
        raise ValueError(f"{entry!r}: invalid host name")
    return "name", h


def _origin_host(origin: str) -> str:
    return origin.split("://", 1)[1].rsplit(":", 1)[0]


@dataclass(frozen=True)
class View:
    allowed_hosts: tuple = ()              # normalised entries (names and full origins), in order
    allowed_names: frozenset = frozenset() # hostnames of every entry (feeds host_ok)
    allowed_origins: frozenset = frozenset()  # full origins only (feeds origin_ok, I15)
    behind_proxy: bool = False
    behind_proxy_source: Optional[str] = None
    host_audio: dict = field(default_factory=dict)        # {radio: {"rx": name|None, "tx": name|None}}
    radio_transports: dict = field(default_factory=dict)  # {radio: {transport, port?, baud?, host?}}
    last_radio: Optional[str] = None
    warnings: tuple = ()
    # the WSJT-X bridge (backend/bridge.py): off, local and receive-only unless an admin says so
    bridge: dict = field(default_factory=dict)
    # a bridge transmit pause in force when TeleRig stopped: {"reason", "by", "at"} or None
    bridge_paused: Optional[dict] = None


def normalize(data: dict) -> View:
    """Build the lenient View of a parsed config.json (never raises)."""
    warn: list = []

    entries: list = []
    ah = data.get("allowed_hosts", [])
    if ah is None:
        ah = []
    if not isinstance(ah, list):
        warn.append(f"allowed_hosts is {type(ah).__name__}, not a list; ignored")
        ah = []
    for x in ah:
        try:
            _, v = normalize_entry(x)
        except ValueError as e:
            warn.append(f"allowed_hosts entry dropped: {e}")
            continue
        if v not in entries:
            entries.append(v)
    origins = frozenset(v for v in entries if "://" in v)
    names = frozenset(_origin_host(v) if "://" in v else v for v in entries)

    bp = data.get("behind_proxy", False)
    if not isinstance(bp, bool):
        warn.append(f"behind_proxy is {type(bp).__name__}, not true/false; using false")
        bp = False
    src = data.get("behind_proxy_source")
    if src is not None and not isinstance(src, str):
        warn.append("behind_proxy_source is not a string; ignored")
        src = None

    pins: dict = {}
    ha = data.get("host_audio", {})
    if ha is None:
        ha = {}
    if not isinstance(ha, dict):
        warn.append("host_audio is not an object; ignored")
        ha = {}
    for rid, pin in ha.items():
        if not isinstance(pin, dict):
            warn.append(f"host_audio[{rid!r}] is not an object; ignored")
            continue
        p = {}
        for k in ("rx", "tx"):
            v = pin.get(k)
            if v is not None and not isinstance(v, str):
                warn.append(f"host_audio[{rid!r}].{k} is not a device name; ignored")
                v = None
            p[k] = v
        pins[rid] = p

    tps: dict = {}
    rt = data.get("radio_transports", {})
    if rt is None:
        rt = {}
    if not isinstance(rt, dict):
        warn.append("radio_transports is not an object; ignored")
        rt = {}
    for rid, t in rt.items():
        if not isinstance(t, dict) or t.get("transport") not in TRANSPORT_KINDS:
            warn.append(f"radio_transports[{rid!r}] is not a valid transport; ignored")
            continue
        if "user" in t or "password" in t:
            warn.append(f"radio_transports[{rid!r}] holds LAN credentials; they are ignored")
        clean = {"transport": t["transport"]}
        for k, typ in (("port", (str, int)), ("baud", int), ("host", str)):
            v = t.get(k)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, typ):
                warn.append(f"radio_transports[{rid!r}].{k} has the wrong type; ignored")
                continue
            clean[k] = v
        tps[rid] = clean

    lr = data.get("last_radio")
    if lr is not None and not isinstance(lr, str):
        warn.append("last_radio is not a string; ignored")
        lr = None

    from .bridge import normalize_config          # lazy: bridge imports nothing from here
    bcfg, bwarn = normalize_config(data.get("bridge"))
    warn.extend(bwarn)

    bpause = _pause_info(data.get("bridge_paused"))
    if data.get("bridge_paused") is not None and bpause is None:
        warn.append("bridge_paused is not a pause record; ignored")

    return View(tuple(entries), names, origins, bp, src, pins, tps, lr, tuple(warn), bcfg, bpause)


def _pause_info(raw) -> Optional[dict]:
    """A bridge pause record, cleaned: {"reason": str, "by": str | None, "at": float | None}."""
    if not isinstance(raw, dict) or not isinstance(raw.get("reason"), str) or not raw["reason"].strip():
        return None
    by, at = raw.get("by"), raw.get("at")
    return {"reason": raw["reason"].strip()[:32], "by": by[:64] if isinstance(by, str) else None,
            "at": float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None}


def view() -> View:
    """The normalised settings (cached per file version). Raises StoreCorrupt when unparsable."""
    global _cache
    st = store()
    if st.status() == "corrupt":
        raise StoreCorrupt(st.path, st.detail())
    key = (str(st.path), st.version)
    cached_key, cached = _cache
    if cached_key == key and cached is not None:
        return cached
    v = normalize(st.load())
    for w in v.warnings:
        k = (str(st.path), w)
        if k not in _warned:
            _warned.add(k)
            console(f"TeleRig: config warning ({st.path}): {w}")
    _cache = (key, v)
    return v


# -- accessors --------------------------------------------------------------------------------
def allowed_hosts() -> tuple:
    return view().allowed_hosts


def allowed_names() -> frozenset:
    return view().allowed_names


def allowed_origins() -> frozenset:
    return view().allowed_origins


def behind_proxy() -> bool:
    return view().behind_proxy


def behind_proxy_source() -> Optional[str]:
    return view().behind_proxy_source


# Who set behind_proxy. TeleRig manages a flag whose source is "tailscale" (Remote -> Turn on, or
# Serve set up by hand as REMOTE-ACCESS.md says) or None (a v1 flag): it is reported stale and may be
# cleared when Tailscale confirms Serve is off. Any other source (the documented "manual", for an
# nginx / socat / ssh -R / ... front TeleRig cannot see, R4) was set by hand in config.json: it is
# never stale and TeleRig never clears it (startup reconcile, Turn off, Clear flag); only an edit of
# config.json removes it.
MANAGED_PROXY_SOURCES = (None, "tailscale")


def proxy_source_managed(source: Optional[str]) -> bool:
    return source in MANAGED_PROXY_SOURCES


def host_audio() -> dict:
    return {k: dict(v) for k, v in view().host_audio.items()}


def radio_transports() -> dict:
    return {k: dict(v) for k, v in view().radio_transports.items()}


def last_radio() -> Optional[str]:
    return view().last_radio


def bridge() -> dict:
    """The WSJT-X bridge settings (normalised; defaults when the key is absent)."""
    from .bridge import DEFAULTS
    b = dict(DEFAULTS, **(view().bridge or {}))
    return dict(b, allow=list(b.get("allow") or []))


def bridge_paused() -> Optional[dict]:
    """The bridge transmit pause that was in force when TeleRig last stopped (None: not paused).
    A pause outlives a restart: only an operator's resume ends it (backend/bridge.py)."""
    return view().bridge_paused


def set_bridge_paused(info: Optional[dict]) -> None:
    """Remember a bridge transmit pause (or forget it: None) across a restart."""
    clean = _pause_info(info)
    if clean is None and status() == "missing":
        return                                   # nothing to forget; do not create the file for it

    def fn(d: dict) -> None:
        if clean is None:
            if "bridge_paused" not in d:
                raise _NoChange()
            d.pop("bridge_paused", None)
        else:
            d["bridge_paused"] = clean
    try:
        mutate(fn, create=True)
    except _NoChange:
        pass


def set_bridge(cfg: dict) -> None:
    """An admin's save (already validated by bridge.validate_config)."""
    clean = {k: cfg[k] for k in ("enabled", "port", "listen", "ptt", "silence_s")}
    clean["allow"] = list(cfg.get("allow") or [])

    def fn(d: dict) -> None:
        d["bridge"] = clean
    mutate(fn, create=True)


# -- writers (explicit actions only) ------------------------------------------------------------
def set_behind_proxy(value: bool, source: Optional[str] = None) -> None:
    value = bool(value)
    if not value and status() == "missing":
        return                                   # already off; do not create the file for it

    def fn(d: dict) -> None:
        d["behind_proxy"] = value
        d["behind_proxy_source"] = source if value else None
    mutate(fn, create=True)


class _NoChange(Exception):
    pass


def clear_managed_behind_proxy() -> bool:
    """Clear behind_proxy unless it was set by hand (proxy_source_managed). Decided under the store
    lock on what is on disk, so a hand edit that lands first is never undone, and nothing at all is
    written when there is nothing TeleRig may clear (a hand-edited file keeps its formatting).
    True when the flag was cleared."""
    if status() == "missing":
        return False                             # no file, no flag; do not create one for it

    def fn(d: dict) -> None:
        src = d.get("behind_proxy_source")
        if d.get("behind_proxy") is not True or not proxy_source_managed(src if isinstance(src, str) else None):
            raise _NoChange
        d["behind_proxy"] = False
        d["behind_proxy_source"] = None
    try:
        mutate(fn, create=True)
    except _NoChange:
        return False
    return True


def add_allowed_host(host: str) -> None:
    host = (host or "").strip()
    if not host:
        return

    def fn(d: dict) -> None:
        hosts = d.get("allowed_hosts")
        if not isinstance(hosts, list):
            hosts = []
        if host.lower() not in {h.lower() for h in hosts if isinstance(h, str)}:
            hosts.append(host)
        d["allowed_hosts"] = hosts
    mutate(fn, create=True)


__all__.append("StoreMissing")
