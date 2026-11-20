"""Offline policy checks — run: python -m backend.policy_test

T30 (I2): every `action == "<name>"` literal in backend/server.py has a real (non-placeholder)
WS_POLICY row or is in REMOVED, and the checker really detects a gap. Then the policy itself with
fake connections, a fake arbiter and fake radios (no radio, socket, server or file is touched):
roles (I11, F23), strict argument types and canonical commands (I20, T60), the lock codes X/K/S/T/O
(F4, F18, F19, I23), menu_write TOT / PTT-routing checks (F8, AR-13, AR-14), the /api/connect
validation and F20 helpers (T70, AR-33, AR-61), plus the TX-safety menu flags and the radios'
menu_item() accessors.
"""
import dataclasses
from pathlib import Path
from types import SimpleNamespace

from backend import policy, profiles
from backend.radio import Radio
from backend.yaesu import YaesuRadio

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception as e:                       # wrong exception type
        return f"{type(e).__name__}: {e}"
    return False


# ---- T30: regex completeness, full table ---------------------------------------------------------
SRC = (Path(__file__).resolve().parent / "server.py").read_text(encoding="utf-8")
lits = policy.action_literals(SRC)

C("T30 server.py has >= 58 action literals (regex still matches the dispatcher)", len(lits) >= 58, True)
C("T30 no completeness errors", policy.completeness_errors(SRC), [])
C("T30 every literal has a row or is REMOVED", sorted(lits - set(policy.WS_POLICY) - policy.REMOVED), [])
C("T30 host_rx/host_tx branches deleted from server.py", sorted(lits & policy.REMOVED), [])
C("T30 REMOVED = host_rx, host_tx", sorted(policy.REMOVED), ["host_rx", "host_tx"])
C("T30 NEW_ACTIONS", sorted(policy.NEW_ACTIONS), ["bridge_tx", "mic", "rx_audio", "tx_hb", "tx_stop"])
C("T30 rows == the dispatcher's 58 literals + the 5 new actions",
  sorted(set(policy.WS_POLICY) ^ (lits | policy.NEW_ACTIONS)), [])
C("T30 63 rows; 65 actions covered with REMOVED", (len(policy.WS_POLICY), len(set(policy.WS_POLICY) | policy.REMOVED)), (63, 65))
C("T30 every row is real: a validator, no placeholder",
  [a for a, r in policy.WS_POLICY.items() if r.placeholder or r.validate is None], [])
C("T30 every role is known", [a for a, r in policy.WS_POLICY.items() if r.min_role not in policy.ROLES], [])
gap = SRC + '\n        elif action == "bogus_action":\n            pass\n'
C("T30 a literal without a row is reported", any("bogus_action" in e for e in policy.completeness_errors(gap)), True)
C("T30 assert_complete raises on a gap", raises(lambda: policy.assert_complete(gap), RuntimeError), True)
C("T30 assert_complete passes on server.py", raises(lambda: policy.assert_complete(SRC), Exception), False)
C("T30 single-quoted literal is seen too", "zz_q" in policy.action_literals("if action == 'zz_q':"), True)
saved = policy.WS_POLICY["set_freq"]
policy.WS_POLICY["set_freq"] = policy.Rule("set_freq", placeholder=True)
C("T30 a placeholder row is reported", any("placeholder" in e for e in policy.completeness_errors(SRC)), True)
policy.WS_POLICY["set_freq"] = saved
from backend import server  # noqa: E402  (import only: no lifespan; radios never connected)
C("LEVEL_TARGETS match server.LEVEL_TARGETS", set(server.LEVEL_TARGETS), set(policy.LEVEL_TARGETS))

# ---- the table: roles, locks, continuous, rate exemption ------------------------------------------
R = policy.WS_POLICY
C("viewer rows: menu reads, tx_hb, rx_audio, mic (on needs operator)",
  sorted(a for a, r in R.items() if r.min_role == "viewer"),
  ["menu_read", "menu_read_group", "mic", "rx_audio", "tx_hb"])
C("no admin-only rows (critical menu writes are decided per item)", [a for a, r in R.items() if r.min_role == "admin"], [])
C("continuous setters (AR-48)", sorted(a for a, r in R.items() if r.continuous),
  ["apf_freq", "contour_freq", "rit_freq", "set_freq", "set_level", "tune"])
C("rate-exempt rows (I4)", sorted(a for a, r in R.items() if r.rate_exempt), ["cw_stop", "tx_hb", "tx_stop"])
C("K rows", sorted(a for a, r in R.items() if r.lock == "K"), ["cw_tx", "tune_atu"])
C("dynamic rows (ptt, rx_func)", sorted(a for a, r in R.items() if r.lock == "dyn"), ["ptt", "rx_func"])
C("O row (ATAS step)", sorted(a for a, r in R.items() if r.lock == "O"), ["atas_step"])
C("T row (tx_stop)", sorted(a for a, r in R.items() if r.lock == "T"), ["tx_stop"])
C("S row (cw_stop)", sorted(a for a, r in R.items() if r.lock == "S"), ["cw_stop"])


# ---- fakes ---------------------------------------------------------------------------------------
class Conn:
    def __init__(self, cid, role, user=None):
        self.id, self.role, self.user = cid, role, user or f"U{cid}"


class Arb:
    def __init__(self, owner=None, user=None, claims=()):
        self.owner, self.owner_user, self.claims = owner, user, set(claims)


