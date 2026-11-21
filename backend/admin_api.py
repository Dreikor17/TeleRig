"""Admin API (spec §2.17-§2.18, E10): every route needs the admin role (the EFFECTIVE role: a migrated
ADMIN off the host PC is an operator, F17, and cannot reach any of them). Mutations that change who
can do what also need a fresh re-auth window ("sudo", §2.18): opened by a password login, by
/api/me/password and by POST /api/admin/reauth; 10 minutes; expired -> 403 reauth_required.

Users are addressed by {uid} = the lowercase hex of the canonical name (callsigns may contain "/",
which cannot ride in a path segment). Errors are {ok:false, error, message[, field]}.

  POST   /api/admin/reauth                        {password} -> {ok, expires_in}
  GET    /api/admin/users                         list (+ online sockets, on-air, lockout)
  POST   /api/admin/users                 sudo    {user, role="viewer", password?, must_change=true}
  PATCH  /api/admin/users/{uid}           sudo    {role}
  POST   /api/admin/users/{uid}/reset_password  sudo  {password?, must_change=true}
  POST   /api/admin/users/{uid}/signout   sudo    -> {ok, kicked}
  POST   /api/admin/users/{uid}/unlock            rate-limit keys K2 + K3
  DELETE /api/admin/users/{uid}           sudo    user + layout and memories files
  DELETE /api/admin/users/{uid}/layout            reset that user's layout
  GET    /api/admin/sessions                      live WebSocket connections
  GET    /api/admin/lockouts · POST /api/admin/lockouts/unlock_ip {ip}
  GET    /api/admin/allowed_hosts · PUT (sudo)    names (Host check) and full origins (I15)
  GET    /api/admin/audio · PUT (sudo)            host sound-card pins per radio (F22 defaults)
  PUT    /api/admin/radio_transport       sudo    the F20 transport pin (never LAN credentials)
  GET    /api/admin/bridge · PUT (sudo)           the WSJT-X bridge: on/off, port, who may connect,
                                                  transmit on/off, the silence limit (+ live status)
  GET    /api/admin/remote_status                 Tailscale + behind_proxy (+ stale flag)
  POST   /api/admin/tailscale_serve       sudo + direct-local (F9)
  POST   /api/admin/tailscale_serve_off           (closing exposure: no sudo)
  POST   /api/admin/clear_behind_proxy    sudo    a stale v1 flag (AR-34)
  GET    /api/admin/audit?limit&kind&format       json (newest first) or a jsonl download

behind_proxy is written ONLY after a confirmed Tailscale result (E8, AR-22): Serve ON after exit 0
and `serve_running`; Serve OFF after exit 0 and a successful `serve status` that shows Serve off.
A flag set by hand (behind_proxy_source other than "tailscale"/None, e.g. the documented "manual"
for a non-Tailscale proxy, settings.proxy_source_managed) is never stale and never cleared here:
no behind_proxy_stale notice, no stale_flag, Turn off leaves it, clear_behind_proxy -> 409 set_by_hand.
Every Tailscale CLI call, device enumeration, scrypt and store write runs in the threadpool (AR-16).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from . import (__version__, accounts, audit, auth, hostaudio, layout_store, memory_store, policy, profiles, settings,
               tailscale)
from . import bridge as bridge_mod
from .account_api import (NO_STORE, _layout_write, _record_failure, _sem, err, hash_async, rate_limited,
                          read_json, source, verify_async)
from .audio_router import CODEC_RE, codec_default, find_device
from .policy import requires
from .ratelimit import limiter
from .store import StoreBusy, StoreCorrupt, StoreError, console
from .transport import available_ports


# -- what the server binds (it owns the Hub, the TX arbiter and the audio router) --------------
class _Ctx:
    hub: Any = None
    arb: Any = None
    audio: Any = None
    bridge: Any = None
    radio: Callable[[], Any] = staticmethod(lambda: None)
    transport_kind: Callable[[], str] = staticmethod(lambda: "none")
    updater: Any = None                     # backend/updater.py (Administration > Update)
    restart: Callable[[], Any] = staticmethod(lambda: "skipped")
    boot: str = ""


_ctx = _Ctx()


def bind(*, hub=None, arb=None, audio=None, radio: Optional[Callable] = None,
         transport_kind: Optional[Callable] = None, bridge=None, updater=None,
         restart: Optional[Callable] = None, boot: Optional[str] = None) -> None:
    if bridge is not None:
        _ctx.bridge = bridge
    if updater is not None:
        _ctx.updater = updater
    if restart is not None:
        _ctx.restart = restart
    if boot is not None:
        _ctx.boot = boot
    if hub is not None:
        _ctx.hub = hub
    if arb is not None:
        _ctx.arb = arb
    if audio is not None:
        _ctx.audio = audio
    if radio is not None:
        _ctx.radio = radio
    if transport_kind is not None:
        _ctx.transport_kind = transport_kind


def online_count(user: str) -> int:
    h = _ctx.hub
    return h.count_user(user) if h is not None else 0


def on_air(user: str) -> bool:
    a = _ctx.arb
    return bool(a is not None and a.owner is not None and a.owner != policy.EXTERNAL and a.owner_user == user)


def _kick_user(user: str, reason: str = "session_revoked") -> int:
    h = _ctx.hub
    return h.kick_user(user, 4401, reason) if h is not None else 0


def _ok(**extra) -> JSONResponse:
    return JSONResponse({"ok": True, **extra}, headers=NO_STORE)


def _iso(t: Optional[float]) -> Optional[str]:
    if not t:
        return None
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _store_failure(e: Exception) -> JSONResponse:
    """StoreBusy -> 503, any other store write error -> 500. A CORRUPT store is re-raised by the
    callers, so the Gate enters locked mode (I6)."""
    if isinstance(e, StoreBusy):
        return err(503, "store_busy", "The store is busy (another TeleRig process holds it); try again.")
    return err(500, "store_error", "The store could not be written; nothing changed.")


async def _write(fn, *a, **kw):
    """A store write in the threadpool. Returns (result, None) or (None, error response)."""
    try:
        return await run_in_threadpool(lambda: fn(*a, **kw)), None
    except StoreCorrupt:
        raise
    except (StoreBusy, StoreError, OSError) as e:
        console(f"TeleRig: admin store write failed: {e!r}")
        return None, _store_failure(e)


# -- users ----------------------------------------------------------------------------------------
_UID_RE = re.compile(r"(?:[0-9a-f]{2}){2,128}")


def name_of(uid: Any) -> Optional[str]:
    """The canonical user name a {uid} path segment names, or None (-> 404)."""
    if not isinstance(uid, str) or not _UID_RE.fullmatch(uid):
        return None
    try:
        name = bytes.fromhex(uid).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return name if accounts.canonical(name) == name else None


def _not_found() -> JSONResponse:
    return err(404, "not_found", "No such user.")


def _target(request):
    """(name, user) for the {uid} of this request, or (None, None) when there is no such user."""
    name = name_of(request.path_params.get("uid"))
    if name is None:
        return None, None
    return name, accounts.get(name)


def _account_error(e: accounts.AccountError) -> JSONResponse:
    status = {"not_found": 404, "exists": 409, "last_admin": 409}.get(e.code, 400)
    extra = {"field": e.field} if e.field else {}
    return err(status, e.code, e.message, **extra)


@requires("admin")
async def api_users(request):
    p = request.state.principal
    out = []
    for name, u in sorted(accounts.users().items()):
        out.append({"uid": accounts.uid(name), "user": name, "role": u.role,
                    "must_change": u.must_change, "default_pw": u.default_pw,
                    "migrated_v1": u.migrated_v1, "created": u.created, "created_by": u.created_by,
                    "pw_changed": u.pw_changed, "last_login": u.last_login,
                    "locked_until": limiter.locked_until(name), "online": online_count(name),
                    "on_air": on_air(name)})
    return JSONResponse({"me": p.user, "users": out}, headers=NO_STORE)


@requires("admin")
async def api_reauth(request):
    """Open the sudo window again (§2.18). A wrong password counts like a failed login (§2.15)."""
    p = request.state.principal
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    pw = body.get("password")
    pw = pw if isinstance(pw, str) else ""
    src, dl = source(request.scope)
    wait = limiter.check(p.user, src, direct_local=dl)
    if wait > 0:
        audit.log("rate_limited", key=src, reason="reauth", user=p.user, src=src)
        return rate_limited(wait)
    u = accounts.get(p.user)
    ok, _ = await verify_async(u.hash if u else None, pw)
    if not (u and ok):
        _record_failure(p.user, src, auth.client_ip(request.scope))
        audit.log("login_fail", key=src, src=src, users=[p.user], reason="reauth")
        return err(401, "invalid_credentials", "That password is not right.", field="password")
    limiter.success(p.user, src)
    auth.sudo_open(p)
    return _ok(expires_in=int(round(auth.sudo_ttl())))


def _bool_field(body: dict, key: str, default: bool):
    v = body.get(key, default)
    return v if type(v) is bool else None


@requires("admin", sudo=True)
async def api_user_create(request):
    p = request.state.principal
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    name = accounts.canonical(body.get("user"))
    if name is None:
        return err(400, "invalid", "Use 2–32 letters, digits, / _ -", field="user")
    role = body.get("role", "viewer")
    if not isinstance(role, str) or role not in accounts.ROLES:
        return err(400, "invalid", "The role must be viewer, operator or admin.", field="role")
    mc = _bool_field(body, "must_change", True)
    if mc is None:
        return err(400, "invalid", "must_change must be true or false.", field="must_change")
    pw = body.get("password")
    if pw is not None:
        prob = accounts.password_problem(pw, user=name)
        if prob:
            return err(400, "invalid", prob, field="password")
    if accounts.get(name) is not None:
        return err(409, "exists", "That callsign already exists.", field="user")
    try:                                             # a re-created callsign starts with no layout
        await run_in_threadpool(layout_store.delete_file, name)
    except Exception as e:
        console(f"TeleRig: could not remove an old layout of {name}: {e!r}")
    try:                                             # ... and with no memories
        await run_in_threadpool(memory_store.delete_file, name)
    except Exception as e:
        console(f"TeleRig: could not remove the old memories of {name}: {e!r}")
    try:
        async with _sem():                           # scrypt: at most 2 at a time (R9)
            res, fail = await _write(accounts.create_user, name, role, pw, must_change=mc, created_by=p.user)
    except accounts.AccountError as e:
        return _account_error(e)
    if fail is not None:
        return fail
    cname, temp = res
    audit.log("user_created", user=cname, role=role, by=p.user, must_change=mc)
    out = {"uid": accounts.uid(cname), "user": cname}
    if temp:
        out["temp_password"] = temp                  # shown once; never logged
    return _ok(**out)


@requires("admin", sudo=True)
async def api_user_patch(request):
    p = request.state.principal
    name, u = _target(request)
    if u is None:
        return _not_found()
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    role = body.get("role")
    if not isinstance(role, str) or role not in accounts.ROLES:
        return err(400, "invalid", "The role must be viewer, operator or admin.", field="role")
    if role == u.role:
        return _ok(changed=False)
    if name == p.user:
        return err(409, "self_demote", "You can't change your own role.")
    try:
        old, fail = await _write(accounts.set_role, name, role)
    except accounts.AccountError as e:
        return _account_error(e)
    if fail is not None:
        return fail
    if old is None:
        return _ok(changed=False)
    _kick_user(name, "role_changed")                 # the reload hook does it too (gen changed)
    audit.log("role_changed", user=name, by=p.user, **{"from": old, "to": role})
    return _ok(changed=True)


@requires("admin", sudo=True)
async def api_user_reset_password(request):
    p = request.state.principal
    name, u = _target(request)
    if u is None:
        return _not_found()
    if name == p.user:
        return err(409, "self", "Use Change password for your own account.")
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    mc = _bool_field(body, "must_change", True)
    if mc is None:
        return err(400, "invalid", "must_change must be true or false.", field="must_change")
    pw = body.get("password")
    temp = None
    if pw is None:
        temp = pw = accounts.temp_password()
    else:
        prob = accounts.password_problem(pw, user=name)
        if prob:
            return err(400, "invalid", prob, field="password")
    h = await hash_async(pw)
    try:
        _, fail = await _write(accounts.admin_set_password, name, h, must_change=mc)
    except accounts.AccountError as e:
        return _account_error(e)
    if fail is not None:
        return fail
    _kick_user(name)
    audit.log("pw_reset", user=name, by=p.user, must_change=mc, generated=temp is not None)
    return _ok(**({"temp_password": temp} if temp else {}))


@requires("admin", sudo=True)
async def api_user_signout(request):
    p = request.state.principal
    name, u = _target(request)
    if u is None:
        return _not_found()
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    n = online_count(name)
    try:
        _, fail = await _write(accounts.bump_gen, name)
    except accounts.AccountError as e:
        return _account_error(e)
    if fail is not None:
        return fail
    _kick_user(name)
    auth.sudo_drop_user(name)
    audit.log("signout_all", user=name, by=p.user, kicked=n)
    return _ok(kicked=n)


@requires("admin")
async def api_user_unlock(request):
    p = request.state.principal
    name = name_of(request.path_params.get("uid"))
    if name is None:
        return _not_found()
    locked = any(x.get("key") == name for x in limiter.lockouts()["users"])
    if accounts.get(name) is None and not locked:
        return _not_found()
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    limiter.clear_user(name)
    audit.log("unlock", user=name, by=p.user)
    return _ok()


@requires("admin", sudo=True)
async def api_user_delete(request):
    p = request.state.principal
    name, u = _target(request)
    if u is None:
        return _not_found()
    if name == p.user:
        return err(409, "self_delete", "You can't delete your own account.")
    try:
        role, fail = await _write(accounts.delete_user, name, keep_admin=True)
    except accounts.AccountError as e:
        return _account_error(e)
    if fail is not None:
        return fail
    _kick_user(name)
    try:
        await run_in_threadpool(layout_store.delete_file, name)
    except Exception as e:                           # the account is gone; a stale layout is cosmetic
        console(f"TeleRig: could not delete the layout of {name}: {e!r}")
    try:
        await run_in_threadpool(memory_store.delete_file, name)
    except Exception as e:                           # nobody can read it; creating the callsign again removes it
        console(f"TeleRig: could not delete the memories of {name}: {e!r}")
    audit.log("user_deleted", user=name, role=role, by=p.user)
    return _ok()


@requires("admin")
async def api_user_layout_delete(request):
    p = request.state.principal
    name, u = _target(request)
    if u is None:
        return _not_found()
    rev, fail = await _layout_write(layout_store.reset, name)
    if fail is not None:
        return fail
    audit.log("layout_reset", user=name, by=p.user)
    return _ok(rev=rev)


# -- sessions + lockouts ----------------------------------------------------------------------
@requires("admin")
async def api_sessions(request):
    hub, arb = _ctx.hub, _ctx.arb
    rows = []
    for c in (list(hub.conns.values()) if hub is not None else []):
        if c.closing:
            continue
        rows.append({"conn": c.id, "user": c.user, "role": c.role, "ip": c.ip, "since": _iso(c.opened),
                     "tx_owner": bool(arb is not None and arb.owner is not None and arb.owner == c.id),
                     "rx_audio": bool(c.rx_sub), "mic": bool(c.tx_audio)})
    return JSONResponse({"sessions": rows, "tx": arb.snapshot() if arb is not None else {"owner": None}},
                        headers=NO_STORE)


@requires("admin")
async def api_lockouts(request):
    return JSONResponse(limiter.lockouts(), headers=NO_STORE)


@requires("admin")
async def api_unlock_ip(request):
    p = request.state.principal
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    ip = body.get("ip")
    if not isinstance(ip, str) or not ip.strip() or len(ip) > 100:
        return err(400, "invalid", "Give the source to unlock as shown in the lockout list.", field="ip")
    limiter.clear_ip(ip.strip())
    audit.log("unlock", ip=ip.strip(), by=p.user)
    return _ok()


# -- allowed hosts (G-18, F21, I15) -----------------------------------------------------------
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
_ORIGIN_RE = re.compile(r"^(https?)://([^:/?#\s]+)(?::(\d{1,5}))?$")
ALLOWED_MAX = 32


def _is_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def _bad_host(h: str) -> Optional[str]:
    """Why `h` (lowercased) can't be allowlisted, or None. IP literals, localhost and *.localhost
    never (I15). A name whose last label is all digits is an IPv4 literal to a browser."""
    if h == "localhost" or h.endswith(".localhost"):
        return "localhost can't be listed (it always works on the host PC)"
    if _is_ip(h) or h.rsplit(".", 1)[-1].isdigit():
        return "IP addresses can't be listed (they always pass the Host check)"
    if len(h) > 253 or not _NAME_RE.match(h):
        return "not a valid host name"
    return None


def parse_entry(raw: Any) -> tuple:
    """("name", "radio-pc") or ("origin", "https://pc.tailnet.ts.net:443"); ValueError(reason).
    The admin-side rule (stricter than the lenient config.json read): a name has no port."""
    if isinstance(raw, dict):
        raw = raw.get("value")
    if not isinstance(raw, str):
        raise ValueError("not text")
    e = raw.strip().lower()
    if not e or len(e) > 300:
        raise ValueError("empty or too long")
    if "://" in e:
        m = _ORIGIN_RE.fullmatch(e)
        if not m:
            raise ValueError("an origin is scheme://name[:port] (http or https, nothing after the port)")
        scheme, host, port = m.group(1), m.group(2), m.group(3)
        why = _bad_host(host)
        if why:
            raise ValueError(why)
        p = int(port) if port else (443 if scheme == "https" else 80)
        if not 1 <= p <= 65535:
            raise ValueError("invalid port")
        return "origin", f"{scheme}://{host}:{p}"
    why = _bad_host(e)
    if why:
        raise ValueError(why)
    return "name", e


def _kind(v: str) -> str:
    return "origin" if "://" in v else "name"


def _entry_rows(values) -> list:
    return [{"value": v, "kind": _kind(v)} for v in values]


def _host_of(v: str) -> str:
    return v.split("://", 1)[1].rsplit(":", 1)[0] if "://" in v else v


def _host_ok_with(h: str, names: set) -> bool:
    """auth.host_ok's rule for a hypothetical allowlist (the lock-out check)."""
    if not h:
        return False
    if h == "localhost" or _is_ip(h):
        return True
    if names:
        return h in names
    return "." not in h or h.endswith(auth.LOCALISH)


