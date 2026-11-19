"""`smoke` group: the Spots panel's endpoint, GET /api/spots, on a real server process.

SAFETY / PRIVACY. Every server is harness.Server (run.py --sim-only --no-autoconnect, scratch config).
The real spot sources are never asked: each server gets TELERIG_TEST_SPOTS = a scratch folder of sample
answers (made-up callsigns, shaped like POTA / WWFF / HamQTH answers), which only a --sim-only server
honours. The page side (filters, tuning) is tests/spots_ui_test.js.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import harness
from harness import check, eq, test


def _iso(min_ago: float, z: bool = False) -> str:
    s = datetime.fromtimestamp(time.time() - min_ago * 60, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return s + ("Z" if z else "")


def _server(ctx):
    d = ctx.tmp("spots")
    (d / "pota.json").write_text(json.dumps([
        {"activator": "N0CALL", "frequency": "14275", "mode": "SSB", "reference": "US-0001", "name": "Sample Park",
         "locationDesc": "US-WI", "spotTime": _iso(2), "spotter": "N1CALL", "comments": ""}]), encoding="utf-8")
    (d / "wwff.json").write_text(json.dumps([
        {"activator": "F0CALL/P", "frequency_khz": 7032, "mode": "CW", "reference": "FFF-0001",
         "reference_name": "SAMPLE FOREST", "spotter": "OK0CALL", "spot_time": time.time() - 180, "remarks": ""}]),
        encoding="utf-8")
    (d / "dx.json").write_text(json.dumps({"spots": [
        {"spotter": "K0CALL", "dxcall": "JA0CALL", "freq": 14074.0, "mode": "DIGI", "comment": None,
         "time": _iso(6, z=True), "country": "Japan"}]}), encoding="utf-8")
    return ctx.server(extra_env={"TELERIG_TEST_SPOTS": str(d)})


@test("smoke")
def spots_endpoint_lists_the_sources(ctx):
    srv = _server(ctx)
    r = harness.Client(srv.port).get("/api/spots")
    check(r.status in (303, 401), f"signed out: no spots (got {r.status})")
    pws = harness.mk_users(srv.tmp, [("V1", "viewer")])
    v = srv.client()
    eq(harness.login(v, "V1", pws["V1"]).status, 200, "viewer login")
    r = v.get("/api/spots")
    eq(r.status, 200, "a viewer gets the spots")
    eq(r.headers.get("cache-control"), "no-store", "never cached by the browser")
    j = r.json()
    eq([(s["src"], s["call"], s["hz"], s["mode"]) for s in j["spots"]],
       [("pota", "N0CALL", 14_275_000, "SSB"), ("wwff", "F0CALL/P", 7_032_000, "CW"), ("dx", "JA0CALL", 14_074_000, "DIGI")],
       "all three sources, newest first, in one shape")
    eq({k: v_["ok"] for k, v_ in j["sources"].items()}, {"pota": True, "wwff": True, "dx": True}, "every source answered")
    eq(j["credit"]["dx"], "Data provided by HamQTH.com", "the credit HamQTH asks for")
    eq([s["src"] for s in v.get("/api/spots?src=wwff").json()["spots"]], ["wwff"], "?src=wwff: WWFF only")
    eq(sorted(v.get("/api/spots?src=pota,nonsense,../../etc").json()["sources"]), ["pota"], "unknown names are ignored")
    eq(sorted(v.get("/api/spots?src=nonsense").json()["sources"]), ["dx", "pota", "wwff"], "none left: all three")
