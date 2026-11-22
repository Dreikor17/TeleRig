"""
Radio profiles — everything that differs between supported radios lives here, so
adding a radio is mostly just adding a RadioProfile to PROFILES (a new make/protocol
also needs a handler class).

Within a make the protocol is shared (Icom: CI-V framing, the 27 00 scope, RS-BA1 LAN,
audio; Yaesu: CAT) — only address/baud, band plan, mode set, filter widths and a couple
of menu numbers change per model.

>>> ADDING A RADIO? Follow the transmit-safety contract in docs/ADDING-A-RADIO.md.
Every radio that can key TX MUST have: operator-triggered keying only (NO autonomous
transmission), the 120 s PTT stuck-TX failsafe in its handler (one SSTV picture: its own
limit, `hold` in set_ptt), a 3-minute TOT set on connect (`tot_s`), a
bounded + stoppable CW send, unkey + restore on disconnect, RF power read (not forced) on
connect, and `tx_tot` / `tx_route` (plus `critical`) on its TX time-out and PTT-routing menu
items. On top, the server's TX arbiter allows one TX owner: the owner's drop unkeys at once,
a silent owner after 6 s, other clients' drops never; it keeps its own 120 s session clock
(one SSTV picture: its time on the air + 5 s, at most 3 s inside the TOT);
a front-panel key is never unkeyed by a browser drop -- only on radios that report TX: both
handlers read TX state (Yaesu CAT `TX;`, Icom CI-V `1C 00`); the IC-728 has no CI-V TX command,
so its front panel stays invisible. The high-SWR cutoff + warning is required but not
implemented in any handler yet. Not optional. <<<
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Band:
    name: str          # button label, e.g. "20" or "144"
    lo: int            # band low edge (Hz)
    hi: int            # band high edge (Hz)
    default: int       # frequency the band button tunes to (Hz)


@dataclass
class SerialCfg:
    baud: int = 115200
    bits: int = 8
    parity: str = "N"          # N | E | O
    stopbits: int = 1          # Icom CI-V = 1, Yaesu CAT = 2


@dataclass
class NetworkCfg:
    enabled: bool = False
    control_port: int = 50001  # RS-BA1 UDP: control / serial / audio
    serial_port: int = 50002
    audio_port: int = 50003


@dataclass
class AudioCfg:
    kind: str = "none"         # usb-codec | lan | none


@dataclass
class ScopeCfg:
    kind: str = "native"       # native (CI-V 27 00) | audio (AF FFT) | none


@dataclass
class Transports:
    serial: SerialCfg = field(default_factory=SerialCfg)
    network: NetworkCfg = field(default_factory=NetworkCfg)
    audio: AudioCfg = field(default_factory=AudioCfg)
    scope: ScopeCfg = field(default_factory=ScopeCfg)


@dataclass
class Capabilities:
    """What the radio supports — drives which controls the adaptive UI renders."""
    preamp: bool = True
    att: bool = True
    tuner: bool = False
    dual_watch: bool = False
    vfo_select: bool = False   # a CAT active-VFO (A/B) selector. FT-991A: no.
    vfo_swap: bool = True
    split: bool = True
    rit: bool = True
    duplex: bool = True
    # The radio's duplex (repeater) offset can be set over CAT: Icom CI-V 0D (the `offset` action).
    # False = it is a menu setting only (Yaesu RPT SHIFT) or TeleRig does not set it.
    duplex_offset: bool = False
    rx_dsp: list = field(default_factory=list)    # ["nb","nr","anotch","mnotch"]
    tx_funcs: list = field(default_factory=list)  # ["comp","vox","mon"]
    tbw: bool = True
    meters: list = field(default_factory=list)    # ["S","PO","SWR","ALC","COMP","Vd","Id"]
    cw_tx: bool = False
    menus: bool = False
    narrow: bool = False        # NAR/WIDE IF-filter toggle (Yaesu NA)
    fm_tone: bool = False       # FM Tone/DCS panel, FM modes only
    fm_dcs: bool = True         # the FM panel exposes DCS (Yaesu) — False on Icom (TeleRig drives no DTCS)
    ext_ops: bool = False       # extra Yaesu operating controls: WIDTH/CONTOUR/APF/CW/TXW/scan
    icom_cw: bool = False       # Icom CW/filter group: APF/break-in/pitch/keyer-speed/filter-shape
    # Coarse presence flags for old/limited rigs (IC-728): default True so every existing radio
    # is unaffected. A minimal radio turns these off to hide whole panels it can't drive.
    ptt: bool = True            # transmit path (PTT button + TX-audio/mic). False = RX/control only.
    tab_rx: bool = True         # the RX control tab (filter/levels/preamp/att)
    tab_tx: bool = True         # the TX control tab (comp/vox/mon/tuner/split/RIT)
    tab_dsp: bool = True        # the DSP control tab
    memory: bool = False        # Radio-tab MEMORY recall group (CI-V 08/0A). Off unless a rig uses it.
    # Icom DATA mode switch (CI-V 1A 06): USB-D / LSB-D / FM-D / AM-D. In DATA mode the radio takes
    # its modulation from the DATA MOD source and bypasses the speech processor, which is what a
    # digital-mode program (WSJT-X "Data/Pkt") expects. Yaesu has real DATA-USB/LSB/FM modes instead.
    data_mode: bool = False


@dataclass
class MenuItem:
    """One radio SET-menu item, fully declarative (see backend/menu_engine.py).
    `digits` is the fixed value-field width on the wire — a wrong width makes the radio
    silently ignore the command, so it is the #1 correctness factor."""
    num: int
    name: str
    group: str
    kind: str = "enum"         # enum | int | signed-int
    digits: int = 1
    ex_width: int = 3          # Yaesu EX menu-number width: FT-991A flat EXNNN=3; FT-891 GGNN=4
    options: list = field(default_factory=list)   # enum option labels (index = wire value)
    min: int = 0
    max: int = 0
    step: int = 1
    unit: str = ""
    readonly: bool = False
    critical: bool = False     # connection/transmit-sensitive -> UI confirms before write
    note: str = ""
    tx_tot: bool = False       # the radio's TX time-out timer (writing OFF needs admin + confirm)
    tx_route: bool = False     # PTT/keying routing (options name RTS/DTR lines; idle + confirm)


