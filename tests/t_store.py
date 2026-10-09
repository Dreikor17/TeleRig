"""`store` group (step A2): fail-closed config (locked mode) and no config I/O at import time.

Servers are harness.Server (sim-only, scratch config). The import-time checks run in harness
children with a scratch HOME/USERPROFILE; the real ~/.telerig and ~/.radio-webop are snapshotted
before and compared after. No TX command is sent.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import harness
from harness import check, eq, test

SIM = {"transport": "sim", "radio": "ic9700"}


def atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".testtmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def wait_status(client, path: str, want: int, timeout: float = 6.0) -> int:
    """Poll `path` with a cookie-less client on the same server (a signed-in client gets 303 from
    /login, A3)."""
    probe = harness.Client(client.port, host=client.host)
    end = time.monotonic() + timeout
    got = None
    while time.monotonic() < end:
        got = probe.get(path).status
        if got == want:
            return got
        time.sleep(0.2)
    return got


def real_config_snapshot() -> dict:
    snap = {}
    for home in harness.REAL_HOMES:
        for sub in harness.REAL_CONFIG_DIRS:
            p = Path(home) / sub
            try:
                st = p.stat()
                snap[str(p)] = (True, st.st_mtime_ns, sorted(os.listdir(p)) if p.is_dir() else None)
            except OSError:
                snap[str(p)] = (False, None, None)
    return snap


@test("store")
def t26a_unparsable_config_locks_the_server(ctx):
    tmp = ctx.tmp()
    cfg = tmp / "config.json"
    bad = b'{"allowed_hosts": ["radio-pc",'
    cfg.write_bytes(bad)
    srv = ctx.server(tmp)
    eq(srv.ready_status, 503, "/login while config.json is unparsable")
    c = srv.client()
    r = c.get("/login")
    eq(r.status, 503, "/login")
    check("locked" in r.text.lower() and "config.json" in r.text, r.text[:300])
    # round 3: with config.json unreadable behind_proxy is unknown, so even this loopback request is
    # not taken for the host PC: the page names the file, never its folder (the console does)
    check(str(tmp).lower() not in r.text.lower(), "the locked page showed the folder: " + r.text[:300])
    eq(r.headers.get("cache-control"), "no-store")
    for path in ("/", "/static/app.js", "/api/radios", "/api/ports"):
        eq(c.get(path).status, 503, path)
    r = c.get("/api/radios")
    eq((r.json() or {}).get("error"), "server_locked", "API body")
    r = c.post("/api/connect", SIM)
    eq((r.status, (r.json() or {}).get("error")), (503, "server_locked"), "POST /api/connect")
    with c.ws() as ws:
        eq(ws.wait_closed(), (4403, "server_locked"), "WS while locked")
    eq(cfg.read_bytes(), bad, "config.json bytes changed")
    log = srv.log_text()
    check("config.json" in log and "--reset-admin" in log, "console does not name the file and the fix:\n" + log)
    names = sorted(p.name for p in tmp.iterdir())
    eq([n for n in names if n not in ("config.json", "home") and not n.startswith("server-")], [],
       "files created while locked")

    eq(sorted(n for n in names if n.startswith("auth")), [], "auth.json seeded while config.json was unparsable")

    # fixing the file unlocks without a restart (and only then is the account store seeded)
    atomic_write(cfg, b"{}")
    eq(wait_status(c, "/login", 200), 200, "unlocked after the fix")
    check((tmp / "auth.json").exists(), "auth.json not seeded after the unlock")
    harness.ready_admin(srv, c)
    eq(c.post("/api/connect", SIM).status, 200, "sim connect after unlock")

    # corrupting it at runtime: open sockets are closed 4403 server_locked, pages get 503
    with c.ws() as ws:
        eq(ws.recv_json(type="state").get("connected"), True)
        atomic_write(cfg, bad)
        eq(ws.wait_closed(timeout=6), (4403, "server_locked"), "open socket when the file broke")
    eq(c.get("/login").status, 503, "/login after a runtime corruption")
    eq(cfg.read_bytes(), bad, "config.json bytes changed after a runtime corruption")
    atomic_write(cfg, b"{}")
    eq(wait_status(c, "/login", 200), 200, "unlocked again")
    eq(c.post("/api/disconnect", None).status, 200)
    check(srv.alive(), "server died")


@test("store")
def t26a_kicked_client_that_ignores_the_close_is_inert(ctx):
    """A2 review fix: a runtime corruption kicks every socket with 4403 server_locked. A client
    that ignores that close frame keeps a CLOSING socket open until the close timeout (about 10 s).
    Nothing it sends in that time may reach the radio, while locked or after the unlock. Only
    set_freq is sent; no TX command."""
    tmp = ctx.tmp()
    cfg = tmp / "config.json"
    srv = ctx.server(tmp)
    c = harness.ready_admin(srv)
    eq(c.post("/api/connect", SIM).status, 200, "sim connect")
    base_hz = 145100000
    with harness.RawWS(c) as raw:
        eq(raw.recv_json(type="state").get("connected"), True, "raw client: first state frame")
        raw.send({"action": "set_freq", "hz": base_hz})          # control: this client really works
        end = time.monotonic() + 5
        while raw.recv_json(type="state").get("freq") != base_hz:
            check(time.monotonic() < end, "the raw client's set_freq was not applied")

        atomic_write(cfg, b'{"broken": ')
        eq(raw.wait_close_frame(timeout=6), (4403, "server_locked"), "close frame after the corruption")
        t_close = time.monotonic()
        sent_locked = 0
        for i in range(6):                                       # never answer the close; keep commanding
            try:
                raw.send({"action": "set_freq", "hz": 145333000 + i})
                raw.send_text("{bad")
                raw.send({"action": "no_such_action"})
            except OSError:
                break
            sent_locked += 1
            time.sleep(0.25)
        check(sent_locked >= 3, f"the server dropped TCP {time.monotonic() - t_close:.1f}s after its close frame "
                                f"({sent_locked} frames sent): this test needs a socket that stays CLOSING")
        eq(c.get("/login").status, 503, "still locked while the kicked client sends")

        atomic_write(cfg, b"{}")
        eq(wait_status(c, "/login", 200), 200, "unlocked after the fix")
        sent_after = 0
        for i in range(3):                                       # unlocking does not revive the socket
            try:
                raw.send({"action": "set_freq", "hz": 145444000 + i})
            except OSError:
                break
            sent_after += 1
            time.sleep(0.2)
        ctx.log(f"frames sent into the closing socket: {sent_locked} while locked, {sent_after} after the "
                f"unlock ({time.monotonic() - t_close:.1f}s after the close frame)")
        time.sleep(0.3)
        with c.ws() as ws:
            eq(ws.recv_json(type="state").get("freq"), base_hz,
               "a frame from the kicked client changed the frequency")
    eq(c.post("/api/disconnect", None).status, 200)
    check(srv.alive(), "server died")


_T36 = r"""
import json, os, sys
from pathlib import Path
real_homes = {real!r}
home = Path(os.environ["USERPROFILE"])
assert "TELERIG_CONFIG" not in os.environ and "RADIO_WEBOP_CONFIG" not in os.environ, "config env leaked"
ph = os.path.normcase(os.path.realpath(str(Path.home())))
assert ph == os.path.normcase(os.path.realpath(str(home))), f"Path.home() is {{ph}}, not the scratch home"
for r in real_homes:
    assert ph != r and not ph.startswith(r.rstrip(os.sep) + os.sep + "."), f"scratch home {{ph}} vs real {{r}}"
