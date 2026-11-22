# Roadmap

TeleRig (formerly Radio WebOp) is a browser control panel for ham transceivers (Icom CI-V + Yaesu CAT).
Each radio is a **declarative `RadioProfile`** in `backend/profiles.py` — transports
(CAT/serial, RS-BA1 LAN, audio, scope), a **capability map** the UI adapts to, and a
**SET-menu table** — so adding a radio is mostly data (see `docs/ADDING-A-RADIO.md`).

## Done

### The VFO with its meter, typing a frequency; SSTV: no false pictures, a viewer (unreleased)
- **The VFO, laid out like an Icom main screen** (picked from the VFO examples, look A). Each band: a top
  line (TX/RX, the mode with -D, the filter and, on an Icom, its width, the VFO and the band), the digits
  with the ones under the kHz small, the offsets beside them (RIT with its kHz, split, duplex with its
  offset, the FM tone, the dial lock) and the lit functions (AGC, the radio's own preamp name, ATT, NB,
  NR / DNR, AN / DNF, MN, NAR, COMP, VOX, MON). With two bands the operating one takes the room.
- **The meter is in the VFO panel**, along its bottom; the Meter panel is gone (a saved layout gives its
  rows to the panels right above it). On receive it is the S-meter, its scale above the bar, S9+ in red; on
  transmit the TX meter picked with its keys (Po unless the radio has none), on its own scale under the
  bar, with the values the radio's CI-V reference gives (Po %, SWR 1.5 / 2 / 3 with amber and red, ALC
  in its zone, COMP dB, Vd V, Id A); a Yaesu shows the raw reading (its CAT reference has no scale). A mark
  on the bar holds the peak for a second, then falls back; the reading stays put (it starts at the same
  place whatever its length, so S9 doesn't move when +0 or +20 joins it). The TX meters are read only while transmitting (Vd and Id
  always), so receiving reads the S-meter as fast as before.
- **Type a frequency**: click the VFO's digits (or Enter on them): MHz (14.074), the VFO's own grouping
  (7.074.50), .230 for the kHz on the MHz you are on, or a bare number, read as MHz where the radio receives
  there and as kHz or Hz otherwise (7074 is 7.074 MHz; 1296 on the IC-9700 is 1296 MHz); Enter tunes,
  Esc cancels, and a frequency the radio can't receive is refused with the reason. The Controls panel's
  frequency box reads the same way. Scrolling over a digit still tunes it. Each radio's receive coverage
  is in its profile (`rx_ranges`, from the manuals), and the Icom meter scales too (`meter_marks`).
- **SSTV receive, no false pictures.** A start is a picture only once its lines prove it: 4 of its first
  12 line syncs must arrive where the header's timing puts them, as real pulses (low inside, the porch
  and the picture around them). Until then nothing shows. A header heard in noise, voice or a tone, or a
  chosen mode started on them, used to paint about ten lines of noise into the strip; now it is dropped
  and the decoder keeps listening, and a real header heard meanwhile is caught (a false start used to
  make it miss a picture that began right after). A picture that ends early goes into the strip only if
  a quarter of it arrived; the status says when it isn't kept.
- **Picture viewer.** Click a picture in the strip to see it large, with when it came and its size:
  Export PNG saves it under its own name (mode, frequency, UTC time), Delete removes it. Esc or a click
  outside closes it; it doesn't open while this tab transmits (it would cover STOP).
- **Delete from the strip.** Each picture shows a red X in its corner while it is hovered or has the
  keyboard focus (always, on a touch screen); the focus moves to the next picture.
- **A spot tunes the filter too, and the scope shows it.** A click on a spot also sets the filter for its
  mode: SSB 2.4 kHz, CW 500 Hz, digital 3 kHz (the whole FT8 / FT4 subband), AM and FM their normal
  filter. An Icom gets its FIL slot (SSB and CW FIL2, digital FIL1 with the DATA switch on: USB-D), a
  Yaesu NAR off and the WIDTH (its DATA-USB defaults to 500 Hz), the IC-728 (no filter command) the mode
  only. Only what isn't set already is sent; a spot whose mode isn't known tunes the frequency only. The
  band-plan view of a radio with no scope over CAT now shades the passband with its width beside it
  ("2.4 kHz"); a Yaesu's comes from its WIDTH (it always showed 2.4 kHz), none in AM / FM, which it
  doesn't report.
- **FT-891 WIDTH works.** Its WIDTH command carries the function's on/off before the code (SH0 1 14;),
  unlike the FT-991A's (SH0 14;); TeleRig sent the FT-991A's, so the FT-891 kept its width (from the WIDTH
  keys or a spot) and its reading came from the wrong digits. Now each radio gets its own form
  (profile `sh_onoff`), and WIDTH off reads as the mode's default.
- **The VFO after a reload.** The page opens its socket before it has the radios' profiles, so a state
  frame could be drawn without one: a Yaesu's width read the Icom way (2.4 kHz whatever its WIDTH) and its
  lit functions with Icom names (P.AMP, NR for AMP1, DNR), until the radio next changed something. Now no
  width shows until the profiles are in, and the VFO is drawn again when they are.