@dataclass
class RadioProfile:
    id: str                       # "ic9700"
    name: str                     # "IC-9700"
    civ_addr: int                 # default CI-V address
    modes: list[str]              # mode buttons, in order
    bands: list[Band]
    filter_bw: dict               # mode_name -> {1: hz, 2: hz, 3: hz}
    mod_dataoff: tuple            # CI-V 1A 05 data number for DATA OFF MOD
    lan_mod_level: tuple          # CI-V 1A 05 data number for LAN MOD Level
    default_freq: int             # simulator / initial frequency
    steps: list                   # [(value_hz, label), ...]
    default_step: int
    dual_watch: bool = False      # True for dual-receiver radios (IC-9700 MAIN/SUB)
    has_preamp: bool = True        # P.AMP available
    has_att: bool = True           # attenuator available
    has_tuner: bool = False        # internal antenna tuner (in/out toggle) available
    # Safety: hardware TX time-out timer set on connect (backstop if the control link drops
    # mid-transmit). Icom = CI-V 1A 05 (datanum_hi, datanum_lo, value); Yaesu = a CAT EX
    # string. 3 min, never longer: the 3-minute limit for a remotely commanded station whose
    # control link fails (47 CFR 97.213(b)); also Icom's shortest non-OFF step. The app's 120 s
    # PTT failsafe stays the precise limit of a voice over; one SSTV picture may be held up to
    # 3 s inside the TOT (radio.ptt_hold_max, from `tot_s`). See docs/ADDING-A-RADIO.md.
    tot_civ: tuple = ()            # Icom: (0x00, datanum_lo, value); () = none
    tot_cat: str = ""              # Yaesu: full CAT EX string; "" = none
    tot_s: int = 0                 # the time-out that command sets, in seconds; 0 = none (no long keying)
    # CW transmit: how this radio sends a typed CW message. Operator-triggered — one
    # bounded message per TX press, fully under the operator's control, and the RIG
    # generates the CW at the keyer speed we set (cleaner than host-timed keying; the
    # Icom carrier can't be keyed by PTT in CW mode anyway). "" = unsupported;
    # "civ17" = Icom Send-CW-message (CI-V 17 + semi BK-IN + 14 0C keyer speed);
    # "line"  = host-timed keying of a serial control line (Yaesu FT-991A: PC KEYING =
    #           RTS/DTR; the FT-991A has NO arbitrary-text CW CAT command, so this is the
    #           same mechanism N1MM / fldigi / cwdaemon use). See docs/ADDING-A-RADIO.md.
    cw_send: str = ""
    cw_line: str = ""             # for cw_send="line": control line to key ("rts" | "dtr")
    # Yaesu safety: force PC KEYING = OFF on connect when we are NOT managing line keying, so a
    # stray control line can never key the rig. Full CAT EX string incl. the radio's own
    # PC-KEYING menu number/width (FT-991A menu 060 -> "EX0600;"; FT-891 menu 07-12 -> "EX07120;").
    # "" = the radio has no such menu / nothing to disarm.
    pc_keying_off_cat: str = ""
    # Yaesu ATAS (Active Tuning Antenna System) support: the CAT read command for the
    # TUNER SELECT menu item (FT-991A = "EX143;"; value 3 = ATAS). "" = no ATAS support.
    # When the radio reports ATAS selected, the UI shows manual antenna up/down controls
    # (mic UP/DN emulation) next to PTT. See docs/ADDING-A-RADIO.md.
    atas_read: str = ""
    # CW on the AUDIO scope (radios with no scope): in CW the radio shows the frequency it hears at
    # its CW pitch (the "zero beat"), so the audio spectrum is offset by that pitch to read real
    # frequencies, and the dial is drawn as the tuned marker. See docs/ADDING-A-RADIO.md.
    #   cw_sides     CW mode name -> +1 when a HIGHER signal sounds a higher tone (carrier on the USB
    #                side), -1 for the LSB side. A mode not listed here is not treated as CW.
    #   cw_pitch_hz  the radio's FIXED CW receive offset; 0 = the radio reports its pitch (Yaesu KP).
    #   cw_bfo_read  Yaesu CAT read of a CW BFO menu that decides the side (FT-891 07-07:
    #                0 USB / 1 LSB / 2 AUTO = LSB on 7 MHz and below, USB on 10 MHz and up).
    #   cw_disp_read Yaesu CAT read of the CW FREQ DISPLAY menu (1 = the dial shows the zero beat,
    #                the factory default; 0 = the dial reads as on SSB, no pitch offset).
    cw_sides: dict = field(default_factory=dict)
    cw_pitch_hz: int = 0
    cw_bfo_read: str = ""
    cw_disp_read: str = ""
    make: str = "Icom"            # manufacturer, shown before the model in the picker
    protocol: str = "civ"         # "civ" (Icom CI-V) | "yaesu" (Yaesu CAT)
    # Old/limited CI-V rigs (IC-728, ~1990) speak ONLY the classic command set 0x00-0x0E:
    # freq/mode/VFO/memory and nothing else — no TX-keying, meters, levels, functions, menu,
    # RIT, tone or scope command exists. civ_legacy=True makes the handler (a) drop any frame
    # whose command byte is > 0x0E on the wire, (b) use a minimal connect/poll (just 03/04), and
    # (c) send mode-only 06 (no filter byte, except CW narrow). See docs/ADDING-A-RADIO.md.
    civ_legacy: bool = False
    has_scope: bool = True        # False = no spectrum/waterfall over the control link
    has_network: bool = True      # False = COM-only (no RS-BA1/LAN) -> hide the LAN option
    default_baud: int = 115200    # default serial baud for the connection bar
    # connect_help: radio-side settings to set before connecting; rendered in the
    # "?" popover. [{"title": str, "items": [str, ...]}, ...]
    connect_help: list = field(default_factory=list)
    preamp_labels: list = field(default_factory=lambda: ["OFF", "P.AMP"])  # preamp states (index = code)
    # Receive coverage in Hz, [(lo, hi), ...], from the manual's specifications. A frequency typed into
    # the VFO is tuned only inside it, and a bare number counts as MHz, else kHz, else Hz, whichever
    # lands inside (7074 -> 7.074 MHz; 1296 on the IC-9700 -> 1296 MHz). [] = the bands' own edges.
    rx_ranges: list = field(default_factory=list)
    # Where a TX meter's raw 0..255 reading means something, per meter key, from the radio's CI-V
    # reference: {"PO": {"unit": "%", "pts": [[0, 0], [143, 50], [213, 100]]}, ...}; optional "warn" /
    # "bad" = the raw reading where the bar turns amber / red. The VFO's meter puts these values on its
    # TX scale and in its readout. A meter with no marks shows its raw reading on an unmarked bar (the
    # Yaesu CAT references give only the 0..255 range). See docs/ADDING-A-RADIO.md.
    meter_marks: dict = field(default_factory=dict)
    # Yaesu WIDTH (SH, the DSP IF bandwidth code 00-21, the same table on both radios): the FT-991A's is
    # SH0 P2P2; the FT-891's carries the WIDTH function's on/off first, SH0 P2 P3P3 (CAT manual p.16): ON
    # with the code, OFF = the mode's default bandwidth (code 00). True = the FT-891 form.
    sh_onoff: bool = False
    # v2 declarative blocks — synthesized from the flat flags above when not given,
    # so the existing profiles keep working unchanged (see __post_init__).
    transports: Optional["Transports"] = None
    capabilities: Optional["Capabilities"] = None
    menu: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.capabilities is None:
            self.capabilities = Capabilities(
                preamp=self.has_preamp, att=self.has_att, tuner=self.has_tuner,
                dual_watch=self.dual_watch,
                vfo_select=self.protocol != "yaesu",   # FT-991A has no CAT active-VFO selector
                vfo_swap=True,
                split=True, rit=True, duplex=True,
                rx_dsp=["nb", "nr", "anotch", "mnotch"],
                tx_funcs=["comp", "vox", "mon"], tbw=True,
                meters=["S", "PO", "SWR", "ALC", "COMP", "Vd", "Id"],
                cw_tx=bool(self.cw_send), menus=bool(self.menu),
                narrow=self.protocol == "yaesu",
                fm_tone=self.protocol == "yaesu",
                ext_ops=self.protocol == "yaesu",
            )
        elif self.menu and not self.capabilities.menus:
            self.capabilities.menus = True
        if self.transports is None:
            self.transports = Transports(
                serial=SerialCfg(baud=self.default_baud,
                                 stopbits=2 if self.protocol == "yaesu" else 1),
                network=NetworkCfg(enabled=self.has_network),
                audio=AudioCfg(kind="lan" if self.has_network else "usb-codec"),
                scope=ScopeCfg(kind="native" if self.has_scope else "audio"),
            )

    def band_default(self, name: str) -> Optional[int]:
        for b in self.bands:
            if b.name == name:
                return b.default
        return None

    def to_json(self) -> dict:
        return {
            "id": self.id, "name": self.name,
            "make": self.make, "protocol": self.protocol, "has_scope": self.has_scope,
            "has_network": self.has_network,
            "default_baud": self.default_baud, "connect_help": self.connect_help,
            "modes": self.modes,
            "bands": [{"name": b.name, "lo": b.lo, "hi": b.hi, "def": b.default} for b in self.bands],
            "steps": [{"v": v, "label": lbl} for v, lbl in self.steps],
            "default_step": self.default_step,
            "default_freq": self.default_freq,
            "dual_watch": self.dual_watch,
            "has_preamp": self.has_preamp,
            "has_att": self.has_att,
            "has_tuner": self.has_tuner,
            "preamp_labels": self.preamp_labels,
            "rx_ranges": [[int(lo), int(hi)] for lo, hi in self.rx_ranges],
            "meter_marks": self.meter_marks,
            "has_cw_tx": bool(self.cw_send),
            "cw_sides": self.cw_sides, "cw_pitch_hz": self.cw_pitch_hz,
            "transports": asdict(self.transports) if self.transports else None,
            "capabilities": asdict(self.capabilities) if self.capabilities else None,
            "menu": [asdict(m) for m in self.menu],
            "has_menu": bool(self.menu),
        }


