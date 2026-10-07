"""
TeleRig server.

ASGI app (Starlette on uvicorn). Everyone signs in (per-user accounts, backend/accounts.py);
every route declares its policy (@requires, backend/policy.py) and the Gate middleware checks
locked mode, Host, Origin, JSON bodies and the session before any route runs (§2.9).
  GET  /              -> the radio UI (+ the bootstrap JSON, §3.15)
  /login, /change-password, /api/login|logout|auth_status|me* -> backend/account_api.py
  GET  /api/status    -> radio/connection status for this role
  GET  /api/ports     -> available COM ports (+ "sim")
  POST /api/connect   -> {transport:"sim"} or {transport:"serial",port,baud}
  POST /api/disconnect
  WS   /ws            -> hello + JSON state in/out + binary scope sweeps out

Scope sweeps are pushed as a single self-contained binary frame:
  <BBBH IIIIII> header then N amplitude bytes (see _pack_scope).
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import mimetypes
import os
import struct
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

from . import (__version__, account_api, accounts, admin_api, audit, auth, civ, hostaudio, policy,
               profiles, settings, tailscale)
from .account_api import read_json
from .hub import AUDIO_RATE, Hub  # noqa: F401  (AUDIO_RATE re-exported: hostaudio.RATE must match it)
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
def _asset_version():
    """Token from frontend file mtimes so the browser refetches updated assets."""
    try:
        return str(int(max(
            (FRONTEND / f).stat().st_mtime
            for f in ("index.html", "app.js", "waterfall.js", "bandplan.js", "cwtool.js", "style.css")
        )))
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
    """GET /api/status for this principal (G-6). Step A3 subset; A4 adds the TX arbiter snapshot,
    the connect transition and the host-audio defaults. Operators and admins also get the full
    transport label (incl. a LAN IP) and the current radio's transport pin (no credentials)."""
    s = radio.state
    rid = s.get("radio") if s.get("connected") else None
    try:
        lr = settings.last_radio()
        pins = settings.host_audio().get(rid or lr or "", {})
    except StoreCorrupt:
        lr, pins = None, {}
    out = {"connected": bool(s.get("connected")), "radio": rid, "last_radio": lr,
           "transport_kind": _transport_kind(), "connecting": False,
           "host_audio": {"available": hostaudio.available(), "rx_pinned": pins.get("rx"),
                          "tx_pinned": pins.get("tx"), "rx_default": None, "tx_default": None},
           "tx": {"owner": None},
           "autoconnect_allowed": not no_autoconnect() and not _autoconnect_spent,
           "sim_only": sim_only()}
    if p is not None and auth.rank(p.role) >= auth.ROLE_RANK["operator"]:
        out["transport"] = s.get("transport")
        try:
            out["pinned_transport"] = settings.radio_transports().get(rid or lr or "")
        except StoreCorrupt:
            out["pinned_transport"] = None
    return out


def notices_for(p) -> list:
    """Banner keys for this principal (§3.15). A3: migrated_local_only (F17 remote session),
    migrated_v1_password and lockouts (admins)."""
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
    return out


def _boot_json(p, request) -> str:
    boot = {"v": 1, "user": p.user, "role": p.role, "perms": auth.perms_for(p.role),
            "port": auth.bound_port(request.scope), "version": __version__,
            "source": f"https://github.com/{_REPO}",
            "autoconnect": not no_autoconnect() and not _autoconnect_spent, "sim_only": sim_only(),
            "layout": None, "layout_error": False, "layout_bounds": None,     # A5: layout_store
            "status": status_for(p), "migrated_remote": p.migrated_remote,
            "radios": [{"id": pid, "name": pr.name,
                        "tile_caps": {"ptt": bool(pr.capabilities.ptt),
                                      "meters": not (pr.capabilities.meters == [])}}
                       for pid, pr in profiles.PROFILES.items()],
            "notices": notices_for(p)}
    payload = json.dumps(boot, separators=(",", ":"))
    return payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


@requires("viewer")
async def index(request):
    p = request.state.principal
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    v = _asset_version()
    for asset in ("app.js", "waterfall.js", "bandplan.js", "cwtool.js", "style.css"):
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={v}")
    tag = f'<script id="tr-boot" type="application/json">{_boot_json(p, request)}</script>'
    if "<!--TR_BOOT-->" in html:
        html = html.replace("<!--TR_BOOT-->", tag, 1)
    else:                                            # index.html gets the placeholder in B1/B3
        html = html.replace("</head>", tag + "\n</head>", 1)
    role = p.role if p.role in policy.ROLES else "viewer"
    html = html.replace("<body>", f'<body data-role="{role}">', 1)
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
        "latest": latest,
        "update_available": available,
        "url": url,
    })


