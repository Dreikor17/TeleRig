"""Audio router unit tests — run: python -m backend.audio_router_test

In-process with a fake HostAudio, a fake hub and a fake arbiter: T43 (RX refcount, mic refcount,
binary gating to the TX owner holding ptt/vox with its mic on, I10) and T72 (the F22 server-chosen
CODEC default, pins, missing devices). No sound card, radio, socket or file is touched.
"""
import asyncio

from backend.audio_router import AudioRouter, codec_default
from backend.hub import Conn

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


class FakeHost:
    def __init__(self):
        self.calls = []
        self.tx_device = None
        self.rx_device = None
        self.written = []

    def start_rx(self, dev):
        self.calls.append(("start_rx", dev))
        self.rx_device = dev

    def stop_rx(self):
        self.calls.append(("stop_rx",))
        self.rx_device = None

    def start_tx(self, dev):
        self.calls.append(("start_tx", dev))
        self.tx_device = dev

    def stop_tx(self):
        self.calls.append(("stop_tx",))
        self.tx_device = None

    def stop(self):
        self.calls.append(("stop",))
        self.rx_device = self.tx_device = None

    def write(self, pcm):
        self.written.append(len(pcm))

    def flush_tx(self):
        self.calls.append(("flush_tx",))


class FakeHub:
    def __init__(self):
        self.conns = {}
        self.sent = []

    def send_to(self, c, f):
        self.sent.append((c.id, f))


class FakeRadio:
    def __init__(self):
        self.state = {"connected": True, "radio": "ft991a"}
        self.audio = []
        self._tp = None

    def write_audio(self, pcm):
        self.audio.append(len(pcm))


class FakeArb:
    def __init__(self):
        self.owner, self.claims = None, set()


ONE = {"available": True, "inputs": [{"id": 1, "name": "Microphone (Realtek)"}, {"id": 4, "name": "Microphone (USB Audio CODEC)"}],
       "outputs": [{"id": 2, "name": "Speakers (Realtek)"}, {"id": 5, "name": "Speakers (USB Audio CODEC)"}]}
TWO = {"available": True, "inputs": [{"id": 4, "name": "Microphone (USB Audio CODEC)"},
                                     {"id": 7, "name": "Microphone (2- USB Audio CODEC)"}],
       "outputs": [{"id": 5, "name": "Speakers (USB Audio CODEC)"}]}


async def blocking(fn, *a):
    return fn(*a)


def make(devices=ONE, pins=None, kind="serial"):
    hub, host, radio, arb = FakeHub(), FakeHost(), FakeRadio(), FakeArb()
    r = AudioRouter(hub, host, lambda: radio, lambda: kind, pins_getter=lambda: pins or {},
                    list_devices=lambda: devices, available=lambda: True, run_blocking=blocking)
    r.arb = arb
    return r, hub, host, radio, arb


def conn(hub, cid, role="operator"):
    c = Conn(cid, None, user=f"U{cid}", role=role)
    hub.conns[cid] = c
    return c


