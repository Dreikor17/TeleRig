"""`layout` group (step A5): per-user layouts over HTTP and in the bootstrap (§3.7, §3.14, §3.15, I22).

Every server is harness.Server (run.py --sim-only --no-autoconnect, scratch config). No radio is
connected and no TX command is sent. The validation/clamping table itself is unit-tested in
backend/layout_store_test.py.
"""
from __future__ import annotations

import json
import re

import harness
from harness import check, eq, test

DESK = {"column": 12, "items": [
    {"id": "controls", "x": 0, "y": 0, "w": 3, "h": 24, "hidden": False, "collapsed": False},
    {"id": "vfo", "x": 3, "y": 0, "w": 4, "h": 5, "hidden": False, "collapsed": False},
    {"id": "cw", "x": 9, "y": 9, "w": 3, "h": 15, "hidden": True, "collapsed": False, "room": {"from": "scope", "dw": 3}}]}
PHONE = {"column": 1, "items": [{"id": "vfo", "x": 0, "y": 0, "w": 1, "h": 5, "hidden": False, "collapsed": False},
                                {"id": "scope", "x": 0, "y": 1, "w": 1, "h": 11, "hidden": False, "collapsed": False}]}
BOUNDS = {"desktop": {"cols": 12, "y_max": 199, "h_max": 96, "bottom_max": 200, "dw_max": 11},
          "phone": {"rank_max": 15, "h_max": 60}, "split": [0.15, 0.85]}


def _err(r):
    j = r.json()
    return j.get("error") if isinstance(j, dict) else None


def boot(client) -> dict:
    m = re.search(r'<script id="tr-boot" type="application/json">(.*?)</script>', client.get("/").text)
    check(m, "no bootstrap JSON in GET /")
    return json.loads(m.group(1))


def users(ctx, *specs):
    srv = ctx.server()
    admin = harness.ready_admin(srv)
    out = {"ADMIN": admin}
    for name, role in specs:
        pw = harness.api_mk_user(admin, name, role)
        c = srv.client()
        eq(harness.login(c, name, pw).status, 200, f"login {name}")
        out[name] = c
    return srv, out


def lfile(srv, name):
    return srv.tmp / "layouts" / f"{harness.uid(name)}.json"


# -- T24: PUT, rev, clamping, size, types, isolation ------------------------------------------------------
@test("layout")
def t24_layout_put_rev_clamp_and_isolation(ctx):
    srv, cl = users(ctx, ("OP1", "operator"), ("OP2", "operator"))
    op1, op2 = cl["OP1"], cl["OP2"]
    b = boot(op1)
    eq((b["layout"], b["layout_error"], b["layout_bounds"]), (None, False, BOUNDS), "bootstrap before any save")
    eq(op1.get("/api/me/layout").json(), {"layout": None, "rev": 0, "layout_error": False}, "GET before any save")
    eq(op2.put("/api/me/layout", {"device": "desktop", "data": DESK}).status, 200, "OP2 saves once")
    op2_bytes = lfile(srv, "OP2").read_bytes()
    r = op1.put("/api/me/layout", {"device": "desktop", "data": DESK, "scope_split": 0.38, "base_rev": 0})
    eq((r.status, r.json().get("rev"), r.json().get("layout"), r.json().get("scope_split")), (200, 1, DESK, 0.38),
       f"T24 valid PUT {r!r}")
    r = op1.put("/api/me/layout", {"device": "desktop", "data": DESK, "base_rev": 1})
    eq(r.json().get("rev"), 2, "T24 rev+1")
    clamp = {"column": 12, "items": [{"id": "vfo", "x": 10, "y": 250, "w": 4, "h": 5},
                                     {"id": "scope", "x": 5, "y": 0, "w": 20, "h": 200}]}
    r = op1.put("/api/me/layout", {"device": "desktop", "data": clamp})
    eq(r.status, 200, f"T24 out-of-range numbers are accepted {r!r}")
    it = {i["id"]: i for i in r.json()["layout"]["items"]}
    eq((it["vfo"]["x"], it["vfo"]["w"], it["vfo"]["y"]), (8, 4, 195), "T24 x+w>12 and y:250 clamped and echoed")
    eq((it["scope"]["w"], it["scope"]["x"], it["scope"]["h"]), (12, 0, 96), "w:20 -> 12 (x -> 0), h:200 -> 96")
    r = op1.put("/api/me/layout", {"device": "phone", "data": {"column": 1, "items": [{"id": "vfo", "x": 0, "y": 0, "w": 2, "h": 5}]}})
    eq((r.status, r.json()["layout"]["items"][0]["w"]), (200, 1), "T24 phone w:2 -> 1, echoed")
    big = {"device": "desktop", "data": DESK, "pad": "x" * 17000}
    r = op1.put("/api/me/layout", big)
    eq((r.status, _err(r)), (413, "too_large"), "T24 a 17 KB body")
    bad = {"column": 12, "items": [{"id": "vfo", "x": True, "y": 0, "w": 4, "h": 5}]}
    r = op1.put("/api/me/layout", {"device": "desktop", "data": bad})
    eq((r.status, _err(r), r.json().get("field")), (400, "invalid", "items[0].x"), "T24 a bool where an int is expected")
    for body, field in (({"device": "tablet", "data": DESK}, "device"), ({"device": "desktop", "data": DESK, "x": 1}, "x"),
                        ({"device": "desktop", "data": DESK, "base_rev": -1}, "base_rev"),
                        ({"device": "desktop", "data": DESK, "scope_split": "0.4"}, "scope_split"),
                        ({"device": "desktop", "data": {"column": 12, "items": [{"id": "nope", "x": 0, "y": 0, "w": 1, "h": 1}]}},
                         "items[0].id")):
        r = op1.put("/api/me/layout", body)
        eq((r.status, r.json().get("field")), (400, field), f"400 field {field}")
    eq(lfile(srv, "OP2").read_bytes(), op2_bytes, "T24 only the caller's file changes")
    lay = json.loads(lfile(srv, "OP1").read_text())
    eq((lay["rev"], lay["v"], lay["gs"]), (4, 1, "14.0.0"), "the stored file: rev counts only successful writes")
    b = boot(op1)
    eq((b["layout"]["rev"], b["layout"]["phone"]["items"][0]["w"], b["layout_error"]), (4, 1, False),
       "the bootstrap carries the stored layout")
    eq(op1.get("/api/me/layout").json()["rev"], 4, "GET /api/me/layout")
    r = op1.raw("PUT", "/api/me/layout", body=b"device=desktop", headers={"Origin": op1.origin, "Content-Type": "text/plain"})
    eq((r.status, _err(r)), (415, "json_required"), "a non-JSON body")
    r = op1.put("/api/me/layout", {"device": "desktop", "data": DESK}, origin="http://evil.example")
    eq(r.status, 403, "a cross-site PUT")


