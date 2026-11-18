"""The Hamlib `rigctld` text protocol, pure and I/O-free (the WSJT-X bridge, backend/bridge.py).

This is the wire format a program speaks when its radio is set to "Hamlib NET rigctl" (WSJT-X,
JS8Call, Fldigi, loggers). TeleRig answers it itself, from its own radio handlers, so those programs
can share a radio that TeleRig already holds, including an Icom on its LAN port, which Hamlib
cannot reach.

Checked against the real programs: the replies below were compared with rigctld 4.6.5, 4.7.1 (the
build that ships with WSJT-X) and 4.7.2 driving Hamlib's dummy rig, and the bridge is exercised by
the real `rigctl -m 2` client of each (tests/t_bridge.py).

Request lines (one or more commands per line, LF or CRLF):
  f                 short command, no argument
  F 14074000.000000 short command with arguments (frequencies arrive as floats)
  \\get_freq         long command (backslash + name)
  +f  ;f  |f  ,f    extended reply: `get_freq:` + `Label: value` records + `RPRT 0`, joined by a
                    newline (+) or by the given separator character

Short replies: the values one per line; setters and errors answer `RPRT <code>` (0 = OK, negative =
a Hamlib error code).

Only what a digital-mode program needs is implemented (frequency, mode, PTT, VFO and split
queries, a few levels, the capability dump). Every other Hamlib command is recognised, so a line
stays in step, and answered `RPRT -4` (not implemented) or `RPRT -11` (not available).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# -- Hamlib error codes (include/hamlib/rig.h, enum rig_errcode_e), negated on the wire ------------
OK = 0
EINVAL = -1          # invalid parameter
ENIMPL = -4          # function not implemented
EIO = -6             # IO error (no radio)
ERJCTED = -9         # command rejected by the rig (busy: someone else owns TX)
ENAVAIL = -11        # function not available (this radio / this bridge)
EDEPRECATED = -18
EACCESS = -22        # permission denied (transmit is switched off or paused)

LINE_MAX = 512       # bytes per request line; a longer one closes the connection
CMDS_PER_LINE = 16

# -- Hamlib mode bits (rmode_t) -----------------------------------------------------------------
MODE_BITS = {
    "AM": 1 << 0, "CW": 1 << 1, "USB": 1 << 2, "LSB": 1 << 3, "RTTY": 1 << 4, "FM": 1 << 5,
    "WFM": 1 << 6, "CWR": 1 << 7, "RTTYR": 1 << 8, "AMS": 1 << 9, "PKTLSB": 1 << 10,
    "PKTUSB": 1 << 11, "PKTFM": 1 << 12, "FMN": 1 << 21, "PKTAM": 1 << 22, "DSTAR": 1 << 24,
    "AMN": 1 << 29, "DD": 1 << 32, "C4FM": 1 << 33, "CWN": 1 << 36,
}

# Hamlib level bits used here (setting_t)
LEVEL_RFPOWER = 1 << 12
LEVEL_STRENGTH = 1 << 30

VFO_A, VFO_B = 1 << 0, 1 << 1
PTT_RIG = 1          # ptt_type: keyed over CAT
PTT_NONE = 0

# Hamlib model numbers (`rigctl -l`), reported in dump_state like a real rigctld would.
HAMLIB_MODEL = {"ic9700": 3081, "ic7300mk2": 3094, "ft991a": 1035, "ft891": 1036, "ic728": 3016}
MODEL_UNKNOWN = 1    # Hamlib "Dummy": a radio this table does not know yet

# -- mode names: TeleRig (per protocol) <-> Hamlib ------------------------------------------------
# Icom: (mode name, data mode on) <-> Hamlib. The -D (data) variants need the profile's data_mode.
_CIV = {
    ("USB", 0): "USB", ("LSB", 0): "LSB", ("CW", 0): "CW", ("CW-R", 0): "CWR",
    ("RTTY", 0): "RTTY", ("RTTY-R", 0): "RTTYR", ("AM", 0): "AM", ("FM", 0): "FM",
    ("DV", 0): "DSTAR", ("DD", 0): "DD", ("CW-NAR", 0): "CWN",
    ("USB", 1): "PKTUSB", ("LSB", 1): "PKTLSB", ("FM", 1): "PKTFM", ("AM", 1): "PKTAM",
}
_CIV_REV = {v: k for k, v in _CIV.items()}
# Yaesu: the radio's own labels (yaesu.YAESU_MD / MD_DECODE) <-> Hamlib (as Hamlib's newcat does).
_YAESU = {
    "USB": "USB", "LSB": "LSB", "AM": "AM", "FM": "FM", "CW-USB": "CW", "CW-LSB": "CWR",
    "RTTY-LSB": "RTTY", "RTTY-USB": "RTTYR", "DATA-USB": "PKTUSB", "DATA-LSB": "PKTLSB",
    "DATA-FM": "PKTFM", "C4FM": "C4FM", "FM-N": "FMN", "AM-N": "AMN",
}
_YAESU_REV = {v: k for k, v in _YAESU.items()}


def _proto(profile) -> str:
    return getattr(profile, "protocol", "civ") or "civ"


def _data_ok(profile) -> bool:
    return bool(getattr(getattr(profile, "capabilities", None), "data_mode", False))


def to_hamlib_mode(profile, state: dict) -> str:
    """The Hamlib name of the radio's current mode ("" when Hamlib has no name for it)."""
    name = str((state or {}).get("mode_name") or "")
    if _proto(profile) == "yaesu":
        return _YAESU.get(name, "")
    data = 1 if (state or {}).get("data") and _data_ok(profile) else 0
    return _CIV.get((name, data)) or _CIV.get((name, 0), "")


