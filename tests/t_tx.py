"""`tx` group (step A4): the WS policy table, the TX arbiter, the audio router and the connect rules
on a real server process.

SAFETY. Every server is harness.Server: run.py --sim-only --no-autoconnect on 127.0.0.1:<free port>,
scratch config. Only the built-in SIMULATOR is ever connected. Every TX-capable command (ptt,
cw_tx, tune_atu, rx_func vox, mic, binary audio) goes through harness.WS, which runs
assert_sim(client) before EACH send: /api/status must say sim_only:true and transport_kind "sim",
otherwise the whole run aborts. No browser is involved.

Users: ADMIN (scratch, harness.ready_admin), operators OP1/OP2 and viewers V1.. made in the scratch
store (harness.mk_users). In-process parts of the A4 table live in backend/*_test.py (unit group):
T30 policy_test, T41/T46/T61/T63/T68 txarb_test, T43/T72 audio_router_test, T65 hub_test.
"""
from __future__ import annotations

import json
import time

import harness
from harness import check, eq, test

SIM = {"transport": "sim", "radio": "ic9700"}
DENY_GAP = 0.6           # the server sends at most 2 denial/busy frames per second per connection


def _err(r):
    j = r.json()
    return j.get("error") if isinstance(j, dict) else None


def setup(ctx, *, users=(("OP1", "operator"), ("OP2", "operator"), ("V1", "viewer")), radio="ic9700",
          extra_env=None, connect=True):
    srv = ctx.server(extra_env=extra_env) if extra_env else ctx.server()
    admin = harness.ready_admin(srv)
    pws = harness.mk_users(srv.tmp, list(users)) if users else {}
    cl = {"ADMIN": admin}
    for name, _ in users:
        c = srv.client()
        r = harness.login(c, name, pws[name])
        check(r.status == 200, f"login {name}: {r!r}")
        cl[name] = c
    if connect:
        r = admin.post("/api/connect", {"transport": "sim", "radio": radio})
        eq(r.status, 200, f"ADMIN sim connect {r!r}")
        harness.assert_sim(admin)
    return srv, cl


def open_ws(client):
    ws = client.ws()
    hello = ws.recv_json(type="hello")
    return ws, hello


def drain(ws, secs: float, type=None) -> list:
    out = []
    end = time.monotonic() + secs
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return out
        try:
            m = ws.recv_json(timeout=left)
        except TimeoutError:
            return out
        if type is None or m.get("type") == type:
            out.append(m)


def radio_state(client) -> dict:
    """The current radio state: the first state frame of a fresh socket."""
    with client.ws() as ws:
        return ws.recv_json(type="state")


def tx_owner(client):
    return client.get("/api/status").json()["tx"]["owner"]


def wait_until(fn, timeout=5.0, step=0.1):
    end = time.monotonic() + timeout
    while True:
        v = fn()
        if v:
            return v
        if time.monotonic() >= end:
            return v
        time.sleep(step)


def key(ws, seq=1):
    """ptt:true (through assert_sim) and wait for the tx frame answering it."""
    ws.send({"action": "ptt", "tx": True, "seq": seq})
    return ws.wait_for(lambda m: m.get("type") in ("tx", "busy", "denied") and
                       (m.get("req") or {}).get("seq") == seq, timeout=5)


def audit_wait(tmp, pred, name="audit-tx.log", timeout=4.0):
    return wait_until(lambda: [r for r in harness.audit_records(tmp, name) if pred(r)], timeout=timeout)


# -- T42: the interlock refuses a non-simulator transport ------------------------------------------------
@test("tx")
def t42_assert_sim_refuses_serial(ctx):
    class Stub:
        port = 8790

        def get(self, path):
            return type("R", (), {"status": 200, "json": lambda s: {"sim_only": True, "transport_kind": "serial"}})()
    try:
        harness.assert_sim(Stub())
    except harness.Refusing as e:
        check(str(e).startswith("REFUSING: non-simulator transport"), str(e))
    else:
        raise harness.TestFailure("assert_sim accepted transport_kind 'serial'")


