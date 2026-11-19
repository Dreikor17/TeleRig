"""The WSJT-X bridge: a rigctld-compatible TCP server in front of TeleRig's own radio handlers
(docs/WSJTX-BRIDGE.md).

A program set to "Hamlib NET rigctl" (WSJT-X, JS8Call, Fldigi, a logger) connects here instead of
to the radio. TeleRig keeps the radio (COM port or the Icom LAN link) and relays what the program
asks, so both work at once. Off by default; an admin turns it on (Administration > WSJT-X bridge).

Every bridge client is a connection like a browser tab, with the fixed role `operator` and the
fixed name BRIDGE_USER. Nothing here touches the radio directly: each command becomes the same
canonical command a browser sends and goes through the same policy table and TX arbiter
(`ops.apply`), so the same rules hold:

  * one TX owner. While someone else transmits, the bridge cannot key or retune (busy); while the
    bridge transmits, everyone sees ON AIR and any operator can STOP it;
  * transmit over the bridge is a separate switch (config `ptt`, off by default), and keying is
    served only while the bridge is enabled and not shutting down;
  * the 120 s session clock and the radio's own failsafes apply unchanged;
  * the keying client must keep talking: after `silence_s` seconds without a command from it the
    transmission ends (the arbiter's heartbeat rule, with this connection's own limit). Its socket
    closing ends it at once, whatever it had still queued (reading and serving are two tasks);
  * a transmission that had to be ended for the client (PAUSE_REASONS: an operator's STOP, the
    120 s limit, the silence limit, an admin's forced radio switch, the server locking) PAUSES
    bridge transmit until an operator resumes it in TeleRig. A digital-mode program re-keys on its
    own schedule; a STOP must stay a stop. The pause is sticky: only an operator's resume, or an
    admin switching transmit from off to on, ends it. It is kept across a disabled bridge, a
    locked server and a restart (`ops.persist_pause`);
  * a radio change or disconnect closes every bridge connection (the client reconnects and reads
    the new radio's capabilities);
  * a connection whose bytes are an HTTP request is closed before any line is read as a command
    (a web page can make a browser POST to this port);
  * `listen` is "local" (127.0.0.1 and ::1 only) or "network" (all interfaces, but only peers in
    the `allow` list of addresses / networks, plus this PC);
  * a client is expected to wait for each answer (Hamlib does): more than BACKLOG_MAX unanswered
    lines closes it; setters are paced per peer address, keying is rate-limited for the whole
    bridge, so a reconnect loop cannot chatter the transmitter.

The radio's own transmit state matters too: both handlers report it (the Yaesu TX; poll, the Icom
1C 00 read), so a hand-mic over at the radio is the FRONT PANEL owner and a bridge program's key or
retune gets busy, like anyone else's. The IC-728 cannot report it (no CI-V TX command), so there a
front-panel key stays invisible.

Runs on the event loop only (asyncio streams). The pure protocol is backend/rigctl.py.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from typing import Any, Callable, Optional

from . import rigctl
from .hub import TokenBucket

BRIDGE_USER = "WSJT-X BRIDGE"        # has a space: it can never be an account name (accounts.canonical)
DEFAULT_PORT = 4532                   # rigctld's own default port
PORT_MIN, PORT_MAX = 1024, 65535
SILENCE_MIN, SILENCE_MAX, SILENCE_DEFAULT = 3, 120, 10
ALLOW_MAX = 32
V4_PREFIX_MIN, V6_PREFIX_MIN = 8, 32  # the widest network an allow entry may name
MAX_CLIENTS = 6
IDLE_EVICT = 60.0                     # with every slot taken, the client idle longest (>= this) makes room
BACKLOG_MAX = 32                      # request lines waiting to be served; more is not a rigctl client
FIRST_LINE_TIMEOUT = 30.0             # a connection that never sends a command is closed
WRITE_TIMEOUT = 5.0                   # a client that does not read its replies is closed
RETRY_SECS = 10.0                     # re-try a listener that could not start (port in use)
SET_BURST, SET_RATE = 20, 20.0        # setters per peer address: burst, then per second
KEY_BURST, KEY_RATE = 6, 0.5          # keyings for the whole bridge: burst, then one per 2 s
PAUSE_REASONS = ("stopped", "timeout", "heartbeat", "admin_switch", "server_locked")
HAMLIB_COMPAT = "Hamlib 4.6.5"        # the rigctld version this bridge answers like

DEFAULTS = {"enabled": False, "port": DEFAULT_PORT, "listen": "local", "allow": [], "ptt": False,
            "silence_s": SILENCE_DEFAULT}


def _noop(*a, **k) -> None:
    return None


# -- configuration (config.json "bridge") -----------------------------------------------------------
def parse_allow(entry: Any) -> Optional[str]:
    """One allowed peer: an IP address or a network in CIDR form, canonical text; None = invalid.
    A network may be at most /8 (IPv4) or /32 (IPv6) wide, so no entry can mean "everyone"; host
    bits are dropped (192.168.2.7/24 -> 192.168.2.0/24); an IPv4-mapped IPv6 form becomes IPv4,
    because that is how peers are compared."""
    if not isinstance(entry, str):
        return None
    s = entry.strip()
    if not s or len(s) > 64:
        return None
    try:
        if "/" in s:
            net = ipaddress.ip_network(s, strict=False)
            if net.version == 6 and net.network_address.ipv4_mapped is not None:
                if net.prefixlen < 96:
                    return None
                net = ipaddress.ip_network(f"{net.network_address.ipv4_mapped}/{net.prefixlen - 96}", strict=False)
            if net.prefixlen < (V4_PREFIX_MIN if net.version == 4 else V6_PREFIX_MIN):
                return None
            return str(net.network_address) if net.prefixlen == net.max_prefixlen else str(net)
        a = ipaddress.ip_address(s)
        if a.version == 6 and a.ipv4_mapped is not None:
            a = a.ipv4_mapped
        if a.is_unspecified:
            return None
        return str(a)
    except ValueError:
        return None


def normalize_config(raw: Any) -> tuple:
    """Lenient read of config.json's "bridge" (never raises): (config, warnings). A wrong type or
    an out-of-range value falls back to its default, and anything doubtful fails closed: the bridge
    stays off, transmit stays off, the listener stays local."""
    cfg = dict(DEFAULTS, allow=[])
    warn: list = []
    if raw is None:
        return cfg, warn
    if not isinstance(raw, dict):
        return cfg, ["bridge is not an object; the bridge stays off"]
    for key in ("enabled", "ptt"):
        v = raw.get(key, False)
        if isinstance(v, bool):
            cfg[key] = v
        else:
            warn.append(f"bridge.{key} is not true/false; using false")
    port = raw.get("port", DEFAULT_PORT)
    if type(port) is int and PORT_MIN <= port <= PORT_MAX:
        cfg["port"] = port
    else:
        warn.append(f"bridge.port must be {PORT_MIN}-{PORT_MAX}; the bridge stays off")
        cfg["enabled"] = False
    listen = raw.get("listen", "local")
    if listen in ("local", "network"):
        cfg["listen"] = listen
    else:
        warn.append('bridge.listen must be "local" or "network"; using "local"')
    allow = raw.get("allow", [])
    if not isinstance(allow, list):
        warn.append("bridge.allow is not a list; ignored")
        allow = []
    for e in allow[:ALLOW_MAX * 4]:
        c = parse_allow(e)
        if c is None:
            warn.append(f"bridge.allow entry {e!r} is not an address or a network of /{V4_PREFIX_MIN} or longer; dropped")
        elif c not in cfg["allow"] and len(cfg["allow"]) < ALLOW_MAX:
            cfg["allow"].append(c)
    if cfg["listen"] == "network" and not cfg["allow"]:
        warn.append('bridge.listen is "network" but bridge.allow is empty: only this PC can connect')
    sil = raw.get("silence_s", SILENCE_DEFAULT)
    if type(sil) is int and SILENCE_MIN <= sil <= SILENCE_MAX:
        cfg["silence_s"] = sil
    else:
        warn.append(f"bridge.silence_s must be {SILENCE_MIN}-{SILENCE_MAX}; using {SILENCE_DEFAULT}")
    return cfg, warn


def validate_config(body: Any, *, web_port: Optional[int] = None) -> tuple:
    """Strict check of an admin's save: (config, None) or (None, (field, message))."""
    if not isinstance(body, dict):
        return None, ("", "Send the bridge settings as an object.")
    cfg = dict(DEFAULTS, allow=[])
    for key in ("enabled", "ptt"):
        v = body.get(key, False)
        if type(v) is not bool:
            return None, (key, f"{key} must be true or false.")
        cfg[key] = v
    port = body.get("port", DEFAULT_PORT)
    if type(port) is not int or not PORT_MIN <= port <= PORT_MAX:
        return None, ("port", f"The port must be a whole number from {PORT_MIN} to {PORT_MAX}.")
    if web_port is not None and port == web_port:
        return None, ("port", f"Port {port} is TeleRig's own web port; choose another (rigctld uses {DEFAULT_PORT}).")
    cfg["port"] = port
    listen = body.get("listen", "local")
    if listen not in ("local", "network"):
        return None, ("listen", 'listen must be "local" or "network".')
    cfg["listen"] = listen
    allow = body.get("allow", [])
    if not isinstance(allow, list) or len(allow) > ALLOW_MAX:
        return None, ("allow", f"allow must be a list of at most {ALLOW_MAX} addresses.")
    for e in allow:
        c = parse_allow(e)
        if c is None:
            return None, ("allow", f"{e!r} is not an IP address or a network like 192.168.1.0/24 "
                                   f"(a network can be at most /{V4_PREFIX_MIN} wide).")
        if c not in cfg["allow"]:
            cfg["allow"].append(c)
    if listen == "network" and not cfg["allow"]:
        return None, ("allow", "Add at least one address that may connect, or listen on this PC only.")
    sil = body.get("silence_s", SILENCE_DEFAULT)
    if type(sil) is not int or not SILENCE_MIN <= sil <= SILENCE_MAX:
        return None, ("silence_s", f"The silence limit must be {SILENCE_MIN} to {SILENCE_MAX} seconds.")
    cfg["silence_s"] = sil
    return cfg, None


