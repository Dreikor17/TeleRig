"""
TeleRig server.

ASGI app (Starlette on uvicorn). Everyone signs in (per-user accounts, backend/accounts.py);
every route declares its policy (@requires, backend/policy.py) and the Gate middleware checks
locked mode, Host, Origin, JSON bodies and the session before any route runs (§2.9).
  GET  /              -> the radio UI (+ the bootstrap JSON incl. the user's layout, §3.15)
  /login, /change-password, /api/login|logout|auth_status|me* (incl. /api/me/layout)
                      -> backend/account_api.py
  /api/admin/*        -> backend/admin_api.py (users, sessions, lockouts, allowed hosts, audio and
                         transport pins, Tailscale Serve, audit)
  GET  /api/status    -> radio/connection status for this role (+ the TX owner)
  GET  /api/ports     -> {ports}: available COM ports (operator)
  POST /api/connect   -> {transport:"sim"|"serial"|"lan", radio, ...} (§2.10 rules, F20 pins)
  POST /api/disconnect
  POST /api/tx_stop   -> STOP whoever transmits (operator; the HTTP twin of WS tx_stop)
  WS   /ws            -> hello + JSON state in/out + binary scope sweeps out
  TCP  <bridge port>  -> the WSJT-X bridge (backend/bridge.py): a rigctld-compatible server, off
                         unless an admin enables it; its commands go through the same policy table
                         and TX arbiter as a WebSocket frame (_bridge_apply)

Every WS text frame goes through the policy table (backend/policy.py): role, strict argument
types, canonical commands, the TX locks. TX start/stop goes to the TX arbiter (backend/txarb.py);
RX/mic audio to the audio router (backend/audio_router.py). _handle_cmd only ever receives
canonical commands rebuilt from validated fields.

Scope sweeps are pushed as a single self-contained binary frame:
  <BBBH IIIIII> header then N amplitude bytes (see _pack_scope).
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import mimetypes
import os
import secrets
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.request

# ES-module glue files (e.g. onnxruntime-web's .mjs wasm loader) must be served with a
# JavaScript MIME type or the browser refuses to dynamically import them. Python maps
# .mjs -> text/plain on some platforms (Windows), so register it explicitly.
mimetypes.add_type("text/javascript", ".mjs")
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from urllib.parse import quote

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

from . import (__version__, account_api, accounts, admin_api, audit, auth, civ, hostaudio,
               layout_store, policy, profiles, settings, txarb)
from . import updater as updater_mod
from . import bridge as bridge_mod
from .account_api import read_json
from .audio_router import AudioRouter
from .hub import AUDIO_RATE, Coalescer, Hub, MenuQueue  # noqa: F401  (AUDIO_RATE: hostaudio.RATE must match)
from .lan import LanTransport
from .policy import requires
from .radio import Radio
from .ratelimit import limiter
from .store import StoreBusy, StoreCorrupt, StoreError, console
from .transport import SerialTransport, SimTransport, YaesuSimTransport, available_ports
from .yaesu import YaesuRadio

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

LEVEL_TARGETS = {"af": 0x01, "rf": 0x02, "sql": 0x03, "rfpwr": 0x0A,
                 "nr_level": 0x06, "nb_level": 0x12, "pbt1": 0x07, "pbt2": 0x08,
                 "mnotch_pos": 0x0D,
                 "mic": 0x0B, "comp_level": 0x0E, "mon_level": 0x15, "vox_gain": 0x16,
                 "cw_pitch": 0x09, "keyer_speed": 0x0C}   # Icom CW pitch / keyer speed

radio = Radio()


# -- update check (latest GitHub release) ------------------------------------
_REPO = "Dreikor17/TeleRig"
_VER_TTL = 3600.0                          # re-check at most hourly
_latest = {"tag": None, "url": None, "ts": 0.0}
_latest_lock = threading.Lock()


def _ver_tuple(s: str) -> tuple:
    """'v0.2.02' -> (0, 2, 2); leading-digit per dotted part, so 0.2.02 == 0.2.2."""
    out = []
    for part in (s or "").lstrip("vV").split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def _fetch_latest_release() -> None:
    tag = url = None
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "TeleRig"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag, url = data.get("tag_name"), data.get("html_url")
    except Exception:
        pass                               # offline / rate-limited / no release -> skip quietly
    with _latest_lock:
        if tag:
            _latest["tag"], _latest["url"] = tag, url
        _latest["ts"] = time.monotonic()


def _maybe_check_update() -> None:
    """Kick a background release check if the cache is empty or stale."""
    with _latest_lock:
        stale = _latest["ts"] == 0.0 or (time.monotonic() - _latest["ts"]) >= _VER_TTL
        if stale:
            _latest["ts"] = time.monotonic()   # claim the slot so callers don't fan out
    if stale:
        threading.Thread(target=_fetch_latest_release, daemon=True, name="ver-check").start()


hub = Hub()                    # connection registry + fan-out (backend/hub.py)
hub.scope_packer = lambda sweep: _pack_scope(sweep, radio.state)   # reads the CURRENT radio
# Host sound-card audio: for a serial/USB radio reached remotely, the radio's RX/TX audio is
# on the HOST's sound card (not the browser's). Capture it server-side and stream over the WS.
host_audio = hostaudio.HostAudio(hub.broadcast_audio)

# One control stack per protocol; the active one is bound to `radio` and is
# swapped on connect based on the chosen profile's protocol.
_icom = radio                 # Icom CI-V radio (created above)
_yaesu = YaesuRadio()         # Yaesu CAT radio (FT-991A) — COM-only


def _bind_radio(r) -> None:
    r.on_state = hub.broadcast_state
    r.on_scope = hub.broadcast_scope
    r.on_audio = hub.broadcast_audio
    r.on_menu = hub.broadcast_menu
    r.on_meter = hub.broadcast_meter


_bind_radio(radio)


# -- TX arbiter, audio router, continuous-setter coalescer, SET-menu queue (§2.12-§2.14) ----------
def _conn_alive(c) -> bool:
    """A live WebSocket, or a live WSJT-X bridge client (it is not in the hub: it gets no frames)."""
    return c is not None and not c.closing and (hub.conns.get(c.id) is c or bridge.has(c))


def _send_to_conn(c, frame: dict) -> None:
    """The arbiter's frame for one connection: a bridge client takes it itself (BridgeConn.deliver,
    it only looks at tx_revoked); a WebSocket gets it on its control queue."""
    deliver = getattr(c, "deliver", None)
    if callable(deliver):
        deliver(frame)
    else:
        hub.send_to(c, frame)


audio = AudioRouter(hub, host_audio, lambda: radio, lambda: _transport_kind(), log=console)
arb = txarb.TxArbiter(lambda: radio, send_to=_send_to_conn,
                      broadcast=lambda f: hub.broadcast_control(f), audit=audit.log,
                      flush_audio=lambda: audio.flush_tx(), conn_alive=_conn_alive, log=console)
audio.arb = arb
hub.on_radio_state = lambda snap: arb.on_radio_state(snap)


def _on_kick(conn, code: int, reason: str) -> None:
    """Every kick (role/password change, delete, sign-out, revocation, expiry, lock, 1013 slow
    consumer): a kicked TX owner is released NOW, not when its receive loop ends. A client that
    ignores the close frame would otherwise keep the carrier up for the close timeout plus the
    heartbeat timeout (I5, I7). Runs on the loop (Hub defers it with call_soon)."""
    try:
        arb.on_conn_closed(conn)
    except Exception as e:
        console(f"TeleRig: TX release on kick failed: {e!r}")


hub.on_kick = _on_kick


class _PolicyCtx:
    """What policy.evaluate() needs to know about the server (looked up at call time)."""
    @property
    def arb(self):
        return arb

    def radio(self):
        return radio

    def transition(self) -> bool:
        return _radio_transition


_ctx = _PolicyCtx()
_radio_transition = False          # set while /api/connect or /api/disconnect switches the radio
_connect_lock = asyncio.Lock()     # never waited on: a second caller gets 409 connecting


# -- the WSJT-X bridge (backend/bridge.py, docs/WSJTX-BRIDGE.md) ----------------------------------
def _bridge_apply(conn, cmd: dict) -> str:
    """One command from a WSJT-X bridge client: decided by the SAME policy table as a browser's
    frame (role, strict types, canonical command, the connect transition, the TX locks) and carried
    out by the same TX arbiter / dispatcher. Returns ok | busy | failed | denied:<reason>. Runs on
    the loop, so the decision and the arbiter call are atomic like a WebSocket message.

    Like a WebSocket frame, a stop (policy.is_fast: an unkey) is decided on the fast path and is
    never refused, not even while the server is locked or the connection is closing; everything
    else is refused then. Denials are audited per peer address ("bridge:<ip>"), so a program that
    retries in a loop is one coalesced line, not one per reconnect."""
    a = cmd.get("action")
    if policy.is_fast(cmd):
        v = policy.evaluate_fast(conn, cmd, _ctx)
    else:
        if _locked is not None or conn.closing:
            return "denied:locked"
        rule = policy.WS_POLICY.get(a)
        if rule is None:
            return "denied:invalid"
        v = policy.evaluate(rule, conn, cmd, _ctx)
    k = v.kind
    key = "bridge:" + str(getattr(conn, "ip", "?"))
    try:
        if k == "busy" and (v.extra or {}).get("tx_denied"):
            audit.log("tx_denied", key=key, user=conn.user, conn=conn.id, action=a,
                      owner=(v.extra or {}).get("owner"))
        elif k in ("busy", "deny"):
            audit.log("denied", key=key, user=conn.user, conn=conn.id, action=a,
                      reason="busy" if k == "busy" else v.reason)
    except Exception:
        pass
    if k == "deny":
        return "denied:" + (v.reason or "invalid")
    if k == "busy":
        return "busy"
    if k == "ignore":
        return "ok"
    if k == "claim":
        r = arb.claim(conn, v.extra["claim"], args=v.extra.get("args"))
        return "ok" if r in ("started", "added", "noop") else ("busy" if r == "busy" else "failed")
    if k == "release":
        arb.release(conn, v.extra["claim"])
        return "ok"
    if k == "noowner_stop":
        arb.stop_idle(v.extra["claims"])
        return "ok"
    if k == "plain":
        _handle_cmd(v.cmd)
        return "ok"
    return "denied:invalid"                           # never allow by default


class _BridgeOps:
    """What the bridge may do in the server (looked up at call time; see bridge.Bridge)."""

    def radio(self):
        return radio

    def usable(self) -> bool:
        return bool(radio.state.get("connected")) and not _radio_transition and _locked is None

    def new_id(self) -> int:
        return hub.new_id()

    def apply(self, conn, cmd: dict) -> str:
        return _bridge_apply(conn, cmd)

    def heartbeat(self, conn) -> None:
        arb.on_tx_hb(conn)                            # never calls the radio (I16)

    def closed(self, conn) -> None:
        arb.on_conn_closed(conn)                      # a TX owner is released at once (I5)

    def owner(self):
        return arb.owner

    def stop_owner(self, by: str) -> None:
        if bridge.owns_tx():
            arb.force_release("stopped", by=by)

    def web_port(self):
        """TeleRig's own web port (run.py exports it), never opened as the bridge port."""
        try:
            return int(os.environ.get("TELERIG_PORT") or 0) or None
        except ValueError:
            return None

    def persist_pause(self, info) -> None:
        settings.set_bridge_paused(info)              # config.json "bridge_paused" (None: removed)


