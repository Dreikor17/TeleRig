"""rigctld protocol unit tests — run: python -m backend.rigctl_test

Pure functions only (no socket, no radio): request parsing, reply formats, the capability dump and
the mode tables. The expected replies are the ones the real rigctld 4.6.5 / 4.7.1 (WSJT-X's build) /
4.7.2 gave for Hamlib's dummy rig; tests/t_bridge.py drives the bridge with the real Hamlib client.
"""
from backend import profiles, rigctl as R

_ok = True
BS = chr(92)


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


def P(line):
    return [(c.name, c.args, c.ext, c.known, c.missing) for c in R.parse_line(line)]


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    return False


# ---- parsing ---------------------------------------------------------------------------------------
C("f", P("f\n"), [("get_freq", [], None, True, False)])
C("CRLF line end", P("f\r\n"), [("get_freq", [], None, True, False)])
C("F with the float the Hamlib client sends", P("F 14074000.000000\n"),
  [("set_freq", ["14074000.000000"], None, True, False)])
C("M mode passband", P("M PKTUSB -1"), [("set_mode", ["PKTUSB", "-1"], None, True, False)])
C("long command", P(BS + "get_powerstat"), [("get_powerstat", [], None, True, False)])
C("long command with an argument", P(BS + "get_vfo_info VFOA"), [("get_vfo_info", ["VFOA"], None, True, False)])
C("long set", P(BS + "set_freq 7074000"), [("set_freq", ["7074000"], None, True, False)])
C("several commands on one line", [c[0] for c in P("f m v")], ["get_freq", "get_mode", "get_vfo"])
C("short commands typed together", [c[0] for c in P("fmv")], ["get_freq", "get_mode", "get_vfo"])
C("a setter then a getter", [(c[0], c[1]) for c in P("F 14074000 f")],
  [("set_freq", ["14074000"]), ("get_freq", [])])
C("extended +", P("+f"), [("get_freq", [], "\n", True, False)])
C("extended with separators", [P(x + "f")[0][2] for x in (";", "|", ",")], [";", "|", ","])
C("extended long command", P("+" + BS + "get_powerstat"), [("get_powerstat", [], "\n", True, False)])
C("an extended command after a plain one", [(c[0], c[2]) for c in P("f ;m")], [("get_freq", None), ("get_mode", ";")])
C("missing argument", P("F"), [("set_freq", [], None, True, True)])
C("M with one argument is missing one", P("M USB")[0][4], True)
C("M ? asks for the list (complete)", P("M ?"), [("set_mode", ["?"], None, True, False)])
C("l ? asks for the list", P("l ?"), [("get_level", ["?"], None, True, False)])
C("unknown short command", P("bogus"), [("", [], None, False, False)])
C("unknown long command", P(BS + "nosuch"), [("", [], None, False, False)])
C("arguments glued to the letter are not rigctl", P("F14074000")[0][3], False)
C("nothing after an unknown command runs", [c[0] for c in P(BS + "nosuch T 1")], [""])
C("nothing after an unknown short token runs", [c[0] for c in P("% T 1")], [""])
C("send_morse takes the rest of the line", P("b CQ CQ DE TEST"), [("send_morse", ["CQ CQ DE TEST"], None, True, False)])
C("recognised but not handled", (P("w FA;")[0][0] in R.SPECS, P("w FA;")[0][0] in R.HANDLED), (True, False))
C("empty line", P("\n"), [])
C("blank line", P("   \r\n"), [])
C("control characters are refused", raises(lambda: R.parse_line("f\x00m"), R.ProtocolError), True)
C("escape characters are refused", raises(lambda: R.parse_line("\x1b[Af"), R.ProtocolError), True)
C("too many commands on one line", raises(lambda: R.parse_line("f " * (R.CMDS_PER_LINE + 1)), R.ProtocolError), True)
C("the maximum on one line is fine", len(R.parse_line("f " * R.CMDS_PER_LINE)), R.CMDS_PER_LINE)
C("T 1 parses as set_ptt", P("T 1"), [("set_ptt", ["1"], None, True, False)])
C("every handled command has a spec", sorted(R.HANDLED - set(R.SPECS)), [])
C("every short command's name has a spec", sorted({s.name for s in R.SHORT.values()} - set(R.SPECS)), [])

# ---- HTTP detection (a browser pointed at the port) ------------------------------------------------
for line in ("GET / HTTP/1.1", "POST /x HTTP/1.1\r\n", "OPTIONS * HTTP/1.1", "PRI * HTTP/2.0", "get /", "Host: x HTTP/1.0"):
    C(f"http: {line!r}", R.looks_like_http(line), True)
for line in ("f", "F 14074000", "T 1", BS + "dump_state", "G TUNE", "P APO 0", "+f", ""):
    C(f"not http: {line!r}", R.looks_like_http(line), False)

