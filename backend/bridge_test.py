"""WSJT-X bridge unit tests — run: python -m backend.bridge_test

In-process: the Bridge with a FAKE server side (`Ops`: a state dict, a recording `apply`) and real
asyncio streams on 127.0.0.1 (free ports) or fake streams (for peers that are not this PC). No
radio, no serial port, no TeleRig server; the fake `apply` only records what the bridge asked for.
What the policy table and the TX arbiter then do with it is tested against a real server process in
tests/t_bridge.py (simulator only).
"""
import asyncio
import socket
import time

from backend import bridge as B
from backend import profiles
from backend import rigctl as R

_ok = True
BS = chr(92)


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---- configuration helpers ---------------------------------------------------------------------
C("defaults: off, local, no transmit", (B.DEFAULTS["enabled"], B.DEFAULTS["listen"], B.DEFAULTS["ptt"], B.DEFAULTS["port"]),
  (False, "local", False, 4532))
C("normalize: absent -> defaults", B.normalize_config(None), (dict(B.DEFAULTS, allow=[]), []))
cfg, warn = B.normalize_config("yes")
C("normalize: not an object -> off + a warning", (cfg["enabled"], len(warn)), (False, 1))
cfg, warn = B.normalize_config({"enabled": True, "ptt": True, "port": 4600, "listen": "network",
                                "allow": ["192.168.2.7/24", "10.0.0.5", "nope", "10.0.0.5"], "silence_s": 20})
C("normalize: a full valid config", cfg, {"enabled": True, "ptt": True, "port": 4600, "listen": "network",
                                         "allow": ["192.168.2.0/24", "10.0.0.5"], "silence_s": 20})
C("normalize: the bad allow entry is reported", len(warn), 1)
cfg, warn = B.normalize_config({"enabled": True, "port": 80})
C("normalize: a bad port turns the bridge off", (cfg["enabled"], cfg["port"]), (False, 4532))
cfg, warn = B.normalize_config({"enabled": 1, "ptt": "true", "listen": "all", "silence_s": 0, "allow": "x"})
C("normalize: wrong types fail closed", (cfg["enabled"], cfg["ptt"], cfg["listen"], cfg["silence_s"], cfg["allow"]),
  (False, False, "local", 10, []))
C("normalize: ... each with a warning", len(warn), 5)
C("normalize: bool is not a port", B.normalize_config({"enabled": True, "port": True})[0]["enabled"], False)

ok_body = {"enabled": True, "port": 4532, "listen": "local", "allow": [], "ptt": False, "silence_s": 10}
C("validate: a good save", B.validate_config(ok_body), (ok_body, None))
C("validate: defaults fill in", B.validate_config({})[0], dict(B.DEFAULTS, allow=[]))
for body, field in (({"enabled": "yes"}, "enabled"), ({"ptt": 1}, "ptt"), ({"port": 80}, "port"),
                    ({"port": 70000}, "port"), ({"port": "4532"}, "port"), ({"port": True}, "port"),
                    ({"listen": "lan"}, "listen"), ({"allow": "10.0.0.1"}, "allow"),
                    ({"allow": ["10.0.0.300"]}, "allow"), ({"allow": [5]}, "allow"),
                    ({"listen": "network", "allow": []}, "allow"), ({"silence_s": 2}, "silence_s"),
                    ({"silence_s": 121}, "silence_s"), ({"silence_s": 5.5}, "silence_s"),
                    ({"allow": ["10.0.0.1"] * 0 + [f"10.0.{i}.1" for i in range(B.ALLOW_MAX + 1)]}, "allow")):
    got = B.validate_config(body)
    C(f"validate: {body if len(str(body)) < 60 else 'too many allow entries'} -> {field}", (got[0], got[1][0] if got[1] else None), (None, field))
C("validate: not an object", B.validate_config([])[0], None)
C("validate: the web port is refused", B.validate_config({"port": 8700}, web_port=8700)[1][0], "port")
C("validate: network + an allowed network", B.validate_config({"listen": "network", "allow": ["192.168.2.9/24"]})[0]["allow"],
  ["192.168.2.0/24"])
C("parse_allow", [B.parse_allow(x) for x in ("10.0.0.1", " 10.0.0.0/8 ", "fd7a:115c:a1e0::1", "fe80::/10", "x", "", None, "1.2.3.4/33")],
  ["10.0.0.1", "10.0.0.0/8", "fd7a:115c:a1e0::1", None, None, None, None, None])
C("parse_allow: a network may be at most /8 (IPv4) or /32 (IPv6) wide",
  [B.parse_allow(x) for x in ("10.0.0.0/7", "0.0.0.0/0", "2001:db8::/32", "2001:db8::/31", "::/0")],
  [None, None, "2001:db8::/32", None, None])
