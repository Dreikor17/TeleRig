"""Accounts tests — run: python -m backend.accounts_test

Credentials (§2.5), tokens and revocation (§2.7), generations (I19), the auth.json validation
(§2.2), first start / v1 migration and reconcile() (§2.6), reset_admin (§2.20). Everything happens
in a scratch folder: <folder of $TELERIG_CONFIG>/accounts_test-<pid> when the runner sets a scratch
TELERIG_CONFIG, else a fresh system temp folder. No real config is touched; no server, radio or
socket is opened; Tailscale is never called (reconcile gets a fake status function).
"""
import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from backend import accounts, auth, settings
from backend.store import StoreBusy, StoreCorrupt

REPO = Path(__file__).resolve().parent.parent
_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    line = " ".join([("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})"])
    print(line.encode("ascii", "backslashreplace").decode("ascii"))   # any console encoding


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return False


def _base() -> Path:
    env = os.environ.get("TELERIG_CONFIG")
    b = Path(env).resolve().parent / f"accounts_test-{os.getpid()}" if env else Path(tempfile.mkdtemp(prefix="tr-acc-test-"))
    for real in (Path.home() / ".telerig", Path.home() / ".radio-webop"):
        if str(b.resolve()).lower().startswith(str(real.resolve()).lower()):
            raise SystemExit(f"REFUSING: scratch folder {b} is inside the real {real}")
    b.mkdir(parents=True, exist_ok=True)
    return b


BASE = _base()
SAVED_ENV = os.environ.get("TELERIG_CONFIG")
_n = 0


def use_dir(name: str) -> Path:
    """A fresh config folder; every module re-resolves its store there."""
    global _n
    _n += 1
    d = BASE / f"{_n:02d}-{name}"
    d.mkdir(parents=True, exist_ok=True)
    os.environ["TELERIG_CONFIG"] = str(d / "config.json")
    os.environ.pop("RADIO_WEBOP_CONFIG", None)
    auth._reset_for_tests()
    settings._reset_for_tests()
    accounts._reset_for_tests()
    return d


def v1_hash(pw: str) -> str:
    salt = secrets.token_bytes(16)
    return base64.b64encode(salt + hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)).decode()


def scope(cookie=None, host="127.0.0.1:8790", peer="127.0.0.1", headers=()):
    h = [(b"host", host.encode())]
    if cookie is not None:
        h.append((b"cookie", cookie if isinstance(cookie, bytes) else cookie.encode("latin-1")))
    h += [(k.encode(), v.encode()) for k, v in headers]
    return {"type": "http", "headers": h, "client": (peer, 50000), "server": ("127.0.0.1", 8790), "scheme": "http"}


def ck(tok):
    return f"tr_session_8790={tok}"


# ---- usernames ------------------------------------------------------------------------------------
for name, want in (("admin", "ADMIN"), ("w1aw/p", "W1AW/P"), ("  N0CALL  ", "N0CALL"), ("ab", "AB"),
                   ("a-b_c/9", "A-B_C/9"), ("x" * 32, "X" * 32), ("N0CALL\n", "N0CALL")):
    C(f"canonical({name!r})", accounts.canonical(name), want)
for name in ("a", "x" * 33, "--", "/_", "N0 CALL", "ÄB", "ab$", "", None, 5, "ab\ncd", "a١"):
    C(f"canonical({name!r}) rejected", accounts.canonical(name), None)
C("uid(ADMIN) is hex of UTF-8", accounts.uid("ADMIN"), "41444d494e")
C("uid(W1AW/P) has no slash", "/" in accounts.uid("W1AW/P"), False)

# ---- new-password rules ---------------------------------------------------------------------------
P = accounts.password_problem
C("7 bytes refused", P("a" * 7) is not None, True)
C("8 bytes accepted", P("a1b2c3d4"), None)
C("72 bytes accepted", P("a" * 72), None)
C("73 bytes refused", P("a" * 73) is not None, True)
C("multi-byte: 36 x é = 72 bytes accepted", P("é" * 36), None)
C("multi-byte: 37 x é = 74 bytes refused", P("é" * 37) is not None, True)
for bad in ("admin", "Admin", "PASSWORD", "password1", "12345678", "changeme", "telerig", "radiowebop", "LetMeIn1"):
    if len(bad.encode()) >= 8 or bad.lower() == "admin":
        C(f"denylist {bad!r} refused", P(bad) is not None, True)
C("same as the callsign (any case) refused", P("w1aw/p-x", user="W1AW/P-X") is not None, True)
C("same as the current password refused", P("Hunter#222", current="Hunter#222") is not None, True)
C("different from current accepted", P("Hunter#223", current="Hunter#222"), None)
C("non-string refused", P(12345678) is not None, True)
C("lone surrogate refused, no exception", P("abcdefg\ud800") is not None, True)
C("temp password: 12 chars from the temp alphabet",
  all(len(t) == 12 and set(t) <= set(accounts.TEMP_ALPHABET) for t in (accounts.temp_password() for _ in range(20))), True)