# -- T14 / T73: viewers -----------------------------------------------------------------------------------
@test("tx")
def t14_t73_viewer_cannot_operate_but_reads_menus(ctx):
    srv, cl = setup(ctx, users=(("V1", "viewer"),))
    v = cl["V1"]
    f0 = radio_state(v)["freq"]
    with v.ws() as ws:
        eq(ws.recv_json(type="hello")["role"], "viewer")
        ws.send({"action": "set_freq", "hz": 145123000})
        d = ws.wait_for(lambda m: m.get("type") == "denied", timeout=5)
        eq((d["action"], d["reason"]), ("set_freq", "role"), "T14 viewer set_freq")
        time.sleep(DENY_GAP)
        ws.send({"action": "ptt", "tx": True})
        d = ws.wait_for(lambda m: m.get("type") == "denied", timeout=5)
        eq((d["action"], d["reason"]), ("ptt", "role"), "T14 viewer ptt")
        time.sleep(DENY_GAP)
        ws.send({"action": "menu_write", "num": 42, "value": "ON"})
        d = ws.wait_for(lambda m: m.get("type") == "denied", timeout=5)
        eq((d["action"], d["reason"]), ("menu_write", "role"), "T73 viewer menu_write")
        time.sleep(DENY_GAP)
        ws.send({"action": "rx_audio", "on": True})
        ws.send_bytes(b"\x00\x01" * 320)                       # binary: dropped silently
        ws.send({"action": "menu_read", "num": 41})            # F23: served
        ws.send({"action": "menu_read_group", "group": "CW Keyer"})
        frames = drain(ws, 2.0)
        eq([f for f in frames if f.get("type") in ("denied", "busy", "error")], [],
           "T14 rx_audio / binary / menu reads draw no denial")
        menus = {}
        for f in frames:
            if f.get("type") == "menu":
                menus.update(f["values"])
        check("41" in menus, f"T14/F23 viewer menu_read served: {menus}")
        check(len([k for k in menus if k != "41"]) >= 5, f"T73 viewer menu_read_group served: {menus}")
    st = radio_state(v)
    eq((st["freq"], st["ptt"]), (f0, False), "T14 state unchanged")


# -- T15: connect permissions -----------------------------------------------------------------------------
@test("tx")
def t15_connect_permissions(ctx):
    srv, cl = setup(ctx)
    r = cl["V1"].post("/api/connect", SIM)
    eq(r.status, 403, f"T15 viewer connect {r!r}")
    eq(cl["ADMIN"].post("/api/disconnect", {}).status, 200)
    r = cl["OP1"].post("/api/connect", SIM)
    eq(r.status, 200, f"T15 operator reconnects the current radio on its pin {r!r}")
    r = cl["OP1"].post("/api/connect", SIM)
    eq((r.status, _err(r)), (409, "already_connected"), f"T15 operator again while connected {r!r}")
    st = cl["OP1"].get("/api/status").json()
    eq((st["connected"], st["transport_kind"], st["pinned_transport"]), (True, "sim", {"transport": "sim"}))
    check("pinned_transport" not in cl["V1"].get("/api/status").json(), "viewers see no transport pin")
    eq(cl["OP1"].get("/api/ports").json().keys() == {"ports"}, True, "/api/ports returns only {ports}")
    eq(cl["V1"].post("/api/tx_stop", {}).status, 403, "viewer HTTP STOP")


# -- T16: owner, busy, F18, admin X exemption, STOP ----------------------------------------------------------
@test("tx")
def t16_owner_busy_and_stop(ctx):
    srv, cl = setup(ctx)
    w1, h1 = open_ws(cl["OP1"])
    w2, h2 = open_ws(cl["OP2"])
    wa, _ = open_ws(cl["ADMIN"])
    hb = None
    try:
        f = key(w1, seq=1)
        hb = harness.Heartbeat(w1)
        eq((f["type"], f["owner"]["user"], f["owner"]["conn"], f["owner"]["claims"]), ("tx", "OP1", h1["conn"], ["ptt"]))
        eq(radio_state(cl["V1"])["ptt"], True, "T16 radio keyed")
        eq(tx_owner(cl["OP2"])["user"], "OP1", "/api/status tx owner")
        b = key(w2, seq=9)
        eq((b["type"], b["owner"]), ("busy", "OP1"), "T16 OP2 ptt true -> busy")
        time.sleep(DENY_GAP)
        w2.send({"action": "ptt", "tx": False, "auto": True})
        eq([m for m in drain(w2, 0.7) if m.get("type") in ("busy", "denied")], [], "auto release by a non-owner: ignored")
        eq((radio_state(cl["V1"])["ptt"], tx_owner(cl["OP2"])["user"]), (True, "OP1"), "T16 still keyed after OP2 auto")
        w2.send({"action": "ptt", "tx": False})
        b = w2.wait_for(lambda m: m.get("type") == "busy", timeout=5)
        eq((b["action"], b["owner"]), ("ptt", "OP1"), "T16 OP2 explicit ptt false -> busy (F18)")
        eq(radio_state(cl["V1"])["ptt"], True, "T16 still keyed after OP2 explicit ptt false")
        time.sleep(DENY_GAP)
        w2.send({"action": "set_freq", "hz": 145300000})
        b = w2.wait_for(lambda m: m.get("type") == "busy", timeout=5)
        eq(b["action"], "set_freq", "T16 OP2 set_freq -> busy")
        wa.send({"action": "set_freq", "hz": 145310000})
        wa.wait_for(lambda m: m.get("type") == "state" and m.get("freq") == 145310000, timeout=5)
        w2.send({"action": "tx_stop"})
        rv = w1.wait_for(lambda m: m.get("type") == "tx_revoked", timeout=5)
        eq((rv["reason"], rv["by"]), ("stopped", "OP2"), "T16 OP1 gets tx_revoked{by:OP2}")
        eq((radio_state(cl["V1"])["ptt"], tx_owner(cl["V1"])), (False, None), "T16 unkeyed, TX free")
        recs = audit_wait(srv.tmp, lambda r: r.get("ev") == "tx_stop_forced")
        eq([(r["by"], r["owner"]) for r in recs], [("OP2", "OP1")], "T16 audit tx_stop_forced")
        ends = audit_wait(srv.tmp, lambda r: r.get("ev") == "tx_end")
        e = ends[-1]
        eq((e["user"], e["reason"], e["by"], e["kind"], e["radio"]), ("OP1", "stopped", "OP2", "ptt", "ic9700"))
        st = [r for r in harness.audit_records(srv.tmp) if r.get("ev") == "tx_start"][-1]
        check(all(k in st for k in ("user", "freq", "mode", "conn", "ip", "radio")), f"T28 tx_start fields {st}")
        check(all(k in e for k in ("user", "freq", "mode", "dur_s", "reason", "start", "freq_end")), f"T28 tx_end fields {e}")
        eq([r for r in harness.audit_records(srv.tmp, "audit.log") if r.get("ev") in ("tx_start", "tx_end")], [],
           "TX records never go to the security log (I21)")
    finally:
        if hb:
            hb.stop()
        for w in (w1, w2, wa):
            w.close()