- **The audio scope in CW zooms to the radio's width.** On a radio with no scope over CAT (FT-991A,
  FT-891, IC-728) CW showed the whole 0-3.6 kHz of audio, so a 500 Hz filter was squeezed into a seventh
  of the scope. Now, with a CW filter up to 1.2 kHz (a Yaesu's WIDTH, the IC-728's CW-NAR), the scope
  shows 2.5 widths centred on the pitch (1.25 kHz for 500 Hz, 625 Hz for 250 Hz, never under 400 Hz) and
  shades the passband around the marker; SSB, data and wider CW filters stay at 3.6 kHz (a wide one may
  be the radio's SSB filter, not centred on the pitch: the IC-728's CW). The waterfall starts again when the scale
  or the side (USB / LSB, which mirrors it) changes, and its finer FFT reacts a little slower (a third of
  a second of audio).
- **No humps at a narrow filter's edges.** The audio scope's noise floor was a 500 Hz-wide average, which
  sank at a narrow filter's edges and lit the passband's rim up in two humps (a 250 Hz filter lit up
  whole). With a CW filter it is now measured inside the passband only (a median, clear of its skirts):
  the noise inside looks as it does on a wide band, the outside stays dark, and signals stand out.
- **The Modem window's text scrolls inside it.** CW's chat, RTTY's text and FT8's list grew the window
  with every line, so the whole panel scrolled, the mode keys and controls with it. Now the window is as
  tall as its panel: the text takes the room the controls leave and scrolls on its own, down to the
  smallest panel the layout allows. At the bottom it follows what comes in; scrolled back, it stays
  where you are reading, and it keeps that place (or keeps following) when you switch modes and come
  back. SSTV's picture still scrolls with the panel when the panel is shorter than it.
- **CW decodes without a click**, as RTTY, FT8 and SSTV always did: showing CW (its key, the page
  opening on it, the Modem window added showing it) turns Decode on. Decode still turns it off, until
  CW is next shown. The decoder starts listening once the page has its audio (RX, or the sidetone's ▶);
  it never starts the audio itself, so a page that opens on CW doesn't leave the browser's audio paused
  (which would have made the mic send silence).
- **Step keys under the scope, and nothing there tunes by scrolling.** The scope's bar has **−** and
  **+** with the tuning step between them: a tap is one step, a held key keeps going (ten a second)
  until you let go. On a phone they are thumb-sized. The step is the Controls panel's STEP (either picker
  sets both) and is now remembered per radio on each device. The mouse wheel over the scope and a finger
  sliding along the waterfall no longer tune: scrolling the page past the scope used to retune the radio,
  and a slide was far too coarse to tune with on a phone. A tap or a click on a signal still tunes to
  it, a mouse drag still pans, and the wheel still tunes over a VFO digit.
- **Calling frequencies in the Spots panel.** The panel has three views now, picked with the keys at its
  top: LIVE (the spots), CALLING and MEMORY. CALLING lists the fixed frequencies worth a click on the
  bands the radio has, band by band: the calling frequencies (SSB / CW and FM simplex on 6 m and up, QRP
  CW and SSB on HF), each band's FT8 dial frequency, SSTV, and a few things to listen to (the NCDXF
  beacons, the 14.300 nets, APRS, the space station). A click tunes as a spot's does (frequency, mode,
  filter), and the row the radio is on is marked Tuned however it got there. The list ships with TeleRig
  (`frontend/callfreqs.js`: United States, each entry from its source: the ARRL band plan and frequency
  guide, QRP ARCI, WSJT-X, NCDXF, ARISS), so it needs no internet, and the spots are fetched only while
  LIVE shows.