C("setup code: 12 chars from the setup alphabet (no I, O, 0, 1)",
  all(accounts.SETUP_RE.fullmatch(c) and not set(c) & set("IO01") for c in (accounts.new_setup_code() for _ in range(50))), True)
C("normalize_setup", (accounts.normalize_setup("k7qm-4xpa 9trb"), accounts.normalize_setup(None)), ("K7QM4XPA9TRB", ""))
C("format_setup", accounts.format_setup("K7QM4XPA9TRB"), "K7QM-4XPA-9TRB")

# ---- hashing ----------------------------------------------------------------------------------------
h = accounts.hash_password("Correct#Horse1")
C("hash format scrypt$32768$8$1$salt$dk", h.split("$")[:4], ["scrypt", "32768", "8", "1"])
C("verify ok, no rehash at current params", accounts.verify(h, "Correct#Horse1"), (True, False))
C("verify wrong password", accounts.verify(h, "Correct#Horse2")[0], False)
h16 = accounts.hash_password("Correct#Horse1", n=16384)
C("needs_rehash for N=16384", accounts.verify(h16, "Correct#Horse1"), (True, True))
for bad in (None, "", "x", "scrypt$1$8$1$AAAA$AAAA", "scrypt$1000$8$1$" + h.split("$", 4)[4],
            "scrypt$262144$8$1$" + h.split("$", 4)[4], "scrypt$32768$99$1$" + h.split("$", 4)[4],
            "scrypt$32768$8$1$!!!$AAAA", "bcrypt$32768$8$1$AAAA$AAAA", "scrypt$+32768$8$1$" + h.split("$", 4)[4], 12):
    C(f"verify({str(bad)[:30]!r}) -> (False, False), no exception", accounts.verify(bad, "x"), (False, False))
C("verify with a non-string password -> (False, False)", accounts.verify(h, None), (False, False))
C("dummy hash uses the current params", accounts.dummy_hash().split("$")[1], "32768")

# ---- v1 conversion ------------------------------------------------------------------------------------
vh = v1_hash("hunter22")
v2 = accounts.v1_to_v2(vh)
C("v1 -> v2 keeps the v1 params", v2.split("$")[:4], ["scrypt", "16384", "8", "1"])
C("v1 conversion verifies the old password (and asks for a rehash)", accounts.verify(v2, "hunter22"), (True, True))
C("v1 conversion: a wrong password fails", accounts.verify(v2, "hunter23")[0], False)
C("v1 malformed (not base64) -> ValueError", raises(lambda: accounts.v1_to_v2("not base64!"), ValueError), True)
C("v1 wrong length (47 bytes) -> ValueError",
  raises(lambda: accounts.v1_to_v2(base64.b64encode(b"x" * 47).decode()), ValueError), True)
C("v1 non-string -> ValueError", raises(lambda: accounts.v1_to_v2(12), ValueError), True)

# ---- first start (M2) ---------------------------------------------------------------------------------
d = use_dir("m2")
accounts.ensure_initialized(reconcile_mode="skip")
a = json.loads((d / "auth.json").read_text())
C("M2: version 2 store", (a["version"], len(a["secret"]), a["meta"]["gen_seq"], a["meta"]["revoked"]), (2, 64, 1, {}))
C("M2: ADMIN default_pw + must_change, not migrated",
  {k: a["users"]["ADMIN"][k] for k in ("role", "gen", "must_change", "default_pw", "migrated_v1")},
  {"role": "admin", "gen": 1, "must_change": True, "default_pw": True, "migrated_v1": False})
C("M2: setup code set", bool(accounts.SETUP_RE.fullmatch(a["meta"]["setup_code"] or "")), True)
C("M2: the default password verifies", accounts.verify(a["users"]["ADMIN"]["hash"], "admin")[0], True)
C("M2: config.json still absent", (d / "config.json").exists(), False)
C("M2: no config.v1.bak.json without a config.json", (d / "config.v1.bak.json").exists(), False)
before = (d / "auth.json").read_bytes()
accounts.ensure_initialized(reconcile_mode="skip")
C("ensure_initialized is idempotent (no rewrite)", (d / "auth.json").read_bytes() == before, True)
C("setup_matches: dashed + lowercase forms",
  (accounts.setup_matches(accounts.format_setup(a["meta"]["setup_code"]).lower()),
   accounts.setup_matches(a["meta"]["setup_code"]), accounts.setup_matches("AAAAAAAAAAAA"),
   accounts.setup_matches(None), accounts.setup_matches("")), (True, True, False, False, False))