def _origin_key(origin: Optional[str]) -> Optional[tuple]:
    """(scheme, host, port) of a request Origin, or None (auth.parse_origin, the Origin check's own)."""
    return auth.parse_origin(origin)


def _browser_origin(scheme: str, host: str, port: Optional[int]) -> Optional[str]:
    if not host:
        return None
    hh = f"[{host}]" if ":" in host else host
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{hh}" + (f":{port}" if port and port != default else "")


@requires("admin")
async def api_allowed_hosts_get(request):
    v = settings.view()
    scope = request.scope
    host, _ = auth.split_host(request.headers.get("host"))
    eh, ep = auth.effective_host(scope)
    return JSONResponse({"entries": _entry_rows(v.allowed_hosts), "current_host": host or None,
                         "current_origin": _browser_origin(auth.scheme(scope), eh, ep),
                         "behind_proxy": v.behind_proxy}, headers=NO_STORE)


def would_lock_out(values: list, host_header: Optional[str], origin: Optional[str], scope) -> Optional[str]:
    """Why saving `values` would lock THIS browser out, or None (G-18):
    - the request's Host would fail the Host check under the new list (for a non-empty list: the
      name is not localhost/IP and not among the entries' hostnames; for an empty list: the F21
      LAN-name rule), or
    - the request's Origin passed only through an allowlisted origin that the new list removes."""
    h, _ = auth.split_host(host_header)
    names = {_host_of(v) for v in values}
    if not _host_ok_with(h, names):
        return (f"Saving this list would lock this browser out: {h or '(no host)'} would no longer be "
                "accepted. Add it to the list first.")
    ok = _origin_key(origin)
    if ok is not None:
        same = auth.same_origin(ok, scope)            # the exact-origin rule (scheme, host, port)
        key = f"{ok[0]}://{ok[1]}:{ok[2]}"
        if not same and key in settings.allowed_origins() and key not in values:
            return (f"Saving this list would lock this browser out: its origin {key} is allowed only "
                    "through that origin entry, which this list removes.")
    return None


