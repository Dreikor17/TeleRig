"""`admin` group (step A5): the admin API on a real server process.

Every server is harness.Server (run.py --sim-only --no-autoconnect on 127.0.0.1:<free port>, scratch
config and home). Users are created through the admin API (harness.api_mk_user). Only the simulator
is ever connected; the one test that needs a TX owner (T49) keys the SIMULATOR through harness.WS,
which runs assert_sim before every TX command.

TAILSCALE: no test here ever calls Serve ON or OFF directly on the host. The live check is only that
Serve ON with a forwarding header is refused (403 local_only) before any CLI call; the CLI paths
(ON/OFF failures, AR-22, T74) are covered in-process with a fake CLI in backend/admin_api_test.py.
"""
from __future__ import annotations

import json
import threading
import time

import harness
from harness import check, eq, test

SIM = {"transport": "sim", "radio": "ic9700"}


def _err(r):
    j = r.json()
    return j.get("error") if isinstance(j, dict) else None


def login(srv, user, pw, **kw):
    c = srv.client()
    r = harness.login(c, user, pw, **kw)
    check(r.status == 200, f"login {user}: {r!r}")
    return c


def audit_events(tmp, ev, timeout=4.0):
    end = time.monotonic() + timeout
    while True:
        out = [r for r in harness.audit_records(tmp, "audit.log") if r.get("ev") == ev]
        if out or time.monotonic() > end:
            return out
        time.sleep(0.1)


def wait_closed_code(ws, timeout=3.0):
    try:
        return ws.wait_closed(timeout=timeout)
    except TimeoutError:
        return None


# -- T13 / T39: the sudo window --------------------------------------------------------------------------
@test("admin")
def t13_sudo_window_and_reauth(ctx):
    srv = ctx.server(extra_env={"TELERIG_TEST_SUDO_TTL": "1"})
    admin = harness.ready_admin(srv)                 # the forced change opened a 1 s window
    time.sleep(2.0)
    r = admin.post("/api/admin/users", {"user": "OP1", "role": "operator"})
    eq((r.status, _err(r)), (403, "reauth_required"), f"T13 create after the window closed {r!r}")
    check(harness.read_auth(srv.tmp)["users"].get("OP1") is None, "OP1 was created anyway")
    r = admin.post("/api/admin/reauth", {"password": "wrong-password"})
    eq((r.status, _err(r)), (401, "invalid_credentials"), "reauth with a wrong password")
    r = admin.post("/api/admin/reauth", {"password": srv.admin_pw})
    eq((r.status, r.json().get("expires_in")), (200, 1), f"reauth {r!r}")
    r = admin.post("/api/admin/users", {"user": "OP1", "role": "operator"})
    eq(r.status, 200, f"T13 create right after reauth {r!r}")
    j = r.json()
    temp = j.get("temp_password")
    check(isinstance(temp, str) and len(temp) == 12, f"temp_password {j}")
    eq((j["user"], j["uid"]), ("OP1", harness.uid("OP1")), "response names the user and its uid")
    u = harness.read_auth(srv.tmp)["users"]["OP1"]
    eq((u["role"], u["must_change"], u["created_by"]), ("operator", True, "ADMIN"), "stored OP1")
    r = harness.login(srv.client(), "OP1", temp)
    eq((r.status, r.json().get("must_change")), (200, True), "T13 OP1 signs in with the temp password: must_change")
    check(temp not in json.dumps(harness.audit_records(srv.tmp, "audit.log")), "the temp password reached the audit log")
    # T39 (second half): a fresh PASSWORD login also opens only a 1 s window under the test TTL
    a2 = login(srv, "admin", srv.admin_pw)
    eq(a2.patch(f"/api/admin/users/{harness.uid('OP1')}", {"role": "viewer"}).status, 200, "T39 mutation right after login")
    time.sleep(2.0)
    r = a2.patch(f"/api/admin/users/{harness.uid('OP1')}", {"role": "operator"})
    eq((r.status, _err(r)), (403, "reauth_required"), "T39 after 2 s with TELERIG_TEST_SUDO_TTL=1")


