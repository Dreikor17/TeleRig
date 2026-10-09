"""App-layer access control (spec §2.1, §2.7-§2.8, §2.18) — the ONLY thing protecting the
transmitter once TeleRig is reachable off the host PC.

- The lazy config folder (§2.1, I12): nothing is read, created or copied at import time;
  `config_dir(migrate=True)` (called by accounts.ensure_initialized) does the one-time copy of an
  old ~/.radio-webop folder to ~/.telerig.
- One trust rule for forwarding headers (§2.8): uvicorn runs with proxy_headers=False, so
  scope["client"] is always the TCP peer. Forwarding headers are interpreted here, and only when
  that peer is loopback.
- `identify(scope)` -> Principal | None from the per-port session cookie `tr_session_<P>`. There is
  NO loopback branch: identity and privilege never come from network location (I1).
  `direct_local()` is a heuristic used only to RESTRICT (default password + setup code, F17,
  Serve ON, the login hint, the rate-limit source key), never to authenticate.
- `host_ok` (F21: with an empty allowlist only localhost, IPs and LAN-style names) and the exact
  `origin_ok` (I15).
- The sudo window (§2.18): a fresh re-auth, in memory.

Standard library only: accounts.py (and so the `--reset-admin` CLI path) imports this module
before uvicorn. Config lives in ~/.telerig/ (override with TELERIG_CONFIG, the path of config.json;
the old RADIO_WEBOP_CONFIG is still honoured).
"""
from __future__ import annotations

import ipaddress
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from . import settings

ROLES = ("viewer", "operator", "admin")
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}


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
    with _sudo_mu:
        _sudo.clear()


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


def _has_header(scope, name: str) -> bool:
    n = name.lower().encode("latin-1")
    return any(k.lower() == n for k, _ in (scope.get("headers") or ()))


def peer_ip(scope) -> str:
    """The TCP peer (uvicorn runs with proxy_headers=False, so this is never rewritten)."""
    c = scope.get("client")
    return c[0] if c else ""


def bound_port(scope) -> int:
    """The port this server is bound to (scope["server"][1]): stable behind Tailscale Serve,
    where the Host port is 443. Names the session cookie."""
    s = scope.get("server")
    try:
        return int(s[1]) if s and s[1] is not None else 0
    except (TypeError, ValueError, IndexError):
        return 0


def _first(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.split(",")[0].strip()
    return v or None


def _is_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def is_loopback(host: Optional[str]) -> bool:
    """Loopback only (fail closed): the LAN is not a trust boundary for a transmitter. Unwraps
    IPv4-mapped IPv6 (::ffff:127.0.0.1)."""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


# Headers a reverse proxy / tunnel adds. Their presence (from a loopback peer) means "proxied".
FWD = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-port", "x-forwarded-proto",
       "forwarded", "via", "x-real-ip", "tailscale-user-login", "tailscale-user-name",
       "cf-connecting-ip", "true-client-ip")
LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def has_forwarding_header(scope) -> bool:
    return any(_has_header(scope, h) for h in FWD)


def client_ip(scope) -> str:
    """The client address for rate limits and logs: the right-most X-Forwarded-For entry when the
    TCP peer is loopback and XFF is present (the entry the local proxy added), else the peer.
    A malformed entry is kept as an opaque key (never treated as loopback)."""
    peer = peer_ip(scope)
    if is_loopback(peer):
        xff = header(scope, "x-forwarded-for")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                last = parts[-1].strip("[]")
                try:
                    return str(ipaddress.ip_address(last))
                except ValueError:
                    return "xff:" + parts[-1][:64]
    return peer


def scheme(scope) -> str:
    """http | https as the browser sees it: the first X-Forwarded-Proto from a loopback peer,
    else the connection's own scheme (ws/wss map to http/https)."""
    if is_loopback(peer_ip(scope)):
        p = (_first(header(scope, "x-forwarded-proto")) or "").lower()
        if p in ("http", "https"):
            return p
    s = (scope.get("scheme") or "http").lower()
    return "https" if s in ("https", "wss") else "http"


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


def direct_local(scope) -> bool:
    """The request looks like it comes from a browser on the host PC itself: loopback TCP peer,
    not behind_proxy, Host is localhost/127.0.0.1/::1, and no forwarding header at all.

    A HEURISTIC, NOT AN AUTHENTICATOR (R4, AR-3): any loopback TCP relay that adds no forwarding
    header makes it true for remote clients. It only ever restricts or adds a factor."""
    if not is_loopback(peer_ip(scope)):
        return False
    if settings.behind_proxy():
        return False
    if has_forwarding_header(scope):
        return False
    h, _ = split_host(header(scope, "host"))
    return h in LOCAL_HOSTNAMES


def local_reason(scope) -> Optional[str]:
    """Why a loopback request is NOT direct-local (the login page's "Trouble signing in on this
    PC?" help): behind_proxy | forwarded_header | host_not_local | None. Only meaningful when the
    TCP peer is loopback; None otherwise."""
    if not is_loopback(peer_ip(scope)):
        return None
    if settings.behind_proxy():
        return "behind_proxy"
    if has_forwarding_header(scope):
        return "forwarded_header"
    h, _ = split_host(header(scope, "host"))
    if h not in LOCAL_HOSTNAMES:
        return "host_not_local"
    return None