C("parse_allow: IPv4-mapped forms become IPv4; host bits are dropped; 0.0.0.0 is no address",
  [B.parse_allow(x) for x in ("::ffff:192.168.2.9", "::ffff:192.168.2.0/120", "192.168.2.77/24", "0.0.0.0", "::")],
  ["192.168.2.9", "192.168.2.0/24", "192.168.2.0/24", None, None])
C("an IPv6 entry around the mapped range never admits an IPv4 peer (peers are compared as IPv4)",
  [B.peer_allowed(x, {"listen": "network", "allow": [B.parse_allow("::fffe:0:0/95")]}) for x in ("192.168.2.9", "::ffff:192.168.2.9")],
  [False, False])
C("normalize: a too wide allow entry is dropped with a warning",
  (B.normalize_config({"allow": ["10.0.0.0/4", "10.1.0.0/16"]})[0]["allow"], any("/8" in w for w in B.normalize_config({"allow": ["10.0.0.0/4"]})[1])),
  (["10.1.0.0/16"], True))
C("normalize: network mode with an empty allow list warns (only this PC can connect)",
  any("allow is empty" in w for w in B.normalize_config({"listen": "network", "allow": []})[1]), True)
C("validate: a too wide network is refused", B.validate_config({"listen": "network", "allow": ["10.0.0.0/6"]})[1][0], "allow")
C("plain_ip", [B.plain_ip(x) for x in ("::ffff:192.168.2.9", "fe80::1%eth0", "127.0.0.1", "junk", None)],
  ["192.168.2.9", "fe80::1", "127.0.0.1", "junk", ""])
NET = {"listen": "network", "allow": ["192.168.2.0/24", "100.64.0.7", "fd7a::/16"]}
C("peer: this PC always", [B.peer_allowed(x, {"listen": "local", "allow": []}) for x in ("127.0.0.1", "::1", "127.9.9.9", "::ffff:127.0.0.1")],
  [True] * 4)
C("peer: local mode refuses everyone else, listed or not",
  [B.peer_allowed(x, {"listen": "local", "allow": ["192.168.2.0/24"]}) for x in ("192.168.2.9", "10.0.0.1")], [False, False])
C("peer: network mode = the allow list", [B.peer_allowed(x, NET) for x in
                                          ("192.168.2.9", "::ffff:192.168.2.9", "192.168.3.9", "100.64.0.7", "100.64.0.8", "fd7a::5", "fd7b::5", "junk", "")],
  [True, True, False, True, False, True, False, False, False])


# ---- the fake server side ------------------------------------------------------------------------
class FakeRadio:
    def __init__(self, pid="ic7300mk2"):
        self.profile = profiles.PROFILES[pid]
        self.state = {"connected": True, "radio": pid, "freq": 14074000, "mode_name": "USB", "data": 0,
                      "filter_bw": 2400, "ptt": False, "split": 0, "smeter": 120, "rfpwr": 128}


class Ops:
    def __init__(self, pid="ic7300mk2"):
        self.r = FakeRadio(pid)
        self.ok = True
        self.applied = []
        self.result = "ok"
        self.closed_ids = []
        self.hb = 0
        self.own = None
        self.stopped = []
        self._id = 100

    def radio(self):
        return self.r

    def usable(self):
        return self.ok

    def new_id(self):
        self._id += 1
        return self._id

    def apply(self, conn, cmd):
        self.applied.append(dict(cmd))
        res = self.result(cmd) if callable(self.result) else self.result
        if res == "ok":
            st, a = self.r.state, cmd["action"]
            if a == "set_freq":
                st["freq"] = cmd["hz"]
            elif a == "set_mode":
                st["mode_name"], st["data"] = cmd["mode"], 0
            elif a == "data_mode":
                st["data"] = 1 if cmd["on"] else 0
            elif a == "split":
                st["split"] = 1 if cmd["on"] else 0
            elif a == "set_level":
                st["rfpwr"] = cmd["value"]
            elif a == "ptt":
                if cmd["tx"]:
                    st["ptt"], self.own = True, conn.id
                elif self.own == conn.id:
                    st["ptt"], self.own = False, None
        return res

    def heartbeat(self, conn):
        self.hb += 1

    def closed(self, conn):
        self.closed_ids.append(conn.id)
        if self.own == conn.id:
            self.own, self.r.state["ptt"] = None, False

    def owner(self):
        return self.own

    def stop_owner(self, by):
        self.stopped.append(by)
        self.own, self.r.state["ptt"] = None, False


