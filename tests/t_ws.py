"""`ws` group (step A2): WebSocket gating and robustness on a real server process.

Every server is harness.Server (run.py --sim-only --no-autoconnect on 127.0.0.1:<free port>, scratch
config). Only the simulator is ever connected. No TX command is sent. A3: the refused-Origin cases
are refused before identity is checked; everything else signs in as the scratch ADMIN first.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
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
