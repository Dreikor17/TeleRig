"""Hub / Conn unit tests — run: python -m backend.hub_test

In-process, with fake WebSockets: the priority control queue (§2.11, AR-46), the drop-oldest data
queue with state superseding, ctl overflow -> 1013, sanitized state, kicks from other threads.
No server, radio, socket or file is touched.
"""
import asyncio
import json
import threading

from backend.hub import CTL_MAX, DATA_MAX, Coalescer, Conn, Hub, MenuQueue, RateWindow, TokenBucket, sanitize_state

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


class FakeWS:
    def __init__(self, block: bool = False):
        self.sent = []
        self.closed = None
        self.gate = asyncio.Event()
        if not block:
            self.gate.set()

    async def send_text(self, t):
        await self.gate.wait()
        self.sent.append(("text", t))

    async def send_bytes(self, b):
        await self.gate.wait()
        self.sent.append(("bytes", b))

    async def close(self, code=1000, reason=None):
        self.closed = (code, reason)


def kinds(ws):
    out = []
    for k, p in ws.sent:
        out.append(json.loads(p).get("type") if k == "text" else "bytes")
    return out


async def settle(n: int = 20):
    for _ in range(n):
        await asyncio.sleep(0)


async def main():
    hub = Hub()
    hub.loop = asyncio.get_running_loop()
    hub.scope_packer = lambda sweep: b"S" + bytes(sweep)

    # -- control frames jump the data queue ---------------------------------------------------
    ws = FakeWS(block=True)
    c = hub.add(ws, ip="127.0.0.1")
    hub.start_sender(c)
    for i in range(10):
        hub.broadcast_meter("swr", i, 0, "S0")
    hub.send_to(c, {"type": "denied", "action": "ptt", "reason": "role"})
    hub.broadcast_control({"type": "tx", "owner": None})
    await settle()
    ws.gate.set()
    await settle(60)
    C("control frames are sent before queued data frames", kinds(ws)[:2], ["denied", "tx"])
    C("then the data frames, in order", kinds(ws)[2:], ["meter"] * 10)

    # -- drop-oldest data queue; a newer state supersedes an older one -----------------------------
    ws2 = FakeWS(block=True)
    c2 = hub.add(ws2)
    hub.start_sender(c2)
    await settle()
    for i in range(DATA_MAX + 20):
        hub.broadcast_meter("po", i, 0, "S0")
    await settle()
    C("data queue bounded at DATA_MAX (drop oldest; one frame may be in flight)",
      DATA_MAX - 1 <= len(c2.q) <= DATA_MAX, True)
    C("the newest data frame is kept", json.loads(c2.q[-1][1])["meter_val"], DATA_MAX + 19)
    hub.broadcast_state({"freq": 1})
    hub.broadcast_state({"freq": 2})
    await settle()
    states = [json.loads(p)["freq"] for k, p in c2.q if k == "state"]
    C("only the newest state frame waits in the queue", states, [2])
    ws2.gate.set()
    await settle(200)

    # -- ctl overflow means a dead consumer: close 1013 --------------------------------------------
    ws3 = FakeWS(block=True)
    c3 = hub.add(ws3)
    hub.start_sender(c3)
    await settle()
    for i in range(CTL_MAX + 5):
        hub.send_to(c3, {"type": "busy", "action": "ptt", "n": i})
    await settle()
    C("control queue overflow closes the socket with 1013", ws3.closed, (1013, "slow_consumer"))
    C("control frames are never dropped before that", len(c3.ctl) >= CTL_MAX, True)
    hub.remove(c3)

    # -- sanitized state ---------------------------------------------------------------------------------
    C("LAN transport label hides the address", sanitize_state({"transport": "LAN 192.168.2.40"})["transport"], "LAN")
    C("other labels unchanged", sanitize_state({"transport": "Simulator"})["transport"], "Simulator")
    C("state_frame carries type=state", hub.state_frame({"transport": "LAN 10.0.0.9", "freq": 7})["transport"], "LAN")
    ws4 = FakeWS()
    c4 = hub.add(ws4)
    hub.start_sender(c4)
    hub.broadcast_state({"transport": "LAN 192.168.2.40", "freq": 5})
    await settle(40)
    C("broadcast state never carries the LAN IP", [json.loads(p).get("transport") for k, p in ws4.sent], ["LAN"])

    # -- radio-thread broadcasts and kicks from another thread ----------------------------------------
    ws5 = FakeWS()
    c5 = hub.add(ws5)
    hub.start_sender(c5)
    seen = []
    hub.on_radio_state = lambda snap: seen.append(snap)
    t = threading.Thread(target=lambda: (hub.broadcast_state({"ptt": True, "freq": 9}),
                                         hub.broadcast_scope([1, 2])))
    t.start()
    t.join()
    await settle(60)
    C("thread broadcasts reach the loop", "state" in kinds(ws5) and "bytes" in kinds(ws5), True)
    t = threading.Thread(target=lambda: hub.kick(lambda x: x is c5, 4401, "session_revoked"))
    t.start()
    t.join()
    await settle(60)
    C("radio-state hook gets the TX snapshot", seen[-1:], [{"ptt": True, "cw_tx": None, "vox": None,
                                                         "tuner": None, "connected": None}])
    C("kick from another thread closes with the given code", ws5.closed, (4401, "session_revoked"))
    hub.on_radio_state = None

    # -- RX audio gating (A4 turns it on) ------------------------------------------------------------------
    a, b = FakeWS(), FakeWS()
    ca, cb = hub.add(a), hub.add(b)
    hub.start_sender(ca)
    hub.start_sender(cb)
    ca.rx_sub = True
    hub.rx_gating = True
    hub.broadcast_audio(b"\x00\x00" * 4)
    await settle(40)
    C("with rx_gating, audio only to rx_sub connections", (kinds(a).count("bytes"), kinds(b).count("bytes")), (1, 0))
    hub.rx_gating = False
    hub.broadcast_audio(b"\x00\x00" * 4)
    await settle(40)
    C("without rx_gating, audio to everyone (v1)", (kinds(a).count("bytes"), kinds(b).count("bytes")), (2, 1))
    C("audio frame header: 'A', 1 channel, 16 kHz", a.sent[0][1][:4], b"\x41\x01\x80\x3e")

    # -- T65 (AR-46): a saturated data queue never delays or drops the tx frame ------------------------------
    ws6 = FakeWS(block=True)
    c6 = hub.add(ws6)
    hub.start_sender(c6)
    await settle()
    for i in range(500):                                   # synthetic scope frames flood the data queue
        hub.broadcast_scope([i % 256] * 16)
    await settle()
    C("T65 data queue saturated but bounded", DATA_MAX - 1 <= len(c6.q) <= DATA_MAX, True)
    hub.broadcast_control({"type": "tx", "owner": {"conn": c6.id, "user": "OP1", "claims": ["ptt"]}})
    await settle()
    ws6.gate.set()
    await settle(400)
    k6 = kinds(ws6)
    C("T65 the tx frame naming OP1 is delivered", "tx" in k6, True)
    C("T65 ... ahead of the queued scope backlog (at most the one frame in flight before it)", k6.index("tx") <= 1, True)
    hub.remove(c6)

    # -- registry --------------------------------------------------------------------------------------------
    n = len(hub.conns)
    hub.remove(ca)
    C("remove drops the connection", len(hub.conns), n - 1)
    C("kick_all counts the open connections", hub.kick_all(4403, "server_locked"), n - 1)
    await settle(40)
    C("kick_all closed them", all(x.closed == (4403, "server_locked") for x in (b, ws, ws2, ws4)), True)
    for conn in list(hub.conns.values()):
        hub.remove(conn)
        if conn.sender:
            conn.sender.cancel()
    await settle(20)