@requires("operator")
async def api_ports(request):
    return JSONResponse({
        "ports": available_ports(),
        "connected": radio.state["connected"],
        "transport": radio.state["transport"],
        "radio": radio.state["radio"],
    })


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


def _connect_error(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error, "message": message}, status_code=status)


@requires("operator")
async def api_connect(request):
    global radio
    body = await read_json(request)
    if not isinstance(body, dict):
        return body                                  # 400 invalid_json / 413 too_large
    kind = body.get("transport", "sim")
    # Guards run before ANY side effect (audio stop, radio switch, port open).
    # --sim-only: only the simulator may be connected; serial and LAN are refused (I13).
    if sim_only() and kind != "sim":
        return _connect_error(403, "sim_only",
                              "This server runs with --sim-only: only the simulator can be connected.")
    # auto (page-load) connects: refused with --no-autoconnect or after an explicit disconnect.
    if body.get("auto") and (no_autoconnect() or _autoconnect_spent):
        return _connect_error(409, "autoconnect_disabled",
                              "Automatic connect is disabled; press Connect to connect a radio.")
    try:
        host_audio.stop()                   # drop any prior host sound-card streams on (re)connect
        profile = profiles.PROFILES.get(body.get("radio"), radio.profile)
        is_yaesu = getattr(profile, "protocol", "civ") == "yaesu"
        # route to the matching protocol stack (Icom CI-V vs Yaesu CAT)
        target = _yaesu if is_yaesu else _icom
        if target is not radio:
            radio.disconnect()
            radio = target
            _bind_radio(radio)
        if kind == "serial":
            baud = int(body.get("baud") or getattr(profile, "default_baud", 115200))
            # CAT port opens with control lines at their defaults (RTS high) so CAT works
            # regardless of the radio's CAT-RTS setting. FT-991A CW keys on the SIBLING
            # (Standard) port's DTR line, opened separately — never on this CAT port.
            tp = SerialTransport(body["port"], baud, stopbits=2 if is_yaesu else 1)
        elif kind == "lan":
            if is_yaesu:
                raise ValueError("The FT-991A is COM-only (no network CAT)")
            host = (body.get("host") or "").strip()
            if not host:
                raise ValueError("LAN host/IP is required")
            tp = LanTransport(host, int(body.get("port", 50001)),
                              body.get("user", ""), body.get("password", ""))
        else:
            tp = YaesuSimTransport(profile) if is_yaesu else SimTransport(profile)
        radio.connect(tp, profile)
        return JSONResponse({"ok": True, "transport": radio.state["transport"], "radio": profile.id})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@requires("operator")
async def api_disconnect(request):
    global _autoconnect_spent
    _autoconnect_spent = True               # explicit disconnect: page loads no longer auto-connect
    host_audio.stop()                       # release the host sound cards with the radio
    radio.disconnect()
    return JSONResponse({"ok": True})


@requires("operator")
async def api_audio_devices(request):
    """Host (server-side) sound cards, for a serial/USB radio's Radio RX / Radio TX pickers."""
    return JSONResponse(hostaudio.list_devices())


# -- WebSocket ---------------------------------------------------------------
WS_TEXT_MAX = 8192                     # bytes; larger text frames are refused (`denied too_large`)
DENY_INTERVAL = 0.5                    # at most 2 denial frames per second per connection


HB_INTERVAL, HB_TIMEOUT = 2, 6            # the TX heartbeat contract (§2.13), announced in hello


def _hello(conn, p) -> dict:
    return {"type": "hello", "v": 1, "conn": conn.id, "user": p.user, "role": p.role,
            "perms": auth.perms_for(p.role), "migrated_remote": p.migrated_remote,
            "status": status_for(p), "tx": {"owner": None}, "hb_interval": HB_INTERVAL,
            "hb_timeout": HB_TIMEOUT, "server_version": __version__, "sim_only": sim_only()}


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
        # Each step on its own, so one failure never skips an unkey (AR-17).
        # safety: PTT is a single shared radio state with one keyer, so the
        # disconnecting client may be the one transmitting. Unkey on ANY client
        # drop while keyed — not just the last one — since the client-side unkey
        # can be lost when the socket is already closing. (A4: owner-scoped, §2.13.)
        try:
            if radio.state.get("ptt"):
                radio.set_ptt(False)
        except Exception:
            pass
        try:
            if radio.state.get("cw_tx"):              # stop an in-progress CW message too
                radio.stop_cw()
        except Exception:
            pass
        try:
            host_audio.stop_tx()             # this client's mic feed ends -> stop playing to the radio
        except Exception:
            pass
        try:
            hub.remove(conn)
            if not hub.conns:                # nobody left listening -> stop capturing the host RX card
                host_audio.stop_rx()
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