# Icom meter readings (CI-V 15 11..16), as the IC-9700 and IC-7300MK2 CI-V references give them; COMP and Id
# differ between the two (their own tables below).
_ICOM_METERS = {
    "PO":  {"unit": "%", "pts": [[0, 0], [143, 50], [213, 100]]},
    "SWR": {"unit": "", "pts": [[0, 1.0], [48, 1.5], [80, 2.0], [120, 3.0]], "warn": 48, "bad": 80},
    "ALC": {"unit": "%", "pts": [[0, 0], [120, 100]], "bad": 120},    # 0..120 = the ALC zone (100 %)
    "Vd":  {"unit": "V", "pts": [[0, 0], [13, 10], [241, 16]]},
}

_SSB = {1: 3000, 2: 2400, 3: 1800}
_CW = {1: 1200, 2: 500, 3: 250}
_RTTY = {1: 2400, 2: 500, 3: 250}

from .menus.ic9700_menu import IC9700_MENU  # noqa: E402  (after MenuItem is defined above)

IC9700 = RadioProfile(
    id="ic9700", name="IC-9700", civ_addr=0xA2,
    # receive = transmit, USA version (Basic Manual, specifications); the EUR version is narrower
    rx_ranges=[(144_000_000, 148_000_000), (430_000_000, 450_000_000), (1_240_000_000, 1_300_000_000)],
    meter_marks=dict(_ICOM_METERS, COMP={"unit": "dB", "pts": [[0, 0], [130, 15], [210, 25.5]]},
                     Id={"unit": "A", "pts": [[0, 0], [121, 10], [241, 20]]}),
    modes=["LSB", "USB", "CW", "CW-R", "AM", "FM", "RTTY", "DV"],
    bands=[
        Band("144", 144_000_000, 148_000_000, 144_200_000),
        Band("430", 430_000_000, 450_000_000, 432_100_000),
        Band("1200", 1_240_000_000, 1_300_000_000, 1_296_100_000),
    ],
    filter_bw={
        "LSB": _SSB, "USB": _SSB, "CW": _CW, "CW-R": _CW,
        "RTTY": _RTTY, "RTTY-R": _RTTY,
        "AM": {1: 9000, 2: 6000, 3: 3000}, "FM": {1: 15000, 2: 7000, 3: 7000},
        "DV": {1: 6250, 2: 6250, 3: 6250}, "DD": {1: 130000, 2: 130000, 3: 130000},
    },
    mod_dataoff=(0x01, 0x15), lan_mod_level=(0x01, 0x14),
    tot_civ=(0x00, 0x41, 0x01), tot_s=180,   # TX TOT 0041 = 3 min (the app's 120 s failsafe is a voice over's limit)
    cw_send="civ17",                # CW message TX via CI-V 17 (semi BK-IN keys it)
    default_freq=144_200_000,
    steps=[(10, "10 Hz"), (100, "100 Hz"), (1000, "1 kHz"), (5000, "5 kHz"),
           (10000, "10 kHz"), (12500, "12.5 kHz"), (25000, "25 kHz")],
    default_step=25000,
    dual_watch=True,                # MAIN + SUB receivers
    menu=IC9700_MENU,
    connect_help=[
        {"title": "USB (CI-V)", "items": [
            "Install Icom's USB driver, then connect [USB] to the PC and pick its COM port above.",
            "MENU > SET > Connectors > CI-V:",
            "CI-V USB Baud Rate = Auto (default) — or 115200 to match the Baud box",
            "CI-V Address = A2h (default)",
            "CI-V Transceive = ON",
            "CI-V USB Echo Back = OFF",
        ]},
        {"title": "Network (LAN / RS-BA1)", "items": [
            "Connect [LAN] to your network. MENU > SET > Network > Network Control = ON.",
            "Set a Network User1 ID + Password (8-16 chars, not all the same); note the radio's IP.",
            "Control port (UDP) = 50001 (default). Restart the radio after network changes.",
            "Enter the IP, port 50001 and the user/password above.",
        ]},
    ],
)
IC9700.capabilities.data_mode = True        # DATA mode with filter: CI-V 1A 06 (CI-V guide p. 18)
# FM repeater tone: TONE / TSQL on/off (CI-V 16 42 / 16 43) and the tone (1B 00 / 01), which radio.py
# polls and sets, so the FM panel shows in FM. The radio has DTCS too (16 4B, 1B 02), but TeleRig drives
# none (radio.set_dcs_code does nothing): no DCS in the panel or in a memory.
IC9700.capabilities.fm_tone = True
IC9700.capabilities.fm_dcs = False
# The duplex offset: CI-V 0D (CI-V guide p. 13-14), 3-byte BCD with a 100 Hz LSB (civ.offset_to_bcd);
# the radio takes the 10 MHz digit on its 1.2 GHz band only.
IC9700.capabilities.duplex_offset = True