@requires("admin", sudo=True)
async def api_allowed_hosts_put(request):
    p = request.state.principal
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    entries = body.get("entries")
    if not isinstance(entries, list) or len(entries) > 4 * ALLOWED_MAX:
        return err(400, "invalid", "entries must be a list of host names and origins.", field="entries")
    values: list = []
    for i, raw in enumerate(entries):
        try:
            _, v = parse_entry(raw)
        except ValueError as e:
            shown = raw.get("value") if isinstance(raw, dict) else raw
            return err(400, "invalid", f"Entry {i + 1} ({str(shown)[:80]!r}): {e}.", field="entries", index=i)
        if v not in values:
            values.append(v)
    if len(values) > ALLOWED_MAX:
        return err(400, "invalid", f"At most {ALLOWED_MAX} entries.", field="entries")
    why = would_lock_out(values, request.headers.get("host"), request.headers.get("origin"), request.scope)
    if why:
        return err(400, "would_lock_out", why)

    def fn(d: dict) -> None:
        d["allowed_hosts"] = list(values)
    _, fail = await _write(settings.mutate, fn, create=True)
    if fail is not None:
        return fail
    audit.log("allowed_hosts_changed", by=p.user, entries=values)
    return _ok(entries=_entry_rows(values))


# -- host audio pins (U1, F22) + the F20 transport pin ----------------------------------------
async def _devices() -> dict:
    au = _ctx.audio
    try:
        devs = await au.refresh_devices() if au is not None else await run_in_threadpool(hostaudio.list_devices)
    except Exception as e:
        console(f"TeleRig: audio device enumeration failed: {e!r}")
        devs = None
    if not isinstance(devs, dict):
        devs = {}
    return {"inputs": list(devs.get("inputs") or []), "outputs": list(devs.get("outputs") or [])}


