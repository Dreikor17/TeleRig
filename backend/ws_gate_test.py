"""WS frame gate tests — run: python -m backend.ws_gate_test

In-process checks of server._on_text / server._on_bytes for a kicked (closing) connection and for
a locked server (A2 review fix). Such a connection may act on nothing but the fast-path stops
(policy.is_fast). Without this gate, a client that ignores the server's close frame keeps
commanding the radio until the close timeout (about 10 s).

`server.radio` and `server.host_audio` are replaced by recorders BEFORE any frame is fed, so no
radio, transport, sound card, server, socket or file is touched. Nothing can transmit: the real
Radio objects are never connected, and the recorder only appends to a list. A3: connections carry
an identity; `server.accounts.get` is replaced by a fake user table (no auth.json is read or
created), and `server.hub.kick_conn` by a recorder.
"""
import json
import time

from backend import accounts, profiles, server
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
server.audio.host = audio            # the audio router writes mic PCM to the (recorded) host card

# fake account table: OP1 operator gen 3, V1 viewer gen 1, MIG migrated_v1 admin gen 2
USERS = {n: accounts.User(n, "x", role, gen, False, False, mig, None, None, None, None)
         for n, role, gen, mig in (("OP1", "operator", 3, False), ("V1", "viewer", 1, False),
                                   ("MIG", "admin", 2, True))}
server.accounts.get = lambda name: USERS.get(name)
kicks = []
server.hub.kick_conn = lambda c, code, reason: kicks.append((c.id, code, reason))


def mk(cid, user="OP1", role="operator", gen=3, direct_local=False):
    return Conn(cid, None, user=user, role=role, gen=gen, direct_local=direct_local)

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
# The recorder pretends to be a connected IC-9700 so the policy's capability checks pass; it is still
# only a list (nothing can transmit).
rec.state = {"connected": True, "radio": "ic9700", "mode_name": "USB"}
rec.profile = profiles.PROFILES["ic9700"]
server._locked = None
live = mk(1)
C("open: set_freq reaches the radio", feed(live, TEXT["set_freq"]), [("set_freq", 145333000)])
C("open: ptt false with no TX owner runs the stop primitive", feed(live, STOPS["ptt_off"][0]), [("set_ptt", False)])
C("open: mic PCM without a TX claim is dropped (I10)", feed_bytes(live, None), [])
C("open: ptt true -> the arbiter keys once", feed(live, TEXT["ptt_on"]), [("set_ptt", True)])
C("open: ... and owns TX", (server.arb.owner, sorted(server.arb.claims)), (1, ["ptt"]))
C("open: owner + ptt but mic off: PCM dropped", feed_bytes(live, None), [])
live.tx_audio = True
C("open: owner + ptt + mic on: PCM -> the radio's modulator (LAN/sim path)",
  [c[0] for c in feed_bytes(live, None) if c[0] != "flush_tx"], ["write_audio"])
real_kind = server._transport_kind
server._transport_kind = lambda: "serial"
C("open: ... -> the host TX card (serial)", [c[0] for c in feed_bytes(live, 3)], ["write"])
C("open: ... serial with no TX device open: dropped", feed_bytes(live, None), [])
server._transport_kind = real_kind
other = mk(30, "OP1", "operator", 3)
other.tx_audio = True
C("open: a non-owner's PCM is dropped even with its mic on", feed_bytes(other, None), [])
C("open: the owner's ptt false releases (one unkey)", feed(live, STOPS["ptt_off"][0]), [("set_ptt", False)])
C("open: ... and TX is free again", server.arb.owner, None)
live.tx_audio = False


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
kicked = mk(2)
kicked.closing = True
gated("kicked", kicked)

# -- a locked server: every socket was kicked, but the gate holds on its own too --------------------
server._locked = StoreCorrupt("config.json", "test")
gated("locked", mk(3))
server._locked = None