# ---- replies -----------------------------------------------------------------------------------------
f = R.parse_line("f")[0]
C("short getter", R.reply(f, ["14074000"]), "14074000\n")
C("short getter, two values", R.reply(R.parse_line("m")[0], ["USB", "2400"]), "USB\n2400\n")
C("short setter", R.reply(R.parse_line("F 1")[0], None, R.OK), "RPRT 0\n")
C("short error", R.reply(f, None, R.ERJCTED), "RPRT -9\n")
C("get_lock_mode carries RPRT 0 too", R.reply(R.parse_line(BS + "get_lock_mode")[0], ["0"], always_rprt=True), "0\nRPRT 0\n")
C("extended getter", R.reply(R.parse_line("+f")[0], ["14074000"]), "get_freq:\nFrequency: 14074000\nRPRT 0\n")
C("extended getter, two values", R.reply(R.parse_line("+m")[0], ["USB", "2400"]),
  "get_mode:\nMode: USB\nPassband: 2400\nRPRT 0\n")
C("extended with a separator", R.reply(R.parse_line(";f")[0], ["14074000"]), "get_freq:;Frequency: 14074000;RPRT 0\n")
C("extended setter repeats its arguments", R.reply(R.parse_line("+F 14074000")[0], None, R.OK),
  "set_freq: 14074000\nRPRT 0\n")
C("extended error", R.reply(R.parse_line("+T 1")[0], None, R.EACCESS), "set_ptt: 1\nRPRT -22\n")
C("extended level value has no label", R.reply(R.parse_line("+l STRENGTH")[0], ["-16"]),
  "get_level: STRENGTH\n-16\nRPRT 0\n")
C("extended split", R.reply(R.parse_line("+s")[0], ["0", "VFOA"]), "get_split_vfo:\nSplit: 0\nTX VFO: VFOA\nRPRT 0\n")
C("extended powerstat", R.reply(R.parse_line("+" + BS + "get_powerstat")[0], ["1"]),
  "get_powerstat:\nPower Status: 1\nRPRT 0\n")
C("parse_freq", [R.parse_freq(x) for x in ("14074000", "14074000.000000", "14074000.6", "abc", "nan", "inf", "")],
  [14074000, 14074000, 14074000, None, None, None, None])
C("parse_int", [R.parse_int(x) for x in ("1", " 3 ", "x", "1.0")], [1, 3, None, None])

# ---- modes -------------------------------------------------------------------------------------------
IC73, IC97, FT991, FT891, IC728 = (profiles.PROFILES[k] for k in ("ic7300mk2", "ic9700", "ft991a", "ft891", "ic728"))
C("Icom USB", R.to_hamlib_mode(IC73, {"mode_name": "USB", "data": 0}), "USB")
C("Icom USB + DATA = PKTUSB", R.to_hamlib_mode(IC73, {"mode_name": "USB", "data": 1}), "PKTUSB")
C("Icom LSB/FM/AM + DATA", [R.to_hamlib_mode(IC73, {"mode_name": m, "data": 1}) for m in ("LSB", "FM", "AM")],
  ["PKTLSB", "PKTFM", "PKTAM"])
C("Icom CW-R / RTTY-R", [R.to_hamlib_mode(IC73, {"mode_name": m}) for m in ("CW-R", "RTTY-R")], ["CWR", "RTTYR"])
C("Icom CW with a stale data flag is still CW", R.to_hamlib_mode(IC73, {"mode_name": "CW", "data": 1}), "CW")
C("IC-9700 DV = DSTAR", R.to_hamlib_mode(IC97, {"mode_name": "DV"}), "DSTAR")
C("IC-728 has no DATA switch: the flag is ignored", R.to_hamlib_mode(IC728, {"mode_name": "USB", "data": 1}), "USB")
C("IC-728 CW-NAR = CWN", R.to_hamlib_mode(IC728, {"mode_name": "CW-NAR"}), "CWN")
C("Yaesu labels", [R.to_hamlib_mode(FT991, {"mode_name": m}) for m in
                   ("USB", "CW-USB", "CW-LSB", "RTTY-LSB", "RTTY-USB", "DATA-USB", "DATA-LSB", "DATA-FM", "C4FM")],
  ["USB", "CW", "CWR", "RTTY", "RTTYR", "PKTUSB", "PKTLSB", "PKTFM", "C4FM"])
C("an unknown mode has no Hamlib name", R.to_hamlib_mode(IC73, {"mode_name": "?"}), "")
C("Icom PKTUSB -> USB + DATA on", R.from_hamlib_mode(IC73, "PKTUSB"), ("USB", True))
C("Icom USB -> USB + DATA off", R.from_hamlib_mode(IC73, "usb"), ("USB", False))
C("Icom CW -> no DATA variant asked (off)", R.from_hamlib_mode(IC73, "CW"), ("CW", False))
C("IC-9700 has no RTTY-R button", R.from_hamlib_mode(IC97, "RTTYR"), None)
C("IC-728: USB yes, PKTUSB no (no DATA switch)", (R.from_hamlib_mode(IC728, "USB"), R.from_hamlib_mode(IC728, "PKTUSB")),
  (("USB", None), None))
