"""Memories over HTTP: GET / POST / DELETE /api/me/memories (in the `layout` group, with the other
per-user files).

Every server is harness.Server (run.py --sim-only --no-autoconnect, scratch config). No radio is
connected and no TX command is sent: a memory is a row in the user's own file, nothing is tuned.
The validation table itself is unit-tested in backend/memory_store_test.py.
"""
from __future__ import annotations

import json
import time

import harness
from harness import check, eq, test

API = "/api/me/memories"
ITEM = {"hz": 146520000, "mode": "FM", "name": "Simplex"}
YAESU = {"hz": 146520000, "mode": "FM", "name": "Simplex", "narrow": False, "width": 0}     # NAR, WIDTH
ICOM = {"hz": 145500000, "mode": "FM", "name": "Calling", "data": False, "filter": 1}       # DATA, FIL
FT8 = {"hz": 7074000, "mode": "DATA-USB", "name": "FT8 40 m"}
EMPTY = {"rev": 0, "radios": {}, "max": 100, "memories_error": False}


def _err(r):
    j = r.json()
    return j.get("error") if isinstance(j, dict) else None


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


def mfile(srv, name):
    return srv.tmp / "memories" / f"{harness.uid(name)}.json"


def written(p):
    """The file's bytes and when it was last written: equal before and after = nothing was written."""
    return p.read_bytes(), p.stat().st_mtime_ns


def three(client):
    """(status, error) of the GET, the POST and the DELETE as this client."""
    return [(r.status, _err(r)) for r in (client.get(API), client.post(API, {"radio": "ft991a", "item": ITEM}),
                                          client.delete(API + "?radio=ft991a&id=1"))]


