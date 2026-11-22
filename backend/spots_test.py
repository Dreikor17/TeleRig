"""The spots cache (backend/spots.py): the three sources' formats, the polling floors, failures.
run: python -m backend.spots_test

No network: `fetch` is a stub that answers from the samples below (made-up callsigns, shaped like each
source's real answer) and records every URL asked for.

  1. POTA / WWFF / HamQTH answers -> one shape: hz from kHz, CW / SSB / FM / AM / DIGI, UTC "...Z" times,
     park / area / country names, text trimmed, capped and kept to one line; junk rows skipped
  2. newest first across sources; spots older than 30 minutes left out; only the sources asked for
  3. never more often than each source allows (POTA / WWFF a minute, HamQTH 30 s), however many pages ask
  4. a source that fails keeps its last list and says why; the others carry on; a huge answer is refused
  5. only the three fixed URLs are ever fetched, with the app's User-Agent; the credit HamQTH asks for
"""
import json
from datetime import datetime, timezone

from backend import spots as S

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got)[:240], "" if good else f"(want {want!r})")


T0 = datetime(2026, 11, 19, 14, 0, 0, tzinfo=timezone.utc).timestamp()   # "now" for the samples


def iso(min_ago):
    return datetime.fromtimestamp(T0 - min_ago * 60, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


POTA = [
    {"spotId": 1, "activator": "n0call", "frequency": "14275", "mode": "SSB", "reference": "US-0001",
     "parkName": None, "spotTime": iso(2), "spotter": "N1CALL", "comments": "QRT  soon\n<b>thanks</b>",
     "source": "Web", "invalid": None, "name": "Sample State Park", "locationDesc": "US-WI", "grid4": "EN53"},
    {"spotId": 2, "activator": "N2CALL/P", "frequency": "7032.5", "mode": "CW", "reference": "US-0002",
     "name": "Other Park", "locationDesc": "", "spotTime": iso(40), "spotter": "N3CALL", "comments": ""},   # too old
    {"spotId": 3, "activator": "N4CALL", "frequency": "", "mode": "FT8", "spotTime": iso(1)},           # no freq: skipped
    {"spotId": 4, "activator": "N5CALL", "frequency": "14074", "mode": "FT8", "reference": "US-0003",
     "name": "Third Park", "locationDesc": "US-MN", "spotTime": iso(5), "spotter": "N6CALL", "invalid": True},  # invalid
    "junk",
]
WWFF = [
    {"id": 10, "activator": "F0CALL/P", "frequency_khz": 14059, "mode": "CW", "reference": "FFF-0001",
     "reference_name": "SAMPLE FOREST", "remarks": "Auto-spotted via RBN", "spotter": "OK0CALL",
     "spot_time": T0 - 3 * 60, "spot_time_formatted": "x"},
    {"id": 11, "activator": "G0CALL", "frequency_khz": 3700, "mode": "LSB", "reference": "GFF-0001",
     "reference_name": "x" * 300, "remarks": None, "spotter": "G1CALL", "spot_time": T0 - 60},
]
HAMQTH = {"spots": [
    {"spotter": "W0CALL", "dxcall": "9A0CALL", "freq": 28003.6, "band": "10M", "mode": "CW", "comment": "up 2",
     "time": iso(4) + "Z", "country": "Croatia", "continent": "EU"},
    {"spotter": "K0CALL", "dxcall": "JA0CALL", "freq": 14074.0, "band": "20M", "mode": "DIGI", "comment": None,
     "time": iso(6) + "Z", "country": "Japan"},
    {"spotter": "K1CALL", "dxcall": "VK0CALL", "freq": "nonsense", "mode": None, "time": iso(1) + "Z"},
]}
ANSWERS = {S.SOURCES["pota"]["url"]: POTA, S.SOURCES["wwff"]["url"]: WWFF, S.SOURCES["dx"]["url"]: HAMQTH}


class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


asked, down = [], set()


def fetch(url):
    asked.append(url)
    if url in down:
        raise OSError("network is unreachable")
    return json.dumps(ANSWERS[url]).encode("utf-8")


clk = Clock()
sp = S.Spots("TeleRig/test", fetch=fetch, clock=clk, wall=lambda: T0)

# -- 1. one shape -----------------------------------------------------------------------------------
r = sp.get(["pota", "wwff", "dx"])
by = {(x["src"], x["call"]): x for x in r["spots"]}
p = by[("pota", "N0CALL")]
C("POTA: call upper-cased, kHz -> Hz, SSB, park (area), spotter", (p["hz"], p["mode"], p["ref"], p["name"], p["spotter"]),
  (14_275_000, "SSB", "US-0001", "Sample State Park (US-WI)", "N1CALL"))
C("POTA: the time is UTC with a Z", p["time"], iso(2) + "Z")
C("POTA: the comment is one line of text (the page shows it as text)", p["note"], "QRT soon <b>thanks</b>")
C("POTA: no frequency, invalid, junk and too-old rows are left out",
  sorted(k[1] for k in by if k[0] == "pota"), ["N0CALL"])
w = by[("wwff", "F0CALL/P")]
C("WWFF: kHz int -> Hz, CW, reference + name, unix time -> UTC", (w["hz"], w["mode"], w["ref"], w["name"], w["time"]),
  (14_059_000, "CW", "FFF-0001", "SAMPLE FOREST", datetime.fromtimestamp(T0 - 180, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
C("WWFF: LSB counts as SSB; a long name is capped at 80", (by[("wwff", "G0CALL")]["mode"], len(by[("wwff", "G0CALL")]["name"])),
  ("SSB", 80))
d = by[("dx", "9A0CALL")]
C("HamQTH: dxcall, kHz float -> Hz, CW, the country as the name", (d["hz"], d["mode"], d["name"], d["note"]),
  (28_003_600, "CW", "Croatia", "up 2"))
C("HamQTH: DIGI stays DIGI; a junk frequency is skipped",
  (by[("dx", "JA0CALL")]["mode"], ("dx", "VK0CALL") in by), ("DIGI", False))
C("modes normalised", [S.norm_mode(m) for m in ("ft8", "USB", "psk31", "", None, "Olivia", "C4FM", "AM")],
  ["DIGI", "SSB", "DIGI", "", "", "DIGI", "", "AM"])
C("SSTV is a category of its own, not data (the page tunes it as SSB, by band)",
  [S.norm_mode(m) for m in ("SSTV", "sstv", " Sstv ")], ["SSTV", "SSTV", "SSTV"])
C("every spot has the same keys", {tuple(sorted(x)) for x in r["spots"]},
  {("call", "hz", "mode", "name", "note", "ref", "spotter", "src", "sub", "time")})
C("sub: the sideband a source names, and a data spot's own mode (the page tunes by them)",
  [S.sub_mode(m) for m in ("USB", "lsb", "SSB", "FT8", "ft4", "RTTY", "PSK", "PSK31", "JS8", "DATA", "DIGI", "CW", "", None, "C4FM", "SSTV")],
  ["USB", "LSB", "", "FT8", "FT4", "RTTY", "PSK31", "PSK31", "JS8", "", "", "", "", "", "", ""])

# -- 2. order, age, sources asked for ------------------------------------------------------------------
times = [x["time"] for x in r["spots"]]
C("newest first across the sources", times, sorted(times, reverse=True))
C("each source's status: ok, count", {k: (v["ok"], v["count"]) for k, v in r["sources"].items()},
  {"pota": (True, 1), "wwff": (True, 2), "dx": (True, 2)})
C("only the sources asked for", sorted({x["src"] for x in sp.get(["wwff"])["spots"]}), ["wwff"])

# -- 3. the polling floors --------------------------------------------------------------------------------
asked.clear()
for _ in range(20):
    sp.get(["pota", "wwff", "dx"])
C("20 more requests inside the floors fetch nothing", asked, [])
clk.t += 31
sp.get(["pota", "wwff", "dx"])
C("after 31 s only HamQTH (30 s floor) is fetched again", asked, [S.SOURCES["dx"]["url"]])
clk.t += 30
asked.clear()
sp.get(["pota", "wwff", "dx"])
C("after a minute POTA and WWFF too", sorted(asked), sorted(S.SOURCES[k]["url"] for k in ("pota", "wwff", "dx")))
C("the floors are the sources' own (POTA / WWFF 60 s, HamQTH 30 s; WWFF asks 30, HamQTH 15)",
  {k: v["every"] for k, v in S.SOURCES.items()}, {"pota": 60.0, "wwff": 60.0, "dx": 30.0})

# -- 4. failures -----------------------------------------------------------------------------------------
down.add(S.SOURCES["dx"]["url"])
clk.t += 31
r = sp.get(["pota", "wwff", "dx"])
C("HamQTH down: its last list stays, marked with the error", (r["sources"]["dx"]["ok"], r["sources"]["dx"]["count"],
  "unreachable" in (r["sources"]["dx"]["error"] or "")), (False, 2, True))
C("... POTA and WWFF carry on", (r["sources"]["pota"]["ok"], r["sources"]["wwff"]["ok"]), (True, True))
asked.clear()
sp.get(["dx"])
C("... and a failing source is not asked again before its floor", asked, [])
C("junk JSON: error, empty list", (lambda g: (g["sources"]["pota"]["ok"], g["sources"]["pota"]["count"]))(
  S.Spots("x", fetch=lambda u: b"<html>", clock=Clock(), wall=lambda: T0).get(["pota"])), (False, 0))

# -- 5. fixed URLs, User-Agent, credit ---------------------------------------------------------------------
C("only these three URLs", sorted(v["url"] for v in S.SOURCES.values()),
  sorted(["https://api.pota.app/spot/activator", "https://spots.wwff.co/static/spots.json",
          "https://www.hamqth.com/api/v2/spots?limit=200"]))
C("the HamQTH credit", r["credit"]["dx"], "Data provided by HamQTH.com")
seen = {}


class Resp:
    def __init__(self, body):
        self.body = body

    def read(self, n):
        return self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


orig = S.urllib.request.urlopen
try:
    def fake_urlopen(req, timeout=None):
        seen["ua"], seen["url"], seen["timeout"] = req.get_header("User-agent"), req.full_url, timeout
        return Resp(b"x" * (S.MAX_BYTES + 5))
    S.urllib.request.urlopen = fake_urlopen
    try:
        S._fetch_url(S.SOURCES["wwff"]["url"], "TeleRig/0.3.0 (+https://example.invalid)")
        C("an over-size answer raises", False, True)
    except ValueError:
        C("an over-size answer raises", True, True)
    C("the request names the app and has a timeout", (seen["ua"], seen["timeout"]),
      ("TeleRig/0.3.0 (+https://example.invalid)", S.TIMEOUT))
finally:
    S.urllib.request.urlopen = orig

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
