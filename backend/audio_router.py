"""Audio router (spec §2.14, G-5, F12, F22): who hears the radio and whose mic reaches it.

RX. `rx_audio {on}` subscribes one connection. The hub sends RX audio frames only to subscribed
connections (rx_gating). For a serial radio the host capture device (the admin's pin, or the
server-chosen default, F22) is opened when the subscriber count goes 0 -> 1 and closed at 1 -> 0;
LAN audio already flows from the radio. Device errors go to the requesting connection only.

Mic (non-exclusive, F12). `mic {on}` sets the connection's tx_audio flag; for a serial radio the
host TX device opens on the first such connection and closes when the last one leaves. Once the
TX path is open the requester gets `{type: mic, on: true, ready: true}` (a device error instead
gets its error frame and no ready), so generated TX audio (SSTV) can wait before keying. A binary
frame reaches the radio ONLY from the TX owner holding ptt or vox with its mic on (I10); anything
else is dropped silently, and never closes the socket.

flush_tx() drops buffered TX audio (the host playback buffer and the LAN TX buffer) whenever the
last ptt/vox claim ends, so a tail cannot re-key a radio with VOX on (AR-18).

Device enumeration and stream open/close are blocking (PortAudio): they run in the threadpool,
serialised by an asyncio.Lock. Pins are device NAMES (config.json host_audio[<radio>]) resolved
against hostaudio.list_devices() at use time. The choice is always made by the server.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, Optional

from . import hostaudio

FRAME_MAX = 32 * 1024                 # bytes per binary frame
CODEC_RE = re.compile("codec", re.I)


def _noop(*a, **k) -> None:
    return None


async def _threadpool(fn, *a):
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(fn, *a)


def codec_default(devices: Optional[dict], direction: str) -> Optional[str]:
    """F22: the only /codec/i input (rx) or output (tx), else None (0 or >= 2 candidates)."""
    if not isinstance(devices, dict):
        return None
    lst = devices.get("inputs" if direction == "rx" else "outputs") or []
    names = [d.get("name") for d in lst if isinstance(d, dict) and CODEC_RE.search(str(d.get("name") or ""))]
    return names[0] if len(names) == 1 else None


def find_device(devices: Optional[dict], direction: str, name: Optional[str]) -> Optional[int]:
    if not name or not isinstance(devices, dict):
        return None
    for d in devices.get("inputs" if direction == "rx" else "outputs") or []:
        if isinstance(d, dict) and d.get("name") == name:
            return d.get("id")
    return None


class AudioRouter:
    def __init__(self, hub, host_audio, radio_getter: Callable[[], Any], kind_getter: Callable[[], str], *,
                 pins_getter: Optional[Callable[[], dict]] = None,
                 list_devices: Optional[Callable[[], dict]] = None,
                 available: Optional[Callable[[], bool]] = None,
                 run_blocking: Optional[Callable] = None,
                 log: Callable[[str], None] = _noop):
        self.hub = hub
        self.host = host_audio
        self.radio_getter = radio_getter
        self.kind_getter = kind_getter
        self.pins_getter = pins_getter or _settings_pins
        self.list_devices = list_devices or hostaudio.list_devices
        self.available = available or hostaudio.available
        self.run_blocking = run_blocking or _threadpool
        self.log = log
        self.arb = None                       # the TxArbiter (set by the server)
        self.devices: Optional[dict] = None   # last enumeration (threadpool)
        self.rx_running: Optional[tuple] = None   # (radio id, device name)
        self.tx_open: Optional[tuple] = None
        self._lock: Optional[asyncio.Lock] = None

    # -- helpers ---------------------------------------------------------------------------------
    def _alock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def radio(self):
        return self.radio_getter()

    def radio_id(self) -> Optional[str]:
        st = getattr(self.radio(), "state", None) or {}
        return st.get("radio") if st.get("connected") else None

    def _conns(self):
        return [c for c in list(getattr(self.hub, "conns", {}).values()) if not getattr(c, "closing", False)]

    def rx_subs(self) -> list:
        return [c for c in self._conns() if c.rx_sub]

    def tx_users(self) -> list:
        return [c for c in self._conns() if c.tx_audio]

    def _pins(self, rid: Optional[str]) -> dict:
        try:
            return dict((self.pins_getter() or {}).get(rid or "", {}) or {})
        except Exception:                     # StoreCorrupt etc.: no pins
            return {}

    def _error(self, conn, code: str, message: str) -> None:
        try:
            self.hub.send_to(conn, {"type": "error", "code": code, "message": message})
        except Exception:
            pass

    def resolve(self, direction: str, devices: Optional[dict] = None, rid: Optional[str] = None) -> dict:
        """{name, id, source: pin|default|None, error: None|unpinned|missing}: the admin's pin, else
        the F22 server-chosen default for a serial radio, else nothing."""
        rid = rid or self.radio_id()
        devices = self.devices if devices is None else devices
        pin = self._pins(rid).get(direction)
        if pin:
            idx = find_device(devices, direction, pin)
            return {"name": pin, "id": idx, "source": "pin", "error": None if idx is not None else "missing"}
        name = codec_default(devices, direction)
        if name:
            return {"name": name, "id": find_device(devices, direction, name), "source": "default", "error": None}
        return {"name": None, "id": None, "source": None, "error": "unpinned"}

    def status(self) -> dict:
        """status.host_audio for /api/status and hello."""
        rid = self.radio_id()
        if rid is None:
            try:
                from . import settings
                rid = settings.last_radio()
            except Exception:
                rid = None
        pins = self._pins(rid)
        out = {"available": bool(self.available()), "rx_pinned": pins.get("rx"), "tx_pinned": pins.get("tx"),
               "rx_default": None, "tx_default": None}
        if self.kind_getter() == "serial" and self.devices is not None:
            if not pins.get("rx"):
                out["rx_default"] = codec_default(self.devices, "rx")
            if not pins.get("tx"):
                out["tx_default"] = codec_default(self.devices, "tx")
        return out

    def default_in_use(self) -> bool:
        st = self.status()
        return bool(st["rx_default"] or st["tx_default"])

    async def refresh_devices(self) -> Optional[dict]:
        try:
            self.devices = await self.run_blocking(self.list_devices)
        except Exception as e:
            self.log(f"TeleRig: audio device enumeration failed: {e!r}")
        return self.devices

    # -- RX ----------------------------------------------------------------------------------------
    async def set_rx(self, conn, on: bool) -> None:
        if not on:
            conn.rx_sub = False
            await self._maybe_stop_rx()
            return
        conn.rx_sub = True
        if self.kind_getter() != "serial":
            return                                    # LAN audio already flows; the sim has none
        await self._ensure_rx([conn])

    async def _ensure_rx(self, requesters: list) -> None:
        async with self._alock():
            if self.rx_running is not None:
                return
            if not self.rx_subs():
                return
            devs = await self.refresh_devices()
            r = self.resolve("rx", devs)
            if r["error"]:
                code = "rx_device_unpinned" if r["error"] == "unpinned" else "rx_device_missing"
                msg = ("Host audio is not set up for this radio - ask an admin (Admin > Radio & audio)."
                       if code == "rx_device_unpinned" else f"The pinned RX device {r['name']!r} is not present.")
                for c in requesters:
                    c.rx_sub = False
                    self._error(c, code, msg)
                return
            try:
                await self.run_blocking(self.host.start_rx, r["id"])
                self.rx_running = (self.radio_id(), r["name"])
            except Exception as e:
                for c in requesters:
                    c.rx_sub = False
                    self._error(c, "rx_failed", f"Could not open {r['name']!r}: {e}")

    async def _maybe_stop_rx(self) -> None:
        if self.rx_subs() or self.rx_running is None:
            return
        async with self._alock():
            if self.rx_subs() or self.rx_running is None:
                return
            self.rx_running = None
            try:
                await self.run_blocking(self.host.stop_rx)
            except Exception:
                pass

    # -- mic -----------------------------------------------------------------------------------------
    async def set_mic(self, conn, on: bool) -> None:
        if not on:
            conn.tx_audio = False
            await self._maybe_close_tx()
            return
        conn.tx_audio = True
        if self.kind_getter() == "serial":
            await self._ensure_tx([conn])
        if conn.tx_audio and not getattr(conn, "closing", False):
            # the TX path is open (a serial radio's host TX device, or nothing to open): tell this
            # connection, so generated audio (SSTV) can wait for it before keying. Changes nothing.
            try:
                self.hub.send_to(conn, {"type": "mic", "on": True, "ready": True})
            except Exception:
                pass

    async def _ensure_tx(self, requesters: list) -> None:
        async with self._alock():
            if self.tx_open is not None:
                return
            if not self.tx_users():
                return
            devs = await self.refresh_devices()
            r = self.resolve("tx", devs)
            if r["error"]:
                code = "tx_device_unpinned" if r["error"] == "unpinned" else "tx_device_missing"
                msg = ("Host TX audio is not set up for this radio - ask an admin (Admin > Radio & audio)."
                       if code == "tx_device_unpinned" else f"The pinned TX device {r['name']!r} is not present.")
                for c in requesters:
                    c.tx_audio = False
                    self._error(c, code, msg)
                return
            try:
                await self.run_blocking(self.host.start_tx, r["id"])
                self.tx_open = (self.radio_id(), r["name"])
            except Exception as e:
                for c in requesters:
                    c.tx_audio = False
                    self._error(c, "tx_failed", f"Could not open {r['name']!r}: {e}")

    async def _maybe_close_tx(self) -> None:
        if self.tx_users() or self.tx_open is None:
            return
        async with self._alock():
            if self.tx_users() or self.tx_open is None:
                return
            self.tx_open = None
            try:
                await self.run_blocking(self.host.stop_tx)
            except Exception:
                pass

    def mic_in(self, conn, data: bytes) -> bool:
        """One binary frame (browser mic PCM). Runs on the loop, so the owner/claim check is atomic
        with arbiter transitions. Returns True when the frame was passed to the radio."""
        try:
            if getattr(conn, "closing", False) or not getattr(conn, "tx_audio", False):
                return False
            if getattr(conn, "role", None) not in ("operator", "admin"):
                return False
            arb = self.arb
            if arb is None or arb.owner is None or arb.owner != conn.id or not (arb.claims & {"ptt", "vox"}):
                return False
            if not data or len(data) > FRAME_MAX:
                return False
            if not conn.audio_rate.take(len(data)):
                return False
            if self.kind_getter() == "serial":
                if self.host.tx_device is None:
                    return False
                self.host.write(data)
            else:
                self.radio().write_audio(data)
            return True
        except Exception:
            return False

    def flush_tx(self) -> None:
        try:
            self.host.flush_tx()
        except Exception:
            pass
        try:
            tp = getattr(self.radio(), "_tp", None)
            fn = getattr(tp, "flush_tx_audio", None)
            if callable(fn):
                fn()
        except Exception:
            pass

    # -- lifecycle -----------------------------------------------------------------------------------
    async def on_conn_closed(self, conn) -> None:
        conn.rx_sub = False
        conn.tx_audio = False
        await self._maybe_stop_rx()
        await self._maybe_close_tx()

    def stop_all(self) -> None:
        """Close the host streams (radio disconnect/change, locked mode, shutdown). Connection flags
        are kept: after a radio change capture restarts for the subscribers (after_connect)."""
        self.rx_running = None
        self.tx_open = None
        try:
            self.host.stop()
        except Exception:
            pass

    async def on_radio_change(self) -> None:
        self.rx_running = None
        self.tx_open = None
        try:
            await self.run_blocking(self.host.stop)      # PortAudio close: off the loop
        except Exception:
            pass

    async def after_connect(self) -> None:
        """After a (re)connect: restart host capture/playback for the existing subscribers using the
        NEW radio's pins / default (serial only)."""
        if self.kind_getter() != "serial":
            return
        await self.refresh_devices()
        subs = self.rx_subs()
        if subs:
            await self._ensure_rx(subs)
        users = self.tx_users()
        if users:
            await self._ensure_tx(users)


def _settings_pins() -> dict:
    from . import settings
    return settings.host_audio()