# -- add, list, delete; one file per user ------------------------------------------------------------------
@test("layout")
def memories_add_list_delete_and_two_users(ctx):
    srv, cl = users(ctx, ("OP1", "operator"), ("OP2", "operator"))
    op1, op2, admin = cl["OP1"], cl["OP2"], cl["ADMIN"]
    r = op1.get(API)
    eq((r.status, r.json(), r.headers.get("cache-control")), (200, EMPTY, "no-store"), "GET before any save")
    check(not (srv.tmp / "memories").exists(), "a read created the memories folder")
    r = op1.post(API, {"radio": "ft991a", "item": {**YAESU, "name": "  Simplex  "}})
    eq((r.status, r.json(), r.headers.get("cache-control")),
       (200, {"ok": True, "rev": 1, "id": 1, "items": [{"id": 1, **YAESU}]}, "no-store"), f"the first POST {r!r}")
    r = op1.post(API, {"radio": "ft991a", "item": FT8})
    eq(r.json(), {"ok": True, "rev": 2, "id": 2, "items": [{"id": 1, **YAESU}, {"id": 2, **FT8}]},
       "rev+1, the next id, the radio's whole list in id order")
    r = op1.post(API, {"radio": "ic9700", "item": ICOM})
    eq(r.json(), {"ok": True, "rev": 3, "id": 3, "items": [{"id": 3, **ICOM}]}, "another radio: a list of its own, the same id counter")
    lists = {"ft991a": [{"id": 1, **YAESU}, {"id": 2, **FT8}], "ic9700": [{"id": 3, **ICOM}]}
    mine = {"rev": 3, "radios": lists, "max": 100, "memories_error": False}
    eq(op1.get(API).json(), mine, "GET: every radio's list, items as stored with their ids")
    eq(json.loads(mfile(srv, "OP1").read_text()), {"v": 1, "rev": 3, "next": 4, "radios": lists}, "the stored file")

    # two users never see each other's
    eq(op2.get(API).json(), EMPTY, "OP2 sees none of OP1's")
    check(not mfile(srv, "OP2").exists(), "OP2 has no file before saving")
    w1 = written(mfile(srv, "OP1"))
    r = op2.post(API, {"radio": "ft991a", "item": FT8})
    eq(r.json(), {"ok": True, "rev": 1, "id": 1, "items": [{"id": 1, **FT8}]}, "OP2's own first memory: rev 1, id 1")
    r = op2.delete(API + "?radio=ft991a&id=2")                  # OP1's id 2: not in OP2's list
    eq((r.status, r.json()), (200, {"ok": True, "rev": 1, "items": [{"id": 1, **FT8}]}), "OP2 cannot delete OP1's memory")
    eq(written(mfile(srv, "OP1")), w1, "only the caller's file changes")
    eq(op1.get(API).json(), mine, "OP1 sees none of OP2's")
    eq(admin.get(API).json(), EMPTY, "an admin is an operator too, with memories of their own")

    # delete
    r = op1.delete(API + "?radio=ft991a&id=1")
    left = {"ok": True, "rev": 4, "items": [{"id": 2, **FT8}]}
    eq((r.status, r.json(), r.headers.get("cache-control")), (200, left, "no-store"), f"DELETE {r!r}")
    w1 = written(mfile(srv, "OP1"))
    for q, what in (("radio=ft991a&id=1", "the id just deleted"), ("radio=ft991a&id=77", "an id that never was"),
                    ("radio=ft991a&id=3", "an id of another radio's list"), ("radio=ft991a&id=0", "id 0")):
        r = op1.delete(API + "?" + q)
        eq((r.status, r.json()), (200, left), f"DELETE {what}: the same answer, rev unchanged")
    r = op1.delete(API + "?radio=ft891&id=2")
    eq((r.status, r.json()), (200, {"ok": True, "rev": 4, "items": []}), "DELETE on a radio with no list")
    eq(written(mfile(srv, "OP1")), w1, "a DELETE of an id that is not there writes nothing")
    r = op1.request("DELETE", API + "?radio=ic9700&id=3")       # with a {} JSON body, as the page sends it
    eq((r.status, r.json()), (200, {"ok": True, "rev": 5, "items": []}), "the last memory of a radio")
    eq(op1.get(API).json(), {"rev": 5, "radios": {"ft991a": [{"id": 2, **FT8}]}, "max": 100, "memories_error": False},
       "a radio with no memory left is not listed")
    r = op1.post(API, {"radio": "ic9700", "item": ICOM})
    eq((r.json().get("rev"), r.json().get("id")), (6, 4), "an id is never used twice")

    # the Gate: exact Origin and a JSON body, as for every state-changing /api/ route
    r = op1.post(API, {"radio": "ft991a", "item": ITEM}, origin="http://evil.example")
    eq(r.status, 403, "a cross-site POST")
    r = op1.delete(API + "?radio=ft991a&id=2", origin="http://evil.example")
    eq(r.status, 403, "a cross-site DELETE")
    eq((op1.post(API, {"radio": "ft991a", "item": ITEM}, origin=None).status, op1.delete(API + "?radio=ft991a&id=2", origin=None).status),
       (403, 403), "POST and DELETE without an Origin")
    r = op1.raw("POST", API, body=b"radio=ft991a", headers={"Origin": op1.origin, "Content-Type": "text/plain"})
    eq((r.status, _err(r)), (415, "json_required"), "a non-JSON body")
    j = op1.get(API).json()
    eq((j["rev"], [i["id"] for i in j["radios"]["ft991a"]]), (6, [2]), "the refused requests changed nothing")


