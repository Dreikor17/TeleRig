"""Account routes (spec §2.9-§2.10, §2.16): /login, /change-password, /api/login, /api/logout,
/api/auth_status, /api/me, /api/me/password, /api/me/signout_all — plus read_json() and the
session-cookie helpers the other routes share.

Login verification is READ-ONLY (AR-53): the cookie is issued first; last_login and an scrypt
re-hash are best-effort writes after the response. scrypt always runs in the threadpool under a
semaphore of 2 (R9). Rate limits per §2.15 are checked before the password is verified.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs

from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import accounts, audit, auth
from .policy import requires
from .ratelimit import DIRECT_LOCAL, limiter
from .store import StoreBusy, StoreError, console

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
BODY_MAX = 65536
PASSWORD_INPUT_MAX = 1024            # longer inputs are never a valid password (8-72 bytes)
NO_STORE = {"Cache-Control": "no-store"}

# server.py binds these (it owns the Hub): close sockets by token fingerprint / by user.
kick_fp: Callable[[str, int, str], int] = lambda fp, code, reason: 0
kick_user: Callable[[str, int, str], int] = lambda user, code, reason: 0

_scrypt_sem: Optional[asyncio.Semaphore] = None


def _sem() -> asyncio.Semaphore:
    global _scrypt_sem
    if _scrypt_sem is None:
        _scrypt_sem = asyncio.Semaphore(2)
    return _scrypt_sem


async def verify_async(stored: Optional[str], pw: str) -> tuple:
    """(ok, needs_rehash), scrypt in the threadpool (never on the loop). An unknown user (stored
    None) or an absurdly long input still costs one dummy verification (no timing oracle)."""
    async with _sem():
        if stored is None or len(pw) > PASSWORD_INPUT_MAX:
            await run_in_threadpool(accounts.verify, accounts.dummy_hash(), pw[:PASSWORD_INPUT_MAX])
            return False, False
        return await run_in_threadpool(accounts.verify, stored, pw)


async def hash_async(pw: str) -> str:
    async with _sem():
        return await run_in_threadpool(accounts.hash_password, pw)


# -- shared helpers -------------------------------------------------------------------------------
def err(status: int, error: str, message: str = "", **extra) -> JSONResponse:
    body = {"ok": False, "error": error}
    if message:
        body["message"] = message
    body.update(extra)
    return JSONResponse(body, status_code=status, headers=NO_STORE)


async def read_json(request, max_bytes: int = BODY_MAX):
    """The JSON object body (§2.9): {} when bodiless; 413 too_large above max_bytes; 400
    invalid_json for bad JSON or a non-object. Returns a dict or the error response."""
    cl = request.headers.get("content-length")
    if cl and cl.strip().isdigit() and int(cl) > max_bytes:
        return err(413, "too_large", f"The request body is larger than {max_bytes} bytes.")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > max_bytes:
            return err(413, "too_large", f"The request body is larger than {max_bytes} bytes.")
    if not bytes(buf).strip():
        return {}
    try:
        obj = json.loads(bytes(buf).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return err(400, "invalid_json", "The request body must be a JSON object.")
    if not isinstance(obj, dict):
        return err(400, "invalid_json", "The request body must be a JSON object.")
    return obj


def validated_next(value) -> str:
    """A same-site path: starts with "/", not "//" or "/\\", no CR/LF/control chars; else "/"."""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith(("//", "/\\")):
        return "/"
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or len(value) > 2048:
        return "/"
    if value.startswith(("/login", "/change-password")):
        return "/"
    return value


def set_session_cookie(resp, request, token: str, exp: int) -> None:
    import time
    resp.set_cookie(auth.cookie_name(request.scope), token, max_age=max(0, int(exp - time.time())),
                    path="/", httponly=True, samesite="lax", secure=auth.scheme(request.scope) == "https")
    expire_legacy(resp)


def expire_legacy(resp) -> None:
    for name in auth.LEGACY_COOKIES:
        resp.delete_cookie(name, path="/")


def clear_session_cookie(resp, request) -> None:
    resp.delete_cookie(auth.cookie_name(request.scope), path="/", httponly=True, samesite="lax",
                       secure=auth.scheme(request.scope) == "https")
    expire_legacy(resp)


def source(scope) -> tuple:
    """(src, is_direct_local) for the rate limiter (§2.15)."""
    dl = auth.direct_local(scope)
    return (DIRECT_LOCAL if dl else auth.client_ip(scope)), dl


def rate_limited(retry: float) -> JSONResponse:
    s = max(1, int(retry + 0.999))
    return JSONResponse({"ok": False, "error": "rate_limited", "retry_after": s,
                         "message": "Too many attempts. Try again later."},
                        status_code=429, headers={**NO_STORE, "Retry-After": str(s)})


def _record_failure(user: Optional[str], src: str, ip: str, **kw) -> None:
    tripped = limiter.fail(user, src, **kw)
    if tripped:
        audit.log("lockout_tripped", user=user, until=limiter.locked_until(user))


def _page(name: str) -> HTMLResponse:
    html = (FRONTEND / name).read_text(encoding="utf-8")
    return HTMLResponse(html, headers=NO_STORE)


# -- pages --------------------------------------------------------------------------------------
@requires(public=True)
async def login_page(request):
    p = getattr(request.state, "principal", None)
    if p is not None:
        if p.restricted:
            return RedirectResponse("/change-password", status_code=303, headers=NO_STORE)
        nxt = parse_qs(request.url.query).get("next", ["/"])[0]
        return RedirectResponse(validated_next(nxt), status_code=303, headers=NO_STORE)
    return _page("login.html")


@requires("viewer", restricted_ok=True)
async def change_password_page(request):
    return _page("change-password.html")


# -- API ----------------------------------------------------------------------------------------
@requires(public=True)
async def api_auth_status(request):
    p = getattr(request.state, "principal", None)
    if p is not None:
        return JSONResponse({"authed": True, "user": p.user, "role": p.role, "must_change": p.restricted,
                             "default_pw": p.default_pw, "migrated_remote": p.migrated_remote},
                            headers=NO_STORE)
    kind = None
    if auth.direct_local(request.scope):
        a = accounts.get(accounts.ADMIN)
        if a is not None:
            kind = "default" if a.default_pw else ("migrated" if a.migrated_v1 else None)
    return JSONResponse({"authed": False, "setup": {"kind": kind},
                         "local_reason": auth.local_reason(request.scope),
                         "port": auth.bound_port(request.scope)}, headers=NO_STORE)


@requires(public=True)
async def api_login(request):
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    raw_user, pw, setup = body.get("username"), body.get("password"), body.get("setup")
    user = accounts.canonical(raw_user)
    pw = pw if isinstance(pw, str) else ""
    scope = request.scope
    src, dl = source(scope)
    ip = auth.client_ip(scope)

    wait = limiter.check(user, src, direct_local=dl)            # BEFORE verifying (§2.15)
    if wait > 0:
        audit.log("rate_limited", key=src, reason="login", user=user, src=src)
        return rate_limited(wait)

    u = accounts.get(user) if user else None
    ok, rehash = await verify_async(u.hash if u else None, pw)
    if not (u and ok):
        _record_failure(user, src, ip)
        audit.log("login_fail", key=src, src=src, ip=ip, users=[user] if user else [])
        return err(401, "invalid_credentials", "Invalid username or password.")

    if u.default_pw:
        if not dl:                                              # F1: indistinguishable from a wrong password
            _record_failure(user, src, ip)
            audit.log("login_default_remote", user=user, src=src, ip=ip)
            return err(401, "invalid_credentials", "Invalid username or password.")
        if not accounts.setup_matches(setup):
            limiter.fail(user, src, k1=False, k3=False)          # K2 only: the code can't be brute-forced
            audit.log("login_fail", key=src, src=src, ip=ip, users=[user], reason="setup_code")
            return err(403, "setup_code_required",
                       "Enter the setup code shown in the TeleRig window on this PC.")

    limiter.success(user, src)
    token, exp = accounts.make_token(u.name, u.gen)
    p = auth.principal_for(u, token, exp, accounts.token_fp(token), dl)
    nxt = "/change-password" if p.restricted else validated_next(body.get("next"))
    old_hash = u.hash

    async def after():                                          # best-effort; never fails the login
        new_hash = None
        if rehash:
            try:
                new_hash = await hash_async(pw)
            except Exception:
                new_hash = None
        try:
            await run_in_threadpool(accounts.record_login, u.name, old_hash=old_hash, new_hash=new_hash)
        except Exception as e:
            console(f"TeleRig: could not record the sign-in of {u.name}: {e!r}")
            audit.log("store_error", op="record_login", user=u.name, error=type(e).__name__)

    resp = JSONResponse({"ok": True, "must_change": p.restricted, "next": nxt}, headers=NO_STORE,
                        background=BackgroundTask(after))
    set_session_cookie(resp, request, token, exp)               # the cookie first (AR-53)
    if not p.restricted:
        auth.sudo_open(p)                                       # never for migrated-remote (F17)
    audit.log("login_ok", user=u.name, role=p.role, src=src, ip=ip, migrated_remote=p.migrated_remote)
    return resp


@requires("viewer", restricted_ok=True)
async def api_logout(request):
    p = request.state.principal
    try:
        # "listed", or "bumped" when the revocation list is full (only this user's gen moves)
        how = await run_in_threadpool(accounts.revoke, p.token_fp, p.exp, p.user, p.gen)
    except Exception as e:                                      # the cookie is cleared regardless
        how = "failed"
        console(f"TeleRig: could not record the sign-out of {p.user}: {e!r}")
        audit.log("logout_revoke_failed", user=p.user, error=type(e).__name__)
    kick_fp(p.token_fp, 4401, "signed_out")
    auth.sudo_drop(p.token_fp)
    resp = JSONResponse({"ok": True}, headers=NO_STORE)
    clear_session_cookie(resp, request)
    audit.log("logout", user=p.user, ip=auth.client_ip(request.scope), revocation=how)
    return resp


@requires("viewer")
async def api_me(request):
    p = request.state.principal
    return JSONResponse({"user": p.user, "role": p.role, "perms": auth.perms_for(p.role),
                         "migrated_remote": p.migrated_remote}, headers=NO_STORE)


MIGRATED_LOCAL_ONLY = ("Change the shared admin password on the host PC itself, or run "
                       "run.bat --reset-admin (python3 run.py --reset-admin elsewhere).")


@requires("viewer", restricted_ok=True)
async def api_me_password(request):
    p = request.state.principal
    if p.migrated_remote:                                       # F17
        return err(403, "migrated_local_only", MIGRATED_LOCAL_ONLY)
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    cur, new = body.get("current"), body.get("new")
    cur = cur if isinstance(cur, str) else ""
    prob = accounts.password_problem(new, user=p.user, current=cur)
    if prob:
        return err(400, "invalid", prob, field="new")
    src, dl = source(request.scope)
    wait = limiter.check(p.user, src, direct_local=dl)
    if wait > 0:
        return rate_limited(wait)
    u = accounts.get(p.user)
    ok, _ = await verify_async(u.hash if u else None, cur)
    if not (u and ok):
        _record_failure(p.user, src, auth.client_ip(request.scope))
        audit.log("login_fail", key=src, src=src, users=[p.user], reason="password_change")
        return err(401, "invalid_credentials", "The current password is wrong.", field="current")
    try:
        new_hash = await hash_async(new)
        u2 = await run_in_threadpool(_store_password, p.user, new_hash)
    except accounts.AccountError as e:
        return err(404 if e.code == "not_found" else 400, e.code, e.message)
    except StoreBusy:
        return err(503, "store_busy", "The account store is busy; try again.")
    except StoreError:
        return err(500, "store_error", "The account store could not be written; nothing changed.")
    limiter.success(p.user, src)
    kick_user(p.user, 4401, "session_revoked")                  # the reload hook does it too
    token, exp = accounts.make_token(u2.name, u2.gen)
    p2 = auth.principal_for(u2, token, exp, accounts.token_fp(token), dl)
    auth.sudo_drop(p.token_fp)
    auth.sudo_open(p2)
    resp = JSONResponse({"ok": True, "next": "/"}, headers=NO_STORE)
    set_session_cookie(resp, request, token, exp)
    audit.log("pw_changed", user=p.user, by=p.user, ip=auth.client_ip(request.scope))
    return resp


def _store_password(name: str, new_hash: str):
    """set_password with a precomputed hash (scrypt already ran under the semaphore)."""
    c = accounts.canonical(name)

    def fn(d):
        u = d["users"].get(c)
        if u is None:
            raise accounts.AccountError("not_found", "No such user.")
        u.update(hash=new_hash, must_change=False, default_pw=False, migrated_v1=False,
                 gen=accounts._next_gen(d), pw_changed=accounts.now_iso())
        accounts._clear_setup_if_unused(d)
    accounts.store().mutate(fn)
    return accounts.get(c)


@requires("viewer")
async def api_me_signout_all(request):
    p = request.state.principal
    if p.migrated_remote:                                       # F17
        return err(403, "migrated_local_only", MIGRATED_LOCAL_ONLY)
    try:
        await run_in_threadpool(accounts.bump_gen, p.user)
    except StoreBusy:
        return err(503, "store_busy", "The account store is busy; try again.")
    except StoreError:
        return err(500, "store_error", "The account store could not be written; nothing changed.")
    kick_user(p.user, 4401, "session_revoked")
    auth.sudo_drop_user(p.user)
    resp = JSONResponse({"ok": True}, headers=NO_STORE)
    clear_session_cookie(resp, request)
    audit.log("signout_all", user=p.user, by=p.user)
    return resp
