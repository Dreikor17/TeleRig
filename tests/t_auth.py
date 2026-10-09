"""`auth` group (step A3): accounts, login, sessions, rate limits, Host/Origin, the CLI.

Every server is harness.Server (run.py --sim-only --no-autoconnect on 127.0.0.1:<free port>,
scratch config + scratch home). Users are created in the scratch store by harness.mk_user (an
in-process write; the server picks it up through its reload). The `--reset-admin` CLI only ever
runs as a harness subprocess with the scratch environment. Only the simulator is ever connected
and no TX command is sent.

"Remote" requests are made from 127.0.0.1 with an X-Forwarded-For header (or a Host that is not
localhost): the server then treats them as proxied, exactly like a real reverse proxy.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import harness
from harness import check, eq, test

SIM = {"transport": "sim", "radio": "ic9700"}
HDRS = ("x-frame-options", "content-security-policy", "x-content-type-options", "referrer-policy",
        "cross-origin-opener-policy", "permissions-policy")


def _err(r):
    j = r.json()
    return j.get("error") if isinstance(j, dict) else None


def xff(ip: str) -> dict:
    return {"X-Forwarded-For": ip}


def audit_events(tmp: Path, ev: str, timeout: float = 4.0) -> list:
    """Wait (the writer thread is asynchronous) for audit.log entries of type ev."""
    end = time.monotonic() + timeout
    while True:
        out = []
        p = Path(tmp) / "audit.log"
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("ev") == ev:
                    out.append(rec)
        if out or time.monotonic() > end:
            return out
        time.sleep(0.1)


class old_cookie_client:
    """Replays one session token as the only cookie (exact request via http.client)."""

    def __init__(self, srv, token: str):
        self.c, self.cookie = srv.client(), f"tr_session_{srv.port}={token}"

    def get(self, path: str) -> harness.Resp:
        return self.c.raw("GET", path, headers={"Cookie": self.cookie}, cookies=False)


def ws_with_cookie(srv, token: str, **kw) -> harness.WS:
    return srv.client().ws(cookies=False, headers={"Cookie": f"tr_session_{srv.port}={token}"}, **kw)


def wait_closed_code(ws, timeout=3.0):
    try:
        return ws.wait_closed(timeout=timeout)
    except TimeoutError:
        return None


# -- T01 / T02: first start and the gate ------------------------------------------------------------
@test("auth")
def t01_first_start_seeds_default_admin(ctx):
    tmp = ctx.tmp()
    srv = ctx.server(tmp)
    a = harness.read_auth(tmp)
    u = a["users"]["ADMIN"]
    eq((u["role"], u["default_pw"], u["must_change"], u["migrated_v1"], u["gen"]), ("admin", True, True, False, 1))
    code = a["meta"]["setup_code"]
    check(re.fullmatch(r"[A-HJ-NP-Z2-9]{12}", code or ""), f"setup code {code!r}")
    check(not (tmp / "config.json").exists(), "config.json was created")
    log = srv.log_text()
    pretty = "-".join(code[i:i + 4] for i in (0, 4, 8))
    check(f"admin / admin with setup code {pretty}" in log, "banner lacks the setup code:\n" + log)
    check(f"/login#setup={pretty}" in log, "banner lacks the login URL with the code")
    check("Login required for everyone." in log and "WARNING: no login" not in log, "stale banner:\n" + log)
    eq([e.get("result") for e in audit_events(tmp, "store_init")], ["default"], "store_init audited")


@test("auth")
def t02_everything_needs_a_session(ctx):
    srv = ctx.server()
    c = srv.client()
    r = c.get("/")
    eq((r.status, r.headers.get("location")), (303, "/login?next=%2F"), "/")
    eq(c.get("/api/status").status, 401, "/api/status")
    eq(_err(c.get("/api/status")), "auth_required")
    r = c.get("/static/app.js")
    eq((r.status, r.headers.get("location")), (303, "/login?next=%2Fstatic%2Fapp.js"), "/static/app.js")
    r = c.get("/?noconnect")
    eq(r.headers.get("location"), "/login?next=%2F%3Fnoconnect", "next keeps the query")
    for path in ("/api/radios", "/api/me", "/api/ports", "/api/version", "/api/admin/users"):
        eq(c.get(path).status, 401, path)
    eq(c.post("/api/connect", SIM).status, 401, "POST /api/connect")
    eq(c.get("/login").status, 200, "/login is public")
    eq(c.get("/api/auth_status").status, 200, "/api/auth_status is public")
    harness.ready_admin(srv, c)
    eq(c.get("/api/set_password").status, 404, "/api/set_password is gone")
    eq(c.post("/api/set_password", {"clear": True}).status, 404, "POST /api/set_password is gone")


# -- T03 / T33 / T38: the first-run login -------------------------------------------------------------
@test("auth")
def t03_default_login_needs_the_setup_code(ctx):
    srv = ctx.server()
    c = srv.client()
    r = harness.admin_login(c, srv.tmp, code=None)
    eq((r.status, _err(r)), (403, "setup_code_required"), f"no code {r!r}")
    check("setup code" in r.json().get("message", ""), r.text)
    r = harness.admin_login(c, srv.tmp, code="AAAA-AAAA-AAAA")
    eq((r.status, _err(r)), (403, "setup_code_required"), f"wrong code {r!r}")
    eq(r.header_all("set-cookie"), [], "a refused login sets no cookie")
    r = harness.admin_login(c, srv.tmp)
    eq(r.status, 200, f"admin/admin + code {r!r}")
    eq((r.json().get("must_change"), r.json().get("next")), (True, "/change-password"))
    cookies = r.header_all("set-cookie")
    mine = [x for x in cookies if x.startswith(f"tr_session_{srv.port}=")]
    eq(len(mine), 1, f"T33: the cookie name carries the bound port: {cookies}")
    low = mine[0].lower()
    check("httponly" in low and "samesite=lax" in low and "path=/" in low, mine[0])
    check("secure" not in low, "Secure on plain http")
    m = re.search(r"max-age=(\d+)", low)
    check(m and abs(int(m.group(1)) - 30 * 86400) < 60, f"Max-Age {m and m.group(1)}")
    for legacy in ("tr_session", "rw_session"):                  # T38: legacy cookies expired
        hit = [x for x in cookies if x.startswith(legacy + "=")]
        check(hit and "max-age=0" in hit[0].lower(), f"{legacy} not expired: {cookies}")
    end = time.monotonic() + 3                                   # best-effort write after the response
    while harness.read_auth(srv.tmp)["users"]["ADMIN"]["last_login"] is None and time.monotonic() < end:
        time.sleep(0.1)
    check(harness.read_auth(srv.tmp)["users"]["ADMIN"]["last_login"] is not None, "last_login not recorded")


@test("auth")
def t38_v1_cookies_are_rejected(ctx):
    srv = ctx.server()
    harness.ready_admin(srv)
    c = srv.client()
    for name in ("tr_session", "rw_session"):
        r = c.raw("GET", "/api/me", headers={"Cookie": f"{name}=1999999999.{'ab' * 32}"}, cookies=False)
        eq(r.status, 401, f"only a v1 {name} cookie")
    r = c.raw("GET", "/api/me", headers={"Cookie": f"tr_session_8700=v2.QURNSU4.1.1999999999.AAAA"}, cookies=False)
    eq(r.status, 401, "another port's cookie name")


# -- T04: the restricted (must-change) session --------------------------------------------------------
@test("auth")
def t04_restricted_session(ctx):
    srv = ctx.server()
    c = srv.client()
    eq(harness.admin_login(c, srv.tmp).status, 200)
    r = c.get("/")
    eq((r.status, r.headers.get("location")), (303, "/change-password"), "/ while restricted")
    r = c.get("/api/me")
    eq((r.status, _err(r)), (403, "password_change_required"), "/api/me while restricted")
    eq(c.post("/api/connect", SIM).status, 403, "POST /api/connect while restricted")
    eq(c.get("/static/app.js").status, 303, "static while restricted")
    with c.ws() as ws:
        eq(ws.wait_closed(), (4403, "password_change_required"), "WS while restricted")
    eq(c.get("/change-password").status, 200, "/change-password")
    eq(c.get("/login").headers.get("location"), "/change-password", "/login while restricted")
    s = c.get("/api/auth_status").json()
    eq((s["authed"], s["user"], s["must_change"], s["default_pw"]), (True, "ADMIN", True, True))


# -- T05: the default password never works remotely (F1, no oracle) -------------------------------------
@test("auth")
def t05_default_password_is_local_only(ctx):
    srv = ctx.server()
    c = srv.client()
    code = harness.setup_code(srv.tmp)
    r = harness.admin_login(c, srv.tmp, headers=xff("203.0.113.9"))
    eq((r.status, _err(r)), (401, "invalid_credentials"), f"via XFF {r!r}")
    eq(r.json().get("message"), "Invalid username or password.", "same text as a wrong password")
    for i in range(4):                                           # counted: 1 + 4 = 5 -> the 6th is 429
        eq(harness.login(c, "admin", f"wrong-{i}", headers=xff("203.0.113.9")).status, 401)
    r = harness.admin_login(c, srv.tmp, headers=xff("203.0.113.9"))
    eq(r.status, 429, f"the default-password attempt was counted {r!r}")
    port = srv.port
    body = {"username": "admin", "password": "admin", "setup": code}
    r = c.raw("POST", "/api/login", body=body, headers={"Host": f"radio-pc:{port}", "Origin": f"http://radio-pc:{port}"})
    eq((r.status, _err(r)), (401, "invalid_credentials"), f"via Host radio-pc {r!r}")
    s = c.get("/api/auth_status", headers=xff("203.0.113.9")).json()
    eq(s["setup"]["kind"], None, "auth_status via XFF")
    s = c.raw("GET", "/api/auth_status", headers={"Host": f"radio-pc:{port}"}).json()
    eq((s["setup"]["kind"], s["local_reason"]), (None, "host_not_local"), "auth_status via Host radio-pc")
    s = c.get("/api/auth_status").json()
    eq((s["setup"]["kind"], s["local_reason"]), ("default", None), "auth_status direct-local")
    check(audit_events(srv.tmp, "login_default_remote"), "login_default_remote not audited")
    eq(harness.admin_login(c, srv.tmp).status, 200, "the same credentials work direct-local")


# -- T06 / T52: the forced change and the setup code -----------------------------------------------
@test("auth")
def t06_change_password(ctx):
    srv = ctx.server()
    c = srv.client()
    eq(harness.admin_login(c, srv.tmp).status, 200)
    old = harness.session_cookie(c)
    for new, label in (("a" * 7, "7 bytes"), ("a" * 73, "73 bytes"), ("admin", "'admin'")):
        r = c.post("/api/me/password", {"current": "admin", "new": new})
        eq((r.status, _err(r), r.json().get("field")), (400, "invalid", "new"), f"{label} {r!r}")
    r = c.post("/api/me/password", {"current": "wrong", "new": "Good#Pass-2026"})
    eq((r.status, r.json().get("field")), (401, "current"), f"wrong current {r!r}")
    r = c.post("/api/me/password", {"current": "admin", "new": "Good#Pass-2026"})
    eq((r.status, r.json()), (200, {"ok": True, "next": "/"}), f"good change {r!r}")
    check(any(x.startswith(f"tr_session_{srv.port}=") for x in r.header_all("set-cookie")), "no new cookie")
    new_cookie = harness.session_cookie(c)
    check(new_cookie and new_cookie != old, "cookie not replaced")
    eq(c.get("/api/me").json().get("role"), "admin", "the new cookie works")
    eq(old_cookie_client(srv, old).get("/api/me").status, 401, "the old cookie")
    a = harness.read_auth(srv.tmp)
    eq((a["users"]["ADMIN"]["must_change"], a["users"]["ADMIN"]["default_pw"], a["meta"]["setup_code"]),
       (False, False, None), "flags + setup code cleared")
    check(audit_events(srv.tmp, "pw_changed"), "pw_changed not audited")


@test("auth")
def t06b_password_change_closes_the_users_other_sockets(ctx):
    """I7: a password change bumps the generation; the user's open sockets close 4401 within 1 s."""
    srv = ctx.server()
    c = harness.ready_admin(srv)
    other = harness.ready_admin(srv, srv.client())               # a second device of the same user
    with other.ws() as ws:
        eq(ws.recv_json(type="hello").get("user"), "ADMIN")
        t0 = time.monotonic()
        r = c.post("/api/me/password", {"current": srv.admin_pw, "new": "Second#Pass-2026"})
        eq(r.status, 200, f"change {r!r}")
        eq(ws.wait_closed(timeout=3), (4401, "session_revoked"), "the other device's socket")
        check(time.monotonic() - t0 < 1.5, f"closed after {time.monotonic() - t0:.2f}s")
    eq(other.get("/api/me").status, 401, "the other device's cookie")