def from_hamlib_mode(profile, mode: str) -> Optional[tuple]:
    """(TeleRig mode name, data on | None) for a Hamlib mode this radio has, else None.
    `data` is None where the radio has no separate data switch (Yaesu, old Icoms)."""
    m = str(mode or "").strip().upper()
    modes = list(getattr(profile, "modes", None) or [])
    if _proto(profile) == "yaesu":
        name = _YAESU_REV.get(m)
        return (name, None) if name in modes else None
    hit = _CIV_REV.get(m)
    if hit is None:
        return None
    name, data = hit
    if name not in modes:
        return None
    if not _data_ok(profile):
        return (name, None) if data == 0 else None
    return (name, bool(data))


def hamlib_modes(profile) -> list:
    """Every Hamlib mode this radio accepts, in the profile's order."""
    out: list = []
    for name in list(getattr(profile, "modes", None) or []):
        if _proto(profile) == "yaesu":
            cands = [_YAESU.get(name)]
        else:
            cands = [_CIV.get((name, 0))]
            if _data_ok(profile):
                cands.append(_CIV.get((name, 1)))
        for c in cands:
            if c and c not in out:
                out.append(c)
    return out


def mode_mask(profile) -> int:
    mask = 0
    for m in hamlib_modes(profile):
        mask |= MODE_BITS.get(m, 0)
    return mask


# -- S-meter: TeleRig's 0..241 scale (0 = S0, 120 = S9, 241 = S9+60 dB) -> dB relative to S9 -------
def strength_db(smeter) -> int:
    try:
        v = max(0, min(241, int(smeter)))
    except (TypeError, ValueError):
        return -54
    if v <= 120:
        return round(v / 120 * 54 - 54)
    return round((v - 120) / 121 * 60)