C("banner_info", {k: v for k, v in accounts.banner_info().items() if k != "setup_code"},
  {"admin": True, "default_pw": True, "migrated_v1": False, "admins": 1})

# ---- tokens -----------------------------------------------------------------------------------------
tok, exp = accounts.make_token("ADMIN", 1)
t = accounts.parse_token(tok)
C("token round trip", (t.user, t.gen, t.exp), ("ADMIN", 1, exp))
C("token expiry is fixed 30 days", abs(exp - (time.time() + 30 * 86400)) < 5, True)
C("token <= 512 bytes", len(tok) <= 512, True)
parts = tok.split(".")


def with_part(i, v):
    p = list(parts)
    p[i] = v
    return ".".join(p)


flip = lambda s: s[:-1] + ("A" if s[-1] != "A" else "B")
C("tampered user -> None", accounts.parse_token(with_part(1, accounts._b64u(b"OP1"))), None)
C("tampered gen -> None", accounts.parse_token(with_part(2, "2")), None)
C("tampered exp -> None", accounts.parse_token(with_part(3, str(exp + 1))), None)
C("tampered signature -> None", accounts.parse_token(with_part(4, flip(parts[4]))), None)
C("wrong version tag -> None", accounts.parse_token(with_part(0, "v1")), None)
C("leading-zero gen (another spelling) -> None", accounts.parse_token(with_part(2, "01")), None)
# the last base64url char of a 32-byte signature carries 2 unused bits: a different character can
# decode to the same bytes. Such a respelling must not parse (else it would dodge revocation).
alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
last = parts[4][-1]
alts = [c for c in alpha if c != last and alpha.index(c) >> 2 == alpha.index(last) >> 2]
C("there are respellings of the signature that decode to the same bytes",
  all(base64.urlsafe_b64decode(parts[4][:-1] + c + "=") == base64.urlsafe_b64decode(parts[4] + "=") for c in alts), True)
C("a non-canonical base64 respelling -> None", [accounts.parse_token(with_part(4, parts[4][:-1] + c)) for c in alts],
  [None] * len(alts))
C("padded base64 -> None", accounts.parse_token(with_part(4, parts[4] + "=")), None)
old, _ = accounts.make_token("ADMIN", 1, now=time.time() - 31 * 86400)
C("expired -> None", accounts.parse_token(old), None)
for bad in (None, "", 5, "1999999999.abcdef", "v2.QURNSU4.1", tok + "." + "x", "v2.é.1.2.3",
            "x" * 600, "v2." + tok[3:].replace(".", "..", 1), "v2.QURNSU4.0." + parts[3] + "." + parts[4]):
    C(f"malformed token {str(bad)[:24]!r} -> None", accounts.parse_token(bad), None)
C("token_fp is 16 hex", bool(accounts.FP_RE.fullmatch(accounts.token_fp(tok))), True)
burst = [accounts.make_token("ADMIN", 1) for _ in range(5)]
C("tokens issued in the same second are all different (one fingerprint per session)",
  len({accounts.token_fp(t) for t, _ in burst}), 5)
C("... their expiry stays ~30 days ahead", all(abs(e - (time.time() + 30 * 86400)) < 30 for _, e in burst), True)
C("... and every one parses", all(accounts.parse_token(t) for t, _ in burst), True)

# identify(): no loopback branch; cookie per bound port; non-ASCII cookie
C("identify: the per-port cookie signs in", auth.identify(scope(ck(tok))).user, "ADMIN")
C("identify: restricted while must_change", auth.identify(scope(ck(tok))).restricted, True)
C("identify: no cookie from loopback -> None (I1)", auth.identify(scope()), None)
C("identify: another port's cookie name -> None", auth.identify(scope(f"tr_session_8700={tok}")), None)
C("identify: legacy tr_session cookie -> None", auth.identify(scope(f"tr_session={tok}")), None)
C("identify: non-ASCII cookie bytes -> None, no exception",
  auth.identify(scope(b"tr_session_8790=v2.\xc3\xa9\xff.1.2.3")), None)
C("identify: garbage cookie header -> None", auth.identify(scope(b";;==;tr_session_8790")), None)