def _first_codec(lst: list) -> Optional[str]:
    for d in lst:
        if isinstance(d, dict) and CODEC_RE.search(str(d.get("name") or "")):
            return d.get("name")
    return None


@requires("admin")
async def api_audio_get(request):
    au = _ctx.audio
    devs = await _devices()
    rid = au.radio_id() if au is not None else None
    last = settings.last_radio()
    radio = rid or last
    resolved = {}
    if au is not None and radio:
        for d in ("rx", "tx"):
            r = au.resolve(d, devs, radio)
            resolved[d] = {"name": r.get("name"), "source": r.get("source"), "error": r.get("error")}
    return JSONResponse({
        "available": bool(au.available() if au is not None else hostaudio.available()),
        "radio": radio, "connected": rid is not None, "transport_kind": _ctx.transport_kind(),
        "inputs": [{"id": d.get("id"), "name": d.get("name")} for d in devs["inputs"]],
        "outputs": [{"id": d.get("id"), "name": d.get("name")} for d in devs["outputs"]],
        "pins": settings.host_audio(),
        "defaults": {"rx": codec_default(devs, "rx"), "tx": codec_default(devs, "tx")},
        "suggested": {"rx": _first_codec(devs["inputs"]), "tx": _first_codec(devs["outputs"])},
        "resolved": resolved,
        "rx_running": bool(au is not None and au.rx_running), "tx_open": bool(au is not None and au.tx_open),
        "listeners": len(au.rx_subs()) if au is not None else 0,
        "transports": settings.radio_transports(), "last_radio": last}, headers=NO_STORE)