class Cli:
    """A raw rigctl client on a real socket."""

    def __init__(self, reader, writer):
        self.r, self.w = reader, writer

    @classmethod
    async def open(cls, port):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        return cls(r, w)

    async def ask(self, line, wait=0.25):
        self.w.write(line.encode())
        await self.w.drain()
        return await self.read(wait)

    async def read(self, wait=0.25):
        buf = b""
        while True:
            try:
                d = await asyncio.wait_for(self.r.read(65536), wait)
            except asyncio.TimeoutError:
                return buf.decode()
            except (ConnectionError, OSError):
                return buf.decode() + "<EOF>"
            if not d:
                return buf.decode() + "<EOF>"
            buf += d

    async def close(self):
        try:
            self.w.close()
            await self.w.wait_closed()
        except Exception:
            pass


class FakeWriter:
    def __init__(self, ip, port=5555):
        self.peer = (ip, port)
        self.closed = False
        self.out = b""

    def get_extra_info(self, name, default=None):
        return self.peer if name == "peername" else default

    def write(self, data):
        self.out += data

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class FakeReader:
    """`hold`: seconds the fake client waits for its answers before it closes (0 = closes at once)."""
    def __init__(self, data=b"", hold=0.0):
        self.buf = data
        self.hold = hold

    async def readuntil(self, sep=b"\n"):
        i = self.buf.find(sep)
        if i < 0:
            if self.hold:
                await asyncio.sleep(self.hold)
                self.hold = 0.0
            part, self.buf = self.buf, b""
            raise asyncio.IncompleteReadError(part, None)
        line, self.buf = self.buf[:i + 1], self.buf[i + 1:]
        return line


def mk(ops=None, **cfg):
    ops = ops or Ops()
    audits, changes = [], [0]
    b = B.Bridge(ops, audit=lambda ev, **f: audits.append((ev, f)), on_change=lambda: changes.__setitem__(0, changes[0] + 1),
                 version="9.9.9")
    conf = dict(B.DEFAULTS, allow=[], enabled=True, port=free_port())
    conf.update(cfg)
    return b, ops, audits, changes, conf


def evs(audits, name):
    return [f for ev, f in audits if ev == name]


async def settle(t=0.08):
    await asyncio.sleep(t)