_bridge_bcast = None               # the pending (debounced) status broadcast


def _bridge_broadcast() -> None:
    """The bridge status to every WebSocket: operators and admins get the peers and the error
    text, viewers the summary."""
    global _bridge_bcast
    _bridge_bcast = None
    try:
        full = {"type": "bridge", "bridge": bridge.status(True)}
        lite = {"type": "bridge", "bridge": bridge.status(False)}
        op = auth.ROLE_RANK["operator"]
        hub.broadcast_control(full, lambda c: auth.rank(c.role) >= op)
        hub.broadcast_control(lite, lambda c: auth.rank(c.role) < op)
    except Exception as e:
        console(f"TeleRig: bridge status broadcast failed: {e!r}")


def _bridge_changed() -> None:
    """Something the UI shows changed (a client came or went, pause / resume, the listener): one
    broadcast at most every 0.25 s, so a client that reconnects in a loop cannot flood the tabs."""
    global _bridge_bcast
    loop = hub.loop
    if loop is None or _bridge_bcast is not None:
        return
    try:
        _bridge_bcast = loop.call_later(0.25, _bridge_broadcast)
    except RuntimeError:
        _bridge_bcast = None


bridge = bridge_mod.Bridge(_BridgeOps(), audit=audit.log, log=console, on_change=_bridge_changed,
                           version=__version__)


_bridge_pause_restored = False     # the pause saved at the last stop was read (once, at start)
_bridge_task = None                # the running follow (single flight)


async def _bridge_follow() -> None:
    """The bridge follows config.json (an admin's save or a hand edit, picked up within a tick);
    while the server is locked, or the file is unreadable, it is off. The first readable look also
    restores a transmit pause that was in force when TeleRig stopped."""
    global _bridge_pause_restored
    try:
        cfg = None if _locked is not None else settings.bridge()
    except Exception:
        cfg = None
    if cfg is not None and not _bridge_pause_restored:
        try:
            bridge.restore_pause(settings.bridge_paused())
            _bridge_pause_restored = True
        except Exception as e:
            console(f"TeleRig: reading the saved bridge pause failed: {e!r}")
    want = dict(bridge_mod.DEFAULTS, allow=[]) if cfg is None else cfg
    if want != bridge.cfg:
        await bridge.configure(want)
    await bridge.tick()


def _bridge_kick() -> None:
    """From the watchdog tick: drop bridge clients whose radio is gone NOW (synchronous), then run
    the follow as its own task, one at a time, so the arbiter tick never waits for a listener to
    close or open."""
    global _bridge_task
    bridge.sweep()
    if _bridge_task is not None and not _bridge_task.done():
        return

    async def run():
        try:
            await _bridge_follow()
        except Exception as exc:
            console(f"TeleRig: bridge tick failed: {exc!r}")
    _bridge_task = asyncio.ensure_future(run())


def _menu_group_run(group: str) -> int:
    """Start reading one SET-menu group (the radio spaces the reads on its own thread)."""
    r = radio
    n = sum(1 for it in (getattr(r.profile, "menu", None) or []) if it.group == group)
    if n:
        _handle_cmd({"action": "menu_read_group", "group": group})
    return n


menu_queue = MenuQueue(_menu_group_run, lambda: hub.loop)


def _pack_scope(sweep: civ.ScopeSweep, state: dict) -> bytes:
    data = sweep.data[: civ.SCOPE_POINTS]
    header = struct.pack(
        "<BBBH IIIIII",
        0x53,                          # 'S' magic
        1 if sweep.mode == 1 else 0,   # 0 center / 1 fixed
        1 if sweep.out_of_range else 0,
        len(data),
        sweep.center_hz or 0,
        sweep.span_hz or state.get("span", 0),
        sweep.lower_hz or 0,
        sweep.upper_hz or 0,
        state.get("freq", 0),          # tuned freq (channel marker)
        state.get("filter_bw", 0),
    )
    return header + data


# -- HTTP routes -------------------------------------------------------------
# One list feeds both the cache-busting token and the ?v= rewrite in index() (§3.15).
ASSETS = ("style.css", "tr.js", "waterfall.js", "bandplan.js", "layout.js", "app.js", "cwtool.js",
          "account.js", "bridge.js", "device.js", "admin.js")


def _asset_version():
    """Token from frontend file mtimes so the browser refetches updated assets."""
    try:
        return str(int(max((FRONTEND / f).stat().st_mtime for f in ("index.html",) + ASSETS)))
    except OSError:
        return "0"


def _transport_kind() -> str:
    """sim | serial | lan | none — from the connected radio's actual transport object."""
    r = radio
    if not r.state.get("connected"):
        return "none"
    tp = getattr(r, "_tp", None)
    if isinstance(tp, (SimTransport, YaesuSimTransport)):
        return "sim"
    if isinstance(tp, SerialTransport):
        return "serial"
    if isinstance(tp, LanTransport):
        return "lan"
    return "none"


def status_for(p) -> dict:
    """GET /api/status for this principal (G-6): connection, transport kind, the connect
    transition, host audio (pins and the F22 server-chosen defaults), the TX owner snapshot.
    Operators and admins also get the full transport label (incl. a LAN IP) and the current
    radio's transport pin (no credentials, F20)."""
    s = radio.state
    rid = s.get("radio") if s.get("connected") else None
    try:
        lr = settings.last_radio()
    except StoreCorrupt:
        lr = None
    out = {"connected": bool(s.get("connected")), "radio": rid, "last_radio": lr,
           "transport_kind": _transport_kind(), "connecting": bool(_radio_transition),
           "host_audio": audio.status(),
           "tx": arb.snapshot(),
           "bridge": bridge.status(p is not None and auth.rank(p.role) >= auth.ROLE_RANK["operator"]),
           "autoconnect_allowed": not no_autoconnect() and not _autoconnect_spent,
           "sim_only": sim_only()}
    if p is not None and auth.rank(p.role) >= auth.ROLE_RANK["operator"]:
        out["transport"] = s.get("transport")
        try:
            out["pinned_transport"] = settings.radio_transports().get(rid or lr or "")
        except StoreCorrupt:
            out["pinned_transport"] = None
    return out


def notices_for(p, port: Optional[int] = None) -> list:
    """Banner keys for this principal (§3.15): migrated_local_only (F17 remote session); for admins
    migrated_v1_password, lockouts, audio_default:<radio> (F22) and behind_proxy_stale (a flag
    TeleRig manages — never one set by hand, e.g. "manual" — is set but Tailscale Serve is confirmed
    off for this port, AR-34; from the last known Tailscale status, refreshed in the background, so
    a page load never waits for the CLI)."""
    out = []
    if p is None:
        return out
    if p.migrated_remote:
        out.append("migrated_local_only")
    if p.role == "admin":
        try:
            u = accounts.get(p.user)
            if u is not None and u.migrated_v1:
                out.append("migrated_v1_password")
        except StoreCorrupt:
            pass
        if limiter.lockouts()["users"]:
            out.append("lockouts")
        try:
            if audio.default_in_use():
                out.append(f"audio_default:{radio.state.get('radio')}")
        except Exception:
            pass
        try:
            if port and settings.behind_proxy() and admin_api.serve_stale(port):
                out.append("behind_proxy_stale")
        except Exception:
            pass
    return out