sys.path.insert(0, {repo!r})
import backend.server                                     # import only: no lifespan
out = {{"after_import": (home / ".telerig").exists()}}
from backend import accounts, settings
out["after_settings_read"] = (settings.status(), (home / ".telerig").exists())
d = accounts.ensure_initialized()
new, old = home / ".telerig" / "config.json", home / ".radio-webop" / "config.json"
out["dir"] = str(d)
out["identical"] = new.read_bytes() == old.read_bytes()
out["old_kept"] = old.exists()
out["names"] = settings.allowed_hosts()
print("RESULT " + json.dumps(out))
"""


@test("store")
def t36_no_config_io_at_import_then_lazy_copy(ctx):
    before = real_config_snapshot()
    tmp = ctx.tmp()
    home = tmp / "home"
    old = home / ".radio-webop"
    old.mkdir(parents=True)
    payload = json.dumps({"session_secret": "cd" * 32, "allowed_hosts": ["Radio-PC:8700"]}, indent=2).encode()
    (old / "config.json").write_bytes(payload)
    code = _T36.format(real=list(harness.REAL_HOMES), repo=str(harness.REPO))
    r = harness.run_child(["-c", code], tmp, timeout=120, config_env=False)
    line = next((ln for ln in (r.stdout or "").splitlines() if ln.startswith("RESULT ")), None)
    check(r.returncode == 0 and line, f"child failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    res = json.loads(line[len("RESULT "):])
    eq(res["after_import"], False, "importing backend.server created ~/.telerig")
    eq(res["after_settings_read"], ["ok", False], "a settings read before init copied the folder")
    eq(Path(res["dir"]).name, ".telerig", "config dir after ensure_initialized")
    eq(res["identical"], True, ".telerig/config.json is not byte-identical")
    eq(res["old_kept"], True, "the old folder was moved instead of copied")
    eq(res["names"], ["radio-pc"], "settings read from the copied folder (normalised)")
    eq((old / "config.json").read_bytes(), payload, "old config.json changed")
    eq(real_config_snapshot(), before, "the real config folders changed")


_T35 = r"""
import os, sys
sys.path.insert(0, {repo!r})
import backend.server
print("RESULT " + repr(sorted(os.listdir(os.path.dirname(os.environ["TELERIG_CONFIG"])))))
"""


@test("store")
def t35_import_leaves_an_empty_config_dir_empty(ctx):
    before = real_config_snapshot()
    tmp = ctx.tmp()
    r = harness.run_child(["-c", _T35.format(repo=str(harness.REPO))], tmp, timeout=120)
    line = next((ln for ln in (r.stdout or "").splitlines() if ln.startswith("RESULT ")), None)
    check(r.returncode == 0 and line, f"child failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    eq(line, "RESULT ['home']", "import created files in the config folder")
    eq(real_config_snapshot(), before, "the real config folders changed")
