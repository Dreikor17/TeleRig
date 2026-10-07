"""Connection registry and fan-out (spec §2.11), moved out of server.py.

Each WebSocket gets a `Conn` with two outbound queues, drained by one sender task:
- `ctl`: the priority control queue (hello, tx, tx_revoked, busy, denied, error). Never dropped;
  more than CTL_MAX waiting frames means a dead consumer, and the socket is closed with 1013
  `slow_consumer`.
- `q`: the data queue (state, meter, menu, scope, audio), drop-oldest at DATA_MAX like v1; a newer
  state frame supersedes an older one still waiting.
The sender always drains `ctl` first.

Radio threads call the broadcast_* methods; delivery is scheduled onto the event loop with
call_soon_threadsafe. Broadcast state is sanitized: a LAN transport label ("LAN 192.168.x.y",
lan.py) is reduced to "LAN" so the radio's address is not shown to every client.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import struct
import time
from collections import deque
from typing import Any, Callable, Dict, Optional

AUDIO_RATE = 16000          # 16 kHz mono int16, the LAN and host-audio wire contract
CTL_MAX = 256
DATA_MAX = 64


class TokenBucket:
    """cap tokens, refilled at rate/s (monotonic). take() -> False when empty."""

    def __init__(self, cap: float, rate: float):
        self.cap, self.rate = float(cap), float(rate)
        self.tokens = float(cap)
        self.t = time.monotonic()

    def _fill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
        self.t = now

    def take(self, n: float = 1.0) -> bool:
        self._fill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def wait_time(self, n: float = 1.0) -> float:
        self._fill()
        return 0.0 if self.tokens >= n else (n - self.tokens) / self.rate


class RateWindow:
    """At most `limit` units per rolling 1 s window."""

    def __init__(self, limit: float):
        self.limit = float(limit)
        self.events: deque = deque()
        self.total = 0.0

    def take(self, n: float = 1.0) -> bool:
        now = time.monotonic()
        while self.events and now - self.events[0][0] >= 1.0:
            self.total -= self.events.popleft()[1]
        if self.total + n > self.limit:
            return False
        self.events.append((now, n))
        self.total += n
        return True


class Conn:
    """One WebSocket connection. Identity: the signed-in user, the EFFECTIVE role (refreshed on
    every non-fast-path message, F17), the user generation and the session token (re-checked
    every 30 s for expiry and revocation)."""

    def __init__(self, cid: int, ws: Any, *, user: Optional[str] = None, role: Optional[str] = None,
                 gen: int = 0, token: Optional[str] = None, token_fp: Optional[str] = None,
                 ip: str = "", direct_local: bool = False, exp: int = 0):
        self.id = cid
        self.ws = ws
        self.user, self.role, self.gen = user, role, gen
        self.token, self.token_fp, self.exp = token, token_fp, exp
        self.ip, self.direct_local = ip, direct_local
        self.opened = time.time()
        # queues
        self.ctl: deque = deque()
        self.q: deque = deque()
        self._wake = asyncio.Event()
        self.sender: Optional[asyncio.Task] = None
        self.closing = False
        # liveness
        self.last_hb = 0.0
        self.last_rx = time.monotonic()
        # audio flags (A4)
        self.rx_sub = False
        self.tx_audio = False
        # limits (A4)
        self.bucket = TokenBucket(60, 30)
        self.cont_bucket = TokenBucket(60, 40)
        self.pending: dict = {}
        self.menu_reads = RateWindow(5)
        self.audio_rate = RateWindow(65536)
        self.denied_at = 0.0

    def __repr__(self) -> str:
        return f"<Conn {self.id} {self.user or '-'} {self.ip}>"


def sanitize_state(state: dict) -> dict:
    s = dict(state)
    t = s.get("transport")
    if isinstance(t, str) and t.startswith("LAN "):
        s["transport"] = "LAN"
    return s


class Hub:
    """Bridges radio threads -> websocket clients via the asyncio loop."""

    def __init__(self) -> None:
        self.conns: Dict[int, Conn] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._ids = itertools.count(1)
        self.scope_packer: Optional[Callable[[Any], bytes]] = None   # server.py binds _pack_scope
        self.on_radio_state: Optional[Callable[[dict], None]] = None  # A4: TX arbiter hook
        # A4/G-5: RX audio only to connections that subscribed with rx_audio. Until that action
        # exists, audio goes to every connection (v1 behaviour).
        self.rx_gating = False

    # -- registry ---------------------------------------------------------------------------
    def add(self, ws: Any, **ident) -> Conn:
        conn = Conn(next(self._ids), ws, **ident)
        self.conns[conn.id] = conn
        return conn

    def remove(self, conn: Conn) -> None:
        conn.closing = True
        self.conns.pop(conn.id, None)
        conn._wake.set()

    @property
    def clients(self) -> list:
        return list(self.conns.values())

    def count_user(self, user: str) -> int:
        return sum(1 for c in self.conns.values() if c.user == user)

    # -- threading ----------------------------------------------------------------------------
    def _on_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    def _call(self, fn: Callable, *a) -> None:
        """Run fn on the loop thread: directly when already there, else call_soon_threadsafe."""
        loop = self.loop
        if loop is None:
            return
        if self._on_loop():
            fn(*a)
        else:
            try:
                loop.call_soon_threadsafe(fn, *a)
            except RuntimeError:                 # loop closed (shutdown)
                pass

    # -- queues (loop thread only) ------------------------------------------------------------
    def _put_ctl(self, conn: Conn, item: tuple) -> None:
        if conn.closing:
            return
        if len(conn.ctl) >= CTL_MAX:
            self._kick_now(conn, 1013, "slow_consumer")
            return
        conn.ctl.append(item)
        conn._wake.set()

    def _put_data(self, conn: Conn, item: tuple) -> None:
        if conn.closing:
            return
        if item[0] == "state":                   # a newer state supersedes a queued older one
            for old in [x for x in conn.q if x[0] == "state"]:
                conn.q.remove(old)
        while len(conn.q) >= DATA_MAX:
            conn.q.popleft()                     # drop oldest, keep the stream live
        conn.q.append(item)
        conn._wake.set()

    def _fanout(self, item: tuple, pred: Optional[Callable[[Conn], bool]] = None) -> None:
        for c in list(self.conns.values()):
            if pred is None or pred(c):
                self._put_data(c, item)

    async def run_sender(self, conn: Conn) -> None:
        """Drain ctl first, then q. Ends when the connection closes or the send fails."""
        ws = conn.ws
        try:
            while True:
                if conn.ctl:
                    kind, payload = conn.ctl.popleft()
                elif conn.q:
                    kind, payload = conn.q.popleft()
                else:
                    if conn.closing:
                        return
                    conn._wake.clear()
                    await conn._wake.wait()
                    continue
                if kind == "bytes":
                    await ws.send_bytes(payload)
                else:
                    await ws.send_text(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass                                 # socket gone; the receive loop cleans up

    def start_sender(self, conn: Conn) -> asyncio.Task:
        conn.sender = asyncio.ensure_future(self.run_sender(conn))
        return conn.sender

    # -- targeted sends -------------------------------------------------------------------------
    def send_to(self, conn: Conn, obj: dict, *, control: bool = True) -> None:
        """One frame to one connection. Control frames use the priority queue."""
        text = json.dumps(obj)
        if control:
            self._call(self._put_ctl, conn, ("text", text))
        else:
            self._call(self._put_data, conn, ("state" if obj.get("type") == "state" else "text", text))

    def broadcast_control(self, obj: dict, pred: Optional[Callable[[Conn], bool]] = None) -> None:
        text = json.dumps(obj)

        def deliver():
            for c in list(self.conns.values()):
                if pred is None or pred(c):
                    self._put_ctl(c, ("text", text))
        self._call(deliver)

    def state_frame(self, state: dict) -> dict:
        return {"type": "state", **sanitize_state(state)}

    # -- radio callbacks (any thread) ---------------------------------------------------------
    def broadcast_state(self, state: dict) -> None:
        item = ("state", json.dumps(self.state_frame(state)))
        self._call(self._fanout, item)
        hook = self.on_radio_state
        if hook is not None:
            snap = {k: state.get(k) for k in ("ptt", "cw_tx", "vox", "tuner", "connected")}
            self._call(hook, snap)

    def broadcast_scope(self, sweep: Any) -> None:
        if self.scope_packer is None:
            return
        self._call(self._fanout, ("bytes", self.scope_packer(sweep)))

    def broadcast_audio(self, pcm: bytes) -> None:
        # 'A' tag + channels + sample-rate header, then 16-bit LE mono PCM
        item = ("bytes", struct.pack("<BBH", 0x41, 1, AUDIO_RATE) + pcm)
        pred = (lambda c: c.rx_sub) if self.rx_gating else None
        self._call(self._fanout, item, pred)

    def broadcast_menu(self, values: dict) -> None:
        # SET-menu values ride a SEPARATE channel so the 154-item table never bloats the
        # high-rate state frames.
        self._call(self._fanout, ("text", json.dumps({"type": "menu", "values": values})))

    def broadcast_meter(self, meter: str, val: int, smeter: int, smeter_s: str) -> None:
        # the meter changes ~every poll; keep it OFF the heavy full-state frame so it can
        # fan out fast on its own lightweight channel (like scope / menu).
        self._call(self._fanout, ("text", json.dumps({"type": "meter", "meter": meter, "meter_val": val,
                                                      "smeter": smeter, "smeter_s": smeter_s})))

    # -- closing --------------------------------------------------------------------------------
    def _kick_now(self, conn: Conn, code: int, reason: str) -> None:
        if conn.closing:
            return
        conn.closing = True
        if conn.sender is not None:
            conn.sender.cancel()
        conn._wake.set()

        async def close():
            try:
                await conn.ws.close(code=code, reason=reason)
            except Exception:
                pass
        if self.loop is not None:
            self.loop.create_task(close())

    def kick(self, pred: Callable[[Conn], bool], code: int, reason: str) -> int:
        """Close every connection matching pred with (code, reason). Callable from any thread;
        the closes are scheduled on the loop. Returns how many matched (at call time)."""
        targets = [c for c in list(self.conns.values()) if pred(c)]

        def do():
            for c in targets:
                self._kick_now(c, code, reason)
        self._call(do)
        return len(targets)

    def kick_conn(self, conn: Conn, code: int, reason: str) -> None:
        self._call(self._kick_now, conn, code, reason)

    def kick_all(self, code: int, reason: str) -> int:
        return self.kick(lambda c: True, code, reason)

    def kick_user(self, user: str, code: int, reason: str) -> int:
        """Close every socket of this user (gen change, delete, sign-out-everywhere)."""
        return self.kick(lambda c: c.user == user, code, reason)

    def kick_fp(self, token_fp: str, code: int, reason: str) -> int:
        """Close every socket opened with this session token (logout revokes the token)."""
        return self.kick(lambda c: c.token_fp == token_fp, code, reason)