from .menus.ic7300mk2_menu import IC7300MK2_MENU  # noqa: E402  (after MenuItem is defined above)

IC7300MK2 = RadioProfile(
    id="ic7300mk2", name="IC-7300MK2", civ_addr=0xB6,
    rx_ranges=[(30_000, 74_800_000)],                      # Basic Manual, specifications
    meter_marks=dict(_ICOM_METERS, COMP={"unit": "dB", "pts": [[0, 0], [130, 15], [210, 30]]},
                     Id={"unit": "A", "pts": [[0, 0], [97, 10], [146, 15], [241, 25]]}),
    modes=["LSB", "USB", "CW", "CW-R", "RTTY", "RTTY-R", "AM", "FM"],
    bands=[
        Band("160", 1_800_000, 2_000_000, 1_840_000),
        Band("80", 3_500_000, 4_000_000, 3_700_000),
        Band("60", 5_300_000, 5_410_000, 5_357_000),
        Band("40", 7_000_000, 7_300_000, 7_150_000),
        Band("30", 10_100_000, 10_150_000, 10_130_000),
        Band("20", 14_000_000, 14_350_000, 14_200_000),
        Band("17", 18_068_000, 18_168_000, 18_130_000),
        Band("15", 21_000_000, 21_450_000, 21_300_000),
        Band("12", 24_890_000, 24_990_000, 24_950_000),
        Band("10", 28_000_000, 29_700_000, 28_400_000),
        Band("6", 50_000_000, 54_000_000, 50_150_000),
    ],
    filter_bw={
        "LSB": _SSB, "USB": _SSB, "CW": _CW, "CW-R": _CW,
        "RTTY": _RTTY, "RTTY-R": _RTTY,
        "AM": {1: 9000, 2: 6000, 3: 3000}, "FM": {1: 15000, 2: 10000, 3: 7000},
    },
    mod_dataoff=(0x00, 0x84), lan_mod_level=(0x00, 0x83),
    tot_civ=(0x00, 0x32, 0x01), tot_s=180,   # TX TOT 0032 = 3 min
    cw_send="civ17",                # CW message TX via CI-V 17 (semi BK-IN keys it)
    default_freq=14_074_000,
    steps=[(1, "1 Hz"), (10, "10 Hz"), (100, "100 Hz"), (1000, "1 kHz"),
           (5000, "5 kHz"), (9000, "9 kHz"), (10000, "10 kHz")],
    default_step=100,
    has_tuner=True,                               # IC-7300MK2 has an internal ATU (1C 01)
    preamp_labels=["OFF", "P.AMP1", "P.AMP2"],    # 16 02: two preamp stages
    capabilities=Capabilities(
        preamp=True, att=True, tuner=True, dual_watch=False,
        vfo_select=True,                          # Icom CI-V has an active-VFO selector
        vfo_swap=True, split=True, rit=True, duplex=True,
        rx_dsp=["nb", "nr", "anotch", "mnotch"],
        tx_funcs=["comp", "vox", "mon"], tbw=True,
        meters=["S", "PO", "SWR", "ALC", "COMP", "Vd", "Id"],
        cw_tx=True, menus=True,
        fm_tone=True, fm_dcs=False,               # FM repeater tone/TSQL (no DTCS over CI-V)
        icom_cw=True,                             # APF / break-in / CW pitch+speed / filter shape
        data_mode=True,                           # DATA mode with filter: CI-V 1A 06 (CI-V guide p. 22)
    ),
    menu=IC7300MK2_MENU,
    connect_help=[
        {"title": "USB (CI-V)", "items": [
            "Install Icom's USB driver, then connect [USB] to the PC and pick its COM port above.",
            "MENU > SET > Connectors > CI-V:",
            "Set CI-V USB Port to 'Unlink from [REMOTE]' first — while linked, USB baud is capped at 19200.",
            "CI-V USB Baud Rate = 115200 (match the Baud box)",
            "CI-V Address = B6h (default)",
            "CI-V Transceive = ON",
            "CI-V USB Echo Back = OFF",
        ]},
        {"title": "Network (LAN / RS-BA1)", "items": [
            "Connect [LAN] to your network. MENU > SET > Network > Network Control = ON.",
            "Set a Network User1 ID + Password (8-16 chars, not all the same); note the radio's IP.",
            "Control port (UDP) = 50001 (default). Restart the radio after network changes.",
            "Enter the IP, port 50001 and the user/password above.",
        ]},
    ],
)

