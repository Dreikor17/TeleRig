"""Hub / Conn unit tests — run: python -m backend.hub_test

In-process, with fake WebSockets: the priority control queue (§2.11, AR-46), the drop-oldest data
queue with state superseding, ctl overflow -> 1013, sanitized state, kicks from other threads.
No server, radio, socket or file is touched.
"""
import asyncio
import json
import threading

from backend.hub import CTL_MAX, DATA_MAX, Hub, RateWindow, TokenBucket, sanitize_state

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

tb = TokenBucket(3, 1)
C("token bucket: cap tokens then empty", [tb.take() for _ in range(4)], [True, True, True, False])
rw = RateWindow(5)
C("rate window: 5 per second", [rw.take() for _ in range(6)], [True] * 5 + [False])

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