@requires("admin", sudo=True)
async def api_audio_put(request):
    p = request.state.principal
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    rid = body.get("radio")
    if not isinstance(rid, str) or rid not in profiles.PROFILES:
        return err(400, "invalid", "Unknown radio.", field="radio")
    want = {}
    for k in ("rx", "tx"):
        v = body.get(k)
        if v is not None and (not isinstance(v, str) or not v.strip() or len(v) > 200):
            return err(400, "invalid", f"{k} must be a device name or null.", field=k)
        want[k] = v
    if want["rx"] or want["tx"]:
        devs = await _devices()
        for k in ("rx", "tx"):
            if want[k] and find_device(devs, k, want[k]) is None:
                what = "input (RX)" if k == "rx" else "output (TX)"
                return err(400, "invalid", f"No {what} device named {want[k]!r} on the host.", field=k)

    def fn(d: dict) -> None:
        ha = d.get("host_audio")
        if not isinstance(ha, dict):
            ha = {}
        if want["rx"] is None and want["tx"] is None:
            ha.pop(rid, None)
        else:
            ha[rid] = {"rx": want["rx"], "tx": want["tx"]}
        d["host_audio"] = ha
    _, fail = await _write(settings.mutate, fn, create=True)
    if fail is not None:
        return fail
    audit.log("audio_pinned", radio=rid, rx=want["rx"], tx=want["tx"], by=p.user)
    restarted = False
    au = _ctx.audio
    try:
        if au is not None and au.radio_id() == rid and _ctx.transport_kind() == "serial" \
                and (au.rx_running or au.tx_open or au.rx_subs() or au.tx_users()):
            await au.on_radio_change()               # close the old devices (threadpool) ...
            await au.after_connect()                 # ... and reopen with the new pins
            restarted = True
    except Exception as e:
        console(f"TeleRig: host audio restart after a pin change failed: {e!r}")
    return _ok(restarted=restarted)


@requires("admin", sudo=True)
async def api_radio_transport(request):
    """Set or clear the F20 pin: the one transport operators may reconnect the radio on. Validated
    exactly like /api/connect (§2.10 step 1); LAN user/password are never stored (R12, R35)."""
    p = request.state.principal
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    rid = body.get("radio")
    if not isinstance(rid, str) or rid not in profiles.PROFILES:
        return err(400, "invalid", "Unknown radio.", field="radio")
    if "transport" not in body:
        return err(400, "invalid", "Give the transport to pin, or null to clear the pin.", field="transport")
    t = body["transport"]
    canon = None
    if t is not None:
        if not isinstance(t, dict) or type(t.get("transport")) is not str:
            return err(400, "invalid", "transport must be {transport, ...} or null.", field="transport")
        field, canon, _creds = policy.connect_canonical(t, profiles.PROFILES[rid])
        if field:
            return err(400, "invalid", f"Invalid transport: {field}.", field=f"transport.{field}")
        if canon["transport"] == "serial":
            ports = await run_in_threadpool(available_ports)
            if canon["port"] not in {pp.get("device") for pp in ports}:
                return err(400, "invalid", f"No serial port {canon['port']!r} on this PC.", field="transport.port")
        if canon["transport"] == "lan" and getattr(profiles.PROFILES[rid], "protocol", "civ") == "yaesu":
            return err(400, "invalid", "This radio is COM-only (no network CAT).", field="transport.transport")

    def fn(d: dict) -> None:
        rt = d.get("radio_transports")
        if not isinstance(rt, dict):
            rt = {}
        if canon is None:
            rt.pop(rid, None)
        else:
            rt[rid] = dict(canon)                    # canonical fields only: never credentials
        d["radio_transports"] = rt
    _, fail = await _write(settings.mutate, fn, create=True)
    if fail is not None:
        return fail
    audit.log("radio_transport_pinned", user=p.user, radio=rid, transport=dict(canon) if canon else None)
    return _ok(pin=canon)