# -- A4: a locked server still lets the TX owner unkey; entering locked mode releases app-keyed TX ------
own = mk(40)
C("owner keys (unlocked)", feed(own, TEXT["ptt_on"]), [("set_ptt", True)])
server._locked = StoreCorrupt("auth.json", "test")
C("locked: the owner's ptt false still releases (fast path, AR-17)", feed(own, STOPS["ptt_off"][0]), [("set_ptt", False)])
C("locked: ... TX free", server.arb.owner, None)
server._locked = None
feed(own, TEXT["ptt_on"])
rec.state["ptt"] = True
rec.calls.clear()
server._enter_locked(StoreCorrupt("auth.json", "test"))
C("entering locked mode force-releases app-keyed TX (I6)", (("set_ptt", False) in rec.calls, server.arb.owner), (True, None))
server._locked = None
rec.state["ptt"] = False

# -- unlocking does not revive a kicked connection ---------------------------------------------------
C("unlocked: the kicked connection still acts on nothing", feed(kicked, TEXT["set_freq"]), [])
C("unlocked: an open connection works again", feed(mk(4), TEXT["set_freq"]), [("set_freq", 145333000)])

# -- A3: identity on every message ---------------------------------------------------------------------
viewer = mk(5, "V1", "viewer", 1)
C("viewer: set_freq is denied (coarse rule, T14a) and never reaches the radio", feed(viewer, TEXT["set_freq"]), [])
C("viewer: ptt false (fast path, last-known role) never reaches the radio (I11)", feed(viewer, STOPS["ptt_off"][0]), [])
C("viewer: menu_read reaches the radio (F23)", feed(viewer, {"action": "menu_read", "num": 41}), [("get_menu", 41)])
kicks.clear()
stale = mk(6, "OP1", "operator", 2)                       # its generation is older than the user's (3)
C("stale generation: set_freq dropped", feed(stale, TEXT["set_freq"]), [])
C("stale generation: socket closed 4401 session_revoked", kicks, [(6, 4401, "session_revoked")])
kicks.clear()
gone = mk(7, "NOBODY", "operator", 1)
C("deleted user: set_freq dropped", feed(gone, TEXT["set_freq"]), [])
C("deleted user: socket closed 4401 session_revoked", kicks, [(7, 4401, "session_revoked")])
kicks.clear()
gone2 = mk(8, "NOBODY", "operator", 1)
C("deleted user: a fast-path stop still unkeys (no store read, AR-17)", feed(gone2, STOPS["ptt_off"][0]),
  [STOPS["ptt_off"][1]])
C("... and is not what closes the socket", kicks, [])
mig_remote = mk(9, "MIG", "admin", 2, direct_local=False)
feed(mig_remote, TEXT["set_freq"])
C("F17: a migrated ADMIN off the host PC is refreshed to operator", mig_remote.role, "operator")
mig_local = mk(10, "MIG", "operator", 2, direct_local=True)
feed(mig_local, TEXT["set_freq"])
C("F17: a migrated ADMIN on the host PC is an admin", mig_local.role, "admin")
promoted = mk(11, "V1", "operator", 1)                    # role refreshed from the store each message
C("role refreshed from the store: a stale 'operator' viewer is denied", feed(promoted, TEXT["set_freq"]), [])
C("... and now carries the stored role", promoted.role, "viewer")


def bad_get(name):
    raise StoreCorrupt("auth.json", "test")


server.accounts.get = bad_get
entered = []
real_enter = server._enter_locked
server._enter_locked = lambda e: entered.append(str(e.path))
C("corrupt auth.json during a message: set_freq dropped", feed(mk(12), TEXT["set_freq"]), [])
C("... and locked mode is entered", entered, ["auth.json"])
C("... while a stop still unkeys (fast path, no store read)", feed(mk(13), STOPS["cw_stop"][0]), [STOPS["cw_stop"][1]])
server._enter_locked = real_enter
server.accounts.get = lambda name: USERS.get(name)

# -- the 30 s token revalidation ------------------------------------------------------------------------
real = (server.accounts.parse_token, server.accounts.is_revoked, server.hub.conns)
now = time.time()
server.hub.conns = {}
cases = {21: ("OP1", 3, now + 999, "good", False), 22: ("OP1", 3, now - 1, "good", False),
         23: ("OP1", 3, now + 999, "bad", False), 24: ("OP1", 3, now + 999, "good", True),
         25: ("OP1", 2, now + 999, "good", False), 26: ("NOBODY", 1, now + 999, "good", False)}