# ---- listener + commands ---------------------------------------------------------------------------
async def t_commands():
    b, ops, audits, changes, conf = mk()
    C("new bridge: off", (b.listening, b.status()["enabled"], b.status()["clients"]), (False, False, 0))
    await b.configure(dict(conf, enabled=False))
    C("configured but disabled: not listening", b.listening, False)
    await b.configure(conf)
    C("enabled: listening on the port", (b.listening, b.bound, b.error), (True, ("local", conf["port"]), None))
    C("the listener start is audited", len(evs(audits, "bridge_listen")), 1)
    c = await Cli.open(conf["port"])
    C("powerstat", await c.ask(BS + "get_powerstat\n"), "1\n")
    C("chk_vfo", await c.ask(BS + "chk_vfo\n"), "0\n")
    C("chk_vfo extended", await c.ask("+" + BS + "chk_vfo\n"), "ChkVFO: 0\n")
    C("one client connected", (b.status()["clients"], [p["ip"] for p in b.status()["peers"]]), (1, ["127.0.0.1"]))
    C("the viewer summary has no peers and no error text", sorted(set(b.status(True)) - set(b.status(False))), ["error_text", "peers"])
    C("f", await c.ask("f\n"), "14074000\n")
    C("m", await c.ask("m\n"), "USB\n2400\n")
    C("v / t / s", await c.ask("v t s\n"), "VFOA\n0\n0\nVFOA\n")
    C("no command so far reached the radio", ops.applied, [])
    C("F -> one canonical set_freq", (await c.ask("F 7074000.000000\n"), ops.applied), ("RPRT 0\n", [{"action": "set_freq", "hz": 7074000}]))
    C("f reads the new frequency back at once", await c.ask("f\n"), "7074000\n")
    ops.applied.clear()
    C("F to the frequency already set does not touch the radio", (await c.ask("F 7074000\n"), ops.applied), ("RPRT 0\n", []))
    C("F with junk / zero / too big -> RPRT -1, nothing applied",
      (await c.ask("F abc\n"), await c.ask("F 0\n"), await c.ask("F -5\n"), await c.ask("F 99999999999999\n"), ops.applied),
      ("RPRT -1\n",) * 4 + ([],))
    C("M PKTUSB from USB: only the DATA switch", (await c.ask("M PKTUSB -1\n"), ops.applied),
      ("RPRT 0\n", [{"action": "data_mode", "on": True}]))
    C("m shows PKTUSB", await c.ask("m\n"), "PKTUSB\n2400\n")
    ops.applied.clear()
    C("M PKTUSB again: DATA ON once more, never trusting a DATA flag that can be stale", (await c.ask("M PKTUSB 0\n"), ops.applied),
      ("RPRT 0\n", [{"action": "data_mode", "on": True}]))
    ops.applied.clear()
    C("M LSB: the mode (DATA goes off with it)", (await c.ask("M LSB 2400\n"), ops.applied), ("RPRT 0\n", [{"action": "set_mode", "mode": "LSB"}]))
    ops.applied.clear()
    C("M PKTLSB... wait, PKTUSB from LSB: mode then DATA", (await c.ask("M PKTUSB 0\n"), ops.applied),
      ("RPRT 0\n", [{"action": "set_mode", "mode": "USB"}, {"action": "data_mode", "on": True}]))
    ops.applied.clear()
    C("M CW from USB-D: just the mode", (await c.ask("M CW 500\n"), ops.applied), ("RPRT 0\n", [{"action": "set_mode", "mode": "CW"}]))
    C("M with a mode the radio lacks / junk -> RPRT -1", (await c.ask("M C4FM 0\n"), await c.ask("M NOSUCH 0\n")), ("RPRT -1\n", "RPRT -1\n"))
    C("M ? lists the radio's modes", await c.ask("M ?\n"), "LSB PKTLSB USB PKTUSB CW CWR RTTY RTTYR AM PKTAM FM PKTFM\nRPRT 0\n")
    ops.applied.clear()
    ops.result = "busy"
    C("busy (someone else transmits) -> RPRT -9", await c.ask("F 7075000\n"), "RPRT -9\n")
    for res, want in (("failed", "RPRT -6\n"), ("denied:not_connected", "RPRT -6\n"), ("denied:unsupported", "RPRT -11\n"),
                      ("denied:invalid", "RPRT -1\n"), ("denied:role", "RPRT -22\n"), ("denied:connecting", "RPRT -9\n"),
                      ("denied:whatever", "RPRT -9\n")):
        ops.result = res
        C(f"{res} -> {want.strip()}", await c.ask("F 7076000\n"), want)
    ops.result = lambda cmd: (_ for _ in ()).throw(RuntimeError("boom"))
    C("a failing apply is an IO error, the connection lives", (await c.ask("F 7077000\n"), await c.ask("f\n")), ("RPRT -6\n", "7074000\n"))
    ops.result = "ok"
    C("V VFOA ok, V VFOB not offered, V ?", (await c.ask("V VFOA\n"), await c.ask("V VFOB\n"), await c.ask("V ?\n")),
      ("RPRT 0\n", "RPRT -11\n", "VFOA \nRPRT 0\n"))
    ops.applied.clear()
    C("S 0 while split is off: nothing to do", (await c.ask("S 0 VFOA\n"), ops.applied), ("RPRT 0\n", []))
    ops.r.state["split"] = 1
    C("s shows split on", await c.ask("s\n"), "1\nVFOB\n")
    C("S 0 turns the radio's split off", (await c.ask("S 0 VFOA\n"), ops.applied), ("RPRT 0\n", [{"action": "split", "on": False}]))
    C("split TX on VFO B is not offered", (await c.ask("S 1 VFOB\n"), await c.ask("I 14075000\n"), await c.ask("i\n"),
                                           await c.ask("X USB 2400\n"), await c.ask("x\n")), ("RPRT -11\n",) * 5)
    C("levels", (await c.ask("l STRENGTH\n"), await c.ask("l RFPOWER\n"), await c.ask("l KEYSPD\n"), await c.ask("l ?\n")),
      ("0\n", "0.501961\n", "RPRT -11\n", "STRENGTH RFPOWER \n"))
    ops.applied.clear()
    C("L RFPOWER", (await c.ask("L RFPOWER 1.0\n"), ops.applied), ("RPRT 0\n", [{"action": "set_level", "target": "rfpwr", "value": 255}]))
    C("L RFPOWER out of range / junk / another level", (await c.ask("L RFPOWER 1.5\n"), await c.ask("L RFPOWER x\n"), await c.ask("L AF 0.5\n")),
      ("RPRT -1\n", "RPRT -1\n", "RPRT -11\n"))
    C("lock mode", (await c.ask(BS + "get_lock_mode\n"), await c.ask(BS + "set_lock_mode 0\n"), await c.ask(BS + "set_lock_mode 1\n")),
      ("0\nRPRT 0\n", "RPRT 0\n", "RPRT -11\n"))
    C("powerstat set", (await c.ask(BS + "set_powerstat 1\n"), await c.ask(BS + "set_powerstat 0\n")), ("RPRT 0\n", "RPRT -11\n"))
    C("vfo_info", (await c.ask(BS + "get_vfo_info VFOA\n"), await c.ask(BS + "get_vfo_info VFOB\n")), ("7074000\nCW\n2400\n0\n0\n", "RPRT -11\n"))
    C("info", await c.ask("_\n"), "TeleRig 9.9.9 bridge - IC-7300MK2\n")
    C("extended", (await c.ask("+f\n"), await c.ask(";m\n")), ("get_freq:\nFrequency: 7074000\nRPRT 0\n", "get_mode:;Mode: CW;Passband: 2400;RPRT 0\n"))
    C("unknown -> -1; recognised but not offered -> -4 / -11; deprecated -> -18",
      (await c.ask("bogus\n"), await c.ask(BS + "nosuch\n"), await c.ask("w FA;\n"), await c.ask("j\n"), await c.ask("a\n")),
      ("RPRT -1\n", "RPRT -1\n", "RPRT -4\n", "RPRT -11\n", "RPRT -18\n"))
    C("a missing argument -> -1", (await c.ask("F\n"), await c.ask("T\n")), ("RPRT -1\n", "RPRT -1\n"))
    d = await c.ask(BS + "dump_state\n", 0.4)
    C("dump_state ends with done and says transmit is off", (d.endswith("\ndone\n"), "ptt_type=0x0\n" in d, "rig_model=3094\n" in d), (True, True, True))
    C("dump_caps names the radio", "Model name:\tIC-7300MK2\n" in await c.ask("1\n"), True)
    hb = ops.hb
    await c.ask("f\n")
    C("every command line is a heartbeat", ops.hb, hb + 1)
    await c.ask("\n")
    C("an empty line is not", ops.hb, hb + 1)
    C("quit answers and closes", await c.ask("q\n", 0.4), "RPRT 0\n<EOF>")
    await settle()
    C("the client is gone and its TX release ran", (b.status()["clients"], len(ops.closed_ids)), (0, 1))
    C("connect / disconnect are audited per peer", (evs(audits, "bridge_connect")[0], evs(audits, "bridge_disconnect")[0]),
      ({"key": "bridge:127.0.0.1", "ip": "127.0.0.1"}, {"key": "bridge:127.0.0.1", "ip": "127.0.0.1", "reason": "quit"}))
    C("the UI was told about changes", changes[0] > 2, True)
    await b.stop()
    C("stop: not listening", b.listening, False)


