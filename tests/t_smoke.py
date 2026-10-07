"""`smoke` group (step A1): the dev guards on a real server process.

Every server here is harness.Server: run.py --sim-only --no-autoconnect on 127.0.0.1:<free port>
with a scratch config. Serial/LAN attempts target COM99 and 192.0.2.1 (RFC 5737 TEST-NET-1), which
do not exist, and the sim-only guard refuses them before any port or socket is opened.
A3: everyone signs in, so each test first signs in as the scratch ADMIN (harness.ready_admin).
No TX command is sent.
"""
from __future__ import annotations

import json
from pathlib import Path

import harness
from harness import check, eq, test

SERIAL = {"transport": "serial", "port": "COM99", "radio": "ic9700"}
SIM_IC9700 = {"transport": "sim", "radio": "ic9700"}
LAN = {"transport": "lan", "radio": "ic9700", "host": "192.0.2.1", "port": 50001, "user": "x", "password": "y"}


def _state(client) -> dict:
    """The radio state snapshot every new socket receives first."""
    with client.ws() as ws:
        return ws.recv_json(type="state")


def _err(r) -> str:
    j = r.json()
    return j.get("error") if isinstance(j, dict) else None


@test("smoke")
def harness_refuses_real_home_config(ctx):
    home = Path.home()
    before = {d: (home / d).exists() for d in harness.REAL_CONFIG_DIRS}
    for d in (home / ".telerig", home / ".radio-webop"):
        try:
            harness.Server(d).start()
        except harness.Refusing as e:
            check(str(e).startswith("REFUSING"), str(e))
        else:
            raise harness.TestFailure(f"Server accepted a config folder inside {d}")
    eq({d: (home / d).exists() for d in harness.REAL_CONFIG_DIRS}, before, "real config folders touched")


@test("smoke")
def server_starts_on_free_port(ctx):
    srv = ctx.server()
    check(srv.port not in harness.FORBIDDEN_PORTS, f"port {srv.port}")
    for flag in ("--sim-only", "--no-autoconnect", "--no-browser"):
        check(flag in srv.cmd, f"{flag} missing from {srv.cmd}")
    eq(srv.cmd[srv.cmd.index("--host") + 1], "127.0.0.1")
    eq(srv.ready_status, 200, "/login")
    c = srv.client()
    eq(c.get("/api/radios").status, 401, "/api/radios before signing in")
    harness.ready_admin(srv, c)
    r = c.get("/api/radios")
    eq(r.status, 200, "/api/radios")
    check(any(p.get("id") == "ic9700" for p in r.json()["radios"]), "ic9700 profile listed")
    st = _state(c)
    eq(st.get("connected"), False, "radio connected at start")
    log = srv.log_text()
    check("SIM-ONLY" in log and "Auto-connect is OFF" in log, "banner does not show the dev guards:\n" + log)


@test("smoke")
def connect_guards(ctx):
    srv = ctx.server()
    c = harness.ready_admin(srv)

    r = c.post("/api/connect", SERIAL)                                  # A1 verify 2
    eq((r.status, _err(r)), (403, "sim_only"), f"serial COM99 {r!r}")
    r = c.post("/api/connect", {**SERIAL, "auto": True})                # sim-only is checked first
    eq((r.status, _err(r)), (403, "sim_only"), f"serial auto {r!r}")
    r = c.post("/api/connect", LAN)
    eq((r.status, _err(r)), (403, "sim_only"), f"LAN {r!r}")
    r = c.post("/api/connect", {**SIM_IC9700, "transport": "SIM"})     # only the exact "sim" passes
    eq((r.status, _err(r)), (403, "sim_only"), f"'SIM' {r!r}")
    eq(_state(c).get("connected"), False, "refused connects left a radio connected")

    r = c.post("/api/connect", {**SIM_IC9700, "auto": True})           # A1 verify 3
    eq((r.status, _err(r)), (409, "autoconnect_disabled"), f"sim auto {r!r}")
    r = c.post("/api/connect", {**SIM_IC9700, "auto": "false"})        # A4: strict types (auto is a bool)
    eq((r.status, _err(r), r.json().get("field")), (400, "invalid", "auto"), f"sim auto 'false' {r!r}")
    eq(_state(c).get("connected"), False, "auto connect connected a radio")

    for bad in (b"{bad", b"[1,2]", b'"sim"'):                          # not a JSON object
        r = c.post("/api/connect", bad)
        eq((r.status, _err(r)), (400, "invalid_json"), f"body {bad!r} {r!r}")

    r = c.post("/api/connect", SIM_IC9700)                              # A1 verify 4
    eq(r.status, 200, f"explicit sim {r!r}")
    eq((r.json().get("ok"), r.json().get("radio")), (True, "ic9700"))
    st = _state(c)
    eq((st.get("connected"), st.get("transport")), (True, "Simulator"), "after explicit sim connect")

    r = c.post("/api/disconnect", None)                                 # bodiless, like app.js
    eq(r.status, 200, f"disconnect {r!r}")
    eq(_state(c).get("connected"), False, "after disconnect")
    r = c.post("/api/connect", {**SIM_IC9700, "auto": True})
    eq((r.status, _err(r)), (409, "autoconnect_disabled"), "auto after disconnect")
    r = c.post("/api/connect", SIM_IC9700)                              # explicit connects still work
    eq(r.status, 200, f"explicit sim after disconnect {r!r}")
    eq(c.post("/api/disconnect", None).status, 200)
    check(srv.alive(), "server died")