# ---- generations (I19) ----------------------------------------------------------------------------
name, temp = accounts.create_user("op1", "operator", "Op1#Password", must_change=False)
C("create_user canonicalises and returns no temp when a password is given", (name, temp), ("OP1", None))
g1 = accounts.get("OP1").gen
C("create: gen = ++gen_seq", (g1, accounts.meta()["gen_seq"]), (2, 2))
t1, _ = accounts.make_token("OP1", g1)
C("OP1 token works", auth.identify(scope(ck(t1))).role, "operator")
accounts.delete_user("OP1")
C("deleted user -> identify None", auth.identify(scope(ck(t1))), None)
C("delete never lowers gen_seq", accounts.meta()["gen_seq"], 2)
accounts.create_user("OP1", "operator", "Op1#Password", must_change=False)
g2 = accounts.get("OP1").gen
C("re-created user gets a NEW gen", g2 > g1, True)
C("the old token stays invalid after re-creation (T51 unit)", auth.identify(scope(ck(t1))), None)
C("create_user duplicate -> exists", raises(lambda: accounts.create_user("op1", "viewer"), accounts.AccountError), True)
C("create_user bad name -> invalid", raises(lambda: accounts.create_user("a", "viewer"), accounts.AccountError), True)
C("create_user bad role -> invalid", raises(lambda: accounts.create_user("OK2", "root"), accounts.AccountError), True)
C("create_user weak password -> invalid",
  raises(lambda: accounts.create_user("OK2", "viewer", "password"), accounts.AccountError), True)
n3, temp3 = accounts.create_user("V1", "viewer")
C("create_user without a password: temp password, must_change by default",
  (len(temp3), accounts.get("V1").must_change, accounts.verify(accounts.get("V1").hash, temp3)[0]), (12, True, True))
t2, _ = accounts.make_token("OP1", g2)
accounts.bump_gen("OP1")
C("bump_gen (sign out everywhere) invalidates the old token", auth.identify(scope(ck(t2))), None)

# set_password clears flags and the setup code
admin_tok, _ = accounts.make_token("ADMIN", accounts.get("ADMIN").gen)
u = accounts.set_password("admin", "Brand#New-2026")
C("set_password: flags cleared, gen bumped",
  (u.must_change, u.default_pw, u.migrated_v1, u.gen > 1), (False, False, False, True))
C("set_password: setup_code cleared once no default password is left", accounts.setup_code(), None)
C("set_password: the old token is invalid", auth.identify(scope(ck(admin_tok))), None)
C("set_password: the new password verifies", accounts.verify(u.hash, "Brand#New-2026")[0], True)

# ---- revocation (T53 unit) -------------------------------------------------------------------------
def gen_of(n):
    return accounts.get(n).gen


def gens():
    return {n: x.gen for n, x in accounts.users().items()}


def logout(tok):
    """What POST /api/logout does with the signed-in principal's token."""
    t = accounts.parse_token(tok)
    return accounts.revoke(accounts.token_fp(tok), t.exp, t.user, t.gen)


def works(tok):
    p = auth.identify(scope(ck(tok)))
    return p.user if p else None


tok3, exp3 = accounts.make_token("ADMIN", gen_of("ADMIN"))
fp3 = accounts.token_fp(tok3)
C("before logout: the token works", works(tok3), "ADMIN")
C("logout lists the token", logout(tok3), "listed")
C("revoked fingerprint", accounts.is_revoked(fp3), True)
C("identify rejects a revoked token", auth.identify(scope(ck(tok3))), None)
C("revoke refuses a bad fingerprint", raises(lambda: accounts.revoke("zz", 1, "ADMIN", 1), accounts.AccountError), True)
C("revoke refuses a non-canonical user",
  raises(lambda: accounts.revoke("a" * 16, exp3, "admin", gen_of("ADMIN")), accounts.AccountError), True)
stale_tok, _ = accounts.make_token("V1", gen_of("V1"))
accounts.bump_gen("V1")
before, g_before = accounts.meta()["revoked"], gens()
C("a token that is already dead (older gen) is not listed", logout(stale_tok), "dead")
C("... the list and every gen are unchanged", (accounts.meta()["revoked"] == before, gens() == g_before), (True, True))

# -- overflow (finding A3-R1-P0): only the signing-out user ever pays --------------------------------
def fill(dd, keep_expired=True):
    """Saturate the list with entries nobody in this process knows the owner of (as after ~2000
    logouts by some account, or a restart)."""
    rv = dd["meta"]["revoked"]
    now = int(time.time())
    for i in range(accounts.REVOKED_MAX - len(rv) - (1 if keep_expired else 0)):
        rv[f"{i:016x}"] = now + 86400 * 20 + i
    if keep_expired:
        rv["ffffffffffff0000"] = now - 5                      # already expired: pruned on the next write


accounts.store().mutate(fill)
C("the list is saturated (2000 entries, one already expired)", len(accounts.meta()["revoked"]), accounts.REVOKED_MAX)
tok4, _ = accounts.make_token("V1", gen_of("V1"))
g0 = gens()
C("a logout into the slot of an expired entry is listed", logout(tok4), "listed")
rv = accounts.meta()["revoked"]
C("expired entries are pruned", "ffffffffffff0000" in rv, False)
C("... the list is full again (2000)", len(rv), accounts.REVOKED_MAX)
C("... and nobody's gen moved", gens(), g0)