# -- T17 / T18: heartbeat --------------------------------------------------------------------------------------
@test("tx")
def t17_silent_owner_released_after_6s(ctx):
    srv, cl = setup(ctx)
    with cl["OP1"].ws() as w1:
        w1.recv_json(type="hello")
        eq(key(w1)["owner"]["user"], "OP1")
        eq(radio_state(cl["OP2"])["ptt"], True)
        rv = w1.wait_for(lambda m: m.get("type") == "tx_revoked", timeout=9)
        eq(rv["reason"], "heartbeat", "T17 tx_revoked reason")
    eq((radio_state(cl["OP2"])["ptt"], tx_owner(cl["OP2"])), (False, None), "T17 unkeyed, owner null")
    ends = audit_wait(srv.tmp, lambda r: r.get("ev") == "tx_end")
    eq(ends[-1]["reason"], "heartbeat", "T17 audit reason heartbeat")
    check(audit_wait(srv.tmp, lambda r: r.get("ev") == "tx_hb_timeout"), "tx_hb_timeout audited")


@test("tx")
def t18_heartbeats_keep_the_owner_keyed(ctx):
    srv, cl = setup(ctx)
    with cl["OP1"].ws() as w1:
        w1.recv_json(type="hello")
        key(w1)
        hb = harness.Heartbeat(w1, interval=2.0)
        try:
            time.sleep(8.0)
            eq((radio_state(cl["OP2"])["ptt"], (tx_owner(cl["OP2"]) or {}).get("user")), (True, "OP1"),
               "T18 still keyed after 8 s of tx_hb every 2 s")
        finally:
            hb.stop()
        w1.send({"action": "ptt", "tx": False})
        w1.wait_for(lambda m: m.get("type") == "tx" and m.get("owner") is None, timeout=5)
    eq(radio_state(cl["OP2"])["ptt"], False)


# -- T19 / T20: drops -----------------------------------------------------------------------------------------------
@test("tx")
def t19_t20_only_the_owners_drop_unkeys(ctx):
    srv, cl = setup(ctx)
    w1, _ = open_ws(cl["OP1"])
    hb = None
    try:
        key(w1)
        hb = harness.Heartbeat(w1)
        with cl["OP2"].ws() as w2:                           # T20: another client connects and leaves
            w2.recv_json(type="hello")
        time.sleep(0.5)
        eq((radio_state(cl["V1"])["ptt"], tx_owner(cl["V1"])["user"]), (True, "OP1"), "T20 still keyed")
        hb.stop()
        hb = None
        w1.abort()                                           # the owner's socket drops (no handshake)
        t0 = time.monotonic()
        got = wait_until(lambda: radio_state(cl["V1"])["ptt"] is False, timeout=2.0, step=0.05)
        dt = time.monotonic() - t0
        check(got and dt <= 1.0, f"T19 unkeyed {dt:.2f}s after the owner's socket dropped")
        eq(tx_owner(cl["V1"]), None)
        ends = audit_wait(srv.tmp, lambda r: r.get("ev") == "tx_end")
        eq(ends[-1]["reason"], "disconnect")
        # a graceful close of a reading client unkeys just as fast
        w1b, _ = open_ws(cl["OP1"])
        key(w1b, seq=2)
        drain(w1b, 0.2)
        t0 = time.monotonic()
        w1b.close()
        got = wait_until(lambda: radio_state(cl["V1"])["ptt"] is False, timeout=2.0, step=0.05)
        dt = time.monotonic() - t0
        check(got and dt <= 1.5, f"T19 unkeyed {dt:.2f}s after a graceful close")
        eq(audit_wait(srv.tmp, lambda r: r.get("ev") == "tx_end")[-1]["reason"], "disconnect")
    finally:
        if hb:
            hb.stop()
        w1.close()


