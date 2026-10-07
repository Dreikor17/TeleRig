"""WS frame gate tests — run: python -m backend.ws_gate_test

In-process checks of server._on_text / server._on_bytes for a kicked (closing) connection and for
a locked server (A2 review fix). Such a connection may act on nothing but the fast-path stops
(policy.is_fast). Without this gate, a client that ignores the server's close frame keeps
commanding the radio until the close timeout (about 10 s).

`server.radio` and `server.host_audio` are replaced by recorders BEFORE any frame is fed, so no
radio, transport, sound card, server, socket or file is touched. Nothing can transmit: the real
Radio objects are never connected, and the recorder only appends to a list.
"""
import json

from backend import server
from backend.hub import Conn
from backend.store import StoreCorrupt

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


class Recorder:
    """Stands in for the radio and the host-audio object: every method call is recorded."""

    def __init__(self):
        self.calls = []
        self.state = {}
        self.tx_device = None

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def call(*a, **kw):
            self.calls.append((name, *a))
        return call


C("the real radios have no transport and are not connected",
  (server._icom._tp, server._yaesu._tp,
   bool(server._icom.state.get("connected")), bool(server._yaesu.state.get("connected"))),
  (None, None, False, False))

rec = Recorder()
audio = Recorder()
server.radio = rec
server.host_audio = audio

TEXT = {
    "set_freq": {"action": "set_freq", "hz": 145333000},
    "tune": {"action": "tune", "delta": 100},
    "menu_write": {"action": "menu_write", "num": 36, "value": 0},
    "ptt_on": {"action": "ptt", "tx": True},
    "ptt_int": {"action": "ptt", "tx": 1},
    "cw_tx": {"action": "cw_tx", "text": "CQ", "wpm": 20},
    "tune_atu": {"action": "tune_atu"},
    "vox_on": {"action": "rx_func", "name": "vox", "on": True},
    "nb_off": {"action": "rx_func", "name": "nb", "on": False},
    "unknown": {"action": "no_such_action"},
}
STOPS = {
    "ptt_off": ({"action": "ptt", "tx": False}, ("set_ptt", False)),
    "cw_stop": ({"action": "cw_stop"}, ("stop_cw",)),
    "vox_off": ({"action": "rx_func", "name": "vox", "on": False}, ("set_rx_func", "vox", False)),
}


def feed(conn, cmd):
    rec.calls.clear()
    server._on_text(conn, json.dumps(cmd))
    return list(rec.calls)


def feed_bytes(conn, tx_device):
    rec.calls.clear()
    audio.calls.clear()
    audio.tx_device = tx_device
    server._on_bytes(conn, b"\x00\x01" * 32)
    return rec.calls + audio.calls


# -- control: an open connection on an unlocked server reaches the (recorded) radio -----------------
server._locked = None
live = Conn(1, None)
C("open: set_freq reaches the radio", feed(live, TEXT["set_freq"]), [("set_freq", 145333000)])
C("open: ptt false reaches the radio", feed(live, STOPS["ptt_off"][0]), [("set_ptt", False)])
C("open: mic PCM -> the radio's modulator (LAN)", [c[0] for c in feed_bytes(live, None)], ["write_audio"])
C("open: mic PCM -> the host TX card (serial)", [c[0] for c in feed_bytes(live, 3)], ["write"])


def gated(label, conn):
    for name, cmd in TEXT.items():
        C(f"{label}: {name} is dropped", feed(conn, cmd), [])
    for name, (cmd, want) in STOPS.items():
        C(f"{label}: {name} still unkeys (AR-17)", feed(conn, cmd), [want])
    C(f"{label}: mic PCM is dropped (LAN)", feed_bytes(conn, None), [])
    C(f"{label}: mic PCM is dropped (serial)", feed_bytes(conn, 3), [])
    C(f"{label}: oversized / malformed frames still never raise",
      (server._on_text(conn, "x" * 9000), server._on_text(conn, "{bad"), server._on_text(conn, "[1]")),
      (None, None, None))


# -- a kicked connection (its close frame is out; the client ignores it) ----------------------------
kicked = Conn(2, None)
kicked.closing = True
gated("kicked", kicked)

# -- a locked server: every socket was kicked, but the gate holds on its own too --------------------
server._locked = StoreCorrupt("config.json", "test")
gated("locked", Conn(3, None))
server._locked = None

# -- unlocking does not revive a kicked connection ---------------------------------------------------
C("unlocked: the kicked connection still acts on nothing", feed(kicked, TEXT["set_freq"]), [])
C("unlocked: an open connection works again", feed(Conn(4, None), TEXT["set_freq"]), [("set_freq", 145333000)])

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