for cid, (user, gen, exp, tok, revoked) in cases.items():
    c = Conn(cid, None, user=user, role="operator", gen=gen, token=tok, token_fp=f"fp{cid}", exp=int(exp))
    server.hub.conns[cid] = c
server.accounts.parse_token = lambda t: object() if t == "good" else None
server.accounts.is_revoked = lambda fp: fp == "fp24"
kicks.clear()
server._revalidate_conns()
C("revalidation: expired / bad token / revoked / old gen / deleted user are closed, the good one kept",
  sorted(kicks), [(22, 4401, "expired"), (23, 4401, "session_revoked"), (24, 4401, "signed_out"),
                  (25, 4401, "session_revoked"), (26, 4401, "session_revoked")])
server.accounts.parse_token, server.accounts.is_revoked, server.hub.conns = real

# -- the auth.json reload hook (A3 review round 1) --------------------------------------------------------
# A logout that does not fit the full revocation list bumps the SIGNING-OUT user's gen
# (accounts.revoke). The hook must treat that as the user's own sign-out: close only that user's
# sockets as signed_out, and keep every rate-limit key (another user's K2 lock must survive). An
# ordinary gen change (password change, admin action) still closes session_revoked and clears the
# user's keys (§2.4).
user_kicks, sudo_drops = [], []
real_hook = (server.hub.kick_user, server.accounts.revocation_bump, server.auth.sudo_drop_user)
server.hub.kick_user = lambda user, code, reason: user_kicks.append((user, code, reason))
server.auth.sudo_drop_user = lambda user: sudo_drops.append(user)
SRC = "127.0.0.1"                                           # a loopback relay: K2 applies, K1 does not
server.limiter.clear_all()
for _ in range(10):
    server.limiter.fail("ADMIN", SRC)
for _ in range(3):
    server.limiter.fail("V1", SRC)
C("setup: ADMIN's K2 key is locked", server.limiter.check("ADMIN", SRC) > 0, True)


def store_dict(gens):
    return {"secret": "a" * 64, "meta": {"limiter_reset": 0, "revoked": {}},
            "users": {n: {"gen": g} for n, g in gens.items()}}


old = store_dict({"ADMIN": 5, "OP1": 6, "V1": 7})
server.accounts.revocation_bump = lambda user, gen: (user, gen) == ("V1", 8)
server._on_accounts_change(old, store_dict({"ADMIN": 5, "OP1": 6, "V1": 8}))
C("revocation bump of V1: only V1's sockets close, as signed_out", user_kicks, [("V1", 4401, "signed_out")])
C("... V1's sudo windows dropped", sudo_drops, ["V1"])
C("... ADMIN's K2 lock survives", server.limiter.check("ADMIN", SRC) > 0, True)
C("... V1's own failure count is kept too (no unlock by signing out)",
  len(server.limiter._k2.get(("V1", SRC), ())), 3)
user_kicks.clear()
server.accounts.revocation_bump = lambda user, gen: False
server._on_accounts_change(old, store_dict({"ADMIN": 5, "OP1": 9, "V1": 7}))
C("an ordinary gen change of OP1: session_revoked", user_kicks, [("OP1", 4401, "session_revoked")])
C("... ADMIN's K2 lock still survives (not OP1's key)", server.limiter.check("ADMIN", SRC) > 0, True)
user_kicks.clear()
server._on_accounts_change(old, store_dict({"ADMIN": 10, "OP1": 6, "V1": 7}))
C("an ordinary gen change of ADMIN (e.g. a password reset) clears ADMIN's keys (spec 2.4)",
  (user_kicks, server.limiter.check("ADMIN", SRC)), ([("ADMIN", 4401, "session_revoked")], 0.0))
server.hub.kick_user, server.accounts.revocation_bump, server.auth.sudo_drop_user = real_hook
server.limiter.clear_all()

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