class FakeRadio:
    def __init__(self, pid="ic9700", **state):
        self.profile = profiles.PROFILES[pid]
        self.state = {"connected": True, "radio": pid, "mode_name": "USB", "cw_tx": False,
                      "cw_tx_ready": bool(self.profile.cw_send), **state}
        self._menu_index = {it.num: it for it in self.profile.menu}

    def menu_item(self, num):
        return self._menu_index.get(int(num))


def ctx(arb=None, radio=None, transition=False):
    return SimpleNamespace(arb=arb or Arb(), radio=radio or FakeRadio(), transition=transition)


def ev(conn, cmd, c=None):
    rule = R.get(cmd.get("action"))
    return policy.evaluate(rule, conn, cmd, c or ctx())


OP, OP2, ADM, V = Conn(1, "operator", "OP1"), Conn(2, "operator", "OP2"), Conn(3, "admin", "ADMIN"), Conn(4, "viewer", "V1")

# ---- roles (I11, F23) --------------------------------------------------------------------------------
for cmd in ({"action": "set_freq", "hz": 145000000}, {"action": "ptt", "tx": True}, {"action": "tune_atu"},
            {"action": "cw_tx", "text": "CQ", "wpm": 20}, {"action": "rx_func", "name": "vox", "on": True},
            {"action": "menu_write", "num": 42, "value": 0}, {"action": "lock", "on": True},
            {"action": "set_meter", "meter": "SWR"}, {"action": "cw_stop"}, {"action": "tx_stop"},
            {"action": "mic", "on": True}):
    v = ev(V, cmd)
    C(f"viewer {cmd['action']} -> denied role", (v.kind, v.reason), ("deny", "role"))
for cmd in ({"action": "menu_read", "num": 41}, {"action": "menu_read_group", "group": "TX"},
            {"action": "tx_hb"}, {"action": "rx_audio", "on": True}, {"action": "mic", "on": False}):
    C(f"viewer {cmd['action']} allowed (F23 / new)", ev(V, cmd).kind in ("plain", "new"), True)
for role in (None, "", "root", "Admin"):
    C(f"role {role!r} -> denied role (fail closed)", ev(Conn(9, role), {"action": "tx_hb"}).reason, "role")
C("viewer ptt false on the fast path -> denied role (I11)",
  policy.evaluate_fast(V, {"action": "ptt", "tx": False}, ctx()).reason, "role")
C("viewer tx_hb on the fast path -> new", policy.evaluate_fast(V, {"action": "tx_hb"}, ctx()).kind, "new")

# ---- strict types + canonical commands (I20, T60) -----------------------------------------------------
for bad in (1, "true", "false", 0, None, [], {}, 1.0):
    v = ev(OP, {"action": "ptt", "tx": bad})
    C(f"ptt tx={bad!r} -> denied invalid", (v.kind, v.reason), ("deny", "invalid"))
    C(f"ptt tx={bad!r} is not a fast-path stop", policy.is_fast({"action": "ptt", "tx": bad}), False)
for bad in ("off", 0, None, "false"):
    C(f"rx_func vox on={bad!r} -> denied invalid", ev(OP, {"action": "rx_func", "name": "vox", "on": bad}).reason, "invalid")
C("rx_func unknown name -> invalid", ev(OP, {"action": "rx_func", "name": "VOX", "on": False}).reason, "invalid")
C("set_freq bool -> invalid", ev(OP, {"action": "set_freq", "hz": True}).reason, "invalid")
C("set_freq float -> invalid", ev(OP, {"action": "set_freq", "hz": 145e6}).reason, "invalid")
C("set_freq out of range -> invalid", ev(OP, {"action": "set_freq", "hz": -1}).reason, "invalid")
C("set_level unknown target -> invalid", ev(OP, {"action": "set_level", "target": "x", "value": 1}).reason, "invalid")
C("set_level value 256 -> invalid", ev(OP, {"action": "set_level", "target": "af", "value": 256}).reason, "invalid")
C("set_span not in SPANS -> invalid", ev(OP, {"action": "set_span", "span": 12345}).reason, "invalid")
C("vfo code not allowed -> invalid", ev(OP, {"action": "vfo", "code": 2}).reason, "invalid")
C("scope_mode center 1 -> invalid", ev(OP, {"action": "scope_mode", "center": 1}).reason, "invalid")
C("seq must be a non-negative int", ev(OP, {"action": "ptt", "tx": True, "seq": -1}).reason, "invalid")
C("auto must be a bool", ev(OP, {"action": "ptt", "tx": False, "auto": 1}).reason, "invalid")
C("set_meter not one of the radio's meters -> invalid", ev(OP, {"action": "set_meter", "meter": "XX"}).reason, "invalid")
v = ev(OP, {"action": "set_freq", "hz": 145000000, "evil": "x", "__proto__": 1})
C("canonical command: unknown keys dropped", (v.kind, v.cmd), ("plain", {"action": "set_freq", "hz": 145000000}))
v = ev(OP, {"action": "preamp", "level": 2})
C("preamp level -> canonical", v.cmd, {"action": "preamp", "level": 2})
C("preamp on bool -> canonical", ev(OP, {"action": "preamp", "on": True}).cmd, {"action": "preamp", "on": True})
C("preamp level 3 -> invalid", ev(OP, {"action": "preamp", "level": 3}).reason, "invalid")