# -- T23: TOT=OFF needs admin + confirm; critical writes audited ---------------------------------------------------
@test("tx")
def t23_tot_off_and_critical_writes(ctx):
    srv, cl = setup(ctx, users=(("OP1", "operator"),))
    with cl["OP1"].ws() as w1:
        w1.recv_json(type="hello")
        w1.send({"action": "menu_write", "num": 41, "value": "3 min"})
        d = w1.wait_for(lambda m: m.get("type") == "denied", timeout=5)
        eq(d["reason"], "critical_admin", "T23 operator critical item")
    with cl["ADMIN"].ws() as wa:
        wa.recv_json(type="hello")
        for val, want in ((0, "tot_off_confirm"), ("OFF", "tot_off_confirm"), ("00", "tot_off_confirm"),
                          (False, "invalid"), (0.0, "invalid"), (None, "invalid"), ("off", "invalid")):
            wa.send({"action": "menu_write", "num": 41, "value": val})
            d = wa.wait_for(lambda m: m.get("type") == "denied", timeout=5)
            eq((d["action"], d["reason"]), ("menu_write", want), f"T23 ADMIN TOT {val!r}")
            time.sleep(DENY_GAP)
        wa.send({"action": "menu_write", "num": 41, "value": "OFF", "confirm": "tot_off"})
        m = wa.wait_for(lambda m: m.get("type") == "menu" and "41" in m.get("values", {}), timeout=5)
        eq(m["values"]["41"], "OFF", "T23 accepted with confirm (the radio confirms OFF)")
    tot = audit_wait(srv.tmp, lambda r: r.get("ev") == "tot_off")
    crit = audit_wait(srv.tmp, lambda r: r.get("ev") == "menu_critical")
    eq([(r["user"], r["num"]) for r in tot], [("ADMIN", 41)], "T23 audit tot_off in audit-tx.log")
    eq([(r["user"], r["num"], r["new"]) for r in crit], [("ADMIN", 41, "OFF")], "T23 audit menu_critical")


# -- T44: a rate-limited flood never blocks the unkey ------------------------------------------------------------------
@test("tx")
def t44_flood_then_ptt_false_unkeys(ctx):
    srv, cl = setup(ctx, users=(("OP1", "operator"),))
    with cl["OP1"].ws() as w1:
        w1.recv_json(type="hello")
        key(w1)
        hb = harness.Heartbeat(w1)
        try:
            for i in range(200):
                w1.send({"action": "set_span", "span": [2500, 5000, 10000, 25000][i % 4]})
            d = w1.wait_for(lambda m: m.get("type") == "denied" and m.get("reason") == "rate_limited", timeout=5)
            eq(d["action"], "set_span", "T44 bucket exhausted")
            w1.send({"action": "ptt", "tx": False})
            w1.wait_for(lambda m: m.get("type") == "tx" and m.get("owner") is None, timeout=5)
        finally:
            hb.stop()
    eq(radio_state(cl["ADMIN"])["ptt"], False, "T44 unkeyed despite the exhausted bucket (I4)")


# -- T47 / T62: CW claim ------------------------------------------------------------------------------------------------
@test("tx")
def t47_t62_cw_claim(ctx):
    srv, cl = setup(ctx)
    w1, _ = open_ws(cl["OP1"])
    w2, _ = open_ws(cl["OP2"])
    hb = None
    try:
        w1.send({"action": "set_mode", "mode": "CW"})
        w1.wait_for(lambda m: m.get("type") == "state" and m.get("mode_name") == "CW", timeout=5)
        w1.send({"action": "cw_tx", "text": "~~~", "wpm": 20, "seq": 1})
        d = w1.wait_for(lambda m: m.get("type") == "denied", timeout=5)
        eq(d["reason"], "invalid", "T62 text empty after the Icom filter")
        eq(tx_owner(cl["OP2"]), None, "T62 no phantom owner")
        time.sleep(DENY_GAP)
        w1.send({"action": "cw_tx", "text": "CQ", "wpm": 20, "seq": 2})
        f = w1.wait_for(lambda m: m.get("type") == "tx" and (m.get("req") or {}).get("seq") == 2, timeout=5)
        hb = harness.Heartbeat(w1)
        eq((f["owner"]["user"], f["owner"]["claims"]), ("OP1", ["cw"]), "T47 cw claim")
        w2.send({"action": "set_mode", "mode": "USB"})
        b = w2.wait_for(lambda m: m.get("type") == "busy", timeout=5)
        eq((b["action"], b["owner"]), ("set_mode", "OP1"), "T47 OP2 set_mode -> busy")
        t0 = time.monotonic()
        w1.wait_for(lambda m: m.get("type") == "tx" and m.get("owner") is None, timeout=10)
        check(time.monotonic() - t0 < 9, "T47 the cw claim drops when cw_tx clears")
        ends = audit_wait(srv.tmp, lambda r: r.get("ev") == "tx_end")
        eq((ends[-1]["kind"], ends[-1]["reason"]), ("cw", "done"))
        time.sleep(DENY_GAP)
        w2.send({"action": "set_mode", "mode": "USB"})
        w2.wait_for(lambda m: m.get("type") == "state" and m.get("mode_name") == "USB", timeout=5)
    finally:
        if hb:
            hb.stop()
        w1.close()
        w2.close()


