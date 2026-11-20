"""The VFO panel's data from the server — run: python -m backend.vfo_meter_test

  1. every profile's receive coverage (rx_ranges, from its manual) is in /api/radios, sane, and holds every
     band the radio offers; the VFO's typed frequencies are checked against it (frontend/freqentry.js);
  2. the TX meters' marks (meter_marks): the Icom radios' are their CI-V references' points, exactly (PO,
     SWR, ALC, Vd the same on both; COMP and Id each their own); the Yaesu CAT references give only the
     0..255 range and the IC-728 has no meters, so theirs are empty; every point list rises;
  3. the meter the VFO shows on transmit: PO by default where the radio has it (S on the IC-728, which has
     no meters); the selected TX meter is polled only while transmitting (PO / SWR / ALC / COMP read 0 on
     receive), Vd and Id always (they mean something on receive), S never (it has its own read); both
     drivers' poll loops ask tx_meter_due.

Pure data and source checks: no radio, simulator, socket or file is opened.
"""
from pathlib import Path

from backend import profiles
from backend.radio import RX_METERS, fresh_state, tx_meter_due

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


P = profiles.PROFILES
J = {pid: p.to_json() for pid, p in P.items()}
MHZ = 1_000_000

# 1. receive coverage
C("the radios", sorted(P), ["ft891", "ft991a", "ic728", "ic7300mk2", "ic9700"])
C("IC-7300MK2 receives 0.03-74.8 MHz", J["ic7300mk2"]["rx_ranges"], [[30_000, 74_800_000]])
C("IC-9700 (USA version) receives 144-148, 430-450, 1240-1300 MHz", J["ic9700"]["rx_ranges"],
  [[144 * MHZ, 148 * MHZ], [430 * MHZ, 450 * MHZ], [1240 * MHZ, 1300 * MHZ]])
C("FT-991A receives 30 kHz-56 MHz, 118-164 MHz, 420-470 MHz", J["ft991a"]["rx_ranges"],
  [[30_000, 56 * MHZ], [118 * MHZ, 164 * MHZ], [420 * MHZ, 470 * MHZ]])
C("FT-891 receives 30 kHz-56 MHz", J["ft891"]["rx_ranges"], [[30_000, 56 * MHZ]])
C("IC-728 receives 0.5-30 MHz", J["ic728"]["rx_ranges"], [[500_000, 30 * MHZ]])
for pid, p in P.items():
    rr = J[pid]["rx_ranges"]
    C(f"{pid}: each range rises and they don't overlap",
      all(lo < hi for lo, hi in rr) and all(rr[i][1] < rr[i + 1][0] for i in range(len(rr) - 1)), True)
    outside = [b.name for b in p.bands if not any(lo <= b.lo and b.hi <= hi for lo, hi in rr)]
    C(f"{pid}: every band it offers is inside what it receives", outside, [])

# 2. the TX meters' marks
ICOM_SAME = {"PO": {"unit": "%", "pts": [[0, 0], [143, 50], [213, 100]]},
             "SWR": {"unit": "", "pts": [[0, 1.0], [48, 1.5], [80, 2.0], [120, 3.0]], "warn": 48, "bad": 80},
             "ALC": {"unit": "%", "pts": [[0, 0], [120, 100]], "bad": 120},
             "Vd": {"unit": "V", "pts": [[0, 0], [13, 10], [241, 16]]}}
C("IC-7300MK2 meter marks (its CI-V reference)", J["ic7300mk2"]["meter_marks"], dict(ICOM_SAME,
  COMP={"unit": "dB", "pts": [[0, 0], [130, 15], [210, 30]]}, Id={"unit": "A", "pts": [[0, 0], [97, 10], [146, 15], [241, 25]]}))
C("IC-9700 meter marks (its CI-V reference)", J["ic9700"]["meter_marks"], dict(ICOM_SAME,
  COMP={"unit": "dB", "pts": [[0, 0], [130, 15], [210, 25.5]]}, Id={"unit": "A", "pts": [[0, 0], [121, 10], [241, 20]]}))
for pid in ("ft991a", "ft891", "ic728"):
    C(f"{pid}: no meter marks (raw 0..255 only, or no meters)", J[pid]["meter_marks"], {})
for pid, j in J.items():
    for key, mk in j["meter_marks"].items():
        pts = mk["pts"]
        C(f"{pid} {key}: a meter the radio has, its points rising inside 0..255",
          (key in P[pid].capabilities.meters, all(0 <= a[0] < b[0] <= 255 and a[1] < b[1] for a, b in zip(pts, pts[1:]))), (True, True))

# 3. the TX meter shown and polled
for pid, p in P.items():
    C(f"{pid}: the TX meter at connect", fresh_state(p)["meter"], "PO" if "PO" in p.capabilities.meters else "S")
C("Vd and Id are the meters read on receive too", RX_METERS, ("Vd", "Id"))
due = {(k, ptt): tx_meter_due({"meter": k, "ptt": ptt}) for k in ("S", "PO", "SWR", "ALC", "COMP", "Vd", "Id") for ptt in (False, True)}
C("polled on receive: Vd and Id only", sorted(k for (k, ptt), v in due.items() if v and not ptt), ["Id", "Vd"])
C("polled on transmit: every TX meter, never S", sorted(k for (k, ptt), v in due.items() if v and ptt),
  ["ALC", "COMP", "Id", "PO", "SWR", "Vd"])
here = Path(__file__).resolve().parent
icom, yaesu = (here / "radio.py").read_text(encoding="utf-8"), (here / "yaesu.py").read_text(encoding="utf-8")
C("the Icom poll asks tx_meter_due before reading the TX meter",
  "if tx_meter_due(self.state):             # the selected TX meter, for the VFO's bar\n"
  "                    self._write(self._b(0x15, civ.METER_SUBS[self.state[\"meter\"]]))" in icom, True)
C("the Yaesu poll asks tx_meter_due before RM", "if mkey in YAESU_METER and tx_meter_due(self.state) and not self._poll_stop.is_set():" in yaesu, True)

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