# ---- X lock (F4) --------------------------------------------------------------------------------------
owned = ctx(Arb(owner=1, user="OP1", claims={"ptt"}))
v = ev(OP2, {"action": "set_freq", "hz": 145000000}, owned)
C("X: non-owner operator while OP1 owns TX -> busy {owner}", (v.kind, v.extra.get("owner")), ("busy", "OP1"))
C("X: the owner itself -> plain", ev(OP, {"action": "set_freq", "hz": 1}, owned).kind, "plain")
C("X: admin -> plain (never busy)", ev(ADM, {"action": "set_freq", "hz": 1}, owned).kind, "plain")
ext = ctx(Arb(owner=policy.EXTERNAL, user="FRONT PANEL", claims={"ptt"}))
C("X: EXTERNAL counts as an owner", (ev(OP2, {"action": "split", "on": True}, ext).kind,
                                    ev(OP2, {"action": "split", "on": True}, ext).extra.get("owner")), ("busy", "FRONT PANEL"))
C("X: set_meter by the owner is allowed (ATAS SWR)", ev(OP, {"action": "set_meter", "meter": "SWR"}, owned).kind, "plain")
C("X: menu_read is never locked", ev(OP2, {"action": "menu_read", "num": 41}, owned).kind, "plain")
C("lock action carries the panel_lock audit", ev(OP, {"action": "lock", "on": True}).extra.get("audit"), "panel_lock")

# ---- K (claims) ------------------------------------------------------------------------------------------
v = ev(OP, {"action": "ptt", "tx": True, "seq": 7})
C("K: idle ptt -> claim ptt (seq kept)", (v.kind, v.extra.get("claim"), v.extra.get("seq")), ("claim", "ptt", 7))
v = ev(OP2, {"action": "ptt", "tx": True, "seq": 3}, owned)
C("K: another owner -> busy + tx_denied + req", (v.kind, v.extra.get("tx_denied"), v.extra.get("req")),
  ("busy", True, {"conn": 2, "seq": 3}))
C("K: admin is busy too (admins take over with tx_stop)", ev(ADM, {"action": "ptt", "tx": True}, owned).kind, "busy")
C("K: the owner re-claiming ptt -> claim (the arbiter makes it a no-op, F19)",
  ev(OP, {"action": "ptt", "tx": True}, owned).kind, "claim")
C("K: not connected -> denied not_connected",
  ev(OP, {"action": "ptt", "tx": True}, ctx(radio=FakeRadio(connected=False))).reason, "not_connected")
C("K: connect transition -> denied connecting", ev(OP, {"action": "ptt", "tx": True}, ctx(transition=True)).reason, "connecting")
C("K: IC-728 has no ptt -> unsupported", ev(OP, {"action": "ptt", "tx": True}, ctx(radio=FakeRadio("ic728"))).reason, "unsupported")
C("K: IC-9700 has no tuner -> tune_atu unsupported", ev(OP, {"action": "tune_atu"}).reason, "unsupported")
C("K: FT-991A tune_atu -> claim tune", ev(OP, {"action": "tune_atu"}, ctx(radio=FakeRadio("ft991a"))).extra.get("claim"), "tune")
C("K: VOX on a Yaesu -> unsupported", ev(OP, {"action": "rx_func", "name": "vox", "on": True}, ctx(radio=FakeRadio("ft991a"))).reason, "unsupported")
C("K: VOX on an Icom -> claim vox", ev(OP, {"action": "rx_func", "name": "vox", "on": True}).extra.get("claim"), "vox")
cwr = FakeRadio(mode_name="CW")
C("K: cw_tx in CW -> claim cw with args", (lambda v: (v.kind, v.extra.get("claim"), v.extra.get("args")))(
    ev(OP, {"action": "cw_tx", "text": "CQ", "wpm": 20}, ctx(radio=cwr))), ("claim", "cw", {"text": "CQ", "wpm": 20}))
C("T62 cw_tx text empty after the Icom filter -> invalid", ev(OP, {"action": "cw_tx", "text": "~~~", "wpm": 20}, ctx(radio=cwr)).reason, "invalid")
C("cw_tx not in CW mode -> unsupported", ev(OP, {"action": "cw_tx", "text": "CQ", "wpm": 20}).reason, "unsupported")
C("cw_tx while already sending -> invalid", ev(OP, {"action": "cw_tx", "text": "CQ", "wpm": 20},
                                              ctx(radio=FakeRadio(mode_name="CW", cw_tx=True))).reason, "invalid")
C("cw_tx without cw_tx_ready -> unsupported", ev(OP, {"action": "cw_tx", "text": "CQ", "wpm": 20},
                                                ctx(radio=FakeRadio(mode_name="CW", cw_tx_ready=False))).reason, "unsupported")
C("cw_tx wpm 61 -> invalid", ev(OP, {"action": "cw_tx", "text": "CQ", "wpm": 61}, ctx(radio=cwr)).reason, "invalid")
C("cw_tx 161 chars -> invalid", ev(OP, {"action": "cw_tx", "text": "E" * 161, "wpm": 20}, ctx(radio=cwr)).reason, "invalid")
C("FT-991A cw: Yaesu filter (lower case is upper-cased, not dropped)",
  ev(OP, {"action": "cw_tx", "text": "cq", "wpm": 20}, ctx(radio=FakeRadio("ft991a", mode_name="CW-USB", cw_tx_ready=True))).kind, "claim")