# -- T48: the SET-menu queue ---------------------------------------------------------------------------------------------
@test("tx")
def t48_menu_queue(ctx):
    srv, cl = setup(ctx, users=(("OP1", "operator"), ("OP2", "operator"), ("V1", "viewer")))
    menu = next(p for p in cl["ADMIN"].get("/api/radios").json()["radios"] if p["id"] == "ic9700")["menu"]
    groups = {}
    for it in menu:
        groups.setdefault(it["group"], []).append(str(it["num"]))
    A, B = "CW Keyer", "Scan"
    obs, _ = open_ws(cl["ADMIN"])
    w1, _ = open_ws(cl["OP1"])
    w2, _ = open_ws(cl["OP2"])
    w3, _ = open_ws(cl["V1"])
    try:
        w1.send({"action": "menu_read_group", "group": A})
        w2.send({"action": "menu_read_group", "group": B})
        w3.send({"action": "menu_read_group", "group": A})
        seen = []
        end = time.monotonic() + 3.5
        while time.monotonic() < end:
            try:
                m = obs.recv_json(timeout=max(0.05, end - time.monotonic()))
            except TimeoutError:
                break
            if m.get("type") == "menu":
                seen.extend(m["values"].keys())
        a_seen = [n for n in seen if n in groups[A]]
        b_seen = [n for n in seen if n in groups[B]]
        eq(sorted(a_seen), sorted(groups[A]), "T48 group A completed, each item read ONCE")
        eq(sorted(b_seen), sorted(groups[B]), "T48 group B completed")
        last_a = max(i for i, n in enumerate(seen) if n in groups[A])
        first_b = min(i for i, n in enumerate(seen) if n in groups[B])
        check(last_a < first_b, f"T48 in order (A then B): {seen}")
        for x in (w1, w2, w3):
            eq([m for m in drain(x, 0.2) if m.get("type") in ("busy", "denied")], [], "no busy for coalesced requests")
        others = [g for g in groups if g not in (A, B)]
        check(len(others) >= 5, f"need 5 more groups: {others}")
        for g in others[:5]:
            w1.send({"action": "menu_read_group", "group": g})
        b = w1.wait_for(lambda m: m.get("type") == "busy", timeout=5)
        eq((b["action"], b.get("retry_after")), ("menu_read_group", 2), "T48 a 5th queued group from one conn -> busy")
    finally:
        for x in (obs, w1, w2, w3):
            x.close()


# -- T60: strict argument types -------------------------------------------------------------------------------------------
@test("tx")
def t60_strict_types_never_key(ctx):
    srv, cl = setup(ctx, users=(("OP1", "operator"),))
    with cl["OP1"].ws() as w1:
        w1.recv_json(type="hello")
        for bad in (1, "true", "false", 0, None, []):
            w1.send({"action": "ptt", "tx": bad})
            d = w1.wait_for(lambda m: m.get("type") == "denied", timeout=5)
            eq((d["action"], d["reason"]), ("ptt", "invalid"), f"T60 ptt tx={bad!r}")
            time.sleep(DENY_GAP)
        w1.send({"action": "rx_func", "name": "vox", "on": "off"})
        d = w1.wait_for(lambda m: m.get("type") == "denied", timeout=5)
        eq((d["action"], d["reason"]), ("rx_func", "invalid"), "T60 vox on:'off'")
        st = radio_state(cl["ADMIN"])
        eq((st["ptt"], bool(st["vox"]), tx_owner(cl["ADMIN"])), (False, False, None), "T60 never keyed")
        time.sleep(DENY_GAP)
        key(w1)
        hb = harness.Heartbeat(w1)
        try:
            time.sleep(DENY_GAP)
            w1.send({"action": "ptt", "tx": "false"})
            d = w1.wait_for(lambda m: m.get("type") == "denied", timeout=5)
            eq(d["reason"], "invalid", "T60 owner ptt tx:'false'")
            time.sleep(0.5)
            eq((radio_state(cl["ADMIN"])["ptt"], tx_owner(cl["ADMIN"])["user"]), (True, "OP1"),
               "T60 still keyed: no re-key, no release")
            starts = [r for r in harness.audit_records(srv.tmp) if r.get("ev") == "tx_start"]
            eq(len(starts), 1, "one session only")
        finally:
            hb.stop()
        w1.send({"action": "ptt", "tx": False})
        w1.wait_for(lambda m: m.get("type") == "tx" and m.get("owner") is None, timeout=5)