C("Yaesu PKTUSB -> DATA-USB", R.from_hamlib_mode(FT991, "PKTUSB"), ("DATA-USB", None))
C("FT-891 has no C4FM / DATA-FM", (R.from_hamlib_mode(FT891, "C4FM"), R.from_hamlib_mode(FT891, "PKTFM")), (None, None))
C("an unknown Hamlib mode", R.from_hamlib_mode(IC73, "NOSUCH"), None)
for prof in (IC73, IC97, FT991, FT891, IC728):
    lst = R.hamlib_modes(prof)
    back = [R.from_hamlib_mode(prof, m) for m in lst]
    C(f"{prof.id}: every listed Hamlib mode maps back to one of its modes",
      (None in back, all(b[0] in prof.modes for b in back if b), len(lst) == len(set(lst))), (False, True, True))
    C(f"{prof.id}: every profile mode has a Hamlib name",
      [m for m in prof.modes if not R.to_hamlib_mode(prof, {"mode_name": m, "data": 0})], [])
C("IC-7300MK2 mode list", R.hamlib_modes(IC73),
  ["LSB", "PKTLSB", "USB", "PKTUSB", "CW", "CWR", "RTTY", "RTTYR", "AM", "PKTAM", "FM", "PKTFM"])
C("mode mask has the bits of the list", R.mode_mask(IC728),
  R.MODE_BITS["LSB"] | R.MODE_BITS["USB"] | R.MODE_BITS["CW"] | R.MODE_BITS["CWN"] | R.MODE_BITS["AM"] | R.MODE_BITS["FM"])

# ---- S-meter ---------------------------------------------------------------------------------------
C("strength", [R.strength_db(v) for v in (0, 60, 120, 241, 999, -5, None, "x")], [-54, -27, 0, 60, 60, -54, -54, -54])

# ---- the capability dump --------------------------------------------------------------------------
d = R.dump_state(IC73, version="Hamlib 4.6.5 (TeleRig test bridge)", ptt=True)
L = d.split("\n")
C("dump: ends with done + newline", (L[-2], L[-1]), ("done", ""))
C("dump: protocol 1, the Hamlib model, ITU 0", L[:3], ["1", "3094", "0"])
C("dump: no empty line before done", [i for i, x in enumerate(L[:-1]) if x == ""], [])
rx_end = L.index("0 0 0 0 0 0 0")
C("dump: one RX range", rx_end, 4)
tx_end = L.index("0 0 0 0 0 0 0", rx_end + 1)
C("dump: one TX range per band", tx_end - rx_end - 1, len(IC73.bands))
C("dump: the RX range spans the bands", L[3].split()[:2], ["1800000.000000", "54000000.000000"])
C("dump: 7 fields per range line", {len(x.split()) for x in L[3:tx_end + 1]}, {7})
rest = L[tx_end + 1:]
C("dump: tuning steps, filters, rit/xit/ifshift, announces, preamp, att",
  rest[:10], [f"0x{R.mode_mask(IC73):x} 1", "0 0", "0 0", "0", "0", "0", "0", "0 ", "0 ", "0x0"])
C("dump: has_get_level = STRENGTH | RFPOWER", rest[11], f"0x{R.LEVEL_STRENGTH | R.LEVEL_RFPOWER:x}")
kv = dict(x.split("=", 1) for x in L if "=" in x)
C("dump: CAT PTT advertised", kv["ptt_type"], "0x1")
C("dump: key=value settings", [kv.get(k) for k in ("has_set_vfo", "has_get_vfo", "has_set_freq", "has_get_freq", "timeout", "rig_model")],
  ["1", "1", "1", "1", "0", "3094"])
C("dump: versions", (kv["rigctld_version"], kv["hamlib_version"]), ("Hamlib 4.6.5 (TeleRig test bridge)",) * 2)
C("dump: transmit off -> no CAT PTT", dict(x.split("=", 1) for x in R.dump_state(IC73, version="v", ptt=False).split("\n")
                                           if "=" in x)["ptt_type"], "0x0")
C("dump: a radio that cannot key -> no CAT PTT", dict(x.split("=", 1) for x in R.dump_state(IC728, version="v", ptt=True).split("\n")
                                                      if "=" in x)["ptt_type"], "0x0")
C("dump: models", [R.dump_state(p, version="v").split("\n")[1] for p in (IC97, FT991, FT891, IC728)],
  ["3081", "1035", "1036", "3016"])
C("dump: pure ASCII", d.isascii(), True)

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