# -- the capability dump (\dump_state, protocol version 1) ---------------------------------------
def dump_state(profile, *, version: str, ptt: bool = True) -> str:
    """What `\\dump_state` answers: the layout Hamlib's NET rigctl client parses line by line
    (rigctld protocol version 1), ending with `done`. `ptt` False reports "no CAT PTT" so a client
    never believes it can key a radio it may not."""
    modes = mode_mask(profile) or MODE_BITS["USB"]
    bands = list(getattr(profile, "bands", None) or [])
    lo = min([int(b.lo) for b in bands] or [30_000])
    hi = max([int(b.hi) for b in bands] or [470_000_000])
    vfos = VFO_A | VFO_B
    can_ptt = bool(ptt and getattr(getattr(profile, "capabilities", None), "ptt", False))
    L: list = []
    L.append("1")                                            # rigctld protocol version
    L.append(str(HAMLIB_MODEL.get(getattr(profile, "id", ""), MODEL_UNKNOWN)))
    L.append("0")                                            # ITU region (unused)
    # RX range: the whole span the bands cover (radios receive between them too)
    L.append(f"{lo:.6f} {hi:.6f} 0x{modes:x} -1 -1 0x{vfos:x} 0x1")
    L.append("0 0 0 0 0 0 0")
    for b in bands:                                          # TX ranges: one per band
        L.append(f"{int(b.lo):.6f} {int(b.hi):.6f} 0x{modes:x} -1 -1 0x{vfos:x} 0x1")
    L.append("0 0 0 0 0 0 0")
    L.append(f"0x{modes:x} 1")                               # tuning steps: 1 Hz
    L.append("0 0")
    L.append("0 0")                                          # filters: none advertised
    L.append("0")                                            # max RIT
    L.append("0")                                            # max XIT
    L.append("0")                                            # max IF shift
    L.append("0")                                            # announces
    L.append("0 ")                                           # preamp list
    L.append("0 ")                                           # attenuator list
    L.append("0x0")                                          # has_get_func
    L.append("0x0")                                          # has_set_func
    L.append(f"0x{LEVEL_STRENGTH | LEVEL_RFPOWER:x}")        # has_get_level
    L.append(f"0x{LEVEL_RFPOWER:x}")                         # has_set_level
    L.append("0x0")                                          # has_get_parm
    L.append("0x0")                                          # has_set_parm
    L.append("vfo_ops=0x0")
    L.append(f"ptt_type=0x{PTT_RIG if can_ptt else PTT_NONE:x}")
    L.append("targetable_vfo=0x0")
    L.append("has_set_vfo=1")
    L.append("has_get_vfo=1")
    L.append("has_set_freq=1")
    L.append("has_get_freq=1")
    L.append("has_set_conf=0")
    L.append("has_get_conf=0")
    L.append("has_power2mW=0")
    L.append("has_mW2power=0")
    L.append("has_get_ant=0")
    L.append("has_set_ant=0")
    L.append("timeout=0")
    L.append(f"rig_model={HAMLIB_MODEL.get(getattr(profile, 'id', ''), MODEL_UNKNOWN)}")
    L.append(f"rigctld_version={version}")
    L.append("agc_levels=")
    L.append("ctcss_list=")
    L.append("dcs_list=")
    L.append("level_gran=")
    L.append("parm_gran=")
    L.append(f"hamlib_version={version}")
    L.append("done")
    return "\n".join(L) + "\n"


# -- the command table ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Spec:
    name: str                    # Hamlib long name
    nargs: int = 0               # arguments it takes
    labels: tuple = ()           # extended-reply labels of its output values ("" = bare value)
    rest: bool = False           # the last argument is the rest of the line (send_morse)