# -- T64: viewer socket reservation + HTTP STOP ---------------------------------------------------------------------------
@test("tx")
def t64_viewer_cap_and_http_stop(ctx):
    users = (("OP1", "operator"), ("OP2", "operator"), ("V1", "viewer"), ("V2", "viewer"), ("V3", "viewer"), ("V4", "viewer"))
    srv, cl = setup(ctx, users=users)
    socks = []
    try:
        for v in ("V1", "V2", "V3", "V4"):
            for _ in range(6):
                ws, h = open_ws(cl[v])
                socks.append(ws)
        eq(len(socks), 24)
        with cl["V1"].ws() as extra:
            eq(extra.wait_closed(), (4429, "too_many_connections"), "T64 the 25th viewer socket")
        w1, h1 = open_ws(cl["OP1"])
        socks.append(w1)
        eq(h1["role"], "operator", "T64 an operator socket still opens")
        key(w1)
        hb = harness.Heartbeat(w1)
        try:
            r = cl["OP2"].post("/api/tx_stop", None)                  # bodiless, like a STOP button
            eq((r.status, r.json()), (200, {"ok": True, "stopped": True}), f"T64 HTTP STOP {r!r}")
            rv = w1.wait_for(lambda m: m.get("type") == "tx_revoked", timeout=5)
            eq(rv["by"], "OP2")
        finally:
            hb.stop()
        eq((radio_state(cl["OP2"])["ptt"], tx_owner(cl["OP2"])), (False, None), "T64 unkeyed")
        r = cl["OP2"].post("/api/tx_stop", {}, origin="http://evil.example")
        eq(r.status, 403, "HTTP STOP is Origin-checked")
        r = cl["OP2"].post("/api/tx_stop", {})
        eq(r.json(), {"ok": True, "stopped": False}, "HTTP STOP with nobody transmitting")
    finally:
        for s in socks:
            s.close()


# -- T65: control frames survive a saturated data queue; heartbeats from claim send -------------------------------------
@test("tx")
def t65_tx_frame_delivered_while_flooded(ctx):
    srv, cl = setup(ctx, users=(("OP1", "operator"),))
    w1, h1 = open_ws(cl["OP1"])
    try:
        w1.send({"action": "ptt", "tx": True, "seq": 77})          # assert_sim first (harness)
        hb = harness.Heartbeat(w1, interval=2.0)                  # heartbeats from the claim send
        try:
            time.sleep(4.0)                                       # not reading: scope frames pile up
            f = w1.wait_for(lambda m: m.get("type") == "tx" and (m.get("req") or {}).get("seq") == 77, timeout=10)
            eq((f["owner"]["user"], f["owner"]["conn"]), ("OP1", h1["conn"]), "T65 the tx frame naming OP1 arrives")
            time.sleep(4.5)
            eq((radio_state(cl["ADMIN"])["ptt"], tx_owner(cl["ADMIN"])["user"]), (True, "OP1"),
               "T65 heartbeats started at claim send keep it keyed for 8 s")
        finally:
            hb.stop()
        w1.send({"action": "ptt", "tx": False})
        w1.wait_for(lambda m: m.get("type") == "tx" and m.get("owner") is None, timeout=5)
    finally:
        w1.close()


# -- T66: continuous setters are coalesced, never rate_limited ---------------------------------------------------------------
@test("tx")
def t66_continuous_setters(ctx):
    srv, cl = setup(ctx, users=(("OP1", "operator"),))
    f0 = radio_state(cl["OP1"])["freq"]
    with cl["OP1"].ws() as w1:
        w1.recv_json(type="hello")
        vals = [(i * 7) % 256 for i in range(300)]
        for v in vals:
            w1.send({"action": "set_level", "target": "af", "value": v})
        for _ in range(300):
            w1.send({"action": "tune", "delta": 10})
        frames = drain(w1, 1.5)
        eq([f for f in frames if f.get("type") == "denied"], [], "T66 no rate_limited (or any denial)")
    time.sleep(0.5)
    st = radio_state(cl["ADMIN"])
    eq(st["af"], vals[-1], "T66 the radio's final AF equals the last value")
    eq(st["freq"], f0 + 3000, "T66 300 x tune +10 -> net delta 3000")