def _deny(conn, action, reason: str) -> None:
    """`denied` to this connection only, on the control queue; at most 2/s per connection."""
    now = time.monotonic()
    if now - conn.denied_at < DENY_INTERVAL:
        return
    conn.denied_at = now
    a = action if isinstance(action, str) else "*"
    hub.send_to(conn, {"type": "denied", "action": a[:64], "reason": reason})


def _on_text(conn, text: str) -> None:
    """One client text frame: size cap, JSON parsed inside try, dict + string-action check, then
    the policy table (§2.12). Never raises, so a bad frame can never kill the socket.

    Fast path first (policy.is_fast: the unkey/heartbeat frames): decided with the connection's
    LAST-KNOWN role and no store read, so a store error, a lock or a kick never swallows an unkey
    (I4, AR-17). Everything else re-reads the user: a deleted user or a changed generation closes
    the socket 4401 session_revoked, and the effective role is refreshed (F17).

    A kicked connection (`conn.closing`: its close frame is already out) and a locked server act on
    nothing but the fast path. A client that ignores the close frame can otherwise keep commanding
    the radio until the close timeout drops it (about 10 s). The receive loop keeps running, so the
    4403/4401 close code is not replaced."""
    try:
        if len(text) > WS_TEXT_MAX or len(text.encode("utf-8", "surrogatepass")) > WS_TEXT_MAX:
            _deny(conn, "*", "too_large")            # a no-op on a closing connection
            return
        try:
            cmd = json.loads(text)
        except Exception:
            return
        if not isinstance(cmd, dict) or not isinstance(cmd.get("action"), str):
            return
        if policy.is_fast(cmd):
            _dispatch(conn, policy.evaluate_fast(conn, cmd))
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
        _dispatch(conn, policy.evaluate(rule, conn, cmd))
    except Exception as e:                           # pragma: no cover - defensive
        console(f"TeleRig: WS message handling failed: {e!r}")


def _dispatch(conn, v) -> None:
    if v.kind == "deny":
        _deny(conn, v.action, v.reason or "invalid")
    elif v.kind == "ignore":
        return
    elif v.kind == "plain":
        _handle_cmd(v.cmd)
    else:                                            # busy/new/claim/release/stop arrive in A4
        _deny(conn, v.action, "invalid")


def _on_bytes(conn, data: bytes) -> None:
    """Binary frame = browser mic PCM (v1 path; A4 gates it to the TX owner, §2.14). A kicked
    (closing) connection or a locked server feeds nothing to the transmitter."""
    try:
        if conn.closing or _locked is not None:
            return
        if host_audio.tx_device is not None:
            host_audio.write(data)                   # serial radio: mic -> host TX sound card
        else:
            radio.write_audio(data)                  # LAN: mic PCM -> the radio's modulator
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


def _locked_message(e: StoreCorrupt) -> str:
    return (f"TeleRig is locked: {e.path} cannot be read ({e.detail or 'corrupt'}). Nothing has been "
            f"changed. On the host PC, fix that file or move it aside (or run {_recovery_cmd()}); "
            "TeleRig unlocks within a few seconds, no restart needed.")


def _store_problem() -> Optional[StoreCorrupt]:
    """Re-reads both stores (subject to their stat/hash intervals); an accounts reload fires the
    reload hook (_on_accounts_change), which kicks revoked sessions."""
    try:
        accounts.view()
        settings.view()
    except StoreCorrupt as e:
        return e
    return None


def _release_tx_for_lock() -> None:
    """Entering locked mode releases app-keyed TX (A4: arb.force_release("server_locked"))."""
    try:
        if radio.state.get("ptt"):
            radio.set_ptt(False)
    except Exception:
        pass
    try:
        if radio.state.get("cw_tx"):
            radio.stop_cw()
    except Exception:
        pass


def _enter_locked(e: StoreCorrupt) -> None:
    global _locked
    entering = _locked is None
    _locked = e
    if not entering:
        return
    console("\n  " + _locked_message(e) + "\n")
    _release_tx_for_lock()
    try:
        host_audio.stop()
    except Exception:
        pass
    hub.kick_all(4403, "server_locked")