@test("admin")
def t39_sudo_opens_at_password_login(ctx):
    srv = ctx.server()                                # default TTL (600 s)
    harness.ready_admin(srv)
    a = login(srv, "admin", srv.admin_pw)            # a plain password login, no reauth
    pw = harness.api_mk_user(a, "OP1", "operator")
    eq(harness.login(srv.client(), "OP1", pw).status, 200, "the user created right after login signs in")
    r = a.patch(f"/api/admin/users/{harness.uid('OP1')}", {"role": "viewer"})
    eq(r.status, 200, f"T39 admin mutation without reauth {r!r}")
    for path in ("/api/admin/users", "/api/admin/sessions", "/api/admin/lockouts", "/api/admin/allowed_hosts",
                 "/api/admin/audit"):
        eq(a.get(path).status, 200, f"GET {path}")


# -- T21: a role change ends the user's sessions ----------------------------------------------------------
@test("admin")
def t21_role_change_kicks_the_user(ctx):
    srv = ctx.server()
    admin = harness.ready_admin(srv)
    pw = harness.api_mk_user(admin, "OP1", "operator")
    op = login(srv, "OP1", pw)
    tok = harness.session_cookie(op)
    with op.ws() as ws:
        eq(ws.recv_json(type="hello")["role"], "operator")
        t0 = time.monotonic()
        r = admin.patch(f"/api/admin/users/{harness.uid('OP1')}", {"role": "viewer"})
        eq((r.status, r.json().get("changed")), (200, True), f"PATCH role {r!r}")
        eq(ws.wait_closed(timeout=3), (4401, "session_revoked"), "T21 OP1's socket")
        dt = time.monotonic() - t0
        check(dt < 1.0, f"T21 closed {dt:.2f}s after the role change (want < 1 s)")
    r = op.raw("GET", "/api/me", headers={"Cookie": f"tr_session_{srv.port}={tok}"}, cookies=False)
    eq(r.status, 401, "T21 the old cookie after the role change")
    eq(harness.read_auth(srv.tmp)["users"]["OP1"]["role"], "viewer")
    ev = audit_events(srv.tmp, "role_changed")
    eq([(e.get("user"), e.get("from"), e.get("to"), e.get("by")) for e in ev], [("OP1", "operator", "viewer", "ADMIN")],
       "role_changed audited")
    v = login(srv, "OP1", pw)
    eq(v.get("/api/me").json()["role"], "viewer", "OP1 signs in again as a viewer")
    eq(admin.patch(f"/api/admin/users/{harness.uid('OP1')}", {"role": "viewer"}).json().get("changed"), False,
       "the same role again changes nothing")
    for bad in ({"role": "root"}, {"role": True}, {}):
        r = admin.patch(f"/api/admin/users/{harness.uid('OP1')}", bad)
        eq((r.status, _err(r), r.json().get("field")), (400, "invalid", "role"), f"PATCH {bad}")
    eq(admin.patch(f"/api/admin/users/{harness.uid('NOBODY')}", {"role": "viewer"}).status, 404, "unknown user")
    eq(admin.patch("/api/admin/users/ZZ", {"role": "viewer"}).status, 404, "a uid that is not hex")


# -- T22: self rules, the last admin --------------------------------------------------------------------
@test("admin")
def t22_self_and_last_admin(ctx):
    srv = ctx.server()
    admin = harness.ready_admin(srv)
    me = harness.uid("ADMIN")
    r = admin.delete(f"/api/admin/users/{me}")
    eq((r.status, _err(r)), (409, "self_delete"), "T22 self-delete")
    r = admin.patch(f"/api/admin/users/{me}", {"role": "operator"})
    eq((r.status, _err(r)), (409, "self_demote"), "T22 self-demote")
    r = admin.post(f"/api/admin/users/{me}/reset_password", {})
    eq((r.status, _err(r)), (409, "self"), "an admin resets their own password -> use Change password")
    eq(harness.read_auth(srv.tmp)["users"]["ADMIN"]["role"], "admin", "ADMIN unchanged")
    # T22 last admin: two admins demote each other at the same moment. The guard is decided under the
    # store lock, so at least one admin always remains; the loser gets 409 last_admin (both passed
    # the Gate) or 401 (its own gen moved first). The deterministic race is in admin_api_test.
    pw2 = harness.api_mk_user(admin, "ADM2", "admin")
    seen = set()
    for i in range(20):
        if i >= 6 and (409, "last_admin") in seen:
            break
        a1, a2 = login(srv, "admin", srv.admin_pw), login(srv, "ADM2", pw2)
        res = {}
        bar = threading.Barrier(2)

        def go(key, client, target):
            bar.wait()
            res[key] = client.patch(f"/api/admin/users/{harness.uid(target)}", {"role": "operator"})
        th = [threading.Thread(target=go, args=("a1", a1, "ADM2")), threading.Thread(target=go, args=("a2", a2, "ADMIN"))]
        for t in th:
            t.start()
        for t in th:
            t.join(10)
        users = harness.read_auth(srv.tmp)["users"]
        admins = [n for n, u in users.items() if u["role"] == "admin"]
        check(len(admins) >= 1, f"round {i}: no admin left: {users}")
        outcomes = sorted((res[k].status, _err(res[k])) for k in res)
        for st_err in outcomes:
            check(st_err in ((200, None), (401, "auth_required"), (409, "last_admin")), f"round {i}: {outcomes}")
        seen.update(outcomes)
        survivor = login(srv, admins[0], srv.admin_pw if admins[0] == "ADMIN" else pw2)
        other = "ADM2" if admins[0] == "ADMIN" else "ADMIN"
        if users[other]["role"] != "admin":
            eq(survivor.patch(f"/api/admin/users/{harness.uid(other)}", {"role": "admin"}).status, 200, "re-promote")
    ctx.log(f"race outcomes seen: {sorted(seen)}")
    check((409, "last_admin") in seen, f"T22 last admin: no 409 last_admin in {i + 1} simultaneous rounds: {sorted(seen)}")
    a = login(srv, "admin", srv.admin_pw)
    eq(a.patch(f"/api/admin/users/{harness.uid('ADM2')}", {"role": "operator"}).status, 200,
       "demoting the other admin is fine while one remains")
    r = a.delete(f"/api/admin/users/{harness.uid('ADM2')}")
    eq(r.status, 200, f"deleting a non-admin {r!r}")