@test("auth")
def t52_setup_code_formats_and_clearing(ctx):
    srv = ctx.server()
    code = harness.setup_code(srv.tmp)
    dashed = "-".join(code[i:i + 4] for i in (0, 4, 8))
    c1, c2 = srv.client(), srv.client()
    r = harness.admin_login(c1, srv.tmp, code=dashed)
    eq((r.status, r.json().get("must_change")), (200, True), f"dashed code {r!r}")
    r = harness.admin_login(c2, srv.tmp, code=code.lower())
    eq((r.status, r.json().get("must_change")), (200, True), f"lower-case undashed code {r!r}")
    r = harness.admin_login(srv.client(), srv.tmp, code=" " + dashed.lower() + " ")
    eq(r.status, 200, f"spaces are ignored {r!r}")
    eq(c1.post("/api/me/password", {"current": "admin", "new": "Setup#Done-2026"}).status, 200)
    eq(harness.read_auth(srv.tmp)["meta"]["setup_code"], None, "setup_code cleared after the change")
    r = harness.admin_login(srv.client(), srv.tmp, code=dashed)
    eq((r.status, _err(r)), (401, "invalid_credentials"), f"admin/admin + old code after the change {r!r}")
    eq(c2.get("/api/auth_status").json().get("authed"), False, "the other restricted session is gone")


# -- T07: hello ----------------------------------------------------------------------------------
@test("auth")
def t07_ws_hello_then_state(ctx):
    srv = ctx.server()
    c = harness.ready_admin(srv)
    with c.ws() as ws:
        h = ws.recv_json()
        eq((h.get("type"), h.get("user"), h.get("role"), h.get("v")), ("hello", "ADMIN", "admin", 1), "first frame")
        eq((h.get("hb_interval"), h.get("hb_timeout"), h.get("sim_only"), h.get("migrated_remote")), (2, 6, True, False))
        check(isinstance(h.get("conn"), int) and "admin" in h.get("perms", []), h)
        eq(h.get("tx"), {"owner": None})
        st = h.get("status") or {}
        eq((st.get("sim_only"), st.get("transport_kind"), st.get("connected")), (True, "none", False), "hello status")
        check(isinstance(h.get("server_version"), str), "server_version")
        eq(ws.recv_json().get("type"), "state", "second frame")
    eq(c.post("/api/connect", SIM).status, 200)
    st = c.get("/api/status").json()
    eq((st["sim_only"], st["transport_kind"], st["connected"], st["radio"]), (True, "sim", True, "ic9700"), "/api/status")
    check("transport" in st and "pinned_transport" in st, "operator+ fields")
    eq(c.post("/api/disconnect", None).status, 200)