# -- who may not, and what is refused -------------------------------------------------------------------
BAD = [
    ({}, "radio"),
    ({"item": ITEM}, "radio"),
    ({"radio": "nope", "item": ITEM}, "radio"),
    ({"radio": 5, "item": ITEM}, "radio"),
    ({"radio": "ft991a"}, "item"),
    ({"radio": "ft991a", "item": [ITEM]}, "item"),
    ({"radio": "ft991a", "item": ITEM, "x": 1}, "x"),
    ({"radio": "ft991a", "item": {**ITEM, "tone": 100}}, "item.tone"),
    ({"radio": "ft991a", "item": {**ITEM, "id": 7}}, "item.id"),
    ({"radio": "ft991a", "item": {"mode": "FM", "name": "x"}}, "item.hz"),
    ({"radio": "ft991a", "item": {**ITEM, "hz": True}}, "item.hz"),
    ({"radio": "ft991a", "item": {**ITEM, "hz": 146520000.5}}, "item.hz"),
    ({"radio": "ft991a", "item": {**ITEM, "hz": "146520000"}}, "item.hz"),
    ({"radio": "ic7300mk2", "item": ITEM}, "item.hz"),                          # 146.520 MHz on an HF radio
    ({"radio": "ft991a", "item": {"hz": 146520000, "name": "x"}}, "item.mode"),
    ({"radio": "ft991a", "item": {**ITEM, "mode": "DV"}}, "item.mode"),         # the IC-9700's
    ({"radio": "ft991a", "item": {**ITEM, "mode": 4}}, "item.mode"),
    ({"radio": "ft991a", "item": {"hz": 146520000, "mode": "FM"}}, "item.name"),
    ({"radio": "ft991a", "item": {**ITEM, "name": ""}}, "item.name"),
    ({"radio": "ft991a", "item": {**ITEM, "name": "   "}}, "item.name"),
    ({"radio": "ft991a", "item": {**ITEM, "name": "x" * 33}}, "item.name"),
    ({"radio": "ft991a", "item": {**ITEM, "name": "a\nb"}}, "item.name"),
    ({"radio": "ft991a", "item": {**ITEM, "name": 5}}, "item.name"),
    ({"radio": "ft991a", "item": {**ITEM, "data": 1}}, "item.data"),
    ({"radio": "ft991a", "item": {**ITEM, "filter": 0}}, "item.filter"),
    ({"radio": "ft991a", "item": {**ITEM, "filter": 4}}, "item.filter"),
    ({"radio": "ft991a", "item": {**ITEM, "filter": True}}, "item.filter"),
    ({"radio": "ft991a", "item": {**ITEM, "narrow": "yes"}}, "item.narrow"),
    ({"radio": "ft991a", "item": {**ITEM, "width": -1}}, "item.width"),
    ({"radio": "ft991a", "item": {**ITEM, "width": 100}}, "item.width"),
]
BAD_DELETE = [
    ("", "id"), ("radio=ft991a", "id"), ("radio=ft991a&id=", "id"), ("radio=ft991a&id=abc", "id"), ("radio=ft991a&id=-1", "id"),
    ("radio=ft991a&id=1.5", "id"), ("radio=ft991a&id=1e3", "id"), ("radio=ft991a&id=" + "9" * 17, "id"),
    ("id=1", "radio"), ("radio=&id=1", "radio"), ("radio=nope&id=1", "radio"), ("radio=FT991A&id=1", "radio"),
]


def padded(n: int) -> bytes:
    """A JSON body of exactly n bytes (a valid request plus a `pad` key)."""
    body = {"radio": "ft991a", "item": ITEM, "pad": ""}
    body["pad"] = "x" * (n - len(json.dumps(body).encode("utf-8")))
    return json.dumps(body).encode("utf-8")