# -- reset password / sign out / unlock / delete ------------------------------------------------------------
@test("admin")
def admin_user_actions(ctx):
    srv = ctx.server()
    admin = harness.ready_admin(srv)
    pw = harness.api_mk_user(admin, "OP1", "operator")
    op = login(srv, "OP1", pw)
    u1 = harness.uid("OP1")
    # reset password: a server-generated temp, must_change, the user's sockets end
    with op.ws() as ws:
        ws.recv_json(type="hello")
        r = admin.post(f"/api/admin/users/{u1}/reset_password", {})
        eq(r.status, 200, f"reset_password {r!r}")
        temp = r.json().get("temp_password")
        check(isinstance(temp, str) and len(temp) == 12, f"temp {r.json()}")
        eq(ws.wait_closed(timeout=3), (4401, "session_revoked"), "OP1's socket after the reset")
    eq(op.get("/api/me").status, 401, "OP1's old cookie after the reset")
    eq(harness.login(srv.client(), "OP1", pw).status, 401, "the old password no longer works")
    r = harness.login(srv.client(), "OP1", temp)
    eq((r.status, r.json().get("must_change")), (200, True), "the temp password signs in with must_change")
    r = admin.post(f"/api/admin/users/{u1}/reset_password", {"password": "Typed#Pass-2026", "must_change": False})
    eq((r.status, "temp_password" in r.json()), (200, False), "an admin-typed password, no forced change")
    eq(harness.login(srv.client(), "OP1", "Typed#Pass-2026").json().get("must_change"), False, "typed password")
    r = admin.post(f"/api/admin/users/{u1}/reset_password", {"password": "short"})
    eq((r.status, r.json().get("field")), (400, "password"), "a 5-byte password")
    r = admin.post(f"/api/admin/users/{u1}/reset_password", {"must_change": "yes"})
    eq((r.status, r.json().get("field")), (400, "must_change"), "must_change must be a bool")
    # sign out everywhere (admin): gen bumped, sockets kicked and counted
    o1, o2 = login(srv, "OP1", "Typed#Pass-2026"), login(srv, "OP1", "Typed#Pass-2026")
    g0 = harness.read_auth(srv.tmp)["users"]["OP1"]["gen"]
    with o1.ws() as w1, o2.ws() as w2:
        w1.recv_json(type="hello")
        w2.recv_json(type="hello")
        r = admin.post(f"/api/admin/users/{u1}/signout", {})
        eq((r.status, r.json().get("kicked")), (200, 2), f"signout {r!r}")
        eq(w1.wait_closed(timeout=3)[0], 4401, "socket 1")
        eq(w2.wait_closed(timeout=3)[0], 4401, "socket 2")
    check(harness.read_auth(srv.tmp)["users"]["OP1"]["gen"] > g0, "gen bumped")
    eq((o1.get("/api/me").status, o2.get("/api/me").status), (401, 401), "both sessions ended")
    # unlock: OP1's K2 key via a loopback relay source (no K1), then the admin unlocks it
    relay = {"X-Forwarded-For": "127.0.0.9"}
    g = srv.client()
    for i in range(10):
        eq(harness.login(g, "OP1", f"wrong-{i}", headers=relay).status, 401)
    eq(harness.login(g, "OP1", "Typed#Pass-2026", headers=relay).status, 429, "OP1 is K2-limited from that source")
    eq(admin.post(f"/api/admin/users/{u1}/unlock", {}).status, 200, "unlock")
    eq(harness.login(g, "OP1", "Typed#Pass-2026", headers=relay).status, 200, "OP1 signs in after the unlock")
    eq(admin.post(f"/api/admin/users/{harness.uid('NOBODY')}/unlock", {}).status, 404, "unlock an unknown user")
    eq(sorted(admin.get("/api/admin/lockouts").json()), ["ips", "users"], "lockouts")
    # create: validation and duplicates
    for body, field in (({"user": "x"}, "user"), ({"user": "OP 2"}, "user"), ({"user": "OP2", "role": "god"}, "role"),
                        ({"user": "OP2", "password": "1234"}, "password"), ({"user": "OP2", "password": "op2"}, "password"),
                        ({"user": "OP2", "must_change": 1}, "must_change")):
        r = admin.post("/api/admin/users", body)
        eq((r.status, _err(r), r.json().get("field")), (400, "invalid", field), f"create {body}")
    r = admin.post("/api/admin/users", {"user": "op1"})
    eq((r.status, _err(r)), (409, "exists"), "a case-insensitive duplicate")
    r = admin.post("/api/admin/users", {"user": "V1"})
    eq((r.status, harness.read_auth(srv.tmp)["users"]["V1"]["role"]), (200, "viewer"), "the default role is viewer (F13)")
    # delete: the account, its sockets and its layout file
    lay = login(srv, "OP1", "Typed#Pass-2026")
    eq(lay.put("/api/me/layout", {"device": "phone", "data": {"column": 1, "items": [{"id": "vfo", "y": 0}]}}).status, 200)
    lf = srv.tmp / "layouts" / f"{u1}.json"
    check(lf.exists(), "OP1 has a layout file")
    with lay.ws() as ws:
        ws.recv_json(type="hello")
        eq(admin.delete(f"/api/admin/users/{u1}").status, 200, "delete OP1")
        eq(ws.wait_closed(timeout=3), (4401, "session_revoked"), "OP1's socket after the delete")
    check("OP1" not in harness.read_auth(srv.tmp)["users"], "OP1 still in auth.json")
    check(not lf.exists(), "OP1's layout file was not deleted")
    eq(admin.delete(f"/api/admin/users/{u1}").status, 404, "delete again")
    for ev in ("user_created", "pw_reset", "signout_all", "unlock", "user_deleted"):
        check(audit_events(srv.tmp, ev), f"{ev} not audited")
    rows = admin.get("/api/admin/audit", headers=None).json()["entries"]
    check(any(r.get("ev") == "user_deleted" for r in rows), "the audit endpoint shows user_deleted")


