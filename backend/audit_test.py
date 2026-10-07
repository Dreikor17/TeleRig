"""Audit log unit tests — run: python -m backend.audit_test

Writes only into the scratch config folder (TELERIG_CONFIG must point at a scratch path; the
runner's run_child sets it). I21: TX records go only to audit-tx.log; denial-type events are
coalesced per (connection, event, action) and capped at one security entry per second per
connection, with nothing lost (merged / counted into a later entry).
"""
import os
import time

from backend import audit
from backend.auth import config_dir

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


cfg = os.environ.get("TELERIG_CONFIG", "")
real = [os.path.join(os.path.expanduser("~"), d) for d in (".telerig", ".radio-webop")]
if not cfg or any(os.path.normcase(os.path.abspath(cfg)).startswith(os.path.normcase(r)) for r in real):
    raise SystemExit("REFUSING: audit_test needs a scratch TELERIG_CONFIG")
d = config_dir()
for n in ("audit.log", "audit-tx.log"):
    try:
        (d / n).unlink()
    except OSError:
        pass

audit.log("tx_start", user="OP1", conn=1, kind="ptt", radio="ic9700", freq=145500000, mode="FM")
for i in range(500):
    audit.log("tx_denied", key=2, user="OP2", conn=2, action="ptt", owner="OP1")
for i in range(300):
    audit.log("rate_limited", key=2, user="OP2", conn=2, action="ptt")
audit.log("tx_end", user="OP1", conn=1, kind="ptt", reason="owner", dur_s=1.0)
audit.log("login_ok", user="OP1")
C("flush drains everything (forced: open windows and deferred entries)", audit.flush(5.0), True)
tx = audit.tail(100, "tx")
sec = audit.tail(100, "security")
C("TX events only in audit-tx.log (I21)", sorted(r["ev"] for r in tx), ["tx_end", "tx_start"])
C("no TX events in audit.log", [r for r in sec if r["ev"].startswith("tx_s") or r["ev"] == "tx_end"], [])
den = [r for r in sec if r["ev"] == "tx_denied"]
rl = [r for r in sec if r["ev"] == "rate_limited"]
C("500 tx_denied -> one coalesced entry with count 500", [(r["count"], r["action"]) for r in den], [(500, "ptt")])
C("300 rate_limited -> one coalesced entry with count 300", [r["count"] for r in rl], [300])
C("coalesced entries carry first/last", all("first" in r and "last" in r for r in den + rl), True)

# the per-connection cap (1 security entry / s per key), without forcing: entries wait, then merge
os.environ["TELERIG_SIM_ONLY"] = "1"
os.environ["TELERIG_TEST_AUDIT_WINDOW"] = "0.2"
C("test window honoured with sim-only", audit.coalesce_window(), 0.2)
os.environ["TELERIG_SIM_ONLY"] = ""
C("test window ignored without sim-only", audit.coalesce_window(), 60.0)
os.environ["TELERIG_SIM_ONLY"] = "1"
before = len(audit.tail(1000, "security"))
t_end = time.time() + 3.0
n = {"tx_denied": 0, "denied": 0}
while time.time() < t_end:                        # two event kinds, every few ms, for 3 s
    audit.log("tx_denied", key=9, user="OP9", conn=9, action="ptt")
    audit.log("denied", key=9, user="OP9", conn=9, action="set_freq", reason="busy")
    n["tx_denied"] += 1
    n["denied"] += 1
    time.sleep(0.005)
time.sleep(3.0)                                   # let the windows close and the deferred drain
rows = [r for r in audit.tail(1000, "security")[: len(audit.tail(1000, "security")) - before] if r.get("conn") == 9]
ts = sorted(r["ts"] for r in rows)
C("cap: at most ~1 entry per second for the connection (3 s flood + drain)", len(rows) <= 8, True)
got = {k: sum(r["count"] for r in rows if r["ev"] == k) for k in n}
C("cap: nothing lost (counts merge into the waiting entry)", got, n)
os.environ.pop("TELERIG_TEST_AUDIT_WINDOW", None)
audit.flush(2.0)

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