from .menus.ft991a_menu import FT991A_MENU  # noqa: E402  (after MenuItem is defined above)

# Yaesu FT-991A — all-mode HF/50/144/430 MHz. Yaesu CAT (serial), COM-only: no
# Ethernet, and NO band scope over CAT (display-only scope), so no waterfall.
# civ_addr / mod_* are unused by the Yaesu path but the dataclass requires them.
FT991A = RadioProfile(
    id="ft991a", name="FT-991A", civ_addr=0x00,
    # Operating Manual, specifications (operating range); RM meter readings are 0..255 only: no marks
    rx_ranges=[(30_000, 56_000_000), (118_000_000, 164_000_000), (420_000_000, 470_000_000)],
    make="Yaesu", protocol="yaesu", has_scope=False, default_baud=38400,
    # mode buttons match the radio's own labels + order (CW-USB=MD3, CW-LSB=MD7,
    # RTTY-LSB=MD6, RTTY-USB=MD9, DATA-LSB=MD8, DATA-USB=MDC, DATA-FM=MDA, C4FM=MDE).
    modes=["LSB", "USB", "AM", "CW-LSB", "CW-USB", "FM", "RTTY-LSB", "RTTY-USB",
           "C4FM", "DATA-LSB", "DATA-USB", "DATA-FM"],
    bands=[
        Band("160", 1_800_000, 2_000_000, 1_840_000),
        Band("80", 3_500_000, 4_000_000, 3_700_000),
        Band("60", 5_330_500, 5_403_500, 5_330_500),
        Band("40", 7_000_000, 7_300_000, 7_074_000),
        Band("30", 10_100_000, 10_150_000, 10_136_000),
        Band("20", 14_000_000, 14_350_000, 14_074_000),
        Band("17", 18_068_000, 18_168_000, 18_100_000),
        Band("15", 21_000_000, 21_450_000, 21_074_000),
        Band("12", 24_890_000, 24_990_000, 24_915_000),
        Band("10", 28_000_000, 29_700_000, 28_074_000),
        Band("6", 50_000_000, 54_000_000, 50_313_000),
        Band("2", 144_000_000, 148_000_000, 144_200_000),
        Band("70", 430_000_000, 450_000_000, 432_100_000),
    ],
    filter_bw={
        "LSB": _SSB, "USB": _SSB, "CW-USB": _CW, "CW-LSB": _CW,
        "RTTY-LSB": _RTTY, "RTTY-USB": _RTTY, "DATA-LSB": _SSB, "DATA-USB": _SSB,
        "AM": {1: 9000, 2: 6000, 3: 3000}, "FM": {1: 16000, 2: 9000, 3: 9000},
        "DATA-FM": {1: 16000, 2: 9000, 3: 9000}, "C4FM": {1: 16000, 2: 16000, 3: 16000},
    },
    mod_dataoff=(0x00, 0x00), lan_mod_level=(0x00, 0x00),
    tot_cat="EX03603;", tot_s=180,  # menu 036 TX TOT = 3 min = 180 s
    # CW TX: host-timed DTR keying on the SECOND (Standard) USB port — the proven method
    # (N1MM/fldigi/cwdaemon). CAT runs on the Enhanced port; on connect the app sets
    # menu 033 CAT RTS=DISABLE + menu 060 PC KEYING=DTR and opens the sibling port DTR-low.
    # RTS keying on the CAT port is forbidden (collides with CAT RTS -> breaks PTT).
    cw_send="line", cw_line="dtr",
    atas_read="EX143;",             # TUNER SELECT (0 OFF/1 INT/2 EXT/3 ATAS/4 LAMP) -> ATAS = 3
    cw_sides={"CW-USB": 1, "CW-LSB": -1},   # CW-U (MD3) / CW-L (MD7): the mode is the side
    cw_disp_read="EX059;",          # 059 CW FREQ DISPLAY: 0 DIRECT FREQ / 1 PITCH OFFSET (default)
    default_freq=14_074_000,
    steps=[(10, "10 Hz"), (100, "100 Hz"), (1000, "1 kHz"), (2500, "2.5 kHz"),
           (5000, "5 kHz"), (10000, "10 kHz"), (25000, "25 kHz")],
    default_step=100,
    # FT-991A has IPO/AMP1 (PA0), a 12 dB RF ATT (RA0), and an internal auto ATU (AC).
    has_preamp=True, has_att=True, has_tuner=True, has_network=False,
    preamp_labels=["IPO", "AMP1", "AMP2"],
    menu=FT991A_MENU,
    connect_help=[
        {"title": "USB CAT (COM only)", "items": [
            "Install the Yaesu USB driver first. The radio shows TWO COM ports — pick the Enhanced (CAT) port above, not Standard.",
            "MENU EX-031 (CAT RATE) = 38400 to match the Baud box (the factory default is lower — change it, or set the Baud box to your radio's rate).",
            "Serial format is 8 data / no parity / 2 stop bits (8N2) — handled for you.",
            "No network: the FT-991A is COM-only.",
        ]},
    ],
)

