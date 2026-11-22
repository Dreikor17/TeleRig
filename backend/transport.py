"""
Transports carry raw CI-V bytes to/from "the radio".

Two implementations share one interface:
  * SerialTransport - a real IC-9700 on a Windows COM port (USB CI-V).
  * SimTransport    - a built-in simulator that speaks CI-V back, including
                      USB-style 11-frame 27 00 scope sweeps, so the whole UI
                      and waterfall can be built and verified with no radio.

Reads happen on a background thread that calls on_bytes(data).
"""
from __future__ import annotations

import math
import threading
import time
from typing import Callable, Optional

import serial
from serial.tools import list_ports

from . import civ


def available_ports() -> list[dict]:
    out = []
    for p in list_ports.comports():
        out.append({"device": p.device, "description": p.description or "",
                    "hwid": p.hwid or ""})
    return out


def _hwid_serial(hwid: str) -> Optional[str]:
    """Pull the USB serial-number token (SER=...) out of a pyserial hwid string."""
    for tok in (hwid or "").split():
        if tok.upper().startswith("SER="):
            return tok[4:]
    return None


def find_sibling_port(device: str) -> Optional[str]:
    """Find the OTHER COM port of the same physical USB device — e.g. the FT-991A's
    Standard port that is the sibling of its Enhanced/CAT port. The FT-991A's CP2105
    enumerates two ports sharing one USB serial number; CW is keyed on the Standard
    one's control line. Returns the sibling device name, or None if not found."""
    ports = list(list_ports.comports())
    target = None
    for p in ports:
        if p.device == device:
            target = _hwid_serial(p.hwid)
            break
    if not target:
        return None
    for p in ports:
        if p.device != device and _hwid_serial(p.hwid) == target:
            return p.device
    return None


class Transport:
    # Audio support (overridden by LanTransport). on_audio is called with raw
    # RX PCM (16-bit LE mono); write_audio takes TX (mic) PCM in the same format.
    supports_audio: bool = False
    on_audio: Optional[Callable[[bytes], None]] = None

    def start(self, on_bytes: Callable[[bytes], None]) -> None: ...
    def write(self, data: bytes) -> None: ...
    def write_audio(self, pcm: bytes) -> None: ...
    def stop(self) -> None: ...
    @property
    def name(self) -> str: return "transport"


class SerialTransport(Transport):
    def __init__(self, port: str, baud: int = 115200, stopbits: int = 1) -> None:
        self.port = port
        self.baud = baud
        self.stopbits = stopbits          # Icom CI-V = 1; Yaesu CAT (FT-991A) = 2
        self._ser: Optional[serial.Serial] = None
        self._on_bytes: Optional[Callable[[bytes], None]] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def name(self) -> str:
        return f"{self.port}@{self.baud}"

    def start(self, on_bytes: Callable[[bytes], None]) -> None:
        self._on_bytes = on_bytes
        sb = serial.STOPBITS_TWO if self.stopbits == 2 else serial.STOPBITS_ONE
        self._ser = serial.Serial(self.port, self.baud, timeout=0.05, stopbits=sb)
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, name="civ-serial",
                                        daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        assert self._ser is not None
        while not self._stop.is_set():
            try:
                data = self._ser.read(4096)
                if data and self._on_bytes:
                    self._on_bytes(data)
            except Exception:
                time.sleep(0.1)

    def write(self, data: bytes) -> None:
        if self._ser and self._ser.is_open:
            try:
                self._ser.write(data)
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass


class CwKeyPort:
    """A SECOND serial port opened solely to key CW via its DTR line (the FT-991A's
    Standard USB port, sibling of the Enhanced/CAT port). No CAT data flows here — only
    the DTR control line is toggled. Always opens key-UP (DTR de-asserted) and forces it
    up again on close, so it can never leave the rig keyed."""

    def __init__(self, device: str) -> None:
        self.device = device
        self._ser: Optional[serial.Serial] = None
        # Serializes key()/close() so a key-down can never land between close()'s
        # CLRDTR and the handle close (which would strand DTR HIGH = stuck carrier).
        self._lock = threading.Lock()

    def open(self) -> None:
        ser = serial.Serial()
        ser.port = self.device
        ser.baudrate = 9600           # irrelevant: we only drive DTR, no data
        ser.timeout = 0.1
        ser.rts = False               # set before open so the lines come up LOW (key up)
        ser.dtr = False
        ser.open()
        with self._lock:
            self._ser = ser

    def key(self, down: bool) -> None:
        with self._lock:              # mutually exclusive with close()
            s = self._ser
            if not s or not s.is_open:
                return
            try:
                s.dtr = bool(down)    # DTR asserted = key down = CW element
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            s = self._ser
            self._ser = None
            if s:
                try:
                    s.dtr = False     # never leave keyed
                    s.rts = False
                except Exception:
                    pass
                try:
                    s.close()
                except Exception:
                    pass

    @property
    def is_open(self) -> bool:
        s = self._ser
        return bool(s and s.is_open)