def _leave_locked() -> None:
    global _locked
    try:
        accounts.ensure_initialized(reconcile_mode="thread")   # on the loop: CLI calls in a thread
    except StoreCorrupt as e:
        _locked = e
        return
    except Exception as e:                       # e.g. a store write failed: stay locked, retry
        console(f"TeleRig: still locked: {e!r}")
        return
    console(f"  TeleRig: {_locked.path if _locked else 'config'} is readable again; unlocked.")
    _locked = None


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
    msg = _locked_message(e)
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
def _on_accounts_change(old: dict, new: dict) -> None:
    """Runs after every auth.json content change: our own writes, a `--reset-admin` from the CLI
    or a hand edit (the watchdog reloads every 0.5 s, so a CLI change bites within ~1 s, I7).
    A changed or removed user loses its sockets (4401 session_revoked), its limiter keys and its
    sudo windows; a newly revoked token loses its sockets (4401 signed_out); a new limiter_reset
    (or a fresh store) clears every lockout.

    One exception: a logout that did not fit the revocation list bumps the signing-out user's gen
    instead (accounts.revoke). That is the user's own sign-out, not an account change: the user's
    sockets close as `signed_out` and the rate-limit keys are kept."""
    ch = accounts.diff(old, new)
    new_users = (new or {}).get("users") or {}
    for user in ch.users:
        if accounts.revocation_bump(user, (new_users.get(user) or {}).get("gen")):
            hub.kick_user(user, 4401, "signed_out")
        else:
            hub.kick_user(user, 4401, "session_revoked")
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
admin_api.online_count = hub.count_user


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
    try:
        info = accounts.banner_info()
        if info["default_pw"] and info["setup_code"]:
            console(f"  TeleRig: auth.json was missing and has been created again. First sign-in on "
                    f"THIS PC: admin / admin with setup code {accounts.format_setup(info['setup_code'])}.")
    except StoreCorrupt:
        pass


# -- watchdog (store reload + locked re-check + session revalidation; A4 adds the TX duties) ----
WATCHDOG_TICK = 0.5
REVALIDATE_EVERY = 30.0
_watchdog_task: Optional[asyncio.Task] = None
_shutting_down = False
_last_reval = 0.0


async def _watchdog() -> None:
    global _last_reval
    _last_reval = time.monotonic()
    while True:
        await asyncio.sleep(WATCHDOG_TICK)
        try:
            _check_locked()          # reloads auth.json + config.json even with no traffic (I7)
            if _locked is None:
                await _ensure_accounts()
                now = time.monotonic()
                if now - _last_reval >= REVALIDATE_EVERY:
                    _last_reval = now
                    _revalidate_conns()
                    limiter.sweep()
        except StoreCorrupt as exc:
            _enter_locked(exc)
        except Exception as exc:     # log and keep ticking
            console(f"TeleRig: watchdog tick failed: {exc!r}")


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
    A WebSocket upgrade does not pass through here; ws_endpoint does the same checks itself."""
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


# -- Tailscale Serve (interim location; A5 moves these under /api/admin/ with success-only
#    behind_proxy writes and the bound port) -------------------------------------------------
def _app_port(request) -> int:
    return request.url.port or 8700


@requires("admin")
async def api_remote_status(request):
    port = _app_port(request)
    st = await run_in_threadpool(tailscale.status, port)
    return JSONResponse({"tailscale": st, "auth_enabled": True, "behind_proxy": settings.behind_proxy(),
                         "local": auth.direct_local(request.scope), "port": port})


@requires("admin", sudo=True, local=True)              # F9: admin + fresh sudo + host-local
async def api_tailscale_serve(request):
    port = _app_port(request)
    code, out, err = await run_in_threadpool(tailscale.serve_on, port)
    st = await run_in_threadpool(tailscale.status, port)
    if st.get("magicdns"):
        settings.add_allowed_host(st["magicdns"])   # so the Origin/Host gate accepts the *.ts.net name
    settings.set_behind_proxy(True, "tailscale")     # (A5: written only after a confirmed success)
    if code != 0:
        return JSONResponse({"ok": False, "error": (err or out or "tailscale serve failed").strip(),
                             "tailscale": st}, status_code=400)
    return JSONResponse({"ok": True, "tailscale": st})


@requires("admin")                                     # F9: closing exposure needs no sudo
async def api_tailscale_serve_off(request):
    await run_in_threadpool(tailscale.serve_off)
    settings.set_behind_proxy(False)
    return JSONResponse({"ok": True, "tailscale": await run_in_threadpool(tailscale.status, _app_port(request))})


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
    Route("/api/admin/users", admin_api.api_users),
    Route("/api/status", api_status),
    Route("/api/remote_status", api_remote_status),
    Route("/api/tailscale_serve", api_tailscale_serve, methods=["POST"]),
    Route("/api/tailscale_serve_off", api_tailscale_serve_off, methods=["POST"]),
    Route("/api/radios", api_radios),
    Route("/api/version", api_version),
    Route("/api/ports", api_ports),
    Route("/api/audio_devices", api_audio_devices),
    Route("/api/connect", api_connect, methods=["POST"]),
    Route("/api/disconnect", api_disconnect, methods=["POST"]),
    WebSocketRoute("/ws", ws_endpoint),
    _static,
]

app = Starlette(debug=False, routes=routes, middleware=[Middleware(Gate)], lifespan=_lifespan)