def _boot_json(p, request) -> str:
    port = auth.bound_port(request.scope)
    layout, layout_error = layout_store.get(p.user)
    boot = {"v": 1, "user": p.user, "role": p.role, "perms": auth.perms_for(p.role),
            "port": port, "version": __version__, "version_label": VERSION_LABEL,
            "source": f"https://github.com/{_REPO}",
            "autoconnect": not no_autoconnect() and not _autoconnect_spent, "sim_only": sim_only(),
            "layout": layout, "layout_error": layout_error, "layout_bounds": layout_store.BOUNDS,
            "status": status_for(p), "migrated_remote": p.migrated_remote,
            "radios": [{"id": pid, "name": pr.name,
                        "tile_caps": {"ptt": bool(pr.capabilities.ptt),
                                      "meters": not (pr.capabilities.meters == []),
                                      "dual_watch": bool(pr.capabilities.dual_watch)}}
                       for pid, pr in profiles.PROFILES.items()],
            "notices": notices_for(p, port)}
    payload = json.dumps(boot, separators=(",", ":"))
    return payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


@requires("viewer")
async def index(request):
    p = request.state.principal
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    v = _asset_version()
    for asset in ASSETS:
        html = html.replace(f'/static/{asset}"', f'/static/{asset}?v={v}"')
    tag = f'<script id="tr-boot" type="application/json">{_boot_json(p, request)}</script>'
    if "<!--TR_BOOT-->" in html:
        html = html.replace("<!--TR_BOOT-->", tag, 1)
    else:                                            # index.html gets the placeholder in B1/B3
        html = html.replace("</head>", tag + "\n</head>", 1)
    role = p.role if p.role in policy.ROLES else "viewer"
    html = html.replace("<body>", f'<body data-role="{role}">', 1)
    html = html.replace("<!--TR_ICONS-->", account_api.sprite("sprite.svg"), 1)   # inline icons (§4.4)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@requires("viewer")
async def api_status(request):
    return JSONResponse(status_for(request.state.principal), headers={"Cache-Control": "no-store"})


@requires("viewer")
async def api_radios(request):
    return JSONResponse({"radios": [p.to_json() for p in profiles.PROFILES.values()]})


@requires("viewer")
async def api_version(request):
    _maybe_check_update()
    with _latest_lock:
        latest, url = _latest["tag"], _latest["url"]
    available = bool(latest) and _ver_tuple(latest) > _ver_tuple(__version__)
    return JSONResponse({
        "current": __version__,
        "label": VERSION_LABEL,           # what the top bar shows (a dev install: "-dev+<commit>")
        "latest": latest,
        "update_available": available,
        "url": url,
    })


@requires("operator")
async def api_ports(request):
    """{ports} only (connection status moved to /api/status, G-6); enumerated in a thread."""
    return JSONResponse({"ports": await run_in_threadpool(available_ports)})


# -- dev guards (run.py --sim-only / --no-autoconnect) -------------------------
def _env_flag(name: str) -> bool:
    """TELERIG_SIM_ONLY / TELERIG_NO_AUTOCONNECT, exported by run.py before uvicorn starts.
    Read per request. Any value except empty/0/false/no/off counts as ON (fail closed)."""
    v = os.environ.get(name)
    return v is not None and v.strip().lower() not in ("", "0", "false", "no", "off")


def sim_only() -> bool:
    return _env_flag("TELERIG_SIM_ONLY")


def no_autoconnect() -> bool:
    return _env_flag("TELERIG_NO_AUTOCONNECT")


# Set by any explicit /api/disconnect since start: afterwards a page load (auto:true connect)
# must not reconnect a radio someone deliberately disconnected.
_autoconnect_spent = False


def _connect_error(status: int, error: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error, "message": message, **extra}, status_code=status)


def _do_connect(kind: str, profile, canon: dict, creds: dict) -> tuple:
    """The v1 connect body (runs in the threadpool, so a LAN timeout never stalls the loop,
    AR-16): pick the protocol stack, build the transport from the VALIDATED fields, connect."""
    global radio
    is_yaesu = getattr(profile, "protocol", "civ") == "yaesu"
    # route to the matching protocol stack (Icom CI-V vs Yaesu CAT)
    target = _yaesu if is_yaesu else _icom
    if target is not radio:
        radio.disconnect()
        radio = target
        _bind_radio(radio)
    if kind == "serial":
        # CAT port opens with control lines at their defaults (RTS high) so CAT works
        # regardless of the radio's CAT-RTS setting. FT-991A CW keys on the SIBLING
        # (Standard) port's DTR line, opened separately — never on this CAT port.
        tp = SerialTransport(canon["port"], canon["baud"], stopbits=2 if is_yaesu else 1)
    elif kind == "lan":
        if is_yaesu:
            raise ValueError("The FT-991A is COM-only (no network CAT)")
        tp = LanTransport(canon["host"], canon["port"], creds.get("user", ""), creds.get("password", ""))
    else:
        tp = YaesuSimTransport(profile) if is_yaesu else SimTransport(profile)
    radio.connect(tp, profile)
    return radio.state["transport"], profile.id


def _busy_owner_error(p, force: bool):
    """§2.10 step 5: someone else owns TX. Non-admin -> 409 busy; admin -> 409 unless force, then
    the owner is force-released (audited `admin_switch`). None when the caller may go on."""
    if arb.owner is None or arb.owner_user == p.user:
        return None
    owner = arb.label()
    if p.role != "admin":
        return _connect_error(409, "busy", f"{owner} is transmitting.", owner=owner)
    if not force:
        return _connect_error(409, "busy", f"{owner} is transmitting; send force:true to stop it.",
                              owner=owner)
    arb.force_release("admin_switch", by=p.user, explicit=True)
    return None


def _pin_transport(rid: str, canon: dict, user: str) -> None:
    """An admin's successful connect pins the radio and its transport for operators (F20)."""
    def fn(d: dict) -> None:
        d["last_radio"] = rid
        rt = d.get("radio_transports")
        if not isinstance(rt, dict):
            rt = {}
        rt[rid] = dict(canon)                      # never LAN credentials (R12, R35)
        d["radio_transports"] = rt
    try:
        settings.mutate(fn, create=True)
        audit.log("radio_transport_pinned", user=user, radio=rid, transport=dict(canon))
    except Exception as e:
        console(f"TeleRig: could not pin the radio transport: {e!r}")


@requires("operator")
async def api_connect(request):
    """§2.10 connect rules, in order: strict types -> --sim-only (I13) -> auto (--no-autoconnect /
    spent) -> transport fields (+ the COM port really exists) -> F20 for non-admins (current radio,
    pinned transport, not already connected) -> TX owner (busy / admin force) -> connect_lock +
    radio_transition, the connect itself in the threadpool -> admin auto-pin -> audit."""
    global _radio_transition
    p = request.state.principal
    body = await read_json(request)
    if not isinstance(body, dict):
        return body                                  # 400 invalid_json / 413 too_large
    field, info = policy.connect_shape(body, profiles.PROFILES)
    if field:
        return _connect_error(400, "invalid", f"Invalid connect request: {field}.", field=field)
    kind = info["transport"]
    # Guards run before ANY side effect (audio stop, radio switch, port open).
    # --sim-only: only the simulator may be connected; serial and LAN are refused (I13).
    if sim_only() and kind != "sim":
        return _connect_error(403, "sim_only",
                              "This server runs with --sim-only: only the simulator can be connected.")
    # auto (page-load) connects: refused with --no-autoconnect or after an explicit disconnect.
    if info["auto"] and (no_autoconnect() or _autoconnect_spent):
        return _connect_error(409, "autoconnect_disabled",
                              "Automatic connect is disabled; press Connect to connect a radio.")
    profile = profiles.PROFILES[info["radio"]]
    field, canon, creds = policy.connect_canonical(body, profile)
    if field:
        return _connect_error(400, "invalid", f"Invalid connect request: {field}.", field=field)
    if kind == "serial":
        ports = await run_in_threadpool(available_ports)
        if canon["port"] not in {pp.get("device") for pp in ports}:
            return _connect_error(400, "invalid", f"No serial port {canon['port']!r} on this PC.", field="port")
    connected = bool(radio.state.get("connected"))
    if info["auto"] and connected:                   # a page load never switches a connected radio
        return _connect_error(409, "already_connected", "A radio is already connected.")
    if p.role != "admin":                            # F20 (AR-23): the current radio, on its pin only
        pins = settings.radio_transports()
        why = policy.connect_rule_error(p.role, info["radio"], canon, settings.last_radio(), pins, profile)
        if why:
            if why == "switch_radio":
                msg = "Only an admin can switch the radio."
            elif not isinstance((pins or {}).get(info["radio"]), dict):
                msg = "Ask an admin to connect this radio once."       # no pin yet (§4.5)
            else:
                msg = "Only an admin can connect this radio on another port or transport."
            return _connect_error(403, "admin_required", msg, reason=why)
        if connected:
            return _connect_error(409, "already_connected", "The radio is already connected.")
    err = _busy_owner_error(p, info["force"])
    if err is not None:
        return err
    if _connect_lock.locked():
        return _connect_error(409, "connecting", "A connect or disconnect is already in progress.")
    async with _connect_lock:
        _radio_transition = True
        try:
            if arb.owner is not None:
                arb.force_release("radio_change", by=p.user)
            bridge.on_radio_change()        # every bridge client reconnects and re-reads the radio
            menu_queue.clear()
            await audio.on_radio_change()
            try:
                label, rid = await run_in_threadpool(_do_connect, kind, profile, canon, creds)
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        finally:
            _radio_transition = False
    if p.role == "admin":
        _pin_transport(rid, canon, p.user)
    audit.log("radio_connect", user=p.user, radio=rid, transport=kind, ip=auth.client_ip(request.scope))
    _spawn(audio.after_connect())
    return JSONResponse({"ok": True, "transport": label, "radio": rid})