# -- T67: denial floods never push TX records out --------------------------------------------------------------------------
@test("tx")
def t67_audit_isolation(ctx):
    srv, cl = setup(ctx, extra_env={"TELERIG_TEST_AUDIT_WINDOW": "1"})
    w1, _ = open_ws(cl["OP1"])
    w2, _ = open_ws(cl["OP2"])
    hb = None
    try:
        key(w1)
        hb = harness.Heartbeat(w1)
        t0 = time.monotonic()
        for i in range(5000):
            w2.send({"action": "ptt", "tx": True})               # assert_sim before EACH (harness)
        ctx.log(f"5000 ptt:true sent in {time.monotonic() - t0:.1f}s")

        def tallies():
            sec = harness.audit_records(srv.tmp, "audit.log")
            d = [r for r in sec if r.get("ev") == "tx_denied"]
            q = [r for r in sec if r.get("ev") == "rate_limited"]
            return d, q, sum(r.get("count", 1) + r.get("suppressed", 0) for r in d + q)
        wait_until(lambda: tallies()[2] >= 5000, timeout=20, step=0.5)   # windows close, the cap drains
        denied, rl, _ = tallies()
        check(denied, "T67 coalesced tx_denied entries exist")
        check(all(isinstance(r.get("count"), int) and r["count"] >= 1 for r in denied + rl), "T67 entries carry counts")
        check(len(denied) <= 100 and len(denied + rl) <= 200, f"T67 coalesced: {len(denied)} tx_denied, {len(rl)} rate_limited lines")
        total = sum(r["count"] + r.get("suppressed", 0) for r in denied + rl)
        eq(total, 5000, "T67 the coalesced counts account for every refused ptt:true")
        txs = harness.audit_records(srv.tmp)
        eq([r["user"] for r in txs if r.get("ev") == "tx_start"], ["OP1"], "T67 audit-tx.log still holds OP1's tx_start")
        eq([r for r in txs if r.get("ev") in ("tx_denied", "rate_limited", "denied")], [], "no denials in audit-tx.log")
        eq(tx_owner(cl["OP1"])["user"], "OP1", "OP1 still owns TX")
    finally:
        if hb:
            hb.stop()
        w1.close()
        w2.close()


# -- T69: locked mode at runtime releases app-keyed TX -------------------------------------------------------------------------
@test("tx")
def t69_locked_mode_releases_tx(ctx):
    srv, cl = setup(ctx, users=(("OP1", "operator"), ("OP2", "operator")))
    w1, _ = open_ws(cl["OP1"])
    w2, _ = open_ws(cl["OP2"])
    hb = None
    authp = srv.tmp / "auth.json"
    good = authp.read_bytes()
    try:
        key(w1)
        hb = harness.Heartbeat(w1)
        authp.write_bytes(b"{ this is not json")
        eq(w1.wait_closed(timeout=8), (4403, "server_locked"), "T69 the owner's socket closed 4403")
        eq(w2.wait_closed(timeout=8), (4403, "server_locked"), "T69 every socket closed 4403")
        ends = audit_wait(srv.tmp, lambda r: r.get("ev") == "tx_end", timeout=5)
        eq((ends[-1]["user"], ends[-1]["reason"]), ("OP1", "server_locked"), "T69 app-keyed TX released on lock")
        eq(cl["OP1"].get("/api/status").status, 503)
    finally:
        if hb:
            hb.stop()
        authp.write_bytes(good)
        w1.close()
        w2.close()
    ok = wait_until(lambda: cl["OP1"].get("/api/status").status == 200, timeout=8)
    check(ok, "unlocks after the file is fixed")
    eq((radio_state(cl["OP1"])["ptt"], tx_owner(cl["OP1"])), (False, None), "T69 radio unkeyed, TX free")


