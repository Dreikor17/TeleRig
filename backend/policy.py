"""Access policy: WebSocket actions (spec §2.12, I2) and HTTP routes (§2.10).

WebSocket. Every action `_handle_cmd` understands has a placeholder row, the removed host-audio
actions are listed in REMOVED, and unknown actions are denied. Step A3's COARSE rule: a
connection whose (effective) role is below operator gets `denied role` for every action except
tx_hb and the menu reads (F23); everyone else passes. Step A4 replaces every placeholder with the
real rule (role, strict argument types, canonical commands, TX locks) and adds the new actions
(tx_hb, tx_stop, rx_audio, mic).

HTTP. Every route endpoint carries `__tr_policy__` (set by @requires); startup refuses to run
otherwise (assert_routes). @requires re-checks the principal the Gate attached
(request.state.principal, effective role per F17), the restricted (must-change) state, the sudo
window and the direct-local requirement.

Completeness (I2): every `action == "<name>"` literal in backend/server.py must have a WS_POLICY
row or be in REMOVED. The server checks this at startup (assert_complete) and policy_test.py
checks it offline (T30).
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

ROLES = ("viewer", "operator", "admin")
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}
VIEWER_ACTIONS = frozenset({"tx_hb", "menu_read", "menu_read_group"})   # A3 coarse rule exemptions

# Every action _handle_cmd (server.py) handles, in its order.
RADIO_ACTIONS = (
    "set_freq", "tune", "set_mode", "set_filter", "vfo", "mem_recall", "mem_to_vfo", "set_level",
    "band", "set_span", "scope_mode", "select_band", "set_meter", "preamp", "att", "lock", "tuner",
    "tune_atu", "atas_step", "rx_func", "agc", "mnotch_w", "tbw", "rit", "rit_freq", "split",
    "duplex", "narrow", "tone_mode", "tone_freq", "dcs_code", "rpt_shift", "width", "contour",
    "contour_freq", "apf", "apf_freq", "txw", "param_eq", "quick_split", "bkin", "keyer",
    "key_speed", "key_pitch", "spot", "zero_in", "scan", "fast", "apf_lvl", "bkin_lvl",
    "filter_shape", "ptt", "cw_tx", "cw_stop", "menu_read", "menu_read_group", "menu_write",
)

REMOVED = frozenset({"host_rx", "host_tx"})     # host audio is server-pinned now (§2.14)
NEW_ACTIONS: frozenset = frozenset()            # A4: tx_hb, tx_stop, rx_audio, mic

DENY_REASONS = ("role", "unknown_action", "not_connected", "connecting", "unsupported",
                "critical_admin", "tot_off_confirm", "tx_route_confirm", "rate_limited",
                "not_owner", "invalid", "too_large")


@dataclass(frozen=True)
class Rule:
    action: str
    min_role: str = "viewer"
    lock: Optional[str] = None          # None | "X" | "K" | "S" | "T" | "O"  (A4)
    continuous: bool = False            # coalesced setter (A4, AR-48)
    rate_exempt: bool = False           # unkey paths (I4)
    placeholder: bool = False           # A2 allow-all row; A4 must leave none (T30 full table)


WS_POLICY: dict = {a: Rule(a, placeholder=True) for a in RADIO_ACTIONS}


@dataclass
class Verdict:
    """What _on_text does with a message: deny | busy | ignore | new | claim | release | stop |
    noowner_stop | plain. `cmd` is the command handed on (A4: canonical, rebuilt from validated
    fields); `reason` is the denial reason."""
    kind: str
    action: str
    cmd: Optional[dict] = None
    reason: Optional[str] = None
    extra: dict = field(default_factory=dict)


def deny(action: str, reason: str) -> Verdict:
    return Verdict("deny", action, reason=reason)


def is_fast(cmd: Any) -> bool:
    """The §2.12 fast-path classifier: tx_hb, tx_stop, ptt{tx:false}, cw_stop,
    rx_func{name:"vox", on:false}, mic{on:false}. Every one of them only stops TX or keeps a claim
    alive; none can start TX. Strict types (I20): `tx`/`on` must be the bool False and `name` the
    exact string "vox", so a value that only looks like "off" (0, "false", null, ...) is not a
    fast-path stop.

    A2 uses it to let only these frames through from a kicked (closing) connection or while the
    server is locked, so neither can swallow an unkey (AR-17, §2.4). tx_hb, tx_stop and mic have
    no rows until A4 and are still denied `unknown_action`. A4 dispatches them via
    evaluate_fast()."""
    if not isinstance(cmd, dict):
        return False
    a = cmd.get("action")
    if a in ("tx_hb", "tx_stop", "cw_stop"):
        return True
    if a == "ptt":
        return cmd.get("tx") is False
    if a == "rx_func":
        n = cmd.get("name")
        return type(n) is str and n == "vox" and cmd.get("on") is False
    if a == "mic":
        return cmd.get("on") is False
    return False


def rank(role: Optional[str]) -> int:
    """viewer 0 < operator 1 < admin 2; anything else (None, typos) -1: fail closed."""
    return ROLE_RANK.get(role or "", -1)


def coarse_ok(conn: Any, action: str) -> bool:
    """A3 coarse WS rule: operators and admins may send anything with a row; a viewer (or a
    connection without a known role) only tx_hb and the menu reads."""
    if action in VIEWER_ACTIONS:
        return rank(getattr(conn, "role", None)) >= 0
    return rank(getattr(conn, "role", None)) >= ROLE_RANK["operator"]


def evaluate(rule: Rule, conn: Any, cmd: dict, ctx: Any = None) -> Verdict:
    """Decide one message. A3: placeholder rows pass the command through unchanged when the
    coarse role rule allows it, else `denied role`; anything else is denied `invalid` until A4
    implements it. Never raises."""
    try:
        if rule.placeholder:
            if not coarse_ok(conn, rule.action):
                return deny(rule.action, "role")
            return Verdict("plain", rule.action, cmd=cmd)
        return deny(rule.action, "invalid")
    except Exception:
        return deny(getattr(rule, "action", "*"), "invalid")


def evaluate_fast(conn: Any, cmd: dict) -> Verdict:
    """The §2.12 fast path for is_fast() frames: strict types (already checked by is_fast), the
    connection's LAST-KNOWN role, no store read and no rate bucket (I4, AR-17), so a store error
    or a kick never swallows an unkey. A3: the coarse role rule on the placeholder row; tx_hb,
    tx_stop and mic have no rows until A4 (`denied unknown_action`). Never raises."""
    a = cmd.get("action") if isinstance(cmd, dict) else None
    try:
        rule = WS_POLICY.get(a)
        if rule is None:
            return deny(a if isinstance(a, str) else "*", "unknown_action")
        return evaluate(rule, conn, cmd)
    except Exception:
        return deny(a if isinstance(a, str) else "*", "invalid")


# -- HTTP route policy (§2.10) ----------------------------------------------------------------
@dataclass(frozen=True)
class RoutePolicy:
    min_role: Optional[str]          # None = public
    restricted_ok: bool = False
    sudo: bool = False
    local: bool = False
    public: bool = False


def _json(status: int, error: str, message: str, **extra):
    from starlette.responses import JSONResponse
    return JSONResponse({"ok": False, "error": error, "message": message, **extra}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def check_route(pol: RoutePolicy, request) -> Optional[Any]:
    """None when the request satisfies the policy, else the error response."""
    if pol.public:
        return None
    from . import auth
    p = getattr(request.state, "principal", None)
    if p is None:
        return _json(401, "auth_required", "Sign in first.")
    if p.restricted and not pol.restricted_ok:
        return _json(403, "password_change_required", "Change your password first.")
    if rank(p.role) < rank(pol.min_role):
        return _json(403, "forbidden", "Not permitted for your role.", need=pol.min_role)
    if pol.local and not auth.direct_local(request.scope):
        return _json(403, "local_only", "Do this from a browser on the host PC itself "
                                        f"(http://localhost:{auth.bound_port(request.scope)}).")
    if pol.sudo and not auth.sudo_active(p):
        return _json(403, "reauth_required", "Confirm your password to continue.")
    return None


def requires(min_role: Optional[str] = "viewer", *, restricted_ok: bool = False, sudo: bool = False,
             local: bool = False, public: bool = False) -> Callable:
    """Decorator for an HTTP endpoint `async def f(request)`: attaches __tr_policy__ and
    re-checks it on every call (the Gate already did the coarse part)."""
    if not public and min_role not in ROLES:
        raise ValueError(f"unknown role {min_role!r}")
    pol = RoutePolicy(None if public else min_role, restricted_ok, sudo, local, public)

    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(request):
            err = check_route(pol, request)
            if err is not None:
                return err
            return await fn(request)
        wrapper.__tr_policy__ = pol
        return wrapper
    return deco


def mark(obj: Any, min_role: Optional[str] = "viewer", **kw) -> Any:
    """Attach a policy to something @requires cannot wrap (the WS endpoint, which checks its own
    principal; the /static mount, which the Gate guards). Returns obj."""
    obj.__tr_policy__ = RoutePolicy(None if kw.get("public") else min_role, **kw)
    return obj


def route_policy_errors(routes) -> list:
    """Every Route/WebSocketRoute endpoint and every Mount must carry __tr_policy__ (I2)."""
    errs = []
    for r in routes:
        target = getattr(r, "endpoint", None)
        if target is None:                       # Mount (no endpoint): the mount object itself
            target = r
        if not isinstance(getattr(target, "__tr_policy__", None), RoutePolicy):
            errs.append(f"route {getattr(r, 'path', r)!r} has no __tr_policy__")
    return errs


def assert_routes(routes) -> None:
    errs = route_policy_errors(routes)
    if errs:
        raise RuntimeError("route policy incomplete:\n  " + "\n  ".join(errs))


# -- completeness (I2) ----------------------------------------------------------------------
_LITERAL_RE = re.compile(r"""\baction\s*==\s*["']([A-Za-z0-9_]+)["']""")


def action_literals(source: str) -> set:
    """Every `action == "<name>"` literal in a source text."""
    return set(_LITERAL_RE.findall(source))


def completeness_errors(source: str) -> list:
    errs = []
    for a in sorted(action_literals(source) - set(WS_POLICY) - REMOVED):
        errs.append(f"action {a!r} in server.py has no WS_POLICY row and is not in REMOVED")
    for a in sorted(set(WS_POLICY) & REMOVED):
        errs.append(f"action {a!r} is both in WS_POLICY and REMOVED")
    for a in sorted(NEW_ACTIONS - set(WS_POLICY)):
        errs.append(f"new action {a!r} has no WS_POLICY row")
    for a, r in WS_POLICY.items():
        if r.action != a:
            errs.append(f"WS_POLICY[{a!r}] names action {r.action!r}")
        if r.min_role not in ROLES:
            errs.append(f"WS_POLICY[{a!r}] has unknown role {r.min_role!r}")
    return errs


def assert_complete(source: str) -> None:
    """Startup check (I2): raise RuntimeError so the server refuses to start on a gap."""
    errs = completeness_errors(source)
    if errs:
        raise RuntimeError("WS policy table incomplete:\n  " + "\n  ".join(errs))
