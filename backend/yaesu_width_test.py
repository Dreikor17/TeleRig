"""Yaesu WIDTH (SH, the DSP IF bandwidth) on each radio's own command — run: python -m backend.yaesu_width_test

The FT-991A's SH is SH0 P2P2; (the code 00-21). The FT-891's carries the WIDTH function's on/off first:
SH0 P2 P3P3; (its CAT manual, p.16): ON with the code, OFF = the mode's default bandwidth (code 00). Both
radios use the same code table. TeleRig used to send the FT-991A form to the FT-891, so its width did not
change (from the WIDTH keys or a spot) and its reading was taken from the wrong digits.

  1. the profiles: only the FT-891 has the on/off form (sh_onoff);
  2. set_width writes each radio's own form: FT-891 SH0114; for code 14 and SH0000; for the default,
     FT-991A SH014; / SH000;
  3. the answers: an FT-891 SH0114; is code 14, SH0010; (WIDTH off, a code left behind) the default 0, and
     an FT-991A-shaped SH014; on an FT-891 is not taken for a code; the FT-991A's SH017; is 17;
  4. the simulator speaks each form: it starts the FT-891 at WIDTH off, keeps what was set, answers it;
  5. a real YaesuRadio's set_width on the simulator reads back the code it set, on both radios.

No radio is connected and nothing keys: a recording stand-in transport and the built-in simulator, in
this process; no serial port, socket, sound card or file is touched.
"""
from backend import profiles
from backend.transport import YaesuSimTransport
from backend.yaesu import YaesuRadio

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


class Rec:
    """A transport that only records what is written."""
    def __init__(self):
        self.sent = []

    def write(self, data):
        self.sent.append(data.decode("ascii"))


P = profiles.PROFILES
C("only the FT-891 has the WIDTH on/off form", sorted(pid for pid, p in P.items() if p.sh_onoff), ["ft891"])


def radio(pid):
    r = YaesuRadio(P[pid])
    r._tp = Rec()
    return r


# 2. what set_width writes
for pid, cases in (("ft891", [(14, "SH0114;"), (0, "SH0000;"), (17, "SH0117;"), (25, "SH0121;")]),
                   ("ft991a", [(14, "SH014;"), (0, "SH000;"), (17, "SH017;"), (-3, "SH000;")])):
    r = radio(pid)
    for code, want in cases:
        r._tp.sent.clear()
        r.set_width(code)
        C(f"{pid}: set_width({code}) writes", (r._tp.sent, r.state["width"]), ([want], max(0, min(21, code))))

# 3. the answers
r = radio("ft891")
for ans, want in (("SH0114", 14), ("SH0010", 0), ("SH0100", 0), ("SH0121", 21)):
    r.state["width"] = -1
    r._parse(ans)
    C(f"ft891: answer {ans}; reads", r.state["width"], want)
r.state["width"] = 7
r._parse("SH014")                                  # the FT-991A form: too short for the FT-891's
C("ft891: an FT-991A-shaped answer is not taken for a code", r.state["width"], 7)
r = radio("ft991a")
for ans, want in (("SH017", 17), ("SH000", 0)):
    r.state["width"] = -1
    r._parse(ans)
    C(f"ft991a: answer {ans}; reads", r.state["width"], want)


# 4. the simulator speaks each form
def ask(sim, cmd):
    out = []
    sim._on_bytes = lambda b: out.append(b.decode("ascii"))
    sim.write(cmd.encode("ascii"))
    return "".join(out)


s891, s991 = YaesuSimTransport(P["ft891"]), YaesuSimTransport(P["ft991a"])
C("sim FT-891 starts with WIDTH off", ask(s891, "SH0;"), "SH0000;")
ask(s891, "SH0114;")
C("sim FT-891 keeps what was set", ask(s891, "SH0;"), "SH0114;")
C("sim FT-991A starts at the default", ask(s991, "SH0;"), "SH000;")
ask(s991, "SH017;")
C("sim FT-991A keeps what was set", ask(s991, "SH0;"), "SH017;")

# 5. the real driver on the simulator: what it sets, it reads back
for pid in ("ft891", "ft991a"):
    r, sim = YaesuRadio(P[pid]), YaesuSimTransport(P[pid])
    r._tp = sim
    sim._on_bytes = r._on_bytes
    r.set_width(16)
    r.state["width"] = -1
    r._send("SH0;")
    C(f"{pid}: set_width(16) on the simulator reads back", r.state["width"], 16)

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