def plain_ip(host: Any) -> str:
    """The peer address as plain text: zone id dropped, IPv4-mapped IPv6 unwrapped."""
    s = str(host or "").split("%", 1)[0]
    try:
        a = ipaddress.ip_address(s)
    except ValueError:
        return s
    if a.version == 6 and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    return str(a)


def peer_allowed(ip: str, cfg: dict) -> bool:
    """This PC always; other peers only in "network" mode and only when listed in `allow`."""
    try:
        a = ipaddress.ip_address(plain_ip(ip))
    except ValueError:
        return False
    if a.is_loopback:
        return True
    if cfg.get("listen") != "network":
        return False
    for e in cfg.get("allow") or []:
        try:
            net = ipaddress.ip_network(e, strict=False)
        except ValueError:
            continue
        if a.version == net.version and a in net:
            return True
    return False


# -- one client -------------------------------------------------------------------------------------
class BridgeConn:
    """One bridge client. Duck-types hub.Conn for the policy table and the TX arbiter: id, user,
    role, ip, closing, last_hb, plus `hb_timeout` (this connection's own silence limit) and
    `deliver` (the arbiter's frames for the owner: tx, busy, tx_revoked)."""
    kind = "bridge"

    def __init__(self, cid: int, ip: str, peer_port: int, writer: Any, owner: "Bridge"):
        self.id = cid
        self.user = BRIDGE_USER
        self.role = "operator"
        self.ip = ip
        self.peer_port = peer_port
        self.writer = writer
        self.bridge = owner
        self.closing = False
        self.direct_local = False
        self.opened = time.time()
        self.opened_mono = time.monotonic()
        self.last_hb = 0.0
        self.hb_timeout = float(SILENCE_DEFAULT)
        self.last_rx = time.monotonic()
        self.cmds = 0

    def deliver(self, frame: dict) -> None:
        try:
            self.bridge._on_frame(self, frame)
        except Exception:
            pass

    def __repr__(self) -> str:
        return f"<BridgeConn {self.id} {self.ip}>"


