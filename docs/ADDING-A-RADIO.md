# Adding a radio — and the transmit‑safety contract

TeleRig is **profile‑driven**: each model is a `RadioProfile` in
`backend/profiles.py`, driven by a protocol handler (`Radio` = Icom CI‑V,
`YaesuRadio` = Yaesu CAT). Adding a model of an existing make is usually just a new
profile; a new make/protocol is a new handler class exposing the same method surface the
server dispatches (`set_freq` / `set_mode` / `set_level` / `set_ptt` / …).

## ⚠️ Transmit‑safety contract — MANDATORY for any radio that can key TX

Remote operation puts a real transmitter on the air over a network, so **every keyed path
needs a backstop**. A radio is **not "done"** until every applicable item below is wired —
or explicitly marked N/A with a reason. When in doubt, **fail safe** (don't transmit / unkey).

1. **Operator‑triggered only — no *unattended* transmission.** Every keyed path is the
   direct result of an operator action *now*, by a signed‑in operator or admin: **PTT** (press =
   key, press again = unkey), **VOX**, **TUNE** and the **CW message send** (one bounded message
   per TX press, which the operator can stop). What is **not** shipped is transmission the app *originates on
   its own*: auto‑CQ on a timer, beacons, scheduled/unattended keying — those would need
   explicit local‑control safeguards and are never an implicit feature. (Hard rule for the
   assistant maintaining this repo: it wires operator‑triggered transmit, but never
   triggers or tests a transmission itself — on‑air testing is the operator's.)
2. **PTT stuck‑TX failsafe (120 s), in the radio handler.** Keying arms `PTT_TIMEOUT`; the poll
   loop auto‑unkeys past the deadline, the client shows a countdown and unkeys too, and
   **disconnect unkeys**. Mirror `Radio.set_ptt`, the `_poll` failsafe, and the `disconnect`
   unkey. Every `set_ptt(True)` (and an Icom VOX‑on) re‑arms the deadline, so a handler must
   never see repeated start calls; the TX arbiter (item 3) guarantees that.
3. **One TX owner, and who is unkeyed when a browser goes away (the server's TX arbiter,
   `backend/txarb.py`).** Every keyed path (PTT, VOX, the CW message, TUNE) is a *claim* by one
   browser connection. At most one connection (or the radio's own front panel) owns TX; anyone
   else gets `busy`. The handler gets each start call **once**: re‑sending a claim the owner
   already holds is a no‑op that never reaches the radio, so it can't push the 120 s deadline
   back. While keyed:
   - **the TX owner's drop unkeys immediately** (its WebSocket closes). Whatever ends an app
     owner's session (its drop, a STOP, a heartbeat stall, a radio change, a locked server) sends
     the unkey for the PTT / CW the app keyed **without** first checking `state.ptt`: a TX poll
     answer already in flight when the key went out can read "receive" for one poll period;
   - **a silent owner is unkeyed after 6 s** (no `tx_hb` heartbeat; the page sends one every
     2 s, and a heartbeat never touches the radio);
   - **other clients' drops never unkey** — a viewer closing a tab doesn't cut the operator off;
   - **a front‑panel or physical‑mic key is never unkeyed by a browser drop.** It shows as
     FRONT PANEL and only an explicit STOP stops it. This holds only for radios that report their
     TX state (Yaesu CAT: the `TX;` poll; Icom CI‑V: the `1C 00` read) — see the note below. TX that shows up to 5 s
     after a session ended is handed back to that session's browser only when the radio itself
     ended its PTT or CW (a misread poll, the CW break‑in hang); after a TUNE (its 20 s claim
     lapsed), a VOX session or a release it is FRONT PANEL;
   - **the arbiter's own 120 s session clock:** a session holding `ptt` or `vox` is
     force‑released 120 s after it started (a monotonic clock that a new claim does not restart),
     whatever the radio's deadline says. A CW message is capped at its duration + 5 s (≤ 300 s)
     and TUNE at 20 s. The handler's failsafe (item 2) stays as a second, independent layer;
   - **only an explicit STOP stops someone else's transmission**: `tx_stop`, from the two‑tap
     STOP in the page or `POST /api/tx_stop`. A non‑owner's `ptt false`, `cw_stop` or VOX‑off
     gets `busy`, so one stray tap can't cut another operator off;
   - **viewers can never key**: only operators and admins may claim TX, and every TX session,
     forced stop and critical menu write is recorded in `audit-tx.log`.

   **Reporting TX state.** Both handlers poll the radio's own TX status about 3 times a second
   and put it in `state.ptt` (status only; reading never keys): the Yaesu `TX;` (`TX1` CAT,
   `TX2` the radio itself) and the Icom `1C 00` (`00` receive, `01` transmit; an echo of the
   app's own `1C 00 xx`, from the controller address, is not an answer). So a front‑panel or
   hand‑mic key becomes the FRONT PANEL owner on both, and nobody (a browser, a WSJT‑X bridge
   program) can key over it. The handler's PTT failsafe unkeys at its deadline whatever the last
   read said, so a lost or stale read never keeps a carrier up. The exception is the IC‑728: it
   has no CI‑V TX command, so its front panel stays invisible (it is also transmit‑less in
   TeleRig). A new handler must state which case it is: if it can report TX, put it in
   `state.ptt`; if it can't, document the limitation.
4. **Auto‑keying paths bound to the same failsafe.** Any mode that can key TX off audio
   (VOX) arms the same deadline, is a `vox` claim in the arbiter, and is dropped on
   disconnect.
5. **Hardware TOT set on connect.** Set the radio's own time‑out timer to ≈120 s (closest
   the radio supports) as a backstop for **control‑link loss** — if the network drops
   mid‑transmit the app failsafe can't fire, but the rig's own timer will. The profile
   carries the command; `N/A` if the radio doesn't expose it over the control link (then
   the app‑level failsafe is the only backstop — note it). Mark the radio's TX TOT menu item
   `tx_tot=True` (and `critical=True`): writing **OFF** then needs an admin **and** an explicit
   confirmation, decided by comparing the encoded bytes with the encoding of OFF.
6. **CW message send is bounded + stoppable** (if `cw_send` is set). The handler keys one
   bounded message (Icom: the rig's keyer via CI‑V `17`; FT‑991A: host‑timed DTR on its
   Standard USB port). Cap the message length, only fire in a CW mode, arm an auto‑stop
   deadline (`_cw_deadline`, the poll stops it — Icom `17 FF`), expose a stop, and stop on
   disconnect. The arbiter treats it as a `cw` claim (the owner's drop stops it). The hardware
   TOT (item 5) is the hard backstop.
7. **PTT routing is flagged.** Mark every menu item that routes PTT or keying to a serial
   control line (its options name **RTS** or **DTR**: PTT SELECT, PC KEYING, USB SEND/Keying)
   `tx_route=True` (and `critical=True`). TeleRig holds the CAT serial port open with RTS/DTR
   asserted (pyserial's default, which CAT relies on), so routing PTT to one of those lines can
   key the radio outside the arbiter. Choosing an RTS/DTR value therefore needs an admin, an
   **idle** arbiter (nobody on the air) and an explicit confirmation, and is audited.
8. **High‑SWR cutoff + warning.** While keyed, read the SWR meter; warn in the UI and
   auto‑unkey above the threshold to protect the PA. Reads + a protective un‑key only.
9. **Power read — not forced — on connect.** The app **reads and displays** the radio's
   current RF power (and AF/RF/SQL) on connect so the panel mirrors the rig; it does **not**
   zero power. (Earlier builds forced 0 % on connect — removed by request so the operator's
   real settings show. Transmit stays guarded by operator‑only keying, the 120 s PTT
   failsafe, the hardware TOT, and unkey‑on‑disconnect.)
10. **Unkey + restore on disconnect.** Never leave the radio keyed; restore any borrowed
    state (e.g. the MOD source on the Icom LAN path).

Items 1–7 and 10 ship today. Item 8 (high‑SWR cutoff) is **not implemented in any handler
yet** — it stays a requirement for new radios and an open item for the existing ones. Item 9 is
read‑and‑display (power is no longer forced to 0). The profile fields make every safety feature
inherit‑by‑filling for future radios.

## Completeness — expose *every* setting the radio has

**Goal: the web UI should let the operator do everything they could do standing at the radio.**
Not just frequency/mode/levels — every operating control and every menu item. Treat the manuals
as the checklist, in two layers:

1. **SET / MENU items** → the data‑driven **Settings** tab. Compile the *complete* menu table
   into `backend/menus/<id>_menu.py` (one `MenuItem` per entry, every group), not a subset.
2. **Operating (front‑panel / FUNCTION‑menu) controls** → real controls in the main tabs, each
   backed by a CAT/CI‑V command. These are the ones most often missed because they are *not* in
   the SET menu. Go through the manual's FUNCTION/operating sections and the CAT reference's full
   command list and wire **all** of them, gating each to the modes it applies to. For the Yaesu
   radios this includes, e.g.: **NAR/WIDE** (`NA`), **WIDTH** (`SH`), **CONTOUR/APF** (`CO`),
   **IF‑SHIFT** (`IS`), **CTCSS/DCS tone mode** (`CT`), **tone/DCS number** (`CN`), **repeater
   shift** (`OS`), **MONITOR** (`ML`), **break‑in/keyer/speed/pitch/spot** (`BI`/`KR`/`KS`/`KP`/`CS`),
   **TXW** (`TS`), **scan/memory/VFO‑MEM** (`SC`/`MC`/`VM`), etc.

How to be exhaustive and not miss things:

- **Audit the manual against the code.** Extract the CAT/CI‑V reference and the operating manual to
  text (`pymupdf`/`fitz` — the Read tool can't rasterize PDFs here), list *every* command and
  operating setting, and diff against what the handler + UI already expose. Anything reachable over
  the control link but not in the UI is a gap to close. (This repo keeps such an audit as a
  workflow; see the FT‑991A gap list in the changelog history.)
- **Gate by capability + mode.** Add a `Capabilities` flag for each optional control family (e.g.
  `narrow`, `fm_tone`) so the UI shows it only on radios that have it, and hide mode‑specific
  controls when out of mode (Tone/DCS only in FM, WIDTH/CONTOUR only in SSB/CW/RTTY/DATA, APF/keyer
  only in CW). Adaptive UI beats a wall of dead buttons.
- **Never ship a dead control.** If a button/slider exists in the HTML, its handler must be real
  (not a `_noop`). A stub that silently does nothing is worse than an absent control.
- **Verify a sample on the real radio** (read → echo) before trusting new commands, and remember
  the transmit‑safety contract above for anything that can key TX (`KY` keyer playback, `MX` MOX).

## The profile is the declarative source of truth

A `RadioProfile` describes *everything* about reaching a radio and what it can do, and the UI
renders/gates itself from it:

- **`transports`** (`Transports`) — serial bits/parity/stopbits, RS‑BA1 network + UDP ports,
  audio kind (`usb-codec`/`lan`/`none`), scope kind (`native`/`audio`/`none`).
- **`capabilities`** (`Capabilities`) — `preamp`/`att`/`tuner`/`dual_watch`/`vfo_select`/
  `rx_dsp`/`tx_funcs`/`meters`/… The UI **shows only what `capabilities` reports.**
- **`menu`** — a declarative `MenuItem` list driving the data‑driven **Setup tab**.

When `transports`/`capabilities` are omitted they are **synthesized from the flat `has_*`
flags in `__post_init__`**, so you only spell them out where the radio differs (e.g. the
FT‑991A has no CAT active‑VFO selector → `vfo_select=False`).

## The WSJT-X bridge and a new radio

The bridge ([WSJTX-BRIDGE.md](WSJTX-BRIDGE.md)) lets WSJT-X and other Hamlib programs use a radio
through TeleRig. It needs nothing from the handler: every bridge command becomes the canonical
command a browser sends and goes through the policy table and the TX arbiter, so the
transmit-safety contract above covers it unchanged (a bridge program is one more TX owner). Per
radio, in `backend/rigctl.py`:

- **Mode names.** Every mode in the profile's `modes` needs a Hamlib name: `_CIV` for a CI-V radio
  (keyed by mode name and DATA on/off), `_YAESU` for the Yaesu labels. `python -m backend.rigctl_test`
  fails for a profile mode without one.
- **Data modes.** Yaesu-style radios have data modes of their own (`DATA-USB` = `PKTUSB`). On an
  Icom with the DATA switch set `capabilities.data_mode = True` (CI-V `1A 06`); the handler then
  reads the DATA state with the mode and sends it explicitly after every mode change.
- **Model number.** Add the radio's Hamlib model number (`rigctl -l`) to `HAMLIB_MODEL`.

## Steps to add a radio
1. Add a `RadioProfile` (id, name, make, protocol, address/baud, bands, modes, filters, steps,
   `connect_help`, the **safety/feature fields** — `tot_civ`/`tot_cat`, SWR meter source,
   `cw_send`/`cw_line`). Add `transports`/`capabilities` only where they differ from the
   synthesized defaults.
   - **No scope over the control link** (`has_scope=False`) → the app draws the **audio (AF)
     scope** from the RX audio. If the radio has CW, tell it where CW sits in that audio: in CW
     a radio shows the *zero‑beat* frequency (the one it plays AT its CW pitch), so without this
     every frequency on the audio scope is off by the pitch and a tap on a CW signal tunes it to a
     0 Hz tone. Set `cw_sides` (CW mode name → `+1` when a higher signal sounds a higher tone,
     `-1` for the LSB side) and, for a radio that cannot report its pitch, `cw_pitch_hz` (the
     fixed CW offset; Yaesu pitch comes from `KP`). A Yaesu whose CW side is a menu (FT‑891 07‑07
     CW BFO) sets `cw_bfo_read`, and one with a CW FREQ DISPLAY menu sets `cw_disp_read` — each
     the radio's own CAT read (`"EX0707;"`); the values ride the state as `cw_bfo` / `cw_disp`.
     Take the side and pitch from the manual (the CW pitch / display menus, or the BFO table:
     the IC‑728's 9.01060 MHz CW TX vs 9.00980 MHz CW RX = an LSB‑side 800 Hz). The frequency
     map itself is `AfMap` in `frontend/waterfall.js` (tests: `tests/af_scope_test.js`).
2. **SET‑menu table (recommended)** — to expose the radio's full menu in the Setup tab, add
   `backend/menus/<id>_menu.py`: one `MenuItem` per entry (num, name, group,
   `kind`=enum|int|signed‑int, **`digits` = the manual's exact wire width** — for signed it
   INCLUDES the sign char, options *or* min/max/step/unit), marking `readonly` and `critical`
   (connection/transmit‑sensitive: only an admin may write them, and every write is audited)
   items, plus the two transmit flags of the contract: `tx_tot` on the TX time‑out timer
   (item 5) and `tx_route` on every PTT/keying‑routing item whose options name RTS/DTR
   (item 7). Reference it from the profile (`menu=<ID>_MENU`).
   Compile it from the manufacturer's CAT/menu reference (a research pass over the PDF).
   **Verify a sample of `digits` on the real radio (read → echo) before trusting the table** —
   a wrong width is silently ignored by the radio. The shared `backend/menu_engine.py` handles
   encode/decode (Yaesu `EX` today; the Icom CI‑V `1A 05` encoder is implemented too — see ic9700_menu.py / ic7300mk2_menu.py).
3. **Same protocol** as an existing radio → done; the handler + menu engine are shared.
   - **Old/limited CI-V rigs** (e.g. the IC-728, ~1990) speak only the classic command set
     `0x00-0x0E` — freq/mode/VFO/memory and nothing else: no CI-V transmit, meters, levels,
     functions, RIT, menu or scope command exists. Set `civ_legacy=True` on the profile. The Icom
     handler then (a) drops any frame whose command byte is `> 0x0E` on the wire — one lever that
     neutralises PTT/meters/levels/functions/RIT/tuner/CW/tone/menu/scope so no method needs a
     per-call guard, (b) uses a minimal connect + gentle poll (just `03`/`04`, so a 1200-baud link
     isn't flooded), and (c) sends mode-only `06` (no filter slot; CW narrow = `03 02`). Turn the
     matching `Capabilities` off (`ptt`, `tab_rx/tx/dsp`, `meters=[]`, `rx_dsp=[]`, `menus`, …) so
     the UI hides every control the radio can't drive rather than shipping dead buttons. The
     transmit-safety contract is **N/A** — with no CI-V key, the app physically cannot transmit.
4. **New protocol/make** → a new handler class exposing the server's method surface (incl.
   `get_menu`/`set_menu`/`read_menu_group`) **and the full safety contract above** (including whether it can report TX state, item 3),
   plus a menu‑engine encoder for that protocol.
5. Register it in `PROFILES`; add any new **frontend** asset to `server.py`'s versioned‑asset
   list (or it 404s / serves stale). Menu tables are pure backend data — no asset entry needed.
6. Verify on real hardware **RX‑side only** (the maintainer never tests TX by transmitting).
   The operator verifies any transmit path — PTT, CW send — and any **critical menu writes**
   (CAT rate, PTT/port routing, max power) themselves.