class SimTransport(Transport):
    """A synthetic IC-9700. Responds to CI-V commands and streams a scope."""

    def __init__(self, profile=None, fps: float = 20.0) -> None:
        self._on_bytes: Optional[Callable[[bytes], None]] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._reader = civ.FrameReader()
        self.fps = fps
        self._t0 = time.time()
        self._civ_addr = profile.civ_addr if profile is not None else civ.DEFAULT_RADIO_ADDR

        # simulated radio state — dual-band (MAIN/SUB) for dual-watch radios
        main_freq = profile.default_freq if profile is not None else 144_200_000
        self.dual = bool(profile is not None and getattr(profile, "dual_watch", False))
        sub_freq = profile.bands[1].default if (self.dual and len(profile.bands) > 1) else main_freq
        self.active = "main"
        self.b = {
            "main": {"freq": main_freq, "mode": 0x01, "filt": 0x01, "data": 0},   # USB
            "sub":  {"freq": sub_freq, "mode": 0x05, "filt": 0x01, "data": 0},    # FM
        }
        self.att = 0x00
        self.span = 500_000        # full span Hz (±250k, widest)
        self.scope_center = True   # center vs fixed
        self.scope_on = False
        self.scope_out = False
        self.levels = {0x01: 128, 0x02: 200, 0x03: 0, 0x0A: 0,   # AF/RF/SQL/RFpwr (power 0%)
                       0x06: 0, 0x12: 0, 0x07: 128, 0x08: 128,    # NR/NB level, twin PBT (center)
                       0x0B: 128, 0x0E: 128, 0x15: 128, 0x16: 128}  # MIC/COMP/MON/VOX level (M3)
        self.funcs = {0x02: 0, 0x50: 0, 0x22: 0, 0x40: 0,        # preamp, dial-lock, NB, NR
                      0x41: 0, 0x48: 0, 0x12: 2, 0x57: 0,          # A-notch, M-notch, AGC=MID, notch-W
                      0x44: 0, 0x45: 0, 0x46: 0, 0x58: 0}          # COMP, MON, VOX, TBW (M3)
        self.rit_on = 0; self.rit_freq = 0
        self.split = 0; self.duplex = 0; self.offset = 600000
        # Simulated TX status, answered on the 1C 00 read like the real radio: `tx` follows what CI-V
        # wrote (1C 00 01 / 1C 00 00), `front_tx` is a key from the radio itself (front panel, the
        # mic's PTT), set only by tests. Nothing is ever keyed: these are numbers.
        self.tx = 0
        self.front_tx = False
        from . import menu_engine
        self._menu = {}            # (dn_hi, dn_lo) -> stored value bytes
        self._menu_index = {tuple(menu_engine.civ_datanum(it.num)): it
                            for it in (getattr(profile, "menu", None) or [])}

    @property
    def name(self) -> str:
        return "Simulator"

    def start(self, on_bytes: Callable[[bytes], None]) -> None:
        self._on_bytes = on_bytes
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="civ-sim",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    # -- incoming commands ---------------------------------------------------
    def write(self, data: bytes) -> None:
        for fr in self._reader.feed(data):
            self._handle(fr)

    def _emit(self, cmd: int, sub: Optional[int], data: bytes = b"") -> None:
        # reply travels radio -> controller (to=E0, from=A2)
        frame = civ.build(cmd, sub, data,
                          radio_addr=civ.DEFAULT_CTRL_ADDR,
                          ctrl_addr=self._civ_addr)
        if self._on_bytes:
            self._on_bytes(frame)

    def _ok(self) -> None:
        self._emit(civ.OK, None)

    def _handle(self, fr: civ.Frame) -> None:
        c, s, d = fr.cmd, fr.sub, fr.data
        with self._lock:
            act = self.b[self.active]
            if c == 0x03:                                   # read freq (active band)
                self._emit(0x03, None, civ.freq_to_bcd(act["freq"]))
            elif c == 0x04:                                 # read mode (active band)
                self._emit(0x04, None, bytes([act["mode"], act["filt"]]))
            elif c == 0x05:                                 # set freq (active band)
                act["freq"] = civ.bcd_to_freq(bytes([s]) + d if s is not None else d)
                self._ok()
            elif c == 0x00:                                 # set freq (transceive)
                act["freq"] = civ.bcd_to_freq(bytes([s]) + d if s is not None else d)
            elif c == 0x06:                                 # set mode (active band)
                if s is not None:
                    act["mode"] = s
                    act["filt"] = d[0] if d else 0x01
                self._ok()
            elif c == 0x07:                                 # VFO / MAIN-SUB band select
                if s == 0xD0:
                    self.active = "main"; self._ok()
                elif s == 0xD1:
                    self.active = "sub"; self._ok()
                elif s == 0xD2:                             # read main/sub selection state
                    if not d:
                        self._emit(civ.NG, None)            # malformed: needs a 00/01 selector
                    else:
                        sel = d[0]                          # 00 = query main, 01 = query sub
                        queried = "main" if sel == 0x00 else "sub"
                        status = 0x01 if self.active == queried else 0x00
                        self._emit(0x07, 0xD2, bytes([sel, status]))
                else:
                    self._ok()
            elif c == 0x11:                                 # attenuator
                if s is not None:                           # set (value in sub)
                    self.att = s; self._ok()
                else:                                       # read
                    self._emit(0x11, self.att)
            elif c == 0x14:                                 # levels
                if d:                                       # set
                    self.levels[s] = civ.bcd_to_level(d)
                    self._ok()
                else:                                       # read
                    self._emit(0x14, s, civ.level_to_bcd(self.levels.get(s, 0)))
            elif c == 0x15:                                 # meters (read)
                self._emit(0x15, s, civ.level_to_bcd(self._meter(s)))
            elif c == 0x16:                                 # functions
                if d:
                    self.funcs[s] = d[0]
                    self._ok()
                else:
                    self._emit(0x16, s, bytes([self.funcs.get(s, 0)]))
            elif c == 0x21:                                 # RIT
                if s == 0x00:
                    if d:
                        self.rit_freq = civ.rit_from_bcd(d); self._ok()
                    else:
                        self._emit(0x21, 0x00, civ.rit_to_bcd(self.rit_freq))
                elif s == 0x01:
                    if d:
                        self.rit_on = d[0]; self._ok()
                    else:
                        self._emit(0x21, 0x01, bytes([self.rit_on]))
                else:
                    self._ok()
            elif c == 0x0F:                                 # split / duplex (mutually exclusive)
                if s is None:                               # read -> one documented status byte
                    if self.split:
                        self._emit(0x0F, 0x01)
                    elif self.duplex == 1:
                        self._emit(0x0F, 0x11)
                    elif self.duplex == 2:
                        self._emit(0x0F, 0x12)
                    else:
                        self._emit(0x0F, 0x00)
                elif s == 0x00:
                    self.split = 0; self._ok()
                elif s == 0x01:
                    self.split = 1; self.duplex = 0; self._ok()
                elif s in (0x10, 0x11, 0x12, 0x13):
                    self.duplex = min(2, s - 0x10); self.split = 0; self._ok()
                else:
                    self._ok()
            elif c == 0x0C:                                 # read duplex offset (3-byte BCD)
                self._emit(0x0C, None, civ.offset_to_bcd(self.offset))
            elif c == 0x0D:                                 # set duplex offset
                raw = (bytes([s]) if s is not None else b"") + d
                if raw:
                    self.offset = civ.offset_from_bcd(raw)
                self._ok()
            elif c == 0x19:                                 # read radio id
                self._emit(0x19, 0x00, bytes([self._civ_addr]))
            elif c == 0x18:                                 # power on/off
                self._ok()
            elif c == 0x27:                                 # scope control
                self._scope_cmd(s, d)
            elif c == 0x1A and s == 0x05:                   # SET-menu (1A 05 <data-number>)
                self._menu_cmd(d)
            elif c == 0x1A and s == 0x06:                   # DATA mode with filter
                if not d:                                   # read
                    on = act.get("data", 0)
                    self._emit(0x1A, 0x06, bytes([on, act["filt"] if on else 0]))
                elif d[0] and act["mode"] not in (0x00, 0x01, 0x02, 0x05):
                    self._emit(civ.NG, None)                # CW / RTTY: DATA is "OFF (fixed)"
                else:
                    act["data"] = 1 if d[0] else 0
                    if act["data"] and len(d) > 1 and d[1] in (1, 2, 3):
                        act["filt"] = d[1]
                    self._ok()
            elif c == 0x1C and s == 0x01:                   # internal antenna tuner
                if d:
                    self.tuner = 1 if d[0] in (1, 2) else 0; self._ok()
                else:
                    self._emit(0x1C, 0x01, bytes([getattr(self, "tuner", 0)]))
            elif c == 0x1C and s == 0x00:                   # TX status: set (CI-V key) / read
                if d:
                    self.tx = 1 if d[0] == 0x01 else 0; self._ok()
                else:
                    self._emit(0x1C, 0x00, bytes([1 if (self.tx or self.front_tx) else 0]))
            elif c == 0x1B and s in (0x00, 0x01):           # FM repeater tone / TSQL frequency
                if d:
                    self._tone_bcd = bytes(d); self._ok()
                else:
                    self._emit(0x1B, s, getattr(self, "_tone_bcd", civ.tone_to_bcd(88.5)))
            else:
                self._ok()

    def _scope_cmd(self, sub: Optional[int], d: bytes) -> None:
        if sub == 0x10:        # scope on/off
            if d:
                self.scope_on = d[0] == 1
            self._ok()
        elif sub == 0x11:      # data output on/off
            if d:
                self.scope_out = d[0] == 1
            self._ok()
        elif sub == 0x14:      # center/fixed
            if len(d) >= 2:
                self.scope_center = d[1] == 0
            self._ok()
        elif sub == 0x15:      # span (main/sub + 5 BCD)
            if len(d) >= 6:
                self.span = civ.bcd_to_freq(d[1:6]) or self.span
            self._ok()
        else:
            self._ok()

    def _meter(self, sub: Optional[int]) -> int:
        t = time.time() - self._t0
        if sub == 0x02:        # S-meter, breathe around S5-S7
            return int(70 + 35 * (0.5 + 0.5 * math.sin(t * 0.7)))
        return 0

    def _menu_cmd(self, d: bytes) -> None:
        """SET-menu 1A 05: d = data-number(2) [+ value for a write]."""
        if len(d) < 2:
            self._ok(); return
        key = (d[0], d[1])
        if len(d) > 2:                                  # write: store the value bytes
            self._menu[key] = bytes(d[2:]); self._ok()
        else:                                           # read: echo stored value or a default
            val = self._menu.get(key) or self._menu_default(d)
            self._emit(0x1A, 0x05, bytes([d[0], d[1]]) + val)

    def _menu_default(self, d: bytes) -> bytes:
        it = self._menu_index.get((d[0], d[1]))
        if it is None:
            return b"\x00"
        try:
            from . import menu_engine
            val = it.min if it.kind == "int" else 0
            return menu_engine.civ_write_data(it, val)[2:]   # strip the data number
        except Exception:
            return b"\x00"

    # -- scope generation ----------------------------------------------------
    def _loop(self) -> None:
        period = 1.0 / self.fps
        while not self._stop.is_set():
            with self._lock:
                run = self.scope_on and self.scope_out
                center = self.b[self.active]["freq"]
                span = self.span
            if run:
                self._emit_scope(center, span)
            time.sleep(period)

    def _emit_scope(self, center: int, span: int) -> None:
        wf = self._make_waveform(span)

        # header frame (div 1 of 11): main/sub, 1, 11, center/fixed=0,
        #   center freq (5), span (5), out-of-range
        header = bytearray([0x00, 0x01, 0x0B, 0x00])
        header += civ.freq_to_bcd(center)
        header += civ.freq_to_bcd(span)
        header += bytes([0x00])
        self._emit(0x27, 0x00, bytes(header))

        # 10 waveform frames (div 2..11)
        chunks = _split_even(wf, 10)
        for i, ch in enumerate(chunks):
            body = bytes([0x00, 0x02 + i, 0x0B]) + ch
            self._emit(0x27, 0x00, body)

    def _make_waveform(self, span: int, t: Optional[float] = None) -> bytes:
        """One sweep: the noise floor plus a band's worth of stations (_SIM_SIGNALS): CW keyed in real
        Morse timing, SSB voices talking in syllables and overs, one steady beacon. Widths are in Hz,
        so a station looks right at every span. `t` (seconds since the start) is for tests."""
        n = civ.SCOPE_POINTS
        if t is None:
            t = time.time() - self._t0
        hz_pt = max(1.0, span / n)                      # Hz per scope point
        level = [0.0] * n
        floor = 22.0
        for x in range(n):
            # cheap deterministic "noise"
            nse = (math.sin(x * 12.9898 + t * 7.0) * 43758.5453)
            level[x] = floor + (nse - math.floor(nse)) * 10

        def bump(cx: float, sigma: float, a: float) -> None:
            for x in range(max(0, int(cx - 4 * sigma)), min(n, int(cx + 4 * sigma) + 2)):
                level[x] += a * math.exp(-((x - cx) ** 2) / (2 * sigma * sigma))

        for s in _SIM_SIGNALS:
            cx = s["pos"] * n
            fade = 1.0 + 0.12 * math.sin(t * s["qsb"] + s["pos"] * 9.0)   # slow QSB
            if s["kind"] == "cw":
                if _cw_key_down(s, t):
                    bump(cx, max(0.7, 70.0 / hz_pt), s["amp"] * fade)
            elif s["kind"] == "ssb":
                strength, f1, f2 = _ssb_voice(s, t)
                if strength <= 0:
                    continue
                lo, hi = cx + 300.0 / hz_pt, cx + 2700.0 / hz_pt   # USB voice: 300-2700 Hz above the dial
                a = s["amp"] * strength * fade
                if hi - lo < 4:                         # a wide span: the voice is a point or two wide
                    bump((lo + hi) / 2, max(0.8, (hi - lo) / 2.5), a * 0.85)
                    continue
                for x in range(max(0, int(lo)), min(n, int(hi) + 1)):
                    f = (x - lo) / (hi - lo)            # 0..1 across the voice band
                    shape = 0.25 + math.exp(-((f - f1) / 0.12) ** 2) + 0.55 * math.exp(-((f - f2) / 0.16) ** 2)
                    level[x] += a * shape / 1.3
            else:                                       # steady carrier (a beacon)
                bump(cx, max(0.7, 90.0 / hz_pt), s["amp"] * fade)
        return bytes(int(min(civ.SCOPE_MAX, max(0.0, v))) for v in level)


