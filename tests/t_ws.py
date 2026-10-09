"""`ws` group (step A2): WebSocket gating and robustness on a real server process.

Every server is harness.Server (run.py --sim-only --no-autoconnect on 127.0.0.1:<free port>, scratch
config). Only the simulator is ever connected. No TX command is sent. A3: the refused-Origin cases
are refused before identity is checked; everything else signs in as the scratch ADMIN first.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import time

import harness
from harness import check, eq, test

SIM = {"transport": "sim", "radio": "ic9700"}


def v1_hash(pw: str) -> str:
    """A v1 config.json password_hash (the v1 algorithm: base64(salt16 + scrypt(n=16384,r=8,p=1)))."""
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    return base64.b64encode(salt + h).decode()


def write_config(tmp, obj: dict) -> None:
    (tmp / "config.json").write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _err(r):
    j = r.json()
    return j.get("error") if isinstance(j, dict) else None


def _connected(client) -> bool:
    with client.ws() as ws:
        return ws.recv_json(type="state").get("connected")


@test("ws")
def t08_unauthenticated_ws_closes_4401(ctx):
    """No session cookie -> the socket is accepted, then closed 4401 (A3: everyone signs in; there
    is no loopback exemption, so plain 127.0.0.1 without a cookie is refused too)."""
    tmp = ctx.tmp()
    write_config(tmp, {"session_secret": "ab" * 32, "password_hash": v1_hash("hunter22"), "behind_proxy": True,
                       "behind_proxy_source": "manual"})
    srv = ctx.server(tmp)
    c = srv.client()
    with c.ws() as ws:                                 # accepted first, then closed: the code arrives
        eq(ws.wait_closed(), (4401, "unauthenticated"), "WS without a session (behind_proxy)")
    eq(c.get("/api/radios").status, 401, "HTTP without a session")
    plain = ctx.server()                               # no proxy flag, no v1 password: still 4401
    pc = plain.client()
    with pc.ws() as ws:
        eq(ws.wait_closed(), (4401, "unauthenticated"), "WS without a session (plain loopback)")
    with pc.ws(headers={"Cookie": "tr_session_%d=v2.QURNSU4.1.9999999999.AAAA" % plain.port}) as ws:
        eq(ws.wait_closed(), (4401, "unauthenticated"), "WS with a forged cookie")
    harness.ready_admin(plain, pc)
    with pc.ws() as ws:
        eq(ws.recv_json(type="state").get("connected"), False, "with a session the socket opens")


@test("ws")
def t09_cross_site_origin_refused(ctx):
    srv = ctx.server()
    c = harness.ready_admin(srv)
    with c.ws(origin="http://evil.example") as ws:
        eq(ws.wait_closed(), (4403, "bad_origin"), "WS from evil.example")
    r = c.post("/api/connect", SIM, origin="http://evil.example")
    eq(r.status, 403, f"POST from evil.example {r!r}")
    check("does not match" in r.text, r.text)
    eq(_connected(c), False, "a refused POST connected the radio")


@test("ws")
def t09b_same_host_other_port_and_missing_origin(ctx):
    srv = ctx.server()
    c = harness.ready_admin(srv)
    other = "http://127.0.0.1:8077"                     # e.g. Echo on the same PC: same host, other port
    with c.ws(origin=other) as ws:
        eq(ws.wait_closed(), (4403, "bad_origin"), "WS from another port")
    r = c.post("/api/connect", SIM, origin=other)
    eq(r.status, 403, f"POST from another port {r!r}")
    with c.ws(origin=None) as ws:                       # no Origin header at all
        eq(ws.wait_closed(), (4403, "bad_origin"), "WS without Origin")
    r = c.post("/api/connect", SIM, origin=None)
    eq(r.status, 403, f"POST without Origin {r!r}")
    r = c.raw("POST", "/api/disconnect", body={})        # http.client: no Origin unless given
    eq(r.status, 403, f"raw POST without Origin {r!r}")
    for o in ("http://localhost:" + str(srv.port), "http://127.0.0.2:" + str(srv.port)):
        r = c.post("/api/connect", SIM, origin=o)       # another host name for the same server
        eq(r.status, 403, f"POST with Origin {o} {r!r}")
    eq(_connected(c), False, "a refused POST connected the radio")
    # GET needs no Origin; the same-origin POST works
    eq(c.get("/api/radios", origin=None).status, 200)
    eq(c.post("/api/connect", SIM).status, 200, "same-origin POST")
    eq(c.post("/api/disconnect", None).status, 200)


@test("ws")
def t37_ipv6_literal_host_and_null_origin(ctx):
    srv = ctx.server()
    c = harness.ready_admin(srv)
    v6 = f"[::1]:{srv.port}"
    with c.ws(origin=f"http://{v6}", host_header=v6) as ws:
        eq(ws.recv_json(type="state").get("type"), "state", "IPv6-literal Host + matching Origin accepted")
    with c.ws(origin=f"http://[::1]:{srv.port + 1}", host_header=v6) as ws:
        eq(ws.wait_closed(), (4403, "bad_origin"), "IPv6 Origin with another port")
    with c.ws(origin="null") as ws:
        eq(ws.wait_closed(), (4403, "bad_origin"), "Origin: null")
    r = c.raw("POST", "/api/disconnect", body={}, headers={"Host": v6, "Origin": f"http://{v6}"})
    eq(r.status, 200, f"POST with [::1] Host + Origin {r!r}")
    r = c.raw("POST", "/api/disconnect", body={}, headers={"Origin": "null"})
    eq(r.status, 403, f"POST with Origin null {r!r}")


@test("ws")
def t10_bad_frames_never_kill_the_socket(ctx):
    srv = ctx.server()
    c = harness.ready_admin(srv)
    eq(c.post("/api/connect", SIM).status, 200, "sim connect")
    with c.ws() as ws:
        st0 = ws.recv_json(type="state")
        eq(st0.get("connected"), True)
        ws.send_text("{bad")
        ws.send_text("[1,2]")
        ws.send_text('"set_freq"')
        ws.send_text(json.dumps({"action": 5}))
        ws.send_text(json.dumps({"hz": 145111000}))
        big = json.dumps({"action": "set_freq", "hz": 145111000, "pad": "x" * 9000})
        check(len(big.encode()) > 9000, "oversized frame")
        ws.send_text(big)
        d = ws.wait_for(lambda m: m.get("type") == "denied", timeout=5)
        eq((d.get("action"), d.get("reason")), ("*", "too_large"), "oversized frame answer")
        time.sleep(0.6)                                  # denials are throttled to 2/s per connection
        ws.send({"action": "host_rx", "on": True, "device": 0})
        d = ws.wait_for(lambda m: m.get("type") == "denied", timeout=5)
        eq((d.get("action"), d.get("reason")), ("host_rx", "unknown_action"), "removed action")
        time.sleep(0.6)
        for _ in range(5):
            ws.send({"action": "no_such_action"})
        first = ws.wait_for(lambda m: m.get("type") == "denied", timeout=5)
        eq(first.get("reason"), "unknown_action")
        extra = []
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            try:
                m = ws.recv_json(timeout=max(0.01, end - time.monotonic()))
            except TimeoutError:
                break
            if m.get("type") == "denied":
                extra.append(m)
        eq(extra, [], "denials are throttled (at most 2/s)")
        # the oversized set_freq was never applied; the socket still works and set_freq does
        with c.ws() as probe:
            check(probe.recv_json(type="state").get("freq") != 145111000, "oversized frame was applied")
        ws.send({"action": "set_freq", "hz": 145222000})
        st = ws.wait_for(lambda m: m.get("type") == "state" and m.get("freq") == 145222000, timeout=5)
        eq(st.get("freq"), 145222000, "set_freq after bad frames")
    eq(c.post("/api/disconnect", None).status, 200)
    check(srv.alive(), "server died")


@test("ws")
def ws_state_hides_lan_address(ctx):
    """Sanitized state (§2.11): the hub never sends a 'LAN <ip>' transport label. The simulator's
    label is unchanged; the LAN case is covered in backend/hub_test.py."""
    srv = ctx.server()
    c = harness.ready_admin(srv)
    eq(c.post("/api/connect", SIM).status, 200)
    with c.ws() as ws:
        eq(ws.recv_json(type="state").get("transport"), "Simulator")
    eq(c.post("/api/disconnect", None).status, 200)


SEC_HDRS = ("x-frame-options", "content-security-policy", "x-content-type-options", "referrer-policy",
            "cross-origin-opener-policy", "permissions-policy")


def raw_upgrade(port: int, path: str, *, origin: str | None = None, cookie: str | None = None,
                timeout: float = 5.0) -> tuple:
    """One raw WebSocket upgrade request (no client library in between). Returns (status, headers with
    lower-case names, body) of the server's HTTP answer; a 101 is closed again at once (no frame sent)."""
    port = harness.check_port(port)
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        lines = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}", "Sec-WebSocket-Version: 13"]
        if origin:
            lines.append(f"Origin: {origin}")
        if cookie:
            lines.append(f"Cookie: {cookie}")
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, body = buf.partition(b"\r\n\r\n")
        first, *rest = head.decode("latin-1").split("\r\n")
        status = int(first.split()[1]) if len(first.split()) > 1 else 0
        hdrs = {}
        for ln in rest:
            k, _, v = ln.partition(":")
            hdrs.setdefault(k.strip().lower(), v.strip())
        if status == 101:
            return status, hdrs, b""
        want = int(hdrs.get("content-length") or 0)
        end = time.monotonic() + timeout
        while len(body) < want and time.monotonic() < end:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            body += chunk
        return status, hdrs, body
    finally:
        s.close()


