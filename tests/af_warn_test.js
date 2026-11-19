/* AF (the radio's own speaker) asks before it leaves 0: the REAL tr.js + app.js in a Node vm (tests/pttenv.js
 * on tests/fakedom.js). No browser, no server, no radio: the "server" is a fake socket; set_level frames
 * are only recorded. Run by tests/t_c1.py (unit):
 *
 *     node tests/af_warn_test.js        -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  F1. AF at 0, dragged up: nothing is sent while dragging; on release the page asks; "Keep it at 0"
 *      puts the slider back at 0 and sends nothing
 *  F2. "Turn it up": the value released at is sent (even if a state frame came in while asking), then
 *      AF moves freely; back at 0 the next raise asks again
 *  F3. AF already up at the radio: no question; other levels (RF) never ask
 *  F4. the radio's AF back at 0 (a reconnect sets it; turned down elsewhere): the next raise asks again,
 *      in a browser that already said "Turn it up"
 */
"use strict";
const { pageEnv } = require("./pttenv");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

function env() {
  const e = pageEnv(0, {
    before(e) {
      for (const id of ["af", "rf"]) {
        const el = e.el("input", { id, type: "range", min: "0", max: "255", step: "1" });
        el.value = "0";
        e.el("b", { id: id + "Val" }).textContent = "0";
      }
    },
  });
  e.asked = [];
  e.answer = null;                                           // resolve the question later: e.reply(true|false)
  e.ctx.TR.confirm = (text, opts) => new Promise((res) => { e.asked.push([text, opts && opts.ok, opts && opts.cancel]); e.reply = res; });
  e.state = (o) => e.frame(Object.assign({ type: "state", connected: true, radio: "ic9700", freq: 144200000, mode_name: "USB",
    filter: 1, main: { freq: 144200000 }, sub: { freq: 0 }, active_band: "main", af: 0, rf: 200 }, o || {}));
  e.levels = (t) => e.sock.sent.filter((f) => f.action === "set_level" && f.target === t).map((f) => f.value);
  e.drag = (t, v) => { const el = e.doc.getElementById(t); el.value = String(v); el.dispatchEvent(new e.Event("input", { bubbles: true })); e.adv(40); };
  e.release = (t) => { e.doc.getElementById(t).dispatchEvent(new e.Event("change", { bubbles: true })); };
  e.val = (t) => [e.doc.getElementById(t).value, e.doc.getElementById(t + "Val").textContent];
  return e;
}
const settle = async (e) => { for (let i = 0; i < 4; i++) await e.flush(); e.adv(40); };

(async function main() {
  // F1
  {
    const e = env();
    e.state({ af: 0 });
    e.drag("af", 20); e.drag("af", 40);
    eq(e.levels("af"), [], "F1 dragging AF up from 0 sends nothing");
    e.release("af"); await settle(e);
    eq(e.asked.length, 1, "F1 on release the page asks");
    check(/speaker on the radio itself/.test(e.asked[0][0]) && e.asked[0][1] === "Turn it up" && e.asked[0][2] === "Keep it at 0",
          "F1 ... in plain words, with Turn it up / Keep it at 0: " + JSON.stringify(e.asked[0]));
    e.reply(false); await settle(e);
    eq([e.val("af"), e.levels("af")], [["0", "0"], []], "F1 Keep it at 0: the slider is back at 0, nothing sent");
  }
  // F2
  {
    const e = env();
    e.state({ af: 0 });
    e.drag("af", 90); e.release("af"); await settle(e);
    e.state({ af: 0 });                                       // a panel re-read while the question is open
    e.reply(true); await settle(e);
    eq(e.levels("af"), [90], "F2 Turn it up: the value released at is sent");
    eq(e.val("af")[0], "90", "F2 ... and stays on the slider");
    e.state({ af: 90 });
    e.drag("af", 120); e.release("af"); await settle(e);
    eq([e.asked.length, e.levels("af").slice(-1)[0]], [1, 120], "F2 then AF moves freely (no new question)");
    e.drag("af", 0); e.release("af"); await settle(e);
    e.state({ af: 0 });
    eq(e.levels("af").slice(-1)[0], 0, "F2 back to 0 is sent");
    e.drag("af", 30); e.release("af"); await settle(e);
    eq(e.asked.length, 2, "F2 ... and the next raise from 0 asks again");
    e.reply(false); await settle(e);
  }
  // F3
  {
    const e = env();
    e.state({ af: 140 });                                     // turned up at the radio
    e.drag("af", 100); e.release("af"); await settle(e);
    eq([e.asked.length, e.levels("af").slice(-1)[0]], [0, 100], "F3 AF already up at the radio: no question");
    e.drag("rf", 10); e.release("rf"); await settle(e);
    eq([e.asked.length, e.levels("rf").slice(-1)[0]], [0, 10], "F3 other levels never ask");
  }
  // F4
  {
    const e = env();
    e.state({ af: 0 });
    e.drag("af", 90); e.release("af"); await settle(e);
    e.reply(true); await settle(e);
    e.state({ af: 90 });                                      // the radio is up
    e.state({ connected: false, af: 90 });                    // a disconnect ...
    e.state({ af: 0 });                                       // ... and a new connect: AF back at 0
    e.drag("af", 60); e.release("af"); await settle(e);
    eq(e.asked.length, 2, "F4 after a reconnect (AF back at 0) the next raise asks again");
    e.reply(false); await settle(e);
    eq([e.val("af")[0], e.levels("af")], ["0", [90]], "F4 ... Keep it at 0: back at 0, nothing more sent");
    e.state({ af: 0 });
    e.drag("af", 75); e.release("af"); await settle(e);
    e.reply(true); await settle(e);
    e.state({ af: 75 });
    e.state({ af: 0 });                                       // turned down at the radio, or by another browser
    e.drag("af", 40); e.release("af"); await settle(e);
    eq(e.asked.length, 4, "F4 turned down to 0 elsewhere: the next raise asks again");
    e.reply(false); await settle(e);
  }
  console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
  process.exit(failures ? 1 : 0);
})().catch((err) => { console.log("FAIL (crash) " + (err && err.stack || err)); process.exit(1); });