@requires("operator")
async def api_disconnect(request):
    """Same busy/force rule as connect; a non-admin needs confirm:true while other users are
    online (the UI asks inline); then TX is released, audio stopped and the radio disconnected in
    the threadpool (it joins threads and sleeps)."""
    global _autoconnect_spent, _radio_transition
    p = request.state.principal
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    for k in ("confirm", "force"):
        if k in body and type(body[k]) is not bool:
            return _connect_error(400, "invalid", f"Invalid disconnect request: {k}.", field=k)
    err = _busy_owner_error(p, body.get("force") is True)
    if err is not None:
        return err
    if p.role != "admin":
        n = hub.others_online(p.user)
        if n and body.get("confirm") is not True:
            return _connect_error(409, "confirm_required",
                                  f"{n} other user(s) are online; disconnecting affects them too.", online=n)
    if _connect_lock.locked():
        return _connect_error(409, "connecting", "A connect or disconnect is already in progress.")
    rid = None
    async with _connect_lock:
        _radio_transition = True
        try:
            _autoconnect_spent = True       # explicit disconnect: page loads no longer auto-connect
            arb.force_release("radio_disconnect", by=p.user)
            bridge.on_radio_change()
            menu_queue.clear()
            await audio.on_radio_change()   # release the host sound cards with the radio
            rid = radio.state.get("radio") if radio.state.get("connected") else None
            await run_in_threadpool(radio.disconnect)
        finally:
            _radio_transition = False
    if rid:
        audit.log("radio_disconnect", user=p.user, radio=rid, ip=auth.client_ip(request.scope))
    return JSONResponse({"ok": True})


BRIDGE_STOP_WINDOW = txarb.REATTACH_WINDOW   # a STOP this soon after a bridge session ended pauses it


def _tx_stop(by) -> bool:
    """STOP (WS tx_stop, POST /api/tx_stop) from `by`. Stopping a bridge client's TX pauses bridge
    transmit (its tx_revoked "stopped"). A STOP that finds nobody transmitting moments after a
    bridge session ended (the program unkeyed on its own just as the operator pressed STOP) pauses
    it too: the operator meant the program to stop, and it would key again at its next slot."""
    had_owner = arb.owner is not None
    stopped = arb.stop(by, explicit=True)
    if not had_owner:
        ls = arb.last_session or {}
        if getattr(ls.get("conn"), "kind", None) == "bridge" \
                and arb.clock() - ls.get("ended_at", -1e9) < BRIDGE_STOP_WINDOW:
            bridge.pause("stopped", by=getattr(by, "user", None))
    return stopped


@requires("operator")
async def api_tx_stop(request):
    """HTTP STOP (AR-15): stops whoever transmits (the owner's own claims, another user's TX, or a
    front-panel key). Origin-checked like any POST (Gate); bodiless or {}; never rate limited."""
    body = await read_json(request)
    if not isinstance(body, dict):
        return body
    stopped = _tx_stop(request.state.principal)
    return JSONResponse({"ok": True, "stopped": bool(stopped)})


_tasks: set = set()