# three live sessions of different users; then consecutive logouts while the list is full
op_a, _ = accounts.make_token("OP1", gen_of("OP1"))
op_b, _ = accounts.make_token("OP1", gen_of("OP1"))
v1_live, _ = accounts.make_token("V1", gen_of("V1"))
ad_live, _ = accounts.make_token("ADMIN", gen_of("ADMIN"))
hook_seen = []


def hook(old, new):
    for u in accounts.diff(old, new).users:
        hook_seen.append((u, accounts.revocation_bump(u, ((new.get("users") or {}).get(u) or {}).get("gen"))))


accounts.add_listener(hook)
g1 = gens()
C("full list: OP1's logout bumps OP1 instead of listing", logout(op_a), "bumped")
g2 = gens()
C("... only OP1's gen moved", {n: g2[n] > g1[n] for n in g1}, {n: n == "OP1" for n in g1})
C("... the list did not grow and kept every unknown-owner entry",
  (len(accounts.meta()["revoked"]), set(rv) <= set(accounts.meta()["revoked"])), (accounts.REVOKED_MAX, True))
C("... the signed-out token is dead", works(op_a), None)
C("... OP1's other session ended too (sign out everywhere, fail safe)", works(op_b), None)
C("... V1 and ADMIN sessions are untouched", (works(v1_live), works(ad_live)), ("V1", "ADMIN"))
C("... the reload hook is told it was a revocation bump (OP1 only)", hook_seen, [("OP1", True)])
op_c, _ = accounts.make_token("OP1", gen_of("OP1"))
hook_seen.clear()
C("second consecutive logout at saturation: bumps OP1 again", logout(op_c), "bumped")
g3 = gens()
C("... and no other user (no global bump, no list reset)", {n: g3[n] > g2[n] for n in g2}, {n: n == "OP1" for n in g2})
C("... the unrelated sessions still work", (works(v1_live), works(ad_live)), ("V1", "ADMIN"))
C("... hook: OP1 only, as a revocation bump", hook_seen, [("OP1", True)])
op_d, _ = accounts.make_token("OP1", gen_of("OP1"))
hook_seen.clear()
C("third logout, by V1: bumps V1 only", logout(v1_live), "bumped")
g4 = gens()
C("... OP1 and ADMIN keep their gens", {n: g4[n] > g3[n] for n in g3}, {n: n == "V1" for n in g3})
C("... OP1's new session survives V1's logout", works(op_d), "OP1")
hook_seen.clear()
accounts.bump_gen("ADMIN")
C("an ordinary gen bump (sign out everywhere) is NOT reported as a revocation bump", hook_seen, [("ADMIN", False)])
C("revocation_bump outside a revoke write is False", accounts.revocation_bump("OP1", gen_of("OP1")), False)
accounts._listeners.remove(hook)

# -- the per-user share and the owner index (in memory) ---------------------------------------------------
d = use_dir("revoke-share")
accounts.ensure_initialized(reconcile_mode="skip")
accounts.create_user("K1ABC", "viewer", "K1abc#Password", must_change=False)
accounts.create_user("OP2", "operator", "Op2#Password", must_change=False)
op2_old, _ = accounts.make_token("OP2", gen_of("OP2"))
C("OP2 logout listed", logout(op2_old), "listed")
k_tokens = [accounts.make_token("K1ABC", gen_of("K1ABC"))[0] for _ in range(accounts.REVOKED_PER_USER + 1)]
res = [logout(t) for t in k_tokens[:accounts.REVOKED_PER_USER]]
C(f"one user's first {accounts.REVOKED_PER_USER} logouts are listed", set(res), {"listed"})
C("... the list holds them plus OP2's", len(accounts.meta()["revoked"]), accounts.REVOKED_PER_USER + 1)
gk, go, ga = gen_of("K1ABC"), gen_of("OP2"), gen_of("ADMIN")
C(f"logout #{accounts.REVOKED_PER_USER + 1} of the same user is a bump instead (one account cannot fill the list)",
  logout(k_tokens[-1]), "bumped")
C("... only that user's gen moved", (gen_of("K1ABC") > gk, gen_of("OP2"), gen_of("ADMIN")), (True, go, ga))
C("... that user's entries are dropped (dead now); OP2's entry stays",
  sorted(accounts.meta()["revoked"]), [accounts.token_fp(op2_old)])
C("... and every one of that user's tokens stays dead", [works(t) for t in k_tokens], [None] * len(k_tokens))
accounts.set_password("OP2", "Op2#Password-2")              # OP2's listed token is dead by gen now
k_new, _ = accounts.make_token("K1ABC", gen_of("K1ABC"))
C("the next write prunes listed entries whose user has a newer gen", (logout(k_new), sorted(accounts.meta()["revoked"])),
  ("listed", [accounts.token_fp(k_new)]))