# -- Tailscale Serve (moved from server.py; F9, E8, AR-22, AR-34) ------------------------------
TS_TTL = 120.0                     # a cached status older than this is refreshed in the background
_ts_mu = threading.Lock()
_ts_cache: dict = {}               # port -> (monotonic time, status)
_ts_busy: set = set()


def _ts_remember(port: int, st: dict) -> None:
    with _ts_mu:
        _ts_cache[port] = (time.monotonic(), dict(st or {}))


def _ts_status_sync(port: int) -> dict:
    st = tailscale.status(port) or {}
    _ts_remember(port, st)
    return st


async def _ts_status(port: int) -> dict:
    return await run_in_threadpool(_ts_status_sync, port)


def _stale(behind: bool, source: Optional[str], st: Optional[dict]) -> bool:
    """behind_proxy is set by TeleRig (source "tailscale", or a v1 flag without one) but Serve is
    confirmed NOT running for this port (or Tailscale is not installed): the condition under which
    clear_behind_proxy may clear it. A flag set by hand (e.g. source "manual" for an nginx / socat
    front, docs/REMOTE-ACCESS.md) is never stale: Tailscale says nothing about that proxy."""
    if not behind or not settings.proxy_source_managed(source) or not isinstance(st, dict):
        return False
    if not st.get("installed"):
        return True
    return bool(st.get("serve_status_ok")) and not st.get("serve_running")


def serve_stale(port: int) -> bool:
    """For the admin notice `behind_proxy_stale` (§3.15), without blocking: the last known status
    for this port; an old or missing one is refreshed in a background thread for the next page.
    A flag set by hand is never stale, so Tailscale is not even asked."""
    source = settings.behind_proxy_source()
    if not settings.proxy_source_managed(source):
        return False
    with _ts_mu:
        ent = _ts_cache.get(port)
        need = (ent is None or time.monotonic() - ent[0] > TS_TTL) and port not in _ts_busy
        if need:
            _ts_busy.add(port)
    if need:
        def run():
            try:
                _ts_status_sync(port)
            except Exception as e:
                console(f"TeleRig: Tailscale status check failed: {e!r}")
            finally:
                with _ts_mu:
                    _ts_busy.discard(port)
        threading.Thread(target=run, daemon=True, name="tr-ts-status").start()
    return _stale(True, source, ent[1]) if ent else False


def _cli_text(out: str, errtxt: str, fallback: str) -> str:
    return ((errtxt or "").strip() or (out or "").strip() or fallback)[:500]


@requires("admin")
async def api_remote_status(request):
    port = auth.bound_port(request.scope)
    st = await _ts_status(port)
    v = settings.view()
    return JSONResponse({"tailscale": st, "behind_proxy": v.behind_proxy,
                         "behind_proxy_source": v.behind_proxy_source,
                         "behind_proxy_by_hand": v.behind_proxy and not settings.proxy_source_managed(v.behind_proxy_source),
                         "can_serve_on": auth.direct_local(request.scope), "port": port,
                         "stale_flag": _stale(v.behind_proxy, v.behind_proxy_source, st)}, headers=NO_STORE)


@requires("admin", sudo=True, local=True)              # F9: admin + fresh sudo + host-local
async def api_tailscale_serve(request):
    p = request.state.principal
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    port = auth.bound_port(request.scope)
    code, out, errtxt = await run_in_threadpool(tailscale.serve_on, port)
    st = await _ts_status(port)
    if code != 0:
        return err(400, "tailscale_failed", _cli_text(out, errtxt, "tailscale serve failed"), tailscale=st)
    if not st.get("serve_running"):
        return err(400, "tailscale_failed", f"Tailscale accepted the command, but Serve is not running for "
                                            f"port {port}. Nothing was changed.", tailscale=st)
    origin = norm = None
    if st.get("magicdns"):
        try:
            norm = settings.normalize_entry(f"https://{st['magicdns']}")[1]
            origin = f"https://{st['magicdns']}".lower()
        except ValueError:
            console(f"TeleRig: Tailscale reported an unusable name {st.get('magicdns')!r}; no origin added")

    def fn(d: dict) -> None:                       # ONLY after a confirmed success (E8)
        d["behind_proxy"] = True
        d["behind_proxy_source"] = "tailscale"
        if origin:
            hosts = d.get("allowed_hosts")
            if not isinstance(hosts, list):
                hosts = []
            have = set()
            for h in hosts:
                try:
                    have.add(settings.normalize_entry(h)[1])
                except ValueError:
                    pass
            if norm not in have:
                hosts.append(origin)               # Serve's exact origin (I15)
            d["allowed_hosts"] = hosts
    _, fail = await _write(settings.mutate, fn, create=True)
    if fail is not None:
        return fail
    audit.log("remote_on", by=p.user, port=port, origin=origin)
    return _ok(tailscale=st)