@test("layout")
def memories_roles_and_refused_requests(ctx):
    srv, cl = users(ctx, ("OP1", "operator"), ("V1", "viewer"))
    op, v, admin = cl["OP1"], cl["V1"], cl["ADMIN"]
    eq(three(v), [(403, "forbidden")] * 3, "a viewer: GET, POST, DELETE")
    eq(v.get(API).json().get("need"), "operator", "the 403 says what is needed")
    eq(three(srv.client()), [(401, "auth_required")] * 3, "signed out: GET, POST, DELETE")
    temp = harness.api_mk_user(admin, "OP3", "operator", must_change=True)
    c = srv.client()
    r = harness.login(c, "OP3", temp)
    eq((r.status, r.json().get("must_change")), (200, True), "OP3 signs in with a password that must be changed")
    eq(three(c), [(403, "password_change_required")] * 3, "a restricted account: GET, POST, DELETE")

    for body, field in BAD:
        r = op.post(API, body)
        j = r.json() or {}
        eq((r.status, j.get("ok"), j.get("error"), j.get("field")), (400, False, "invalid", field), f"POST {body}")
        check(isinstance(j.get("message"), str) and j["message"].endswith("."), f"a sentence for the user: {j}")
        eq(r.headers.get("cache-control"), "no-store", "no-store on a 400")
    r = op.raw("POST", API, headers={"Origin": op.origin},     # half of a character pair: could not be sent back
               body=b'{"radio":"ft991a","item":{"hz":146520000,"mode":"FM","name":"x\\ud83d"}}')
    eq((r.status, _err(r), r.json().get("field")), (400, "invalid", "item.name"), f"an unpaired surrogate in the name {r!r}")
    for q, field in BAD_DELETE:
        r = op.delete(API + "?" + q)
        j = r.json() or {}
        eq((r.status, j.get("error"), j.get("field")), (400, "invalid", field), f"DELETE ?{q}")
        check(isinstance(j.get("message"), str) and j["message"].endswith("."), f"a sentence for the user: {j}")

    eq((len(padded(2048)), len(padded(2049))), (2048, 2049), "the test bodies")
    r = op.post(API, padded(2048))
    eq((r.status, r.json().get("field")), (400, "pad"), "a body of 2048 bytes is read (and refused for its extra key)")
    r = op.post(API, padded(2049))
    eq((r.status, _err(r)), (413, "too_large"), f"a body of 2049 bytes {r!r}")
    for label, body in (("not JSON", b"{nope"), ("a JSON array", b"[1]"), ("a JSON string", b'"ft991a"')):
        r = op.post(API, body)
        eq((r.status, _err(r)), (400, "invalid_json"), label)
    r = op.post(API, b'{"radio":' + b"[" * 1000 + b"]" * 1000 + b"}")
    check(r.status == 400 and _err(r) in ("invalid_json", "invalid"), f"a deeply nested body under the cap: {r!r}")

    check(not (srv.tmp / "memories").exists(), "a refused request wrote something")
    eq(op.get(API).json(), EMPTY, "nothing was kept")
    check(srv.alive(), "server died")


# -- the limit --------------------------------------------------------------------------------------------
@test("layout")
def memories_full_at_100_a_radio(ctx):
    srv, cl = users(ctx, ("OP1", "operator"))
    op = cl["OP1"]
    eq(op.get(API).json().get("max"), 100, "GET names the limit")
    for n in range(100):
        r = op.post(API, {"radio": "ft991a", "item": {"hz": 144_000_000 + n * 12_500, "mode": "FM", "name": f"Ch {n + 1}"}})
        check(r.status == 200, f"memory {n + 1}: {r!r}")
    j = r.json()
    eq((j["rev"], j["id"], len(j["items"])), (100, 100, 100), "the 100th")
    before = written(mfile(srv, "OP1"))
    r = op.post(API, {"radio": "ft991a", "item": ITEM})
    j = r.json() or {}
    eq((r.status, j.get("ok"), j.get("error"), r.headers.get("cache-control")), (409, False, "full", "no-store"), f"the 101st {r!r}")
    check(isinstance(j.get("message"), str) and "100" in j["message"], f"a sentence for the user: {j}")
    eq(written(mfile(srv, "OP1")), before, "the refused one wrote nothing")
    r = op.post(API, {"radio": "ic9700", "item": ICOM})
    eq((r.status, r.json().get("id")), (200, 101), "another radio still takes one")
    eq(len(op.delete(API + "?radio=ft991a&id=50").json()["items"]), 99, "one deleted")
    r = op.post(API, {"radio": "ft991a", "item": ITEM})
    eq((r.status, r.json().get("id"), len(r.json()["items"])), (200, 102, 100), "room again after a delete, under a new id")
    eq(_err(op.post(API, {"radio": "ft991a", "item": ITEM})), "full", "and full again")
    got = op.get(API).json()
    eq(([i["id"] for i in got["radios"]["ft991a"]], len(got["radios"]["ic9700"])),
       ([n for n in range(1, 101) if n != 50] + [102], 1), "GET: all 100, in id order")