accounts._rv_owner.clear()                                  # as after a restart: no owners known
accounts.store().mutate(lambda dd: fill(dd, keep_expired=False))
full = dict(accounts.meta()["revoked"])
ad_tok, _ = accounts.make_token("ADMIN", gen_of("ADMIN"))
k_live, _ = accounts.make_token("K1ABC", gen_of("K1ABC"))
ga = gen_of("ADMIN")
C("after a restart, a full list of unknown owners: ADMIN's logout bumps ADMIN", logout(ad_tok), "bumped")
C("... nobody else (K1ABC's session works) and every unknown entry is kept",
  (works(k_live), accounts.meta()["revoked"] == full, gen_of("ADMIN") > ga), ("K1ABC", True, True))

# ---- the change hook diff (reload kicks) --------------------------------------------------------------
o = {"secret": "a", "meta": {"limiter_reset": 0, "revoked": {"aaaaaaaaaaaaaaaa": 1}},
     "users": {"A": {"gen": 1}, "B": {"gen": 2}, "C": {"gen": 3}}}
nw = {"secret": "a", "meta": {"limiter_reset": 1, "revoked": {"aaaaaaaaaaaaaaaa": 1, "bbbbbbbbbbbbbbbb": 2}},
      "users": {"A": {"gen": 1}, "B": {"gen": 5}, "D": {"gen": 6}}}
ch = accounts.diff(o, nw)
C("diff: changed/removed users", sorted(ch.users), ["B", "C"])
C("diff: newly revoked", sorted(ch.revoked), ["bbbbbbbbbbbbbbbb"])
C("diff: limiter_reset changed", ch.limiter_reset, True)
C("diff: same secret", ch.secret, False)
C("diff: a fresh store (other secret) is flagged", accounts.diff(o, dict(nw, secret="b")).secret, True)
C("diff: first seed (old empty) is not 'secret changed'", accounts.diff({}, nw).secret, False)

# ---- validation (§2.2): every violation -> corrupt -----------------------------------------------------
good = json.loads((accounts.path()).read_text())
C("the live store validates", raises(lambda: accounts.validate(good), Exception), False)


def mutated(fn):
    x = json.loads(json.dumps(good))
    fn(x)
    return x


bad_cases = {
    "version 1": lambda x: x.update(version=1),
    "short secret": lambda x: x.update(secret="ab"),
    "upper-case secret": lambda x: x.update(secret="AB" * 32),
    "limiter_reset negative": lambda x: x["meta"].update(limiter_reset=-1),
    "limiter_reset bool": lambda x: x["meta"].update(limiter_reset=True),
    "gen_seq below a user's gen": lambda x: x["meta"].update(gen_seq=1),
    "setup_code lower-case": lambda x: x["meta"].update(setup_code="k7qm4xpa9trb"),
    "setup_code with O": lambda x: x["meta"].update(setup_code="O7QM4XPA9TRB"),
    "revoked bad key": lambda x: x["meta"].update(revoked={"xyz": 1}),
    "revoked str expiry": lambda x: x["meta"].update(revoked={"aaaaaaaaaaaaaaaa": "1"}),
    "revoked > 2000": lambda x: x["meta"].update(revoked={f"{i:016x}": 1 for i in range(2001)}),
    "user key not canonical": lambda x: x["users"].update(admin=x["users"].pop("ADMIN")),
    "unknown role": lambda x: x["users"]["ADMIN"].update(role="root"),
    "gen 0": lambda x: x["users"]["ADMIN"].update(gen=0),
    "bool as string": lambda x: x["users"]["ADMIN"].update(must_change="false"),
    "timestamp number": lambda x: x["users"]["ADMIN"].update(last_login=5),
    "hash garbage": lambda x: x["users"]["ADMIN"].update(hash="plain"),
    "users not an object": lambda x: x.update(users=[]),
}
for label, fn in bad_cases.items():
    C(f"validate rejects: {label}", raises(lambda: accounts.validate(mutated(fn)), ValueError), True)
C("zero admins is allowed", raises(lambda: accounts.validate(mutated(lambda x: x["users"]["ADMIN"].update(role="viewer"))), Exception), False)

# a corrupt file: status corrupt, ensure_initialized raises, bytes untouched (locked mode, I6)
d = use_dir("corrupt")
(d / "auth.json").write_text(json.dumps(mutated(bad_cases["version 1"])))
raw = (d / "auth.json").read_bytes()
C("corrupt auth.json: status", accounts.status(), "corrupt")
C("corrupt auth.json: ensure_initialized raises StoreCorrupt", raises(lambda: accounts.ensure_initialized(reconcile_mode="skip"), StoreCorrupt), True)
C("corrupt auth.json: parse_token raises StoreCorrupt (the caller locks)", raises(lambda: accounts.parse_token(tok), StoreCorrupt), True)
C("corrupt auth.json: bytes unchanged", (d / "auth.json").read_bytes(), raw)