@requires("admin")                                     # F9: closing exposure needs no sudo
async def api_tailscale_serve_off(request):
    p = request.state.principal
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    port = auth.bound_port(request.scope)
    code, out, errtxt = await run_in_threadpool(tailscale.serve_off)
    st = await _ts_status(port)
    if code != 0:
        return err(400, "tailscale_failed", _cli_text(out, errtxt, "tailscale serve reset failed"),
                   tailscale=st, behind_proxy=settings.behind_proxy())
    if not st.get("serve_status_ok") or st.get("serve_running"):
        return err(400, "tailscale_failed", "Tailscale did not confirm that Serve is off; the "
                                             "behind-proxy setting was left as it was.",
                   tailscale=st, behind_proxy=settings.behind_proxy())
    if settings.behind_proxy():
        _, fail = await _write(settings.clear_managed_behind_proxy)   # a flag set by hand stays
        if fail is not None:
            return fail
    audit.log("remote_off", by=p.user, port=port)
    return _ok(tailscale=st, behind_proxy=settings.behind_proxy())


@requires("admin", sudo=True)
async def api_clear_behind_proxy(request):
    p = request.state.principal
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    if not settings.behind_proxy():
        return _ok(changed=False)
    source = settings.behind_proxy_source()
    if not settings.proxy_source_managed(source):
        return _set_by_hand(source)
    port = auth.bound_port(request.scope)
    st = await _ts_status(port)
    if st.get("installed") and st.get("serve_running"):
        return err(409, "serve_running", f"Tailscale Serve is running for port {port}; turn it off "
                                         "instead (Remote → Turn off).", tailscale=st)
    if not _stale(True, source, st):
        return err(409, "serve_status_unknown", "Could not confirm that Tailscale Serve is off for this "
                                                "port; the flag was left as it was.", tailscale=st)
    cleared, fail = await _write(settings.clear_managed_behind_proxy)
    if fail is not None:
        return fail
    if not cleared:                                    # changed on disk meanwhile (a hand edit wins)
        if settings.behind_proxy():
            return _set_by_hand(settings.behind_proxy_source())
        return _ok(changed=False)
    audit.log("behind_proxy_cleared", by=p.user, reason="admin")
    return _ok(changed=True)


def _set_by_hand(source: Optional[str]):
    return err(409, "set_by_hand", f"This flag was set by hand in config.json (behind_proxy_source "
                                   f"{json.dumps(source)}) for a proxy or tunnel Tailscale knows nothing "
                                   "about; TeleRig never clears it. Edit config.json to change it.")


# -- audit (view + download) ------------------------------------------------------------------
AUDIT_JSON_MAX = 1000
AUDIT_JSONL_MAX = 100000


@requires("admin")
async def api_audit(request):
    q = request.query_params
    fmt = q.get("format", "json")
    kind = q.get("kind", "all")
    if fmt not in ("json", "jsonl"):
        return err(400, "invalid", "format is json or jsonl.", field="format")
    if kind not in ("tx", "security", "all"):
        return err(400, "invalid", "kind is tx, security or all.", field="kind")
    cap = AUDIT_JSON_MAX if fmt == "json" else AUDIT_JSONL_MAX
    raw = q.get("limit")
    if raw is None:
        limit = 200 if fmt == "json" else cap
    elif not raw.isascii() or not raw.isdigit() or len(raw) > 9:
        return err(400, "invalid", "limit is a whole number.", field="limit")
    else:
        limit = max(1, min(cap, int(raw)))
    rows = await run_in_threadpool(audit.tail, limit, kind)
    if fmt == "json":
        return JSONResponse({"entries": rows, "kind": kind, **audit.stats()}, headers=NO_STORE)
    text = "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in reversed(rows))
    return PlainTextResponse(text, headers={"Content-Disposition": 'attachment; filename="telerig-audit.jsonl"',
                                            "Cache-Control": "no-store"})


# -- the WSJT-X bridge (backend/bridge.py) ------------------------------------------------------
def _bridge_status():
    b = _ctx.bridge
    return b.status(True) if b is not None else None


@requires("admin")
async def api_bridge_get(request):
    return JSONResponse({"config": settings.bridge(), "status": _bridge_status(),
                         "web_port": auth.bound_port(request.scope),
                         "limits": {"port": [bridge_mod.PORT_MIN, bridge_mod.PORT_MAX],
                                    "silence_s": [bridge_mod.SILENCE_MIN, bridge_mod.SILENCE_MAX],
                                    "allow": bridge_mod.ALLOW_MAX, "clients": bridge_mod.MAX_CLIENTS},
                         "defaults": dict(bridge_mod.DEFAULTS, allow=[])}, headers=NO_STORE)