# -- the simulator's band: stations on the scope ---------------------------------------------------------
_MORSE = {"A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.", "H": "....",
          "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.", "O": "---", "P": ".--.",
          "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
          "Y": "-.--", "Z": "--..", "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
          "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----."}


def _morse_keying(text: str, wpm: float, pause_s: float) -> tuple[list, float]:
    """The key-down intervals [(start, end), ...] in seconds of `text` sent at `wpm` (PARIS timing:
    dot 1.2/wpm s, dash 3 dots, 1 dot between elements, 3 between letters, 7 between words) and
    the length of one cycle: the message plus `pause_s` of silence before it repeats."""
    dot = 1.2 / wpm
    t, down = 0.0, []
    for w, word in enumerate(text.upper().split()):
        if w:
            t += 6 * dot                    # 1 (after the last element) + 6 = 7 between words
        for li, ch in enumerate(word):
            if li:
                t += 2 * dot                # 1 (after the last element) + 2 = 3 between letters
            for el in _MORSE.get(ch, ""):
                length = dot if el == "." else 3 * dot
                down.append((t, t + length))
                t += length + dot
    return down, t + pause_s


def _hash01(*k: float) -> float:
    """A repeatable 0..1 value for the inputs (the simulator stays deterministic)."""
    s = math.sin(sum(v * c for v, c in zip(k, (12.9898, 78.233, 37.719, 4.581)))) * 43758.5453
    return s - math.floor(s)