def _spawn(coro) -> None:
    """Run a coroutine on the loop (fire and forget, reference kept); without a running loop
    (in-process unit tests) run it to completion now."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            asyncio.run(coro)
        except Exception as e:
            console(f"TeleRig: background task failed: {e!r}")
        return
    t = loop.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


# -- WebSocket ---------------------------------------------------------------
WS_TEXT_MAX = 8192                     # bytes; larger text frames are refused (`denied too_large`)
DENY_INTERVAL = 0.5                    # at most 2 denial frames per second per connection


HB_INTERVAL, HB_TIMEOUT = 2, 6            # the TX heartbeat contract (§2.13), announced in hello
WS_TOTAL_MAX = 32                         # sockets in all
WS_VIEWER_MAX = 24                        # viewers never take the last 8 (AR-15)
WS_USER_MAX = 8                           # per user; a migrated_v1 ADMIN shared by a club: 24 (AR-49)
WS_USER_MAX_MIGRATED = 24


def _hello(conn, p) -> dict:
    return {"type": "hello", "v": 1, "conn": conn.id, "user": p.user, "role": p.role,
            "perms": auth.perms_for(p.role), "migrated_remote": p.migrated_remote,
            "status": status_for(p), "tx": arb.snapshot(), "hb_interval": HB_INTERVAL,
            "hb_timeout": HB_TIMEOUT, "server_version": __version__, "sim_only": sim_only()}


def _too_many(p) -> bool:
    """§2.11 connection caps: 32 in all, 24 viewers, 8 per user (24 for a migrated_v1 ADMIN)."""
    cap = WS_USER_MAX
    try:
        u = accounts.get(p.user)
        if u is not None and u.migrated_v1 and u.role == "admin":
            cap = WS_USER_MAX_MIGRATED
    except StoreCorrupt:
        pass
    return (len(hub.conns) >= WS_TOTAL_MAX
            or (p.role == "viewer" and hub.count_role("viewer") >= WS_VIEWER_MAX)
            or hub.count_user(p.user) >= cap)


async def ws_endpoint(ws: WebSocket):
    # The HTTP middleware doesn't see WS upgrades — gate here. Accept FIRST so the close code
    # reaches the browser (a close before accept becomes an HTTP 403 and the page sees 1006).
    await ws.accept()
    try:
        if _check_locked() is not None:
            return await _ws_refuse(ws, 4403, "server_locked")
        host = ws.headers.get("host")
        if not auth.host_ok(host) or not auth.origin_ok(ws.headers.get("origin"), ws.scope):
            return await _ws_refuse(ws, 4403, "bad_origin")
        p = auth.identify(ws.scope)                  # per-user session; no loopback branch (I1)
    except StoreCorrupt as e:
        _enter_locked(e)
        return await _ws_refuse(ws, 4403, "server_locked")
    if p is None:
        return await _ws_refuse(ws, 4401, "unauthenticated")
    if p.restricted:
        return await _ws_refuse(ws, 4403, "password_change_required")
    if _too_many(p):
        return await _ws_refuse(ws, 4429, "too_many_connections")
    conn = hub.add(ws, user=p.user, role=p.role, gen=p.gen, token=p.token, token_fp=p.token_fp,
                   ip=auth.client_ip(ws.scope), direct_local=auth.direct_local(ws.scope), exp=p.exp)
    hub.send_to(conn, _hello(conn, p))                                # first frame (control queue)
    hub.send_to(conn, hub.state_frame(radio.state), control=False)   # then the current state
    hub.start_sender(conn)
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            conn.last_rx = time.monotonic()
            if msg.get("text") is not None:
                _on_text(conn, msg["text"])          # never raises
            elif msg.get("bytes") is not None:
                _on_bytes(conn, msg["bytes"])
    except Exception:                                # WebSocketDisconnect, closed by a kick, ...
        pass
    finally:
        # Owner-scoped (F6, §2.13): only the TX OWNER's drop unkeys, immediately; other clients'
        # drops never do. Each step on its own, in this order, so one failure never skips an
        # unkey (AR-17): arbiter -> audio router -> hub.
        try:
            arb.on_conn_closed(conn)
        except Exception as e:
            console(f"TeleRig: TX release on close failed: {e!r}")
        try:
            conn.rx_sub = conn.tx_audio = False      # this client's RX/mic refcounts only; the device
            _spawn(audio.on_conn_closed(conn))       # close runs as its own task (never cancelled here)
        except Exception:
            pass
        try:
            coalescer.drop(conn)
        except Exception:
            pass
        try:
            hub.remove(conn)
        except Exception:
            pass
        if conn.sender is not None:
            conn.sender.cancel()


async def _ws_refuse(ws: WebSocket, code: int, reason: str) -> None:
    """Close an accepted socket with an application code (the client may already be gone)."""
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        pass


def _deny(conn, action, reason: str, **extra) -> None:
    """`denied` to this connection only, on the control queue; at most 2/s per connection."""
    _reply(conn, policy.deny(action if isinstance(action, str) else "*", reason, **extra))


def _reply(conn, v) -> None:
    """A deny / busy verdict: the frame to this connection only (control queue, at most 2 denial
    frames per second per connection), and a coalesced audit entry (never one line per event)."""
    a = (v.action if isinstance(v.action, str) else "*")[:64]
    ex = v.extra or {}
    if v.kind == "busy":
        frame = {"type": "busy", "action": a, "owner": ex.get("owner")}
        if ex.get("retry_after") is not None:
            frame["retry_after"] = ex["retry_after"]
    else:
        frame = {"type": "denied", "action": a, "reason": v.reason or "invalid"}
        if ex.get("need"):
            frame["need"] = ex["need"]
    if ex.get("req"):
        frame["req"] = ex["req"]
    now = time.monotonic()
    if now - conn.denied_at >= DENY_INTERVAL:
        conn.denied_at = now
        hub.send_to(conn, frame)
    try:
        if v.kind == "busy" and ex.get("tx_denied"):
            audit.log("tx_denied", key=conn.id, user=conn.user, conn=conn.id, action=a, owner=ex.get("owner"))
        elif v.kind == "busy":
            audit.log("denied", key=conn.id, user=conn.user, conn=conn.id, action=a, reason="busy")
        elif v.reason == "rate_limited":
            audit.log("rate_limited", key=conn.id, user=conn.user, conn=conn.id, action=a)
        elif v.reason != "too_large" or not conn.closing:
            audit.log("denied", key=conn.id, user=conn.user, conn=conn.id, action=a, reason=v.reason)
    except Exception:
        pass


def _on_text(conn, text: str) -> None:
    """One client text frame: size cap, JSON parsed inside try, dict + string-action check, then
    the policy table (§2.12). Never raises, so a bad frame can never kill the socket.

    Fast path first (policy.is_fast: the unkey/heartbeat frames): decided with the connection's
    LAST-KNOWN role and no store read or rate bucket, so a store error, a lock, a kick or a flood
    never swallows an unkey (I4, AR-17). Everything else re-reads the user: a deleted user or a
    changed generation closes the socket 4401 session_revoked, and the effective role is refreshed
    (F17). Continuous setters go to the coalescer (never `rate_limited`, AR-48); everything else
    that is not rate-exempt charges the connection's token bucket.

    A kicked connection (`conn.closing`: its close frame is already out) and a locked server act on
    nothing but the fast path. A client that ignores the close frame can otherwise keep commanding
    the radio until the close timeout drops it (about 10 s). The kick itself already released the
    connection's TX (Hub.on_kick), and its tx_hb is ignored by the arbiter, so the fast path can
    only stop TX, never keep it up. The receive loop keeps running, so the 4403/4401 close code is
    not replaced."""
    try:
        if len(text) > WS_TEXT_MAX or len(text.encode("utf-8", "surrogatepass")) > WS_TEXT_MAX:
            if not conn.closing:
                _deny(conn, "*", "too_large")
            return
        try:
            cmd = json.loads(text)
        except Exception:
            return
        if not isinstance(cmd, dict) or not isinstance(cmd.get("action"), str):
            return
        if policy.is_fast(cmd):
            _dispatch(conn, policy.evaluate_fast(conn, cmd, _ctx))
            return
        if conn.closing or _locked is not None:
            return                                   # kicked or locked: dropped, nothing answered
        try:
            u = accounts.get(conn.user)
        except StoreCorrupt as e:
            _enter_locked(e)
            return
        if u is None or u.gen != conn.gen:
            hub.kick_conn(conn, 4401, "session_revoked")
            return
        conn.role = auth.effective_role(u, conn.direct_local)
        a = cmd["action"]
        rule = policy.WS_POLICY.get(a)
        if rule is None:                             # unknown, or REMOVED (host_rx / host_tx)
            _deny(conn, a, "unknown_action")
            return
        v = policy.evaluate(rule, conn, cmd, _ctx)
        if v.kind == "plain" and rule.continuous:
            coalescer.submit(conn, v.cmd)            # AR-48: never "rate_limited"
            return
        if not rule.rate_exempt and not conn.bucket.take():
            _deny(conn, a, "rate_limited")
            return
        _dispatch(conn, v)
    except Exception as e:                           # pragma: no cover - defensive
        console(f"TeleRig: WS message handling failed: {e!r}")


def _dispatch(conn, v) -> None:
    k = v.kind
    if k in ("deny", "busy"):
        _reply(conn, v)
    elif k == "ignore":                              # e.g. a non-owner's auto release (G-1)
        return
    elif k == "new":
        NEW_HANDLERS[v.action](conn, v)
    elif k == "claim":
        arb.claim(conn, v.extra["claim"], args=v.extra.get("args"), seq=v.extra.get("seq"))
    elif k == "release":
        arb.release(conn, v.extra["claim"])
    elif k == "stop":                                # tx_stop only (F18, I23)
        _tx_stop(conn)
    elif k == "noowner_stop":                        # nobody owns TX: the stop primitive, as v1
        arb.stop_idle(v.extra["claims"])
    elif k == "plain":
        _plain(conn, v)
    else:                                            # pragma: no cover - never allow by default
        _deny(conn, v.action, "invalid")


def _plain(conn, v) -> None:
    a = v.action
    ex = v.extra or {}
    if a == "menu_read" and not conn.menu_reads.take():
        return                                       # > 5/s per connection: silently dropped
    if ex.get("audit") == "panel_lock":
        audit.log("panel_lock", key=conn.id, user=conn.user, conn=conn.id, action="lock", on=v.cmd.get("on"))
    if a == "menu_write" and ex.get("critical"):
        it = ex.get("item")
        r = radio
        rid = r.state.get("radio")
        old = (getattr(r, "_menu_vals", None) or {}).get(getattr(it, "num", None))
        if ex.get("tot_off"):
            audit.log("tot_off", user=conn.user, conn=conn.id, radio=rid, num=it.num, name=it.name)
        audit.log("menu_critical", user=conn.user, conn=conn.id, radio=rid, num=it.num, name=it.name,
                  old=old, new=v.cmd.get("value"), tx_route=ex.get("tx_route"))
    _handle_cmd(v.cmd)


def _h_tx_hb(conn, v) -> None:
    arb.on_tx_hb(conn)                               # never calls the radio (I16)


def _h_rx_audio(conn, v) -> None:
    _spawn(audio.set_rx(conn, bool(v.cmd["on"])))


def _h_mic(conn, v) -> None:
    _spawn(audio.set_mic(conn, bool(v.cmd["on"])))


def _h_menu_group(conn, v) -> None:
    retry = menu_queue.submit(conn.id, v.cmd["group"])
    if retry is not None:
        _reply(conn, policy.busy("menu_read_group", None, retry_after=retry))


def _h_bridge_tx(conn, v) -> None:
    """Pause (on:false) or resume (on:true) transmit over the WSJT-X bridge (operators). Pausing
    while a bridge client transmits stops that transmission first, the same forced stop as STOP."""
    if v.cmd["on"]:
        bridge.resume(by=conn.user)
        return
    if bridge.owns_tx():
        arb.stop(conn, explicit=True)            # tx_revoked "stopped" -> the bridge pauses itself
    bridge.pause("operator", by=conn.user)       # (a no-op when the stop above already paused it)


NEW_HANDLERS = {"tx_hb": _h_tx_hb, "rx_audio": _h_rx_audio, "mic": _h_mic,
                "menu_read_group": _h_menu_group, "bridge_tx": _h_bridge_tx}


def _coalesced_apply(conn, cmd: dict) -> None:
    _handle_cmd(cmd)


def _coalesced_recheck(conn, cmd: dict) -> bool:
    """The X lock re-evaluated when a coalesced setter is flushed (AR-48)."""
    if conn.closing or _locked is not None or _radio_transition:
        return False
    x = policy._lock_x(cmd.get("action", "*"), conn, _ctx)
    if x is not None:
        _reply(conn, x)
        return False
    return True


coalescer = Coalescer(_coalesced_apply, _coalesced_recheck, lambda: hub.loop)


def _on_bytes(conn, data: bytes) -> None:
    """Binary frame = browser mic PCM: reaches the radio only from the TX owner holding ptt/vox
    with its mic on (I10, audio_router.mic_in). A kicked (closing) connection or a locked server
    feeds nothing to the transmitter."""
    try:
        if conn.closing or _locked is not None:
            return
        audio.mic_in(conn, data)
    except Exception:
        pass


def _handle_cmd(cmd: dict) -> None:
    action = cmd.get("action")
    try:
        if action == "set_freq":
            radio.set_freq(int(cmd["hz"]))
        elif action == "tune":
            radio.tune(int(cmd["delta"]))
        elif action == "set_mode":
            # Icom names map to a CI-V code; radio-specific labels (e.g. the Yaesu
            # CW-USB/CW-LSB/DATA-LSB spellings) fall through as the name, which the
            # Yaesu handler resolves directly.
            m = cmd.get("mode")
            code = civ.MODE_CODES.get(m, m)
            if code not in (None, ""):
                radio.set_mode(code, cmd.get("filter"))
        elif action == "set_filter":
            radio.set_filter(int(cmd["filter"]))
        elif action == "data_mode":
            radio.set_data(bool(cmd["on"]))
        elif action == "vfo":
            radio.select_vfo(int(cmd["code"]))
        elif action == "mem_recall":
            radio.recall_memory(int(cmd["ch"]))
        elif action == "mem_to_vfo":
            radio.memory_to_vfo()
        elif action == "set_level":
            sub = LEVEL_TARGETS.get(cmd["target"])
            if sub is not None:
                radio.set_level(sub, int(cmd["value"]))
        elif action == "band":
            radio.set_band(str(cmd["band"]))
        elif action == "set_span":
            radio.set_span(int(cmd["span"]))
        elif action == "scope_mode":
            radio.set_scope_mode(bool(cmd["center"]))
        elif action == "select_band":
            radio.select_band(str(cmd["band"]))
        elif action == "set_meter":
            radio.set_meter(str(cmd["meter"]))
        elif action == "preamp":
            radio.set_preamp(int(cmd["level"]) if "level" in cmd else (1 if cmd.get("on") else 0))
        elif action == "att":
            radio.set_att(bool(cmd["on"]))
        elif action == "lock":
            radio.set_lock(bool(cmd["on"]))
        elif action == "tuner":
            radio.set_tuner(bool(cmd["on"]))
        elif action == "tune_atu":
            radio.tune_atu()
        elif action == "atas_step":
            radio.atas_step(int(cmd["dir"]))
        elif action == "rx_func":
            radio.set_rx_func(str(cmd["name"]), bool(cmd["on"]))
        elif action == "agc":
            radio.set_agc(int(cmd["mode"]))
        elif action == "mnotch_w":
            radio.set_mnotch_w(int(cmd["width"]))
        elif action == "tbw":
            radio.set_tbw(int(cmd["w"]))
        elif action == "rit":
            radio.set_rit(bool(cmd["on"]))
        elif action == "rit_freq":
            radio.set_rit_freq(int(cmd["hz"]))
        elif action == "split":
            radio.set_split(bool(cmd["on"]))
        elif action == "duplex":
            radio.set_duplex(int(cmd["mode"]))
        elif action == "narrow":
            radio.set_narrow(bool(cmd["on"]))
        elif action == "tone_mode":
            radio.set_tone_mode(int(cmd["value"]))
        elif action == "tone_freq":
            radio.set_tone_freq(int(cmd["idx"]))
        elif action == "dcs_code":
            radio.set_dcs_code(int(cmd["idx"]))
        elif action == "rpt_shift":
            radio.set_rpt_shift(int(cmd["value"]))
        elif action == "width":
            radio.set_width(int(cmd["code"]))
        elif action == "contour":
            radio.set_contour(bool(cmd["on"]))
        elif action == "contour_freq":
            radio.set_contour_freq(int(cmd["hz"]))
        elif action == "apf":
            radio.set_apf(bool(cmd["on"]))
        elif action == "apf_freq":
            radio.set_apf_freq(int(cmd["v"]))
        elif action == "txw":
            radio.set_txw(bool(cmd["on"]))
        elif action == "param_eq":
            radio.set_param_eq(bool(cmd["on"]))
        elif action == "quick_split":
            radio.set_quick_split()
        elif action == "bkin":
            radio.set_bkin(bool(cmd["on"]))
        elif action == "keyer":
            radio.set_keyer(bool(cmd["on"]))
        elif action == "key_speed":
            radio.set_key_speed(int(cmd["wpm"]))
        elif action == "key_pitch":
            radio.set_key_pitch(int(cmd["code"]))
        elif action == "spot":
            radio.set_spot(bool(cmd["on"]))
        elif action == "zero_in":
            radio.set_zero_in()
        elif action == "scan":
            radio.set_scan(int(cmd["dir"]))
        elif action == "fast":
            radio.set_fast(bool(cmd["on"]))
        elif action == "apf_lvl":
            radio.set_apf_lvl(int(cmd["v"]))
        elif action == "bkin_lvl":
            radio.set_bkin_lvl(int(cmd["v"]))
        elif action == "filter_shape":
            radio.set_filter_shape(bool(cmd["on"]))
        elif action == "ptt":
            radio.set_ptt(bool(cmd["tx"]))
        elif action == "cw_tx":
            radio.send_cw(str(cmd.get("text", "")), int(cmd.get("wpm", 18)))
        elif action == "cw_stop":
            radio.stop_cw()
        elif action == "menu_read":
            radio.get_menu(int(cmd["num"]))
        elif action == "menu_read_group":
            radio.read_menu_group(str(cmd["group"]))
        elif action == "menu_write":
            radio.set_menu(int(cmd["num"]), cmd["value"])
    except (KeyError, ValueError, TypeError):
        pass


# -- locked mode (I6, §2.4) ----------------------------------------------------
# A corrupt auth.json or an unparsable config.json locks the server: every HTTP request gets 503,
# every WebSocket is closed with 4403 server_locked, nothing is defaulted or written. Fixing the
# file (or `--reset-admin`) unlocks it without a restart (the watchdog re-checks).
_locked: Optional[StoreCorrupt] = None


def _recovery_cmd() -> str:
    return "run.bat --reset-admin" if sys.platform == "win32" else "python3 run.py --reset-admin"


def _locked_message(e: StoreCorrupt, local: bool = True) -> str:
    """The locked-mode text. The console and a browser on the host PC itself (local) get the file's
    full path and the parse error; anyone else gets the file's NAME only: the folder (it carries the
    OS user name) and the file's content details are not for unauthenticated network clients."""
    if local:
        return (f"TeleRig is locked: {e.path} cannot be read ({e.detail or 'corrupt'}). Nothing has been "
                f"changed. On the host PC, fix that file or move it aside (or run {_recovery_cmd()}); "
                "TeleRig unlocks within a few seconds, no restart needed.")
    name = Path(str(e.path)).name if e.path else ""
    what = f"its {name}" if name else "a settings file"
    return (f"TeleRig is locked: {what} on the host PC cannot be read. Nothing has been changed. On the "
            f"host PC, fix that file or move it aside (the TeleRig window shows where it is), or run "
            f"{_recovery_cmd()}; TeleRig unlocks within a few seconds, no restart needed.")