def effective_host(scope) -> tuple:
    """The (host, port) the browser addressed: the Host header, or X-Forwarded-Host when the TCP peer
    is loopback (a local reverse proxy reporting the external host). The port is the one in that
    host value, else a trusted (loopback-peer) X-Forwarded-Port (nginx $host drops the port), else
    None (the scheme's default; see effective_origin). A browser page cannot set these headers
    cross-origin (preflight never granted; a WebSocket can't)."""
    hh, hp = split_host(header(scope, "host"))
    if is_loopback(peer_ip(scope)):
        xh = _first(header(scope, "x-forwarded-host"))
        if xh:
            hh, hp = split_host(xh)
        if hh and hp is None:
            xp = _first(header(scope, "x-forwarded-port")) or ""
            if xp.isdigit() and 0 < int(xp) < 65536:
                hp = int(xp)
    return hh, hp


def default_port(scheme_: str) -> int:
    return 443 if scheme_ == "https" else 80


def effective_origin(scope) -> tuple:
    """(scheme, host, port) of the address the browser used, as the Origin check compares it (I15):
    the effective scheme (scheme(): X-Forwarded-Proto only from a loopback peer, else the
    connection's own), the effective host (effective_host()) and the effective port (the host's own
    port, else a trusted X-Forwarded-Port, else the effective scheme's default). host is "" when the
    Host header is missing or malformed."""
    s = scheme(scope)
    hh, hp = effective_host(scope)
    return s, hh, (hp if hp is not None else default_port(s))


# -- Host allowlist (DNS rebinding) + exact Origin (CSWSH) ------------------------------------
LOCALISH = (".local", ".lan", ".home.arpa", ".internal", ".ts.net")


def host_ok(host_header: Optional[str]) -> bool:
    """Reject DNS rebinding. localhost and IP literals always pass. Once an allowlist is
    configured it is enforced (v1 rule). With an EMPTY allowlist only LAN-style names pass
    (F21): single-label names and *.local, *.lan, *.home.arpa, *.internal, *.ts.net — DNS
    rebinding needs a public name, so a public FQDN must be allowlisted."""
    h, _ = split_host(host_header)
    if not h:
        return False
    if h == "localhost" or _is_ip(h):
        return True
    names = settings.allowed_names()               # hostnames of every entry (names and origins)
    if names:
        return h in names
    return "." not in h or h.endswith(LOCALISH)


def host_denied_text(host_header: Optional[str], scope) -> str:
    h, _ = split_host(host_header)
    name = h or (host_header or "(none)")
    return (f"Bad Host header: {name} is not an allowed name for this server. On the host PC open "
            f"http://localhost:{bound_port(scope)} → Admin → Access and add it.")


def parse_origin(origin: Optional[str]) -> Optional[tuple]:
    """'https://Name:443' -> ('https', 'name', 443); None for a missing, "null", non-http(s), or
    malformed Origin, or one with a path / query / fragment / user info."""
    if not origin:
        return None
    try:
        u = urlparse(origin.strip())
        oh = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not oh:
        return None
    if u.path not in ("", "/") or u.query or u.fragment or u.params or u.username or u.password:
        return None
    return u.scheme, oh, (port or default_port(u.scheme))


def same_origin(o: tuple, scope) -> bool:
    """The parsed Origin `o` is exactly the address the browser used: scheme, host and port all equal
    to effective_origin(scope)."""
    es, eh, ep = effective_origin(scope)
    return bool(eh) and o == (es, eh, ep)


def origin_ok(origin: Optional[str], scope) -> bool:
    """Exact Origin rule (I15): scheme, host AND port all equal to the effective request origin
    (effective_origin, §2.8), or the full origin is in the allowlist (compared exactly). The scheme
    counts: http://name is not https://name, and https://127.0.0.1:P is not a plain-http server on
    P. A bare allowlisted name relaxes host_ok only, never this check. Missing, "null", file:,
    data: and anything malformed are refused."""
    o = parse_origin(origin)
    if o is None:
        return False
    if same_origin(o, scope):
        return True
    return f"{o[0]}://{o[1]}:{o[2]}" in settings.allowed_origins()


def origin_denied_text(origin: Optional[str], scope) -> str:
    es, eh, ep = effective_origin(scope)
    shown = f"[{eh}]" if ":" in eh else eh
    where = f"{es}://{shown}:{ep}" if eh else "?"
    if not origin:
        return f"Cross-site request blocked: no Origin header (this address is {where})."
    return (f"Origin {origin} does not match {where} — if a reverse proxy rewrites Host, add {origin} "
            "under Admin → Access (from the host PC).")


# -- cookies ------------------------------------------------------------------------------------
COOKIE_PREFIX = "tr_session_"
LEGACY_COOKIES = ("tr_session", "rw_session")


def cookie_name(scope) -> str:
    return f"{COOKIE_PREFIX}{bound_port(scope)}"