def _cw_key_down(s: dict, t: float) -> bool:
    """Whether the CW station `s` has its key down at `t` seconds."""
    ph = (t + s["phase"]) % s["cycle"]
    return any(a <= ph < b for a, b in s["down"])


def _ssb_voice(s: dict, t: float) -> tuple[float, float, float]:
    """The SSB station `s` at `t`: (strength 0..1, first and second formant 0..1 across the voice
    band). It talks for some 7 s overs and listens for others; within an over, 160 ms syllables
    with gaps between words."""
    ph = t + s["phase"]
    if _hash01(math.floor(ph / 7.0), s["seed"]) < 0.38:
        return 0.0, 0.0, 0.0                # its over is done: listening
    k = math.floor(ph / 0.16)
    if _hash01(k, s["seed"], 1) < 0.28:
        return 0.0, 0.0, 0.0                # a gap between words
    return (0.55 + 0.45 * _hash01(k, s["seed"], 2), 0.10 + 0.25 * _hash01(k, s["seed"], 3),
            0.45 + 0.35 * _hash01(k, s["seed"], 4))


def _cw(pos: float, amp: float, wpm: float, text: str, pause_s: float, phase: float, qsb: float) -> dict:
    down, cycle = _morse_keying(text, wpm, pause_s)
    return {"kind": "cw", "pos": pos, "amp": amp, "down": down, "cycle": cycle, "phase": phase, "qsb": qsb}