# short command character -> Spec. Handled commands first, then the ones only recognised.
_SHORT = {
    "f": Spec("get_freq", 0, ("Frequency",)), "F": Spec("set_freq", 1),
    "m": Spec("get_mode", 0, ("Mode", "Passband")), "M": Spec("set_mode", 2),
    "v": Spec("get_vfo", 0, ("VFO",)), "V": Spec("set_vfo", 1),
    "t": Spec("get_ptt", 0, ("PTT",)), "T": Spec("set_ptt", 1),
    "s": Spec("get_split_vfo", 0, ("Split", "TX VFO")), "S": Spec("set_split_vfo", 2),
    "i": Spec("get_split_freq", 0, ("TX Frequency",)), "I": Spec("set_split_freq", 1),
    "x": Spec("get_split_mode", 0, ("TX Mode", "TX Passband")), "X": Spec("set_split_mode", 2),
    "k": Spec("get_split_freq_mode", 0, ("TX Frequency", "TX Mode", "TX Passband")),
    "K": Spec("set_split_freq_mode", 3),
    "j": Spec("get_rit", 0, ("RIT",)), "J": Spec("set_rit", 1),
    "z": Spec("get_xit", 0, ("XIT",)), "Z": Spec("set_xit", 1),
    "l": Spec("get_level", 1, ("",)), "L": Spec("set_level", 2),
    "u": Spec("get_func", 1, ("",)), "U": Spec("set_func", 2),
    "p": Spec("get_parm", 1, ("",)), "P": Spec("set_parm", 2),
    "_": Spec("get_info", 0, ("Info",)),
    "q": Spec("quit"), "Q": Spec("quit"),
    "1": Spec("dump_caps"), "3": Spec("dump_conf"),
    "2": Spec("power2mW", 3), "4": Spec("mW2power", 3),
    # recognised only (argument counts keep a multi-command line in step)
    "n": Spec("get_ts"), "N": Spec("set_ts", 1),
    "r": Spec("get_rptr_shift"), "R": Spec("set_rptr_shift", 1),
    "o": Spec("get_rptr_offs"), "O": Spec("set_rptr_offs", 1),
    "c": Spec("get_ctcss_tone"), "C": Spec("set_ctcss_tone", 1),
    "d": Spec("get_dcs_code"), "D": Spec("set_dcs_code", 1),
    "e": Spec("get_mem"), "E": Spec("set_mem", 1),
    "h": Spec("get_channel", 2), "H": Spec("set_channel", 1, rest=True),
    "B": Spec("set_bank", 1),
    "y": Spec("get_ant", 1), "Y": Spec("set_ant", 2),
    "a": Spec("get_trn"), "A": Spec("set_trn", 1),
    "G": Spec("vfo_op", 1), "g": Spec("scan", 2),
    "w": Spec("send_cmd", 1, rest=True), "W": Spec("send_cmd_rx", 2, rest=True),
    "b": Spec("send_morse", 1, rest=True),
    "*": Spec("reset", 1),
}
# long-only commands
_LONG_ONLY = [
    Spec("dump_state"), Spec("chk_vfo"),
    Spec("get_powerstat", 0, ("Power Status",)), Spec("set_powerstat", 1),
    Spec("get_lock_mode", 0, ("Locked",)), Spec("set_lock_mode", 1),
    Spec("get_vfo_info", 1, ("Freq", "Mode", "Width", "Split", "SatMode")),
    Spec("get_rig_info"), Spec("get_dcd", 0, ("DCD",)),
    Spec("get_vfo_list"), Spec("get_modes"),
    # recognised only
    Spec("set_vfo_opt", 1), Spec("stop_morse"), Spec("wait_morse"), Spec("send_voice_mem", 1),
    Spec("stop_voice_mem"), Spec("send_dtmf", 1), Spec("recv_dtmf"), Spec("set_ctcss_sql", 1),
    Spec("get_ctcss_sql"), Spec("set_dcs_sql", 1), Spec("get_dcs_sql"), Spec("set_twiddle", 1),
    Spec("get_twiddle"), Spec("uplink", 1), Spec("set_cache", 1), Spec("get_cache"),
    Spec("get_mode_bandwidths", 1), Spec("get_clock"), Spec("set_clock", 1), Spec("halt"),
    Spec("pause", 1), Spec("password", 1), Spec("send_raw", 2, rest=True), Spec("client_version", 1),
    Spec("hamlib_version"), Spec("get_separator"), Spec("set_separator", 1), Spec("chk_blk"),
    Spec("get_conf", 1), Spec("set_conf", 2), Spec("test"),
]
SPECS: dict = {s.name: s for s in list(_SHORT.values()) + _LONG_ONLY}
SHORT: dict = dict(_SHORT)

# The commands the bridge answers itself (everything else recognised -> RPRT -4).
HANDLED = frozenset({
    "get_freq", "set_freq", "get_mode", "set_mode", "get_vfo", "set_vfo", "get_ptt", "set_ptt",
    "get_split_vfo", "set_split_vfo", "get_split_freq", "set_split_freq", "get_split_mode",
    "set_split_mode", "get_split_freq_mode", "set_split_freq_mode", "get_rit", "set_rit", "get_xit",
    "set_xit", "get_level", "set_level", "get_func", "set_func", "get_parm", "set_parm", "get_info",
    "quit", "dump_caps", "dump_state", "chk_vfo", "get_powerstat", "set_powerstat", "get_lock_mode",
    "set_lock_mode", "get_vfo_info", "get_rig_info", "get_dcd", "get_vfo_list", "get_modes",
    "get_trn", "set_trn"})

EXT_PREFIX = "+;|,"


@dataclass
class Command:
    name: str                               # Hamlib long name ("" = not a known command)
    args: list = field(default_factory=list)
    ext: Optional[str] = None               # None = short reply; else the record separator
    raw: str = ""                           # the token as typed (for error text)
    known: bool = True
    missing: bool = False                   # too few arguments on the line


class ProtocolError(Exception):
    """The line cannot be a rigctl request at all (binary data, too many commands)."""