# one SSTV picture: the claim names its mode; only a mode that ends inside the radio's own TOT is let through
sv = lambda v: (v.kind, v.extra.get("claim"), v.extra.get("args"), v.extra.get("seq"), v.reason)   # noqa: E731
C("K: ptt + sstv pd160 on the IC-9700 (3-min TOT) -> claim ptt with args {sstv}",
  sv(ev(OP, {"action": "ptt", "tx": True, "sstv": "pd160", "seq": 4})), ("claim", "ptt", {"sstv": "pd160"}, 4, None))
C("K: ... the canonical command keeps the mode", ev(OP, {"action": "ptt", "tx": True, "sstv": "r36"}).cmd,
  {"action": "ptt", "tx": True, "sstv": "r36"})
C("K: ptt + sstv pd160 on the FT-991A -> claim", ev(OP, {"action": "ptt", "tx": True, "sstv": "pd160"}, ctx(radio=FakeRadio("ft991a"))).kind,
  "claim")
C("K: a plain ptt claim carries no args", ev(OP, {"action": "ptt", "tx": True}).extra.get("args"), None)
for mid in ("pd180", "pd240", "pd290", "sdx"):
    C(f"K: ptt + sstv {mid} (longer than the 3-min TOT allows) -> unsupported, seq echoed",
      (lambda v: (v.kind, v.reason, v.extra.get("req")))(ev(OP, {"action": "ptt", "tx": True, "sstv": mid, "seq": 9})),
      ("deny", "unsupported", {"conn": 1, "seq": 9}))
notot = FakeRadio("ic9700")
notot.profile = dataclasses.replace(profiles.PROFILES["ic9700"], tot_s=0)     # a radio with no hardware TOT
C("K: no TOT on the radio: Martin M1 (1:57) still fits 120 s", ev(OP, {"action": "ptt", "tx": True, "sstv": "m1"}, ctx(radio=notot)).kind,
  "claim")
C("K: no TOT on the radio: PD-120 (2:08) -> unsupported", ev(OP, {"action": "ptt", "tx": True, "sstv": "pd120"}, ctx(radio=notot)).reason,
  "unsupported")
for bad in ("pd999", "", "PD160", 160, None, True, ["pd160"], {"m": "pd160"}):
    C(f"K: ptt + sstv {bad!r} -> invalid", ev(OP, {"action": "ptt", "tx": True, "sstv": bad}).reason, "invalid")
C("K: sstv on a release (ptt false) -> invalid", ev(OP, {"action": "ptt", "tx": False, "sstv": "r36"}, owned).reason, "invalid")
C("K: IC-728 picture -> unsupported (no ptt)", ev(OP, {"action": "ptt", "tx": True, "sstv": "r36"}, ctx(radio=FakeRadio("ic728"))).reason,
  "unsupported")
C("K: a viewer's picture -> role", ev(V, {"action": "ptt", "tx": True, "sstv": "r36"}).reason, "role")
C("K: someone else on the air: a picture is busy", ev(OP2, {"action": "ptt", "tx": True, "sstv": "r36"}, owned).kind, "busy")

# ---- S (unkey-style, F18 / I23) ------------------------------------------------------------------------
C("S: owner ptt false -> release", (lambda v: (v.kind, v.extra.get("claim")))(ev(OP, {"action": "ptt", "tx": False}, owned)), ("release", "ptt"))
C("S: owner auto ptt false -> release", ev(OP, {"action": "ptt", "tx": False, "auto": True}, owned).kind, "release")
C("S: non-owner auto -> ignore (G-1)", ev(OP2, {"action": "ptt", "tx": False, "auto": True}, owned).kind, "ignore")
C("S: non-owner explicit ptt false -> busy (F18)", ev(OP2, {"action": "ptt", "tx": False}, owned).kind, "busy")
C("S: non-owner cw_stop -> busy", ev(OP2, {"action": "cw_stop"}, owned).kind, "busy")
C("S: non-owner vox off -> busy", ev(OP2, {"action": "rx_func", "name": "vox", "on": False}, owned).kind, "busy")
C("S: admin non-owner ptt false -> busy too (only tx_stop stops another user)",
  ev(ADM, {"action": "ptt", "tx": False}, owned).kind, "busy")
v = ev(OP, {"action": "ptt", "tx": False})
C("S: no owner -> noowner_stop ptt", (v.kind, v.extra.get("claims")), ("noowner_stop", ("ptt",)))
C("S: no owner cw_stop -> noowner_stop cw", ev(OP, {"action": "cw_stop"}).extra.get("claims"), ("cw",))
C("S: stops pass during a connect transition", ev(OP, {"action": "ptt", "tx": False}, ctx(transition=True)).kind, "noowner_stop")
C("T: tx_stop -> stop (the arbiter decides)", ev(OP2, {"action": "tx_stop"}, owned).kind, "stop")
C("T: tx_stop during a transition -> stop", ev(OP2, {"action": "tx_stop"}, ctx(transition=True)).kind, "stop")
C("fast path: owner ptt false -> release", policy.evaluate_fast(OP, {"action": "ptt", "tx": False}, owned).kind, "release")
C("fast path: tx_hb -> new", policy.evaluate_fast(OP, {"action": "tx_hb"}, owned).kind, "new")
C("fast path: mic off -> new", policy.evaluate_fast(V, {"action": "mic", "on": False}, owned).kind, "new")
C("fast path: strict auto type", policy.evaluate_fast(OP, {"action": "cw_stop", "auto": "yes"}, owned).reason, "invalid")
C("fast path refuses a non-fast frame", policy.evaluate_fast(OP, {"action": "ptt", "tx": True}, owned).kind, "deny")