# -- T49: sessions -------------------------------------------------------------------------------------
@test("admin")
def t49_sessions_list_live_connections(ctx):
    srv = ctx.server()
    admin = harness.ready_admin(srv)
    pws = {n: harness.api_mk_user(admin, n, r) for n, r in (("OP1", "operator"), ("V1", "viewer"))}
    eq(admin.post("/api/connect", SIM).status, 200, "ADMIN connects the simulator")
    harness.assert_sim(admin)
    op, v = login(srv, "OP1", pws["OP1"]), login(srv, "V1", pws["V1"])
    eq((v.get("/api/admin/sessions").status, op.get("/api/admin/sessions").status), (403, 403),
       "T49 viewer and operator -> 403")
    with op.ws() as ows, v.ws() as vws:
        oc = ows.recv_json(type="hello")["conn"]
        vc = vws.recv_json(type="hello")["conn"]
        hb = harness.Heartbeat(ows, 1.0)
        try:
            ows.send({"action": "ptt", "tx": True, "seq": 1})        # harness.WS: assert_sim first
            ows.wait_for(lambda m: m.get("type") == "tx" and (m.get("owner") or {}).get("conn") == oc, timeout=5)
            s = admin.get("/api/admin/sessions").json()
            rows = {r["conn"]: r for r in s["sessions"]}
            check(oc in rows and vc in rows, f"T49 both sockets listed: {s}")
            eq((rows[oc]["user"], rows[oc]["role"], rows[oc]["tx_owner"], rows[oc]["ip"]), ("OP1", "operator", True, "127.0.0.1"),
               "T49 OP1 is the TX owner")
            eq((rows[vc]["user"], rows[vc]["tx_owner"], rows[vc]["rx_audio"], rows[vc]["mic"]), ("V1", False, False, False),
               "V1 row")
            check(isinstance(rows[oc]["since"], str) and rows[oc]["since"].endswith("Z"), "since is ISO-8601")
            eq((s["tx"]["owner"] or {}).get("user"), "OP1", "the TX snapshot")
            users = {u["user"]: u for u in admin.get("/api/admin/users").json()["users"]}
            eq((users["OP1"]["on_air"], users["OP1"]["online"], users["V1"]["on_air"]), (True, 1, False), "users: on_air / online")
        finally:
            hb.stop()
            ows.send({"action": "ptt", "tx": False})                # assert_sim first
        ows.wait_for(lambda m: m.get("type") == "tx" and m.get("owner") is None, timeout=5)
        s = admin.get("/api/admin/sessions").json()
        eq([r["tx_owner"] for r in s["sessions"] if r["conn"] == oc], [False], "released")
    end = time.monotonic() + 3
    while admin.get("/api/admin/sessions").json()["sessions"] and time.monotonic() < end:
        time.sleep(0.1)
    eq(admin.get("/api/admin/sessions").json()["sessions"], [], "closed sockets disappear from the list")


