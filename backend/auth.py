"""App-layer access control — the ONLY thing protecting the transmitter once TeleRig is
reachable off the host PC.

Phase 1 (in progress). This module now has:
- the lazy config folder (§2.1, I12): nothing is read, created or copied at import time;
  `config_dir(migrate=True)` (called by accounts.ensure_initialized) does the one-time copy of an
  old ~/.radio-webop folder to ~/.telerig.
- the exact Origin rule (§2.8, I15): an Origin must match the effective request host AND port, or
  equal a full origin (scheme://host:port) in the allowlist. A missing Origin is refused; callers
  check it on WebSocket upgrades and on every non-GET/HEAD request.
- the v1 shared-password session code, now reading and writing config.json through
  backend/settings.py (no file creation on read). Step A3 replaces it with per-user accounts.

Fail closed: an unparseable/ambiguous client IP is treated as remote; an unrecognized Host is
rejected. Config lives in ~/.telerig/ (override with TELERIG_CONFIG, the path of config.json; the
old RADIO_WEBOP_CONFIG is still honoured).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from . import settings

SESSION_TTL = 30 * 24 * 3600          # signed-cookie lifetime (seconds)
COOKIE_NAME = "tr_session"


# -- config folder (lazy; §2.1) ---------------------------------------------------------------
_CACHED: Optional[Path] = None


def config_dir(*, migrate: bool = False) -> Path:
    """The folder holding config.json, auth.json, layouts/ and the audit logs.

    TELERIG_CONFIG (or the old RADIO_WEBOP_CONFIG) names config.json; its folder is the config
    folder. Otherwise ~/.telerig. When only an old ~/.radio-webop exists, a read-only caller gets
    the old folder (nothing is copied or cached); migrate=True (ensure_initialized only) copies it
    to ~/.telerig once — a copy, never a move, so an older install keeps its own — and caches."""
    global _CACHED
    if _CACHED:
        return _CACHED
    env = os.environ.get("TELERIG_CONFIG") or os.environ.get("RADIO_WEBOP_CONFIG")
    if env:
        d = Path(env).parent
    else:
        new, old = Path.home() / ".telerig", Path.home() / ".radio-webop"
        d = new
        if not new.exists() and old.is_dir():
            if not migrate:
                return old                  # read-only caller before init: never copy, never cache
            try:
                shutil.copytree(old, new)
            except Exception:
                d = old                     # couldn't migrate -> keep using the old folder
    if migrate:
        _CACHED = d
    return d


def config_file() -> Path:
    """config.json. With TELERIG_CONFIG/RADIO_WEBOP_CONFIG set, exactly that file (v1 semantics);
    otherwise config.json in config_dir()."""
    env = os.environ.get("TELERIG_CONFIG") or os.environ.get("RADIO_WEBOP_CONFIG")
    if env:
        return Path(env)
    return config_dir() / "config.json"


def _reset_for_tests() -> None:
    global _CACHED
    _CACHED = None


# -- v1 shared password (removed in A3) ---------------------------------------------------------
def _v1(key: str):
    return settings.raw().get(key)


def auth_enabled() -> bool:
    """Remote auth is ON exactly when a password has been set (v1 rule: any truthy value, so a
    malformed hash still demands a login that cannot succeed — fail closed)."""
    return bool(_v1("password_hash"))


def _ensure_secret(d: dict) -> str:
    s = d.get("session_secret")
    if not isinstance(s, str) or not s:
        s = secrets.token_hex(32)
        d["session_secret"] = s
    return s


def set_password(pw: str) -> None:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)

    def fn(d: dict) -> None:
        _ensure_secret(d)                   # the only place the v1 session secret is created
        d["password_hash"] = base64.b64encode(salt + h).decode()
    settings.mutate(fn, create=True)


def clear_password() -> None:
    if settings.status() == "missing":
        return

    def fn(d: dict) -> None:
        d.pop("password_hash", None)
    settings.mutate(fn, create=False)


def verify_password(pw: str) -> bool:
    stored = _v1("password_hash")
    if not isinstance(stored, str) or not stored:
        return False
    try:
        raw = base64.b64decode(stored)
        salt, h = raw[:16], raw[16:]
        test = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(h, test)
    except Exception:
        return False


def _secret() -> Optional[str]:
    s = _v1("session_secret")
    return s if isinstance(s, str) and s else None


# -- stateless HMAC-signed session cookie (v1; removed in A3) ---------------------------------
def make_session() -> str:
    secret = _secret()
    if secret is None:                      # hand-edited config with a password but no secret:
        def fn(d: dict) -> None:            # create it now (a write after a successful login)
            _ensure_secret(d)
        secret = _ensure_secret(settings.mutate(fn, create=False))
    exp = str(int(time.time()) + SESSION_TTL)
    sig = hmac.new(secret.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_session(token: Optional[str]) -> bool:
    secret = _secret()
    if not secret or not token or "." not in token:
        return False
    try:
        exp, sig = token.rsplit(".", 1)
        good = hmac.new(secret.encode(), exp.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig.encode("utf-8"), good.encode("utf-8")):
            return False
        return int(exp) > time.time()
    except Exception:
        return False


# -- local vs remote (fail closed: ONLY loopback is "local") -----------------
def is_local(host: Optional[str]) -> bool:
    """The host PC itself (loopback). NOT the LAN — a LAN is not a trust boundary for a
    transmitter. Behind a reverse proxy/tunnel the peer is also loopback, so loopback-exempt
    is only sound with DIRECT TLS (the v1 Let's Encrypt setup), where the peer IP is real."""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


is_loopback = is_local


def behind_proxy() -> bool:
    """Set when fronted by a reverse proxy (e.g. Tailscale Serve), where every client arrives
    from loopback — so loopback can NOT be trusted as 'this is the host operator'."""
    return settings.behind_proxy()


def set_behind_proxy(v: bool, source: Optional[str] = None) -> None:
    settings.set_behind_proxy(v, source)


def local_exempt(host: Optional[str]) -> bool:
    """Skip the password only for the host operator on loopback — and only with DIRECT TLS.
    Behind a proxy, loopback is everyone, so nothing is exempt."""
    return is_local(host) and not behind_proxy()


# -- request helpers (ASGI scope) ---------------------------------------------------------------
def header(scope, name: str) -> Optional[str]:
    """First value of a request header (case-insensitive), from an ASGI scope."""
    n = name.lower().encode("latin-1")
    for k, v in scope.get("headers") or ():
        if k.lower() == n:
            try:
                return v.decode("latin-1")
            except Exception:
                return None
    return None


def peer_ip(scope) -> str:
    """The TCP peer (uvicorn runs with proxy_headers=False, so this is never rewritten)."""
    c = scope.get("client")
    return c[0] if c else ""


def _first(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.split(",")[0].strip()
    return v or None


def split_host(value: Optional[str]) -> tuple:
    """'[::1]:8790' -> ('::1', 8790); 'Radio-PC' -> ('radio-pc', None). Malformed -> ('', None)."""
    v = (value or "").strip()
    if not v:
        return "", None
    if v.startswith("["):
        end = v.find("]")
        if end < 0:
            return "", None
        h, rest = v[1:end], v[end + 1:]
        if rest and not rest.startswith(":"):
            return "", None
        p = rest[1:]
    else:
        if v.count(":") > 1:                      # bare IPv6 without brackets is not a Host value
            return "", None
        h, _, p = v.partition(":")
    port = None
    if p:
        if not p.isdigit() or not 0 < int(p) < 65536:
            return "", None
        port = int(p)
    return h.lower(), port


def effective_host(scope) -> tuple:
    """The (host, port) the browser addressed: the Host header, or X-Forwarded-Host/-Port when the
    TCP peer is loopback (a local reverse proxy reporting the external host; nginx $host drops the
    port). A browser page cannot set these cross-origin (preflight never granted; WS can't)."""
    hh, hp = split_host(header(scope, "host"))
    if is_loopback(peer_ip(scope)):
        xh = _first(header(scope, "x-forwarded-host"))
        if xh:
            hh, hp = split_host(xh)
            xp = (header(scope, "x-forwarded-port") or "").strip()
            if xp.isdigit() and 0 < int(xp) < 65536:
                hp = int(xp)
    return hh, hp


# -- Origin / Host allowlist (CSWSH + DNS-rebinding) -------------------------
def _hostname(netloc: str) -> str:
    h = (netloc or "").strip()
    if h.startswith("["):                          # [ipv6]:port
        return h[1:h.find("]")].lower()
    return h.split(":")[0].lower()


def _is_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def host_ok(host_header: Optional[str]) -> bool:
    """Reject DNS-rebinding: Host must be localhost, a literal IP, or a configured name.
    Permissive until an allowlist is configured, so it never breaks existing hostname access on a
    LAN; once a remote host is configured, it's enforced. (A3 tightens the empty-list case, F21.)"""
    h = _hostname(host_header or "")
    if not h:
        return False
    if h == "localhost" or _is_ip(h):
        return True
    names = settings.allowed_names()               # hostnames of every entry (names and origins)
    if not names:
        return True
    return h in names


def origin_ok(origin: Optional[str], scope) -> bool:
    """Exact Origin rule (I15): scheme http(s), and host AND port equal to the effective request
    host (§2.8), or the full origin is in the allowlist. A bare allowlisted name relaxes host_ok
    only, never this check. Missing, "null", file:, data: and anything malformed are refused."""
    if not origin:
        return False
    try:
        u = urlparse(origin.strip())
        oh = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not oh:
        return False
    if u.path not in ("", "/") or u.query or u.fragment or u.params or u.username or u.password:
        return False
    op = port or (443 if u.scheme == "https" else 80)
    hh, hp = effective_host(scope)
    if hh and oh == hh and op == (hp or (443 if u.scheme == "https" else 80)):
        return True
    return f"{u.scheme}://{oh}:{op}" in settings.allowed_origins()


def origin_denied_text(origin: Optional[str], scope) -> str:
    hh, hp = effective_host(scope)
    where = f"{hh}:{hp}" if hp else (hh or "?")
    if not origin:
        return f"Cross-site request blocked: no Origin header (host {where})."
    return (f"Origin {origin} does not match {where} — if a reverse proxy rewrites Host, add {origin} "
            "under Admin → Access (from the host PC).")


def add_allowed_host(host: str) -> None:
    settings.add_allowed_host(host)
