"""`unit` group: the harness's own safety interlocks, plus every backend/*_test.py module.

None of these tests starts TeleRig. The interlock tests use stub clients and a stub WebSocket, so no
TX command ever reaches anything.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path

import harness
from harness import Refusing, check, eq, test


def _refuses(fn, *a, **kw) -> str:
    try:
        fn(*a, **kw)
    except Refusing as e:
        return str(e)
    raise harness.TestFailure(f"{getattr(fn, '__name__', fn)} did not refuse {a!r} {kw!r}")


# -- ports ----------------------------------------------------------------------------------
@test("unit")
def ports_8700_8722_never_used(ctx):
    for p in (8700, 8722):
        check("REFUSING" in _refuses(harness.check_port, p), f"check_port({p})")
        _refuses(harness.Client, p)
        _refuses(harness.Server, ctx.tmp(f"p{p}"), port=p)      # refused in __init__, never spawned
    for bad in (0, 70000, "8790", True, None):
        _refuses(harness.check_port, bad)
    seen = {harness.free_port() for _ in range(25)}
    check(not seen & harness.FORBIDDEN_PORTS, f"free_port returned a forbidden port: {seen}")


# -- config path under the real home ---------------------------------------------------------
@test("unit")
def config_under_real_home_refused(ctx):
    check(harness.REAL_HOMES, "no real home detected")
    home = Path.home()
    before = {d: (home / d).exists() for d in harness.REAL_CONFIG_DIRS}
    bad = [home / ".telerig" / "config.json", home / ".radio-webop" / "config.json",
           home / ".telerig", home / ".telerig" / "sub" / "config.json",
           home / "x" / ".." / ".telerig" / "config.json",     # traversal resolves into it
           home / "config.json"]                                # config folder = the real home
    if sys.platform == "win32":                                 # case-insensitive filesystem
        bad += [Path(str(home).upper()) / ".TELERIG" / "CONFIG.JSON",
                Path(str(home).lower()) / ".Radio-WebOp" / "config.json"]
    for p in bad:
        check("REFUSING" in _refuses(harness.check_config_path, p), f"accepted {p}")
    # scratch_env / Server / Ctx refuse such folders BEFORE creating anything
    for d in (home / ".telerig", home / ".radio-webop", home / ".telerig" / "deep"):
        _refuses(harness.scratch_env, d)
        _refuses(harness.Server, d)
    _refuses(harness.check_scratch_home, home)
    _refuses(harness.check_scratch_home, home / ".radio-webop" / "home")
    after = {d: (home / d).exists() for d in harness.REAL_CONFIG_DIRS}
    eq(after, before, "real config folders touched")
    # a scratch path (even one under the real home, like the temp folder) is fine
    ok = ctx.tmp("ok")
    eq(harness.check_config_path(ok / "config.json"), Path(harness._norm(ok / "config.json")))
    # the logic is generic: a fake home is protected the same way
    fake = ctx.tmp("fakehome")
    _refuses(harness.check_config_path, fake / ".telerig" / "config.json", homes=(fake,))
    harness.check_config_path(fake / "elsewhere" / "config.json", homes=(fake,))


@test("unit")
def scratch_env_is_isolated(ctx):
    saved = {k: os.environ.get(k) for k in ("RADIO_WEBOP_CONFIG", "TELERIG_CONFIG", "TELERIG_SSL_CERT")}
    os.environ["RADIO_WEBOP_CONFIG"] = str(Path.home() / ".radio-webop" / "config.json")
    os.environ["TELERIG_CONFIG"] = str(Path.home() / ".telerig" / "config.json")
    os.environ["TELERIG_SSL_CERT"] = "nope.pem"
    try:
        t = ctx.tmp()
        env = harness.scratch_env(t)
        check("RADIO_WEBOP_CONFIG" not in env, "RADIO_WEBOP_CONFIG leaked")
        check("TELERIG_SSL_CERT" not in env, "TLS env leaked")
        eq(env["TELERIG_CONFIG"], str(harness.check_config_path(t / "config.json")))
        eq(env["HOME"], env["USERPROFILE"])
        eq(Path(env["HOME"]), Path(harness._norm(t / "home")))
        eq((env["TELERIG_SIM_ONLY"], env["TELERIG_NO_AUTOCONNECT"]), ("1", "1"))
        env2 = harness.scratch_env(t, sim_only=False, no_autoconnect=False)
        check("TELERIG_SIM_ONLY" not in env2 and "TELERIG_NO_AUTOCONNECT" not in env2, "flags leaked")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# -- the TX interlock -------------------------------------------------------------------------
class _StubResp:
    def __init__(self, status, payload):
        self.status, self._p = status, payload

    def json(self):
        return self._p


class _StubClient:
    def __init__(self, status=200, payload=None, port=8790):
        self.port, self.status, self.payload, self.calls = port, status, payload, 0

    def get(self, path):
        assert path == "/api/status"
        self.calls += 1
        return _StubResp(self.status, self.payload)


class _StubConn:
    def __init__(self):
        self.sent = []

    def send(self, m):
        self.sent.append(m)


SIM = {"sim_only": True, "transport_kind": "sim", "connected": True}


@test("unit")
def assert_sim_requires_sim_only_and_sim_transport(ctx):
    eq(harness.assert_sim(_StubClient(payload=SIM)), SIM)
    for status, payload in ((404, None), (200, None), (200, []), (200, {}),
                            (200, {"sim_only": False, "transport_kind": "sim"}),
                            (200, {"sim_only": "true", "transport_kind": "sim"}),
                            (200, {"sim_only": 1, "transport_kind": "sim"}),
                            (200, {"sim_only": True, "transport_kind": "serial"}),
                            (200, {"sim_only": True, "transport_kind": "lan"}),
                            (200, {"sim_only": True, "transport_kind": "none"}),
                            (200, {"sim_only": True})):
        msg = _refuses(harness.assert_sim, _StubClient(status, payload))
        check(msg.startswith("REFUSING: non-simulator transport"), msg)
    _refuses(harness.assert_sim, None)
    _refuses(harness.assert_sim, _StubClient(payload=SIM, port=8700))


@test("unit")
def tx_classification(ctx):
    tx = [{"action": "ptt", "tx": True}, {"action": "ptt", "tx": False}, {"action": " PTT "},
          {"action": "cw_tx", "text": "CQ", "wpm": 20}, {"action": "tune_atu"},
          {"action": "mic", "on": True}, {"action": "rx_func", "name": "vox", "on": True},
          {"action": "rx_func", "name": "VOX", "on": False}, {"action": "rx_func", "name": ["vox"]},
          {"action": "rx_func"}]
    not_tx = [{"action": "set_freq", "hz": 145000000}, {"action": "rx_func", "name": "nb", "on": True},
              {"action": "tx_hb"}, {"action": "tx_stop"}, {"action": "cw_stop"}, {"action": 5},
              [1, 2], None, "ptt", {"tx": True}]
    for c in tx:
        check(harness.is_tx_command(c), f"not classified TX: {c!r}")
    for c in not_tx:
        check(not harness.is_tx_command(c), f"classified TX: {c!r}")


@test("unit")
def tx_sends_always_pass_assert_sim(ctx):
    # not a simulator: every TX-capable send is refused and NOTHING reaches the socket
    bad = _StubClient(payload={"sim_only": True, "transport_kind": "serial"})
    conn = _StubConn()
    ws = harness.WS(conn, bad)
    _refuses(ws.send, {"action": "ptt", "tx": True})
    _refuses(ws.send, {"action": "rx_func", "name": "vox", "on": True})
    _refuses(ws.send, {"action": "mic", "on": True})
    _refuses(ws.send_text, '{"action": "cw_tx", "text": "CQ", "wpm": 20}')
    _refuses(ws.send_text, '{"action":"set_freq","action":"tune_atu"}')   # last key wins, as on the server
    _refuses(ws.send_bytes, b"\x00\x01" * 64)
    eq(conn.sent, [], "a TX command reached the socket")
    eq(bad.calls, 6, "assert_sim not run for each TX send")
    # non-TX sends need no status check
    ws.send({"action": "set_freq", "hz": 145000000})
    ws.send_text("{bad")
    eq(bad.calls, 6)
    eq(len(conn.sent), 2)
    # a simulator: assert_sim runs before EVERY TX command, not once per run
    good = _StubClient(payload=SIM)
    conn2 = _StubConn()
    ws2 = harness.WS(conn2, good)
    for _ in range(3):
        ws2.send({"action": "ptt", "tx": True})
    eq(good.calls, 3)
    eq(len(conn2.sent), 3)
    # a socket with no client to ask cannot send TX at all
    _refuses(harness.WS(_StubConn(), None).send, {"action": "ptt", "tx": True})


@test("unit")
def tx_burst_send_repeated_interlock(ctx):
    """WS.send_repeated (T67's refused burst): assert_sim before the first send and every
    check_every sends; keep_going before every send; a non-simulator refuses before anything."""
    ptt = {"action": "ptt", "tx": True}
    # not a simulator: refused before the first frame
    bad = _StubClient(payload={"sim_only": True, "transport_kind": "serial"})
    conn = _StubConn()
    _refuses(harness.WS(conn, bad).send_repeated, ptt, 10, check_every=5, keep_going=lambda: True)
    eq(conn.sent, [], "a TX burst frame reached the socket before assert_sim")
    # a simulator: one status check per check_every frames, starting with the first
    good = _StubClient(payload=SIM)
    conn = _StubConn()
    eq(harness.WS(conn, good).send_repeated(ptt, 1000, check_every=250, keep_going=lambda: True), 1000)
    eq((len(conn.sent), good.calls), (1000, 4), "1000 frames, assert_sim at 0/250/500/750")

    # the transport stops being a simulator mid-burst: the next check refuses, nothing more is sent
    class Flip(_StubClient):
        def get(self, path):
            r = super().get(path)
            if self.calls >= 2:
                r._p = {"sim_only": True, "transport_kind": "lan"}
            return r
    conn = _StubConn()
    _refuses(harness.WS(conn, Flip(payload=SIM)).send_repeated, ptt, 100, check_every=10, keep_going=lambda: True)
    eq(len(conn.sent), 10, "frames after a failed check")
    # keep_going False stops at once (before the frame), and a TX burst without it is refused
    conn, n = _StubConn(), {"i": 0}

    def kg():
        n["i"] += 1
        return n["i"] <= 3
    try:
        harness.WS(conn, _StubClient(payload=SIM)).send_repeated(ptt, 10, check_every=5, keep_going=kg)
    except harness.TestFailure as e:
        check("after 3 of 10" in str(e), str(e))
    else:
        raise harness.TestFailure("keep_going=False did not stop the burst")
    eq(len(conn.sent), 3)
    for kw in ({}, {"check_every": 0, "keep_going": kg}, {"check_every": 501, "keep_going": kg},
               {"check_every": True, "keep_going": kg}, {"keep_going": kg, "rate": 0}, {"keep_going": kg, "rate": True}):
        try:
            harness.WS(_StubConn(), _StubClient(payload=SIM)).send_repeated(ptt, 5, **kw)
        except harness.HarnessError:
            pass
        else:
            raise harness.TestFailure(f"send_repeated accepted {kw!r}")
    # a non-TX burst needs no status checks; `rate` spreads a burst over time
    c = _StubClient(payload=SIM)
    conn = _StubConn()
    t0 = time.monotonic()
    harness.WS(conn, c).send_repeated({"action": "tx_hb"}, 50, rate=250)
    dt = time.monotonic() - t0
    eq((len(conn.sent), c.calls), (50, 0))
    check(dt >= 49 / 250 - 0.01, f"50 frames at 250/s took {dt:.3f}s")


# -- the close-code accessor (pinned against a stub websockets server, not TeleRig) ------------
@test("unit")
def ws_close_code_accessor(ctx):
    from websockets.sync.server import serve

    def handler(ws):
        ws.send('{"type":"hello"}')
        ws.close(4403, "bad_origin")

    port = harness.free_port()
    srv = serve(handler, "127.0.0.1", port)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    ctx.cleanup(srv.shutdown)
    conn = harness.ws_connect(f"ws://127.0.0.1:{port}/", origin=f"http://127.0.0.1:{port}")
    ws = harness.WS(conn, None)
    eq(ws.recv_json(), {"type": "hello"})
    eq(ws.wait_closed(), (4403, "bad_origin"))
    eq((ws.close_code, ws.close_reason), (4403, "bad_origin"))


# -- backend/*_test.py modules (python -m backend.<name>, scratch env) ------------------------
def _register_backend_module(mod: str) -> None:
    def run(ctx):
        r = harness.run_child(["-m", f"backend.{mod}"], ctx.tmp(), timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        ctx.log(out[-2000:])
        failed = r.returncode != 0 or "SOME FAILED" in out or re.search(r"^FAIL\b", out, re.M)
        check(not failed, f"backend.{mod} exit {r.returncode}:\n{out[-3000:]}")
    harness.REGISTRY.append(("unit", f"backend.{mod}", run))


for _p in sorted((harness.REPO / "backend").glob("*_test.py")):
    _register_backend_module(_p.stem)