# -- the files: gone with the user, never in the way --------------------------------------------------------
@test("layout")
def memories_file_goes_with_the_user(ctx):
    srv, cl = users(ctx, ("OP1", "operator"), ("OP2", "operator"))
    op1, op2, admin = cl["OP1"], cl["OP2"], cl["ADMIN"]
    eq(op1.post(API, {"radio": "ft991a", "item": YAESU}).status, 200, "OP1 keeps one")
    eq(admin.post(API, {"radio": "ic9700", "item": ICOM}).status, 200, "ADMIN keeps one")
    f1 = mfile(srv, "OP1")
    check(f1.exists() and mfile(srv, "ADMIN").exists(), "memories/<hex>.json per user")
    eq(admin.delete(f"/api/admin/users/{harness.uid('OP1')}").status, 200, "the admin deletes OP1")
    check(not f1.exists(), "OP1's memories file was not deleted")
    eq(op1.get(API).status, 401, "OP1's session ended")
    eq(admin.get(API).json()["radios"], {"ic9700": [{"id": 1, **ICOM}]}, "the others keep theirs")
    pw = harness.api_mk_user(admin, "OP1", "operator")          # the callsign again: a new account
    again = srv.client()
    eq(harness.login(again, "OP1", pw).status, 200, "the new OP1 signs in")
    eq(again.get(API).json(), EMPTY, "the new OP1 has none of the old one's")
    eq(again.post(API, {"radio": "ft991a", "item": FT8}).json().get("id"), 1, "and starts at id 1")

    # a file left behind under a callsign (a delete that could not remove it) goes when the callsign is created
    f9 = mfile(srv, "OP9")
    f9.write_text(json.dumps({"v": 1, "rev": 1, "next": 2, "radios": {"ft991a": [{"id": 1, **ITEM}]}}), encoding="utf-8")
    pw = harness.api_mk_user(admin, "OP9", "operator")
    check(not f9.exists(), "a file left behind was not removed when the callsign was created")
    op9 = srv.client()
    eq(harness.login(op9, "OP9", pw).status, 200, "OP9 signs in")
    eq(op9.get(API).json(), EMPTY, "OP9 starts with none")

    # a file edited by hand: what does not validate is left out of the answer and stays in the file
    hand = {"v": 1, "rev": 7, "next": 9, "radios": {
        "ft991a": [{"id": 5, **ITEM}, {"id": 6, "hz": 146520000, "mode": "DV", "name": "not an FT-991A mode"},
                   {"id": 3, **FT8}, {"id": 7, "hz": 1296000000, "mode": "FM", "name": "outside its coverage"}],
        "ft1000": [{"id": 8, "hz": 14074000, "mode": "USB", "name": "a radio TeleRig does not have"}]}}
    f9.write_text(json.dumps(hand), encoding="utf-8")
    w9 = written(f9)
    time.sleep(harness.STORE_PICKUP)                            # the server looks at a file it has read every 0.5 s
    eq(op9.get(API).json(), {"rev": 7, "radios": {"ft991a": [{"id": 3, **FT8}, {"id": 5, **ITEM}]}, "max": 100, "memories_error": False},
       "GET leaves out the unknown radio and the items that do not validate; id order")
    eq(written(f9), w9, "the read did not touch the file")
    r = op9.post(API, {"radio": "ft991a", "item": YAESU})
    eq((r.status, _err(r)), (200, None), f"a save on it {r!r}")
    eq((r.json()["rev"], r.json()["id"], [i["id"] for i in r.json()["items"]]), (8, 9, [3, 5, 9]), "rev+1, the file's next id")
    kept = json.loads(f9.read_text(encoding="utf-8"))["radios"]
    eq((len(kept["ft991a"]), kept["ft1000"]), (5, hand["radios"]["ft1000"]), "the save kept what the read leaves out")

    # an unreadable file is reported, never in the way, and replaced by the next save
    f2 = mfile(srv, "OP2")
    f2.write_text("{not json", encoding="utf-8")                # OP2 never asked before: nothing cached
    eq(op2.get(API).json(), {"rev": 0, "radios": {}, "max": 100, "memories_error": True}, "an unreadable file is reported")
    eq(op2.delete(API + "?radio=ft991a&id=1").json(), {"ok": True, "rev": 0, "items": []}, "a DELETE answers and leaves it alone")
    eq(f2.read_text(encoding="utf-8"), "{not json", "the unreadable file is still there")
    r = op2.post(API, {"radio": "ft991a", "item": ITEM})
    eq((r.status, r.json()), (200, {"ok": True, "rev": 1, "id": 1, "items": [{"id": 1, **ITEM}]}), "the next save starts a fresh file")
    aside = [x for x in f2.parent.iterdir() if x.name.startswith(f2.name + ".corrupt-")]
    eq([x.read_text(encoding="utf-8") for x in aside], ["{not json"], "the unreadable file was moved aside, not lost")
    eq(op2.get(API).json(), {"rev": 1, "radios": {"ft991a": [{"id": 1, **ITEM}]}, "max": 100, "memories_error": False},
       "readable again")
    check(srv.alive(), "server died")