# ---- the transmit gate, pause / resume -----------------------------------------------------------
async def t_ptt():
    b, ops, audits, changes, conf = mk()
    await b.configure(conf)                               # ptt: False
    c = await Cli.open(conf["port"])
    C("transmit off: T 1 -> RPRT -22 and nothing reaches the arbiter", (await c.ask("T 1\n"), ops.applied), ("RPRT -22\n", []))
    C("... also for T 2 / T 3 / the long form", (await c.ask("T 2\n"), await c.ask("T 3\n"), await c.ask(BS + "set_ptt 1\n"), ops.applied),
      ("RPRT -22\n",) * 3 + ([],))
    C("the refusal is audited (coalesced per peer)", evs(audits, "bridge_tx_refused")[0]["reason"], "off")
    C("transmit off: T 0 with nothing keyed answers RPRT 0 and writes nothing (no loop chatter)",
      (await c.ask("T 0\n"), await c.ask("T 0\n"), ops.applied), ("RPRT 0\n", "RPRT 0\n", []))
    ops.r.state["ptt"] = True                             # keyed with no owner (e.g. a stale state)
    C("transmit off: T 0 while the radio shows TX goes through (an unkey is never refused)", (await c.ask("T 0\n"), ops.applied),
      ("RPRT 0\n", [{"action": "ptt", "tx": False, "auto": True}]))
    ops.r.state["ptt"] = False
    ops.applied.clear()
    conf2 = dict(conf, ptt=True)
    await b.configure(conf2)
    await settle()
    C("switching transmit on drops the client (it must re-read the capabilities)", await c.read(0.3), "<EOF>")
    c = await Cli.open(conf["port"])
    C("the capability dump now advertises CAT PTT", "ptt_type=0x1\n" in await c.ask(BS + "dump_state\n", 0.4), True)
    ops.applied.clear()
    C("transmit on: T 1 -> one canonical ptt claim", (await c.ask("T 1\n"), ops.applied), ("RPRT 0\n", [{"action": "ptt", "tx": True}]))
    C("t shows the radio keyed; the bridge owns TX", (await c.ask("t\n"), b.owns_tx(), b.status()["on_air"], b.status()["peers"][0]["tx"]),
      ("1\n", True, True, True))
    C("T 0 releases", (await c.ask("T 0\n"), await c.ask("t\n"), b.owns_tx()), ("RPRT 0\n", "0\n", False))
    C("T 2 and T 3 key too", (await c.ask("T 2\n"), await c.ask("T 0\n"), await c.ask("T 3\n"), await c.ask("T 0\n")), ("RPRT 0\n",) * 4)
    C("T 9 / T x -> RPRT -1", (await c.ask("T 9\n"), await c.ask("T x\n")), ("RPRT -1\n", "RPRT -1\n"))
    ops.result = "busy"
    C("someone else transmits: T 1 -> RPRT -9", await c.ask("T 1\n"), "RPRT -9\n")
    ops.result = "failed"
    C("the radio refused to key: RPRT -6", await c.ask("T 1\n"), "RPRT -6\n")
    ops.result = "ok"

    conn = next(iter(b.conns.values()))
    C("the connection is an operator named for the bridge, with the silence limit",
      (conn.role, conn.user, conn.hb_timeout, " " in conn.user), ("operator", "WSJT-X BRIDGE", 10.0, True))
    for frame in ({"type": "tx", "owner": None}, {"type": "busy"}, {"type": "tx_revoked", "reason": "radio_change"},
                  {"type": "tx_revoked", "reason": "radio_disconnect"}, {"type": "tx_revoked", "reason": "disconnect"},
                  {"type": "tx_revoked", "reason": "shutdown"}, "junk", None):
        conn.deliver(frame)
    C("other arbiter frames never pause the bridge", b.paused, None)
    conn.deliver({"type": "tx_revoked", "reason": "stopped", "by": "OP1"})
    C("a forced STOP pauses bridge transmit", (b.paused["reason"], b.paused["by"], b.status()["paused"]),
      ("stopped", "OP1", {"reason": "stopped", "by": "OP1"}))
    conn.deliver({"type": "tx_revoked", "reason": "timeout"})
    C("the first reason is kept", b.paused["reason"], "stopped")
    ops.applied.clear()
    C("paused: T 1 -> RPRT -22, nothing reaches the arbiter", (await c.ask("T 1\n"), ops.applied), ("RPRT -22\n", []))
    C("... audited as paused", evs(audits, "bridge_tx_refused")[-1]["reason"], "paused")
    C("paused: tuning still works", await c.ask("F 7074500\n"), "RPRT 0\n")
    C("resume", (b.resume(by="OP2"), b.paused, b.resume(by="OP2")), (True, None, False))
    C("pause / resume are audited", (evs(audits, "bridge_tx_paused")[0], evs(audits, "bridge_tx_resumed")[0]),
      ({"reason": "stopped", "by": "OP1"}, {"by": "OP2"}))
    C("after resume T 1 keys again", await c.ask("T 1\n"), "RPRT 0\n")
    for why in ("timeout", "heartbeat"):
        b.paused = None
        conn.deliver({"type": "tx_revoked", "reason": why})
        C(f"a {why} release pauses too", b.paused and b.paused["reason"], why)
    b.resume()
    b.pause("operator", by="OP1")
    C("an operator can pause by hand", b.status()["paused"], {"reason": "operator", "by": "OP1"})
    b.resume()

    # the client vanishes while keyed: its TX release runs at once
    await c.ask("T 1\n")
    C("keyed again", (ops.own == conn.id, ops.r.state["ptt"]), (True, True))
    await c.close()
    await settle(0.2)
    C("socket closed while keyed -> released at once, not paused", (ops.own, ops.r.state["ptt"], conn.id in ops.closed_ids, b.paused),
      (None, False, True, None))

    # transmit switched off by the admin while a bridge client is keyed
    c = await Cli.open(conf["port"])
    await c.ask("T 1\n")
    C("keyed", b.owns_tx(), True)
    conn = next(iter(b.conns.values()))
    conn.deliver({"type": "tx_revoked", "reason": "stopped", "by": "x"})   # an operator's STOP: paused
    await b.configure(dict(conf2, ptt=False))
    await settle()
    C("transmit switched off while keyed: stopped, client dropped, the pause stays (sticky)",
      (ops.stopped, b.owns_tx(), await c.read(0.3), (b.paused or {}).get("reason"), (b.paused or {}).get("by")),
      (["bridge transmit switched off"], False, "<EOF>", "stopped", "x"))
    await b.configure(dict(conf2, enabled=False))
    await b.configure(dict(conf2))
    C("... and survives disabling and enabling the bridge", (b.paused or {}).get("reason"), "stopped")
    c = await Cli.open(conf["port"])
    C("while paused, T 1 is refused (RPRT -22) and nothing reaches the arbiter", (await c.ask("T 1\n"), b.owns_tx()), ("RPRT -22\n", False))
    C("... and the refusal says why", evs(audits, "bridge_tx_refused")[-1]["reason"], "paused")
    C("an operator's resume ends it", (b.resume("op"), b.paused, b.resume("op")), (True, None, False))
    await c.close()

    # live settings
    await b.configure(dict(conf2, silence_s=30))
    c = await Cli.open(conf["port"])
    await c.ask("f\n")
    conn = next(iter(b.conns.values()))
    C("a new client gets the configured silence limit", conn.hb_timeout, 30.0)
    await b.configure(dict(conf2, silence_s=5))
    C("changing only the silence limit keeps the client and updates it", (conn.closing, conn.hb_timeout, b.status()["clients"]), (False, 5.0, 1))
    await c.ask("T 1\n")
    b.on_radio_change()
    await settle()
    C("a radio change drops every client and releases a keyed one", (await c.read(0.3), ops.own, b.status()["clients"]), ("<EOF>", None, 0))
    c = await Cli.open(conf["port"])
    await c.ask("f\n")
    ops.ok = False
    C("no radio: every radio command is an IO error", (await c.ask("f\n"), await c.ask("T 1\n"), await c.ask(BS + "dump_state\n")), ("RPRT -6\n",) * 3)
    C("... but the handshake probes still answer", await c.ask(BS + "chk_vfo\n"), "0\n")
    await b.tick()
    await settle()
    C("the watchdog tick drops clients once the radio is gone", (await c.read(0.3), b.status()["clients"]), ("<EOF>", 0))
    c2 = await Cli.open(conf["port"])
    C("no radio: a new connection is closed at once", await c2.read(0.4), "<EOF>")
    C("... and audited", evs(audits, "bridge_refused")[-1], {"key": "bridge:127.0.0.1", "ip": "127.0.0.1", "reason": "no_radio"})
    ops.ok = True
    await b.stop()


