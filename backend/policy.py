"""WebSocket action policy (spec §2.12, I2).

Step A2 skeleton: every action `_handle_cmd` understands has an ALLOW-ALL placeholder row, the
removed host-audio actions are listed in REMOVED, and unknown actions are denied. Step A3 adds the
coarse viewer rule; step A4 replaces every placeholder with the real rule (role, strict argument
types, canonical commands, TX locks) and adds the new actions (tx_hb, tx_stop, rx_audio, mic).

Completeness (I2): every `action == "<name>"` literal in backend/server.py must have a WS_POLICY
row or be in REMOVED. The server checks this at startup (assert_complete) and policy_test.py
checks it offline (T30).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

ROLES = ("viewer", "operator", "admin")

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


def evaluate(rule: Rule, conn: Any, cmd: dict, ctx: Any = None) -> Verdict:
    """Decide one message. A2: placeholder rows pass the command through unchanged (allow-all);
    anything else is denied `invalid` until A4 implements it. Never raises."""
    try:
        if rule.placeholder:
            return Verdict("plain", rule.action, cmd=cmd)
        return deny(rule.action, "invalid")
    except Exception:
        return deny(getattr(rule, "action", "*"), "invalid")


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