# -- a file that is busy or cannot be written ---------------------------------------------------------------
_TROUBLE = r"""
import json, os, re, sys
sys.path.insert(0, {tests!r}); sys.path.insert(0, {repo!r})
import harness
harness.check_config_path(os.environ["TELERIG_CONFIG"])        # scratch store only
from backend import accounts, memory_store, server
from backend.store import StoreBusy, StoreError
accounts.ensure_initialized(reconcile_mode="skip")
accounts.reset_admin("Mem#Probe-2026")
r = harness.asgi_request(server.app, "POST", "/api/login", body={{"username": "admin", "password": "Mem#Probe-2026"}})
CK = {{"cookie": "tr_session_8790=" + re.search(r"tr_session_8790=([^;]+)", " ".join(r.header_all("set-cookie"))).group(1)}}
BODY = {{"radio": "ft991a", "item": {{"hz": 146520000, "mode": "FM", "name": "Simplex"}}}}


def both():
    p = harness.asgi_request(server.app, "POST", "/api/me/memories", body=BODY, headers=CK)
    d = harness.asgi_request(server.app, "DELETE", "/api/me/memories", headers=CK, query=b"radio=ft991a&id=1")
    return [[x.status, x.json(), x.headers.get("cache-control")] for x in (p, d)]


out = {{}}
real = memory_store.add, memory_store.delete
for label, exc in (("busy", StoreBusy("held by another process")), ("store", StoreError("the write failed")),
                   ("os", OSError(28, "No space left on device"))):
    def fail(*a, _exc=exc, **k):
        raise _exc
    memory_store.add = memory_store.delete = fail
    out[label] = both()
memory_store.add, memory_store.delete = real
out["afterwards"] = both()
print("RESULT " + json.dumps(out))
"""


@test("layout")
def memories_file_busy_or_unwritable_is_503_or_500(ctx):
    """A memories file whose lock another TeleRig process holds answers 503 store_busy, one that
    cannot be written 500 store_error, each with a sentence for the user and like a layout save;
    the next request is answered normally. In-process (backend.server.app via harness.asgi_request
    in a scratch child): the store's add and delete are swapped for ones that fail. No server,
    socket or radio."""
    tmp = ctx.tmp()
    r = harness.run_child(["-c", _TROUBLE.format(tests=str(harness.TESTS), repo=str(harness.REPO))], tmp, timeout=120)
    line = next((ln for ln in (r.stdout or "").splitlines() if ln.startswith("RESULT ")), None)
    check(r.returncode == 0 and line, f"child failed ({r.returncode}):\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
    out = json.loads(line[len("RESULT "):])
    busy = {"ok": False, "error": "store_busy", "message": "The memories file is busy; try again."}
    failed = {"ok": False, "error": "store_error", "message": "The memories file could not be written; nothing changed."}
    eq(out["busy"], [[503, busy, "no-store"]] * 2, "POST and DELETE while the file's lock is held")
    eq(out["store"], [[500, failed, "no-store"]] * 2, "POST and DELETE when the write fails")
    eq(out["os"], [[500, failed, "no-store"]] * 2, "POST and DELETE on a disk error")
    eq(out["afterwards"], [[200, {"ok": True, "rev": 1, "id": 1, "items": [{"id": 1, **ITEM}]}, "no-store"],
                           [200, {"ok": True, "rev": 2, "items": []}, "no-store"]], "the next requests are answered normally")
    check("No space left on device" in (r.stderr or ""), "the console names the cause:\n" + (r.stderr or "")[-2000:])