asyncio.run(main())

C("RX gating is on by default (G-5: audio only to rx_audio subscribers)", Hub().rx_gating, True)


# -- the kick hook (server.py wires it to the TX arbiter's release, I5/I7) ----------------------------
async def kick_hook():
    hub = Hub()
    hub.loop = asyncio.get_running_loop()
    calls, threads = [], []

    def hook(c, code, reason):
        calls.append((c.id, code, reason))
        threads.append(threading.get_ident())
    hub.on_kick = hook
    ws = FakeWS()
    c = hub.add(ws)
    hub.start_sender(c)
    hub.kick_conn(c, 4401, "session_revoked")
    C("kick hook is deferred (call_soon), never run inside the kicking call", calls, [])
    await settle()
    C("... then runs once with (conn, code, reason)", calls, [(c.id, 4401, "session_revoked")])
    hub.kick_conn(c, 4401, "session_revoked")
    hub.kick_all(4403, "server_locked")
    await settle()
    C("a connection already closing is not kicked (or hooked) again", calls, [(c.id, 4401, "session_revoked")])
    calls.clear()
    threads.clear()
    c2 = hub.add(FakeWS())
    hub.start_sender(c2)
    t = threading.Thread(target=lambda: hub.kick_user(None, 4401, "signed_out"))
    t.start()
    t.join()
    await settle(60)
    C("a kick from another thread runs the hook on the loop thread", (calls, threads), ([(c2.id, 4401, "signed_out")],
                                                                                         [threading.get_ident()]))
    hub.on_kick = lambda c, code, reason: (_ for _ in ()).throw(RuntimeError("boom"))
    ws3 = FakeWS()
    c3 = hub.add(ws3)
    hub.start_sender(c3)
    hub.kick_conn(c3, 1013, "slow_consumer")
    await settle()
    C("a hook that raises never breaks the kick", (c3.closing, ws3.closed), (True, (1013, "slow_consumer")))
    for x in list(hub.conns.values()):
        hub.remove(x)
        if x.sender:
            x.sender.cancel()
    await settle(20)