# ---- refusals, hostile input -----------------------------------------------------------------------
async def t_refuse():
    b, ops, audits, changes, conf = mk(ptt=True)
    await b.configure(conf)
    # peers that are not this PC (fake streams: nothing is bound to a network interface)
    w = FakeWriter("192.168.2.50")
    await b._client(FakeReader(b"T 1\n"), w)
    C("local mode: another PC is refused before a byte is read", (w.closed, w.out, ops.applied, evs(audits, "bridge_refused")[-1]["reason"]),
      (True, b"", [], "not_allowed"))
    b.cfg = dict(conf, listen="network", allow=["192.168.2.0/24"])
    w = FakeWriter("10.1.1.1")
    await b._client(FakeReader(b"T 1\n"), w)
    C("network mode: a peer outside the allow list is refused", (w.closed, w.out, ops.applied), (True, b"", []))
    w = FakeWriter("::ffff:192.168.2.50")
    await b._client(FakeReader(b"f\nT 1\nT 0\n", hold=0.5), w)
    C("network mode: a listed peer is served", (w.out, [c["action"] for c in ops.applied]), (b"14074000\nRPRT 0\nRPRT 0\n", ["ptt", "ptt"]))
    C("... and its TX session shows its address", evs(audits, "bridge_connect")[-1]["ip"], "192.168.2.50")
    b.cfg = dict(conf)
    ops.applied.clear()

    # too many clients
    clients = [await Cli.open(conf["port"]) for _ in range(B.MAX_CLIENTS)]
    for cl in clients:
        await cl.ask("f\n")
    C("the client limit", b.status()["clients"], B.MAX_CLIENTS)
    extra = await Cli.open(conf["port"])
    C("one more is refused", (await extra.read(0.4), evs(audits, "bridge_refused")[-1]["reason"]), ("<EOF>", "too_many"))
    n = b.drop_all("test")
    await settle()
    C("drop_all closes them all", (n, b.status()["clients"], [await cl.read(0.2) for cl in clients]), (B.MAX_CLIENTS, 0, ["<EOF>"] * B.MAX_CLIENTS))

    # a browser (or any HTTP client) on the port
    for first in ("POST / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\nT 1\n", "GET / HTTP/1.1\r\n\r\n", "OPTIONS * HTTP/1.1\r\n"):
        ops.applied.clear()
        c = await Cli.open(conf["port"])
        C(f"HTTP ({first.split()[0]}): closed, nothing answered, nothing run", (await c.ask(first, 0.4), ops.applied, ops.r.state["ptt"]),
          ("<EOF>", [], False))
    C("... audited", evs(audits, "bridge_refused")[-1]["reason"], "http")
    c = await Cli.open(conf["port"])
    await c.ask("f\n")
    ops.applied.clear()
    C("an HTTP line later in a session closes it too", (await c.ask("POST /x HTTP/1.1\r\nT 1\n", 0.4), ops.applied), ("<EOF>", []))

    for label, data in (("a line without an end", b"f" * (R.LINE_MAX + 50)), ("bytes that are not text", b"f\xff\n"),
                        ("control characters", b"f\x00m\n"), ("too many commands", b"f " * (R.CMDS_PER_LINE + 2) + b"\n")):
        c = await Cli.open(conf["port"])
        c.w.write(data)
        await c.w.drain()
        C(f"{label}: closed without an answer", await c.read(0.5), "<EOF>")
    await settle()
    C("no client is left behind", b.status()["clients"], 0)

    old = B.FIRST_LINE_TIMEOUT
    B.FIRST_LINE_TIMEOUT = 0.3
    try:
        c = await Cli.open(conf["port"])
        C("a connection that never sends a command is closed", await c.read(1.5), "<EOF>")
        c = await Cli.open(conf["port"])
        await c.ask("f\n")
        C("... but one that spoke may stay silent", await c.read(0.8), "")
        await c.close()
    finally:
        B.FIRST_LINE_TIMEOUT = old

    # pacing: a burst of setters is slowed down, never refused
    n = B.BACKLOG_MAX - 2                                 # more than the setter burst, within the backlog
    C("(the burst is above the setter burst)", n > B.SET_BURST, True)
    c = await Cli.open(conf["port"])
    ops.applied.clear()
    t0 = time.monotonic()
    c.w.write("".join(f"F {7000000 + i}\n" for i in range(n)).encode())
    await c.w.drain()
    got = await c.read(3.0)
    C(f"{n} setters in one burst: all applied in order, none refused",
      (got.count("RPRT 0\n"), len(ops.applied), ops.applied[-1]["hz"] if ops.applied else None), (n, n, 7000000 + n - 1))
    C("... and paced (the ones above the burst wait their turn)", time.monotonic() - t0 >= (n - B.SET_BURST) / B.SET_RATE * 0.6, True)
    await c.close()
    await settle(0.3)
    # a flood: far more unanswered lines than any rigctl client sends -> closed, the rest discarded
    c = await Cli.open(conf["port"])
    ops.applied.clear()
    c.w.write("".join(f"F {7100000 + i}\n" for i in range(400)).encode())
    await c.w.drain()
    got = await c.read(2.0)
    C("400 lines without waiting for an answer: the connection is closed", got.endswith("<EOF>"), True)
    C("... most were never served", len(ops.applied) < 400, True)
    C("... and it was a flood", evs(audits, "bridge_disconnect")[-1]["reason"], "flood")
    await b.stop()