@test("ws")
def ws_upgrade_off_the_ws_path_is_refused_cleanly(ctx):
    """Final review P2: a WebSocket upgrade to any path but /ws is refused before any route. Before,
    /static/* reached StaticFiles (HTTP-only), which raised: HTTP 500 and a traceback on the console
    for an unauthenticated request, outside the Gate; other paths got a bare 403 without the security
    headers. Now: 404 {"ok": false, "error": "not_found"} with every security header and no-store,
    the same signed in or not, nothing on the console; /ws and plain HTTP to /static are unchanged."""
    srv = ctx.server()
    port = srv.port
    origin = f"http://127.0.0.1:{port}"
    paths = ("/static/app.js", "/static/", "/static/style.css", "/static/vendor/lucide/sprite.svg", "/static/nope.js",
             "/static/../run.py", "/", "/login", "/api/status", "/api/admin/users", "/ws/", "/WS", "/wss", "/nope")

    def sweep(who: str, cookie: str | None) -> None:
        for p in paths:
            for o in (origin, None, "https://evil.example"):
                st, h, body = raw_upgrade(port, p, origin=o, cookie=cookie)
                tag = f"{who}, Origin {o}: WS upgrade {p}"
                eq(st, 404, tag)
                eq([x for x in SEC_HDRS if not h.get(x)], [], f"{tag}: security headers")
                eq(h.get("cache-control"), "no-store", f"{tag}: Cache-Control")
                try:
                    j = json.loads(body.decode("utf-8"))
                except ValueError:
                    j = body[:200]
                eq(j, {"ok": False, "error": "not_found"}, f"{tag}: body")

    sweep("signed out", None)
    c = harness.ready_admin(srv)
    sweep("signed in", c.cookie_header())
    time.sleep(0.3)
    log = srv.log_text()
    for bad in ("Exception in ASGI application", "Traceback", "AssertionError", "500 Internal Server Error"):
        check(bad not in log, f"the server log has {bad!r}:\n{srv.log_tail(30)}")
    check(srv.alive(), "server died")
    # /ws itself and plain HTTP are unchanged
    with c.ws() as ws:
        check(ws.recv_json(type="hello").get("conn") is not None, "signed-in /ws: hello")
    with srv.client().ws() as ws:
        eq(ws.wait_closed(), (4401, "unauthenticated"), "signed-out /ws: still accepted, then 4401")
    eq(raw_upgrade(port, "/ws", origin=origin, cookie=c.cookie_header())[0], 101, "raw upgrade to /ws: 101")
    eq(c.get("/static/app.js").status, 200, "plain HTTP GET /static/app.js")
    eq(srv.client().get("/static/app.js").status, 303, "plain HTTP GET /static/app.js signed out: to /login")