asyncio.run(kick_hook())
_nl = Hub()
_nl_calls = []
_nl.on_kick = lambda c, code, reason: _nl_calls.append(reason)
_nl._kick_now(Conn(1, FakeWS()), 4401, "expired")
C("with no loop (unit use) the hook runs synchronously", _nl_calls, ["expired"])


# -- Coalescer (AR-48) -------------------------------------------------------------------------------
async def coalescer_tests():
    loop = asyncio.get_running_loop()
    applied = []
    co = Coalescer(lambda c, cmd: applied.append(dict(cmd)), lambda c, cmd: True, lambda: loop)
    c = Conn(1, None, user="OP1", role="operator")
    for i in range(300):                                   # 300 set_level af "in 1 s" (all at once)
        co.submit(c, {"action": "set_level", "target": "af", "value": i % 256})
    co.submit(c, {"action": "set_level", "target": "rf", "value": 7})
    C("coalescer: the first ~60 go straight through (cont bucket)", 55 <= len(applied) <= 62, True)
    await asyncio.sleep(0.3)
    afs = [a["value"] for a in applied if a.get("target") == "af"]
    C("coalescer: the FINAL af value reaches the radio", afs[-1], 299 % 256)
    C("coalescer: the rf value too (separate key)", [a["value"] for a in applied if a.get("target") == "rf"], [7])
    applied.clear()
    c2 = Conn(2, None, user="OP1", role="operator")
    for _ in range(300):
        co.submit(c2, {"action": "tune", "delta": 10})
    await asyncio.sleep(0.3)
    C("coalescer: 300 x tune +10 -> net delta 3000 applied", sum(a["delta"] for a in applied), 3000)
    C("coalescer: ... in far fewer radio calls", len(applied) < 100, True)
    applied.clear()
    c3 = Conn(3, None, user="OP1", role="operator")
    c3.cont_bucket.tokens = 0
    co.submit(c3, {"action": "tune", "delta": 5})
    co.submit(c3, {"action": "set_freq", "hz": 7000000})       # an absolute value supersedes pending tunes
    co.submit(c3, {"action": "tune", "delta": 100})            # ... later tunes fold into it
    await asyncio.sleep(0.2)
    C("coalescer: set_freq supersedes earlier tunes; later tunes fold in", applied, [{"action": "set_freq", "hz": 7000100}])
    applied.clear()
    blocked = Coalescer(lambda c, cmd: applied.append(cmd), lambda c, cmd: False, lambda: loop)
    c4 = Conn(4, None, user="OP1", role="operator")
    c4.cont_bucket.tokens = 0
    blocked.submit(c4, {"action": "set_freq", "hz": 1})
    await asyncio.sleep(0.2)
    C("coalescer: the X lock re-checked at flush time can drop it", (applied, c4.pending), ([], {}))
    c5 = Conn(5, None, user="OP1", role="operator")
    c5.cont_bucket.tokens = 0
    co.submit(c5, {"action": "set_freq", "hz": 1})
    co.drop(c5)
    await asyncio.sleep(0.2)
    C("coalescer: drop() on close forgets the backlog", (applied, c5.pending, c5.flush_handle), ([], {}, None))