def _locked_local(scope) -> bool:
    """auth.direct_local() for the locked page, failing closed: with config.json unreadable,
    behind_proxy is unknown, so no request counts as on the host PC (the console has the path)."""
    try:
        return auth.direct_local(scope)
    except Exception:
        return False


def _store_problem() -> Optional[StoreCorrupt]:
    """Re-reads both stores (subject to their stat/hash intervals); an accounts reload fires the
    reload hook (_on_accounts_change), which kicks revoked sessions."""
    try:
        accounts.view()
        settings.view()
    except StoreCorrupt as e:
        return e
    return None


def _enter_locked(e: StoreCorrupt) -> None:
    """Entering locked mode (from any load or the watchdog) releases app-keyed TX (a front-panel
    key is left alone), stops host audio and kicks every socket with 4403 server_locked (I6)."""
    global _locked
    entering = _locked is None
    _locked = e
    if not entering:
        return
    console("\n  " + _locked_message(e) + "\n")
    try:
        arb.force_release("server_locked")
    except Exception as exc:
        console(f"TeleRig: TX release on lock failed: {exc!r}")
    try:
        audio.stop_all()
    except Exception:
        pass
    try:
        bridge.drop_all("server_locked")         # no bridge client stays connected while locked
    except Exception:
        pass
    hub.kick_all(4403, "server_locked")


def _leave_locked() -> None:
    global _locked
    try:
        reseed = accounts.status() == "missing"  # auth.json moved aside / deleted while locked
        accounts.ensure_initialized(reconcile_mode="thread")   # on the loop: CLI calls in a thread
    except StoreCorrupt as e:
        _locked = e
        return
    except Exception as e:                       # e.g. a store write failed: stay locked, retry
        console(f"TeleRig: still locked: {e!r}")
        return
    console(f"  TeleRig: {_locked.path if _locked else 'config'} is readable again; unlocked.")
    _locked = None
    if reseed:
        _announce_reseed()


def _check_locked() -> Optional[StoreCorrupt]:
    """The current locked state (re-reads the stores, subject to their stat/hash intervals);
    enters or leaves locked mode on a transition."""
    e = _store_problem()
    if e is not None:
        _enter_locked(e)
    elif _locked is not None:
        _leave_locked()
    return _locked


def _locked_response(request, e: StoreCorrupt):
    msg = _locked_message(e, local=_locked_local(request.scope))
    hdrs = {"Cache-Control": "no-store"}
    if request.url.path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": "server_locked", "message": msg},
                            status_code=503, headers=hdrs)
    page = ("<!doctype html><html><head><meta charset=utf-8><title>TeleRig locked</title>"
            "<meta name=viewport content='width=device-width,initial-scale=1'></head>"
            "<body style='font-family:system-ui,sans-serif;max-width:40em;margin:3em auto;padding:0 16px'>"
            f"<h1>TeleRig is locked</h1><p>{_html.escape(msg)}</p></body></html>")
    return HTMLResponse(page, status_code=503, headers=hdrs)