# ---- listener lifecycle ----------------------------------------------------------------------------
async def t_listener():
    b, ops, audits, changes, conf = mk()
    await b.configure(conf)
    c = await Cli.open(conf["port"])
    await c.ask("f\n")
    p2 = free_port()
    await b.configure(dict(conf, port=p2))
    await settle()
    C("a port change restarts the listener and drops the clients", (b.bound, await c.read(0.3)), (("local", p2), "<EOF>"))
    try:
        await asyncio.open_connection("127.0.0.1", conf["port"])
        old_open = True
    except OSError:
        old_open = False
    C("the old port is closed", old_open, False)
    c = await Cli.open(p2)
    C("the new port answers", await c.ask("f\n"), "14074000\n")
    await b.configure(dict(conf, port=p2, enabled=False))
    await settle()
    C("disabling closes the listener and the clients", (b.listening, b.bound, await c.read(0.3), b.status()["enabled"]), (False, None, "<EOF>", False))
    await b.configure(dict(conf, port=p2))
    C("enabling again listens", b.listening, True)
    await b.configure(None)
    C("an unreadable configuration means off", (b.listening, b.cfg["enabled"], b.cfg["ptt"]), (False, False, False))

    # the port is taken by another program
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    taken = busy.getsockname()[1]
    await b.configure(dict(conf, port=taken))
    C("port in use: not listening, an error the admin can read", (b.listening, b.status(False)["error"], "could not be opened" in (b.status()["error_text"] or "")),
      (False, True, True))
    await b.tick()
    C("no retry before its time", b.listening, False)
    busy.close()
    b.retry_at = 0.0
    await b.tick()
    C("the retry succeeds once the port is free", (b.listening, b.error), (True, None))
    await b.stop()

    # only IPv4 available: still listening
    calls = []

    async def v4_only(cb, host=None, port=None, limit=None):
        calls.append(host)
        if host == "::1":
            raise OSError("no IPv6")
        return await asyncio.start_server(cb, host=host, port=port, limit=limit)
    b2 = B.Bridge(Ops(), start_server=v4_only)
    await b2.configure(dict(conf, port=free_port()))
    C("local = 127.0.0.1 and ::1; IPv4 alone is enough", (calls, b2.listening, len(b2.servers)), (["127.0.0.1", "::1"], True, 1))
    await b2.stop()
    calls.clear()

    async def nothing(cb, host=None, port=None, limit=None):
        calls.append(host)
        raise OSError("refused")
    b3 = B.Bridge(Ops(), start_server=nothing)
    await b3.configure(dict(conf, listen="network", allow=["10.0.0.1"]))
    C("network = all interfaces; IPv4 failing is the error", (calls, b3.listening, bool(b3.error)), (["0.0.0.0"], False, True))


for t in (t_commands, t_ptt, t_refuse, t_listener):
    asyncio.run(t())

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
