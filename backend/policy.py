"""Access policy: WebSocket actions (spec §2.12, I2) and HTTP routes (§2.10).

WebSocket. WS_POLICY has one row per action: the dispatcher's 58 radio actions (server._handle_cmd)
plus the new tx_hb, tx_stop, rx_audio, mic and bridge_tx. The removed host-audio actions are in
REMOVED, and an unknown action is denied. A WSJT-X bridge client (backend/bridge.py) is decided by
the same table: its commands arrive as the same canonical commands, from a connection whose role
is always `operator`. evaluate() decides one message, in this order:

  role -> STRICT argument types (I20; the verdict carries a CANONICAL command rebuilt from the
  validated fields, never the client's dict) -> the connect transition -> the lock -> the TX rule

and returns a Verdict: deny | busy | ignore | new | claim | release | stop | noowner_stop | plain.
TX start and stop never reach _handle_cmd: claims, releases and stops go to the TX arbiter
(backend/txarb.py), which calls the radio's start/stop primitives itself.

Lock codes (§2.12):
  X  busy {owner} when someone else owns TX (another connection or the FRONT PANEL) and the sender
     is not an admin (F4).
  K  acquire TX claim c (ptt | cw | tune | vox); idempotent for a claim the sender already holds
     (F19, I17). Needs a connected radio and no connect transition.
  S  unkey-style action (ptt false, cw_stop, vox off): from the owner -> release; from a non-owner
     -> ignore with auto:true, else busy (F18, I23); with no owner -> the radio's stop primitive.
  T  tx_stop: the only way to stop another user's TX (I23); the arbiter decides owner / forced /
     no-owner.
  O  only the owner holding ptt (ATAS step).

evaluate_fast() is the fast path for the unkey / heartbeat frames (is_fast): strict types, the
connection's LAST-KNOWN role, no store read and no rate bucket (I4, AR-17).

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
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

ROLES = ("viewer", "operator", "admin")
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}

# Every action _handle_cmd (server.py) handles, in its order.
RADIO_ACTIONS = (
    "set_freq", "tune", "set_mode", "set_filter", "data_mode", "vfo", "mem_recall", "mem_to_vfo",
    "set_level",
    "band", "set_span", "scope_mode", "select_band", "set_meter", "preamp", "att", "lock", "tuner",
    "tune_atu", "atas_step", "rx_func", "agc", "mnotch_w", "tbw", "rit", "rit_freq", "split",
    "duplex", "narrow", "tone_mode", "tone_freq", "dcs_code", "rpt_shift", "width", "contour",
    "contour_freq", "apf", "apf_freq", "txw", "param_eq", "quick_split", "bkin", "keyer",
    "key_speed", "key_pitch", "spot", "zero_in", "scan", "fast", "apf_lvl", "bkin_lvl",
    "filter_shape", "ptt", "cw_tx", "cw_stop", "menu_read", "menu_read_group", "menu_write",
)

REMOVED = frozenset({"host_rx", "host_tx"})     # host audio is server-pinned now (§2.14)
NEW_ACTIONS = frozenset({"tx_hb", "tx_stop", "rx_audio", "mic", "bridge_tx"})

DENY_REASONS = ("role", "unknown_action", "not_connected", "connecting", "unsupported",
                "critical_admin", "tot_off_confirm", "tx_route_confirm", "rate_limited",
                "not_owner", "invalid", "too_large")

# Server constants the validators share (kept here so policy has no server import).
LEVEL_TARGETS = frozenset({"af", "rf", "sql", "rfpwr", "nr_level", "nb_level", "pbt1", "pbt2",
                           "mnotch_pos", "mic", "comp_level", "mon_level", "vox_gain", "cw_pitch",
                           "keyer_speed"})          # == server.LEVEL_TARGETS keys (policy_test checks)
SPANS = frozenset({2500, 5000, 10000, 25000, 50000, 100000, 250000, 500000})   # app.js SPANS
RX_FUNC_NAMES = frozenset({"nb", "nr", "anotch", "mnotch", "comp", "mon", "vox"})
VFO_CODES = frozenset({0, 1, 160, 176})
CONFIRMS = frozenset({"tot_off", "tx_route"})
TX_CLAIMS = ("ptt", "cw", "tune", "vox")
FRONT_PANEL = "FRONT PANEL"
EXTERNAL = -1                                   # the TX arbiter's owner id for a front-panel key


class Invalid(Exception):
    """A field failed strict validation (`denied invalid`)."""


@dataclass(frozen=True)
class Rule:
    action: str
    min_role: str = "operator"
    lock: Optional[str] = "X"          # None | "X" | "K" | "S" | "T" | "O" | "dyn" (ptt, rx_func)
    continuous: bool = False           # coalesced setter: never `rate_limited` (AR-48)
    rate_exempt: bool = False          # unkey / heartbeat paths (I4)
    radio: bool = True                 # a radio command: refused during a connect transition
    validate: Optional[Callable] = field(default=None, compare=False, repr=False)
    placeholder: bool = False          # A2 allow-all rows; none are left (T30 full table)


@dataclass
class Verdict:
    """What _on_text does with a message: deny | busy | ignore | new | claim | release | stop |
    noowner_stop | plain. `cmd` is the CANONICAL command (rebuilt from validated fields); `reason`
    the denial reason; `extra` carries claim / owner / seq / audit details."""
    kind: str
    action: str
    cmd: Optional[dict] = None
    reason: Optional[str] = None
    extra: dict = field(default_factory=dict)


def deny(action: str, reason: str, **extra) -> Verdict:
    return Verdict("deny", action, reason=reason, extra=extra)


def busy(action: str, owner: Optional[str], **extra) -> Verdict:
    return Verdict("busy", action, extra={"owner": owner, **extra})


def rank(role: Optional[str]) -> int:
    """viewer 0 < operator 1 < admin 2; anything else (None, typos) -1: fail closed."""
    return ROLE_RANK.get(role or "", -1)


# -- strict field helpers (I20) ------------------------------------------------------------------
_MISSING = object()


def _b(cmd: dict, key: str) -> bool:
    v = cmd.get(key, _MISSING)
    if type(v) is not bool:
        raise Invalid(key)
    return v


def _i(cmd: dict, key: str, lo: int, hi: int, default: Any = _MISSING) -> int:
    v = cmd.get(key, _MISSING)
    if v is _MISSING and default is not _MISSING:
        return default
    if type(v) is not int or not lo <= v <= hi:
        raise Invalid(key)
    return v


def _in(cmd: dict, key: str, allowed) -> Any:
    v = cmd.get(key, _MISSING)
    if isinstance(v, bool) or v is _MISSING or v not in allowed:
        raise Invalid(key)
    return v


def _s(cmd: dict, key: str, maxlen: int, *, allow_empty: bool = False) -> str:
    v = cmd.get(key, _MISSING)
    if type(v) is not str or len(v) > maxlen or (not allow_empty and not v):
        raise Invalid(key)
    return v


def _opt_bool(cmd: dict, key: str) -> Optional[bool]:
    if key not in cmd:
        return None
    return _b(cmd, key)


def _opt_seq(cmd: dict) -> Optional[int]:
    if "seq" not in cmd:
        return None
    v = cmd["seq"]
    if type(v) is not int or not 0 <= v <= 2 ** 53:
        raise Invalid("seq")
    return v


def _flags(cmd: dict, out: dict, *, auto: bool = False, seq: bool = False) -> dict:
    if auto:
        a = _opt_bool(cmd, "auto")
        if a is not None:
            out["auto"] = a
    if seq:
        s = _opt_seq(cmd)
        if s is not None:
            out["seq"] = s
    return out


# -- per-action validators: (cmd, ctx) -> canonical field dict (raises Invalid) ------------------
def _none(cmd, ctx):
    return {}


def _on(cmd, ctx):
    return {"on": _b(cmd, "on")}


def _field_int(key, lo, hi):
    def v(cmd, ctx):
        return {key: _i(cmd, key, lo, hi)}
    return v


def _v_set_mode(cmd, ctx):
    out = {"mode": _s(cmd, "mode", 16)}
    if "filter" in cmd and cmd["filter"] is not None:
        out["filter"] = _i(cmd, "filter", 1, 3)
    return out


def _v_set_level(cmd, ctx):
    return {"target": _in(cmd, "target", LEVEL_TARGETS), "value": _i(cmd, "value", 0, 255)}


def _v_set_meter(cmd, ctx):
    m = _s(cmd, "meter", 8)
    caps = getattr(getattr(_radio(ctx), "profile", None), "capabilities", None)
    meters = list(getattr(caps, "meters", None) or [])
    if m not in meters:
        raise Invalid("meter")
    return {"meter": m}


def _v_preamp(cmd, ctx):
    if "level" in cmd:
        return {"level": _i(cmd, "level", 0, 2)}
    return {"on": _b(cmd, "on")}


def _v_rx_func(cmd, ctx):
    name = cmd.get("name")
    if type(name) is not str or name not in RX_FUNC_NAMES:
        raise Invalid("name")
    out = {"name": name, "on": _b(cmd, "on")}
    if name == "vox":
        _flags(cmd, out, auto=True, seq=True)
    return out


def _v_ptt(cmd, ctx):
    return _flags(cmd, {"tx": _b(cmd, "tx")}, auto=True, seq=True)


def _v_cw_tx(cmd, ctx):
    out = {"text": _s(cmd, "text", 160), "wpm": _i(cmd, "wpm", 4, 60)}
    return _flags(cmd, out, seq=True)


def _v_cw_stop(cmd, ctx):
    return _flags(cmd, {}, auto=True)


def _v_seq_only(cmd, ctx):
    return _flags(cmd, {}, seq=True)


def _v_menu_read(cmd, ctx):
    return {"num": _i(cmd, "num", 0, 99999)}


def _v_menu_read_group(cmd, ctx):
    return {"group": _s(cmd, "group", 32)}


def _v_menu_write(cmd, ctx):
    num = _i(cmd, "num", 0, 99999)
    value = cmd.get("value", _MISSING)
    if not (type(value) is str or type(value) is int) or (type(value) is str and len(value) > 32):
        raise Invalid("value")
    out = {"num": num, "value": value}
    if "confirm" in cmd:
        c = cmd["confirm"]
        if type(c) is not str or c not in CONFIRMS:
            raise Invalid("confirm")
        out["confirm"] = c
    return out


def _v_atas(cmd, ctx):
    return {"dir": _in(cmd, "dir", (-1, 1))}


def _v_rx_audio(cmd, ctx):
    return {"on": _b(cmd, "on")}


def _row(action, validate, *, role="operator", lock="X", continuous=False, rate_exempt=False,
         radio=True) -> Rule:
    return Rule(action, role, lock, continuous, rate_exempt, radio, validate)


_ROWS = [
    _row("set_freq", _field_int("hz", 0, 10_000_000_000), continuous=True),
    _row("tune", _field_int("delta", -100_000_000, 100_000_000), continuous=True),
    _row("set_mode", _v_set_mode),
    _row("set_filter", _field_int("filter", 1, 3)),
    _row("data_mode", _on),                          # Icom DATA mode (1A 06); needs caps.data_mode
    _row("vfo", lambda c, x: {"code": _in(c, "code", VFO_CODES)}),
    _row("mem_recall", _field_int("ch", 1, 99)),
    _row("mem_to_vfo", _none),
    _row("set_level", _v_set_level, continuous=True),
    _row("band", lambda c, x: {"band": _s(c, "band", 16)}),
    _row("set_span", lambda c, x: {"span": _in(c, "span", SPANS)}),
    _row("scope_mode", lambda c, x: {"center": _b(c, "center")}),
    _row("select_band", lambda c, x: {"band": _in(c, "band", ("main", "sub"))}),
    _row("set_meter", _v_set_meter),                 # X: the owner is never busy (ATAS SWR switch)
    _row("preamp", _v_preamp),
    _row("att", _on),
    _row("lock", _on),
    _row("tuner", _on),
    _row("tune_atu", _v_seq_only, lock="K"),
    _row("atas_step", _v_atas, lock="O"),
    _row("rx_func", _v_rx_func, lock="dyn"),
    _row("agc", _field_int("mode", 1, 3)),
    _row("mnotch_w", _field_int("width", 0, 2)),
    _row("tbw", _field_int("w", 0, 2)),
    _row("rit", _on),
    _row("rit_freq", _field_int("hz", -9999, 9999), continuous=True),
    _row("split", _on),
    _row("duplex", _field_int("mode", 0, 2)),
    _row("narrow", _on),
    _row("tone_mode", _field_int("value", 0, 4)),
    _row("tone_freq", _field_int("idx", 0, 49)),
    _row("dcs_code", _field_int("idx", 0, 103)),
    _row("rpt_shift", _field_int("value", 0, 2)),
    _row("width", _field_int("code", 0, 21)),
    _row("contour", _on),
    _row("contour_freq", _field_int("hz", 10, 3200), continuous=True),
    _row("apf", _on),
    _row("apf_freq", _field_int("v", 0, 50), continuous=True),
    _row("txw", _on),
    _row("param_eq", _on),
    _row("quick_split", _none),
    _row("bkin", _on),
    _row("keyer", _on),
    _row("key_speed", _field_int("wpm", 4, 60)),
    _row("key_pitch", _field_int("code", 0, 75)),
    _row("spot", _on),
    _row("zero_in", _none),
    _row("scan", _field_int("dir", 0, 2)),
    _row("fast", _on),
    _row("apf_lvl", _field_int("v", 0, 3)),
    _row("bkin_lvl", _field_int("v", 0, 2)),
    _row("filter_shape", _on),
    _row("ptt", _v_ptt, lock="dyn"),
    _row("cw_tx", _v_cw_tx, lock="K"),
    _row("cw_stop", _v_cw_stop, lock="S", rate_exempt=True),
    _row("menu_read", _v_menu_read, role="viewer", lock=None),            # F23
    _row("menu_read_group", _v_menu_read_group, role="viewer", lock=None),  # F23 (menu queue)
    _row("menu_write", _v_menu_write),               # admin for critical items (in evaluate)
    # new actions
    _row("tx_hb", _none, role="viewer", lock=None, rate_exempt=True, radio=False),
    _row("tx_stop", _none, lock="T", rate_exempt=True, radio=False),
    _row("rx_audio", _v_rx_audio, role="viewer", lock=None, radio=False),
    _row("mic", _v_rx_audio, role="viewer", lock=None, radio=False),     # on: operator (evaluate)
    # pause (on:false) / resume (on:true) transmit over the WSJT-X bridge; pausing also stops it
    _row("bridge_tx", _v_rx_audio, lock=None, radio=False),
]
WS_POLICY: dict = {r.action: r for r in _ROWS}

# Which TX claim an action starts / stops.
K_CLAIM = {"tune_atu": "tune", "cw_tx": "cw"}
S_CLAIM = {"cw_stop": "cw"}


# -- the fast path classifier ----------------------------------------------------------------------
def is_fast(cmd: Any) -> bool:
    """The §2.12 fast-path classifier: tx_hb, tx_stop, ptt{tx:false}, cw_stop,
    rx_func{name:"vox", on:false}, mic{on:false}, bridge_tx{on:false} (pause the WSJT-X bridge,
    which also stops its TX). Every one of them only stops TX or keeps a claim alive; none can
    start TX. Strict types (I20): `tx`/`on` must be the bool False and `name` the
    exact string "vox", so a value that only looks like "off" (0, "false", null, ...) is not a
    fast-path stop: it falls through to normal evaluation and is denied there."""
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
    if a in ("mic", "bridge_tx"):
        return cmd.get("on") is False
    return False


# -- context accessors (the server passes ctx; unit tests pass fakes) ------------------------------
def _radio(ctx):
    r = getattr(ctx, "radio", None)
    return r() if callable(r) else r


def _arb(ctx):
    return getattr(ctx, "arb", None)


def _owner(ctx):
    a = _arb(ctx)
    return getattr(a, "owner", None) if a is not None else None


def _owner_label(ctx) -> Optional[str]:
    a = _arb(ctx)
    if a is None or a.owner is None:
        return None
    return FRONT_PANEL if a.owner == EXTERNAL else a.owner_user


def _holds(ctx, conn_id, claim) -> bool:
    a = _arb(ctx)
    return a is not None and a.owner == conn_id and claim in a.claims


def _transition(ctx) -> bool:
    t = getattr(ctx, "transition", False)
    return bool(t() if callable(t) else t)


def _state(ctx) -> dict:
    r = _radio(ctx)
    return getattr(r, "state", None) or {}


def _conn_id(conn):
    return getattr(conn, "id", None)


# -- TX rule helpers ---------------------------------------------------------------------------
def _seq_extra(conn, cmd: dict) -> dict:
    s = cmd.get("seq")
    return {"req": {"conn": _conn_id(conn), "seq": s}} if s is not None else {}


def _lock_s(action: str, claim: str, conn, cmd: dict, ctx) -> Verdict:
    owner = _owner(ctx)
    if owner is not None and owner == _conn_id(conn):
        return Verdict("release", action, cmd=cmd, extra={"claim": claim})
    if owner is not None:
        if cmd.get("auto") is True:
            return Verdict("ignore", action, cmd=cmd)
        return busy(action, _owner_label(ctx))
    return Verdict("noowner_stop", action, cmd=cmd, extra={"claims": (claim,)})


def _cw_filtered(radio, text: str) -> str:
    """The text the radio would really send (its own filter, radio.py / yaesu.py)."""
    from .radio import CW_CIV_CHARS, CW_YAESU_CHARS
    prof = getattr(radio, "profile", None)
    if getattr(prof, "cw_send", "") == "line":
        return "".join(c for c in str(text).upper() if c in CW_YAESU_CHARS)[:80]
    return "".join(c for c in str(text) if c in CW_CIV_CHARS)[:30]


def _k_checks(action: str, claim: str, conn, cmd: dict, ctx) -> Optional[Verdict]:
    """Capability / state checks for a NEW claim (after the busy and idempotency checks)."""
    r = _radio(ctx)
    prof = getattr(r, "profile", None)
    caps = getattr(prof, "capabilities", None)
    st = _state(ctx)
    if claim == "ptt":
        if not getattr(caps, "ptt", False):
            return deny(action, "unsupported", **_seq_extra(conn, cmd))
    elif claim == "tune":
        if not getattr(caps, "tuner", False):
            return deny(action, "unsupported", **_seq_extra(conn, cmd))
    elif claim == "vox":
        if getattr(prof, "protocol", "civ") != "civ" or "vox" not in (getattr(caps, "tx_funcs", None) or []):
            return deny(action, "unsupported", **_seq_extra(conn, cmd))
    elif claim == "cw":
        how = getattr(prof, "cw_send", "")
        if not how or not st.get("cw_tx_ready"):
            return deny(action, "unsupported", **_seq_extra(conn, cmd))
        modes = ("CW-USB", "CW-LSB") if how == "line" else ("CW", "CW-R")
        if st.get("mode_name") not in modes:
            return deny(action, "unsupported", **_seq_extra(conn, cmd))
        if st.get("cw_tx"):
            return deny(action, "invalid", **_seq_extra(conn, cmd))
        if not _cw_filtered(r, cmd["text"]).strip():
            return deny(action, "invalid", **_seq_extra(conn, cmd))
    return None


def _lock_k(action: str, claim: str, conn, cmd: dict, ctx) -> Verdict:
    if not _state(ctx).get("connected"):
        return deny(action, "not_connected", **_seq_extra(conn, cmd))
    owner = _owner(ctx)
    if owner is not None and owner != _conn_id(conn):
        return busy(action, _owner_label(ctx), tx_denied=True, **_seq_extra(conn, cmd))
    if not _holds(ctx, _conn_id(conn), claim):      # a held claim is an idempotent no-op (F19)
        bad = _k_checks(action, claim, conn, cmd, ctx)
        if bad is not None:
            return bad
    args = {"text": cmd["text"], "wpm": cmd["wpm"]} if claim == "cw" else None
    return Verdict("claim", action, cmd=cmd, extra={"claim": claim, "args": args, "seq": cmd.get("seq")})


def _lock_x(action: str, conn, ctx) -> Optional[Verdict]:
    owner = _owner(ctx)
    if owner is not None and owner != _conn_id(conn) and getattr(conn, "role", None) != "admin":
        return busy(action, _owner_label(ctx))
    return None


def _lock_o(action: str, conn, cmd: dict, ctx) -> Verdict:
    owner = _owner(ctx)
    if owner is not None and owner != _conn_id(conn):
        return busy(action, _owner_label(ctx))
    if not _holds(ctx, _conn_id(conn), "ptt"):
        return deny(action, "not_owner")
    return Verdict("plain", action, cmd=cmd)


def _menu_write(conn, cmd: dict, ctx) -> Verdict:
    """§2.12 menu_write checks: item, critical -> admin, X lock, the radio's own encoder, TOT=OFF
    (admin + confirm, decided by the encoded bytes), PTT-routing (idle + confirm), audit."""
    from . import menu_engine
    a = "menu_write"
    r = _radio(ctx)
    item = None
    try:
        item = r.menu_item(cmd["num"]) if r is not None and hasattr(r, "menu_item") else None
    except Exception:
        item = None
    if item is None or getattr(item, "readonly", False):
        return deny(a, "invalid")
    if item.critical and getattr(conn, "role", None) != "admin":
        return deny(a, "critical_admin", need="admin")
    x = _lock_x(a, conn, ctx)
    if x is not None:
        return x
    proto = getattr(getattr(r, "profile", None), "protocol", "civ")
    enc = menu_engine.yaesu_encode if proto == "yaesu" else menu_engine.civ_write_data
    try:
        wire = enc(item, cmd["value"])
    except (menu_engine.MenuError, ValueError, TypeError, IndexError):
        return deny(a, "invalid")
    extra: dict = {"item": item, "critical": bool(item.critical)}
    if getattr(item, "tx_tot", False):
        try:
            off = enc(item, "OFF" if item.kind == "enum" else 0)
        except Exception:
            off = None
        if off is not None and wire == off:
            if cmd.get("confirm") != "tot_off":
                return deny(a, "tot_off_confirm")
            extra["tot_off"] = True
    if getattr(item, "tx_route", False) and item.kind == "enum":
        try:
            label = str(item.options[menu_engine._enum_index(item, cmd["value"])])
        except Exception:
            return deny(a, "invalid")
        if "RTS" in label.upper() or "DTR" in label.upper():
            if cmd.get("confirm") != "tx_route":
                return deny(a, "tx_route_confirm")
            if _owner(ctx) is not None:
                return busy(a, _owner_label(ctx))
            extra["tx_route"] = label
    return Verdict("plain", a, cmd=cmd, extra=extra)


# -- evaluate ------------------------------------------------------------------------------------
def evaluate(rule: Rule, conn: Any, cmd: dict, ctx: Any = None) -> Verdict:
    """Decide one message: role -> strict argument types (-> canonical cmd) -> transition -> lock
    -> TX rule. Never raises: any unexpected error is `denied invalid`."""
    a = getattr(rule, "action", "*")
    try:
        if rule.placeholder:                       # no placeholder rows remain (T30)
            return deny(a, "invalid")
        if rank(getattr(conn, "role", None)) < rank(rule.min_role) or rank(getattr(conn, "role", None)) < 0:
            return deny(a, "role", need=rule.min_role)
        if not isinstance(cmd, dict) or rule.validate is None:
            return deny(a, "invalid")
        try:
            fields = rule.validate(cmd, ctx)
        except Invalid:
            return deny(a, "invalid", **({"req": {"conn": _conn_id(conn), "seq": cmd["seq"]}}
                                         if type(cmd.get("seq")) is int else {}))
        canon = {"action": a, **fields}
        return _decide(rule, conn, canon, ctx)
    except Exception:
        return deny(a, "invalid")


def _decide(rule: Rule, conn: Any, cmd: dict, ctx: Any) -> Verdict:
    a = rule.action
    stopping = (a in ("cw_stop", "tx_stop", "tx_hb")
                or (a == "ptt" and cmd.get("tx") is False)
                or (a == "rx_func" and cmd.get("name") == "vox" and cmd.get("on") is False)
                or (a in ("mic", "rx_audio") and cmd.get("on") is False))
    if rule.radio and not stopping and _transition(ctx):
        return deny(a, "connecting")
    if a == "tx_hb":
        return Verdict("new", a, cmd=cmd)
    if a == "tx_stop":
        return Verdict("stop", a, cmd=cmd)
    if a in ("rx_audio", "bridge_tx"):
        return Verdict("new", a, cmd=cmd)
    if a == "mic":
        if cmd["on"]:
            if rank(getattr(conn, "role", None)) < ROLE_RANK["operator"]:
                return deny(a, "role", need="operator")
            st = _state(ctx)
            caps = getattr(getattr(_radio(ctx), "profile", None), "capabilities", None)
            if not st.get("connected"):
                return deny(a, "not_connected")
            if not getattr(caps, "ptt", False):
                return deny(a, "unsupported")
        return Verdict("new", a, cmd=cmd)
    if a == "data_mode":
        caps = getattr(getattr(_radio(ctx), "profile", None), "capabilities", None)
        if not getattr(caps, "data_mode", False):
            return deny(a, "unsupported")            # then the X lock below, like any setter
    if a == "menu_read":
        return Verdict("plain", a, cmd=cmd)
    if a == "menu_read_group":
        return Verdict("new", a, cmd=cmd)
    if a == "menu_write":
        return _menu_write(conn, cmd, ctx)
    if a == "ptt":
        if cmd["tx"]:
            return _lock_k(a, "ptt", conn, cmd, ctx)
        return _lock_s(a, "ptt", conn, cmd, ctx)
    if a == "rx_func":
        if cmd["name"] == "vox":
            if cmd["on"]:
                return _lock_k(a, "vox", conn, cmd, ctx)
            return _lock_s(a, "vox", conn, cmd, ctx)
        x = _lock_x(a, conn, ctx)
        return x if x is not None else Verdict("plain", a, cmd=cmd)
    if rule.lock == "K":
        return _lock_k(a, K_CLAIM[a], conn, cmd, ctx)
    if rule.lock == "S":
        return _lock_s(a, S_CLAIM[a], conn, cmd, ctx)
    if rule.lock == "O":
        if not _state(ctx).get("connected"):
            return deny(a, "not_connected")
        return _lock_o(a, conn, cmd, ctx)
    if rule.lock == "X":
        x = _lock_x(a, conn, ctx)
        if x is not None:
            return x
        extra = {"audit": "panel_lock"} if a == "lock" else {}
        return Verdict("plain", a, cmd=cmd, extra=extra)
    return deny(a, "invalid")                     # never allow by default


def evaluate_fast(conn: Any, cmd: dict, ctx: Any = None) -> Verdict:
    """The §2.12 fast path for is_fast() frames: strict types, the connection's LAST-KNOWN role, no
    store read and no rate bucket (I4, AR-17), so a store error, a lock or a kick never swallows an
    unkey. Never raises."""
    a = cmd.get("action") if isinstance(cmd, dict) else None
    try:
        if not is_fast(cmd):
            return deny(a if isinstance(a, str) else "*", "invalid")
        rule = WS_POLICY.get(a)
        if rule is None:                            # pragma: no cover - every fast action has a row
            return deny(a, "unknown_action")
        return evaluate(rule, conn, cmd, ctx)
    except Exception:
        return deny(a if isinstance(a, str) else "*", "invalid")


# -- /api/connect body validation (§2.10 step 1) -------------------------------------------------
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


def connect_shape(body: dict, profiles: dict) -> tuple:
    """Step 1a: strict types only (no port enumeration). Returns (error_field | None, info) where
    info = {transport, radio, auto, force, confirm}. `transport` must be a str here; whether it is
    a known kind is decided after the sim-only guard (which refuses anything but exactly "sim")."""
    t = body.get("transport", _MISSING)
    if type(t) is not str or len(t) > 16:
        return "transport", None
    rid = body.get("radio", _MISSING)
    if type(rid) is not str or rid not in profiles:
        return "radio", None
    info = {"transport": t, "radio": rid}
    for k in ("auto", "force", "confirm"):
        if k in body:
            if type(body[k]) is not bool:
                return k, None
            info[k] = body[k]
        else:
            info[k] = False
    return None, info


def _is_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def connect_canonical(body: dict, profile) -> tuple:
    """Step 1b (after the sim-only / autoconnect guards): the transport fields with strict types.
    Returns (error_field | None, canonical transport dict WITHOUT credentials, extras) where extras
    holds the LAN user/password. Serial port existence is checked by the caller (enumeration)."""
    t = body.get("transport")
    if t == "sim":
        return None, {"transport": "sim"}, {}
    if t == "serial":
        port = body.get("port", _MISSING)
        if type(port) is not str or not port or len(port) > 64:
            return "port", None, {}
        baud = body.get("baud", getattr(profile, "default_baud", 115200))
        if type(baud) is not int or not 300 <= baud <= 230400:
            return "baud", None, {}
        return None, {"transport": "serial", "port": port, "baud": baud}, {}
    if t == "lan":
        host = body.get("host", _MISSING)
        if type(host) is not str:
            return "host", None, {}
        host = host.strip()
        if not host or not (HOST_RE.match(host) or _is_ip(host)):
            return "host", None, {}
        port = body.get("port", 50001)
        if type(port) is not int or not 1 <= port <= 65535:
            return "port", None, {}
        ex = {}
        for k in ("user", "password"):
            v = body.get(k, "")
            if v is None:
                v = ""
            if type(v) is not str or len(v) > 64:
                return k, None, {}
            ex[k] = v
        return None, {"transport": "lan", "host": host, "port": port}, ex
    return "transport", None, {}


def same_transport(canon: dict, pin: Optional[dict], profile=None) -> bool:
    """F20: the request equals the pin on transport + port + baud (serial) or host + port (LAN).
    LAN credentials are never compared (they are never stored)."""
    if not isinstance(pin, dict) or pin.get("transport") != canon.get("transport"):
        return False
    t = canon["transport"]
    if t == "sim":
        return True
    if t == "serial":
        pb = pin.get("baud", getattr(profile, "default_baud", None))
        return str(pin.get("port")) == canon["port"] and pb == canon["baud"]
    if t == "lan":
        pp = pin.get("port", 50001)
        try:
            pp = int(pp)
        except (TypeError, ValueError):
            return False
        return str(pin.get("host", "")).strip().lower() == canon["host"].lower() and pp == canon["port"]
    return False


def connect_rule_error(role: str, radio_id: str, canon: dict, last_radio: Optional[str],
                       pins: dict, profile=None) -> Optional[str]:
    """§2.10 step 4 (F20, AR-23): None when allowed, else the reason (switch_radio |
    transport_not_pinned). Admins are never limited here.

    A non-admin needs BOTH:
      * the radio: equal to settings.last_radio (only while last_radio is unset may it be any
        radio, e.g. one pinned in Admin -> Radio & audio before any admin connect), and
      * a pin: settings.radio_transports[radio] must exist and the request must equal it.
    With no pin there is nothing an operator may open: a fresh or upgraded install (v1 never
    stored last_radio) refuses every operator connect until an admin connects once (§4.5 "Ask an
    admin to connect this radio once"). Otherwise an operator could pick any COM port (pyserial
    raises RTS/DTR on open, which keys an RTS-PTT interface) or aim LAN credentials anywhere."""
    if role == "admin":
        return None
    if last_radio is not None and radio_id != last_radio:
        return "switch_radio"
    pin = (pins or {}).get(radio_id) if isinstance(pins, dict) else None
    if not isinstance(pin, dict):
        return "transport_not_pinned"
    return None if same_transport(canon, pin, profile) else "transport_not_pinned"


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
        if r.placeholder or r.validate is None:
            errs.append(f"WS_POLICY[{a!r}] is a placeholder row")
        if r.lock not in (None, "X", "K", "S", "T", "O", "dyn"):
            errs.append(f"WS_POLICY[{a!r}] has unknown lock {r.lock!r}")
    return errs


def assert_complete(source: str) -> None:
    """Startup check (I2): raise RuntimeError so the server refuses to start on a gap."""
    errs = completeness_errors(source)
    if errs:
        raise RuntimeError("WS policy table incomplete:\n  " + "\n  ".join(errs))
    if not NEW_ACTIONS <= set(WS_POLICY):
        raise RuntimeError("WS policy table lacks a new action")