from .menus.ft891_menu import FT891_MENU  # noqa: E402  (after MenuItem is defined above)

# Yaesu FT-891 — HF/50 MHz all-mode mobile. Yaesu CAT (serial), COM-only: no Ethernet and
# NO band scope over CAT, so the app shows an audio (AF) scope. Typically reached over a
# Digirig (one USB serial port for CAT + a separate USB sound card for RX/TX audio), so the
# radio audio rides the host sound-card path (v0.2.16). No internal ATU (menu 16-15 TUNER
# SELECT is external/ATAS only), a 2-state preamp (IPO/AMP), a single 12 dB ATT, 5-100 W. The
# SET menu is grouped (GG-NN) and 4-digit over CAT (EXGGNN) -> the table's ex_width=4.
# civ_addr / mod_* are unused by the Yaesu path but the dataclass requires them.
FT891 = RadioProfile(
    id="ft891", name="FT-891", civ_addr=0x00,
    rx_ranges=[(30_000, 56_000_000)],                      # CAT manual, FA / FB: 000030000 - 056000000 Hz
    sh_onoff=True,                                         # WIDTH = SH0 <on/off> <code>; (CAT manual p.16)
    make="Yaesu", protocol="yaesu", has_scope=False, has_network=False, default_baud=38400,
    # radio-accurate mode labels (no C4FM / DATA-FM on the FT-891)
    modes=["LSB", "USB", "AM", "CW-LSB", "CW-USB", "FM", "RTTY-LSB", "RTTY-USB",
           "DATA-LSB", "DATA-USB"],
    bands=[
        Band("160", 1_800_000, 2_000_000, 1_840_000),
        Band("80", 3_500_000, 4_000_000, 3_700_000),
        Band("60", 5_330_500, 5_403_500, 5_330_500),
        Band("40", 7_000_000, 7_300_000, 7_074_000),
        Band("30", 10_100_000, 10_150_000, 10_136_000),
        Band("20", 14_000_000, 14_350_000, 14_074_000),
        Band("17", 18_068_000, 18_168_000, 18_100_000),
        Band("15", 21_000_000, 21_450_000, 21_074_000),
        Band("12", 24_890_000, 24_990_000, 24_915_000),
        Band("10", 28_000_000, 29_700_000, 28_074_000),
        Band("6", 50_000_000, 54_000_000, 50_313_000),
    ],
    filter_bw={
        "LSB": _SSB, "USB": _SSB, "CW-USB": _CW, "CW-LSB": _CW,
        "RTTY-LSB": _RTTY, "RTTY-USB": _RTTY, "DATA-LSB": _SSB, "DATA-USB": _SSB,
        "AM": {1: 9000, 2: 6000, 3: 3000}, "FM": {1: 16000, 2: 9000, 3: 9000},
    },
    mod_dataoff=(0x00, 0x00), lan_mod_level=(0x00, 0x00),
    tot_cat="EX051403;", tot_s=180, # menu 05-14 TX TOT = 03 min = 180 s
    pc_keying_off_cat="EX07120;",   # menu 07-12 PC KEYING = OFF on connect (safety default)
    cw_send="",                     # no CAT text-CW yet: FT-891 KY only plays stored KM memories
    # CW: MD3 and MD7 are both plain "CW (CW BFO)" on the FT-891 (CAT manual, MD) — the side is
    # menu 07-07, read below; the mode name is only the fallback until that read answers.
    cw_sides={"CW-USB": 1, "CW-LSB": -1},
    cw_bfo_read="EX0707;",          # 07-07 CW BFO: 0 USB (default) / 1 LSB / 2 AUTO
    cw_disp_read="EX0711;",         # 07-11 CW FREQ DISPLAY: 0 FREQ / 1 PITCH (default)
    default_freq=14_074_000,
    steps=[(10, "10 Hz"), (100, "100 Hz"), (1000, "1 kHz"), (2500, "2.5 kHz"),
           (5000, "5 kHz"), (10000, "10 kHz"), (25000, "25 kHz")],
    default_step=100,
    # FT-891: IPO/AMP preamp (PA0 0/1), single 12 dB RF ATT (RA0), no internal ATU.
    has_preamp=True, has_att=True, has_tuner=False,
    preamp_labels=["IPO", "AMP"],
    capabilities=Capabilities(
        preamp=True, att=True, tuner=False, dual_watch=False,
        vfo_select=False,          # Yaesu CAT has no active-VFO (A/B) selector
        vfo_swap=True, split=True, rit=True, duplex=True,
        rx_dsp=["nb", "nr", "anotch", "mnotch"],
        tx_funcs=["comp", "vox", "mon"], tbw=True,
        meters=["S", "PO", "SWR", "ALC", "COMP", "Id"],   # FT-891 RM: 7=ID is the top; no VDD (8)
        cw_tx=False, menus=True,
        narrow=True, fm_tone=True, ext_ops=True,
    ),
    menu=FT891_MENU,
    connect_help=[
        {"title": "USB CAT via Digirig (COM only)", "items": [
            "Install the Digirig / CH340 USB-serial driver, then pick the Digirig's COM port above.",
            "MENU 05-06 [CAT RATE] = 38400 bps to match the Baud box (factory default is 4800 — raise it, or set the Baud box to match your radio).",
            "MENU 05-08 [CAT RTS] to suit your Digirig cable (DISABLE if RTS on that port keys PTT).",
            "Serial format is 8 data / no parity / 2 stop bits (8N2) — handled for you.",
            "Audio: the radio's RX/TX audio is on the Digirig's USB sound card — pick it as Radio RX / Radio TX in the audio selector (Mic In stays your computer mic).",
            "No network: the FT-891 is COM-only.",
        ]},
    ],
)