# -- the bridge -------------------------------------------------------------------------------------
class Bridge:
    """`ops` is what the server lends the bridge (looked up at call time):
        ops.radio()            the active radio handler (.state, .profile)
        ops.usable() -> bool   a radio is connected, no connect transition, the server not locked
        ops.new_id() -> int    a connection id never used by a WebSocket
        ops.apply(conn, cmd) -> "ok" | "busy" | "failed" | "denied:<reason>"   policy + arbiter
        ops.heartbeat(conn)    the arbiter's tx_hb for this connection
        ops.closed(conn)       this connection is gone: release its TX now
        ops.owner()            the arbiter's current owner id (or None)
        ops.stop_owner(by)     force-release the current TX session (the owner is a bridge client)
      optional:
        ops.web_port() -> int  TeleRig's own web port (never opened as the bridge port)
        ops.persist_pause(info | None)   remember the pause across a restart
    A pause needs nothing from the arbiter's re-attach rule (AR-47): it re-attaches only a session
    the RADIO ended (a misread poll), never one a STOP, a limit or a lock ended; and a re-attached
    bridge session stays under its own T 0, the silence limit and the session clock.
    """

    def __init__(self, ops: Any, *, audit: Callable[..., None] = _noop, log: Callable[[str], None] = _noop,
                 on_change: Callable[[], None] = _noop, version: str = "",
                 clock: Callable[[], float] = time.monotonic,
                 start_server: Optional[Callable] = None):
        self.ops = ops
        self.audit = audit
        self.log = log
        self.on_change = on_change
        self.clock = clock
        self.version = f"{HAMLIB_COMPAT} (TeleRig {version} bridge)".replace("  ", " ")
        self.app_version = version
        self._start_server = start_server or asyncio.start_server
        self.cfg: dict = dict(DEFAULTS, allow=[])
        self.conns: dict = {}
        self.servers: list = []
        self._closed_servers: list = []              # closed, not yet awaited
        self.listening = False
        self.bound: Optional[tuple] = None           # (listen, port) of the running listener
        self.error: Optional[str] = None
        self.retry_at = 0.0
        self.stopping = False                        # shutdown has begun: nothing is served any more
        self.paused: Optional[dict] = None           # {"reason", "by", "at"}
        self._buckets: dict = {}                     # peer address -> TokenBucket (setters)
        self._key_bucket = TokenBucket(KEY_BURST, KEY_RATE)
        self._mu: Optional[asyncio.Lock] = None

    # -- helpers ---------------------------------------------------------------------------------
    def _lock(self) -> asyncio.Lock:
        if self._mu is None:
            self._mu = asyncio.Lock()
        return self._mu

    def _audit(self, ev: str, **fields) -> None:
        try:
            self.audit(ev, **fields)
        except Exception:
            pass

    def _changed(self) -> None:
        try:
            self.on_change()
        except Exception:
            pass

    def _owner(self):
        try:
            return self.ops.owner()
        except Exception:
            return None

    def _opt(self, name: str, *args):
        """Call an optional ops hook; a missing hook or a failing one never breaks the bridge."""
        fn = getattr(self.ops, name, None)
        if not callable(fn):
            return None
        try:
            return fn(*args)
        except Exception as e:
            self.log(f"TeleRig: bridge {name} failed: {e!r}")
            return None

    def serving(self) -> bool:
        """Commands are served only while the bridge is enabled and not shutting down."""
        return bool(self.cfg["enabled"]) and not self.stopping

    def owns_tx(self) -> bool:
        """True while a bridge client is the TX owner."""
        o = self._owner()
        return o is not None and o in self.conns

    def has(self, conn: Any) -> bool:
        return conn is not None and self.conns.get(getattr(conn, "id", None)) is conn

    def status(self, full: bool = True) -> dict:
        """What the UI shows. `full` (operators and admins) adds the peers and the error text."""
        c = self.cfg
        now = time.monotonic()
        out = {"enabled": bool(c["enabled"]), "listening": bool(self.listening), "port": c["port"],
               "listen": c["listen"], "ptt": bool(c["ptt"]), "clients": len(self.conns),
               "paused": ({"reason": self.paused.get("reason"), "by": self.paused.get("by")}
                          if self.paused else None),
               "on_air": self.owns_tx(), "error": bool(self.error)}
        if full:
            out["error_text"] = self.error
            owner = self._owner()
            out["peers"] = [{"ip": x.ip, "secs": int(now - x.opened_mono), "cmds": x.cmds,
                             "tx": owner is not None and owner == x.id}
                            for x in sorted(self.conns.values(), key=lambda v: v.id)]
        return out

    # -- pause / resume ------------------------------------------------------------------------------
    def pause(self, reason: str, by: Optional[str] = None) -> None:
        """Hold bridge transmit until an operator resumes it. Never unkeys by itself."""
        if self.paused is not None:
            return
        self.paused = {"reason": str(reason), "by": by, "at": time.time()}
        self._audit("bridge_tx_paused", reason=reason, by=by)
        self._opt("persist_pause", dict(self.paused))
        self._changed()

    def resume(self, by: Optional[str] = None) -> bool:
        """An operator's resume (or an admin switching transmit from off to on): the ONLY ways a
        pause ends. Returns False when it was not paused."""
        if self.paused is None:
            return False
        self.paused = None
        self._audit("bridge_tx_resumed", by=by)
        self._opt("persist_pause", None)
        self._changed()
        return True

    def restore_pause(self, info: Any) -> None:
        """At start: a pause that was in force when the server stopped is in force again."""
        if self.paused is not None or not isinstance(info, dict) or not info.get("reason"):
            return
        by = info.get("by")
        at = info.get("at")
        self.paused = {"reason": str(info["reason"])[:32], "by": by if isinstance(by, str) else None,
                       "at": float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else time.time()}
        self.log(f"  TeleRig: transmit over the WSJT-X bridge is still paused ({self.paused['reason']}); "
                 "an operator resumes it from the bridge button.")
        self._changed()

    def _on_frame(self, conn: BridgeConn, frame: dict) -> None:
        """An arbiter frame for a bridge connection (BridgeConn.deliver). Only tx_revoked matters:
        a session the server had to end for the client pauses bridge transmit."""
        if not isinstance(frame, dict) or frame.get("type") != "tx_revoked":
            return
        reason = frame.get("reason")
        if reason in PAUSE_REASONS:
            self.pause(str(reason), frame.get("by"))

    # -- configuration + listener --------------------------------------------------------------------
    async def configure(self, cfg: Optional[dict]) -> None:
        """Apply a configuration (None = unreadable: everything off). Whatever ends a transmission
        or a connection happens first and without waiting; only then is a listener closed or
        opened. The listener restarts only when what it is bound to changes. A pause is never
        touched here."""
        new = dict(DEFAULTS, allow=[]) if cfg is None else dict(cfg, allow=list(cfg.get("allow") or []))
        async with self._lock():
            old, self.cfg = self.cfg, new
            if new == old and (self.listening or not new["enabled"] or self.error):
                return
            want = (new["listen"], new["port"]) if new["enabled"] and not self.stopping else None
            rebind = want != self.bound or (want is not None and not self.listening)
            if not new["ptt"] and self.owns_tx():
                try:
                    self.ops.stop_owner("bridge transmit switched off")
                except Exception as e:
                    self.log(f"TeleRig: bridge TX stop failed: {e!r}")
            if rebind:
                self._close_listener()
            if rebind or new["ptt"] != old["ptt"]:
                # a new port or address; or a client that learnt "can / cannot key over CAT" from
                # the capability dump it read at connect and must read the new answer
                self.drop_all("config")
            for c in list(self.conns.values()):
                c.hb_timeout = float(new["silence_s"])
                if not peer_allowed(c.ip, new):
                    self._drop(c, "not_allowed")
            if rebind:
                await self._wait_closed()
                if want is not None:
                    await self._start_listener()
            if not new["enabled"]:
                self.error = None
            self._changed()

    async def _start_listener(self) -> None:
        cfg = self.cfg
        web = self._opt("web_port")
        if web and cfg["port"] == web:
            self.listening, self.bound = False, None
            self.error = f"Port {cfg['port']} is TeleRig's own web port. Choose another port for the bridge."
            self.retry_at = self.clock() + RETRY_SECS
            self.log(f"TeleRig: WSJT-X bridge not listening: {self.error}")
            return
        hosts = ("127.0.0.1", "::1") if cfg["listen"] == "local" else ("0.0.0.0", "::")
        servers: list = []
        first_err: Optional[BaseException] = None
        for n, host in enumerate(hosts):
            try:
                srv = await self._start_server(self._client, host=host, port=cfg["port"],
                                               limit=rigctl.LINE_MAX)
                servers.append(srv)
            except (OSError, ValueError) as e:
                if n == 0:
                    first_err = e                    # IPv4 is the one that must work
                    break
                # no IPv6 on this PC, or the port is taken on ::: IPv4 alone is enough
        if first_err is not None:
            for s in servers:
                s.close()
            self.listening, self.bound = False, None
            self.error = (f"Port {cfg['port']} could not be opened ({_oserr(first_err)}). Another program "
                          "may be using it (a running rigctld uses 4532).")
            self.retry_at = self.clock() + RETRY_SECS
            self.log(f"TeleRig: WSJT-X bridge not listening: {self.error}")
            return
        self.servers = servers
        self.listening, self.bound, self.error = True, (cfg["listen"], cfg["port"]), None
        where = "this PC only" if cfg["listen"] == "local" else "the network (allow-listed peers)"
        self.log(f"  TeleRig: WSJT-X bridge listening on port {cfg['port']} ({where}); "
                 f"transmit over the bridge is {'ON' if cfg['ptt'] else 'off'}.")
        self._audit("bridge_listen", port=cfg["port"], listen=cfg["listen"], ptt=bool(cfg["ptt"]))

    def _close_listener(self) -> None:
        """Stop accepting, NOW (no await): the listening sockets are closed synchronously."""
        servers, self.servers = self.servers, []
        was = self.listening
        self.listening, self.bound = False, None
        for s in servers:
            try:
                s.close()
            except Exception:
                pass
        self._closed_servers += servers
        if was:
            self._audit("bridge_closed")

    async def _wait_closed(self) -> None:
        """Let the closed listeners finish. Since Python 3.12 Server.wait_closed() also waits for
        every client connection, so this runs only AFTER the clients were dropped; bounded."""
        servers, self._closed_servers = self._closed_servers, []
        for s in servers:
            try:
                await asyncio.wait_for(s.wait_closed(), 1.0)
            except Exception:
                pass

    def begin_stop(self) -> None:
        """Shutdown, the synchronous part: from here no command is served, nobody is connected and
        nothing is accepted. Call it BEFORE the arbiter's shutdown release."""
        self.stopping = True
        self._close_listener()
        self.drop_all("shutdown")

    async def stop(self) -> None:
        self.begin_stop()
        async with self._lock():
            self._close_listener()
            self.drop_all("shutdown")
            await self._wait_closed()

    def sweep(self) -> None:
        """Every watchdog tick (no await): drop the clients when the radio is gone. A radio change
        drops them explicitly; this catches a link that died."""
        if self.conns and not self._usable():
            self.drop_all("no_radio")

    def retry_due(self) -> bool:
        return bool(self.cfg["enabled"] and not self.stopping and not self.listening and self.error
                    and self.clock() >= self.retry_at)

    async def retry(self) -> None:
        """Try again to open a listener that could not start (the port was in use)."""
        async with self._lock():
            if self.cfg["enabled"] and not self.stopping and not self.listening:
                await self._start_listener()
                self._changed()

    async def tick(self) -> None:
        self.sweep()
        if self.retry_due():
            await self.retry()

    def _usable(self) -> bool:
        try:
            return bool(self.ops.usable())
        except Exception:
            return False

    # -- connections ---------------------------------------------------------------------------------
    def drop_all(self, why: str) -> int:
        n = 0
        for c in list(self.conns.values()):
            self._drop(c, why)
            n += 1
        return n

    def on_radio_change(self) -> None:
        """The radio is being switched, connected or disconnected: every client must reconnect."""
        self.drop_all("radio_change")

    def _drop(self, conn: BridgeConn, why: str) -> None:
        if conn.closing:
            return
        conn.closing = True
        try:
            self.ops.closed(conn)                    # a TX owner is released NOW (I5), not on EOF
        except Exception as e:
            self.log(f"TeleRig: bridge TX release failed: {e!r}")
        self.conns.pop(conn.id, None)
        try:
            conn.writer.close()
        except Exception:
            pass
        self._audit("bridge_disconnect", key="bridge:" + conn.ip, ip=conn.ip, reason=why)
        self._changed()

    def _evict_idle(self) -> bool:
        """Every slot is taken: the client that has been silent longest (and for at least
        IDLE_EVICT, and is not transmitting) makes room, so stale connections never lock a
        program out."""
        now = time.monotonic()
        owner = self._owner()
        idle = [c for c in self.conns.values() if c.id != owner and now - c.last_rx >= IDLE_EVICT]
        if not idle:
            return False
        self._drop(min(idle, key=lambda c: c.last_rx), "evicted")
        return True

    def _refuse(self, ip: str) -> Optional[str]:
        if not self.serving():
            return "disabled"
        if not peer_allowed(ip, self.cfg):
            return "not_allowed"
        if not self._usable():
            return "no_radio"
        if len(self.conns) >= MAX_CLIENTS and not self._evict_idle():
            return "too_many"
        return None

    def _bucket(self, ip: str) -> TokenBucket:
        b = self._buckets.get(ip)
        if b is None:
            if len(self._buckets) >= 64:             # keep the ones still connected
                live = {c.ip for c in self.conns.values()}
                self._buckets = {k: v for k, v in self._buckets.items() if k in live}
            b = self._buckets[ip] = TokenBucket(SET_BURST, SET_RATE)
        return b

    async def _client(self, reader: Any, writer: Any) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        ip = plain_ip(peer[0])
        why = self._refuse(ip)
        if why is not None:
            self._audit("bridge_refused", key="bridge:" + ip, ip=ip, reason=why)
            _close(writer)
            return
        _tune_socket(writer)
        try:
            conn = BridgeConn(self.ops.new_id(), ip, int(peer[1] or 0), writer, self)
        except Exception as e:
            self.log(f"TeleRig: bridge connection setup failed: {e!r}")
            _close(writer)
            return
        conn.hb_timeout = float(self.cfg["silence_s"])
        self.conns[conn.id] = conn
        self._audit("bridge_connect", key="bridge:" + ip, ip=ip)
        self._changed()
        lines: asyncio.Queue = asyncio.Queue()
        pump = asyncio.ensure_future(self._pump(conn, reader, lines))
        why = "closed"
        try:
            while not conn.closing:
                line = await lines.get()
                if line is None or conn.closing:
                    break
                try:
                    out, done = await self._line(conn, line)
                except rigctl.ProtocolError:
                    why = "protocol"
                    break
                if out and not conn.closing:
                    try:
                        writer.write(out.encode("ascii", "replace"))
                        await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT)
                    except (asyncio.TimeoutError, ConnectionError, OSError):
                        why = "write"
                        break
                if done:
                    why = "quit"
                    break
        except asyncio.CancelledError:
            why = "shutdown"
            raise
        except Exception as e:                       # never let a handler bug leave a client keyed
            self.log(f"TeleRig: bridge client handler failed: {e!r}")
            why = "error"
        finally:
            self._drop(conn, why)
            pump.cancel()

    async def _pump(self, conn: BridgeConn, reader: Any, lines: asyncio.Queue) -> None:
        """Read the client's lines as they arrive, apart from serving them. That way a closed or
        reset socket is noticed at once, however many lines still wait (paced setters queue up), and
        the TX release never waits behind a backlog. Every line that arrives is the client's
        heartbeat. An HTTP request, bytes that are not text, an over-long line or more than
        BACKLOG_MAX unanswered lines end the connection."""
        why = "closed"
        try:
            first = True
            while not conn.closing:
                try:
                    raw = await asyncio.wait_for(reader.readuntil(b"\n"),
                                                 FIRST_LINE_TIMEOUT if first else None)
                except asyncio.TimeoutError:
                    why = "idle"
                    break
                except asyncio.LimitOverrunError:
                    why = "line_too_long"
                    break
                except (asyncio.IncompleteReadError, ConnectionError, OSError):
                    break
                try:
                    line = raw.decode("ascii")
                except UnicodeDecodeError:
                    why = "not_text"
                    break
                if rigctl.looks_like_http(line):
                    self._audit("bridge_refused", key="bridge:" + conn.ip, ip=conn.ip, reason="http")
                    why = "http"
                    break
                if not line.strip():
                    continue
                first = False
                conn.last_rx = time.monotonic()
                try:
                    self.ops.heartbeat(conn)         # a line arrived: the client is alive
                except Exception:
                    pass
                if lines.qsize() >= BACKLOG_MAX:
                    why = "flood"
                    break
                lines.put_nowait(line)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.log(f"TeleRig: bridge reader failed: {e!r}")
            why = "error"
        finally:
            self._drop(conn, why)                    # releases its TX now; whatever is queued is discarded
            lines.put_nowait(None)

    # -- commands ------------------------------------------------------------------------------------
    async def _line(self, conn: BridgeConn, line: str) -> tuple:
        out: list = []
        for cmd in rigctl.parse_line(line):
            if conn.closing:
                break
            conn.cmds += 1
            text, done = await self._run(conn, cmd)
            out.append(text)
            if done:
                return "".join(out), True
        return "".join(out), False

    def _apply(self, conn: BridgeConn, cmd: dict) -> int:
        """One canonical command through the policy table and the arbiter -> a Hamlib code."""
        try:
            res = self.ops.apply(conn, cmd)
        except Exception as e:
            self.log(f"TeleRig: bridge command failed: {e!r}")
            return rigctl.EIO
        if res == "ok":
            return rigctl.OK
        if res == "busy":
            return rigctl.ERJCTED
        if res == "failed":
            return rigctl.EIO
        why = res.split(":", 1)[1] if isinstance(res, str) and ":" in res else ""
        return {"not_connected": rigctl.EIO, "locked": rigctl.EIO, "unsupported": rigctl.ENAVAIL,
                "invalid": rigctl.EINVAL, "role": rigctl.EACCESS}.get(why, rigctl.ERJCTED)

    async def _pace(self, conn: BridgeConn) -> None:
        """Setters are slowed down per PEER ADDRESS (a reconnect does not reset it), never refused."""
        b = self._bucket(conn.ip)
        if not b.take():
            await asyncio.sleep(min(1.0, max(0.01, b.wait_time())))
            b.take()

    async def _run(self, conn: BridgeConn, c: rigctl.Command) -> tuple:
        """One command -> (reply text, close the connection)."""
        R = rigctl
        if not c.known:
            return R.rprt(R.EINVAL), False
        n = c.name
        if n == "quit":
            return R.rprt(R.OK), True
        if c.missing:
            return R.reply(c, None, R.EINVAL), False
        if n == "chk_vfo":                           # "no VFO argument on every command"
            return ("ChkVFO: 0\n" if c.ext is not None else "0\n"), False
        if n in ("get_trn", "set_trn"):
            return R.reply(c, None, R.EDEPRECATED), False
        if n not in R.HANDLED:
            return R.reply(c, None, R.ENIMPL), False
        a = c.args
        if n == "set_ptt":                           # before every gate: an unkey is never refused
            v = R.parse_int(a[0])
            if v not in (0, 1, 2, 3):
                return R.reply(c, None, R.EINVAL), False
            code = self._unkey(conn) if v == 0 else await self._key(conn)
            return R.reply(c, None, code), False
        if not self.serving() or not self._usable():
            return R.reply(c, None, R.EIO), False
        radio = self.ops.radio()
        st = getattr(radio, "state", None) or {}
        prof = getattr(radio, "profile", None)

        if n == "dump_state":
            body = R.dump_state(prof, version=self.version, ptt=bool(self.cfg["ptt"]))
            return (f"dump_state:\n{body}RPRT 0\n" if c.ext is not None else body), False
        if n == "dump_caps":
            return self._caps(prof), False
        if n == "get_info":
            return R.reply(c, [f"TeleRig {self.app_version} bridge - {getattr(prof, 'name', '?')}"]), False
        if n == "get_powerstat":
            return R.reply(c, ["1"]), False
        if n == "set_powerstat":
            return R.reply(c, None, R.OK if a[0] == "1" else R.ENAVAIL), False
        if n == "get_lock_mode":
            return R.reply(c, ["0"], always_rprt=True), False
        if n == "set_lock_mode":
            return R.reply(c, None, R.OK if a[0] == "0" else R.ENAVAIL), False

        if n == "get_freq":
            return R.reply(c, [str(int(st.get("freq") or 0))]), False
        if n == "set_freq":
            hz = R.parse_freq(a[0])
            if hz is None or not 1 <= hz <= 10_000_000_000:
                return R.reply(c, None, R.EINVAL), False
            if hz == st.get("freq"):
                return R.reply(c, None, R.OK), False
            await self._pace(conn)
            return R.reply(c, None, self._apply(conn, {"action": "set_freq", "hz": hz})), False
        if n == "get_mode":
            return R.reply(c, [R.to_hamlib_mode(prof, st), str(int(st.get("filter_bw") or 0))]), False
        if n == "set_mode":
            if a[0] == "?":
                return " ".join(R.hamlib_modes(prof)) + "\nRPRT 0\n", False
            want = R.from_hamlib_mode(prof, a[0])
            if want is None:
                return R.reply(c, None, R.EINVAL), False
            return R.reply(c, None, await self._set_mode(conn, st, want)), False
        if n == "get_vfo":
            return R.reply(c, ["VFOA"]), False
        if n == "set_vfo":
            if a[0] == "?":
                return "VFOA \nRPRT 0\n", False
            ok = a[0].upper() in ("VFOA", "CURRVFO", "VFO", "MAIN", "MAINA", "A")
            return R.reply(c, None, R.OK if ok else R.ENAVAIL), False
        if n == "get_ptt":
            return R.reply(c, ["1" if st.get("ptt") else "0"]), False
        if n == "get_split_vfo":
            on = bool(st.get("split"))
            return R.reply(c, ["1" if on else "0", "VFOB" if on else "VFOA"]), False
        if n == "set_split_vfo":
            v = R.parse_int(a[0])
            if v not in (0, 1):
                return R.reply(c, None, R.EINVAL), False
            if v == 1:                               # split TX on VFO B is not offered (use "Fake It")
                return R.reply(c, None, R.ENAVAIL), False
            if not st.get("split"):
                return R.reply(c, None, R.OK), False
            await self._pace(conn)
            return R.reply(c, None, self._apply(conn, {"action": "split", "on": False})), False
        if n == "get_vfo_info":
            if a[0].upper() not in ("VFOA", "CURRVFO", "VFO", "MAIN", "MAINA", "A"):
                return R.reply(c, None, R.ENAVAIL), False
            return R.reply(c, [str(int(st.get("freq") or 0)), R.to_hamlib_mode(prof, st),
                               str(int(st.get("filter_bw") or 0)), "1" if st.get("split") else "0", "0"]), False
        if n == "get_level":
            if a[0] == "?":
                return "STRENGTH RFPOWER \n", False
            name = a[0].upper()
            if name == "STRENGTH":
                return R.reply(c, [str(R.strength_db(st.get("smeter")))]), False
            if name == "RFPOWER":
                return R.reply(c, [f"{max(0, min(255, int(st.get('rfpwr') or 0))) / 255:g}"]), False
            return R.reply(c, None, R.ENAVAIL), False
        if n == "set_level":
            if a[0].upper() != "RFPOWER":
                return R.reply(c, None, R.ENAVAIL), False
            try:
                f = float(a[1])
            except ValueError:
                return R.reply(c, None, R.EINVAL), False
            if not 0.0 <= f <= 1.0:
                return R.reply(c, None, R.EINVAL), False
            await self._pace(conn)
            code = self._apply(conn, {"action": "set_level", "target": "rfpwr", "value": int(round(f * 255))})
            return R.reply(c, None, code), False
        if n in ("get_func", "get_parm") and a and a[0] == "?":
            return "\n", False
        # split frequency/mode, RIT/XIT, functions and parameters are not offered over the bridge
        return R.reply(c, None, R.ENAVAIL), False

    async def _set_mode(self, conn: BridgeConn, st: dict, want: tuple) -> int:
        """Mode, then the Icom DATA switch. A PKT mode says DATA ON every time: it costs one frame
        and never trusts a DATA flag that can be a moment stale (after a MAIN/SUB switch the panel
        still shows the other band's)."""
        name, data = want
        code = rigctl.OK
        if st.get("mode_name") != name:
            await self._pace(conn)
            code = self._apply(conn, {"action": "set_mode", "mode": name})
            if code != rigctl.OK:
                return code
        if data is True or (data is False and st.get("data")):
            await self._pace(conn)
            code = self._apply(conn, {"action": "data_mode", "on": bool(data)})
        return code

    def _unkey(self, conn: BridgeConn) -> int:
        """T 0. Never gated, never paced. The owner releases; a non-owner's unkey is ignored (auto:
        never another user's TX); with no owner the radio's own unkey runs, as for a browser, but
        only when the radio shows TX: a program that sends T 0 in a loop writes nothing to the radio."""
        if conn.closing:
            return rigctl.OK
        if self._owner() is None:
            try:
                st = getattr(self.ops.radio(), "state", None) or {}
            except Exception:
                st = {}
            if not st.get("ptt"):
                return rigctl.OK
        return self._apply(conn, {"action": "ptt", "tx": False, "auto": True})

    def _may_key(self) -> bool:
        return self.serving() and bool(self.cfg["ptt"]) and self.paused is None

    async def _key(self, conn: BridgeConn) -> int:
        """T 1 / 2 / 3. Keying needs: the bridge serving, transmit allowed, not paused, a usable
        radio; then the peer's pace and the bridge-wide keying rate; then the policy table and the
        arbiter decide like for a browser. Checked again after every wait."""
        if not self._may_key():
            self._audit("bridge_tx_refused", key="bridge:" + conn.ip, ip=conn.ip,
                        reason="paused" if (self.serving() and self.cfg["ptt"]) else "off")
            return rigctl.EACCESS
        if not self._usable():
            return rigctl.EIO
        if self._owner() == conn.id:                 # already ours: a no-op for the arbiter (F19)
            return self._apply(conn, {"action": "ptt", "tx": True})
        await self._pace(conn)
        if conn.closing or not self._may_key():
            return rigctl.EACCESS
        if not self._usable():
            return rigctl.EIO
        if not self._key_bucket.take():
            self._audit("bridge_tx_refused", key="bridge:" + conn.ip, ip=conn.ip, reason="rate")
            return rigctl.ERJCTED
        return self._apply(conn, {"action": "ptt", "tx": True})

    def _caps(self, prof: Any) -> str:
        model = rigctl.HAMLIB_MODEL.get(getattr(prof, "id", ""), rigctl.MODEL_UNKNOWN)
        return (f"Caps dump for model: {model}\n"
                f"Model name:\t{getattr(prof, 'name', '?')}\n"
                f"Mfg name:\t{getattr(prof, 'make', '?')}\n"
                f"Hamlib version:\t{self.version}\n"
                "Backend status:\tTeleRig bridge\n"
                f"Mode list: {' '.join(rigctl.hamlib_modes(prof))}\n"
                "VFO list: VFOA\n")


def _oserr(e: BaseException) -> str:
    return (getattr(e, "strerror", None) or str(e) or type(e).__name__).strip()


def _close(writer: Any) -> None:
    try:
        writer.close()
    except Exception:
        pass


def _tune_socket(writer: Any) -> None:
    """No Nagle delay on the short replies, and TCP keepalive so a peer that vanished without
    closing (a dropped link) is noticed in seconds, whatever the program's own polling does."""
    try:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, val in (("TCP_KEEPIDLE", 5), ("TCP_KEEPINTVL", 2), ("TCP_KEEPCNT", 3)):
            opt = getattr(socket, name, None)
            if opt is not None:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, opt, val)
                except OSError:
                    pass
    except Exception:
        pass