asyncio.run(coalescer_tests())


# -- MenuQueue (AR-32) -------------------------------------------------------------------------------
class FakeLoop:
    def __init__(self):
        self.timers = []

    def call_later(self, delay, fn, *a):
        h = type("H", (), {"cancel": lambda s: None})()
        self.timers.append((delay, fn, a))
        return h

    def fire(self):
        delay, fn, a = self.timers.pop(0)
        fn(*a)
        return delay


runs = []
fl = FakeLoop()
mq = MenuQueue(lambda g: runs.append(g) or {"A": 10, "B": 5}.get(g, 3), lambda: fl)
C("menu queue: A starts at once", (mq.submit(1, "A"), runs), (None, ["A"]))
C("menu queue: B queued behind A", (mq.submit(2, "B"), runs), (None, ["A"]))
C("menu queue: A again (third conn) is coalesced, not queued twice", (mq.submit(3, "A"), list(mq.queue)), (None, [("B", 2)]))
C("menu queue: B starts len(A)*0.03 + 0.5 s after A", round(fl.fire(), 3), 0.8)
C("menu queue: ... in order, A read once", runs, ["A", "B"])
fl.fire()
C("menu queue: idle again", (mq.running, list(mq.queue)), (None, []))
for g in ("G1", "G2", "G3", "G4"):
    C(f"menu queue: conn 7 group {g} accepted", mq.submit(7, g), None)
C("menu queue: a 5th pending group from one conn -> retry_after 2", mq.submit(7, "G5"), 2)
C("menu queue: another conn is still served", mq.submit(8, "G5"), None)
for i in range(11):
    mq.submit(100 + i, f"H{i}")
C("menu queue: at most 16 in total", (len(mq.queue) + 1, mq.submit(200, "Z")), (16, 2))
mq.clear()
C("menu queue: clear() on radio change", (mq.running, list(mq.queue)), (None, []))
runs.clear()
mq2 = MenuQueue(lambda g: runs.append(g) or 2, lambda: None)
mq2.submit(1, "X")
mq2.submit(1, "Y")
C("menu queue without a loop runs synchronously", runs, ["X", "Y"])

tb = TokenBucket(3, 1)
C("token bucket: cap tokens then empty", [tb.take() for _ in range(4)], [True, True, True, False])
rw = RateWindow(5)
C("rate window: 5 per second", [rw.take() for _ in range(6)], [True] * 5 + [False])

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