# -- T70: connect pins (F20) --------------------------------------------------------------------------------------------------
@test("tx")
def t70_connect_pins(ctx):
    srv, cl = setup(ctx, users=(("OP1", "operator"),))
    cfg = json.loads((srv.tmp / "config.json").read_text(encoding="utf-8"))
    eq((cfg.get("last_radio"), cfg.get("radio_transports")), ("ic9700", {"ic9700": {"transport": "sim"}}),
       "T70 ADMIN connect pins the radio and transport")
    check(audit_wait(srv.tmp, lambda r: r.get("ev") == "radio_transport_pinned"), "pin audited")
    check(audit_wait(srv.tmp, lambda r: r.get("ev") == "radio_connect"), "connect audited")
    r = cl["OP1"].post("/api/connect", {"transport": "sim", "radio": "ft991a"})
    eq((r.status, _err(r), r.json().get("reason")), (403, "admin_required", "switch_radio"), f"T70 OP1 other radio {r!r}")
    eq(cl["ADMIN"].post("/api/disconnect", {}).status, 200)
    check(audit_wait(srv.tmp, lambda r: r.get("ev") == "radio_disconnect"), "disconnect audited")
    cfg["radio_transports"] = {"ic9700": {"transport": "serial", "port": "COM7", "baud": 115200}}
    (srv.tmp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    time.sleep(0.7)
    r = cl["OP1"].post("/api/connect", SIM)
    eq((r.status, r.json().get("reason")), (403, "transport_not_pinned"), f"T70 OP1 off the pinned transport {r!r}")
    r = cl["ADMIN"].post("/api/connect", {"transport": "serial", "radio": "ic9700", "port": "COM7"})
    eq((r.status, _err(r)), (403, "sim_only"), "serial is refused by --sim-only before any pin logic")
    r = cl["ADMIN"].post("/api/connect", {"transport": "sim", "radio": "ic9700", "auto": "false"})
    eq((r.status, _err(r), r.json().get("field")), (400, "invalid", "auto"), "strict types on connect")
    r = cl["ADMIN"].post("/api/connect", SIM)
    eq(r.status, 200, "ADMIN connects (re-pins sim)")
    eq(json.loads((srv.tmp / "config.json").read_text(encoding="utf-8"))["radio_transports"]["ic9700"],
       {"transport": "sim"}, "re-pinned")


# -- T70b: disconnect rules ------------------------------------------------------------------------------------------------------
@test("tx")
def t70b_disconnect_confirm_and_busy(ctx):
    srv, cl = setup(ctx, users=(("OP1", "operator"), ("OP2", "operator")))
    w2, _ = open_ws(cl["OP2"])
    w1, _ = open_ws(cl["OP1"])
    hb = None
    try:
        r = cl["OP1"].post("/api/disconnect", None)
        eq((r.status, _err(r), r.json().get("online")), (409, "confirm_required", 1), "non-admin with others online")
        key(w2)
        hb = harness.Heartbeat(w2)
        r = cl["OP1"].post("/api/disconnect", {"confirm": True})
        eq((r.status, _err(r), r.json().get("owner")), (409, "busy", "OP2"), "busy while someone else transmits")
        r = cl["ADMIN"].post("/api/disconnect", {})
        eq((r.status, _err(r)), (409, "busy"), "admin needs force")
        r = cl["ADMIN"].post("/api/disconnect", {"force": True})
        eq(r.status, 200, "admin force")
        rv = w2.wait_for(lambda m: m.get("type") == "tx_revoked", timeout=5)
        eq((rv["reason"], rv["by"]), ("admin_switch", "ADMIN"))
        eq(radio_state(cl["OP1"])["connected"], False)
    finally:
        if hb:
            hb.stop()
        w1.close()
        w2.close()


# -- T71: PTT-routing menu writes ----------------------------------------------------------------------------------------------
@test("tx")
def t71_tx_route_write(ctx):
    srv, cl = setup(ctx, users=(("OP1", "operator"),), radio="ft991a")
    wa, _ = open_ws(cl["ADMIN"])
    w1, _ = open_ws(cl["OP1"])
    try:
        wa.send({"action": "menu_write", "num": 110, "value": "RTS"})
        d = wa.wait_for(lambda m: m.get("type") == "denied", timeout=5)
        eq(d["reason"], "tx_route_confirm", "T71 RTS without confirm")
        time.sleep(DENY_GAP)
        f = key(w1)                                   # the FT-991A sim reports TX0, so this claim is short
        eq(f["type"], "tx", f"OP1 keyed {f}")
        wa.send({"action": "menu_write", "num": 110, "value": "RTS", "confirm": "tx_route"})
        b = wa.wait_for(lambda m: m.get("type") in ("busy", "denied"), timeout=5)
        eq((b["type"], b.get("owner")), ("busy", "OP1"), "T71 with confirm while OP1 keyed -> busy")
        check(wait_until(lambda: tx_owner(cl["ADMIN"]) is None, timeout=8), "TX idle again")
        w1.send({"action": "ptt", "tx": False})
        time.sleep(DENY_GAP)
        wa.send({"action": "menu_write", "num": 110, "value": "RTS", "confirm": "tx_route"})
        m = wa.wait_for(lambda m: m.get("type") == "menu" and "110" in m.get("values", {}), timeout=5)
        eq(m["values"]["110"], "RTS", "T71 idle + confirm -> accepted")
        crit = audit_wait(srv.tmp, lambda r: r.get("ev") == "menu_critical")
        eq([(r["num"], r["new"], r.get("tx_route")) for r in crit], [(110, "RTS", "RTS")], "T71 audited menu_critical")
    finally:
        wa.close()
        w1.close()