# -- T50: a callsign with "/" -------------------------------------------------------------------------
@test("admin")
def t50_callsign_with_slash(ctx):
    srv = ctx.server()
    admin = harness.ready_admin(srv)
    r = admin.post("/api/admin/users", {"user": "w1aw/p", "role": "viewer", "password": "Slash#Call-2026",
                                        "must_change": False})
    eq((r.status, r.json().get("user"), r.json().get("uid")), (200, "W1AW/P", "573141572f50"), f"create {r!r}")
    r = admin.patch("/api/admin/users/573141572f50", {"role": "operator"})
    eq((r.status, r.json().get("changed")), (200, True), "T50 PATCH by hex uid")
    eq(harness.read_auth(srv.tmp)["users"]["W1AW/P"]["role"], "operator")
    c = login(srv, "W1AW/P", "Slash#Call-2026")
    r = c.put("/api/me/layout", {"device": "desktop", "data": {"column": 12, "items": [{"id": "vfo", "x": 0, "y": 0, "w": 4, "h": 5}]}})
    eq(r.status, 200, f"layout PUT {r!r}")
    check((srv.tmp / "layouts" / "573141572f50.json").exists(), "T50 layouts/<hex>.json")
    users = {u["user"]: u["uid"] for u in admin.get("/api/admin/users").json()["users"]}
    eq(users.get("W1AW/P"), "573141572f50", "the users list carries the uid")


# -- T31 / T40: allowed hosts -------------------------------------------------------------------------
@test("admin")
def t31_t40_allowed_hosts(ctx):
    srv = ctx.server()
    admin = harness.ready_admin(srv)
    port = srv.port
    for entries in (["localhost"], ["radio.localhost"], ["127.0.0.1"], ["http://127.0.0.1:80"]):
        r = admin.put("/api/admin/allowed_hosts", {"entries": entries})
        eq((r.status, _err(r)), (400, "invalid"), f"T40 {entries}")
    hdr = {"Host": f"radio-pc:{port}", "Origin": f"http://radio-pc:{port}"}
    r = admin.raw("PUT", "/api/admin/allowed_hosts", body={"entries": ["other.example"]}, headers=hdr)
    eq((r.status, _err(r)), (400, "would_lock_out"), f"T31 {r!r}")
    check(not (srv.tmp / "config.json").exists(), "nothing written by the refused PUTs")
    r = admin.raw("PUT", "/api/admin/allowed_hosts", body={"entries": ["other.example", "radio-pc"]}, headers=hdr)
    eq(r.status, 200, f"a list that keeps this browser's name {r!r}")
    eq(json.loads((srv.tmp / "config.json").read_text())["allowed_hosts"], ["other.example", "radio-pc"])
    eq(admin.raw("GET", "/login", headers={"Host": f"radio.lan:{port}"}).status, 403, "the list is enforced now")
    g = admin.get("/api/admin/allowed_hosts").json()
    eq([e["value"] for e in g["entries"]], ["other.example", "radio-pc"], "GET entries")
    eq(admin.put("/api/admin/allowed_hosts", {"entries": []}).status, 200, "back to the empty list")
    r = admin.put("/api/admin/allowed_hosts", {"entries": "radio-pc"})
    eq((r.status, r.json().get("field")), (400, "entries"), "entries must be a list")
    check(audit_events(srv.tmp, "allowed_hosts_changed"), "allowed_hosts_changed audited")