@requires("admin", sudo=True)
async def api_bridge_put(request):
    """Save the bridge settings (sudo: this decides who may command the radio, and whether a
    program may transmit through it). Applied at once; a client already connected is dropped when
    what it was told at connect is no longer true.

    `expect` (optional) is the configuration the form was showing: when the saved one differs
    (another admin's save, or a hand edit of config.json meanwhile) nothing is written and the
    answer is 409 conflict with the current configuration, so a stale form never undoes a change it
    did not show. Switching transmit from off to on ends a transmit pause (audited as a resume by
    this admin): it is a deliberate new go-ahead, like an operator's resume."""
    p = request.state.principal
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    cfg, bad = bridge_mod.validate_config(body, web_port=auth.bound_port(request.scope))
    if bad is not None:
        return err(400, "invalid", bad[1], field=bad[0] or None)
    old = settings.bridge()
    if "expect" in body:
        exp = body["expect"]
        if not isinstance(exp, dict):
            return err(400, "invalid", "expect must be the settings object the form was showing.", field="expect")
        if bridge_mod.normalize_config(exp)[0] != bridge_mod.normalize_config(old)[0]:
            return err(409, "conflict", "The bridge settings were changed meanwhile (by another admin, or by "
                                        "hand in config.json). Here they are as saved now: check them and save again.",
                       config=old)
    _, fail = await _write(settings.set_bridge, cfg)
    if fail is not None:
        return fail
    audit.log("bridge_config", by=p.user, ip=auth.client_ip(request.scope), enabled=cfg["enabled"],
              port=cfg["port"], listen=cfg["listen"], allow=list(cfg["allow"]), ptt=cfg["ptt"],
              silence_s=cfg["silence_s"], was_enabled=old.get("enabled"), was_ptt=old.get("ptt"))
    b = _ctx.bridge
    if b is not None:
        try:
            await b.configure(cfg)
        except Exception as e:
            console(f"TeleRig: applying the bridge settings failed: {e!r}")
        if cfg["ptt"] and not old.get("ptt"):
            try:
                b.resume(by=p.user)                  # a no-op (False) when it was not paused
            except Exception as e:
                console(f"TeleRig: ending the bridge pause failed: {e!r}")
    return _ok(config=cfg, status=_bridge_status())


# -- Administration > Update (backend/updater.py) ------------------------------------------------
def _update_payload() -> dict:
    u = _ctx.updater
    st = u.status() if u is not None else {"job": {"state": "idle"}, "check": None}
    info = u.info() if u is not None else {"updatable": False, "reason": "Updating is not available on this server."}
    return {"info": info, "job": st["job"], "check": st["check"], "boot": _ctx.boot, "pid": os.getpid(),
            "version": __version__}


def _tx_busy() -> bool:
    a = _ctx.arb
    return bool(a is not None and a.owner is not None)      # anyone, the FRONT PANEL included


@requires("admin")
async def api_update_get(request):
    """This install (branch, commit, can it update itself), the last check and the running update."""
    return JSONResponse(await run_in_threadpool(_update_payload), headers=NO_STORE)


@requires("admin")
async def api_update_check(request):
    """Fetch this install's branch from GitHub and list what is new. Changes nothing."""
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    u = _ctx.updater
    if u is None:
        return err(404, "not_available", "Updating is not available on this server.")
    res = await run_in_threadpool(u.check)
    if not res.get("ok"):
        return err(409, "check_failed", res.get("error") or "The check failed.")
    return _ok(check=res)


@requires("admin", sudo=True)
async def api_update_start(request):
    """Update now (sudo): refused while anyone transmits; then fetch, move forward, packages, self-test
    and restart run on a thread (backend/updater.py). Poll GET for the progress."""
    p = request.state.principal
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    u = _ctx.updater
    if u is None:
        return err(404, "not_available", "Updating is not available on this server.")
    audit.log("update_requested", by=p.user, ip=auth.client_ip(request.scope))
    why = await run_in_threadpool(lambda: u.start(p.user, tx_busy=_tx_busy, restart=_ctx.restart))
    if why == "busy":
        return err(409, "busy", "Someone is transmitting. Update when the radio is idle.")
    if why == "running":
        return err(409, "running", "An update is already running.")
    if why:
        return err(409, "not_updatable", why)
    return _ok(**(await run_in_threadpool(_update_payload)))


# -- the route table (server.py adds these) ---------------------------------------------------
def routes() -> list:
    u = "/api/admin/users/{uid}"
    return [
        Route("/api/admin/reauth", api_reauth, methods=["POST"]),
        Route("/api/admin/users", api_users, methods=["GET"]),
        Route("/api/admin/users", api_user_create, methods=["POST"]),
        Route(u, api_user_patch, methods=["PATCH"]),
        Route(u, api_user_delete, methods=["DELETE"]),
        Route(u + "/reset_password", api_user_reset_password, methods=["POST"]),
        Route(u + "/signout", api_user_signout, methods=["POST"]),
        Route(u + "/unlock", api_user_unlock, methods=["POST"]),
        Route(u + "/layout", api_user_layout_delete, methods=["DELETE"]),
        Route("/api/admin/sessions", api_sessions, methods=["GET"]),
        Route("/api/admin/lockouts", api_lockouts, methods=["GET"]),
        Route("/api/admin/lockouts/unlock_ip", api_unlock_ip, methods=["POST"]),
        Route("/api/admin/allowed_hosts", api_allowed_hosts_get, methods=["GET"]),
        Route("/api/admin/allowed_hosts", api_allowed_hosts_put, methods=["PUT"]),
        Route("/api/admin/audio", api_audio_get, methods=["GET"]),
        Route("/api/admin/audio", api_audio_put, methods=["PUT"]),
        Route("/api/admin/radio_transport", api_radio_transport, methods=["PUT"]),
        Route("/api/admin/bridge", api_bridge_get, methods=["GET"]),
        Route("/api/admin/bridge", api_bridge_put, methods=["PUT"]),
        Route("/api/admin/remote_status", api_remote_status, methods=["GET"]),
        Route("/api/admin/tailscale_serve", api_tailscale_serve, methods=["POST"]),
        Route("/api/admin/tailscale_serve_off", api_tailscale_serve_off, methods=["POST"]),
        Route("/api/admin/clear_behind_proxy", api_clear_behind_proxy, methods=["POST"]),
        Route("/api/admin/audit", api_audit, methods=["GET"]),
        Route("/api/admin/update", api_update_get, methods=["GET"]),
        Route("/api/admin/update/check", api_update_check, methods=["POST"]),
        Route("/api/admin/update", api_update_start, methods=["POST"]),
    ]