# Icom IC-728 — HF (160-10 m) all-band-ish transceiver, ~1990. The SIMPLEST radio here:
# CI-V is the classic command set 0x00-0x0E only (freq/mode/VFO/memory). There is NO CI-V
# transmit/keying, NO meter read, NO level/function/menu/RIT/tone/scope command — so the app
# physically cannot key or meter it, and the transmit-safety contract is N/A (nothing to key).
# COM-only through a CT-17 CI-V level converter; default 1200 baud (300/1200/9600 by an internal
# diode matrix — must match). No LAN, no band scope -> the app shows the audio (AF) FFT scope from
# a host sound card, same path as the FT-891. AM-TX and FM need the optional UI-7 unit; base is
# SSB/CW. CW-NAR (CW-Narrow, from the front-panel [CW/N] switch) = mode 03 + width 02 (needs the
# FL-100/101 crystal filter fitted) — NOT CW-reverse, which this radio doesn't have. Address 38h
# (IC-729 = 3Ah + 6 m). civ_addr / mod_* required by the dataclass; mod_* unused (no LAN).
IC728 = RadioProfile(
    id="ic728", name="IC-728", civ_addr=0x38,
    rx_ranges=[(500_000, 30_000_000)],                     # general-coverage receive 0.5-30 MHz (connect_help)
    make="Icom", protocol="civ",
    modes=["LSB", "USB", "CW", "CW-NAR", "AM", "FM"],
    bands=[
        Band("160", 1_800_000,  2_000_000,  1_900_000),
        Band("80",  3_500_000,  4_000_000,  3_700_000),
        Band("40",  7_000_000,  7_300_000,  7_100_000),
        Band("30", 10_100_000, 10_150_000, 10_120_000),
        Band("20", 14_000_000, 14_350_000, 14_200_000),
        Band("17", 18_068_000, 18_168_000, 18_130_000),
        Band("15", 21_000_000, 21_450_000, 21_300_000),
        Band("12", 24_890_000, 24_990_000, 24_950_000),
        Band("10", 28_000_000, 29_700_000, 28_400_000),
    ],
    # No CI-V filter-slot selection — one bandwidth per mode (hint for the scope span box).
    # CW-NAR assumes the FL-100 (500 Hz); an FL-101 would be 250 Hz.
    filter_bw={
        "LSB": {1: 2400}, "USB": {1: 2400}, "CW": {1: 2400}, "CW-NAR": {1: 500},
        "AM": {1: 6000}, "FM": {1: 12000},
    },
    mod_dataoff=(0x00, 0x00), lan_mod_level=(0x00, 0x00),   # unused: no LAN MOD routing
    # CW receives 800 Hz from the TX carrier (manual p.39 BFO table: CW transmit 9.01060 MHz, CW
    # receive 9.00980 MHz) through an inverted 9 MHz IF (USB BFO 9.0130 sits ABOVE LSB 9.0100 around
    # the one 9.0115 MHz filter), so a HIGHER signal sounds a LOWER tone: an LSB-side 800 Hz pitch.
    # The display is the TX (zero-beat) frequency.
    cw_sides={"CW": -1, "CW-NAR": -1}, cw_pitch_hz=800,
    default_freq=14_100_000,                                # power-on default 14.100 MHz USB
    steps=[(10, "10 Hz"), (20, "20 Hz"), (50, "50 Hz"), (100, "100 Hz"),
           (1_000, "1 kHz"), (10_000, "10 kHz"), (100_000, "100 kHz"), (1_000_000, "1 MHz")],
    default_step=1_000,
    civ_legacy=True,                                        # only CI-V 0x00-0x0E exist
    has_scope=False, has_network=False, default_baud=1200,
    has_preamp=False, has_att=False, has_tuner=False,       # no CI-V command for any of these
    capabilities=Capabilities(
        preamp=False, att=False, tuner=False, dual_watch=False,
        vfo_select=True, vfo_swap=False,        # 07 00/01 select VFO A/B; no CI-V A=B / exchange
        split=False, rit=False, duplex=False,   # no 0F / 21 / 0C-0D command on this radio
        rx_dsp=[], tx_funcs=[], tbw=False, meters=[],   # no 16 / 14 / 15 command -> no DSP/levels/meters
        cw_tx=False, menus=False,               # no CI-V keying, no 1A menu
        narrow=False, fm_tone=False, fm_dcs=False, ext_ops=False, icom_cw=False,
        ptt=False,                              # no CI-V transmit path at all
        tab_rx=False, tab_tx=False, tab_dsp=False,   # nothing in those tabs is CI-V-reachable
        # OPERATING (scan/fast) group is already hidden for non-ext_ops (Icom) rigs by syncExtPanel
        memory=True,                            # memory recall 08 / mem->VFO 0A (IC-735-class: yes)
    ),
    connect_help=[
        {"title": "Interface (CT-17 or a CI-V USB cable)", "items": [
            "Connect a CT-17 CI-V level converter (or a CI-V-to-USB cable) from the radio's",
            "REMOTE (CI-V) jack on the rear panel to a COM/USB serial port, and pick it above.",
            "This radio is COM-only — there is no network/LAN option.",
        ]},
        {"title": "CI-V address & baud", "items": [
            "Set the CI-V address to 38h (IC-728). The IC-729 uses 3Ah.",
            "Baud is fixed inside the radio by a diode matrix (300 / 1200 / 9600); factory default",
            "is 1200 bps. Set the Baud box above to the SAME value your radio is set to, 8N1.",
            "CI-V transceive / echo settings don't matter — the app actively polls freq + mode.",
        ]},
        {"title": "What this radio can do remotely", "items": [
            "Frequency, mode, band, VFO A/B and memory recall are controllable over CI-V.",
            "Metering, AF/RF/squelch, RIT, preamp/ATT and TRANSMIT are front-panel only on this",
            "radio — there is no CI-V command for them, so the app cannot key or meter it.",
            "General-coverage receive is 0.5-30 MHz; transmit is the HF ham bands (AM-TX and FM",
            "need the optional UI-7 unit). Turn on RX audio for the audio spectrum + CW decode.",
        ]},
    ],
)

PROFILES = {p.id: p for p in (IC9700, IC7300MK2, FT991A, FT891, IC728)}
DEFAULT_PROFILE_ID = "ic9700"