# Positions are 0..1 across the span, so the stations sit the same way at every span. Calls are
# placeholders (N0CALL, N1CALL), never real ones.
_SIM_SIGNALS = [
    {"kind": "ssb", "pos": 0.500, "amp": 128, "seed": 1, "phase": 0.0, "qsb": 0.40},   # the tuned station
    {"kind": "ssb", "pos": 0.655, "amp": 104, "seed": 2, "phase": 3.1, "qsb": 0.23},
    {"kind": "ssb", "pos": 0.835, "amp": 90, "seed": 3, "phase": 5.7, "qsb": 0.31},
    _cw(0.120, 118, 22, "CQ CQ DE N0CALL N0CALL K", 3.0, 0.0, 0.27),
    _cw(0.255, 96, 18, "N1CALL DE N0CALL 599 599 TU", 4.5, 2.2, 0.35),
    _cw(0.300, 82, 26, "TEST N0CALL TEST", 2.0, 1.1, 0.52),
    _cw(0.405, 106, 14, "CQ DX DE N1CALL K", 6.0, 4.0, 0.19),
    {"kind": "carrier", "pos": 0.720, "amp": 104, "phase": 0.0, "qsb": 0.61},          # a steady beacon
]


def _split_even(data: bytes, parts: int) -> list[bytes]:
    n = len(data)
    base = n // parts
    rem = n % parts
    chunks = []
    i = 0
    for p in range(parts):
        size = base + (1 if p < rem else 0)
        chunks.append(data[i:i + size])
        i += size
    return chunks