# -- T24b: split bounds and per-device merge ------------------------------------------------------------
@test("layout")
def t24b_split_clamp_and_device_merge(ctx):
    srv, cl = users(ctx, ("V1", "viewer"))
    v = cl["V1"]                                     # viewers have layouts too
    r = v.put("/api/me/layout", {"device": "phone", "data": PHONE, "scope_split": 0.1})
    eq((r.status, r.json().get("scope_split")), (200, 0.15), "T24b scope_split 0.1 -> 0.15")
    r = v.put("/api/me/layout", {"device": "phone", "data": {"column": 1, "items": [{"id": "scope", "y": 0, "h": 70}]},
                                 "scope_split": 0.42})
    eq((r.json()["layout"]["items"][0]["h"], r.json()["scope_split"]), (60, 0.42), "T24b phone h:70 -> 60")
    r = v.put("/api/me/layout", {"device": "desktop", "data": DESK, "scope_split": 0.9})
    eq(r.json().get("scope_split"), 0.85, "desktop 0.9 -> 0.85")
    stored = json.loads(lfile(srv, "V1").read_text())
    eq(stored["scope_split"], {"phone": 0.42, "desktop": 0.85}, "T24b a desktop PUT leaves the stored phone split untouched")
    check("phone" in stored and "desktop" in stored, "both devices kept")
    r = v.put("/api/me/layout", {"device": "desktop", "data": DESK})
    eq((r.json()["scope_split"], json.loads(lfile(srv, "V1").read_text())["scope_split"]),
       (0.85, {"phone": 0.42, "desktop": 0.85}), "a PUT without scope_split keeps both")
    r = v.request("DELETE", "/api/me/layout?device=phone", None)
    eq((r.status, r.json().get("rev")), (200, 5), "DELETE ?device=phone -> rev+1")
    stored = json.loads(lfile(srv, "V1").read_text())
    eq(("phone" in stored, stored["scope_split"], "desktop" in stored), (False, {"desktop": 0.85}, True),
       "phone layout and split gone, desktop kept")
    r = v.request("DELETE", "/api/me/layout?device=tablet", None)
    eq((r.status, r.json().get("field")), (400, "device"), "DELETE an unknown device")
    admin = cl["ADMIN"]
    r = admin.delete(f"/api/admin/users/{harness.uid('V1')}/layout")
    eq(r.status, 200, "an admin resets V1's layout")
    eq(boot(v)["layout"].get("desktop"), None, "V1 boots with the defaults")


# -- T34: restricted sessions -----------------------------------------------------------------------------
@test("layout")
def t34_restricted_session_has_no_layout_routes(ctx):
    srv = ctx.server()
    c = srv.client()
    eq(harness.admin_login(c, srv.tmp).status, 200, "first-run login (must change)")
    r = c.get("/api/me/layout")
    eq((r.status, _err(r)), (403, "password_change_required"), "T34 GET /api/me/layout while restricted")
    r = c.put("/api/me/layout", {"device": "desktop", "data": DESK})
    eq((r.status, _err(r)), (403, "password_change_required"), "PUT while restricted")
    r = c.request("DELETE", "/api/me/layout", None)
    eq(r.status, 403, "DELETE while restricted")
    check(not (srv.tmp / "layouts").exists(), "nothing written")
    eq(srv.client().get("/api/me/layout").status, 401, "signed out -> 401")