def looks_like_http(line: str) -> bool:
    """A browser (or any HTTP client) on this port: its first line is a request line. A web page
    can make a browser POST to 127.0.0.1:<port>; such a connection is dropped before any of its
    lines could be read as a command."""
    s = line.strip()
    head = s.split(" ", 1)[0].upper() if s else ""
    return (head in ("GET", "POST", "PUT", "HEAD", "OPTIONS", "DELETE", "PATCH", "CONNECT", "TRACE", "PRI")
            or " HTTP/" in s.upper())


def parse_line(line: str) -> list:
    """One request line -> its commands, in order (the line end, LF or CRLF, may still be on it).
    Raises ProtocolError for a line that is not text or holds more than CMDS_PER_LINE commands."""
    line = line.rstrip("\r\n")
    if any((ord(ch) < 32 and ch != "\t") or ord(ch) == 127 for ch in line):
        raise ProtocolError("control characters")
    out: list = []
    toks = line.split()
    i = 0
    while i < len(toks):
        if len(out) >= CMDS_PER_LINE:
            raise ProtocolError("too many commands on one line")
        tok = toks[i]
        i += 1
        ext = None
        if tok[0] in EXT_PREFIX:
            ext = "\n" if tok[0] == "+" else tok[0]
            tok = tok[1:]
            if not tok:                      # "+ f": the prefix stood alone
                if i >= len(toks):
                    break
                tok = toks[i]
                i += 1
        if tok.startswith("\\"):
            name = tok[1:]
            spec = SPECS.get(name)
            if spec is None:
                out.append(Command("", [], ext, tok, known=False))
                break                        # nothing after an unknown long command can be trusted
            pending = [(spec, tok)]
        else:
            # a run of short commands typed together ("fmv"); the last one takes the arguments
            pending = []
            bad = False
            for ch in tok:
                spec = SHORT.get(ch)
                if spec is None:
                    bad = True
                    break
                pending.append((spec, ch))
            if bad or not pending:
                out.append(Command("", [], ext, tok, known=False))
                break
            if any(sp.nargs for sp, _ in pending[:-1]):
                # e.g. "F14074000": arguments glued to the letter are not rigctl syntax
                out.append(Command("", [], ext, tok, known=False))
                break
        for n, (spec, rawtok) in enumerate(pending):
            last = n == len(pending) - 1
            args: list = []
            missing = False
            if last and spec.nargs:
                if spec.rest:
                    need = spec.nargs - 1
                    args = toks[i:i + need]
                    i += len(args)
                    args.append(" ".join(toks[i:]))
                    i = len(toks)
                    missing = len(args) < spec.nargs or not args[-1]
                else:
                    args = toks[i:i + spec.nargs]
                    i += len(args)
                    # "M ?" / "V ?" / "l ?" ask for the list of choices: one argument is enough
                    missing = len(args) < spec.nargs and args[:1] != ["?"]
            out.append(Command(spec.name, args, ext, rawtok, missing=missing))
            if len(out) > CMDS_PER_LINE:
                raise ProtocolError("too many commands on one line")
    return out


# -- replies -------------------------------------------------------------------------------------
def rprt(code: int) -> str:
    return f"RPRT {int(code)}\n"


def reply(cmd: Command, values: Optional[list] = None, code: int = OK, *, always_rprt: bool = False) -> str:
    """Format one command's answer. `values` are a getter's output values (strings), None for a
    setter. An error (code < 0) answers RPRT only. Extended replies repeat the command and label
    each value."""
    spec = SPECS.get(cmd.name)
    if cmd.ext is None:
        if code != OK or values is None:
            return rprt(code)
        body = "".join(f"{v}\n" for v in values)
        return body + (rprt(OK) if always_rprt else "")
    sep = cmd.ext
    head = f"{cmd.name}:" + (" " + " ".join(str(a) for a in cmd.args) if cmd.args else "")
    recs = [head]
    if code == OK and values is not None:
        labels = spec.labels if spec else ()
        for n, v in enumerate(values):
            lab = labels[n] if n < len(labels) else ""
            recs.append(f"{lab}: {v}" if lab else f"{v}")
    recs.append(f"RPRT {int(code)}")
    return sep.join(recs) + "\n"


def parse_freq(text: str) -> Optional[int]:
    """`14074000`, `14074000.000000` (what the Hamlib client sends) -> Hz, else None."""
    try:
        f = float(text)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return int(f)


def parse_int(text: str) -> Optional[int]:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None