@test("smoke")
def cli_flags_alone_enable_guards(ctx):
    """run.py exports the guards itself (env set before uvicorn.run): no TELERIG_* flags in env."""
    srv = ctx.server(env_flags=False)
    c = harness.ready_admin(srv)
    r = c.post("/api/connect", SERIAL)
    eq((r.status, _err(r)), (403, "sim_only"), f"serial {r!r}")
    r = c.post("/api/connect", {**SIM_IC9700, "auto": True})
    eq((r.status, _err(r)), (409, "autoconnect_disabled"), f"auto {r!r}")


_CHILD = r"""
import json, os, sys
sys.path.insert(0, {tests!r}); sys.path.insert(0, {repo!r})
import harness
harness.check_config_path(os.environ["TELERIG_CONFIG"])        # paranoia: scratch config only
assert os.environ.get("TELERIG_SIM_ONLY") == "1" and "TELERIG_NO_AUTOCONNECT" not in os.environ
from backend import accounts, server
accounts.ensure_initialized(reconcile_mode="skip")
accounts.create_user("OP1", "operator", "Pw-OP1-2026!", must_change=False, created_by="test")
u = accounts.get("OP1")
tok, _ = accounts.make_token(u.name, u.gen)
COOKIE = {{"cookie": "tr_session_8790=" + tok}}           # asgi_request binds server port 8790
out = []
def call(method, path, body=None):
    r = harness.asgi_request(server.app, method, path, body=body, headers=COOKIE)
    out.append([path, r.status, r.json()])
SIM = {{"transport": "sim", "radio": "ic9700"}}
call("POST", "/api/connect", {{"transport": "serial", "port": "COM99", "radio": "ic9700"}})
call("POST", "/api/connect", dict(SIM, auto=True))      # allowed: autoconnect on, not spent
call("POST", "/api/disconnect")                          # explicit disconnect spends it
call("POST", "/api/connect", dict(SIM, auto=True))      # refused now
call("POST", "/api/connect", SIM)                        # explicit still works
call("POST", "/api/disconnect")
print("RESULT " + json.dumps(out))
"""


@test("smoke")
def autoconnect_spent_after_disconnect(ctx):
    """In-process ASGI calls in a child (no listening socket): TELERIG_SIM_ONLY=1 but auto-connect
    left ON, to show that an explicit disconnect spends the boot auto-connect. The child signs in as
    an operator it creates in the scratch store."""
    code = _CHILD.format(tests=str(harness.TESTS), repo=str(harness.REPO))
    r = harness.run_child(["-c", code], ctx.tmp(), timeout=120, sim_only=True, no_autoconnect=False)
    line = next((ln for ln in (r.stdout or "").splitlines() if ln.startswith("RESULT ")), None)
    check(r.returncode == 0 and line, f"child failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    res = json.loads(line[len("RESULT "):])
    got = [(p, s, (j or {}).get("error")) for p, s, j in res]
    eq(got, [("/api/connect", 403, "sim_only"),
             ("/api/connect", 200, None),
             ("/api/disconnect", 200, None),
             ("/api/connect", 409, "autoconnect_disabled"),
             ("/api/connect", 200, None),
             ("/api/disconnect", 200, None)])