# ---- reset_admin (§2.20) ---------------------------------------------------------------------------------
res = accounts.reset_admin("Reset#Pass-2026", move_corrupt=True)
C("reset_admin repairs: corrupt file moved aside", Path(res["backup"]).read_bytes(), raw)
C("reset_admin: fresh store with ADMIN", (res["created"], sorted(accounts.users())), (True, ["ADMIN"]))
a = accounts.get("ADMIN")
C("reset_admin: ADMIN flags", (a.role, a.must_change, a.default_pw, a.migrated_v1), ("admin", False, False, False))
C("reset_admin: limiter_reset bumped, setup_code null", (accounts.meta()["limiter_reset"], accounts.setup_code()), (1, None))
accounts.create_user("W1AW", "admin", "W1aw#Password", must_change=False)
accounts.create_user("K2X", "admin", "K2x#Password", must_change=False)
g_w = accounts.get("W1AW").gen
C("other_admins", accounts.other_admins(), ["K2X", "W1AW"])
res = accounts.reset_admin("Reset#Pass-2027", demote=("w1aw",))
C("reset_admin --demote: W1AW demoted, K2X kept", (res["demoted"], accounts.get("W1AW").role, accounts.get("K2X").role),
  (["W1AW"], "operator", "admin"))
C("reset_admin --demote: the demoted admin's gen is bumped", accounts.get("W1AW").gen > g_w, True)
C("reset_admin: limiter_reset bumped again", accounts.meta()["limiter_reset"], 2)
C("reset_admin: new password verifies", accounts.verify(accounts.get("ADMIN").hash, "Reset#Pass-2027")[0], True)
accounts.delete_user("ADMIN")
gs = accounts.meta()["gen_seq"]
accounts.reset_admin("Reset#Pass-2028")
C("reset_admin after ADMIN was deleted: re-created with gen > every earlier gen", accounts.get("ADMIN").gen, gs + 1)

# record_login is best-effort and gives up quickly while another process holds the lock
HOLD = ("import sys, time; sys.path.insert(0, %r)\nfrom backend.store import JsonStore\n"
        "s = JsonStore(sys.argv[1])\nwith s.hold_lock():\n    print('LOCKED', flush=True)\n    time.sleep(60)\n") % str(REPO)
pr = subprocess.Popen([sys.executable, "-c", HOLD, str(accounts.path())], stdout=subprocess.PIPE, text=True)
C("lock holder ready", pr.stdout.readline().strip(), "LOCKED")
t0 = time.monotonic()
C("record_login while the lock is held -> StoreBusy", raises(lambda: accounts.record_login("ADMIN", timeout=0.3), StoreBusy), True)
C("... quickly (short lock wait)", time.monotonic() - t0 < 2.0, True)
C("reads still work while the lock is held", accounts.get("ADMIN").role, "admin")
pr.kill()
pr.wait(timeout=30)
accounts.record_login("ADMIN", old_hash="x", new_hash="y")
C("record_login sets last_login; a stale old_hash never swaps the hash",
  (accounts.get("ADMIN").last_login is not None, accounts.get("ADMIN").hash != "y"), (True, True))

# ---- v1 migration (M1) ------------------------------------------------------------------------------------
d = use_dir("m1")
cfg_bytes = json.dumps({"session_secret": "cd" * 32, "password_hash": v1_hash("hunter22")}, indent=2).encode()
(d / "config.json").write_bytes(cfg_bytes)
accounts.ensure_initialized(reconcile_mode="skip")
a = accounts.get("ADMIN")
C("M1: ADMIN keeps the v1 password, no forced change, migrated_v1",
  (accounts.verify(a.hash, "hunter22")[0], a.must_change, a.default_pw, a.migrated_v1, a.created_by),
  (True, False, False, True, "migration"))
C("M1: no setup code", accounts.setup_code(), None)
C("M1: config.v1.bak.json is a copy", (d / "config.v1.bak.json").read_bytes(), cfg_bytes)
C("M1: config.json byte-identical", (d / "config.json").read_bytes(), cfg_bytes)
mt, mexp = accounts.make_token("ADMIN", a.gen)
pr_remote = auth.identify(scope(ck(mt), headers=(("x-forwarded-for", "198.51.100.20"),)))
pr_local = auth.identify(scope(ck(mt)))
C("F17: migrated ADMIN off the host PC = operator (effective), admin stored",
  (pr_remote.role, pr_remote.stored_role, pr_remote.migrated_remote), ("operator", "admin", True))