# ---- O (ATAS step) --------------------------------------------------------------------------------------
C("O: owner holding ptt -> plain", ev(OP, {"action": "atas_step", "dir": 1}, owned).kind, "plain")
C("O: nobody owns -> denied not_owner", ev(OP, {"action": "atas_step", "dir": 1}).reason, "not_owner")
C("O: another owner -> busy", ev(OP2, {"action": "atas_step", "dir": -1}, owned).kind, "busy")
C("O: owner without ptt -> not_owner", ev(OP, {"action": "atas_step", "dir": 1}, ctx(Arb(1, "OP1", {"cw"}))).reason, "not_owner")
C("O: dir 2 -> invalid", ev(OP, {"action": "atas_step", "dir": 2}, owned).reason, "invalid")

# ---- mic -------------------------------------------------------------------------------------------------
C("mic on: operator, connected, ptt-capable -> new", ev(OP, {"action": "mic", "on": True}).kind, "new")
C("mic on: radio not connected -> not_connected", ev(OP, {"action": "mic", "on": True}, ctx(radio=FakeRadio(connected=False))).reason, "not_connected")
C("mic on: IC-728 -> unsupported", ev(OP, {"action": "mic", "on": True}, ctx(radio=FakeRadio("ic728"))).reason, "unsupported")
C("mic is NOT a TX claim (F12): non-owner mic on while OP1 owns -> new", ev(OP2, {"action": "mic", "on": True}, owned).kind, "new")

# ---- menu_write (F8, AR-13, AR-14; T23 / T71 in-process) --------------------------------------------------
ic = ctx(radio=FakeRadio("ic9700"))
C("T23 operator critical item -> critical_admin", ev(OP, {"action": "menu_write", "num": 41, "value": "3 min"}, ic).reason, "critical_admin")
for val in (0, "OFF", "00"):
    C(f"T23 admin TOT {val!r} -> tot_off_confirm", ev(ADM, {"action": "menu_write", "num": 41, "value": val}, ic).reason, "tot_off_confirm")
for val in (False, 0.0, None):
    C(f"T23 admin TOT {val!r} -> invalid", ev(ADM, {"action": "menu_write", "num": 41, "value": val}, ic).reason, "invalid")
C("T23 admin TOT 'off' -> invalid (encoder error)", ev(ADM, {"action": "menu_write", "num": 41, "value": "off"}, ic).reason, "invalid")
v = ev(ADM, {"action": "menu_write", "num": 41, "value": "OFF", "confirm": "tot_off"}, ic)
C("T23 admin TOT OFF + confirm -> plain + tot_off audit flag", (v.kind, v.extra.get("tot_off"), v.extra.get("critical")), ("plain", True, True))
C("T23 admin TOT '3 min' needs no confirm", ev(ADM, {"action": "menu_write", "num": 41, "value": "3 min"}, ic).kind, "plain")
C("confirm must be a known string", ev(ADM, {"action": "menu_write", "num": 41, "value": "OFF", "confirm": True}, ic).reason, "invalid")
C("menu_write unknown item -> invalid", ev(ADM, {"action": "menu_write", "num": 99999, "value": 0}, ic).reason, "invalid")
C("menu_write non-critical by an operator -> plain", ev(OP, {"action": "menu_write", "num": 42, "value": "ON"}, ic).kind, "plain")
C("menu_write non-critical by an operator while OP2 owns -> busy (X)",
  ev(OP, {"action": "menu_write", "num": 42, "value": "ON"}, ctx(Arb(2, "OP2", {"ptt"}), FakeRadio("ic9700"))).kind, "busy")
ft = ctx(radio=FakeRadio("ft991a"))
C("FT-991A TOT 0 (int item) -> tot_off_confirm", ev(ADM, {"action": "menu_write", "num": 36, "value": 0}, ft).reason, "tot_off_confirm")
C("FT-991A TOT '00' -> tot_off_confirm", ev(ADM, {"action": "menu_write", "num": 36, "value": "00"}, ft).reason, "tot_off_confirm")
C("FT-991A TOT 2 -> plain", ev(ADM, {"action": "menu_write", "num": 36, "value": 2}, ft).kind, "plain")
C("T71 FT-991A 110 RTS without confirm -> tx_route_confirm", ev(ADM, {"action": "menu_write", "num": 110, "value": "RTS"}, ft).reason, "tx_route_confirm")
C("T71 ... index 2 (DTR) without confirm -> tx_route_confirm", ev(ADM, {"action": "menu_write", "num": 110, "value": 2}, ft).reason, "tx_route_confirm")
C("T71 ... with confirm while someone owns TX -> busy",
  ev(ADM, {"action": "menu_write", "num": 110, "value": "RTS", "confirm": "tx_route"}, ctx(Arb(1, "OP1", {"ptt"}), FakeRadio("ft991a"))).kind, "busy")
