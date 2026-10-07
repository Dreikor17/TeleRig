"""Offline policy checks — run: python -m backend.policy_test

T30 (I2): every `action == "<name>"` literal in backend/server.py has a WS_POLICY row or is in
REMOVED, and the checker really detects a gap. Plus the TX-safety menu flags the policy relies on
(MenuItem.tx_tot / tx_route) and the radios' menu_item() accessors.
No file outside the repo is read or written; no radio, socket or server is opened.
"""
from pathlib import Path

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


# ---- T30: regex completeness -------------------------------------------------------------
SRC = (Path(__file__).resolve().parent / "server.py").read_text(encoding="utf-8")
lits = policy.action_literals(SRC)

C("T30 server.py has >= 57 action literals (regex still matches the dispatcher)", len(lits) >= 57, True)
C("T30 no completeness errors", policy.completeness_errors(SRC), [])
C("T30 every literal has a row or is REMOVED", sorted(lits - set(policy.WS_POLICY) - policy.REMOVED), [])
C("T30 host_rx/host_tx branches deleted from server.py", sorted(lits & policy.REMOVED), [])
C("T30 REMOVED = host_rx, host_tx", sorted(policy.REMOVED), ["host_rx", "host_tx"])
C("T30 no row is also REMOVED", sorted(set(policy.WS_POLICY) & policy.REMOVED), [])
C("T30 skeleton rows == the dispatcher's literals (57 actions)",
  (sorted(set(policy.WS_POLICY) ^ lits), len(policy.WS_POLICY)), ([], 57))
C("T30 59 actions covered in all (57 rows + 2 REMOVED)", len(set(policy.WS_POLICY) | policy.REMOVED), 59)
C("T30 NEW_ACTIONS all have rows", sorted(policy.NEW_ACTIONS - set(policy.WS_POLICY)), [])

gap = SRC + '\n        elif action == "bogus_action":\n            pass\n'
C("T30 a literal without a row is reported", any("bogus_action" in e for e in policy.completeness_errors(gap)), True)
C("T30 assert_complete raises on a gap", raises(lambda: policy.assert_complete(gap), RuntimeError), True)
C("T30 assert_complete passes on server.py", raises(lambda: policy.assert_complete(SRC), Exception), False)
C("T30 single-quoted literal is seen too", "zz_q" in policy.action_literals("if action == 'zz_q':"), True)

# ---- A2 skeleton semantics ------------------------------------------------------------------
cmd = {"action": "set_freq", "hz": 145000000}
v = policy.evaluate(policy.WS_POLICY["set_freq"], None, cmd)
C("A2 placeholder row passes the command through (allow-all)", (v.kind, v.cmd), ("plain", cmd))
C("A2 every row is a placeholder until A4", all(r.placeholder for r in policy.WS_POLICY.values()), True)
C("A2 unknown action has no row", policy.WS_POLICY.get("host_rx") is None and policy.WS_POLICY.get("nope") is None, True)
C("A2 non-placeholder rule is denied (never allow by default)",
  policy.evaluate(policy.Rule("x"), None, {"action": "x"}).kind, "deny")

# ---- fast-path classifier (§2.12): the only frames a kicked/locked connection still acts on ----
fast = [{"action": "ptt", "tx": False}, {"action": "cw_stop"}, {"action": "tx_hb"}, {"action": "tx_stop"},
        {"action": "rx_func", "name": "vox", "on": False}, {"action": "mic", "on": False},
        {"action": "ptt", "tx": False, "auto": True, "extra": 1}]
not_fast = [{"action": "ptt", "tx": True}, {"action": "ptt", "tx": 0}, {"action": "ptt", "tx": "false"},
            {"action": "ptt", "tx": None}, {"action": "ptt", "tx": []}, {"action": "ptt"},
            {"action": "cw_tx", "text": "CQ", "wpm": 20}, {"action": "tune_atu"},
            {"action": "rx_func", "name": "vox", "on": True}, {"action": "rx_func", "name": "vox", "on": 0},
            {"action": "rx_func", "name": "VOX", "on": False}, {"action": "rx_func", "name": ["vox"], "on": False},
            {"action": "rx_func", "name": "nb", "on": False}, {"action": "rx_func", "on": False},
            {"action": "mic", "on": True}, {"action": "mic", "on": 0}, {"action": "mic"},
            {"action": "set_freq", "hz": 145000000}, {"action": "PTT", "tx": False}, {"action": 5},
            {"tx": False}, [], None, "ptt"]
C("is_fast: the unkey/heartbeat frames", [c for c in fast if not policy.is_fast(c)], [])
C("is_fast: strict types, nothing that can start TX", [c for c in not_fast if policy.is_fast(c)], [])

# ---- TX-safety menu flags (§1 file map, §2.12 menu_write) -----------------------------------
def items(pid):
    return {it.num: it for it in profiles.PROFILES[pid].menu}


ft = items("ft991a")
C("FT-991A item 36 TX TOT is critical", ft[36].critical, True)
C("FT-991A item 36 TX TOT is tx_tot", ft[36].tx_tot, True)
for n in (47, 71, 76, 110):
    C(f"FT-991A item {n} PTT SELECT is tx_route", ft[n].tx_route, True)

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
