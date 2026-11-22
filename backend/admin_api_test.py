"""Admin API in-process tests — run: python -m backend.admin_api_test

The parts of the A5 table that a live server cannot show safely or deterministically:
  * T32: Serve ON / OFF with a monkeypatched Tailscale CLI (tailscale._run + cli_path): an ON failure
    leaves behind_proxy false and the allowlist unchanged; an OFF failure leaves behind_proxy true
    (AR-22); behind_proxy is written only after a confirmed result.
  * T74: a v1 behind_proxy flag with Tailscale reporting Serve off is cleared at startup
    (ensure_initialized -> reconcile) and audited; with Serve on it is kept and clear_behind_proxy
    gets 409 (AR-34); the behind_proxy_stale notice.
  * last_admin: the guard is decided under the store lock, so it also holds in a race (two admins
    demoting each other) that the self_* checks cannot see.
  * T40 / T31: allowlist validation and would_lock_out; audio pins with fake devices; the F20
    transport pin (T75, never credentials); the audit endpoint's parameters.

SAFETY: the Tailscale CLI is replaced BEFORE anything can call it, and the test asserts that every
call went to the fake; the real `tailscale serve` / `serve reset` can never run from here. No radio
is connected, no socket or server is opened (requests go straight into the ASGI app), and the
scratch TELERIG_CONFIG of the runner is required.
"""
import dataclasses
import json
import os
import re
import sys
from pathlib import Path

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    line = " ".join([("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})"])
    print(line.encode("ascii", "backslashreplace").decode("ascii"))


cfg = os.environ.get("TELERIG_CONFIG", "")
real = [os.path.join(os.path.expanduser("~"), d) for d in (".telerig", ".radio-webop")]
if not cfg or any(os.path.normcase(os.path.abspath(cfg)).startswith(os.path.normcase(r)) for r in real):
    raise SystemExit("REFUSING: admin_api_test needs a scratch TELERIG_CONFIG")
CFG = Path(cfg)
DIR = CFG.parent
DIR.mkdir(parents=True, exist_ok=True)
for name in ("auth.json", "config.json", "audit.log", "audit-tx.log"):
    try:
        (DIR / name).unlink()
    except OSError:
        pass

# -- the fake Tailscale CLI, installed before ANY backend code can reach the real one -----------------
from backend import tailscale  # noqa: E402

PORT = 8790                                     # harness.asgi_request's scope["server"] port
CALLS = []
TS = {}
SERVE_ON_TEXT = f"https://pc.tailnet.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:{PORT}\n"
STATUS_JSON = json.dumps({"BackendState": "Running", "Self": {"DNSName": "pc.tailnet.ts.net."}})


def ts_set(serve=(0, "", ""), reset=(0, "", ""), serve_status=(0, "No serve config\n", ""),
           status_json=(0, STATUS_JSON, "")):
    TS.update(serve=serve, reset=reset, serve_status=serve_status, status_json=status_json)


def fake_run(args, timeout=12):
    args = [str(a) for a in args]
    CALLS.append(args)
    if args[:2] == ["status", "--json"]:
        return TS["status_json"]
    if args[:2] == ["serve", "status"]:
        return TS["serve_status"]
    if args[:2] == ["serve", "--bg"]:
        return TS["serve"]
    if args[:2] == ["serve", "reset"]:
        return TS["reset"]
    return 1, "", "unexpected tailscale call"


ts_set()
tailscale._run = fake_run
tailscale.cli_path = lambda: "C:/fake/tailscale.exe"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
import harness  # noqa: E402  (asgi_request only: no I/O, no backend import)

from backend import accounts, admin_api, audit, auth, layout_store, memory_store, server, settings  # noqa: E402

accounts.ensure_initialized(reconcile_mode="skip")
PW = "Admin#Test-2026"
accounts.reset_admin(PW)


def login(user, pw, headers=None):
    r = harness.asgi_request(server.app, "POST", "/api/login", body={"username": user, "password": pw},
                             headers=headers)
    m = re.search(r"tr_session_%d=([^;]+)" % PORT, " ".join(r.header_all("set-cookie")))
    return r, (m.group(1) if m else None)


def call(method, path, body=None, tok=None, headers=None, query=b""):
    h = dict(headers or {})
    if tok:
        h["cookie"] = f"tr_session_{PORT}={tok}"
    return harness.asgi_request(server.app, method, path, body=body, headers=h, query=query)


def e(r):
    j = r.json()
    return (r.status, j.get("error") if isinstance(j, dict) else None)


def audit_events(ev, kind="security"):
    audit.flush(2.0)
    return [x for x in audit.tail(1000, kind) if x.get("ev") == ev]


def cfg_json():
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except OSError:
        return None


def write_cfg(d):
    CFG.write_text(json.dumps(d), encoding="utf-8")
    settings.store().check()                   # the test wrote it by hand: re-read now


r, ADM = login("admin", PW)
C("ADMIN signs in (direct-local, sudo opens)", (r.status, bool(ADM)), (200, True))

# ---- uid paths ----------------------------------------------------------------------------------------
C("uid <-> name", (admin_api.name_of("41444d494e"), admin_api.name_of("573141572f50")), ("ADMIN", "W1AW/P"))
C("bad uids -> None (404): upper-case hex, odd length, non-canonical name, junk",
  [admin_api.name_of(x) for x in ("41444D494E", "41444d494", "6f7031", "zz", "", "41")], [None] * 6)
C("unknown uid -> 404", e(call("PATCH", "/api/admin/users/4e4f424f4459", {"role": "viewer"}, ADM)), (404, "not_found"))

# ---- last_admin under the store lock -------------------------------------------------------------------
try:
    accounts.set_role("ADMIN", "operator")
    got = "no error"
except accounts.AccountError as ex:
    got = ex.code
C("set_role: demoting the only admin -> last_admin", got, "last_admin")
try:
    accounts.delete_user("ADMIN", keep_admin=True)
    got = "no error"
except accounts.AccountError as ex:
    got = ex.code
C("delete_user(keep_admin): deleting the only admin -> last_admin", got, "last_admin")
C("still exactly one admin, unchanged", (accounts.admin_count(), accounts.get("ADMIN").role), (1, "admin"))
g0 = accounts.get("ADMIN").gen
C("set_role to the same role writes nothing (None, gen kept)", (accounts.set_role("ADMIN", "admin"),
                                                                  accounts.get("ADMIN").gen), (None, g0))
accounts.create_user("ADM2", "admin", "Adm2#Test-2026", must_change=False)
r, ADM2 = login("ADM2", "Adm2#Test-2026")
C("ADM2 (admin) signs in", r.status, 200)


def demote_raw(d):                              # a concurrent change the Gate did not see yet
    d["users"]["ADM2"]["role"] = "operator"


accounts.store().mutate(demote_raw)
real_identify = auth.identify
auth.identify = lambda scope: (lambda p: dataclasses.replace(p, role="admin", stored_role="admin") if p else p)(
    real_identify(scope))
try:
    C("race: ADM2 (seen as admin) demotes ADMIN, the last real admin -> 409 last_admin",
      e(call("PATCH", "/api/admin/users/41444d494e", {"role": "operator"}, ADM2)), (409, "last_admin"))
    C("race: ADM2 deletes ADMIN -> 409 last_admin",
      e(call("DELETE", "/api/admin/users/41444d494e", None, ADM2)), (409, "last_admin"))
finally:
    auth.identify = real_identify
C("ADMIN is still an admin after the refused race", accounts.get("ADMIN").role, "admin")
r, ADM = login("admin", PW)

# ---- admin_set_password: default_pw / setup code cleared ---------------------------------------------
accounts.create_user("OP9", "operator", "Op9#Test-2026", must_change=False)
h = accounts.hash_password("Reset#Op9-2026")
u9 = accounts.admin_set_password("OP9", h, must_change=True)
C("admin_set_password: must_change as given, gen bumped, verifies", (u9.must_change, accounts.verify(u9.hash, "Reset#Op9-2026")[0]),
  (True, True))

# ---- reauth ---------------------------------------------------------------------------------------------
C("reauth with a wrong password -> 401", e(call("POST", "/api/admin/reauth", {"password": "nope-nope"}, ADM)),
  (401, "invalid_credentials"))
r = call("POST", "/api/admin/reauth", {"password": PW}, ADM)
C("reauth -> {ok, expires_in: 600}", (r.status, r.json().get("expires_in")), (200, 600))

# ---- T40 / T31: allowed hosts ------------------------------------------------------------------------
for raw, want in (("Radio-PC", ("name", "radio-pc")), ("https://PC.tailnet.ts.net", ("origin", "https://pc.tailnet.ts.net:443")),
                  ("http://radio-pc:8790", ("origin", "http://radio-pc:8790")), ({"value": "radio.lan"}, ("name", "radio.lan"))):
    C(f"parse_entry({raw!r})", admin_api.parse_entry(raw), want)
BAD = ["localhost", "radio.localhost", "127.0.0.1", "http://127.0.0.1:80", "[::1]", "http://[::1]:80", "::1",
       "radio-pc:8080", "1.2.3", "ftp://radio-pc", "https://radio-pc/path", "https://radio-pc:99999", "bad_name",
       "-x.example", "", 5, None]
bad_ok = []
for raw in BAD:
    try:
        admin_api.parse_entry(raw)
        bad_ok.append(raw)
    except ValueError:
        pass
C("parse_entry refuses IPs, localhost, *.localhost, names with ports, junk", bad_ok, [])
for entries in (["localhost"], ["radio.localhost"], ["127.0.0.1"], ["http://127.0.0.1:80"]):
    C(f"T40 PUT {entries} -> 400 invalid", e(call("PUT", "/api/admin/allowed_hosts", {"entries": entries}, ADM)),
      (400, "invalid"))
C("nothing was written by the refused PUTs", cfg_json(), None)
lan = {"host": f"radio-pc:{PORT}", "origin": f"http://radio-pc:{PORT}"}
C("T31: empty list, Host radio-pc, PUT [other.example] -> 400 would_lock_out",
  e(call("PUT", "/api/admin/allowed_hosts", {"entries": ["other.example"]}, ADM, headers=lan)), (400, "would_lock_out"))
r = call("PUT", "/api/admin/allowed_hosts", {"entries": ["radio-pc", "RADIO-PC", "https://pub.example"]}, ADM, headers=lan)
C("PUT [radio-pc, RADIO-PC, https://pub.example] from radio-pc -> ok, lowercased + deduped",
  (r.status, r.json().get("entries")), (200, [{"value": "radio-pc", "kind": "name"},
                                              {"value": "https://pub.example:443", "kind": "origin"}]))
C("stored in config.json", cfg_json().get("allowed_hosts"), ["radio-pc", "https://pub.example:443"])
proxied = {"host": f"radio-pc:{PORT}", "origin": "https://pub.example"}   # a proxy that rewrote Host only
C("the allowlisted origin lets a Host-rewriting proxy through",
  call("GET", "/api/admin/allowed_hosts", None, ADM, headers=proxied).status, 200)
C("removing the origin this browser came through -> would_lock_out",
  e(call("PUT", "/api/admin/allowed_hosts", {"entries": ["radio-pc"]}, ADM, headers=proxied)), (400, "would_lock_out"))
g = call("GET", "/api/admin/allowed_hosts", None, ADM, headers=lan).json()
C("GET: entries with kinds, current host and origin, behind_proxy",
  (g["entries"][0], g["current_host"], g["current_origin"], g["behind_proxy"]),
  ({"value": "radio-pc", "kind": "name"}, "radio-pc", f"http://radio-pc:{PORT}", False))
pub = {"host": "pub.example", "origin": "https://pub.example", "x-forwarded-proto": "https"}   # a local TLS proxy
write_cfg({"allowed_hosts": ["pub.example"]})
C("emptying the list from a public name it allowed -> would_lock_out (F21 rule for the empty list)",
  e(call("PUT", "/api/admin/allowed_hosts", {"entries": []}, ADM, headers=pub)), (400, "would_lock_out"))
C("an empty list from 127.0.0.1 is fine", call("PUT", "/api/admin/allowed_hosts", {"entries": []}, ADM).status, 200)
C("33 entries -> 400", e(call("PUT", "/api/admin/allowed_hosts", {"entries": [f"h{i}" for i in range(33)]}, ADM)),
  (400, "invalid"))
write_cfg({})

# final review P2 (test gap): would_lock_out compares the SCHEME too (auth.same_origin, round 3). Each
# browser below has the same host AND port as the request but another scheme, so it passed the Origin
# check only through its allowlisted origin entry: removing that entry must be refused. A host+port-only
# comparison (the pre-round-3 rule) took it for same-origin and saved the list, locking the browser out.
for label, hdrs, keep, origin_entry in (
        ("plain http request, Origin https://radio-pc:PORT",
         {"host": f"radio-pc:{PORT}", "origin": f"https://radio-pc:{PORT}"}, "radio-pc", f"https://radio-pc:{PORT}"),
        ("XFP https from a local proxy, Origin http://pc.tailnet.ts.net:8443",
         {"host": "pc.tailnet.ts.net:8443", "x-forwarded-proto": "https", "origin": "http://pc.tailnet.ts.net:8443"},
         "pc.tailnet.ts.net", "http://pc.tailnet.ts.net:8443")):
    write_cfg({"allowed_hosts": [keep, origin_entry]})
    scope = {"type": "http", "scheme": "http", "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", PORT),
             "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()]}
    C(f"would_lock_out: {label} -> the request is NOT same-origin (scheme differs)",
      auth.same_origin(auth.parse_origin(hdrs["origin"]), scope), False)
    C(f"would_lock_out: {label}, removing its origin entry -> a reason",
      bool(admin_api.would_lock_out([keep], hdrs["host"], hdrs["origin"], scope)), True)
    C(f"PUT [{keep}] ({label}) -> 400 would_lock_out",
      e(call("PUT", "/api/admin/allowed_hosts", {"entries": [keep]}, ADM, headers=hdrs)), (400, "would_lock_out"))
    C(f"... ({label}) config.json unchanged", cfg_json().get("allowed_hosts"), [keep, origin_entry])
    C(f"PUT [{keep}, the origin] ({label}) -> 200 (keeping it is fine)",
      call("PUT", "/api/admin/allowed_hosts", {"entries": [keep, origin_entry]}, ADM, headers=hdrs).status, 200)
# control: the browser whose origin IS the request's (scheme, host and port) may remove that entry
write_cfg({"allowed_hosts": ["radio-pc", f"https://radio-pc:{PORT}"]})
C("control: from http://radio-pc:PORT (exactly this server's origin) removing the https entry -> 200",
  call("PUT", "/api/admin/allowed_hosts", {"entries": ["radio-pc"]}, ADM, headers=lan).status, 200)
C("... saved", cfg_json().get("allowed_hosts"), ["radio-pc"])
write_cfg({})

# ---- T32: Tailscale Serve ON / OFF with the fake CLI -------------------------------------------------
CALLS.clear()
r = call("POST", "/api/admin/tailscale_serve", {}, ADM, headers={"x-forwarded-for": "203.0.113.5"})
C("T32: Serve ON with XFF -> 403 local_only", e(r), (403, "local_only"))
C("... and the CLI was never called", CALLS, [])
before = CFG.read_bytes()
ts_set(serve=(1, "", "serve: permission denied"))
r = call("POST", "/api/admin/tailscale_serve", {}, ADM)
C("T32: Serve ON, CLI exit 1 -> 400 with the CLI text", (e(r), r.json().get("message")),
  ((400, "tailscale_failed"), "serve: permission denied"))
C("T32: ... behind_proxy still false, allowlist unchanged", (settings.behind_proxy(), CFG.read_bytes() == before),
  (False, True))
ts_set(serve=(0, "", ""))                       # exit 0, but `serve status` does not show this port
r = call("POST", "/api/admin/tailscale_serve", {}, ADM)
C("Serve ON exit 0 without serve_running -> 400, nothing written", (e(r), settings.behind_proxy(), CFG.read_bytes() == before),
  ((400, "tailscale_failed"), False, True))
ts_set(serve=(0, "", ""), serve_status=(0, SERVE_ON_TEXT, ""))
r = call("POST", "/api/admin/tailscale_serve", {}, ADM)
C("Serve ON confirmed -> 200; behind_proxy + source + Serve origin written",
  (r.status, settings.behind_proxy(), settings.behind_proxy_source(), cfg_json().get("allowed_hosts")),
  (200, True, "tailscale", ["https://pc.tailnet.ts.net"]))
C("remote_on audited", len(audit_events("remote_on")), 1)
C("every CLI call went to the fake (status, serve status, serve --bg)",
  sorted({tuple(c[:2]) for c in CALLS}), [("serve", "--bg"), ("serve", "status"), ("status", "--json")])
rs = call("GET", "/api/admin/remote_status", None, ADM).json()
C("remote_status: bound port, Serve running, not stale, can_serve_on false behind the proxy",
  (rs["port"], rs["tailscale"]["serve_running"], rs["stale_flag"], rs["can_serve_on"], rs["behind_proxy"]),
  (PORT, True, False, False, True))
ts_set(reset=(1, "", "reset: boom"), serve_status=(0, SERVE_ON_TEXT, ""))
r = call("POST", "/api/admin/tailscale_serve_off", {}, ADM)
C("T32: Serve OFF, CLI exit 1 -> 400; behind_proxy STAYS true (AR-22)", (e(r), settings.behind_proxy()),
  ((400, "tailscale_failed"), True))
ts_set(reset=(0, "", ""), serve_status=(0, SERVE_ON_TEXT, ""))
r = call("POST", "/api/admin/tailscale_serve_off", {}, ADM)
C("Serve OFF exit 0 but Serve still running -> 400, flag kept", (e(r), settings.behind_proxy()),
  ((400, "tailscale_failed"), True))
ts_set(reset=(0, "", ""), serve_status=(1, "", "can't ask"))
r = call("POST", "/api/admin/tailscale_serve_off", {}, ADM)
C("Serve OFF exit 0 but `serve status` fails (unconfirmed) -> 400, flag kept", (e(r), settings.behind_proxy()),
  ((400, "tailscale_failed"), True))
ts_set(reset=(0, "", ""))
r = call("POST", "/api/admin/tailscale_serve_off", {}, ADM)
C("Serve OFF confirmed -> 200, behind_proxy false, source cleared",
  (r.status, settings.behind_proxy(), settings.behind_proxy_source()), (200, False, None))
C("remote_off audited", len(audit_events("remote_off")), 1)

# ---- T74: a stale v1 behind_proxy flag at startup + clear_behind_proxy -----------------------------
write_cfg({"behind_proxy": True})                   # v1 wrote no source
ts_set(serve_status=(0, "No serve config\n", ""))
accounts._state["reconciled"] = False
accounts.ensure_initialized(port=PORT)               # the startup path (reconcile runs synchronously)
C("T74: Serve reported off -> the flag is cleared at startup", settings.behind_proxy(), False)
C("T74: ... audited behind_proxy_cleared (serve_not_running)",
  [x.get("reason") for x in audit_events("behind_proxy_cleared")], ["serve_not_running"])
write_cfg({"behind_proxy": True, "behind_proxy_source": "tailscale"})
ts_set(serve_status=(0, SERVE_ON_TEXT, ""))
accounts._state["reconciled"] = False
accounts.ensure_initialized(port=PORT)
C("T74: Serve on -> the flag is kept at startup", settings.behind_proxy(), True)
r, ADM = login("admin", PW)                          # behind_proxy: no longer direct-local; still admin
C("T74: clear_behind_proxy while Serve runs -> 409 serve_running",
  (e(call("POST", "/api/admin/clear_behind_proxy", {}, ADM)), settings.behind_proxy()), ((409, "serve_running"), True))
ts_set(serve_status=(1, "", "can't ask"))
C("clear_behind_proxy when Serve can't be confirmed off -> 409 serve_status_unknown, kept",
  (e(call("POST", "/api/admin/clear_behind_proxy", {}, ADM)), settings.behind_proxy()), ((409, "serve_status_unknown"), True))
ts_set(serve_status=(0, "No serve config\n", ""))
rs = call("GET", "/api/admin/remote_status", None, ADM).json()
C("remote_status: stale_flag when the flag is set and Serve is confirmed off", rs["stale_flag"], True)
boot = re.search(r'<script id="tr-boot" type="application/json">(.*?)</script>', call("GET", "/", None, ADM).text).group(1)
C("the admin bootstrap carries the behind_proxy_stale notice", "behind_proxy_stale" in json.loads(boot)["notices"], True)
n0 = len(audit_events("behind_proxy_cleared"))
r = call("POST", "/api/admin/clear_behind_proxy", {}, ADM)
C("clear_behind_proxy with Serve off -> 200, cleared, audited", (r.status, settings.behind_proxy(),
                                                                  len(audit_events("behind_proxy_cleared")) - n0), (200, False, 1))

# ---- T74 (final review round 1): a flag set by hand ("manual", REMOTE-ACCESS.md for nginx / socat /
# ssh -R fronts) is never stale and never cleared by TeleRig -------------------------------------------
MANUAL = {"behind_proxy": True, "behind_proxy_source": "manual"}
write_cfg(MANUAL)
manual_bytes = CFG.read_bytes()
n0 = len(audit_events("behind_proxy_cleared"))
real_cli = tailscale.cli_path
tailscale.cli_path = lambda: None                    # Tailscale "not installed" (the usual case behind nginx)
try:
    CALLS.clear()
    accounts._state["reconciled"] = False
    accounts.ensure_initialized(port=PORT)
    C("T74 manual: kept at startup, Tailscale not asked", (settings.behind_proxy(), settings.behind_proxy_source(), CALLS),
      (True, "manual", []))
    r, ADM = login("admin", PW)
    rs = call("GET", "/api/admin/remote_status", None, ADM).json()
    C("T74 manual, Tailscale not installed: remote_status not stale, reported as set by hand",
      (rs["tailscale"]["installed"], rs["behind_proxy"], rs["behind_proxy_source"], rs["stale_flag"], rs.get("behind_proxy_by_hand")),
      (False, True, "manual", False, True))
    boot = re.search(r'<script id="tr-boot" type="application/json">(.*?)</script>', call("GET", "/", None, ADM).text).group(1)
    C("T74 manual: no behind_proxy_stale notice for admins", "behind_proxy_stale" in json.loads(boot)["notices"], False)
    C("T74 manual: serve_stale is False even with a cached 'not installed' status", admin_api.serve_stale(PORT), False)
    r = call("POST", "/api/admin/clear_behind_proxy", {}, ADM)
    C("T74 manual: clear_behind_proxy -> 409 set_by_hand, flag kept", (e(r), settings.behind_proxy(), settings.behind_proxy_source()),
      ((409, "set_by_hand"), True, "manual"))
    C("... the message says it was set by hand in config.json", "set by hand in config.json" in r.json().get("message", ""), True)
finally:
    tailscale.cli_path = real_cli
ts_set(serve_status=(0, "No serve config\n", ""))   # installed, Serve confirmed OFF for this port
rs = call("GET", "/api/admin/remote_status", None, ADM).json()
C("T74 manual, Serve confirmed off: still not stale", (rs["tailscale"]["serve_status_ok"], rs["tailscale"]["serve_running"],
                                                      rs["stale_flag"]), (True, False, False))
C("T74 manual, Serve confirmed off: clear_behind_proxy -> 409 set_by_hand",
  e(call("POST", "/api/admin/clear_behind_proxy", {}, ADM)), (409, "set_by_hand"))
ts_set(reset=(0, "", ""), serve_status=(0, "No serve config\n", ""))
r = call("POST", "/api/admin/tailscale_serve_off", {}, ADM)
C("T74 manual: Turn off (confirmed) -> 200, the manual flag stays", (r.status, r.json().get("behind_proxy"),
                                                                     settings.behind_proxy(), settings.behind_proxy_source()),
  (200, True, True, "manual"))
C("T74 manual: config.json never rewritten, nothing audited as cleared",
  (CFG.read_bytes() == manual_bytes, len(audit_events("behind_proxy_cleared")) - n0), (True, 0))
# A hand edit that lands after the view was read but before the write wins (decided under the store lock).
write_cfg({"behind_proxy": True, "behind_proxy_source": "tailscale"})
CFG.write_text(json.dumps(MANUAL, indent=4), encoding="utf-8")   # by hand, behind the cache's back
hand = CFG.read_bytes()
C("clear_managed_behind_proxy re-reads under the lock: a manual flag on disk is left alone, file untouched",
  (settings.clear_managed_behind_proxy(), settings.behind_proxy_source(), CFG.read_bytes() == hand), (False, "manual", True))
write_cfg({"behind_proxy": True, "behind_proxy_source": "tailscale"})
C("clear_managed_behind_proxy clears a tailscale flag", (settings.clear_managed_behind_proxy(), settings.behind_proxy(),
                                                          settings.behind_proxy_source()), (True, False, None))
C("... and with no flag set there is nothing to clear", settings.clear_managed_behind_proxy(), False)
C("no real Tailscale call was ever made (every call recorded by the fake)", all(isinstance(c, list) for c in CALLS), True)
write_cfg({})
r, ADM = login("admin", PW)

# ---- audio pins with fake devices (F22) -----------------------------------------------------------------
DEVS = {"available": True, "inputs": [{"id": 3, "name": "Microphone (USB Audio CODEC)"}, {"id": 4, "name": "Line In"}],
        "outputs": [{"id": 0, "name": "Speakers (USB Audio CODEC)"}]}
server.audio.list_devices = lambda: DEVS
server.audio.available = lambda: True
a = call("GET", "/api/admin/audio", None, ADM).json()
C("GET audio: devices, F22 defaults, suggestions, no pins",
  (a["available"], [d["name"] for d in a["inputs"]], a["defaults"], a["suggested"], a["pins"]),
  (True, ["Microphone (USB Audio CODEC)", "Line In"],
   {"rx": "Microphone (USB Audio CODEC)", "tx": "Speakers (USB Audio CODEC)"},
   {"rx": "Microphone (USB Audio CODEC)", "tx": "Speakers (USB Audio CODEC)"}, {}))
C("PUT audio: unknown radio -> 400", e(call("PUT", "/api/admin/audio", {"radio": "nope", "rx": None, "tx": None}, ADM)),
  (400, "invalid"))
r = call("PUT", "/api/admin/audio", {"radio": "ft991a", "rx": "No Such Mic", "tx": None}, ADM)
C("PUT audio: unknown device -> 400 field rx", (e(r), r.json().get("field")), ((400, "invalid"), "rx"))
r = call("PUT", "/api/admin/audio", {"radio": "ft991a", "rx": "Line In", "tx": "Speakers (USB Audio CODEC)"}, ADM)
C("PUT audio: pins stored (an output with device id 0 is valid)", (r.status, settings.host_audio().get("ft991a")),
  (200, {"rx": "Line In", "tx": "Speakers (USB Audio CODEC)"}))
C("audio_pinned audited", len(audit_events("audio_pinned")), 1)


class FakeRouter:                                    # a serial radio with capture running
    def __init__(self):
        self.calls, self.rx_running, self.tx_open = [], ("ft991a", "Line In"), None

    def radio_id(self):
        return "ft991a"

    def rx_subs(self):
        return [object()]

    def tx_users(self):
        return []

    def available(self):
        return True

    def resolve(self, d, devs, rid):
        return {"name": None, "source": None, "error": "unpinned"}

    async def refresh_devices(self):
        return DEVS

    async def on_radio_change(self):
        self.calls.append("stop")

    async def after_connect(self):
        self.calls.append("start")


fake = FakeRouter()
real_audio, real_kind = admin_api._ctx.audio, admin_api._ctx.transport_kind
admin_api.bind(audio=fake, transport_kind=lambda: "serial")
try:
    r = call("PUT", "/api/admin/audio", {"radio": "ft991a", "rx": "Microphone (USB Audio CODEC)", "tx": None}, ADM)
    C("a pin change for the connected serial radio restarts the running capture", (r.json().get("restarted"), fake.calls),
      (True, ["stop", "start"]))
    fake.calls.clear()
    r = call("PUT", "/api/admin/audio", {"radio": "ic9700", "rx": None, "tx": None}, ADM)
    C("a pin change for another radio restarts nothing", (r.json().get("restarted"), fake.calls), (False, []))
finally:
    admin_api.bind(audio=real_audio, transport_kind=real_kind)
r = call("PUT", "/api/admin/audio", {"radio": "ft991a", "rx": None, "tx": None}, ADM)
C("PUT audio rx/tx null removes the pin", (r.status, "ft991a" in settings.host_audio()), (200, False))

# ---- T75: the F20 transport pin ---------------------------------------------------------------------
LAN = {"transport": "lan", "host": "192.168.2.40", "port": 50001, "user": "radiouser", "password": "s3cret-pw"}
r = call("PUT", "/api/admin/radio_transport", {"radio": "ic9700", "transport": LAN}, ADM)
C("T75: LAN pin stored without credentials", (r.status, settings.radio_transports().get("ic9700")),
  (200, {"transport": "lan", "host": "192.168.2.40", "port": 50001}))
C("T75: ... the credentials are nowhere in config.json", ("s3cret-pw" in CFG.read_text(), "radiouser" in CFG.read_text()),
  (False, False))
C("radio_transport_pinned audited in audit-tx.log", [x.get("radio") for x in audit_events("radio_transport_pinned", "tx")],
  ["ic9700"])
for body, field in (({"radio": "ic9700", "transport": {"transport": "lan", "host": "bad host!"}}, "transport.host"),
                    ({"radio": "ic9700", "transport": {"transport": "serial", "port": "COM_NOPE_99", "baud": 9600}}, "transport.port"),
                    ({"radio": "ic9700", "transport": {"transport": "serial", "port": "COM3", "baud": True}}, "transport.baud"),
                    ({"radio": "ft991a", "transport": {"transport": "lan", "host": "10.0.0.1"}}, "transport.transport"),
                    ({"radio": "ic9700", "transport": {"transport": "carrier-pigeon"}}, "transport.transport"),
                    ({"radio": "ic9700"}, "transport"), ({"radio": "zz", "transport": None}, "radio")):
    r = call("PUT", "/api/admin/radio_transport", body, ADM)
    C(f"radio_transport {body} -> 400 {field}", (r.status, r.json().get("field")), (400, field))
r = call("PUT", "/api/admin/radio_transport", {"radio": "ic9700", "transport": {"transport": "sim"}}, ADM)
C("a sim pin", settings.radio_transports().get("ic9700"), {"transport": "sim"})
r = call("PUT", "/api/admin/radio_transport", {"radio": "ic9700", "transport": None}, ADM)
C("T75: null clears the pin", (r.status, "ic9700" in settings.radio_transports()), (200, False))

# ---- audit endpoint ---------------------------------------------------------------------------------
for q, field in ((b"limit=abc", "limit"), (b"kind=x", "kind"), (b"format=xml", "format"), (b"limit=-5", "limit")):
    r = call("GET", "/api/admin/audit", None, ADM, query=q)
    C(f"audit ?{q.decode()} -> 400 {field}", (r.status, r.json().get("field")), (400, field))
audit.flush(2.0)
r = call("GET", "/api/admin/audit", None, ADM, query=b"limit=5&kind=security")
j = r.json()
C("audit json: newest first, limit honoured, drop count", (r.status, len(j["entries"]) <= 5, "dropped" in j,
                                                          [x["ts"] for x in j["entries"]] == sorted((x["ts"] for x in j["entries"]), reverse=True)),
  (200, True, True, True))
r = call("GET", "/api/admin/audit", None, ADM, query=b"kind=tx&format=jsonl")
lines = [json.loads(x) for x in r.text.splitlines() if x.strip()]
C("audit jsonl: an attachment of JSON lines (TX log only)",
  (r.status, "attachment" in r.headers.get("content-disposition", ""), "telerig-audit.jsonl" in r.headers.get("content-disposition", ""),
   r.headers.get("content-type", "").startswith("text/plain"), all(x["ev"] in audit.TX_EVENTS for x in lines), len(lines) >= 1),
  (200, True, True, True, True, True))
r = call("GET", "/api/admin/audit", None, ADM, query=b"limit=999999")
C("audit json limit is capped at 1000", (r.status, len(r.json()["entries"]) <= 1000), (200, True))

# ---- layout reset + delete of a user removes the layout and memories files ------------------------
accounts.create_user("OP7", "operator", "Op7#Test-2026", must_change=False)
layout_store.put("OP7", "desktop", {"column": 12, "items": [{"id": "vfo", "x": 0, "y": 0, "w": 4, "h": 5}]}, 0.4)
r = call("DELETE", "/api/admin/users/4f5037/layout", None, ADM)
C("admin layout reset -> {ok, rev}; both devices gone", (r.status, r.json().get("rev"), "desktop" in layout_store.get("OP7")[0]),
  (200, 2, False))
C("layout reset of an unknown user -> 404", e(call("DELETE", "/api/admin/users/4e4f424f4459/layout", None, ADM)), (404, "not_found"))
memory_store.add("OP7", "ic9700", {"hz": 145500000, "mode": "FM", "name": "Calling"})
C("OP7 has a memories file", memory_store.path_for("OP7").exists(), True)
r = call("DELETE", "/api/admin/users/4f5037", None, ADM)
C("delete OP7 -> ok; account and layout file gone", (r.status, accounts.get("OP7"), layout_store.path_for("OP7").exists()),
  (200, None, False))
C("... and the memories file", memory_store.path_for("OP7").exists(), False)
C("user_deleted audited", [x.get("user") for x in audit_events("user_deleted")], ["OP7"])
memory_store.add("OP8", "ic9700", {"hz": 145500000, "mode": "FM", "name": "left behind"})   # no such user (yet)
r = call("POST", "/api/admin/users", {"user": "OP8", "role": "operator", "password": "Op8#Test-2026", "must_change": False}, ADM)
C("a callsign created again starts with no memories: a file left behind is removed",
  (r.status, memory_store.path_for("OP8").exists(), memory_store.get("OP8")), (200, False, ({"rev": 0, "radios": {}, "seeded": []}, False)))

# ---- sessions (no sockets in-process) + lockouts ----------------------------------------------------
s = call("GET", "/api/admin/sessions", None, ADM).json()
C("sessions: none open in-process; the TX snapshot rides along", (s["sessions"], s["tx"]), ([], {"owner": None}))
C("lockouts shape", sorted(call("GET", "/api/admin/lockouts", None, ADM).json()), ["ips", "users"])
C("unlock_ip needs an ip", e(call("POST", "/api/admin/lockouts/unlock_ip", {"ip": 5}, ADM)), (400, "invalid"))
C("unlock_ip", call("POST", "/api/admin/lockouts/unlock_ip", {"ip": "203.0.113.5"}, ADM).status, 200)

C("the fake CLI saw every Tailscale call; none reached the real one", tailscale._run is fake_run, True)
print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