# -- account changes (the auth.json reload hook, §2.4) -----------------------------------------
def change_reason(old_user, new_user) -> str:
    """The 4401 close reason for a changed account: `role_changed` when the account still exists
    and its role differs, else `session_revoked` (password change or reset, sign-out everywhere,
    delete, --reset-admin)."""
    if isinstance(old_user, dict) and isinstance(new_user, dict) and old_user.get("role") != new_user.get("role"):
        return "role_changed"
    return "session_revoked"


def _on_accounts_change(old: dict, new: dict) -> None:
    """Runs after every auth.json content change: our own writes, a `--reset-admin` from the CLI
    or a hand edit (the watchdog reloads every 0.5 s, so a CLI change bites within ~1 s, I7).
    A changed or removed user loses its sockets (4401 session_revoked), its limiter keys and its
    sudo windows; a newly revoked token loses its sockets (4401 signed_out); a new limiter_reset
    (or a fresh store) clears every lockout.

    One exception: a logout that did not fit the revocation list bumps the signing-out user's gen
    instead (accounts.revoke). That is the user's own sign-out, not an account change: the user's
    sockets close as `signed_out` and the rate-limit keys are kept.

    A user who still exists with a different role closes as `role_changed`, so their tab lands on
    /login?reason=role_changed ("Your role changed — sign in again", §5.1, B4)."""
    ch = accounts.diff(old, new)
    old_users = (old or {}).get("users") or {}
    new_users = (new or {}).get("users") or {}
    for user in ch.users:
        if accounts.revocation_bump(user, (new_users.get(user) or {}).get("gen")):
            hub.kick_user(user, 4401, "signed_out")
        else:
            hub.kick_user(user, 4401, change_reason(old_users.get(user), new_users.get(user)))
            limiter.clear_user(user)
        auth.sudo_drop_user(user)
    for fp in ch.revoked:
        hub.kick_fp(fp, 4401, "signed_out")
        auth.sudo_drop(fp)
    if ch.limiter_reset or ch.secret:
        limiter.clear_all()
    if ch.secret:
        hub.kick_all(4401, "session_revoked")


accounts.add_listener(_on_accounts_change)
account_api.kick_fp = lambda fp, code, reason: hub.kick_fp(fp, code, reason)
account_api.kick_user = lambda user, code, reason: hub.kick_user(user, code, reason)
# -- Administration > Update (backend/updater.py) --------------------------------------------
_BOOT_ID = secrets.token_hex(8)    # new at every start: the Update tab sees the restart happen
_REPO_DIR = Path(__file__).resolve().parent.parent
_restart_requested = False         # run.py: the KeyboardInterrupt of this shutdown is a restart


def _update_repo() -> Path:
    """TeleRig's own folder. Only a --sim-only test server may point the updater at a scratch
    repository (TELERIG_TEST_UPDATE_REPO), never at anything else."""
    t = os.environ.get("TELERIG_TEST_UPDATE_REPO")
    return Path(t) if (t and sim_only()) else _REPO_DIR


def _restart_self():
    """After a successful update: start a new copy of TeleRig with this command line (run.py waits
    until this process has exited and its port is free), then shut this one down the normal way:
    TX released, the bridge stopped, audio off, the radio disconnected. A --sim-only test server
    with TELERIG_TEST_UPDATE_NORESTART skips it; so does uvicorn --reload (it restarts by itself)."""
    global _restart_requested
    if sim_only() and _env_flag("TELERIG_TEST_UPDATE_NORESTART"):
        return "skipped"
    argv = list(sys.argv)
    if "--reload" in argv:
        return "skipped"
    if "--no-browser" not in argv:
        argv.append("--no-browser")
    env = dict(os.environ, TELERIG_RESTART_WAIT=str(os.getpid()))
    env.pop("TELERIG_PORT", None)
    out = subprocess.DEVNULL
    try:
        if sys.stdout is not None:
            sys.stdout.flush()
            sys.stdout.fileno()
            out = sys.stdout                     # the new copy writes to the same window / log
    except Exception:
        out = subprocess.DEVNULL
    subprocess.Popen([sys.executable] + argv, cwd=str(_REPO_DIR), env=env, stdin=subprocess.DEVNULL,
                     stdout=out, stderr=out)
    _restart_requested = True
    loop = hub.loop
    if loop is not None:
        loop.call_soon_threadsafe(_shutdown_for_restart)
    return "started"


def _shutdown_for_restart() -> None:
    console("  TeleRig: restarting to run the update ...")
    signal.raise_signal(signal.SIGINT)           # uvicorn's own graceful shutdown (the lifespan's finally)


updater = updater_mod.Updater(_update_repo(), log=console, audit=audit.log)


def _version_label() -> str:
    """The version the pages show (updater.version_label): a dev install adds "-dev+<commit>". Read
    once, at start, from TeleRig's own folder (never a test's scratch update repository), so it names
    the commit THIS process runs; an update restarts the server. Any trouble: the number alone."""
    try:
        return updater_mod.version_label(__version__, updater_mod.Updater(_REPO_DIR).info())
    except Exception:
        return __version__


VERSION_LABEL = _version_label()
admin_api.bind(hub=hub, arb=arb, audio=audio, radio=lambda: radio, transport_kind=lambda: _transport_kind(),
               bridge=bridge, updater=updater, restart=_restart_self, boot=_BOOT_ID)


def _revalidate_conns() -> int:
    """Every 30 s: re-parse each socket's token. Expired -> 4401 expired; revoked -> 4401
    signed_out; user gone or generation changed -> 4401 session_revoked. Returns the kicks."""
    n = 0
    now = time.time()
    for c in list(hub.conns.values()):
        if c.closing or c.user is None:
            continue
        reason = None
        if not c.exp or c.exp <= now:
            reason = "expired"
        elif accounts.parse_token(c.token) is None:
            reason = "session_revoked"
        elif accounts.is_revoked(c.token_fp):
            reason = "signed_out"
        else:
            u = accounts.get(c.user)
            if u is None or u.gen != c.gen:
                reason = "session_revoked"
        if reason:
            hub.kick_conn(c, 4401, reason)
            n += 1
    return n


async def _ensure_accounts() -> None:
    """auth.json deleted while running: seed it again exactly like a first start (in a thread:
    scrypt) and show the first-run setup code on the console."""
    if accounts.status() != "missing":
        return
    await run_in_threadpool(accounts.ensure_initialized, None, reconcile_mode="skip")
    _announce_reseed()


def _announce_reseed() -> None:
    """auth.json was created again (deleted while running, or moved aside while locked): show the
    first-run setup code on the console (F1: the console shows it on purpose)."""
    try:
        info = accounts.banner_info()
        if info["default_pw"] and info["setup_code"]:
            console(f"  TeleRig: auth.json was missing and has been created again. First sign-in on "
                    f"THIS PC: admin / admin with setup code {accounts.format_setup(info['setup_code'])}.")
    except StoreCorrupt:
        pass


# -- watchdog (TX duties + store reload + locked re-check + session revalidation) -------------
WATCHDOG_TICK = 0.5
REVALIDATE_EVERY = 30.0
_watchdog_task: Optional[asyncio.Task] = None
_shutting_down = False
_last_reval = 0.0


async def _store_tick() -> None:
    """The non-TX watchdog duties, after the arbiter tick (txarb.watchdog_loop)."""
    global _last_reval
    try:
        _check_locked()              # reloads auth.json + config.json even with no traffic (I7)
        if _locked is None:
            await _ensure_accounts()
            now = time.monotonic()
            if now - _last_reval >= REVALIDATE_EVERY:
                _last_reval = now
                _revalidate_conns()
                limiter.sweep()
    except StoreCorrupt as exc:
        _enter_locked(exc)
    try:
        _bridge_kick()               # config changes, a listener to retry, clients without a radio
    except Exception as exc:
        console(f"TeleRig: bridge tick failed: {exc!r}")


async def _watchdog() -> None:
    """Every 0.5 s: the TX arbiter tick (heartbeat timeout, 120 s session clock, claim caps,
    reconciliation, EXTERNAL debounce; lag-aware, AR-16), then the store duties. Each step is
    wrapped (log and keep ticking); a dead task is restarted by _watchdog_done."""
    global _last_reval
    _last_reval = time.monotonic()
    await txarb.watchdog_loop(arb, _store_tick, interval=WATCHDOG_TICK, log=console)


def _watchdog_done(task: asyncio.Task) -> None:
    if _shutting_down or task.cancelled():
        return
    console(f"TeleRig: watchdog stopped ({task.exception()!r}); restarting")
    audit.log("watchdog_restart", error=repr(task.exception())[:200])
    _start_watchdog()


def _start_watchdog() -> None:
    global _watchdog_task
    _watchdog_task = asyncio.ensure_future(_watchdog())
    _watchdog_task.add_done_callback(_watchdog_done)