# -- T11 / T12 / T45: rate limits ---------------------------------------------------------------------
@test("auth")
def t11_per_source_limit(ctx):
    srv = ctx.server()
    c = harness.ready_admin(srv)
    ip = xff("203.0.113.50")
    for i in range(5):
        eq(harness.login(c, "admin", f"bad-{i}", headers=ip).status, 401, f"bad login {i + 1}")
    r = harness.login(c, "admin", "bad-6", headers=ip)
    eq(r.status, 429, f"6th {r!r}")
    check(r.json().get("retry_after", 0) > 0 and r.headers.get("retry-after"), r.headers)
    r = harness.login(c, "admin", srv.admin_pw, headers=ip)
    eq((r.status, _err(r)), (429, "rate_limited"), "the correct password is still refused (checked first)")
    eq(harness.login(c, "admin", srv.admin_pw, headers=xff("203.0.113.51")).status, 200, "another IP")


@test("auth")
def t12_user_source_and_user_wide_limits(ctx):
    srv = ctx.server()
    harness.ready_admin(srv)
    pw = harness.mk_user(srv.tmp, "X1AA", "operator")
    c = srv.client()
    known = xff("198.51.100.200")
    eq(harness.login(c, "X1AA", pw, headers=known).status, 200, "a sign-in from the later 'known clean' IP")
    # K2: (user, source). A public IP hits K1 (5) first, so use a loopback XFF source (no K1).
    src = xff("127.0.0.9")
    for i in range(10):
        eq(harness.login(c, "X1AA", f"bad-{i}", headers=src).status, 401, f"K2 failure {i + 1}")
    eq(harness.login(c, "X1AA", "bad-11", headers=src).status, 429, "11th from that source (K2)")
    eq(harness.login(c, "X1AA", pw, headers=src).status, 429, "right password from that source (K2)")
    eq(harness.login(c, "X1AA", pw, headers=xff("198.51.100.10")).status, 200, "a different clean IP verifies")
    eq(harness.login(c, "ADMIN", srv.admin_pw, headers=src).status, 200, "another user from the same source")
    # K3: X1AA already has 10 failures in the window; 20 more over many IPs make 30.
    for i in range(20):
        eq(harness.login(c, "X1AA", "bad", headers=xff(f"203.0.113.{i + 1}")).status, 401, f"spread failure {i + 1}")
    r = harness.login(c, "X1AA", pw, headers=xff("203.0.113.99"))
    eq((r.status, _err(r)), (429, "rate_limited"), f"a fresh unknown IP after the K3 lock {r!r}")
    eq(harness.login(c, "X1AA", pw, headers=known).status, 200, "the known clean IP bypasses the user lock")
    eq(harness.login(srv.client(), "X1AA", pw).status, 200, "direct-local bypasses the user lock")
    check(audit_events(srv.tmp, "lockout_tripped"), "lockout_tripped not audited")


@test("auth")
def t45_loopback_proxy_and_direct_local_sources(ctx):
    srv = ctx.server()
    harness.ready_admin(srv)
    pw_y = harness.mk_user(srv.tmp, "Y1YY", "operator")
    pw_z = harness.mk_user(srv.tmp, "Z1ZZ", "operator")
    c = srv.client()
    port = srv.port
    host = {"Host": f"pc.tail1.ts.net:{port}", "Origin": f"http://pc.tail1.ts.net:{port}"}
    codes = []
    for i in range(20):                                          # loopback peer, no XFF, not direct-local
        r = c.raw("POST", "/api/login", body={"username": "Y1YY", "password": f"bad-{i}"}, headers=host)
        codes.append(r.status)
    eq((codes[:10], set(codes[10:])), ([401] * 10, {429}), "Y1YY: K2 after 10 (never K1 on a loopback source)")
    r = c.raw("POST", "/api/login", body={"username": "Z1ZZ", "password": pw_z}, headers=host)
    eq(r.status, 200, f"(a) Z1ZZ from the same loopback source is not blocked {r!r}")
    d = srv.client()
    for i in range(20):                                          # 20 direct-local failures, 20 different names
        eq(harness.login(d, f"U{i:02d}X", "bad-password").status, 401, f"direct-local failure {i + 1}")
    r = harness.login(d, "ADMIN", srv.admin_pw)
    eq((r.status, _err(r)), (429, "rate_limited"), f"(b) the 21st direct-local attempt (K1 direct-local) {r!r}")
    eq(harness.login(srv.client(), "Y1YY", pw_y, headers=xff("198.51.100.7")).status, 200,
       "(b) a correct login via a real XFF IP still succeeds")


# -- T14a: the coarse WS rule ------------------------------------------------------------------------
@test("auth")
def t14a_viewer_cannot_operate(ctx):
    srv = ctx.server()
    admin = harness.ready_admin(srv)
    eq(admin.post("/api/connect", SIM).status, 200, "sim connect")
    pw = harness.mk_user(srv.tmp, "V1VV", "viewer")
    v = srv.client()
    eq(harness.login(v, "V1VV", pw).status, 200)
    eq(v.get("/api/me").json(), {"user": "V1VV", "role": "viewer", "perms": ["view", "listen", "menu_read"],
                                 "migrated_remote": False})
    eq(v.post("/api/connect", SIM).status, 403, "viewer POST /api/connect")
    eq(v.post("/api/disconnect", None).status, 403, "viewer POST /api/disconnect")
    eq(v.get("/api/ports").status, 403, "viewer /api/ports")
    with v.ws() as ws:
        eq(ws.recv_json(type="hello").get("role"), "viewer")
        f0 = ws.recv_json(type="state").get("freq")
        ws.send({"action": "set_freq", "hz": 145123000})
        d = ws.wait_for(lambda m: m.get("type") == "denied", timeout=5)
        eq((d.get("action"), d.get("reason")), ("set_freq", "role"), "viewer set_freq")
    time.sleep(0.5)
    with admin.ws() as ws:
        st = ws.recv_json(type="state")
        eq((st.get("freq"), st.get("freq") != 145123000), (f0, True), "the viewer's set_freq changed nothing")
        ws.send({"action": "set_freq", "hz": 145124000})
        ws.wait_for(lambda m: m.get("type") == "state" and m.get("freq") == 145124000, timeout=5)
    eq(admin.post("/api/disconnect", None).status, 200)


# -- T25: bootstrap ---------------------------------------------------------------------------------
@test("auth")
def t25_bootstrap_json_is_escaped(ctx):
    tmp = ctx.tmp()
    evil = "</script><b>x&y"
    (tmp / "config.json").write_text(json.dumps({"last_radio": evil}), encoding="utf-8")
    srv = ctx.server(tmp)
    c = harness.ready_admin(srv)
    r = c.get("/")
    eq(r.status, 200)
    html = r.text
    tag = '<script id="tr-boot" type="application/json">'
    check(tag in html, "no bootstrap tag")
    payload = html.split(tag, 1)[1].split("</script>", 1)[0]
    check("<" not in payload and ">" not in payload and "&" not in payload, f"unescaped payload: {payload[:200]}")
    check("\\u003c/script\\u003e\\u003cb\\u003ex\\u0026y" in payload, "escaped value missing")
    check(evil not in html, "the raw value reached the page")
    boot = json.loads(payload)
    eq((boot["user"], boot["role"], boot["layout"], boot["status"]["last_radio"], boot["port"]),
       ("ADMIN", "admin", None, evil, srv.port))
    check(boot["sim_only"] is True and "admin" in boot["perms"] and boot["radios"], boot)
    check('<body data-role="admin">' in html, "body data-role")
    eq(r.headers.get("cache-control"), "no-store")


# -- T26b / T55: --reset-admin --------------------------------------------------------------------------
RESET = ["run.py", "--reset-admin", "--password-stdin"]