v = ev(ADM, {"action": "menu_write", "num": 110, "value": "RTS", "confirm": "tx_route"}, ft)
C("T71 ... idle + confirm -> plain, critical, tx_route label", (v.kind, v.extra.get("critical"), v.extra.get("tx_route")), ("plain", True, "RTS"))
C("T71 ... DAKY needs no confirm", ev(ADM, {"action": "menu_write", "num": 110, "value": "DAKY"}, ft).kind, "plain")
C("T71 operator -> critical_admin", ev(OP, {"action": "menu_write", "num": 110, "value": "RTS", "confirm": "tx_route"}, ft).reason, "critical_admin")
i7 = ctx(radio=FakeRadio("ic7300mk2"))
C("IC-7300MK2 96 'USB (A) DTR' -> tx_route_confirm", ev(ADM, {"action": "menu_write", "num": 96, "value": "USB (A) DTR"}, i7).reason, "tx_route_confirm")
C("IC-7300MK2 96 OFF -> plain", ev(ADM, {"action": "menu_write", "num": 96, "value": "OFF"}, i7).kind, "plain")

# ---- /api/connect helpers (§2.10, T70 unit) ---------------------------------------------------------------
P = profiles.PROFILES
C("connect_shape: transport must be a str", policy.connect_shape({"transport": 1, "radio": "ic9700"}, P)[0], "transport")
C("connect_shape: 'SIM' is a str (the sim-only guard refuses it next)", policy.connect_shape({"transport": "SIM", "radio": "ic9700"}, P)[0], None)
C("connect_shape: unknown radio", policy.connect_shape({"transport": "sim", "radio": "x"}, P)[0], "radio")
C("connect_shape: auto must be a bool", policy.connect_shape({"transport": "sim", "radio": "ic9700", "auto": "false"}, P)[0], "auto")
C("connect_shape: force must be a bool", policy.connect_shape({"transport": "sim", "radio": "ic9700", "force": 1}, P)[0], "force")
bauds = sorted({p.default_baud for p in P.values()} | {300, 1200, 9600, 230400})
C("T70 every profile's default_baud and 300/1200/9600 pass",
  [b for b in bauds if policy.connect_canonical({"transport": "serial", "port": "COM7", "baud": b}, P["ic728"])[0]], [])
C("T70 baud 299 / 230401 / '9600' / True refused",
  [policy.connect_canonical({"transport": "serial", "port": "COM7", "baud": b}, P["ic728"])[0] for b in (299, 230401, "9600", True)],
  ["baud"] * 4)
C("serial baud defaults to the profile's", policy.connect_canonical({"transport": "serial", "port": "COM7"}, P["ft991a"])[1],
  {"transport": "serial", "port": "COM7", "baud": 38400})
err, canon, creds = policy.connect_canonical({"transport": "lan", "host": "192.168.2.40", "port": 50001, "user": "u", "password": "p"}, P["ic9700"])
C("T70 LAN {port:50001} is a UDP port, credentials kept apart", (err, canon, creds),
  (None, {"transport": "lan", "host": "192.168.2.40", "port": 50001}, {"user": "u", "password": "p"}))
C("LAN port 0 / 65536 / '50001' refused", [policy.connect_canonical({"transport": "lan", "host": "h", "port": p}, P["ic9700"])[0]
                                          for p in (0, 65536, "50001")], ["port"] * 3)
C("LAN host with a space refused", policy.connect_canonical({"transport": "lan", "host": "a b"}, P["ic9700"])[0], "host")
C("LAN host 'radio.lan' and an IPv6 literal accepted",
  [policy.connect_canonical({"transport": "lan", "host": h}, P["ic9700"])[0] for h in ("radio.lan", "fe80::1")], [None, None])
SIMC = {"transport": "sim"}
SER = {"transport": "serial", "port": "COM7", "baud": 38400}
C("F20 admin is never limited", policy.connect_rule_error("admin", "ft991a", SER, "ic9700", {}), None)
C("F20 fresh install (no last_radio, no pin) -> transport_not_pinned",
  policy.connect_rule_error("operator", "ic9700", SIMC, None, {}), "transport_not_pinned")
C("F20 fresh install: an operator serial connect (any COM port) -> transport_not_pinned",
  policy.connect_rule_error("operator", "ft991a", {"transport": "serial", "port": "COM3", "baud": 38400}, None, {},
                            P["ft991a"]), "transport_not_pinned")
C("F20 fresh install: an operator LAN connect (any host) -> transport_not_pinned",
  policy.connect_rule_error("operator", "ic9700", {"transport": "lan", "host": "10.0.0.1", "port": 50001}, None, {}),
  "transport_not_pinned")
C("F20 no last_radio, pins None / not a dict -> transport_not_pinned",
  [policy.connect_rule_error("operator", "ic9700", SIMC, None, pins) for pins in (None, [], {"ic9700": "sim"})],
  ["transport_not_pinned"] * 3)
C("F20 no last_radio, but the radio is pinned (Admin -> Radio & audio) -> allowed on its pin",
  policy.connect_rule_error("operator", "ic9700", SIMC, None, {"ic9700": SIMC}), None)
C("F20 no last_radio, pinned radio, another transport -> transport_not_pinned",
  policy.connect_rule_error("operator", "ft991a", SIMC, None, {"ft991a": SER}), "transport_not_pinned")
C("F20 no last_radio: another radio without its own pin -> transport_not_pinned",
  policy.connect_rule_error("operator", "ft991a", SIMC, None, {"ic9700": SIMC}), "transport_not_pinned")
C("T70 operator, other radio -> switch_radio",
  policy.connect_rule_error("operator", "ft991a", SIMC, "ic9700", {"ic9700": SIMC}), "switch_radio")
