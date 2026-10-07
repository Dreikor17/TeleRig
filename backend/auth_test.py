"""auth.py tests — run: python -m backend.auth_test

host_ok with the F21 empty-allowlist rule (T57 unit), direct_local / local_reason (§2.8), the one
trust rule for forwarding headers (client_ip, scheme), cookie parsing, perms_for and the sudo
window (§2.18). Scratch config folders only (see accounts_test); no server, socket or radio.
"""
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from backend import accounts, auth, settings

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    line = " ".join([("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})"])
    print(line.encode("ascii", "backslashreplace").decode("ascii"))


env = os.environ.get("TELERIG_CONFIG")
BASE = Path(env).resolve().parent / f"auth_test-{os.getpid()}" if env else Path(tempfile.mkdtemp(prefix="tr-auth-test-"))
for real in (Path.home() / ".telerig", Path.home() / ".radio-webop"):
    if str(BASE.resolve()).lower().startswith(str(real.resolve()).lower()):
        raise SystemExit(f"REFUSING: scratch folder {BASE} is inside the real {real}")
SAVED = {k: os.environ.get(k) for k in ("TELERIG_CONFIG", "TELERIG_SIM_ONLY", "TELERIG_TEST_SUDO_TTL")}
_n = 0


def use_config(cfg: dict = None) -> Path:
    global _n
    _n += 1
    d = BASE / f"{_n:02d}"
    d.mkdir(parents=True, exist_ok=True)
    if cfg is not None:
        (d / "config.json").write_text(json.dumps(cfg))
    os.environ["TELERIG_CONFIG"] = str(d / "config.json")
    os.environ.pop("RADIO_WEBOP_CONFIG", None)
    auth._reset_for_tests()
    settings._reset_for_tests()
    accounts._reset_for_tests()
    return d


def sc(host="127.0.0.1:8790", peer="127.0.0.1", scheme="http", **hdrs):
    h = [] if host is None else [(b"host", host.encode())]
    h += [(k.replace("_", "-").encode(), v.encode("latin-1")) for k, v in hdrs.items()]
    return {"type": "http", "headers": h, "client": (peer, 50000), "server": ("127.0.0.1", 8790), "scheme": scheme}


# ---- host_ok (F21) ------------------------------------------------------------------------------
use_config()
for h in ("radio-pc:8790", "radio-pc", "radio.local", "pc.tail1.ts.net:8790", "box.lan", "x.home.arpa",
          "svc.internal", "localhost:8790", "127.0.0.1:8790", "[::1]:8790", "192.168.1.5", "RADIO-PC"):
    C(f"host_ok, empty allowlist: {h}", auth.host_ok(h), True)
for h in ("evil.example", "evil.example:8790", "radio.example.com", "foo.localhost", "a.b.c.d.e", "",
          None, "radio-pc:99999", "radio-pc:abc", "::1", "x.local.evil.example", "radio-pc."):
    C(f"host_ok, empty allowlist refuses: {h}", auth.host_ok(h), False)
C("the 403 text names Admin -> Access and the bound port",
  "localhost:8790 → Admin → Access" in auth.host_denied_text("evil.example:8790", sc()), True)
use_config({"allowed_hosts": ["radio.example.com", "https://pc.tailnet.ts.net"]})
C("allowlist set: listed name ok", auth.host_ok("radio.example.com"), True)
C("allowlist set: an origin entry's hostname ok", auth.host_ok("pc.tailnet.ts.net"), True)
C("allowlist set: unlisted LAN name now refused (v1 rule)", auth.host_ok("radio-pc"), False)
C("allowlist set: localhost and IPs always ok", (auth.host_ok("localhost"), auth.host_ok("10.1.2.3")), (True, True))

# ---- direct_local / local_reason (§2.8) ---------------------------------------------------------------
use_config()
C("direct_local: loopback + Host 127.0.0.1", auth.direct_local(sc()), True)
C("direct_local: loopback + Host localhost", auth.direct_local(sc("localhost:8790")), True)
C("direct_local: loopback + Host [::1]", auth.direct_local(sc("[::1]:8790", peer="::1")), True)
C("direct_local: IPv4-mapped loopback peer", auth.direct_local(sc(peer="::ffff:127.0.0.1")), True)
C("direct_local: LAN peer", auth.direct_local(sc(peer="192.168.1.20")), False)
C("direct_local: Host by name", auth.direct_local(sc("radio-pc:8790")), False)
C("direct_local: no Host", auth.direct_local(sc(None)), False)
for hdr in ("x_forwarded_for", "x_forwarded_host", "x_forwarded_proto", "forwarded", "via", "x_real_ip",
            "tailscale_user_login", "cf_connecting_ip", "true_client_ip"):
    C(f"direct_local: {hdr} present -> False", auth.direct_local(sc(**{hdr: "x"})), False)
C("local_reason: direct -> None", auth.local_reason(sc()), None)
C("local_reason: forwarded header", auth.local_reason(sc(via="1.1 proxy")), "forwarded_header")
C("local_reason: Host by name", auth.local_reason(sc("radio-pc:8790")), "host_not_local")
C("local_reason: non-loopback peer -> None (only meaningful on loopback)", auth.local_reason(sc(peer="10.0.0.9")), None)
use_config({"behind_proxy": True, "behind_proxy_source": "manual"})
C("direct_local: behind_proxy -> False", auth.direct_local(sc()), False)
C("local_reason: behind_proxy first", auth.local_reason(sc(via="x")), "behind_proxy")

# ---- client_ip / scheme: forwarding headers only from a loopback peer -------------------------------------
use_config()
C("client_ip: peer", auth.client_ip(sc(peer="192.0.2.7")), "192.0.2.7")
C("client_ip: right-most XFF from loopback", auth.client_ip(sc(x_forwarded_for="1.2.3.4, 203.0.113.9")), "203.0.113.9")
C("client_ip: XFF ignored from a non-loopback peer", auth.client_ip(sc(peer="192.0.2.7", x_forwarded_for="1.2.3.4")), "192.0.2.7")
C("client_ip: XFF IPv6 normalised", auth.client_ip(sc(x_forwarded_for="2001:DB8::1")), "2001:db8::1")
C("client_ip: junk XFF kept as an opaque key", auth.client_ip(sc(x_forwarded_for="not-an-ip")), "xff:not-an-ip")
C("client_ip: loopback XFF stays loopback", auth.client_ip(sc(x_forwarded_for="127.0.0.9")), "127.0.0.9")
C("scheme: X-Forwarded-Proto https from loopback", auth.scheme(sc(x_forwarded_proto="https")), "https")
C("scheme: X-Forwarded-Proto ignored from a LAN peer", auth.scheme(sc(peer="10.0.0.2", x_forwarded_proto="https")), "http")
C("scheme: junk X-Forwarded-Proto ignored", auth.scheme(sc(x_forwarded_proto="gopher")), "http")
C("scheme: wss -> https", auth.scheme(sc(scheme="wss")), "https")
C("bound_port names the cookie", (auth.bound_port(sc()), auth.cookie_name(sc())), (8790, "tr_session_8790"))

# ---- cookies ------------------------------------------------------------------------------------------------
C("cookie: picks the named one", auth.cookie(sc(cookie="a=1; tr_session_8790=TOK; b=2"), "tr_session_8790"), "TOK")
C("cookie: quoted value", auth.cookie(sc(cookie='tr_session_8790="TOK"'), "tr_session_8790"), "TOK")
C("cookie: prefix names do not match", auth.cookie(sc(cookie="tr_session_87900=X"), "tr_session_8790"), None)
C("cookie: non-ASCII bytes never raise", auth.cookie(sc(cookie="tr_session_8790=\xe9\xff"), "tr_session_8790"), "\xe9\xff")
C("cookie: none", auth.cookie(sc(), "tr_session_8790"), None)

# ---- perms -----------------------------------------------------------------------------------------------------
C("perms viewer", auth.perms_for("viewer"), ["view", "listen", "menu_read"])
C("perms operator adds operate/tx/connect/menu", auth.perms_for("operator")[3:], ["operate", "tx", "connect", "menu"])
C("perms admin adds admin powers", auth.perms_for("admin")[7:], ["admin", "menu_critical", "switch_radio", "audio_pin", "remote"])
C("perms unknown -> none", auth.perms_for("root"), [])
C("rank fails closed", (auth.rank("viewer"), auth.rank("admin"), auth.rank(None), auth.rank("Admin")), (0, 2, -1, -1))

# ---- sudo (§2.18) --------------------------------------------------------------------------------------------------
P = auth.Principal("ADMIN", "admin", "admin", 3, False, False, False, "aaaaaaaaaaaaaaaa", 0)
os.environ.pop("TELERIG_SIM_ONLY", None)
os.environ["TELERIG_TEST_SUDO_TTL"] = "1"
C("sudo TTL override ignored without --sim-only", auth.sudo_ttl(), 600.0)
os.environ["TELERIG_SIM_ONLY"] = "1"
C("sudo TTL override honoured with --sim-only", auth.sudo_ttl(), 1.0)
C("sudo closed before opening", auth.sudo_active(P), False)
auth.sudo_open(P)
C("sudo open", auth.sudo_active(P), True)
C("sudo is bound to the generation", auth.sudo_active(auth.Principal("ADMIN", "admin", "admin", 4, False, False, False,
                                                                     "aaaaaaaaaaaaaaaa", 0)), False)
time.sleep(1.1)
C("sudo expires", auth.sudo_active(P), False)
auth.sudo_open(P)
auth.sudo_drop(P.token_fp)
C("sudo_drop closes it", auth.sudo_active(P), False)
auth.sudo_open(P)
auth.sudo_drop_user("ADMIN")
C("sudo_drop_user closes it", auth.sudo_active(P), False)
M = auth.Principal("ADMIN", "operator", "admin", 3, False, False, True, "bbbbbbbbbbbbbbbb", 0)
auth.sudo_open(M)
C("sudo never opens for a migrated-remote session (F17)", auth.sudo_active(M), False)

for k, v in SAVED.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v
auth._reset_for_tests()
settings._reset_for_tests()
accounts._reset_for_tests()
if _ok:
    shutil.rmtree(BASE, ignore_errors=True)
print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
