"""The simulator's band on the scope — run: python -m backend.sim_scope_test

transport.SimTransport._make_waveform draws a band's worth of stations (_SIM_SIGNALS) so the scope
and waterfall look like a real band with no radio:

  1. Morse keying in PARIS timing: dot 1.2/wpm s, dash 3 dots, gaps of 1 / 3 / 7 dots;
  2. the CW stations pulse (key down and up many times, at a believable duty cycle);
  3. the SSB voices talk in syllables with gaps, and pause between overs;
  4. a sweep: 475 points in 0..160, the same for the same time, a CW peak only while its key is down;
  5. an SSB voice is 300-2700 Hz wide above its dial at any span (many points at a narrow span, a
     point or two at the widest);
  6. nothing sweeps across the span (a sliding burst drew LoRa-like chirps on the waterfall).

Pure computation on a SimTransport that is never started: no thread, port or socket.
"""
import math

from backend import civ
from backend import transport as T

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


def R(x):
    return [(round(a, 3), round(b, 3)) for a, b in x]


st = T.SimTransport.__new__(T.SimTransport)
st._t0 = 0.0
N = civ.SCOPE_POINTS
CW = [s for s in T._SIM_SIGNALS if s["kind"] == "cw"]
SSB = [s for s in T._SIM_SIGNALS if s["kind"] == "ssb"]

# -- 1. Morse timing ---------------------------------------------------------------------------------
down, cycle = T._morse_keying("E T", 20, 1.0)
C("E then T at 20 wpm: a 60 ms dot, then a 180 ms dash 7 dots later", R(down), [(0.0, 0.06), (0.48, 0.66)])
C("... the cycle is the message, its last gap and the pause", round(cycle, 3), 1.72)
C("EE: the second E 3 dots after the first ends", R(T._morse_keying("EE", 20, 0)[0]), [(0.0, 0.06), (0.24, 0.3)])
C("A: the dash 1 dot after the dot", R(T._morse_keying("A", 20, 0)[0]), [(0.0, 0.06), (0.12, 0.3)])
C("PARIS is 43 dots (+1 trailing gap) at any speed", round(T._morse_keying("PARIS", 20, 0)[1] / 0.06), 44)
C("unknown characters send nothing", T._morse_keying("?!", 20, 0)[0], [])

# -- 2. the CW stations pulse -----------------------------------------------------------------------------
for s in CW:
    on = [T._cw_key_down(s, k * 0.02) for k in range(1000)]           # 20 s in 20 ms steps
    flips = sum(1 for i in range(1, len(on)) if on[i] != on[i - 1])
    duty = sum(on) / len(on)
    C(f"CW at {s['pos']}: keyed on and off many times in 20 s", flips >= 40, True)
    C(f"CW at {s['pos']}: a believable duty cycle", 0.15 <= duty <= 0.7, True)

# -- 3. the SSB voices talk and pause ---------------------------------------------------------------------
for s in SSB:
    talk = [T._ssb_voice(s, k * 0.05)[0] > 0 for k in range(2400)]     # 120 s in 50 ms steps
    frac = sum(talk) / len(talk)
    longest_quiet = cur = 0
    for v in talk:
        cur = 0 if v else cur + 1
        longest_quiet = max(longest_quiet, cur)
    gaps = sum(1 for i in range(1, len(talk)) if talk[i - 1] and not talk[i])
    C(f"SSB at {s['pos']}: talks part of the time", 0.2 <= frac <= 0.8, True)
    C(f"SSB at {s['pos']}: pauses between overs (>= 5 s of quiet)", longest_quiet * 0.05 >= 5.0, True)
    C(f"SSB at {s['pos']}: gaps between words while talking", gaps >= 30, True)
    st_ = [T._ssb_voice(s, 12.0 + k * 0.05) for k in range(40)]
    C(f"SSB at {s['pos']}: strength and formants stay in range",
      all(0 <= a <= 1 and 0 <= f1 <= 1 and 0 <= f2 <= 1 for a, f1, f2 in st_), True)

# -- 4. one sweep ---------------------------------------------------------------------------------------
w = st._make_waveform(500_000, t=7.25)
C("a sweep has SCOPE_POINTS points", len(w), N)
C("... all inside the documented 0..160 range", (min(w) >= 0, max(w) <= civ.SCOPE_MAX), (True, True))
C("... the same time gives the same sweep", st._make_waveform(500_000, t=7.25) == w, True)
s = CW[0]
x = int(round(s["pos"] * N))
t_down = next(k * 0.01 for k in range(3000) if T._cw_key_down(s, k * 0.01))
t_up = next(k * 0.01 for k in range(3000) if not T._cw_key_down(s, k * 0.01) and k * 0.01 > t_down)
peak_down = max(st._make_waveform(500_000, t=t_down)[x - 1:x + 2])
peak_up = max(st._make_waveform(500_000, t=t_up)[x - 1:x + 2])
C("the first CW station shows while its key is down", peak_down >= 100, True)
C("... and is gone (floor only) while it is up", peak_up <= 33, True)

# -- 5. the SSB width follows the span ---------------------------------------------------------------------
def voice_points(span, t):
    lo = int(0.5 * N + 300 / (span / N)) - 1
    hi = int(0.5 * N + 2700 / (span / N)) + 2
    sweep = st._make_waveform(span, t=t)
    return sum(1 for v in sweep[max(0, lo):hi] if v > 40)


t_talk = next(k * 0.05 for k in range(2400) if T._ssb_voice(SSB[0], k * 0.05)[0] > 0.8)
C("at a 50 kHz span the tuned SSB voice covers many points (2.4 kHz / 105 Hz)", voice_points(50_000, t_talk) >= 15, True)
C("at the widest span it is a point or a few", 1 <= voice_points(500_000, t_talk) <= 6, True)
left = st._make_waveform(50_000, t=t_talk)[int(0.5 * N) - 12:int(0.5 * N) - 1]
C("... and it sits ABOVE its dial (USB): nothing just below it", max(left) <= 33, True)

# -- 6. nothing sweeps across the span ---------------------------------------------------------------------
gap = range(int(0.43 * N), int(0.49 * N))            # between the 0.405 CW station and the tuned voice
worst = max(max(st._make_waveform(500_000, t=k * 0.1)[i] for i in gap) for k in range(200))
C("20 s of sweeps: the quiet stretch between stations stays at the noise floor", worst <= 33, True)

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