C("T70 operator, the pinned transport -> allowed",
  policy.connect_rule_error("operator", "ft991a", SER, "ft991a", {"ft991a": SER}, P["ft991a"]), None)
C("T70 operator, serial port differing from the pin -> transport_not_pinned",
  policy.connect_rule_error("operator", "ft991a", {**SER, "port": "COM8"}, "ft991a", {"ft991a": SER}, P["ft991a"]), "transport_not_pinned")
C("operator, baud differing from the pin -> transport_not_pinned",
  policy.connect_rule_error("operator", "ft991a", {**SER, "baud": 9600}, "ft991a", {"ft991a": SER}, P["ft991a"]), "transport_not_pinned")
C("operator, sim while serial is pinned -> transport_not_pinned",
  policy.connect_rule_error("operator", "ft991a", SIMC, "ft991a", {"ft991a": SER}), "transport_not_pinned")
C("operator, last_radio set but no pin -> transport_not_pinned",
  policy.connect_rule_error("operator", "ic9700", SIMC, "ic9700", {}), "transport_not_pinned")
LANP = {"transport": "lan", "host": "192.168.2.40", "port": 50001}
C("operator LAN same host/port (credentials never compared) -> allowed",
  policy.connect_rule_error("operator", "ic9700", dict(LANP), "ic9700", {"ic9700": LANP}), None)
C("operator LAN other host -> transport_not_pinned",
  policy.connect_rule_error("operator", "ic9700", {**LANP, "host": "10.0.0.1"}, "ic9700", {"ic9700": LANP}), "transport_not_pinned")

# ---- HTTP route policy (I2: every route declares one; startup aborts otherwise) ------------------
C("every route of server.app carries __tr_policy__", policy.route_policy_errors(server.app.routes), [])


def _route_key(r):
    ms = sorted(m for m in (getattr(r, "methods", None) or ()) if m != "HEAD")
    return f"{'|'.join(ms) or 'ANY'} {getattr(r, 'path', '')}"


pols = {_route_key(r): getattr(getattr(r, "endpoint", r), "__tr_policy__") for r in server.app.routes}
paths = {getattr(r, "path", "") for r in server.app.routes}
C("no (method, path) appears twice", len(pols), len(server.app.routes))
C("public routes are exactly /login, /api/login, /api/auth_status",
  sorted(p for p, pol in pols.items() if pol.public), ["GET /api/auth_status", "GET /login", "POST /api/login"])
C("restricted_ok routes", sorted(p for p, pol in pols.items() if pol.restricted_ok),
  ["GET /change-password", "POST /api/logout", "POST /api/me/password"])
C("operator routes (incl. HTTP STOP, AR-15)", sorted(p for p, pol in pols.items() if pol.min_role == "operator"),
  ["GET /api/ports", "POST /api/connect", "POST /api/disconnect", "POST /api/tx_stop"])
ADMIN_ROUTES = {   # §2.17: (sudo, local)
    "POST /api/admin/reauth": (False, False), "GET /api/admin/users": (False, False),
    "POST /api/admin/users": (True, False), "PATCH /api/admin/users/{uid}": (True, False),
    "DELETE /api/admin/users/{uid}": (True, False), "POST /api/admin/users/{uid}/reset_password": (True, False),
    "POST /api/admin/users/{uid}/signout": (True, False), "POST /api/admin/users/{uid}/unlock": (False, False),
    "DELETE /api/admin/users/{uid}/layout": (False, False), "GET /api/admin/sessions": (False, False),
    "GET /api/admin/lockouts": (False, False), "POST /api/admin/lockouts/unlock_ip": (False, False),
    "GET /api/admin/allowed_hosts": (False, False), "PUT /api/admin/allowed_hosts": (True, False),
    "GET /api/admin/audio": (False, False), "PUT /api/admin/audio": (True, False),
    "PUT /api/admin/radio_transport": (True, False), "GET /api/admin/remote_status": (False, False),
    "POST /api/admin/tailscale_serve": (True, True), "POST /api/admin/tailscale_serve_off": (False, False),
    "POST /api/admin/clear_behind_proxy": (True, False), "GET /api/admin/audit": (False, False),
    "GET /api/admin/bridge": (False, False), "PUT /api/admin/bridge": (True, False),
    # Administration > Update: reading and checking need admin; Update now also a fresh sudo
    "GET /api/admin/update": (False, False), "POST /api/admin/update/check": (False, False),
    "POST /api/admin/update": (True, False)}
C("admin routes (§2.17)", sorted(p for p, pol in pols.items() if pol.min_role == "admin"), sorted(ADMIN_ROUTES))
C("admin routes: sudo / direct-local flags (F9: Serve ON = admin + sudo + local; OFF = admin only)",
  {k: (pols[k].sudo, pols[k].local) for k in ADMIN_ROUTES if k in pols}, ADMIN_ROUTES)
C("every /api/admin/ route is admin", sorted(p for p, pol in pols.items()
                                             if "/api/admin/" in p and pol.min_role != "admin"), [])
C("/api/me/layout GET/PUT/DELETE: viewer, not while restricted (T34)",
  [(pols[f"{m} /api/me/layout"].min_role, pols[f"{m} /api/me/layout"].restricted_ok) for m in ("GET", "PUT", "DELETE")],
  [("viewer", False)] * 3)