@test("auth")
def t26b_reset_admin_repairs_a_corrupt_store(ctx):
    tmp = ctx.tmp()
    srv = ctx.server(tmp)
    c = harness.ready_admin(srv)
    ip = xff("203.0.113.77")
    for i in range(5):
        harness.login(c, "admin", f"bad-{i}", headers=ip)
    eq(harness.login(c, "admin", srv.admin_pw, headers=ip).status, 429, "IP locked out before the repair")
    with c.ws() as ws:
        ws.recv_json(type="hello")
        (tmp / "auth.json").write_text('{"version": 2, "users": {', encoding="utf-8")
        eq(ws.wait_closed(timeout=4), (4403, "server_locked"), "open socket when auth.json broke")
    eq(srv.client().get("/login").status, 503, "/login with a corrupt auth.json")
    eq(srv.client().post("/api/login", {"username": "admin", "password": "x"}).status, 503, "POST /api/login")
    r = harness.run_child(RESET, tmp, input="NewPass#2026\n")
    eq(r.returncode, 0, f"--reset-admin:\n{r.stdout}\n{r.stderr}")
    check("ADMIN password reset" in r.stdout, r.stdout)
    check(list(tmp.glob("auth.json.corrupt-*")), "the corrupt store was not kept")
    end = time.monotonic() + 6
    while srv.client().get("/login").status != 200:
        check(time.monotonic() < end, "still locked after --reset-admin")
        time.sleep(0.2)
    r = harness.login(c, "admin", "NewPass#2026", headers=ip)
    eq((r.status, r.json().get("must_change")), (200, False), f"login from the formerly locked IP {r!r}")
    check(audit_events(tmp, "reset_admin"), "reset_admin not audited")
    # the same command without TELERIG_CONFIG and without --real-config refuses (AR-24)
    r = harness.run_child(RESET, tmp, input="Other#Pass-2026\n", config_env=False)
    eq(r.returncode, 1, f"no TELERIG_CONFIG:\n{r.stdout}\n{r.stderr}")
    check("refusing" in r.stdout.lower(), r.stdout)
    check(not (tmp / "home" / ".telerig").exists(), "the refused run created ~/.telerig")


_T55 = r"""
import sys, runpy
sys.modules["uvicorn"] = None                 # 'import uvicorn' now raises ImportError
sys.argv = ["run.py", "--reset-admin", "--password-stdin", "--demote-other-admins"]
runpy.run_path("run.py", run_name="__main__")
"""


@test("auth")
def t55_reset_admin_without_uvicorn_and_demotion(ctx):
    tmp = ctx.tmp()
    harness.mk_user(tmp, "W1AW", "admin")
    before = harness.read_auth(tmp)["users"]["W1AW"]["gen"]
    r = harness.run_child(["-c", _T55], tmp, input="Fresh#Pass-2026\n")
    eq(r.returncode, 0, f"--reset-admin with uvicorn unimportable:\n{r.stdout}\n{r.stderr}")
    check("Other admin accounts: W1AW" in r.stdout and "Demoted W1AW" in r.stdout, r.stdout)
    a = harness.read_auth(tmp)
    eq(a["users"]["W1AW"]["role"], "operator", "W1AW demoted")
    check(a["users"]["W1AW"]["gen"] > before, "the demoted admin's gen was not bumped")
    adm = a["users"]["ADMIN"]
    eq((adm["role"], adm["must_change"], adm["default_pw"], a["meta"]["setup_code"], a["meta"]["limiter_reset"]),
       ("admin", False, False, None, 1))
    res = harness.accounts_child(tmp, "result = accounts.verify(accounts.get('ADMIN').hash, 'Fresh#Pass-2026')[0]")
    eq(res, True, "the new password verifies")
    r = harness.run_child(["run.py", "--reset-admin", "--password-stdin"], tmp, input="short\n")
    eq(r.returncode, 1, "a weak password is refused")
    r = harness.run_child(["run.py", "--password-stdin"], tmp, input="x\n")
    check(r.returncode != 0 and "--reset-admin" in r.stderr, "--password-stdin alone is a usage error")


@test("auth")
def t26c_locked_page_names_the_folder_only_on_the_host_pc(ctx):
    """Final review round 3: the locked-mode 503 (page and API) shows the config file's full path
    (which carries the OS user name) and the parse error only to a browser on the host PC itself;
    a network client gets the file's name and the recovery command, nothing more. The console
    always has the full path."""
    tmp = ctx.tmp()
    srv = ctx.server(tmp)
    harness.ready_admin(srv)
    port = srv.port
    auth_p = tmp / "auth.json"
    auth_p.write_text('{"version": 2, "users": {', encoding="utf-8")
    c = srv.client()
    end = time.monotonic() + 8
    while c.get("/login").status != 503:
        check(time.monotonic() < end, "the server did not lock")
        time.sleep(0.2)
    folder, home = str(tmp).lower(), str(Path.home()).lower()       # the server prints normcase'd paths
    r = c.get("/login")                                               # direct-local
    check(str(auth_p).lower() in r.text.lower() and "--reset-admin" in r.text, f"host PC: the full path: {r.text[:400]}")
    for label, hdrs in (("remote (X-Forwarded-For)", xff("198.51.100.4")),
                        ("LAN name", {"Host": f"radio-pc:{port}"}),
                        ("Tailscale Serve", {"Host": "pc.tailnet.ts.net", "X-Forwarded-Proto": "https"})):
        for path in ("/login", "/api/auth_status", "/"):
            r = srv.client().get(path, headers=hdrs)
            eq(r.status, 503, f"{label} {path}")
            t = r.text
            check("locked" in t.lower() and "auth.json" in t and "--reset-admin" in t,
                  f"{label} {path}: names the file and the recovery command: {t[:300]}")
            tl = t.lower().replace("\\\\", "\\")                      # JSON doubles the backslashes
            check(folder not in tl and home not in tl, f"{label} {path}: the folder leaked: {t[:400]}")
            check("Expecting" not in t and "line 1" not in t, f"{label} {path}: the parse error leaked: {t[:400]}")
        missing = [h for h in HDRS if not r.headers.get(h)]
        eq(missing, [], f"{label}: security headers on the 503")
    check(str(auth_p).lower() in srv.log_text().lower(), "the console names the full path")
    auth_p.unlink()                                                   # recover: seeded again, unlocked
    end = time.monotonic() + 8
    while c.get("/login").status != 200:
        check(time.monotonic() < end, "did not unlock")
        time.sleep(0.2)