async def main():
    # -- T43: RX refcount ------------------------------------------------------------------------
    r, hub, host, radio, arb = make()
    a, b, v = conn(hub, 1), conn(hub, 2), conn(hub, 3, "viewer")
    await r.set_rx(a, True)
    await r.set_rx(b, True)
    await r.set_rx(v, True)
    C("T43 three RX subscribers -> capture started ONCE (default CODEC id 4)",
      [c for c in host.calls if c[0] == "start_rx"], [("start_rx", 4)])
    await r.set_rx(a, False)
    await r.set_rx(v, False)
    C("T43 ... still capturing while one subscriber remains", [c for c in host.calls if c[0] == "stop_rx"], [])
    await r.on_conn_closed(b)
    C("T43 ... stopped once after the last one leaves", [c for c in host.calls if c[0] == "stop_rx"], [("stop_rx",)])
    # -- T43: mic refcount -------------------------------------------------------------------------
    hub.sent.clear()
    await r.set_mic(a, True)
    C("mic on: the requester hears 'ready' once the host TX device is open",
      (hub.sent, [c for c in host.calls if c[0] == "start_tx"]), ([(1, {"type": "mic", "on": True, "ready": True})], [("start_tx", 5)]))
    await r.set_mic(b, True)
    C("T43 mic refcount: TX device opened ONCE", [c for c in host.calls if c[0] == "start_tx"], [("start_tx", 5)])
    C("... the second requester hears 'ready' too (already open)", hub.sent[-1], (2, {"type": "mic", "on": True, "ready": True}))
    await r.set_mic(a, False)
    C("T43 ... kept open while another mic is on", [c for c in host.calls if c[0] == "stop_tx"], [])
    await r.set_mic(a, True)
    # -- T43 / I10: binary gating -----------------------------------------------------------------
    pcm = b"\x00\x01" * 320
    C("no owner: owner-less PCM dropped", r.mic_in(a, pcm), False)
    arb.owner, arb.claims = 1, {"ptt"}
    C("owner holding ptt + mic on -> written to the host TX card", (r.mic_in(a, pcm), host.written), (True, [640]))
    C("non-owner with its mic on -> dropped", r.mic_in(b, pcm), False)
    a.tx_audio = False
    C("owner with mic off -> dropped", r.mic_in(a, pcm), False)
    a.tx_audio = True
    v.tx_audio = True
    arb.owner = 3
    C("a viewer (never a TX owner in practice) -> dropped", r.mic_in(v, pcm), False)
    arb.owner, arb.claims = 1, {"cw"}
    C("owner holding only cw -> dropped (ptt/vox only)", r.mic_in(a, pcm), False)
    arb.claims = {"vox"}
    C("owner holding vox -> written", r.mic_in(a, pcm), True)
    C("a frame over 32 KB -> dropped", r.mic_in(a, b"\x00" * (32 * 1024 + 2)), False)
    n = 0
    for _ in range(200):
        n += r.mic_in(a, b"\x00" * 1024)
    C("rate limit: at most ~64 KB/s pass", n <= 64, True)
    a.closing = True
    C("a closing connection -> dropped", r.mic_in(a, pcm), False)
    a.closing = False
    # LAN: the radio's modulator
    rl, hubl, hostl, radiol, arbl = make(kind="lan")
    x = conn(hubl, 1)
    await rl.set_mic(x, True)
    C("LAN mic on opens no host device", hostl.calls, [])
    C("... and is ready at once", hubl.sent, [(1, {"type": "mic", "on": True, "ready": True})])
    hubl.sent.clear()
    await rl.set_mic(x, False)
    C("mic off: no 'ready'", hubl.sent, [])
    await rl.set_mic(x, True)
    arbl.owner, arbl.claims = 1, {"ptt"}
    C("LAN owner PCM -> radio.write_audio", (rl.mic_in(x, pcm), radiol.audio), (True, [640]))
    await rl.set_rx(x, True)
    C("LAN rx_audio on opens no host capture", hostl.calls, [])
    rs, hubs, hosts, radios_, arbs = make(kind="sim")
    y = conn(hubs, 1)
    await rs.set_rx(y, True)
    await rs.set_mic(y, True)
    C("sim: rx/mic on touch no sound card", (hosts.calls, y.rx_sub, y.tx_audio), ([], True, True))
    # flush
    r.flush_tx()
    C("flush_tx -> host flush", host.calls[-1], ("flush_tx",))

    # -- T72: the F22 server-chosen default -------------------------------------------------------------
    r, hub, host, radio, arb = make(devices=ONE)
    c = conn(hub, 1)
    await r.refresh_devices()
    st = r.status()
    C("T72 one CODEC in/out -> rx_default / tx_default reported",
      (st["rx_default"], st["tx_default"], st["rx_pinned"]), ("Microphone (USB Audio CODEC)", "Speakers (USB Audio CODEC)", None))
    C("T72 ... default_in_use (admin notice)", r.default_in_use(), True)
    await r.set_rx(c, True)
    C("T72 ... capture starts on rx_audio on with the default", ([x for x in host.calls if x[0] == "start_rx"], c.rx_sub),
      ([("start_rx", 4)], True))
    r, hub, host, radio, arb = make(devices=TWO)
    c = conn(hub, 1)
    await r.set_rx(c, True)
    C("T72 two CODEC inputs -> rx_device_unpinned to the requester only, flag cleared",
      ([f["code"] for cid, f in hub.sent], c.rx_sub, host.calls), (["rx_device_unpinned"], False, []))
    C("T72 ... no default reported", r.status()["rx_default"], None)
    C("T72 ... but the single CODEC output is still the TX default", codec_default(TWO, "tx"), "Speakers (USB Audio CODEC)")
    r, hub, host, radio, arb = make(devices=TWO, pins={"ft991a": {"rx": "Microphone (2- USB Audio CODEC)", "tx": None}})
    c = conn(hub, 1)
    await r.set_rx(c, True)
    C("a pin wins over the default rule", [x for x in host.calls if x[0] == "start_rx"], [("start_rx", 7)])
    C("... status shows the pin, no default", (r.status()["rx_pinned"], r.status()["rx_default"]),
      ("Microphone (2- USB Audio CODEC)", None))
    r, hub, host, radio, arb = make(devices=ONE, pins={"ft991a": {"rx": "Gone Device", "tx": None}})
    c = conn(hub, 1)
    await r.set_rx(c, True)
    C("a pinned device that is not present -> rx_device_missing", [f["code"] for cid, f in hub.sent], ["rx_device_missing"])
    r, hub, host, radio, arb = make(devices={"available": True, "inputs": [], "outputs": []})
    c = conn(hub, 1)
    await r.set_mic(c, True)
    C("no CODEC output -> tx_device_unpinned, mic flag cleared", ([f["code"] for cid, f in hub.sent], c.tx_audio),
      (["tx_device_unpinned"], False))
    C("... and no 'ready' (the error frame is the only answer)", [f.get("type") for cid, f in hub.sent], ["error"])

    # -- radio change: stop all, restart for the subscribers -------------------------------------------
    r, hub, host, radio, arb = make()
    c = conn(hub, 1)
    await r.set_rx(c, True)
    await r.on_radio_change()
    C("radio change stops the host streams", (host.calls[-1], r.rx_running), (("stop",), None))
    await r.after_connect()
    C("... and restarts capture for the existing subscriber", [x for x in host.calls if x[0] == "start_rx"],
      [("start_rx", 4), ("start_rx", 4)])


asyncio.run(main())
print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