C("F17: migrated ADMIN direct-local = admin", (pr_local.role, pr_local.migrated_remote), ("admin", False))
C("banner_info for M1", accounts.banner_info()["migrated_v1"], True)

d = use_dir("m1-bad")
(d / "config.json").write_text(json.dumps({"password_hash": "not-a-v1-hash"}))
C("malformed v1 password_hash -> StoreCorrupt (refuse to seed)",
  raises(lambda: accounts.ensure_initialized(reconcile_mode="skip"), StoreCorrupt), True)
C("... and no auth.json was written", (d / "auth.json").exists(), False)

d = use_dir("cfg-corrupt")
(d / "config.json").write_bytes(b'{"allowed_hosts": [')
C("unparsable config.json + no auth.json -> StoreCorrupt",
  raises(lambda: accounts.ensure_initialized(reconcile_mode="skip"), StoreCorrupt), True)
C("... nothing seeded (no auth.json, no lock file)", sorted(p.name for p in d.iterdir()), ["config.json"])

# ---- reconcile (§2.6, AR-34) with a fake Tailscale ---------------------------------------------------------
calls = []


def fake(st):
    def f(port):
        calls.append(port)
        return st
    return f


def recon_case(label, cfg, st, want_bp, want_called=True):
    d = use_dir("recon")
    (d / "config.json").write_text(json.dumps(cfg))
    accounts.ensure_initialized(reconcile_mode="skip")
    calls.clear()
    accounts.reconcile(8790, ts_status=fake(st))
    C(f"reconcile: {label}", (settings.behind_proxy(), bool(calls)), (want_bp, want_called))


OFF = {"installed": True, "serve_status_ok": True, "serve_running": False}
recon_case("Serve off (status ok) clears a tailscale-set flag", {"behind_proxy": True, "behind_proxy_source": "tailscale"}, OFF, False)
recon_case("Serve off clears a v1 flag without a source", {"behind_proxy": True}, OFF, False)
recon_case("serve status failed -> flag kept",
           {"behind_proxy": True, "behind_proxy_source": "tailscale"}, dict(OFF, serve_status_ok=False), True)
recon_case("Serve running -> flag kept", {"behind_proxy": True}, dict(OFF, serve_running=True), True)
recon_case("not installed + source tailscale -> cleared",
           {"behind_proxy": True, "behind_proxy_source": "tailscale"}, {"installed": False}, False)
recon_case("not installed + no source -> kept", {"behind_proxy": True}, {"installed": False}, True)
recon_case("a manual flag is never touched (Tailscale not even asked)",
           {"behind_proxy": True, "behind_proxy_source": "manual"}, OFF, True, want_called=False)
recon_case("flag off: Tailscale not asked", {}, OFF, False, want_called=False)


d = use_dir("recon-race")
(d / "config.json").write_text(json.dumps({"behind_proxy": True, "behind_proxy_source": "tailscale"}))
accounts.ensure_initialized(reconcile_mode="skip")


def hand_edit_then_off(port):                      # the user sets "manual" while Tailscale is asked
    (d / "config.json").write_text(json.dumps({"behind_proxy": True, "behind_proxy_source": "manual"}))
    return OFF


accounts.reconcile(8790, ts_status=hand_edit_then_off)
C("reconcile: a hand edit to 'manual' that lands mid-reconcile wins (decided under the store lock)",
  (settings.behind_proxy(), settings.behind_proxy_source()), (True, "manual"))


def boom(port):
    raise RuntimeError("cli exploded")


d = use_dir("recon-err")
(d / "config.json").write_text(json.dumps({"behind_proxy": True}))
accounts.ensure_initialized(reconcile_mode="skip")
accounts.reconcile(8790, ts_status=boom)
C("reconcile: a CLI error leaves the flag alone", settings.behind_proxy(), True)
d = use_dir("recon-origin")
(d / "config.json").write_text(json.dumps({"behind_proxy": True, "behind_proxy_source": "manual",
                                           "allowed_hosts": ["pc.tail1.ts.net", "radio-pc"]}))
accounts.ensure_initialized(reconcile_mode="skip")
accounts.reconcile(8790, ts_status=fake(OFF))
C("reconcile: behind a proxy, a bare *.ts.net name gains its https origin (I15)",
  settings.allowed_hosts(), ("pc.tail1.ts.net", "radio-pc", "https://pc.tail1.ts.net:443"))

# ---- restore --------------------------------------------------------------------------------------------
if SAVED_ENV is None:
    os.environ.pop("TELERIG_CONFIG", None)
else:
    os.environ["TELERIG_CONFIG"] = SAVED_ENV
auth._reset_for_tests()
settings._reset_for_tests()
accounts._reset_for_tests()
if _ok:
    shutil.rmtree(BASE, ignore_errors=True)
else:
    print(f"scratch kept at {BASE}")
print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
