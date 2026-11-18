"""`bridge` group: the WSJT-X bridge (backend/bridge.py) on a real server process.

SAFETY. Every server is harness.Server: run.py --sim-only --no-autoconnect on 127.0.0.1:<free port>,
scratch config. Only the built-in SIMULATOR is ever connected. The bridge listens on a free
loopback port (never rigctld's 4532, which a real rigctld on this PC may own). Every line that can
key over the bridge socket (T 1/2/3, \\set_ptt) goes through Rig.send, which runs
harness.assert_sim(client) first, exactly like harness.WS does for a browser's ptt frame; the real
Hamlib client (B13, only when one is installed) is started only after the same check. /api/status
must say sim_only:true and transport_kind "sim", otherwise the whole run aborts.

What is covered: off by default; the admin API (role, sudo, validation, audit, config.json); frequency
and mode through the policy table (incl. Icom DATA mode and the Yaesu DATA modes); transmit off by
default; one TX owner in both directions (a browser user vs. the bridge, two bridge clients); an
operator's STOP pauses bridge transmit until an operator resumes it; the silence limit; a closed
socket unkeys at once; a radio change drops every client; an HTTP request on the port runs nothing;
the status frames per role; config.json followed without a restart; a port that is in use; the pause
outliving a restart (config.json "bridge_paused"); an admin form that is stale (expect -> 409); a STOP
right after a bridge session; the web port refused as the bridge port; the shutdown order.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import harness
from harness import check, eq, test

BS = chr(92)
BRIDGE = "WSJT-X BRIDGE"
KEYING = re.compile(r"(^|[\s+;|,])(T|" + re.escape(BS) + r"set_ptt)\s+[1-9]")


class Rig:
    """A raw rigctl client on the bridge port."""

    def __init__(self, port: int, client, timeout: float = 3.0):
        self.client = client
        self.s = socket.create_connection(("127.0.0.1", harness.check_port(port)), timeout)

    def send(self, line: str) -> None:
        if KEYING.search(line):
            harness.assert_sim(self.client)          # interlock: simulator only, before every key
        self.s.sendall(line.encode("ascii"))

    def read(self, wait: float = 0.3) -> str:
        self.s.settimeout(wait)
        buf = b""
        while True:
            try:
                d = self.s.recv(65536)
            except socket.timeout:
                return buf.decode("ascii", "replace")
            except OSError:
                return buf.decode("ascii", "replace") + "<EOF>"
            if not d:
                return buf.decode("ascii", "replace") + "<EOF>"
            buf += d

    def ask(self, line: str, wait: float = 0.3) -> str:
        self.send(line)
        return self.read(wait)

    def eof(self, timeout: float = 3.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.read(0.2).endswith("<EOF>"):
                return True
        return False

    def close(self) -> None:
        try:
            self.s.close()
        except OSError:
            pass

    def abort(self) -> None:
        """Drop the TCP connection without a clean close (a crashed program)."""
        try:
            self.s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh" if os.name == "nt" else "ii", 1, 0))
        except OSError:
            pass
        self.close()

    def __enter__(self) -> "Rig":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def setup(ctx, *, users=(("OP1", "operator"), ("V1", "viewer")), radio="ic7300mk2", extra_env=None, connect=True):
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


def bridge_port(srv) -> int:
    for _ in range(50):
        p = harness.free_port()
        if p != srv.port and p != 4532:
            return p
    raise harness.HarnessError("no free bridge port")


def enable(admin, port: int, **over) -> dict:
    cfg = {"enabled": True, "port": port, "listen": "local", "allow": [], "ptt": False, "silence_s": 10}
    cfg.update(over)
    r = admin.put("/api/admin/bridge", cfg)
    eq(r.status, 200, f"PUT /api/admin/bridge {r!r}")
    st = r.json()["status"]
    check(st["listening"] is True and st["port"] == port, f"bridge listening: {st}")
    return st


def bstatus(client) -> dict:
    return client.get("/api/status").json()["bridge"]


def state(client) -> dict:
    with client.ws() as ws:
        return ws.recv_json(type="state")


def owner(client):
    return client.get("/api/status").json()["tx"]["owner"]


def wait_until(fn, timeout=5.0, step=0.1):
    end = time.monotonic() + timeout
    while True:
        v = fn()
        if v or time.monotonic() >= end:
            return v
        time.sleep(step)


def open_ws(client):
    ws = client.ws()
    return ws, ws.recv_json(type="hello")


def audit_wait(tmp, pred, name="audit-tx.log", timeout=4.0):
    return wait_until(lambda: [r for r in harness.audit_records(tmp, name) if pred(r)], timeout=timeout)


def key_ws(ws, seq=1):
    ws.send({"action": "ptt", "tx": True, "seq": seq})
    return ws.wait_for(lambda m: m.get("type") in ("tx", "busy", "denied") and (m.get("req") or {}).get("seq") == seq, timeout=5)


def _err(r):
    j = r.json()
    return j.get("error") if isinstance(j, dict) else None


# -- B01: off by default; the admin API ---------------------------------------------------------------------
@test("bridge")
def b01_off_by_default_and_admin_api(ctx):
    srv, cl = setup(ctx, extra_env={"TELERIG_TEST_SUDO_TTL": "3"})
    admin, op, v = cl["ADMIN"], cl["OP1"], cl["V1"]
    b = bstatus(admin)
    eq((b["enabled"], b["listening"], b["ptt"], b["clients"], b["paused"]), (False, False, False, 0, None), "B01 a new install: off")
    eq(sorted(set(bstatus(op)) - set(bstatus(v))), ["error_text", "peers"], "B01 a viewer gets the summary only")
    r = admin.get("/api/admin/bridge")
    eq((r.status, r.json()["config"]), (200, {"enabled": False, "port": 4532, "listen": "local", "allow": [],
                                               "ptt": False, "silence_s": 10}), "B01 GET: the defaults")
    eq(r.json()["web_port"], srv.port, "B01 GET names the web port")
    for c, who in ((op, "operator"), (v, "viewer")):
        eq((c.get("/api/admin/bridge").status, c.put("/api/admin/bridge", {"enabled": True}).status), (403, 403), f"B01 {who}: admin only")
    port = bridge_port(srv)
    eq(admin.post("/api/admin/reauth", {"password": srv.admin_pw}).status, 200, "B01 reauth (3 s here)")
    for body, field in (({"enabled": "yes"}, "enabled"), ({"enabled": True, "port": 80}, "port"),
                        ({"enabled": True, "port": srv.port}, "port"), ({"enabled": True, "port": port, "listen": "network", "allow": []}, "allow"),
                        ({"enabled": True, "port": port, "allow": ["nope"]}, "allow"), ({"enabled": True, "port": port, "silence_s": 1}, "silence_s"),
                        ({"enabled": True, "port": port, "ptt": 1}, "ptt")):
        r = admin.put("/api/admin/bridge", body)
        eq((r.status, _err(r), r.json().get("field")), (400, "invalid", field), f"B01 PUT {body}")
    eq(bstatus(admin)["enabled"], False, "B01 a refused save changes nothing")
    time.sleep(3.6)                                   # the sudo window (3 s here) has closed
    r = admin.put("/api/admin/bridge", {"enabled": True, "port": port})
    eq((r.status, _err(r)), (403, "reauth_required"), "B01 a save needs a fresh password confirmation")
    eq(bstatus(admin)["enabled"], False, "B01 ... and nothing changed")
    eq(admin.post("/api/admin/reauth", {"password": srv.admin_pw}).status, 200, "B01 reauth")
    st = enable(admin, port)
    eq((st["enabled"], st["ptt"], st["listen"], st["error"]), (True, False, "local", False), "B01 enabled, receive-only")
    saved = json.loads(srv.config_path.read_text("utf-8"))["bridge"]
    eq(saved, {"enabled": True, "port": port, "listen": "local", "ptt": False, "silence_s": 10, "allow": []}, "B01 config.json holds the settings")
    recs = audit_wait(srv.tmp, lambda r: r.get("ev") == "bridge_config")
    check(recs and recs[-1].get("by") == "ADMIN" and recs[-1].get("enabled") is True and recs[-1].get("ptt") is False,
          f"B01 the save is in the TX log: {recs}")
    with Rig(port, admin) as rig:
        eq(rig.ask("f\n"), f"{state(v)['freq']}\n", "B01 the bridge answers")
        eq(rig.ask("T 1\n"), "RPRT -22\n", "B01 transmit is off by default: T 1 is refused")
        eq((owner(admin), state(v)["ptt"]), (None, False), "B01 ... and the radio is not keyed")
    admin.post("/api/admin/reauth", {"password": srv.admin_pw})
    r = admin.put("/api/admin/bridge", {"enabled": False, "port": port})
    eq((r.status, r.json()["status"]["listening"]), (200, False), "B01 disabled again")
    try:
        socket.create_connection(("127.0.0.1", port), 1.0).close()
        is_open = True
    except OSError:
        is_open = False
    eq(is_open, False, "B01 the port is closed when the bridge is off")


# -- B02: frequency and mode, Icom DATA mode, Yaesu DATA modes, a radio change ----------------------------
@test("bridge")
def b02_commands_modes_and_radio_change(ctx):
    srv, cl = setup(ctx)
    admin, v = cl["ADMIN"], cl["V1"]
    port = bridge_port(srv)
    enable(admin, port)
    rig = Rig(port, admin)
    eq(rig.ask(BS + "get_powerstat\n"), "1\n", "B02 powerstat")
    eq(rig.ask(BS + "chk_vfo\n"), "0\n", "B02 chk_vfo")
    d = rig.ask(BS + "dump_state\n", 0.5)
    check(d.startswith("1\n3094\n0\n") and d.endswith("\ndone\n") and "ptt_type=0x0\n" in d, f"B02 dump_state (IC-7300MK2, no PTT): {d[:60]!r}")
    eq(rig.ask("F 7074000.000000\n"), "RPRT 0\n", "B02 F")
    eq((rig.ask("f\n"), state(v)["freq"]), ("7074000\n", 7074000), "B02 the simulator radio moved")
    eq(rig.ask("M PKTUSB -1\n"), "RPRT 0\n", "B02 M PKTUSB")
    s = state(v)
    eq((rig.ask("m\n").split("\n")[0], s["mode_name"], s["data"]), ("PKTUSB", "USB", 1), "B02 Icom: USB + DATA on")
    eq(rig.ask("M USB 0\n"), "RPRT 0\n", "B02 M USB")
    s = state(v)
    eq((s["mode_name"], s["data"]), ("USB", 0), "B02 plain USB: DATA off")
    eq(rig.ask("M PKTLSB 0\n"), "RPRT 0\n", "B02 M PKTLSB")
    time.sleep(1.6)                                   # the radio's own poll must agree (not just our guess)
    s = state(v)
    eq((s["mode_name"], s["data"], rig.ask("m\n").split("\n")[0]), ("LSB", 1, "PKTLSB"), "B02 LSB-D confirmed by the radio's poll")
    eq(rig.ask("M CW 500\n"), "RPRT 0\n", "B02 M CW")
    time.sleep(1.6)
    s = state(v)
    eq((s["mode_name"], s["data"]), ("CW", 0), "B02 CW has no DATA variant")
    eq(rig.ask("M C4FM 0\n"), "RPRT -1\n", "B02 a mode this radio lacks")
    eq(rig.ask("S 1 VFOB\n"), "RPRT -11\n", "B02 split TX on VFO B is not offered")
    eq(rig.ask("L RFPOWER 0.5\n"), "RPRT 0\n", "B02 L RFPOWER")
    eq(wait_until(lambda: state(v)["rfpwr"] in (127, 128), 3.0), True, "B02 the power level reached the radio")
    with open_ws(cl["OP1"])[0] as w:
        rig.ask("F 7075000\n")
        st = w.wait_for(lambda m: m.get("type") == "state" and m.get("freq") == 7075000, timeout=5)
        eq(st["freq"], 7075000, "B02 browsers see the bridge's tuning as it happens")

    # a radio change drops the client; the new radio answers with its own capabilities
    r = admin.post("/api/connect", {"transport": "sim", "radio": "ft991a"})
    eq(r.status, 200, f"B02 switch to the FT-991A simulator {r!r}")
    check(rig.eof(3.0), "B02 the radio change closed the bridge connection")
    rig.close()
    harness.assert_sim(admin)
    rig = Rig(port, admin)
    d = rig.ask(BS + "dump_state\n", 0.5)
    check(d.startswith("1\n1035\n0\n") and d.endswith("\ndone\n"), f"B02 dump_state now names the FT-991A: {d[:40]!r}")
    eq(rig.ask("M PKTUSB 0\n"), "RPRT 0\n", "B02 Yaesu M PKTUSB")
    eq((state(v)["mode_name"], rig.ask("m\n").split("\n")[0]), ("DATA-USB", "PKTUSB"), "B02 Yaesu: DATA-USB is a mode of its own")
    eq(rig.ask("M CW 0\n"), "RPRT 0\n", "B02 Yaesu M CW")
    eq((state(v)["mode_name"], rig.ask("m\n").split("\n")[0]), ("CW-USB", "CW"), "B02 Yaesu CW = CW-USB")
    eq(rig.ask("M PKTFM 0\n"), "RPRT 0\n", "B02 Yaesu M PKTFM")
    eq(state(v)["mode_name"], "DATA-FM", "B02 Yaesu DATA-FM")
    eq(rig.ask("q\n", 0.5), "RPRT 0\n<EOF>", "B02 quit")
    rig.close()

    # no radio: the bridge has nothing to offer
    eq(admin.post("/api/disconnect", {}).status, 200, "B02 disconnect")
    with Rig(port, admin) as rig:
        check(rig.eof(2.0), "B02 with no radio a connection is closed at once")
    eq(bstatus(admin)["clients"], 0, "B02 nobody is connected")


# -- B03: transmit through the arbiter ----------------------------------------------------------------------
@test("bridge")
def b03_ptt_through_the_arbiter(ctx):
    srv, cl = setup(ctx)
    admin, op, v = cl["ADMIN"], cl["OP1"], cl["V1"]
    port = bridge_port(srv)
    enable(admin, port, ptt=True)
    rig = Rig(port, admin)
    check("ptt_type=0x1\n" in rig.ask(BS + "dump_state\n", 0.5), "B03 the capability dump advertises CAT PTT")
    wv, hello = open_ws(v)
    eq(hello["status"]["bridge"]["clients"], 1, "B03 hello carries the bridge status")
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B03 T 1")
    f = wv.wait_for(lambda m: m.get("type") == "tx" and m.get("owner"), timeout=5)
    eq((f["owner"]["user"], f["owner"]["claims"], f["owner"]["external"]), (BRIDGE, ["ptt"], False), "B03 everyone sees who is on the air")
    o = owner(admin)
    b = bstatus(admin)
    eq((o["user"], state(v)["ptt"], rig.ask("t\n"), b["on_air"], b["peers"][0]["tx"]), (BRIDGE, True, "1\n", True, True), "B03 keyed by the bridge")
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B03 a second T 1 is a no-op")
    eq(rig.ask("T 0\n"), "RPRT 0\n", "B03 T 0")
    f = wv.wait_for(lambda m: m.get("type") == "tx" and m.get("owner") is None, timeout=5)
    eq((owner(admin), state(v)["ptt"], rig.ask("t\n")), (None, False, "0\n"), "B03 released by the bridge")
    for cmd in ("T 2\n", "T 3\n", BS + "set_ptt 1\n", "+T 1\n"):
        got = rig.ask(cmd)
        check(got.endswith("RPRT 0\n"), f"B03 {cmd!r} keys: {got!r}")
        eq((owner(admin) or {}).get("user"), BRIDGE, f"B03 {cmd!r}: owned")
        rig.ask("T 0\n")
        eq(wait_until(lambda: owner(admin) is None, 3.0), True, f"B03 {cmd!r}: released")
        time.sleep(0.2)
    recs = audit_wait(srv.tmp, lambda r: r.get("ev") in ("tx_start", "tx_end") and r.get("user") == BRIDGE)
    starts = [r for r in recs if r["ev"] == "tx_start"]
    ends = [r for r in recs if r["ev"] == "tx_end"]
    check(len(starts) >= 5 and len(ends) >= 5, f"B03 every bridge transmission is in the TX log: {len(starts)} / {len(ends)}")
    eq((starts[0]["ip"], starts[0]["kind"], ends[0]["reason"]), ("127.0.0.1", "ptt", "owner"), "B03 ... with the peer address")
    wv.close()
    rig.close()


# -- B04: one TX owner, both directions; two bridge clients ------------------------------------------------
@test("bridge")
def b04_one_owner_both_ways(ctx):
    srv, cl = setup(ctx)
    admin, op = cl["ADMIN"], cl["OP1"]
    port = bridge_port(srv)
    enable(admin, port, ptt=True)
    rig = Rig(port, admin)
    f0 = int(rig.ask("f\n"))
    w, _ = open_ws(op)
    eq(key_ws(w)["type"], "tx", "B04 OP1 keys from a browser")
    eq(owner(admin)["user"], "OP1", "B04 OP1 owns TX")
    eq(rig.ask("T 1\n"), "RPRT -9\n", "B04 the bridge cannot key over OP1")
    eq(rig.ask(f"F {f0 + 1000}\n"), "RPRT -9\n", "B04 ... nor retune under OP1")
    eq(rig.ask("M CW 0\n"), "RPRT -9\n", "B04 ... nor change the mode")
    eq((rig.ask("f\n"), rig.ask("t\n")), (f"{f0}\n", "1\n"), "B04 reading still works, and shows the radio keyed")
    eq(rig.ask("T 0\n"), "RPRT 0\n", "B04 the bridge's T 0 answers OK ...")
    eq((owner(admin)["user"], state(admin)["ptt"]), ("OP1", True), "B04 ... but never unkeys OP1")
    w.send({"action": "ptt", "tx": False})
    eq(wait_until(lambda: owner(admin) is None, 3.0), True, "B04 OP1 unkeys")
    time.sleep(0.3)

    eq(rig.ask("T 1\n"), "RPRT 0\n", "B04 now the bridge keys")
    eq(key_ws(w, seq=2)["type"], "busy", "B04 OP1 gets busy while the bridge transmits")
    time.sleep(0.6)
    w.send({"action": "set_freq", "hz": f0 + 2000})
    busy = w.wait_for(lambda m: m.get("type") == "busy", timeout=5)
    eq((busy["action"], busy["owner"]), ("set_freq", BRIDGE), "B04 OP1 cannot retune under the bridge")
    time.sleep(0.6)
    w.send({"action": "ptt", "tx": False})
    w.wait_for(lambda m: m.get("type") == "busy", timeout=5)
    eq((owner(admin)["user"], rig.ask("f\n")), (BRIDGE, f"{f0}\n"), "B04 OP1's unkey never ends the bridge's TX")
    eq(rig.ask(f"F {f0 + 500}\n"), "RPRT 0\n", "B04 the owner itself may retune (WSJT-X shifts the dial while keyed)")

    rig2 = Rig(port, admin)
    eq(rig2.ask("T 1\n"), "RPRT -9\n", "B04 a second bridge client cannot key over the first")
    eq(rig2.ask(f"F {f0 + 3000}\n"), "RPRT -9\n", "B04 ... nor retune under it")
    eq(rig2.ask("T 0\n"), "RPRT 0\n", "B04 ... and its T 0 changes nothing")
    eq(owner(admin)["user"], BRIDGE, "B04 the first client still transmits")
    eq(bstatus(admin)["clients"], 2, "B04 two clients")
    rig.close()                                       # the transmitting client goes away
    eq(wait_until(lambda: owner(admin) is None, 3.0), True, "B04 its socket closing unkeys at once")
    time.sleep(2.3)                                   # the arbiter's release grace
    eq(rig2.ask("T 1\n"), "RPRT 0\n", "B04 now the second client may key")
    eq(rig2.ask("T 0\n"), "RPRT 0\n", "B04 and unkey")
    rig2.close()
    w.close()


# -- B05: STOP pauses bridge transmit until an operator resumes it -----------------------------------------
@test("bridge")
def b05_stop_pauses_until_resumed(ctx):
    srv, cl = setup(ctx)
    admin, op, v = cl["ADMIN"], cl["OP1"], cl["V1"]
    port = bridge_port(srv)
    enable(admin, port, ptt=True)
    rig = Rig(port, admin)
    w, _ = open_ws(op)
    wv, _ = open_ws(v)
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B05 keyed by the bridge")
    wv.send({"action": "tx_stop"})
    d = wv.wait_for(lambda m: m.get("type") == "denied", timeout=5)
    eq((d["action"], d["reason"], owner(admin)["user"]), ("tx_stop", "role", BRIDGE), "B05 a viewer cannot stop it")
    w.send({"action": "tx_stop"})
    eq(wait_until(lambda: owner(admin) is None, 3.0), True, "B05 an operator's STOP unkeys the bridge")
    f = w.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"].get("paused"), timeout=5)
    eq(f["bridge"]["paused"], {"reason": "stopped", "by": "OP1"}, "B05 operators are told it is paused, and by whom")
    eq((state(v)["ptt"], rig.ask("t\n")), (False, "0\n"), "B05 the radio is unkeyed")
    time.sleep(2.3)                                   # past the release grace: only the pause holds it now
    eq(rig.ask("T 1\n"), "RPRT -22\n", "B05 the program cannot key again on its own")
    eq((owner(admin), bstatus(v)["paused"]), (None, {"reason": "stopped", "by": "OP1"}), "B05 ... and a viewer sees the pause too")
    eq(rig.ask("F 7074000\n"), "RPRT 0\n", "B05 tuning still works while paused")
    time.sleep(0.6)
    wv.send({"action": "bridge_tx", "on": True})
    d = wv.wait_for(lambda m: m.get("type") == "denied", timeout=5)
    eq((d["action"], d["reason"], bstatus(admin)["paused"] is not None), ("bridge_tx", "role", True), "B05 a viewer cannot resume it")
    w.send({"action": "bridge_tx", "on": True})
    f = w.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"].get("paused") is None, timeout=5)
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B05 after an operator resumes, the program keys again")
    eq(owner(admin)["user"], BRIDGE, "B05 keyed")

    # the hold switch: pausing while it transmits stops it, like STOP
    w.send({"action": "bridge_tx", "on": False})
    eq(wait_until(lambda: owner(admin) is None, 3.0), True, "B05 pausing during a transmission stops it")
    eq(bstatus(admin)["paused"], {"reason": "stopped", "by": "OP1"}, "B05 ... and holds")
    w.send({"action": "bridge_tx", "on": True})
    w.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"].get("paused") is None, timeout=5)
    w.send({"action": "bridge_tx", "on": False})       # pausing while idle
    f = w.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"].get("paused"), timeout=5)
    eq(f["bridge"]["paused"], {"reason": "operator", "by": "OP1"}, "B05 an operator can hold it while idle")
    eq(rig.ask("T 1\n"), "RPRT -22\n", "B05 held")
    w.send({"action": "bridge_tx", "on": True})
    w.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"].get("paused") is None, timeout=5)

    # the HTTP STOP does the same
    time.sleep(2.3)
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B05 keyed again")
    r = op.post("/api/tx_stop", {})
    eq((r.status, r.json().get("stopped")), (200, True), "B05 POST /api/tx_stop")
    eq((owner(admin), bstatus(admin)["paused"]), (None, {"reason": "stopped", "by": "OP1"}), "B05 stopped and paused over HTTP too")
    recs = audit_wait(srv.tmp, lambda r: r.get("ev") in ("bridge_tx_paused", "bridge_tx_resumed", "tx_stop_forced"))
    kinds = [r["ev"] for r in recs]
    check(kinds.count("bridge_tx_paused") >= 4 and kinds.count("bridge_tx_resumed") >= 3 and kinds.count("tx_stop_forced") >= 3,
          f"B05 pauses, resumes and forced stops are in the TX log: {kinds}")
    ends = [r for r in harness.audit_records(srv.tmp) if r.get("ev") == "tx_end" and r.get("user") == BRIDGE]
    eq({r["reason"] for r in ends}, {"stopped"}, "B05 each transmission ended as `stopped`")
    # switching transmit off and on again in Administration clears a pause
    st = enable(admin, port, ptt=False)
    eq(st["paused"], {"reason": "stopped", "by": "OP1"}, "B05 switching transmit off keeps the pause (it is sticky)")
    st = enable(admin, port, ptt=True)
    eq(st["paused"], None, "B05 transmit off -> on starts unpaused")
    recs = audit_wait(srv.tmp, lambda r: r.get("ev") == "bridge_tx_resumed" and r.get("by") == "ADMIN")
    check(recs, "B05 ... audited as a resume by the admin")
    w.close()
    wv.close()
    rig.close()


# -- B06: the silence limit; B07: a closed socket -----------------------------------------------------------
@test("bridge")
def b06_silence_limit_and_socket_close(ctx):
    srv, cl = setup(ctx)
    admin = cl["ADMIN"]
    port = bridge_port(srv)
    enable(admin, port, ptt=True, silence_s=3)
    rig = Rig(port, admin)
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B06 keyed")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 5.0:                # a program that keeps polling stays keyed
        time.sleep(0.8)
        eq(rig.ask("f\n", 0.2) != "", True, "B06 poll")
    t1 = time.monotonic()                             # its last command; now the program goes silent
    eq((owner(admin) or {}).get("user"), BRIDGE, "B06 still keyed after 5 s of polling (the limit is 3 s of SILENCE)")
    eq(wait_until(lambda: owner(admin) is None, 8.0, 0.1), True, "B06 a silent program is unkeyed")
    took = time.monotonic() - t1
    check(2.4 <= took <= 4.8, f"B06 ... 3 s after its last command, not the 6 s default ({took:.1f} s)")
    eq((state(admin)["ptt"], bstatus(admin)["paused"]), (False, {"reason": "heartbeat", "by": None}), "B06 unkeyed and paused")
    recs = audit_wait(srv.tmp, lambda r: r.get("ev") in ("tx_hb_timeout", "tx_end") and r.get("user") == BRIDGE)
    eq([r["ev"] for r in recs], ["tx_hb_timeout", "tx_end"], "B06 logged")
    eq((recs[-1]["reason"], 2.5 <= recs[0]["silent_s"] <= 4.5), ("heartbeat", True), f"B06 reason + measured silence {recs[0]}")
    eq(rig.ask("T 1\n"), "RPRT -22\n", "B06 it cannot simply key again")
    rig.close()

    # a LONGER limit than the 6 s default is honoured too (a program that polls slowly)
    enable(admin, port, ptt=True, silence_s=9)
    with open_ws(admin)[0] as w:
        w.send({"action": "bridge_tx", "on": True})
        w.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"].get("paused") is None, timeout=5)
    rig = Rig(port, admin)
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B06 keyed (limit 9 s)")
    time.sleep(7.5)
    eq((owner(admin) or {}).get("user"), BRIDGE, "B06 7.5 s of silence under a 9 s limit: still keyed")
    eq(rig.ask("T 0\n"), "RPRT 0\n", "B06 unkeyed by the program")
    eq(wait_until(lambda: owner(admin) is None, 3.0), True, "B06 released")
    rig.close()
    enable(admin, port, ptt=True, silence_s=3)

    # B07: the program dies while keyed -> unkeyed at once, and NOT paused (nothing was forced)
    w, _ = open_ws(admin)
    w.send({"action": "bridge_tx", "on": True})
    w.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"].get("paused") is None, timeout=5)
    time.sleep(2.3)
    rig = Rig(port, admin)
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B07 keyed")
    t1 = time.monotonic()
    rig.abort()
    eq(wait_until(lambda: owner(admin) is None, 3.0, 0.05), True, "B07 the socket closing unkeys")
    check(time.monotonic() - t1 < 1.5, f"B07 ... at once ({time.monotonic() - t1:.2f} s)")
    eq((state(admin)["ptt"], bstatus(admin)["paused"], bstatus(admin)["clients"]), (False, None, 0), "B07 unkeyed, not paused")
    ends = [r for r in audit_wait(srv.tmp, lambda r: r.get("ev") == "tx_end" and r.get("user") == BRIDGE and r.get("reason") == "disconnect")]
    eq(len(ends), 1, "B07 logged as a disconnect")
    eq(bstatus(admin)["paused"], None, "B07 still not paused")
    w.close()


# -- B08: a radio change / disconnect while the bridge transmits -------------------------------------------
@test("bridge")
def b08_radio_change_while_keyed(ctx):
    srv, cl = setup(ctx)
    admin, op = cl["ADMIN"], cl["OP1"]
    port = bridge_port(srv)
    enable(admin, port, ptt=True)
    rig = Rig(port, admin)
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B08 keyed")
    r = op.post("/api/disconnect", {"confirm": True})
    eq((r.status, _err(r), r.json().get("owner")), (409, "busy", BRIDGE), "B08 an operator cannot disconnect under the bridge's TX")
    r = admin.post("/api/connect", {"transport": "sim", "radio": "ic9700"})
    eq((r.status, _err(r)), (409, "busy"), "B08 an admin needs force")
    eq(owner(admin)["user"], BRIDGE, "B08 still keyed")
    r = admin.post("/api/connect", {"transport": "sim", "radio": "ic9700", "force": True})
    eq(r.status, 200, f"B08 forced switch {r!r}")
    check(rig.eof(3.0), "B08 the bridge connection was closed")
    harness.assert_sim(admin)
    eq((owner(admin), state(admin)["ptt"], state(admin)["radio"], bstatus(admin)["clients"]), (None, False, "ic9700", 0), "B08 the new radio is not keyed")
    eq(bstatus(admin)["paused"], {"reason": "admin_switch", "by": "ADMIN"},
       "B08 a forced switch under the bridge's TX pauses bridge transmit (the program would key the new radio on its own)")
    rig.close()
    time.sleep(2.3)
    rig = Rig(port, admin)
    check(rig.ask(BS + "dump_state\n", 0.5).startswith("1\n3081\n"), "B08 a new connection sees the IC-9700")
    eq(rig.ask("T 1\n"), "RPRT -22\n", "B08 ... and cannot key it until an operator resumes")
    w, _ = open_ws(op)
    w.send({"action": "bridge_tx", "on": True})
    w.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"].get("paused") is None, timeout=5)
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B08 keyed on the new radio after the resume")
    eq(admin.post("/api/disconnect", {"force": True}).status, 200, "B08 forced disconnect")
    check(rig.eof(3.0), "B08 disconnecting closes the bridge connection")
    eq((owner(admin), state(admin)["ptt"]), (None, False), "B08 nothing left keyed")
    eq((bstatus(admin)["paused"] or {}).get("reason"), "admin_switch", "B08 a forced disconnect under it pauses too")
    w.close()
    rig.close()


# -- B09: a browser (HTTP) on the bridge port ---------------------------------------------------------------
@test("bridge")
def b09_http_on_the_port_runs_nothing(ctx):
    srv, cl = setup(ctx)
    admin = cl["ADMIN"]
    port = bridge_port(srv)
    enable(admin, port, ptt=True)
    harness.assert_sim(admin)                         # the requests below carry "T 1" (they must not key)
    s = socket.create_connection(("127.0.0.1", port), 3)
    s.sendall(b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: text/plain\r\nContent-Length: 4\r\n\r\nT 1\n")
    s.settimeout(2.0)
    try:
        got = s.recv(100)
    except OSError:
        got = b""
    s.close()
    eq(got, b"", "B09 an HTTP request gets no answer")
    eq((owner(admin), state(admin)["ptt"]), (None, False), "B09 ... and keys nothing")
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=b"T 1\nT 1\n", method="POST",
                                     headers={"Content-Type": "text/plain"})
        urllib.request.urlopen(req, timeout=3).read()
        answered = True
    except (urllib.error.URLError, OSError, ValueError):
        answered = False
    eq(answered, False, "B09 a real HTTP client is dropped")
    eq((owner(admin), state(admin)["ptt"], bstatus(admin)["clients"]), (None, False, 0), "B09 nothing keyed, nobody connected")
    harness.assert_sim(admin)
    with Rig(port, admin) as rig:
        eq(rig.ask("f\n") != "", True, "B09 a rigctl client still works")
        s2 = rig.s
        s2.sendall(b"GET /x HTTP/1.1\r\nT 1\n")
        check(rig.eof(3.0), "B09 an HTTP line inside a session closes it")
    eq(owner(admin), None, "B09 ... without keying")
    recs = wait_until(lambda: [r for r in harness.audit_records(srv.tmp, "audit.log") if r.get("ev") == "bridge_refused"], 2.0)
    check(recs is not None, "B09 (the refusals are coalesced into the security log)")


# -- B10: the status frames, per role; config.json followed without a restart; a port in use ---------------
@test("bridge")
def b10_frames_config_follow_port_in_use(ctx):
    srv, cl = setup(ctx)
    admin, op, v = cl["ADMIN"], cl["OP1"], cl["V1"]
    port = bridge_port(srv)
    wo, ho = open_ws(op)
    wv, hv = open_ws(v)
    eq((ho["status"]["bridge"]["enabled"], "peers" in ho["status"]["bridge"], "peers" in hv["status"]["bridge"]), (False, True, False),
       "B10 hello: operators get the full status, viewers the summary")
    # a hand edit of config.json is followed within a watchdog tick
    data = json.loads(srv.config_path.read_text("utf-8"))
    data["bridge"] = {"enabled": True, "port": port, "listen": "local", "allow": [], "ptt": False, "silence_s": 10}
    srv.config_path.write_text(json.dumps(data), "utf-8")
    eq(wait_until(lambda: bstatus(admin)["listening"], 4.0), True, "B10 config.json enabled the bridge without a restart")
    f = wo.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"]["listening"], timeout=5)
    fv = wv.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"]["listening"], timeout=5)
    eq(("peers" in f["bridge"], "error_text" in f["bridge"], "peers" in fv["bridge"], "error_text" in fv["bridge"]), (True, True, False, False),
       "B10 bridge frames: per role")
    rig = Rig(port, admin)
    rig.ask("f\n")
    f = wo.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"]["clients"] == 1, timeout=5)
    eq(([p["ip"] for p in f["bridge"]["peers"]]), ["127.0.0.1"], "B10 operators see who is connected")
    fv = wv.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"]["clients"] == 1, timeout=5)
    eq("peers" in fv["bridge"], False, "B10 viewers see only the count")
    data["bridge"]["enabled"] = "yes"                 # a wrong type: fails closed
    srv.config_path.write_text(json.dumps(data), "utf-8")
    eq(wait_until(lambda: not bstatus(admin)["listening"], 4.0), True, "B10 a doubtful config.json turns the bridge off")
    check(rig.eof(3.0), "B10 ... and drops its clients")
    rig.close()
    check("bridge.enabled" in srv.log_text(), "B10 the console says why")
    wo.close()
    wv.close()

    # a port another program holds
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    taken = busy.getsockname()[1]
    try:
        r = admin.put("/api/admin/bridge", {"enabled": True, "port": taken})
        st = r.json()["status"]
        eq((r.status, st["listening"], st["error"]), (200, False, True), "B10 a port in use: saved, not listening")
        check("could not be opened" in (st["error_text"] or ""), f"B10 the admin is told why: {st['error_text']!r}")
        eq(bstatus(v).get("error_text"), None, "B10 a viewer does not get the error text")
    finally:
        busy.close()
    eq(wait_until(lambda: bstatus(admin)["listening"], 14.0, 0.5), True, "B10 the listener is retried and starts once the port is free")


# -- B11: Icom DATA mode from the browser side (the new WS action) ----------------------------------------
@test("bridge")
def b11_data_mode_action(ctx):
    srv, cl = setup(ctx)
    admin, op, v = cl["ADMIN"], cl["OP1"], cl["V1"]
    w, _ = open_ws(op)
    w.send({"action": "data_mode", "on": True})
    st = w.wait_for(lambda m: m.get("type") == "state" and m.get("data") == 1, timeout=5)
    eq((st["mode_name"], st["data"]), ("USB", 1), "B11 DATA on (USB-D)")
    w.send({"action": "set_filter", "filter": 2})
    st = w.wait_for(lambda m: m.get("type") == "state" and m.get("filter") == 2, timeout=5)
    eq(st["data"], 1, "B11 a filter change keeps DATA")
    w.send({"action": "set_mode", "mode": "LSB"})
    st = w.wait_for(lambda m: m.get("type") == "state" and m.get("mode_name") == "LSB", timeout=5)
    eq(st["data"], 0, "B11 a mode button selects the plain mode")
    w.send({"action": "set_mode", "mode": "CW"})
    w.wait_for(lambda m: m.get("type") == "state" and m.get("mode_name") == "CW", timeout=5)
    w.send({"action": "data_mode", "on": True})
    time.sleep(1.6)
    eq(state(v)["data"], 0, "B11 CW has no DATA variant: ignored")
    w.send({"action": "data_mode", "on": "yes"})
    d = w.wait_for(lambda m: m.get("type") == "denied", timeout=5)
    eq((d["action"], d["reason"]), ("data_mode", "invalid"), "B11 strict types")
    wv, _ = open_ws(v)
    wv.send({"action": "data_mode", "on": True})
    d = wv.wait_for(lambda m: m.get("type") == "denied", timeout=5)
    eq((d["action"], d["reason"]), ("data_mode", "role"), "B11 viewers cannot")
    wv.close()
    w.close()
    eq(admin.post("/api/connect", {"transport": "sim", "radio": "ft991a"}).status, 200, "B11 FT-991A simulator")
    harness.assert_sim(admin)
    w, _ = open_ws(op)
    w.send({"action": "data_mode", "on": True})
    d = w.wait_for(lambda m: m.get("type") == "denied", timeout=5)
    eq((d["action"], d["reason"]), ("data_mode", "unsupported"), "B11 a radio without the DATA switch refuses it")
    w.close()


# -- B12: a radio that REPORTS its TX state (Yaesu CAT): the arbiter's reconciliation leaves a held key alone ---
@test("bridge")
def b12_yaesu_ptt_stays_keyed_while_held(ctx):
    srv, cl = setup(ctx, radio="ft991a")
    admin, v = cl["ADMIN"], cl["V1"]
    port = bridge_port(srv)
    enable(admin, port, ptt=True)
    rig = Rig(port, admin)
    check(rig.ask(BS + "dump_state\n", 0.5).startswith("1\n1035\n"), "B12 the FT-991A simulator")
    eq(rig.ask("M PKTUSB 0\n"), "RPRT 0\n", "B12 DATA-USB")
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B12 keyed")
    seen = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < 4.0:                # several of the radio's own TX polls go by
        seen.append((rig.ask("t\n", 0.2), (owner(admin) or {}).get("user")))
        time.sleep(0.4)
    check(all(s == ("1\n", BRIDGE) for s in seen), f"B12 the radio reports TX and the bridge keeps the session: {seen}")
    eq(state(v)["ptt"], True, "B12 the radio's own state says keyed")
    eq(rig.ask("T 0\n"), "RPRT 0\n", "B12 unkeyed")
    eq(wait_until(lambda: owner(admin) is None and state(v)["ptt"] is False, 3.0), True, "B12 released, the radio reports RX")
    eq(wait_until(lambda: rig.ask("t\n", 0.2) == "0\n", 3.0), True, "B12 t follows the radio")
    time.sleep(1.5)
    eq(owner(admin), None, "B12 no FRONT PANEL session appears after a bridge transmission")
    recs = [r for r in harness.audit_records(srv.tmp) if r.get("ev") in ("tx_start", "tx_end")]
    eq([(r["ev"], r["user"]) for r in recs], [("tx_start", BRIDGE), ("tx_end", BRIDGE)], "B12 one session in the TX log")
    eq((recs[1]["reason"], recs[0]["mode"]), ("owner", "DATA-USB"), "B12 ended by the program, in DATA-USB")
    rig.close()


# -- B13: the real Hamlib client, when one is installed ----------------------------------------------------
# -- B14: the pause outlives a restart ------------------------------------------------------------------
@test("bridge")
def b14_pause_outlives_a_restart(ctx):
    srv, cl = setup(ctx, users=(("OP1", "operator"),))
    admin, op = cl["ADMIN"], cl["OP1"]
    port = bridge_port(srv)
    enable(admin, port, ptt=True)
    rig = Rig(port, admin)
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B14 keyed by the bridge")
    eq(op.post("/api/tx_stop", {}).status, 200, "B14 an operator's STOP")
    eq(wait_until(lambda: bstatus(admin)["paused"], 3.0), {"reason": "stopped", "by": "OP1"}, "B14 paused")
    saved = json.loads(srv.config_path.read_text("utf-8")).get("bridge_paused") or {}
    eq((saved.get("reason"), saved.get("by")), ("stopped", "OP1"), "B14 the pause is saved in config.json")
    st = enable(admin, port, ptt=True, silence_s=12)
    eq(st["paused"], {"reason": "stopped", "by": "OP1"}, "B14 a save with transmit still on keeps the pause")
    rig.close()
    srv.stop()
    srv2 = ctx.server(srv.tmp)
    srv2.admin_pw = harness.ADMIN_PW
    admin2 = harness.ready_admin(srv2)
    eq(admin2.post("/api/connect", {"transport": "sim", "radio": "ic7300mk2"}).status, 200, "B14 sim connect after the restart")
    harness.assert_sim(admin2)
    eq(wait_until(lambda: bstatus(admin2)["listening"], 5.0), True, "B14 the bridge listens again after the restart")
    eq(bstatus(admin2)["paused"], {"reason": "stopped", "by": "OP1"}, "B14 ... and is still paused")
    check("still paused" in srv2.log_text(), "B14 the start-up console says so")
    rig = Rig(port, admin2)
    eq(rig.ask("T 1\n"), "RPRT -22\n", "B14 the program cannot key after the restart")
    w, _ = open_ws(admin2)
    w.send({"action": "bridge_tx", "on": True})
    w.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"].get("paused") is None, timeout=5)
    eq(wait_until(lambda: "bridge_paused" not in json.loads(srv2.config_path.read_text("utf-8")), 3.0), True,
       "B14 the resume removes it from config.json")
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B14 resumed: the program keys again")
    eq(rig.ask("T 0\n"), "RPRT 0\n", "B14 unkeyed")
    w.close()
    rig.close()


# -- B15: a stale admin form ---------------------------------------------------------------------------
@test("bridge")
def b15_admin_save_checks_what_the_form_saw(ctx):
    srv, cl = setup(ctx, users=())
    admin = cl["ADMIN"]
    port = bridge_port(srv)
    cur = admin.get("/api/admin/bridge").json()["config"]
    want = dict(cur, enabled=True, port=port)
    r = admin.put("/api/admin/bridge", dict(want, expect=cur))
    eq(r.status, 200, f"B15 a save that names what the form showed {r!r}")
    r = admin.put("/api/admin/bridge", dict(want, silence_s=20, expect=cur))
    eq((r.status, _err(r), (r.json().get("config") or {}).get("port")), (409, "conflict", port),
       "B15 a stale form (it still shows the old settings) gets 409 and the saved settings")
    eq(json.loads(srv.config_path.read_text("utf-8"))["bridge"]["silence_s"], 10, "B15 ... and nothing was written")
    r = admin.put("/api/admin/bridge", dict(want, expect="x"))
    eq((r.status, _err(r), r.json().get("field")), (400, "invalid", "expect"), "B15 expect must be the settings object")
    r = admin.put("/api/admin/bridge", dict(want, silence_s=20, expect=dict(want)))
    eq(r.status, 200, "B15 a form that shows the saved settings saves")
    r = admin.put("/api/admin/bridge", dict(want, silence_s=25))
    eq(r.status, 200, "B15 a save without expect is not checked")


# -- B16: a STOP right after a bridge session ----------------------------------------------------------
@test("bridge")
def b16_stop_right_after_a_bridge_session_pauses(ctx):
    srv, cl = setup(ctx, users=(("OP1", "operator"),))
    admin, op = cl["ADMIN"], cl["OP1"]
    port = bridge_port(srv)
    enable(admin, port, ptt=True)
    rig = Rig(port, admin)
    w, _ = open_ws(op)
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B16 keyed")
    eq(rig.ask("T 0\n"), "RPRT 0\n", "B16 the program unkeys by itself")
    eq(wait_until(lambda: owner(admin) is None, 2.0), True, "B16 nobody transmits")
    w.send({"action": "tx_stop"})                      # the operator's STOP lands just after it
    f = w.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"].get("paused"), timeout=5)
    eq(f["bridge"]["paused"], {"reason": "stopped", "by": "OP1"}, "B16 a STOP right after the bridge's session still pauses it")
    eq(rig.ask("T 1\n"), "RPRT -22\n", "B16 held")
    w.send({"action": "bridge_tx", "on": True})
    w.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"].get("paused") is None, timeout=5)
    time.sleep(2.3)                                     # past the release grace of the last session
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B16 keyed again")
    eq(rig.ask("T 0\n"), "RPRT 0\n", "B16 unkeyed")
    r = op.post("/api/tx_stop", {})
    eq(r.status, 200, "B16 the HTTP STOP")
    eq(wait_until(lambda: bstatus(admin)["paused"], 3.0), {"reason": "stopped", "by": "OP1"}, "B16 ... pauses it too")
    w.send({"action": "bridge_tx", "on": True})
    w.wait_for(lambda m: m.get("type") == "bridge" and m["bridge"].get("paused") is None, timeout=5)
    time.sleep(2.3)
    eq(rig.ask("T 1\n"), "RPRT 0\n", "B16 keyed once more")
    eq(rig.ask("T 0\n"), "RPRT 0\n", "B16 unkeyed")
    time.sleep(5.6)                                     # past the window
    w.send({"action": "tx_stop"})
    time.sleep(0.8)
    eq(bstatus(admin)["paused"], None, "B16 a STOP long after the bridge's session does not pause it")
    w.close()
    rig.close()


# -- B17: TeleRig's own web port is never the bridge port -----------------------------------------------
@test("bridge")
def b17_the_web_port_is_never_the_bridge_port(ctx):
    srv, cl = setup(ctx, users=())
    admin = cl["ADMIN"]
    r = admin.put("/api/admin/bridge", {"enabled": True, "port": srv.port, "listen": "local", "allow": [], "ptt": False, "silence_s": 10})
    eq((r.status, r.json().get("field")), (400, "port"), "B17 an admin save of the web port is refused")
    cfg = json.loads(srv.config_path.read_text("utf-8")) if srv.config_path.exists() else {}
    cfg["bridge"] = {"enabled": True, "port": srv.port, "listen": "local", "allow": [], "ptt": False, "silence_s": 10}
    srv.config_path.write_text(json.dumps(cfg), "utf-8")      # a hand edit
    st = wait_until(lambda: (lambda s: s if s.get("enabled") else None)(admin.get("/api/admin/bridge").json()["status"]), 6.0)
    check(bool(st) and st["listening"] is False and "own web port" in (st.get("error_text") or ""),
          f"B17 a hand edit to the web port: not listening, and it says why: {st}")
    eq(admin.get("/api/status").status, 200, "B17 the web server still answers")


# -- B18: the wiring the review asked for (source checks; no server) -------------------------------------
@test("bridge")
def b18_shutdown_order_watchdog_audit_keys(ctx):
    src = (harness.REPO / "backend" / "server.py").read_text("utf-8")
    life = src.split("async def _lifespan", 1)[1]
    check(life.index("bridge.begin_stop()") < life.index('arb.force_release("shutdown"'),
          "B18 at shutdown the bridge stops serving and drops its clients BEFORE the arbiter's release")
    tick = src.split("async def _store_tick", 1)[1].split("\nasync def ", 1)[0]
    check("_bridge_kick()" in tick and "await _bridge_follow()" not in tick,
          "B18 the watchdog never waits for the bridge (the follow runs as its own task)")
    apply_ = src.split("def _bridge_apply(", 1)[1].split("\nclass _BridgeOps", 1)[0]
    check('key = "bridge:" + str(getattr(conn, "ip", "?"))' in apply_ and "key=conn.id" not in apply_,
          "B18 a bridge client's denials are coalesced per peer address")
    check(apply_.index("policy.is_fast(cmd)") < apply_.index('return "denied:locked"'),
          "B18 an unkey from a bridge client is decided on the fast path, before the lock refusal")
    stop = src.split("def _tx_stop(", 1)[1].split("\n\n\n", 1)[0]
    check("BRIDGE_STOP_WINDOW" in stop and "bridge.pause(" in stop, "B18 a STOP right after a bridge session pauses it")
    check("_tx_stop(request.state.principal)" in src and "_tx_stop(conn)" in src, "B18 both STOP paths use it")


def _find_rigctl() -> list:
    out = []
    env = os.environ.get("TELERIG_TEST_RIGCTL")
    if env:
        out.append(env)
    for name in ("rigctl-wsjtx", "rigctl"):
        w = shutil.which(name)
        if w:
            out.append(w)
    for pat in (r"C:\WSJT\wsjtx\bin\rigctl-wsjtx.exe", r"C:\Program Files\hamlib-*\bin\rigctl.exe",
                r"C:\Program Files (x86)\hamlib-*\bin\rigctl.exe", "/usr/bin/rigctl", "/usr/local/bin/rigctl"):
        out += sorted(glob.glob(pat))
    seen, uniq = set(), []
    for x in out:
        k = os.path.normcase(os.path.abspath(x))
        if k not in seen and Path(x).exists():
            seen.add(k)
            uniq.append(x)
    return uniq[:3]


@test("bridge")
def b13_real_hamlib_client(ctx):
    clients = _find_rigctl()
    if not clients:
        ctx.log("no Hamlib rigctl found (set TELERIG_TEST_RIGCTL to test with one): skipped")
        print("      (B13 skipped: no Hamlib rigctl on this PC)")
        return
    srv, cl = setup(ctx)
    admin, v = cl["ADMIN"], cl["V1"]
    port = bridge_port(srv)
    enable(admin, port, ptt=True)

    def run(exe, *args):
        p = subprocess.run([exe, "-m", "2", "-r", f"127.0.0.1:{port}", *args], capture_output=True, text=True, timeout=40)
        return p.returncode, [ln for ln in p.stdout.split("\n") if ln.strip()]

    for exe in clients:
        name = Path(exe).name
        ver = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout.strip().split("\n")[0]
        ctx.log(f"{exe}: {ver}")
        rc, out = run(exe, "F", "7074000", "f", "m", "v", "s")
        eq((rc, out), (0, ["7074000", "USB", str(state(v)["filter_bw"]), "VFOA", "0", "VFOA"]), f"B13 {name}: open + F f m v s")
        rc, out = run(exe, "M", "PKTUSB", "0", "m")
        eq((rc, out[0], state(v)["data"]), (0, "PKTUSB", 1), f"B13 {name}: M PKTUSB")
        rc, out = run(exe, "M", "USB", "0", "m")
        eq((rc, out[0], state(v)["data"]), (0, "USB", 0), f"B13 {name}: M USB")
        rc, out = run(exe, "l", "RFPOWER", "_")
        check(rc == 0 and len(out) == 2 and "TeleRig" in out[1], f"B13 {name}: level + info {out}")
        n0 = len([r for r in harness.audit_records(srv.tmp) if r.get("ev") == "tx_end" and r.get("user") == BRIDGE])
        harness.assert_sim(admin)                     # interlock before the real client keys (the simulator)
        rc, out = run(exe, "T", "1", "T", "0")
        eq(rc, 0, f"B13 {name}: T 1 T 0")
        ends = audit_wait(srv.tmp, lambda r: r.get("ev") == "tx_end" and r.get("user") == BRIDGE)
        check(ends and len(ends) == n0 + 1 and ends[-1]["reason"] in ("owner", "disconnect"),
              f"B13 {name}: one bridge transmission, ended by the client: {ends[-1:] if ends else ends}")
        eq((owner(admin), state(v)["ptt"]), (None, False), f"B13 {name}: nothing left keyed")
        time.sleep(2.3)
    print(f"      (B13 ran with {len(clients)} Hamlib client(s): {', '.join(Path(c).name for c in clients)})")