class YaesuSimTransport(Transport):
    """A synthetic Yaesu CAT radio (FT-991A): answers FA/FB/MD/SM/TX/ID/IF reads
    and applies sets, so the FT-991A profile is demoable with no radio. There is
    no scope (the real FT-991A exposes none over CAT)."""

    def __init__(self, profile=None) -> None:
        self._on_bytes: Optional[Callable[[bytes], None]] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._buf = ""
        self._t0 = time.time()
        self.freq = profile.default_freq if profile is not None else 14_074_000
        self.mode = "2"            # USB
        self._menu_index = {it.num: it for it in (getattr(profile, "menu", None) or [])}
        self._menu_vals = {}       # num -> wire value field (as written), for EX read-back
        self._levels = {"PC": "025", "AG0": "140", "RG0": "200",   # power(W)/AF/RF: so the
                        "SQ0": "015", "MG": "050", "GT0": "2"}      # sim reports real levels
        # operating toggles/enums echoed on read (key = command prefix, value = param field)
        self._ops = {"NA0": "0", "CT0": "0", "CN00": "012", "CN01": "000", "OS0": "0",
                     "SH0": "00", "CO00": "0000", "CO01": "0300", "CO02": "0000", "CO03": "0025",
                     "ML0": "000", "ML1": "030", "PR0": "1", "PR1": "1", "TS": "0",
                     "BI": "0", "KR": "0", "KS": "020", "KP": "40", "CS": "0", "SC": "0", "FS": "0"}
        # Simulated TX state, answered on the TX; poll like the real radio: "0" receive, "1" keyed
        # by CAT (TX1;), "2" keyed from the radio itself (front panel / mic PTT). Without this the
        # sim always answered TX0; and the TX arbiter's reconciliation dropped every PTT claim on
        # the FT-991A profile about 2.5 s after keying. Nothing is ever keyed: this is a number.
        self.tx = "0"
        if getattr(profile, "sh_onoff", False):
            self._ops["SH0"] = "000"   # the FT-891 form: WIDTH off (the default bandwidth), code 00

    @property
    def name(self) -> str:
        return "Simulator (CAT)"

    def start(self, on_bytes: Callable[[bytes], None]) -> None:
        self._on_bytes = on_bytes
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="yaesu-sim")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():    # idle; replies are produced in write()
            time.sleep(0.2)

    def write(self, data: bytes) -> None:
        try:
            s = data.decode("ascii", "ignore")
        except Exception:
            return
        with self._lock:
            self._buf += s
            while ";" in self._buf:
                cmd, self._buf = self._buf.split(";", 1)
                self._handle(cmd.strip())

    def _handle(self, cmd: str) -> None:
        out = None
        if cmd == "FA":
            out = f"FA{self.freq:09d};"
        elif cmd.startswith("FA") and len(cmd) >= 11:
            try:
                self.freq = int(cmd[2:11])
            except ValueError:
                pass
        elif cmd == "FB":
            out = f"FB{self.freq:09d};"
        elif cmd == "MD0":
            out = f"MD0{self.mode};"
        elif cmd.startswith("MD0") and len(cmd) >= 4:
            self.mode = cmd[3]
        elif cmd == "SM0":
            lvl = int(90 + 70 * abs(math.sin((time.time() - self._t0) * 0.7)))   # wandering S-meter
            out = f"SM0{min(255, lvl):03d};"
        elif cmd.startswith("RM") and len(cmd) >= 3 and cmd[2] in "012345678":   # READ METER
            p1 = cmd[2]
            v = int(70 + 80 * abs(math.sin((time.time() - self._t0) * (0.6 + int(p1) * 0.13))))
            out = f"RM{p1}{min(255, max(0, v)):03d};"
        elif cmd == "TX":
            out = f"TX{self.tx};"
        elif len(cmd) == 3 and cmd.startswith("TX") and cmd[2] in "012":
            self.tx = cmd[2]                       # TX0; unkey / TX1; CAT key / TX2; radio-side key
        elif cmd == "ID":
            out = "ID0670;"
        elif cmd == "IF":
            out = f"IF001{self.freq:09d}+000000{self.mode}0000000;"
        elif cmd in self._levels:                  # level read: PC/AG0/RG0/SQ0/MG/GT0
            out = f"{cmd}{self._levels[cmd]};"
        elif any(cmd.startswith(k) and cmd[len(k):].isdigit() for k in self._levels):
            for k in self._levels:                 # level set -> remember (sim)
                if cmd.startswith(k) and cmd[len(k):].isdigit():
                    self._levels[k] = cmd[len(k):]; break
        elif any(cmd == p or (cmd.startswith(p) and cmd[len(p):].isdigit()) for p in self._ops):
            for pfx in self._ops:                  # NA0/CT0/CN00/CN01/OS0 read + write
                if cmd == pfx:
                    out = f"{pfx}{self._ops[pfx]};"; break
                if cmd.startswith(pfx) and cmd[len(pfx):].isdigit():
                    self._ops[pfx] = cmd[len(pfx):]; break
        elif cmd.startswith("EX") and len(cmd) >= 5 and cmd[2:5].isdigit():     # SET menu
            # menu-number width varies (FT-991A NNN=3, FT-891 GGNN=4); match a known item.
            body = cmd[2:]
            num = it = None
            for w in (4, 3):
                if len(body) >= w and body[:w].isdigit() and int(body[:w]) in self._menu_index:
                    num, it = int(body[:w]), self._menu_index[int(body[:w])]
                    field = body[w:]
                    break
            if it is not None:
                if field:                              # write -> remember it
                    self._menu_vals[num] = field
                else:                                  # read -> echo stored value or a default
                    w = getattr(it, "ex_width", 3)
                    out = f"EX{num:0{w}d}{self._menu_vals.get(num) or self._ex_default(num)};"
        if out and self._on_bytes:
            self._on_bytes(out.encode("ascii"))

    def _ex_default(self, num: int) -> str:
        """A plausible default value field for an unwritten EX read (sim only)."""
        from . import menu_engine
        if num == 87:
            return "0670"                              # RADIO ID (read-only)
        it = self._menu_index.get(num)
        if it is None:
            return "0"
        try:
            val = it.min if it.kind == "int" else 0    # enum -> index 0, signed -> 0
            if it.name == "CW FREQ DISPLAY":
                val = 1                                # the radio's factory default: PITCH (offset)
            elif it.name.startswith("RPT SHIFT "):     # the repeater offsets (kHz): the usual US ones
                val = {"28MHz": 100, "50MHz": 1000, "144MHz": 600, "430MHz": 5000}.get(it.name[10:], val)
            w = getattr(it, "ex_width", 3)
            return menu_engine.yaesu_encode(it, val)[2 + w:-1]   # strip EX<num> and ';'
        except Exception:
            return "0"

    def stop(self) -> None:
        self._stop.set()
        self.tx = "0"
        if self._thread:
            self._thread.join(timeout=1.0)