@test("auth")
def t29b_deeply_nested_json_body_is_a_400_with_headers(ctx):
    """Final review round 3: a deeply nested JSON body (json.loads raises RecursionError) is a bad
    body, 400 invalid_json with the normal security headers, on the PUBLIC /api/login and on every
    JSON-body route (they share account_api.read_json); never a 500. No TX route can act on it:
    the body is refused before anything is read from it."""
    srv = ctx.server()
    port = srv.port
    o = {"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}
    anon = srv.client()
    # under the 64 KB body cap; json.loads raises RecursionError on these (CPython 3.14: a C stack
    # overflow from ~20000 levels), the object one only after it has started a dict
    deep = b"[" * 30000 + b"]" * 30000
    bodies = {"[ x30000 ]": deep, "[ x60000 unclosed": b"[" * 60000, '{"a": [ x30000 ]}': b'{"a":' + deep + b"}"}
    for label, body in bodies.items():
        check(len(body) <= 65536, f"{label}: under the body cap")
        r = anon.raw("POST", "/api/login", body=body, headers=o)
        eq((r.status, _err(r)), (400, "invalid_json"), f"/api/login {label}")
        eq([h for h in HDRS if not r.headers.get(h)], [], f"/api/login {label}: security headers")
    c = harness.ready_admin(srv)
    nested = bodies['{"a": [ x30000 ]}']
    for method, path in (("POST", "/api/me/password"), ("PUT", "/api/me/layout"), ("POST", "/api/connect"),
                         ("POST", "/api/disconnect"), ("POST", "/api/tx_stop"), ("POST", "/api/admin/reauth"),
                         ("POST", "/api/admin/users"), ("PUT", "/api/admin/allowed_hosts"),
                         ("POST", "/api/admin/lockouts/unlock_ip"), ("PUT", "/api/admin/audio"),
                         ("PUT", "/api/admin/radio_transport")):
        r = c.raw(method, path, body=nested, headers=o)
        want = (413, "too_large") if path == "/api/me/layout" else (400, "invalid_json")   # its cap is 16 KB
        eq((r.status, _err(r)), want, f"{method} {path} nested body")
        eq([h for h in HDRS if not r.headers.get(h)], [], f"{method} {path}: security headers")
    r = c.raw("PUT", "/api/me/layout", body=b'{"a":' + b"[" * 8000 + b"]" * 8000 + b"}", headers=o)
    check(r.status == 400 and _err(r) in ("invalid_json", "invalid"), f"PUT /api/me/layout deep (16 KB) body: {r!r}")
    eq(anon.get("/api/auth_status").status, 200, "the server still answers")
    check("RecursionError" not in srv.log_text(), "no RecursionError reached the server log")
    check(srv.alive(), "server died")


@test("auth")
def auth_json_deleted_at_runtime_is_seeded_again(ctx):
    """A deleted auth.json is treated like a first start (never an open server): every session
    ends, a new default ADMIN with a NEW setup code is created and the code goes to the console."""
    tmp = ctx.tmp()
    srv = ctx.server(tmp)
    c = harness.ready_admin(srv)
    (tmp / "auth.json").unlink()
    end = time.monotonic() + 6
    while not (tmp / "auth.json").exists():
        check(time.monotonic() < end, "auth.json was not created again")
        time.sleep(0.1)
    time.sleep(0.3)
    eq(c.get("/api/me").status, 401, "the old session after the re-seed")
    code = harness.setup_code(tmp)
    check(code and "-".join(code[i:i + 4] for i in (0, 4, 8)) in srv.log_text(), "the new setup code is not on the console")
    r = harness.admin_login(srv.client(), tmp, headers=xff("198.51.100.9"))
    eq(r.status, 401, "the fresh default password is still local-only")
    eq(harness.admin_login(srv.client(), tmp).status, 200, "first-run login on this PC")


@test("auth")
def banner_warns_when_there_is_no_admin(ctx):
    tmp = ctx.tmp()
    harness.mk_user(tmp, "OP9", "operator")
    harness.del_user(tmp, "ADMIN")
    srv = ctx.server(tmp)
    log = srv.log_text()
    check("there is no admin account" in log and "--reset-admin" in log, "no zero-admin warning:\n" + log)
    check("First run:" not in log, "first-run line without a default ADMIN")


# -- T27 / T54: v1 migration ------------------------------------------------------------------------------
def _v1_config(tmp: Path) -> bytes:
    import base64
    import hashlib
    import secrets
    salt = secrets.token_bytes(16)
    ph = base64.b64encode(salt + hashlib.scrypt(b"hunter22", salt=salt, n=16384, r=8, p=1, dklen=32)).decode()
    data = json.dumps({"session_secret": "cd" * 32, "password_hash": ph}, indent=2).encode()
    (tmp / "config.json").write_bytes(data)
    return data


@test("auth")
def t27_v1_shared_password_becomes_admin(ctx):
    tmp = ctx.tmp()
    cfg = _v1_config(tmp)
    srv = ctx.server(tmp)
    r = harness.login(srv.client(), "ADMIN", "hunter22")
    eq((r.status, r.json().get("must_change")), (200, False), f"ADMIN/hunter22 {r!r}")
    eq(harness.login(srv.client(), "admin", "hunter22").status, 200, "login is case-insensitive")
    eq((tmp / "config.v1.bak.json").read_bytes(), cfg, "config.v1.bak.json")
    eq((tmp / "config.json").read_bytes(), cfg, "config.json byte-identical")
    check("Upgraded install: sign in as admin with your previous remote-access password" in srv.log_text(), "banner")
    s = srv.client().get("/api/auth_status").json()
    eq(s["setup"]["kind"], "migrated", "direct-local login hint")
    eq(srv.client().get("/api/auth_status", headers=xff("198.51.100.3")).json()["setup"]["kind"], None, "remote: no hint")


@test("auth")
def t27b_v1_password_is_imported_once(ctx):
    """Final review round 3: the v1 shared password is migrated ONCE. After auth.json is damaged and
    moved aside (as the locked page says), or deleted while running, the re-seed is the default ADMIN
    with a setup code: the old shared password (retired or not) never signs in again, locally or
    remotely, and config.json / config.v1.bak.json stay byte-identical."""
    tmp = ctx.tmp()
    cfg = _v1_config(tmp)
    srv = ctx.server(tmp)
    remote = xff("198.51.100.20")
    eq(harness.login(srv.client(), "admin", "hunter22", headers=remote).status, 200, "the migrated password, remote")
    bak = (tmp / "config.v1.bak.json").read_bytes()
    auth_p = tmp / "auth.json"
    auth_p.write_text(auth_p.read_text(encoding="utf-8")[:-5], encoding="utf-8")       # damaged
    c = srv.client()
    end = time.monotonic() + 8
    while c.get("/api/auth_status").status != 503:
        check(time.monotonic() < end, "the server did not lock on the damaged auth.json")
        time.sleep(0.2)
    auth_p.rename(tmp / "auth.json.bad")                                                # "move it aside"
    end = time.monotonic() + 8
    while c.get("/login").status != 200:
        check(time.monotonic() < end, "the server did not re-seed and unlock")
        time.sleep(0.2)
    a = harness.read_auth(tmp)["users"]["ADMIN"]
    eq({k: a[k] for k in ("default_pw", "must_change", "migrated_v1", "created_by")},
       {"default_pw": True, "must_change": True, "migrated_v1": False, "created_by": "system"}, "re-seeded ADMIN")
    eq(harness.login(srv.client(), "admin", "hunter22", headers=remote).status, 401, "old shared password, remote")
    eq(harness.login(srv.client(), "admin", "hunter22").status, 401, "old shared password, on the host PC")
    code = harness.setup_code(tmp)
    log = srv.log_text()
    check(code and "-".join(code[i:i + 4] for i in (0, 4, 8)) in log, "the new setup code is on the console")
    check("NOT imported" in log, "the console says why the old password was not imported")
    eq(harness.admin_login(srv.client(), tmp, headers=remote).status, 401, "the default password is local-only")
    eq(harness.admin_login(srv.client(), tmp).status, 200, "first-run login on this PC with the setup code")
    # deleted while running: the same
    auth_p.unlink()
    end = time.monotonic() + 6
    while not auth_p.exists():
        check(time.monotonic() < end, "auth.json was not created again")
        time.sleep(0.1)
    time.sleep(0.3)
    eq(harness.read_auth(tmp)["users"]["ADMIN"]["migrated_v1"], False, "deleted while running: not migrated again")
    eq(harness.login(srv.client(), "admin", "hunter22", headers=remote).status, 401, "deleted: old password, remote")
    eq(((tmp / "config.json").read_bytes(), (tmp / "config.v1.bak.json").read_bytes()), (cfg, bak),
       "config.json and config.v1.bak.json byte-identical")


@test("auth")
def t54_migrated_admin_is_an_operator_off_the_host(ctx):
    tmp = ctx.tmp()
    _v1_config(tmp)
    srv = ctx.server(tmp)
    remote = xff("198.51.100.20")
    rc = srv.client()
    r = harness.login(rc, "admin", "hunter22", headers=remote)
    eq((r.status, r.json().get("must_change")), (200, False), f"remote login {r!r}")
    me = rc.get("/api/me", headers=remote).json()
    eq((me["role"], me["migrated_remote"], "admin" in me["perms"]), ("operator", True, False), "remote /api/me")
    with rc.ws(headers=remote) as ws:
        h = ws.recv_json(type="hello")
        eq((h["role"], h["migrated_remote"]), ("operator", True), "remote hello")
    eq(rc.get("/api/admin/users", headers=remote).status, 403, "remote /api/admin/users")
    r = rc.post("/api/me/password", {"current": "hunter22", "new": "Local#Only-2026"}, headers=remote)
    eq((r.status, _err(r)), (403, "migrated_local_only"), f"remote password change {r!r}")
    r = rc.post("/api/me/signout_all", headers=remote)
    eq((r.status, _err(r)), (403, "migrated_local_only"), "remote sign out everywhere")
    boot = rc.get("/", headers=remote).text
    check('data-role="operator"' in boot and "migrated_local_only" in boot, "remote bootstrap")
    lc = srv.client()
    eq(harness.login(lc, "admin", "hunter22").status, 200, "direct-local login")
    eq(lc.get("/api/me").json()["role"], "admin", "direct-local /api/me")
    eq(lc.get("/api/admin/users").status, 200, "direct-local /api/admin/users")
    check("migrated_v1_password" in lc.get("/").text, "admin notice")
    eq(lc.post("/api/me/password", {"current": "hunter22", "new": "Local#Change-2026"}).status, 200, "local change")
    eq(harness.read_auth(tmp)["users"]["ADMIN"]["migrated_v1"], False, "migrated_v1 cleared")
    r2 = srv.client()
    eq(harness.login(r2, "admin", "Local#Change-2026", headers=remote).status, 200, "remote login after the change")
    eq(r2.get("/api/me", headers=remote).json()["role"], "admin", "remote is admin now")
    eq(r2.get("/api/admin/users", headers=remote).status, 200, "remote /api/admin/users now")
    eq(rc.get("/api/me", headers=remote).status, 401, "the old remote session ended with the change")


_T54_SUDO = r"""
import json, os, re, sys
sys.path.insert(0, {tests!r}); sys.path.insert(0, {repo!r})
import harness
harness.check_config_path(os.environ["TELERIG_CONFIG"])
from backend import accounts, auth, server
accounts.ensure_initialized(reconcile_mode="skip")
out = {{}}
def login(headers=None, pw="hunter22"):
    r = harness.asgi_request(server.app, "POST", "/api/login", body={{"username": "admin", "password": pw}},
                             headers=headers)
    tok = re.search(r"tr_session_8790=([^;]+)", " ".join(r.header_all("set-cookie"))).group(1)
    return r.status, accounts.token_fp(tok), tok
s, fp_local, tok = login()
out["direct_local"] = [s, fp_local in auth._sudo]
s, fp_remote, _ = login({{"x-forwarded-for": "198.51.100.20"}})
out["remote"] = [s, fp_remote in auth._sudo]
r = harness.asgi_request(server.app, "POST", "/api/me/password", body={{"current": "hunter22", "new": "Sudo#Check-2026"}},
                         headers={{"cookie": "tr_session_8790=" + tok}})
tok2 = re.search(r"tr_session_8790=([^;]+)", " ".join(r.header_all("set-cookie"))).group(1)
out["after_change"] = [r.status, accounts.token_fp(tok2) in auth._sudo, fp_local in auth._sudo]
s, fp_r2, _ = login({{"x-forwarded-for": "198.51.100.20"}}, "Sudo#Check-2026")
out["remote_after_change"] = [s, fp_r2 in auth._sudo]
print("RESULT " + json.dumps(out))
"""


@test("auth")
def t54b_sudo_opens_only_for_real_admin_sessions(ctx):
    """In-process (a child with the scratch env): the sudo window opens at a direct-local login of
    the migrated ADMIN and after the password change, never for its remote (operator) session."""
    tmp = ctx.tmp()
    _v1_config(tmp)
    code = _T54_SUDO.format(tests=str(harness.TESTS), repo=str(harness.REPO))
    r = harness.run_child(["-c", code], tmp, timeout=120)
    line = next((ln for ln in (r.stdout or "").splitlines() if ln.startswith("RESULT ")), None)
    check(r.returncode == 0 and line, f"child failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    res = json.loads(line[len("RESULT "):])
    eq(res, {"direct_local": [200, True], "remote": [200, False], "after_change": [200, True, False],
             "remote_after_change": [200, True]})


# -- T29 / gate body rules ----------------------------------------------------------------------------
@test("auth")
def t29_security_headers_everywhere(ctx):
    srv = ctx.server()
    anon = srv.client()
    port = srv.port
    checks = [("GET /login", anon.get("/login"), True), ("GET /api/auth_status", anon.get("/api/auth_status"), True),
              ("401 /api/me", anon.get("/api/me"), True), ("303 /", anon.get("/"), True),
              ("403 bad Host", anon.raw("GET", "/login", headers={"Host": f"evil.example:{port}"}), False),
              ("403 cross-site POST", anon.post("/api/login", {}, origin="http://evil.example"), False)]
    c = harness.ready_admin(srv)
    checks += [("200 /", c.get("/"), True), ("200 static css", c.get("/static/style.css"), False),
               ("200 /api/status", c.get("/api/status"), True), ("404 /api/nope", c.get("/api/nope"), True)]
    for label, r, nostore in checks:
        missing = [h for h in HDRS if not r.headers.get(h)]
        eq(missing, [], f"{label}: security headers")
        eq(r.headers.get("x-frame-options"), "DENY", label)
        check("frame-ancestors 'none'" in r.headers.get("content-security-policy", ""), label)
        if nostore:
            eq(r.headers.get("cache-control"), "no-store", f"{label}: no-store")


@test("auth")
def gate_json_body_rules(ctx):
    srv = ctx.server()
    c = harness.ready_admin(srv)
    r = c.raw("POST", "/api/connect", body=b"transport=sim", headers={"Origin": c.origin, "Content-Type": "text/plain"})
    eq((r.status, _err(r)), (415, "json_required"), "a non-JSON API body")
    r = c.post("/api/login", b'{"username": "' + b"x" * 70000 + b'"}')
    eq((r.status, _err(r)), (413, "too_large"), "a 70 KB body")
    r = c.post("/api/login", b"[1, 2]")
    eq((r.status, _err(r)), (400, "invalid_json"), "a non-object body")
    r = c.raw("POST", "/api/disconnect", headers={"Origin": c.origin})
    eq(r.status, 200, f"a bodiless POST is allowed {r!r}")
    r = harness.login(srv.client(), "admin", srv.admin_pw, nxt="//evil.example/x")
    eq(r.json().get("next"), "/", "an off-site next is replaced by /")
    r = harness.login(srv.client(), "admin", srv.admin_pw, nxt="/?noconnect")
    eq(r.json().get("next"), "/?noconnect", "a same-site next is kept")
    signed = srv.client()
    harness.login(signed, "admin", srv.admin_pw)
    eq(signed.get("/login?next=%2F%3Fnoconnect").headers.get("location"), "/?noconnect", "/login when signed in")


# -- T51 / T53: generations and revocation -------------------------------------------------------------
@test("auth")
def t51_generations_are_never_reused(ctx):
    tmp = ctx.tmp()
    srv = ctx.server(tmp)
    harness.ready_admin(srv)
    pw = harness.mk_user(tmp, "OP1", "operator")
    c = srv.client()
    eq(harness.login(c, "OP1", pw).status, 200)
    tok = harness.session_cookie(c)
    g1 = harness.read_auth(tmp)["users"]["OP1"]["gen"]
    with c.ws() as ws:
        ws.recv_json(type="hello")
        harness.del_user(tmp, "OP1")
        eq(ws.wait_closed(timeout=3), (4401, "session_revoked"), "OP1's socket when OP1 is deleted")
    eq(old_cookie_client(srv, tok).get("/api/me").status, 401, "deleted user's cookie")
    harness.mk_user(tmp, "OP1", "operator")
    g2 = harness.read_auth(tmp)["users"]["OP1"]["gen"]
    check(g2 > g1, f"re-created gen {g2} <= old gen {g1}")
    time.sleep(1.2)
    eq(old_cookie_client(srv, tok).get("/api/me").status, 401, "the old cookie after re-creation")
    with ws_with_cookie(srv, tok) as ws:
        eq(ws.wait_closed(), (4401, "unauthenticated"), "WS with the old cookie")
    eq(harness.login(srv.client(), "OP1", pw).status, 200, "the re-created account signs in")
    # ADMIN deleted, then restored by --reset-admin from the CLI (a running server sees it within ~1 s)
    a = harness.ready_admin(srv, srv.client())
    atok = harness.session_cookie(a)
    with a.ws() as ws:
        ws.recv_json(type="hello")
        harness.del_user(tmp, "ADMIN")
        eq(ws.wait_closed(timeout=3), (4401, "session_revoked"), "ADMIN's socket when ADMIN is deleted")
    r = harness.run_child(RESET, tmp, input="Restored#Pass-2026\n")
    eq(r.returncode, 0, f"--reset-admin:\n{r.stdout}\n{r.stderr}")
    time.sleep(1.2)
    eq(old_cookie_client(srv, atok).get("/api/me").status, 401, "ADMIN's old cookie after the restore")
    with ws_with_cookie(srv, atok) as ws:
        eq(ws.wait_closed(), (4401, "unauthenticated"), "WS with ADMIN's old cookie")
    eq(harness.login(srv.client(), "admin", "Restored#Pass-2026").status, 200, "the restored ADMIN signs in")


@test("auth")
def t51b_cli_reset_kicks_open_admin_sockets(ctx):
    """I7: a --reset-admin from the CLI bumps ADMIN's generation; the server's watchdog reload
    closes ADMIN's open sockets within about 1 s, with no other traffic."""
    tmp = ctx.tmp()
    srv = ctx.server(tmp)
    c = harness.ready_admin(srv)
    with c.ws() as ws:
        ws.recv_json(type="hello")
        r = harness.run_child(RESET, tmp, input="Kicked#Pass-2026\n")
        eq(r.returncode, 0, r.stdout + r.stderr)
        t0 = time.monotonic()
        eq(ws.wait_closed(timeout=4), (4401, "session_revoked"), "ADMIN's open socket after --reset-admin")
        check(time.monotonic() - t0 < 2.5, f"closed {time.monotonic() - t0:.2f}s after the CLI exited")


@test("auth")
def t53_logout_revokes_the_token(ctx):
    srv = ctx.server()
    c = harness.ready_admin(srv)
    tok = harness.session_cookie(c)
    with c.ws() as ws:
        ws.recv_json(type="hello")
        r = c.post("/api/logout")
        eq(r.status, 200, f"logout {r!r}")
        eq(ws.wait_closed(timeout=3), (4401, "signed_out"), "the session's open socket")
    check(any(x.startswith(f"tr_session_{srv.port}=") and "max-age=0" in x.lower() for x in r.header_all("set-cookie")),
          "cookie not cleared")
    r = c.raw("GET", "/api/me", headers={"Cookie": f"tr_session_{srv.port}={tok}"}, cookies=False)
    eq(r.status, 401, "replaying the old cookie without any Origin (curl-style)")
    with ws_with_cookie(srv, tok) as ws:
        eq(ws.wait_closed(), (4401, "unauthenticated"), "WS with the logged-out cookie")
    check(audit_events(srv.tmp, "logout"), "logout not audited")
    other = harness.ready_admin(srv, srv.client())
    eq(other.get("/api/me").status, 200, "a different session of the same user is unaffected")
    r = other.post("/api/me/signout_all")
    eq(r.status, 200, "sign out everywhere")
    eq(other.get("/api/me").status, 401, "after sign out everywhere")


_SATURATE = """
import time
def fill(d):
    rv = d["meta"]["revoked"]
    now = int(time.time())
    for i in range(accounts.REVOKED_MAX - len(rv)):
        rv[f"{i:016x}"] = now + 86400 * 20 + i
accounts.store().mutate(fill)
result = len(accounts.meta()["revoked"])
"""


@test("auth")
def t53b_full_revocation_list_costs_only_the_signer(ctx):
    """Review A3-R1 (P0): with the revocation list saturated (2000 entries of no known owner, as
    after ~2000 logouts by one account), consecutive logouts bump only the SIGNING-OUT user's gen.
    Every other user keeps their gen, sessions and open sockets, and another user's K2 lock (ADMIN
    guessed through a loopback relay, no K1) is not reset."""
    tmp = ctx.tmp()
    srv = ctx.server(tmp)
    admin = harness.ready_admin(srv)
    op_pw = harness.mk_user(tmp, "OP1", "operator")
    v_pw = harness.mk_user(tmp, "V1", "viewer")
    viewer = srv.client()
    eq(harness.login(viewer, "V1", v_pw).status, 200, "V1 signs in")
    relay = xff("127.0.0.9")
    g = srv.client()
    for i in range(10):
        eq(harness.login(g, "ADMIN", f"guess-{i}", headers=relay).status, 401, f"ADMIN guess {i + 1}")
    eq(harness.login(g, "ADMIN", srv.admin_pw, headers=relay).status, 429, "ADMIN's K2 lock (right password)")
    eq(harness.accounts_child(tmp, _SATURATE), 2000, "saturated")
    time.sleep(harness.STORE_PICKUP)
    before = {n: u["gen"] for n, u in harness.read_auth(tmp)["users"].items()}
    with viewer.ws() as vws:
        vws.recv_json(type="hello")
        c1, c2 = srv.client(), srv.client()
        eq(harness.login(c1, "OP1", op_pw).status, 200, "OP1 device 1")
        eq(harness.login(c2, "OP1", op_pw).status, 200, "OP1 device 2")
        with c1.ws() as ows:
            ows.recv_json(type="hello")
            eq(c1.post("/api/logout").status, 200, "first logout with the list full")
            eq(ows.wait_closed(timeout=3), (4401, "signed_out"), "the signing-out session's socket")
        eq(c2.get("/api/me").status, 401, "OP1's other device is signed out too (fail safe)")
        c3 = srv.client()
        eq(harness.login(c3, "OP1", op_pw).status, 200, "OP1 signs in again")
        eq(c3.post("/api/logout").status, 200, "second consecutive logout with the list full")
        eq(harness.login(g, "ADMIN", srv.admin_pw, headers=relay).status, 429, "ADMIN's K2 lock survives")
        after = {n: u["gen"] for n, u in harness.read_auth(tmp)["users"].items()}
        eq({n: after[n] != before[n] for n in before}, {n: n == "OP1" for n in before}, "only OP1's gen moved")
        eq(wait_closed_code(vws, timeout=1.0), None, "V1's open socket survives both logouts")
    eq(viewer.get("/api/me").status, 200, "V1's session survives")
    eq(admin.get("/api/me").status, 200, "ADMIN's session survives")
    eq(len(harness.read_auth(tmp)["meta"]["revoked"]), 2000, "the list never grows past 2000")
    end = time.monotonic() + 4.0
    while True:                                                  # the audit writer is asynchronous
        hows = [e.get("revocation") for e in audit_events(tmp, "logout") if e.get("user") == "OP1"]
        if len(hows) >= 2 or time.monotonic() > end:
            break
        time.sleep(0.1)
    eq(hows, ["bumped", "bumped"], "both logouts audited as bumps")


# -- T56: login never depends on a store write ---------------------------------------------------------
_HOLD = r"""
import sys, time
sys.path.insert(0, {repo!r})
from backend import accounts
with accounts.store().hold_lock():
    print("HELD", flush=True)
    time.sleep(30)
"""


@test("auth")
def t56_login_while_the_store_lock_is_held(ctx):
    tmp = ctx.tmp()
    srv = ctx.server(tmp)
    harness.ready_admin(srv)
    before = harness.read_auth(tmp)["users"]["ADMIN"]["last_login"]
    env = harness.scratch_env(tmp)
    p = subprocess.Popen([sys.executable, "-c", _HOLD.format(repo=str(harness.REPO))], cwd=str(harness.REPO),
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ctx.cleanup(lambda: (p.kill(), p.wait(timeout=10)))
    eq(p.stdout.readline().strip(), "HELD", "lock holder")
    time.sleep(1.1)                                              # a different last_login second
    t0 = time.monotonic()
    r = harness.login(srv.client(), "admin", srv.admin_pw)
    dt = time.monotonic() - t0
    eq(r.status, 200, f"login while auth.json.lock is held {r!r}")
    check(dt < 2.0, f"login took {dt:.2f}s")
    errs = audit_events(tmp, "store_error", timeout=5)
    check(errs and errs[0].get("op") == "record_login", f"store_error not audited: {errs}")
    eq(harness.read_auth(tmp)["users"]["ADMIN"]["last_login"], before, "last_login skipped while locked")


# -- T57 / T58: Host and Origin --------------------------------------------------------------------------
@test("auth")
def t57_host_allowlist_empty_means_lan_names_only(ctx):
    srv = ctx.server()
    c = srv.client()
    port = srv.port
    for h in ("radio-pc", "radio.local", "pc.tail1.ts.net", "localhost", "127.0.0.1"):
        eq(c.raw("GET", "/login", headers={"Host": f"{h}:{port}"}).status, 200, f"Host {h}")
    r = c.raw("GET", "/login", headers={"Host": f"evil.example:{port}"})
    eq(r.status, 403, "Host evil.example")
    check("Admin → Access" in r.text and "evil.example" in r.text, r.text[:300])
    r = c.raw("GET", "/api/auth_status", headers={"Host": f"evil.example:{port}"})
    eq(r.status, 403, "API with Host evil.example")
    with harness.Client(port).ws(origin=f"http://evil.example:{port}", host_header=f"evil.example:{port}") as ws:
        eq(ws.wait_closed(), (4403, "bad_origin"), "WS with Host evil.example")


@test("auth")
def t58_exact_origin_with_allowlisted_origins(ctx):
    tmp = ctx.tmp()
    (tmp / "config.json").write_text(json.dumps({"allowed_hosts": ["https://pc.tailnet.ts.net", "radio-pc"]}))
    srv = ctx.server(tmp)
    c = harness.ready_admin(srv)
    port = srv.port
    ts = {"Host": "pc.tailnet.ts.net", "Origin": "https://pc.tailnet.ts.net"}
    eq(c.raw("POST", "/api/disconnect", headers=ts).status, 200, "Serve origin + Serve Host (POST)")
    with c.ws(origin="https://pc.tailnet.ts.net", host_header="pc.tailnet.ts.net") as ws:
        eq(ws.recv_json().get("type"), "hello", "Serve origin + Serve Host (WS)")
    bad = dict(ts, Origin="https://pc.tailnet.ts.net:8443")
    eq(c.raw("POST", "/api/disconnect", headers=bad).status, 403, "Serve name on another port (POST)")
    with c.ws(origin="https://pc.tailnet.ts.net:8443", host_header="pc.tailnet.ts.net") as ws:
        eq(ws.wait_closed(), (4403, "bad_origin"), "Serve name on another port (WS)")
    echo = {"Host": f"radio-pc:{port}", "Origin": "http://radio-pc:8077"}
    eq(c.raw("POST", "/api/disconnect", headers=echo).status, 403, "bare allowlisted name, other port (POST)")
    with c.ws(origin="http://radio-pc:8077", host_header=f"radio-pc:{port}") as ws:
        eq(ws.wait_closed(), (4403, "bad_origin"), "bare allowlisted name, other port (WS)")
    same = {"Host": f"radio-pc:{port}", "Origin": f"http://radio-pc:{port}"}
    eq(c.raw("POST", "/api/disconnect", headers=same).status, 200, "radio-pc same host+port")
    # a local TLS proxy reports the external origin: X-Forwarded-Host/Port AND -Proto (round 3: the scheme counts)
    xf = {"X-Forwarded-Host": "radio.lan", "X-Forwarded-Port": "8443", "X-Forwarded-Proto": "https"}
    eq(c.raw("POST", "/api/disconnect", headers=dict(xf, Origin="https://radio.lan:8443")).status, 200,
       "X-Forwarded-Host/Port/Proto from 127.0.0.1 (POST)")
    with c.ws(origin="https://radio.lan:8443", headers=xf) as ws:
        eq(ws.recv_json().get("type"), "hello", "X-Forwarded-Host/Port/Proto from 127.0.0.1 (WS)")
    eq(c.raw("GET", "/login", headers={"Host": f"radio.lan:{port}"}).status, 403,
       "with an allowlist, an unlisted LAN name is refused (v1 rule)")


@test("auth")
def t58b_origin_scheme_must_match_the_request(ctx):
    """Final review round 3: the Origin's scheme must equal the request's EFFECTIVE scheme
    (X-Forwarded-Proto only from a loopback peer, else the connection's own), and its port the
    effective port (the Host's port, else a trusted X-Forwarded-Port, else the default of the
    effective scheme). Before, a Host without a port took the default port from the ORIGIN's scheme
    and the scheme was never compared. No allowlist here: only the exact-origin rule can pass."""
    srv = ctx.server()
    c = harness.ready_admin(srv)
    port = srv.port
    serve = {"Host": "pc.tailnet.ts.net", "X-Forwarded-Proto": "https"}      # what Tailscale Serve sends
    eq(c.raw("POST", "/api/disconnect", headers=dict(serve, Origin="https://pc.tailnet.ts.net")).status, 200,
       "Serve Host + X-Forwarded-Proto https: Origin https://pc.tailnet.ts.net (POST)")
    with c.ws(origin="https://pc.tailnet.ts.net", host_header="pc.tailnet.ts.net",
              headers={"X-Forwarded-Proto": "https"}) as ws:
        eq(ws.recv_json().get("type"), "hello", "... (WS)")
    for bad in ("http://pc.tailnet.ts.net", "https://pc.tailnet.ts.net:8443", "http://pc.tailnet.ts.net:443"):
        r = c.raw("POST", "/api/disconnect", headers=dict(serve, Origin=bad))
        eq(r.status, 403, f"Serve Host + X-Forwarded-Proto https: Origin {bad} (POST)")
        check("https://pc.tailnet.ts.net:443" in r.text, f"the refusal names the effective origin: {r.text[:200]}")
        with c.ws(origin=bad, host_header="pc.tailnet.ts.net", headers={"X-Forwarded-Proto": "https"}) as ws:
            eq(ws.wait_closed(), (4403, "bad_origin"), f"Serve Host + X-Forwarded-Proto https: Origin {bad} (WS)")
    # the plain-http server: an https:// Origin for the very same host and port is not this origin
    https_same = f"https://127.0.0.1:{port}"
    eq(c.raw("POST", "/api/disconnect", headers={"Origin": https_same}).status, 403, f"{https_same} on plain http (POST)")
    with c.ws(origin=https_same) as ws:
        eq(ws.wait_closed(), (4403, "bad_origin"), f"{https_same} on plain http (WS)")
    # a Host without a port on plain http: https://name is refused, http://name (port 80) passes
    eq(c.raw("POST", "/api/disconnect", headers={"Host": "radio-pc", "Origin": "https://radio-pc"}).status, 403,
       "Host radio-pc on plain http: Origin https://radio-pc")
    eq(c.raw("POST", "/api/disconnect", headers={"Host": "radio-pc", "Origin": "http://radio-pc"}).status, 200,
       "Host radio-pc on plain http: Origin http://radio-pc")
    # the cases that passed before still pass
    eq(c.raw("POST", "/api/disconnect", headers={"Origin": f"http://127.0.0.1:{port}"}).status, 200, "same origin")
    with c.ws() as ws:
        eq(ws.recv_json().get("type"), "hello", "same-origin WS")
    v6 = f"[::1]:{port}"
    eq(c.raw("POST", "/api/disconnect", headers={"Host": v6, "Origin": f"http://{v6}"}).status, 200, "IPv6 literal")