C("removed routes are gone (404): set_password, audio_devices, the old Tailscale routes",
  sorted(p for p in ("/api/set_password", "/api/audio_devices", "/api/remote_status", "/api/tailscale_serve",
                     "/api/tailscale_serve_off") if p in paths), [])
from starlette.routing import Route  # noqa: E402


async def _bare(request):
    return None
C("a route without a policy is reported", policy.route_policy_errors([Route("/x", _bare)]) != [], True)
C("assert_routes raises on a route without a policy",
  raises(lambda: policy.assert_routes([Route("/x", _bare)]), RuntimeError), True)
C("requires() refuses an unknown role", raises(lambda: policy.requires("superuser"), ValueError), True)

# ---- fast-path classifier (§2.12): the only frames a kicked/locked connection still acts on ----
fast = [{"action": "ptt", "tx": False}, {"action": "cw_stop"}, {"action": "tx_hb"}, {"action": "tx_stop"},
        {"action": "rx_func", "name": "vox", "on": False}, {"action": "mic", "on": False},
        {"action": "ptt", "tx": False, "auto": True, "extra": 1}, {"action": "bridge_tx", "on": False}]
not_fast = [{"action": "ptt", "tx": True}, {"action": "ptt", "tx": 0}, {"action": "ptt", "tx": "false"},
            {"action": "ptt", "tx": None}, {"action": "ptt", "tx": []}, {"action": "ptt"},
            {"action": "cw_tx", "text": "CQ", "wpm": 20}, {"action": "tune_atu"},
            {"action": "rx_func", "name": "vox", "on": True}, {"action": "rx_func", "name": "vox", "on": 0},
            {"action": "rx_func", "name": "VOX", "on": False}, {"action": "rx_func", "name": ["vox"], "on": False},
            {"action": "rx_func", "name": "nb", "on": False}, {"action": "rx_func", "on": False},
            {"action": "mic", "on": True}, {"action": "mic", "on": 0}, {"action": "mic"},
            {"action": "bridge_tx", "on": True}, {"action": "bridge_tx", "on": 0}, {"action": "bridge_tx"},
            {"action": "bridge_tx", "on": "false"},
            {"action": "set_freq", "hz": 145000000}, {"action": "PTT", "tx": False}, {"action": 5},
            {"tx": False}, [], None, "ptt"]
C("is_fast: the unkey/heartbeat frames", [c for c in fast if not policy.is_fast(c)], [])
C("is_fast: strict types, nothing that can start TX", [c for c in not_fast if policy.is_fast(c)], [])

# ---- TX-safety menu flags (§1 file map, §2.12 menu_write) -----------------------------------
def items(pid):
    return {it.num: it for it in profiles.PROFILES[pid].menu}


ftm = items("ft991a")
C("FT-991A item 36 TX TOT is critical", ftm[36].critical, True)
C("FT-991A item 36 TX TOT is tx_tot", ftm[36].tx_tot, True)
for n in (47, 71, 76, 110):
    C(f"FT-991A item {n} PTT SELECT is tx_route", ftm[n].tx_route, True)

want_tot = {"ft991a": {36}, "ft891": {514}, "ic7300mk2": {32}, "ic9700": {41}}
want_route = {"ft991a": {47, 71, 76, 110}, "ft891": {607, 712, 810, 903, 1108},
              "ic7300mk2": {96, 97, 98}, "ic9700": set()}
for pid in want_tot:
    m = items(pid)
    C(f"{pid} tx_tot items", {n for n, it in m.items() if it.tx_tot}, want_tot[pid])
    C(f"{pid} tx_route items", {n for n, it in m.items() if it.tx_route}, want_route[pid])
    C(f"{pid} every tx_tot/tx_route item is critical",
      [n for n, it in m.items() if (it.tx_tot or it.tx_route) and not it.critical], [])
    C(f"{pid} every tx_route item has an RTS/DTR option",
      [n for n, it in m.items() if it.tx_route and not any(("RTS" in o or "DTR" in o) for o in it.options)], [])
    C(f"{pid} no tx_route item is readonly", [n for n, it in m.items() if it.tx_route and it.readonly], [])
C("ic728 has no flagged items",
  [it.num for it in (profiles.PROFILES["ic728"].menu or []) if it.tx_tot or it.tx_route], [])
C("MenuItem defaults: tx_tot/tx_route False",
  (profiles.MenuItem(1, "x", "g").tx_tot, profiles.MenuItem(1, "x", "g").tx_route), (False, False))

# ---- menu_item() accessors (no connect: the index is what connect() builds) ------------------
r = Radio()
r._menu_index = {it.num: it for it in profiles.PROFILES["ic9700"].menu}
C("Radio.menu_item(41) is the TOT item", r.menu_item(41).tx_tot, True)
C("Radio.menu_item('41') accepts the str form", r.menu_item("41") is r.menu_item(41), True)
C("Radio.menu_item(99999) -> None", r.menu_item(99999), None)
y = YaesuRadio()
y._menu_index = {it.num: it for it in profiles.PROFILES["ft991a"].menu}
C("YaesuRadio.menu_item(36) is TX TOT", (y.menu_item(36).name, y.menu_item(36).tx_tot), ("TX TOT", True))
C("YaesuRadio.menu_item(110).tx_route", y.menu_item(110).tx_route, True)
C("menu_item before connect -> None", Radio().menu_item(41), None)

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