# -- T32 (live part): Serve ON is host-local only; the old routes are gone ---------------------------------
@test("admin")
def t32_serve_on_is_local_only(ctx):
    srv = ctx.server()
    admin = harness.ready_admin(srv)
    r = admin.post("/api/admin/tailscale_serve", {}, headers={"X-Forwarded-For": "203.0.113.5"})
    eq((r.status, _err(r)), (403, "local_only"), f"T32 Serve ON via XFF {r!r}")
    r = admin.raw("POST", "/api/admin/tailscale_serve", body={},
                  headers={"Host": f"radio-pc:{srv.port}", "Origin": f"http://radio-pc:{srv.port}"})
    eq((r.status, _err(r)), (403, "local_only"), "Serve ON by host name (not direct-local)")
    for path in ("/api/remote_status", "/api/tailscale_serve", "/api/tailscale_serve_off"):
        eq(admin.request("POST" if "serve" in path else "GET", path, {} if "serve" in path else None).status, 404,
           f"{path} is gone")
    pw = harness.api_mk_user(admin, "OP1", "operator")
    op = login(srv, "OP1", pw)
    for method, path in (("GET", "/api/admin/remote_status"), ("POST", "/api/admin/tailscale_serve_off"),
                         ("POST", "/api/admin/clear_behind_proxy"), ("GET", "/api/admin/audio"),
                         ("PUT", "/api/admin/radio_transport"), ("POST", "/api/admin/reauth")):
        r = op.request(method, path, None if method == "GET" else {})
        eq((r.status, _err(r)), (403, "forbidden"), f"operator {method} {path}")


# -- T75: the F20 transport pin -------------------------------------------------------------------------
@test("admin")
def t75_radio_transport_pin(ctx):
    srv = ctx.server()
    admin = harness.ready_admin(srv)
    lan = {"transport": "lan", "host": "192.168.2.40", "port": 50001, "user": "radio-user", "password": "Radio#Secret"}
    r = admin.put("/api/admin/radio_transport", {"radio": "ic9700", "transport": lan})
    eq(r.status, 200, f"T75 pin {r!r}")
    text = (srv.tmp / "config.json").read_text()
    eq(json.loads(text)["radio_transports"]["ic9700"], {"transport": "lan", "host": "192.168.2.40", "port": 50001},
       "T75 stored pin")
    check("Radio#Secret" not in text and "radio-user" not in text, "T75 credentials reached config.json")
    st = admin.get("/api/status").json()
    eq(st.get("pinned_transport"), None, "no last_radio yet: status shows no pin for the current radio")
    r = admin.put("/api/admin/radio_transport", {"radio": "ic9700", "transport": {"transport": "sim"}})
    eq(r.status, 200, "pin the simulator")
    pw = harness.api_mk_user(admin, "OP1", "operator")
    op = login(srv, "OP1", pw)
    r = op.post("/api/connect", SIM)
    eq(r.status, 200, f"F20: an operator connects the radio on its admin-set pin {r!r}")
    eq(op.post("/api/disconnect", {}).status, 200)
    r = admin.put("/api/admin/radio_transport", {"radio": "ic9700", "transport": None})
    eq(r.status, 200, "T75 clear")
    check("ic9700" not in json.loads((srv.tmp / "config.json").read_text()).get("radio_transports", {}), "T75 cleared")
    r = op.post("/api/connect", SIM)
    eq((r.status, (r.json() or {}).get("reason")), (403, "transport_not_pinned"), "no pin -> operators can't connect")
    recs = harness.audit_records(srv.tmp, "audit-tx.log")
    eq([x.get("transport") for x in recs if x.get("ev") == "radio_transport_pinned"][:3],
       [{"transport": "lan", "host": "192.168.2.40", "port": 50001}, {"transport": "sim"}, None],
       "radio_transport_pinned audited in audit-tx.log (never with credentials)")