- **Memories.** MEMORY holds your own frequencies for the radio you are on. SAVE takes the radio as it is
  and asks for a name, with the frequency filled in and selected: type over it, or don't, then SAVE again
  (or Enter). A click on a memory brings back the frequency, the mode and the filter (an Icom's FIL slot
  and DATA, a Yaesu's NAR and WIDTH); the X beside it deletes it at the second tap. A memory can be
  anywhere the radio receives (WWV, an airband channel), not only on its ham bands. They are TeleRig's,
  not the radio's own memory channels: the server keeps them per account and per radio
  (`/api/me/memories`, up to 100 a radio), so every device you sign in on has them. Operators only.
- **The Modem window, easier to get going.** Every mode decodes the radio's audio, so with RX off the
  window now says so under its mode keys, with an **RX on** key right there (it presses the Audio
  panel's RX key; no more "turn on RX in the Audio panel" in each mode). CW, FT8 and SSTV have a **Tune
  to** menu: that mode's frequencies on the radio's bands (each band's FT8 dial frequency, the SSTV
  frequencies, the QRP CW calling frequencies and the beacons), from the same list as the Spots panel's
  CALLING view; a choice sets the frequency, the mode and the filter. CW: one line under the bar says
  where the decoder stands (off, loading, listening, copying, or why it can't) in place of "ready" and a
  hint under the chat; turning Decode off and on keeps the chat (Clear empties it); the speed and the
  sidetone's pitch are remembered per browser, and the pitch is shown beside its slider. FT8: the list has
  column headings (dB, DT, Hz, Message); the status names the radio's mode when FT8 can't be heard in it,
  and after 45 s with nothing decoded says what to check; the note says it is receive only and that WSJT-X
  works FT8 through the bridge. On a phone or any touch screen the mode keys, SSTV's Receive / Send tabs,
  the menus and the text fields are as tall as the keys (44 px; the Spots panel's too), and a Modem window
  added to a phone layout starts taller.
- **The Modem window: your callsign, quick CW messages, Copy, pictures that stay.** CW has a **Your
  call** field (kept by the browser; an account named as a callsign stands in until you type one) and
  three keys beside it: **CQ** writes a CQ call into the message field, **DE** and **73** add to what is
  there. They only write: ▶ still plays the message here and TX still sends it. FT8 marks the messages
  that name that callsign (also inside <...> or around a /), shows it at the end of the list's headings,
  and keeps such a message in view under CQ only. CW, RTTY and FT8 have a **Copy** key: the text goes to
  the clipboard (CW's with each message's time and RX / TX; FT8's as the list shows, tab-separated), also
  on a plain-http LAN address, where the browser has no clipboard API. CW has the small audio strip RTTY
  and FT8 have, with two lines at the radio's CW pitch: a station that is tuned in runs between them. It
  gives way when the window is too short for it and a readable chat (a Modem window added to a phone
  layout is now tall enough for both). SSTV's received pictures are kept by the browser (the newest 12,
  as many as the strip holds), so a reload or a restart of the browser no longer loses them; the X
  deletes a picture there too.
- **The scope's frequency reads whole.** The tuning line ran down through the middle of the frequency
  over the scope (14.05|780 MHz in CW). The line now starts under the label, and the label stands on a
  small plate of the scope's own colour, so neither the line, the passband's shading nor a strong signal
  right on the marker covers a digit. With the marker at an edge of the scope the label stays whole.
- **Memories keep a repeater's shift and tone.** A memory saved in FM keeps the repeater shift (simplex,
  + or −) and the tone (off, TONE or TSQL with its CTCSS tone, and on the Yaesus DCS or DCS-ENC with its
  code); one saved in DV (D-STAR) keeps the shift. The IC-9700 keeps the offset too (it can be set over
  CI-V); the other radios use their own offset for the band. A click sets them every time, after the
  mode, so a simplex memory also turns a repeater's shift and tone off; memories saved before keep
  leaving them as they are. The row shows the shift and the tone on a second line (−600 kHz · TSQL
  100.0 Hz). The IC-9700 now has the FM tone controls in its Controls panel too (TONE / TSQL and the
  tone; TeleRig drives no DTCS on it).

### v0.4.0 — the Modem window (CW, RTTY, FT8, SSTV), Spots, Recorder, WSJT-X bridge, Update now
- **WSJT-X bridge.** WSJT-X, JS8Call, Fldigi or a logger set to "Hamlib NET rigctl" connects to
  TeleRig, which answers Hamlib's `rigctld` protocol from its own radio handlers, so the program
  and the browser share one radio, including an Icom on its LAN port. Off by default, this PC only
  by default, receive-only until an admin allows transmit. A bridge program is one more user:
  one transmitter at a time, **ON AIR · WSJT-X BRIDGE** for everyone, STOP by any operator (which
  then pauses bridge transmit until an operator resumes it), the 120 s limit, a silence limit,
  and every transmission in the TX log. Checked against the real Hamlib client of 4.6.5, 4.7.1
  (WSJT-X's build) and 4.7.2 on the simulator; on-air use is for the operator to confirm. Control
  only: audio still comes from the radio's USB sound card. See [docs/WSJTX-BRIDGE.md](docs/WSJTX-BRIDGE.md).
- **Icom DATA mode** (USB-D / LSB-D / FM-D / AM-D, CI-V `1A 06`) on the IC-9700 and IC-7300MK2: a
  **DATA** switch next to the mode buttons, and what the bridge selects for WSJT-X's "Data/Pkt".
- **PTT hotkey.** A key of your choice on this device, hold-to-talk or press-to-toggle, with the PTT
  button's fail-safe rule: a press while you transmit always stops, and the key never keys while
  you type, while a dialog or menu is open or while the tab is hidden (user menu > PTT hotkey). A
  hold whose claim is answered late can never stay keyed after the key is up; Shift, Ctrl and Alt
  can't be the hotkey.
- **Full screen with the screen kept awake** (top bar; "Keep screen awake" on phones without a
  full-screen mode). The wake lock needs HTTPS or localhost.
- **Update now** (Administration > Update). A copy installed with git updates itself from GitHub,
  without a trip to its PC. It follows its own branch: a master install updates from master (the
  releases), a dev install from dev. Fast-forward only; packages only when `requirements.txt`
  changed; the new code must import, or it goes back to the previous commit; then TeleRig restarts
  and the page reloads onto the new version. Refused while anyone transmits, and on a folder with
  local changes or commits GitHub does not have. Every step is in the security log. A dev install
  says so in the top bar: the version reads `0.4.0-dev+<commit>`, the commit it runs.
- **Band plan in its own strip** just above the scope, so it no longer covers the bottom of the
  spectrum; it lines up with the scope's frequency axis, and hovering (or tapping) names the segment.
- **Radio screen theme**, the third step of the theme button (night, day, radio screen): the same
  layout dressed as a transceiver's own screen. Black screen regions with cyan italic panel names,
  glossy softkeys (blue when selected, red for anything that transmits), the operating VFO on a blue
  band with italic digits and the 10 Hz and 1 Hz digits smaller, a tick meter that turns red past S9,
  a black spectrum with a white trace and a green tuned marker over a deep-blue rainbow waterfall, the
  link as a boxed label instead of a status dot, and a UTC clock. Exo 2 (SIL OFL) is bundled, so
  nothing loads from the internet. Each browser keeps its own choice.
- **Recorder panel**, under Controls: records what you hear, the RX audio taken before the volume
  control, so it works the same for every radio (LAN or a serial radio's USB audio). Record / Stop,
  the running time and a level bar (live whenever RX audio is on, so the level can be checked
  first), then each recording in a list with Download and Delete, named after the radio, frequency,
  mode and UTC start (`TeleRig_FT-991A_14.074MHz_USB_20261118-210630Z.mp3`). MP3 at 48 kb/s, made
  from the radio's own 16 kHz audio by LAME (lamejs, bundled, LGPL), about a third of a megabyte a
  minute; a browser that can't load the encoder gets WebM/Opus instead. "Also save a lossless WAV"
  (kept per browser) adds a WAV of the same audio, sample for sample as the radio sent it, 16-bit
  mono, about 2 MB a minute: the copy to decode from later. Recordings stay in the browser tab
  until downloaded, and leaving the page with a file not downloaded asks first. A layout saved
  before the Recorder existed gets it under Controls too.
- **Spots panel** (Add panel > Spots): POTA and WWFF activators and DX cluster spots (HamQTH.com),
  newest first, with source, mode and "this band" filters each browser keeps. Click a spot to tune:
  the frequency, then the mode in the radio's own names (SSB by band, CW, data, FM); a spot off the
  radio's bands, or seen by a viewer, is shown but not clickable. The spot you clicked stays at the
  top, marked Tuned, until the radio is tuned more than 3 kHz away from it. The server fetches the sources
  only while someone has the panel open, never more often than each allows (POTA and WWFF once a
  minute, HamQTH every 30 s), and every page shares that; text from the sources is only ever text.
- **SSTV receive** (Add panel > SSTV): slow-scan TV pictures decoded in the browser from the radio's
  audio, painting line by line as they arrive. Robot 36 / 72, Martin M1 / M2, Scottie S1 / S2 / DX
  and PD-50 to PD-290 (PD-120 is the ISS's usual mode on 145.800 MHz FM). The VIS header picks the
  mode and its leader measures the tuning offset; each line re-locks on its sync pulse, so a station
  with a fast or slow sound card clock does not slant the picture; a chosen mode joins a picture
  already under way. Finished pictures go into a strip, each a PNG download named after the mode,
  frequency and UTC time.
- **SSTV transmit** (the SSTV panel's Send tab, operators only): a picture (chosen, dropped or
  pasted, cropped to fill the mode's frame) or a test card, with a line of text such as your
  callsign, sent as one operator-triggered transmission on the same rules as PTT: one TX owner,
  STOP, and a hidden tab or a dropped connection unkeys. The audio is made in the page before
  anything is keyed and goes out on the mic's path in real time; the radio is released only after
  the last line has played out. Each picture is held for its own length (its time on the air + 5 s,
  set by the server from the mode, not the page; a voice over keeps its 120 s), and the modes offered
  are the ones whose whole transmission ends inside the radio's own 3-minute TX time-out: Robot 36 /
  72, Martin M1 / M2, Scottie S1 / S2, PD-50, PD-90, PD-120 and PD-160, each with its time on the
  air. Never in CW, RTTY, DV or C4FM; a level control for the ALC.
- **Modem window** (Add panel > Modem; it was the SSTV panel, then Digital, and a saved layout keeps it):
  CW, RTTY, FT8 and SSTV in one panel, one shown at a time, every decoder listening while the panel is
  open, so switching loses nothing; a mode with news while hidden gets a dot, and while a picture or a
  CW message goes out the other modes wait (its STOP stays in view). RTTY and FT8 decode only, in the
  browser, from the radio's audio, under a small waterfall of it (0 to 3 kHz):
  - **CW**: the CW panel, moved in whole: the neural decoder (Decode; on by itself when CW is shown,
    since the unreleased changes above), the sidetone and CW TX. The scope's CW button shows the window
    on CW and starts the decoder. A layout saved with the CW panel
    showing gives its place to the window, or, with the window already showing, its columns back to
    the panel that lent them.
  - **RTTY**: Baudot with US-TTY figures, 45.45 / 50 / 75 baud and 170 / 425 / 850 Hz shifts; mark and
    space follow the radio's mode (LSB normal, USB reversed) and Rev swaps them; click a signal in the
    strip to tune to its pair, or nudge with the arrow keys; a squelch keeps noise off the screen.
  - **FT8**: 15 s slots on UTC, decoded in a background worker by a JavaScript port of ft8_lib's decoder
    (MIT, Kārlis Goba, YL3JG): the messages per slot with dB, DT and frequency, CQs marked, a CQ only
    filter. On a real recording it finds 17 of the 22 messages WSJT-X found there, nothing WSJT-X
    didn't, with the same DT and frequency and an SNR within a few dB. The slots follow this computer's
    clock, which has to be right to about a second.
- **TX time-out 3 minutes on every radio.** The radio's own TX time-out, set on connect as the
  backstop if the control link fails, is now 3 minutes on the FT-991A and FT-891 too (it was 2),
  like the Icoms: the 3-minute limit for a remotely commanded station (47 CFR 97.213(b)).
- **Icom front-panel TX detection.** The Icom radios now report their own transmit state (CI-V
  `1C 00`, read about 3 times a second), like the Yaesu radios always did: a hand-mic or
  front-panel key is the FRONT PANEL owner, shown to everyone, and nobody can key over it (a
  browser, a WSJT-X bridge program); it ends when the radio goes back to receive. The IC-728 has
  no CI-V TX command, so its front panel stays invisible.
- **RX volume with headroom.** VOL is a dB scale from off to +24 dB with 0 dB (the audio exactly as
  the radio sends it, the old top of the slider) a little past the middle, shows its value, and a
  limiter keeps boosted peaks from clipping. Each browser keeps its own VOL.
- **AF starts at 0.** AF is the volume of the radio's own speaker, in the room with the radio: every
  connect sets it to 0 (Icom and Yaesu), and turning it up from 0 asks first, in plain words.
  "Keep it at 0" puts the slider back; once allowed, AF moves freely until it is back at 0.
- **PWR in the TX tab.** The RF power slider moved from the RX tab to the TX group, above MIC.
- **CW chat: a pause splits received text.** After 5 s with nothing new decoded, the next received
  characters start a new bubble, so each over reads as its own message.

### Phase 1 (v0.3.0) — modular panels, multi-user sign-in, one TX owner
- **Modular panels.** The panel is a grid of cards — VFO, Transmit, Audio, Meter, Scope, Controls,
  CW — that can be moved, resized, collapsed, hidden and added back in **Arrange** mode (mouse,
  keyboard, or ▲/▼ on a phone), with Reset + Undo. Layouts are stored on the server per user, one
  for desktop and one for phone. Fresh dark/day look with an offline icon set (GridStack 14,
  Lucide; see NOTICE).
- **Multi-user sign-in.** Everyone signs in, on the host PC too; roles **viewer / operator / admin**.
  A new install's `admin` / `admin` works only on the host PC with a one-time setup code and forces
  a new password; `run.bat --reset-admin` / `python3 run.py --reset-admin` recovers. Sessions with
  sign-out / sign-out-everywhere, lockouts after repeated failures, exact-origin + allowed-names
  checks, and an **Administration** dialog: users, access, remote access (Tailscale Serve), radio &
  audio pins, activity (who is on, who transmitted, security log) and lockouts. Radio WebOp's shared
  password migrates (admin rights only on the host PC until changed). See
  [docs/REMOTE-ACCESS.md](docs/REMOTE-ACCESS.md).
- **One TX owner.** One connection transmits at a time; everyone sees **ON AIR · *callsign***, others
  get "busy", and any operator can stop it with a two-tap **STOP**. The owner's dropped link unkeys
  at once, a silent one after 6 s, other users' drops never do; the server keeps its own 120 s
  session clock on top of the radio's failsafe, and every TX session is logged (`audit-tx.log`).
  The transmit-safety contract in [docs/ADDING-A-RADIO.md](docs/ADDING-A-RADIO.md) is updated to
  match, including the Icom limitation (no front-panel TX detection yet).

### v0.2.25 — FT-991A ATAS manual tuning
- When TUNER SELECT = **ATAS** (read over CAT via `EX143`), **ATAS ▲/▼** controls appear next to PTT
  to step the motorized antenna (mic UP/DN emulation) while transmitting, with the meter auto‑switched
  to **SWR** — mirroring the radio's hold‑PTT‑and‑step manual‑tune procedure. The step buttons act
  only while keyed and never transmit on their own. (Auto‑tune stays on the TUNE button = `AC002`.)

### v0.2.24 — Icom IC-728 (control-only HF rig)
- Added the **IC-728** (fifth radio): CI-V over COM via a CT-17 (addr `38h`, 1200 baud). The first
  radio whose CI-V is the classic `00`-`0E` set only, so it gets a **minimal, honest panel** —
  frequency / mode (LSB/USB/CW/CW-NAR/AM/FM) / band / VFO A-B / **memory recall** (`08`/`0A`) + AF
  scope + RX audio + CW decode. **No CI-V transmit/meter/level/RIT/menu** exists on this radio, so
  the app can't key or meter it and hides every control it can't drive (transmit-safety = N/A). A new
  `civ_legacy` profile flag drives a minimal connect/poll + a write-layer allowlist (drops any CI-V
  command `> 0x0E`) so the modern handler never floods a 1200-baud link with commands it lacks.

### v0.2.23 — CW chat as a texting UI
- The decoded‑CW window now reads like a messaging thread: **received in grey bubbles (left),
  transmitted in blue bubbles (right)**, time‑stamped, with the live in‑progress decode shown in
  amber inside the current receive bubble. Also: the **header version is now driven by
  `/api/version`** so it can't drift out of sync with the build.

### v0.2.22 — scope frequency scale + cursor readout, auto RX audio, CW-TX mode fix
- Scope shows the **centre (tuned) + edge frequencies** and a **cursor frequency readout** on hover
  (CI-V + AF scopes). **RX audio auto-starts on COM/USB connect**. **CW encoder TX** now works in any
  CW mode (fixed the FT-991A CW-USB/CW-LSB rejection).

### v0.2.21 — IC-7300MK2 operating controls
- Filled the Icom operating-control gaps: **internal ATU** (`1C 01`, reuses TUNER/TUNE),
  **FM tone/TSQL + CTCSS freq** (`16 42/43`, `1B 00`), an **Icom CW/filter group** (APF `16 32`,
  break-in `16 47`, CW pitch `14 09`, keyer speed `14 0C`, filter shape `16 56`), and **3-state
  preamp**. FM panel made adaptive (`fm_dcs` cap hides DCS on Icom). New caps `fm_dcs` / `icom_cw`.
  Remaining tracked in [docs/IC-7300MK2-SETTINGS-AUDIT.md](docs/IC-7300MK2-SETTINGS-AUDIT.md);
  most commands are shared with the IC-9700.

### v0.2.20 — FT-991A operating controls, rounded out
- **WIDTH** (`SH`, per-mode stepper), **CONTOUR** + **APF** (`CO`), the **CW cluster**
  (break-in/keyer/speed/pitch/spot/zero-in), **TXW**/**Quick Split**/**Parametric EQ**, **SCAN**
  and **FAST** — all adaptive by mode. **MONITOR** (`ML`) is now real (was a no-op). New
  `ext_ops` capability. Remaining (memory management, DVS, keyer-message play) tracked in
  [docs/FT-991A-SETTINGS-AUDIT.md](docs/FT-991A-SETTINGS-AUDIT.md).

### v0.2.19 — Yaesu operating controls (modes, Tone/DCS, NAR/WIDE)
- **Radio-accurate mode labels** on the Yaesu radios (CW-LSB/CW-USB, RTTY-LSB/RTTY-USB,
  DATA-LSB/DATA-USB, correct order) — the UI mirrors the rig's own MODE list.
- **FM Tone/DCS panel** — tone mode (`CT`), 50 CTCSS tones + 104 DCS codes (`CN`), repeater shift
  (`OS`); FM-family modes only. **NAR/WIDE** toggle (`NA`). New caps `narrow` / `fm_tone`.
- Kicks off the **"expose every setting"** effort: [docs/ADDING-A-RADIO.md](docs/ADDING-A-RADIO.md)
  now requires it, and [docs/FT-991A-SETTINGS-AUDIT.md](docs/FT-991A-SETTINGS-AUDIT.md) tracks the
  remaining operating controls (WIDTH/CONTOUR/TXW/MONITOR/CW-cluster/…).

### v0.2.18 — one-click Windows install
- **`install.bat`** (one-click install/update): checks for Python first (installs 3.12 via
  `winget` if missing), builds an isolated `.venv`, installs deps, verifies the import, and
  makes a desktop shortcut. Idempotent — re-run to update (git pull on a clean checkout).
  `run.bat` now uses the `.venv`. See [README](README.md#install--run-windows).

### v0.2.17 — Yaesu FT‑891
- **FT‑891 profile** (HF/50 MHz mobile, typically over a Digirig) on the shared Yaesu CAT driver:
  160–6 m, 10 modes, IPO/AMP preamp, RF ATT, 5–100 W, S/PO/SWR/ALC/COMP/ID meters; COM‑only, AF
  scope, **no internal ATU** (external/ATAS only). Host sound‑card audio (Digirig CODEC as Radio
  RX/TX).
- **Full 159‑item SET menu** in the Settings tab, compiled from the CAT reference and cross‑checked
  against a deterministic parse; connection/transmit‑sensitive items confirm‑gated, versions
  read‑only. The menu engine now carries a per‑item **`ex_width`** so it speaks the FT‑891's 4‑digit
  `EXGGNN` menu numbers as well as the FT‑991A's 3‑digit `EXNNN`.
- **Safety:** TX TOT (05‑14) set to 120 s and PC KEYING (07‑12) forced OFF on connect; the driver
  no longer emits an FT‑991A menu number for a radio that doesn't line‑key. CW *transmit* is not
  wired yet (FT‑891 `KY` replays stored memories only).

### v0.2.16 — remote operation: host audio + fast meter + secure remote access
- **Host sound‑card audio** for serial/USB radios operated remotely — the server captures the host's
  RX card → browser and plays the browser mic → the host's TX card (16 kHz PCM over the WS, like the
  LAN path). Radio RX/TX pickers list host devices; optional `sounddevice` dep.
- **Faster meter** — S/TX meters on a dedicated lightweight WS channel (~25 Hz Icom / ~12 Hz Yaesu),
  hi‑res‑timer pacing, and full‑state de‑dup so it doesn't flood. Failsafes untouched.
- **Secure remote access (Tailscale).** In‑app **🌐 Remote** setup: detects Tailscale, runs
  `tailscale serve` (tailnet‑only HTTPS, no port‑forward, CGNAT‑friendly), explains why HTTPS is
  needed. App‑layer shared‑password auth (loopback exempt; not‑trusted behind a proxy) + an
  Origin/Host allowlist closing cross‑site‑WS / DNS‑rebinding.

### v0.2.15 — audio routing + sharper waterfall + live CW decode
- **Three-input audio routing** — COM radios expose **Radio RX / Radio TX / Mic In**; IP radios just
  **Mic In** (defaults to the OS mic, never the radio CODEC). Remembered per radio.
- **Higher-resolution, WSJT-style audio scope** — adaptive FFT (~1 bin/pixel) + float dB data + a
  per-frequency noise-baseline subtraction that flattens the shaped passband to a dark floor (no
  smoothing, crisp pixels).
- **Live-streaming CW decode (no seam)** — a growing-buffer decoder that commits at word gaps and
  slides past them (after [e04/web-deep-cw-decoder](https://github.com/e04/web-deep-cw-decoder)); the
  worker returns per-character / word-space frame spans. Plus the "turn on RX" false-prompt fix.

### v0.2.14 — Icom Setup menus + preamp/tuner
- **Icom CI-V Setup menus** — the data-driven Setup tab generalized to Icom (`1A 05`): the
  **IC-9700** (54 items) and the full **IC-7300MK2** (176 items), on the same engine as the Yaesu
  `EX` menus; per-model tables in `backend/menus/<id>_menu.py`.
- **FT-991A preamp IPO / AMP1 / AMP2** (preamp states declared per radio via `preamp_labels`).
- **Antenna-tuner TUNE** action (operator-triggered ATU tuning cycle) beside the tuner in/out
  toggle, plus a **waterfall "click or drag to tune"** hint.
- **RF power read, not forced, on connect** — the panel mirrors the rig's actual power (and
  AF/RF/SQL) instead of zeroing it; the Icom per-band connect power-zero is removed.

### v0.2.13 — declarative profiles + full Setup menu
- **Declarative `RadioProfile`** is the single source of truth: structured `transports`
  (serial/network/audio/scope) + a `capabilities` map (synthesized from the flat flags when
  omitted). The **UI is adaptive** — it shows only the controls a rig reports (and fixed the
  FT-991A's dead VFO A/B buttons).
- **Data-driven Setup tab** — the **FT-991A's full 154-item SET menu** (001–154) as a grouped
  accordion: each section reads live from the radio on open (lazy), and writes back with a
  confirm-read. Connection/transmit-sensitive items (CAT rate/RTS, PC keying, PTT/port routing,
  per-band max power) are flagged and confirmed; CAT-RTS / PC-KEYING are protected so the menu
  can't break CW/PTT keying. Generic codec in `backend/menu_engine.py`; per-model table in
  `backend/menus/<id>_menu.py` (Icom CI-V `1A 05` menus are a stubbed seam for later).

### v0.2.2 — multi-manufacturer + overlay tools
- **Yaesu FT-991A** — first non-Icom radio (Yaesu CAT over USB, COM-only); profiles
  carry make/protocol/capabilities. Serial ports show Enhanced/Standard + auto-pick.
- **Band-plan overlay** (ARRL VHF/UHF + FCC license-class HF) with color key + tooltips.
- **CW decoder/coder** overlay tool (adaptive squelch/tone/WPM; Morse sidetone, no TX);
  **audio (AF) spectrum scope** from RX audio for radios with no CAT scope.
- **USB audio** RX-in/Mic-out pickers for COM radios; **per-radio connection memory**
  (+ "?" help) and **live panel re-sync** so the UI mirrors the radio's real state.

### v0.2.0 — unified multi-radio base
- **Multi-radio app** with a model selector (IC-9700, IC-7300MK2); bands/modes/
  steps render from the selected profile.
- Transports behind one interface: **USB CI-V**, **LAN** (RS-BA1 UDP), simulator.
- **Scope + waterfall** with the tuned marker + filter passband; tap-to-select and
  drag/slide tuning, step, direct entry, wheel.
- **RX audio** + **mic TX** over LAN (auto MOD-Input → LAN); **PTT** tap-to-toggle
  with a failsafe time-out; mobile-friendly; remembers radio + connection.

### v0.2.1 — IC-9700 build-out (M1–M3)
- **Dual-watch** MAIN/SUB Radio view + **multi-meter** + core RX (preamp/att/lock).
- **RX DSP** — NB, NR, auto/manual notch (+ width/position), AGC, twin-PBT.
- **TX** — mic / COMP / VOX / monitor / TBW, **RIT**, **split/duplex** (VOX bound by
  the same failsafe as PTT).
- **Skeuomorphic TFT Radio view** from the IC-9700 manual (mode/filter box, lit
  function-indicator strip, VFO + wavelength band labels); controls below the
  waterfall; full-travel sliders; abbreviation tooltips.
- Reference: full per-control map in `docs/CONTROL-MAP.md`, screen/menu blueprint
  in `docs/UI-SPEC.md`.

### Band-plan overlay
- **ARRL band plan / FCC license-class overlay** on the scope — a toggleable strip
  (now just above the scope) showing the segments for the band in view, aligned to the
  frequency axis, with a hover tooltip giving the range, type, modes and notes.
  VHF/UHF (2 m / 70 cm / 23 cm) is colored by **use** (CW / weak-signal / SSB / FM /
  repeater / simplex / satellite / beacon / digital, ARRL voluntary band plan); HF +
  6 m is colored by **license class** (Technician / General / Extra sub-bands, FCC
  Part 97). Data in `frontend/bandplan.js`, compiled + adversarially verified.

## Planned

### Operating
- **Band-plan overlay enhancements** (the overlay itself shipped): let the user pick
  their license class to highlight just their privileges and soft-flag tuning outside
  them; make the data region-aware so non-US band plans can drop in.
- **CW encoder / decoder / transmit** — *shipped*, the Modem window's CW mode (the scope's "CW"
  button shows it): a **neural decoder** (the DeepCW ONNX model from e04/deepcw-engine,
  run in-browser via onnxruntime-web), a type-to-Morse soft-keyed off-air sidetone, and a
  **TX button** that sends the typed message as CW — operator-triggered, one bounded message,
  failsafe- and TOT-bound. Icom keys via CI-V `17` (semi break-in) so the rig generates the CW;
  the FT-991A is keyed by host-timed **DTR on its Standard USB port** (PC KEYING=DTR, CAT RTS
  disabled), since it has no arbitrary-text CW CAT command. The decoder is what relicensed the app to **AGPL-3.0** (a
  deliberate opt-in for the accuracy). **Live streaming decode shipped (v0.2.15)** — a growing-buffer
  decoder that commits at settled word gaps and slides past them, so the live stream decodes with no
  fixed-window seam (after e04/web-deep-cw-decoder). *Follow-ups:* optional WebGPU backend.
- **Recorder, next steps** — the TX audio in the recording too, and QSO timestamps / a
  recording log on top of the Recorder panel (Done above).
- **SSTV receive, next steps** — the last 2 to 4 pixels of each line pick up what follows the scan (in
  Robot 36 the next line's sync, so the right edge turns green) and the first is a little dark: the scan
  edges need to allow for the FM discriminator's filter. Decoding a saved recording also needs the
  decoder to work through a long buffer as it goes (today it keeps about 6 s and works after each
  piece, which is right for the radio's 20 ms packets).
- **SSTV transmit, next steps** — replying with a received picture, and decoding a saved recording.
  Scottie DX, PD-180, PD-240 and PD-290 (3:09 to 4:51 on the air) are longer than the 3-minute TX
  time-out each radio is set to on connect, the backstop if the control link fails, so TeleRig
  doesn't send them (decided 2026-11-19: keep every transmission inside the 3 minutes).
- **Modem window, next steps** — FT8's other message types (DXpedition, Field Day, RTTY Roundup, EU
  VHF: ft8_lib skips them too), RTTY's AFC, decoding a saved recording, FT4.
- **More overlay tools** over the waterfall (the framework is in place) — e.g. a tuning/zero-beat aid,
  memory keyer.
- **Tone / DTCS** — DTCS on the Icoms (the IC-9700 has it over CI-V: 16 4B, 1B 02) + an editable
  duplex offset in the Controls panel (M3.5; a memory already sets the IC-9700's).
- **Memory channels**, band-stacking registers, scan control.
- **CW** keyer/memories; **DV / D-STAR** (DR, call signs, GPS);
  **satellite** dual-scope.
- Read **true IF filter widths** (`1A 03`); scope **reference-level / sweep-speed /
  VBW** controls.

### WSJT-X bridge follow-ups
- **Audio for the bridge**: route a LAN radio's audio to and from sound devices on the TeleRig PC
  (a virtual audio cable), so WSJT-X works with an Icom reached over LAN only; and an audio path
  for a program on another computer.
- **Split with a separate transmit VFO** over the bridge (WSJT-X "Rig" split), per radio.
- **Meters over the bridge** (SWR, power out) for programs that show them.

### From the Rig-Control-Web review
- **SOTA spots** in the Spots panel: SOTA's API terms ask for SOTA's approval before AI-written
  software connects, and for the developer to join their API-consumers group, so it waits for that.
- **HF conditions**: solar numbers and MUF maps.
- **Command console**: send a raw CI-V / CAT command and see the answer; a diagnostics panel.
- **FT8 decoder** in the browser, for a quick look at a band — *shipped*, in the Modem window (see Done).
- **Live CW keying** from a paddle (MIDI adapter), the keyboard or touch.
- **Video feed** of the radio's own screen.
- **Packaging**: a desktop app, Docker, installers.

### UI
- The **FUNCTION-screen panel** — a clean modern panel of the IC-9700 FUNCTION-screen
  toggles grouped by category. *(The SET-mode menu tree shipped — see Done.)*

### Platform
- **More radios** — IC-705, IC-7610, IC-905, IC-7300, etc. (each is just a profile).
- **Authentication** for safe remote use — *shipped (Phase 1)*: everyone signs in, with roles and
  a single TX owner (see Done). *Built-in HTTPS shipped* — serve TLS directly with
  `run.py --ssl-certfile/--ssl-keyfile` (or `TELERIG_SSL_CERT/_KEY`).
- **Phase 2 follow-ups** to sign-in and TX ownership: the high-SWR cutoff, LAN radio credentials
  kept on the server (admin-only) instead of in each browser, server-side session ids with a
  shorter admin session, stable host audio device ids, and a per-radio serial-line (RTS/DTR)
  policy.
- **Remote power-on** from networked standby where the radio supports it.
- Audio: selectable **codec / sample-rate**, lower-latency options.

## Non-goals (for now)
- Hamlib backends (other makes driven through Hamlib instead of a TeleRig profile) — possible
  later via the transport+profile split, but not a near-term focus.