@asynccontextmanager
async def _lifespan(app):
    global _shutting_down
    # capture the serving loop so radio threads can schedule WS sends onto it.
    # (lifespan, not on_startup= — Starlette removed the on_startup/on_shutdown kwargs.)
    hub.loop = asyncio.get_running_loop()
    # I2: every WS action literal in this file has a policy row (or is REMOVED), and every route
    # declares its policy (__tr_policy__), else no start.
    policy.assert_complete(Path(__file__).read_text(encoding="utf-8"))
    policy.assert_routes(app.routes)
    try:
        accounts.ensure_initialized(reconcile_mode="thread")   # run.py already did it in-process
    except StoreCorrupt as e:
        _enter_locked(e)                   # start anyway, locked: 503 + the fix, never a traceback
    _maybe_check_update()                  # warm the update-check cache at startup
    _shutting_down = False
    _start_watchdog()
    try:
        yield
    finally:
        _shutting_down = True
        if _watchdog_task is not None:
            _watchdog_task.cancel()
        # Shutdown never leaves the radio keyed (§2.13): explicit release, audio off, disconnect.
        # The bridge first: from here it serves nothing and nobody is connected to it, so no
        # bridge command can key again between the release and the end.
        try:
            bridge.begin_stop()
        except Exception as e:
            console(f"TeleRig: bridge stop failed: {e!r}")
        try:
            arb.force_release("shutdown", explicit=True)
        except Exception as e:
            console(f"TeleRig: TX release at shutdown failed: {e!r}")
        try:
            await bridge.stop()
        except Exception as e:
            console(f"TeleRig: bridge stop failed: {e!r}")
        try:
            audio.stop_all()
        except Exception:
            pass
        try:
            await run_in_threadpool(radio.disconnect)
        except Exception:
            pass
        audit.flush(1.0)


# -- the HTTP Gate (§2.9) -----------------------------------------------------------------------
PUBLIC = frozenset({"/login", "/api/login", "/api/auth_status"})
RESTRICTED_OK = frozenset({"/change-password", "/api/me/password", "/api/logout", "/api/auth_status",
                           "/login"})
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
}


def add_security_headers(resp, path: str):
    """I14: on every response; no-store on HTML, redirects and /api/*."""
    for k, v in SECURITY_HEADERS.items():
        resp.headers[k] = v
    ctype = (resp.headers.get("content-type") or "").lower()
    if path.startswith("/api/") or ctype.startswith("text/html") or 300 <= resp.status_code < 400:
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _is_page_request(request) -> bool:
    return request.method in ("GET", "HEAD") and not request.url.path.startswith("/api/")


def _has_body(request) -> bool:
    cl = (request.headers.get("content-length") or "").strip()
    return (cl.isdigit() and int(cl) > 0) or "transfer-encoding" in request.headers


class Gate(BaseHTTPMiddleware):
    """Every HTTP request, before any route: locked mode (503) -> Host allowlist (DNS rebinding,
    F21) -> exact Origin on every non-GET/HEAD (CSWSH, I15) -> JSON content type for API bodies
    (415) -> identify() -> coarse auth (401/303 to /login, restricted 403/303 to
    /change-password) -> the route (which re-checks its own @requires policy) -> security headers.
    A WebSocket upgrade does not pass through here; ws_endpoint does the same checks itself (an
    upgrade to any other path is refused before this by WsOnlyAtWs)."""
    async def dispatch(self, request, call_next):
        path = request.url.path
        try:
            resp = await self._dispatch(request, call_next, path)
        except StoreCorrupt as e:
            _enter_locked(e)
            resp = _locked_response(request, e)
        except StoreBusy:
            resp = JSONResponse({"ok": False, "error": "store_busy"}, status_code=503)
        except StoreError:
            resp = JSONResponse({"ok": False, "error": "store_error"}, status_code=500)
        except Exception as e:                   # a bug: still a JSON 500 WITH the security headers (I14)
            import traceback
            console(f"TeleRig: {request.method} {path} failed: {e!r}\n{traceback.format_exc()}")
            resp = JSONResponse({"ok": False, "error": "internal_error"}, status_code=500)
        return add_security_headers(resp, path)

    async def _dispatch(self, request, call_next, path):
        lk = _check_locked()
        if lk is not None:
            return _locked_response(request, lk)
        host = request.headers.get("host")
        if not auth.host_ok(host):
            msg = auth.host_denied_text(host, request.scope)
            if _is_page_request(request):
                return HTMLResponse("<!doctype html><meta charset=utf-8><title>TeleRig: bad Host</title>"
                                    "<body style='font-family:system-ui,sans-serif;max-width:40em;"
                                    f"margin:3em auto;padding:0 16px'><p>{_html.escape(msg)}</p>",
                                    status_code=403)
            return PlainTextResponse(msg, status_code=403)
        if request.method not in ("GET", "HEAD"):           # block cross-site state changes
            origin = request.headers.get("origin")
            if not auth.origin_ok(origin, request.scope):
                return PlainTextResponse(auth.origin_denied_text(origin, request.scope), status_code=403)
            if path.startswith("/api/") and _has_body(request):
                ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    return JSONResponse({"ok": False, "error": "json_required"}, status_code=415)
        p = auth.identify(request.scope)
        request.state.principal = p
        if path in PUBLIC:
            return await call_next(request)
        if p is None:
            if path.startswith("/api/"):
                return JSONResponse({"ok": False, "error": "auth_required"}, status_code=401)
            target = path + (("?" + request.url.query) if request.url.query else "")
            return RedirectResponse("/login?next=" + quote(target, safe=""), status_code=303)
        if p.restricted and path not in RESTRICTED_OK:
            if path.startswith("/api/") or request.method not in ("GET", "HEAD"):
                return JSONResponse({"ok": False, "error": "password_change_required"}, status_code=403)
            return RedirectResponse("/change-password", status_code=303)
        return await call_next(request)


WS_PATH = "/ws"
_WS_REFUSED_BODY = b'{"ok": false, "error": "not_found"}'


class WsOnlyAtWs:
    """A WebSocket upgrade to any path but /ws is refused here, before any route (pure ASGI, outside
    the Gate, which sees HTTP only). Without it Starlette's Mount("/static") also matches websocket
    scopes and StaticFiles raises on them (HTTP 500 and a traceback, unauthenticated), and every
    other path got a bare 403 without the security headers. The refusal is the same for everyone
    (nothing is read from the request): a 404 JSON body with the security headers through the ASGI
    websocket.http.response extension (uvicorn offers it), else a close before accept (uvicorn
    answers 403). /ws itself is untouched: ws_endpoint makes its own checks (§2.11)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "websocket":
            await self.app(scope, receive, send)
            return
        path, root = scope.get("path") or "", scope.get("root_path") or ""
        if root and path.startswith(root):           # the route path, as the router matches it
            path = path[len(root):]
        if path == WS_PATH:
            await self.app(scope, receive, send)
            return
        if "websocket.http.response" in (scope.get("extensions") or {}):
            hdrs = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in SECURITY_HEADERS.items()]
            hdrs += [(b"cache-control", b"no-store"), (b"content-type", b"application/json"),
                     (b"content-length", str(len(_WS_REFUSED_BODY)).encode("ascii"))]
            await send({"type": "websocket.http.response.start", "status": 404, "headers": hdrs})
            await send({"type": "websocket.http.response.body", "body": _WS_REFUSED_BODY})
        else:
            await send({"type": "websocket.close", "code": 1008})


# The Tailscale Serve routes moved to /api/admin/ (backend/admin_api.py): /api/remote_status,
# /api/tailscale_serve and /api/tailscale_serve_off are gone (404), like /api/set_password.

policy.mark(ws_endpoint, "viewer")                     # identity checked inside ws_endpoint (§2.11)
_static = policy.mark(Mount("/static", StaticFiles(directory=str(FRONTEND)), name="static"), "viewer")

routes = [
    Route("/", index),
    Route("/login", account_api.login_page),
    Route("/change-password", account_api.change_password_page),
    Route("/api/login", account_api.api_login, methods=["POST"]),
    Route("/api/logout", account_api.api_logout, methods=["POST"]),
    Route("/api/auth_status", account_api.api_auth_status),
    Route("/api/me", account_api.api_me),
    Route("/api/me/password", account_api.api_me_password, methods=["POST"]),
    Route("/api/me/signout_all", account_api.api_me_signout_all, methods=["POST"]),
    Route("/api/me/layout", account_api.api_me_layout_get, methods=["GET"]),
    Route("/api/me/layout", account_api.api_me_layout_put, methods=["PUT"]),
    Route("/api/me/layout", account_api.api_me_layout_delete, methods=["DELETE"]),
    *admin_api.routes(),
    Route("/api/status", api_status),
    Route("/api/radios", api_radios),
    Route("/api/version", api_version),
    Route("/api/ports", api_ports),
    Route("/api/connect", api_connect, methods=["POST"]),
    Route("/api/disconnect", api_disconnect, methods=["POST"]),
    Route("/api/tx_stop", api_tx_stop, methods=["POST"]),
    WebSocketRoute(WS_PATH, ws_endpoint),
    _static,
]

# WsOnlyAtWs first: a WebSocket upgrade off /ws never reaches a route (StaticFiles is HTTP-only).
app = Starlette(debug=False, routes=routes, middleware=[Middleware(WsOnlyAtWs), Middleware(Gate)],
                lifespan=_lifespan)