def cookie(scope, name: str) -> Optional[str]:
    """The first value of cookie `name` from the Cookie header(s). Never raises."""
    try:
        for k, v in scope.get("headers") or ():
            if k.lower() != b"cookie":
                continue
            for part in v.decode("latin-1").split(";"):
                key, sep, val = part.strip().partition("=")
                if sep and key.strip() == name:
                    val = val.strip()
                    if len(val) >= 2 and val[0] == val[-1] == '"':
                        val = val[1:-1]
                    return val
    except Exception:
        return None
    return None


# -- identity -----------------------------------------------------------------------------------
_PERMS = {
    "viewer": ("view", "listen", "menu_read"),
    "operator": ("view", "listen", "menu_read", "operate", "tx", "connect", "menu"),
    "admin": ("view", "listen", "menu_read", "operate", "tx", "connect", "menu",
              "admin", "menu_critical", "switch_radio", "audio_pin", "remote"),
}


def perms_for(role: Optional[str]) -> list:
    return list(_PERMS.get(role or "", ()))


def rank(role: Optional[str]) -> int:
    """viewer 0 < operator 1 < admin 2; anything else -1 (fail closed)."""
    return ROLE_RANK.get(role or "", -1)


@dataclass(frozen=True)
class Principal:
    user: str                 # canonical (upper-case) user name
    role: str                 # EFFECTIVE role (F17: a migrated ADMIN off the host PC is an operator)
    stored_role: str          # the role in auth.json
    gen: int
    restricted: bool          # must_change: only the password-change routes are allowed
    default_pw: bool
    migrated_remote: bool     # F17: migrated_v1 ADMIN on a session that is not direct-local
    token_fp: str
    exp: int
    token: str = field(default="", repr=False, compare=False)


def effective_role(user, is_direct_local: bool) -> str:
    """F17: while migrated_v1 is set, ADMIN has admin powers only from a direct-local session."""
    if getattr(user, "migrated_v1", False) and user.role == "admin" and not is_direct_local:
        return "operator"
    return user.role


def principal_for(user, token: str, exp: int, fp: str, is_direct_local: bool) -> Principal:
    role = effective_role(user, is_direct_local)
    return Principal(user.name, role, user.role, user.gen, bool(user.must_change), bool(user.default_pw),
                     role != user.role, fp, int(exp), token)


def identify(scope) -> Optional[Principal]:
    """The signed-in user for this request, or None. No loopback branch (I1). Raises StoreCorrupt
    when auth.json is corrupt (the caller enters locked mode)."""
    from . import accounts
    tok = cookie(scope, cookie_name(scope))
    if not tok:
        return None
    t = accounts.parse_token(tok)
    if t is None:
        return None
    u = accounts.get(t.user)
    if u is None or u.gen != t.gen:
        return None
    fp = accounts.token_fp(tok)
    if accounts.is_revoked(fp):
        return None
    return principal_for(u, tok, t.exp, fp, direct_local(scope))


# -- sudo window (§2.18), in memory -------------------------------------------------------------
SUDO_TTL = 600.0
_sudo: dict = {}                     # token_fp -> (user, gen, until monotonic)
_sudo_mu = threading.Lock()


def _env_on(name: str) -> bool:
    v = os.environ.get(name)
    return v is not None and v.strip().lower() not in ("", "0", "false", "no", "off")


def sudo_ttl() -> float:
    """600 s; TELERIG_TEST_SUDO_TTL overrides it ONLY when TELERIG_SIM_ONLY=1 (tests)."""
    v = os.environ.get("TELERIG_TEST_SUDO_TTL")
    if v and _env_on("TELERIG_SIM_ONLY"):
        try:
            t = float(v)
            if 0 < t <= SUDO_TTL:
                return t
        except ValueError:
            pass
    return SUDO_TTL


def sudo_open(p: Principal) -> None:
    """Open (or refresh) the window for this session. Never for a migrated-remote session,
    which has no admin powers (F17)."""
    if p is None or p.migrated_remote:
        return
    now = time.monotonic()
    with _sudo_mu:
        for k in [k for k, v in _sudo.items() if v[2] <= now]:
            del _sudo[k]
        _sudo[p.token_fp] = (p.user, p.gen, now + sudo_ttl())


def sudo_active(p: Optional[Principal]) -> bool:
    if p is None or p.migrated_remote:
        return False
    with _sudo_mu:
        e = _sudo.get(p.token_fp)
    return bool(e) and e[0] == p.user and e[1] == p.gen and e[2] > time.monotonic()


def sudo_left(p: Optional[Principal]) -> float:
    if not sudo_active(p):
        return 0.0
    with _sudo_mu:
        e = _sudo.get(p.token_fp)
    return max(0.0, e[2] - time.monotonic()) if e else 0.0


def sudo_drop(fp: str) -> None:
    with _sudo_mu:
        _sudo.pop(fp, None)


def sudo_drop_user(user: str) -> None:
    with _sudo_mu:
        for k in [k for k, v in _sudo.items() if v[0] == user]:
            del _sudo[k]
