# The WSJT-X bridge

WSJT-X, JS8Call, Fldigi and most logging programs control a radio through Hamlib. Normally that
means the program owns the radio's COM port, so TeleRig cannot have it at the same time, and Hamlib
cannot reach an Icom over its LAN port at all.

With the bridge, TeleRig keeps the radio and the program talks to TeleRig instead. TeleRig answers
the same network protocol as Hamlib's `rigctld`, so in the program you pick the rig
**Hamlib NET rigctl** and point it at TeleRig. Both then work at once: you can watch the waterfall
and the meters in the browser while WSJT-X tunes and (if you allow it) keys the radio.

The bridge is **off** until an admin turns it on, it listens **on this PC only** unless told
otherwise, and it is **receive-only** until an admin allows transmit.

- [What it does and what it does not](#what-it-does-and-what-it-does-not)
- [Audio](#audio)
- [Turn it on](#turn-it-on)
- [Set up WSJT-X](#set-up-wsjt-x)
- [Who transmits: the rules](#who-transmits-the-rules)
- [What you see in TeleRig](#what-you-see-in-telerig)
- [A program on another computer](#a-program-on-another-computer)
- [When something goes wrong](#when-something-goes-wrong)
- [For developers](#for-developers)

## What it does and what it does not

| Works over the bridge | Not offered |
|---|---|
| Read and set the frequency | Split with a separate transmit VFO (use WSJT-X's **Fake It**) |
| Read and set the mode, including the data modes | RIT / XIT, memories, scanning |
| PTT on and off (only when an admin allows it) | The SET menu, DSP and level controls (use the browser) |
| Read the S-meter and the RF power setting; set RF power | **Audio**: see the next section |
| Turn the radio's split off | |

Data modes map to what each radio really has: on the Icoms `PKTUSB` is **USB with DATA on**
(USB-D), on the Yaesus it is the **DATA-USB** mode. A mode the connected radio lacks is refused
rather than replaced by something similar.

**How far it is tested.** The bridge is exercised by the real Hamlib client (`rigctl -m 2`) of
Hamlib 4.6.5, 4.7.1 (the build that ships with WSJT-X) and 4.7.2 against TeleRig's simulator, and
its replies were compared with the real `rigctld` of those versions. Operating WSJT-X on the air
through it is for you to confirm on your own station.

## Audio

The bridge carries control only. The program needs the receive audio and a place to send its
transmit audio, exactly as without TeleRig:

- **A radio with a USB sound card** (FT-991A, FT-891 on a Digirig, IC-7300MK2 or IC-9700 on their
  USB cable): choose the radio's sound card ("USB Audio CODEC") as input and output in the program,
  as usual. Windows lets TeleRig and the program use the same card at the same time.
- **An Icom that TeleRig reaches over LAN**: there is no audio path from the LAN link to another
  program yet (it is on the roadmap). Until then, also connect the radio's USB cable to the PC that
  runs the program and use its USB sound card for audio. In a data mode the Icoms take the
  transmit audio from their **DATA MOD** source, which is USB by default; TeleRig does not change
  that setting (it only routes **DATA OFF MOD** to LAN, for the browser microphone).

## Turn it on

1. Sign in as an admin and open **Administration > WSJT-X bridge**.
2. Tick **Bridge on** and press **Save** (TeleRig asks for your password again if it has been a
   while).
3. Leave the **port** at 4532 unless another program on this PC already uses it. A real `rigctld`
   uses 4532; if one is running, the status line says the port could not be opened and TeleRig
   keeps retrying.
4. Tick **Allow transmit (PTT) through the bridge** only when you want the program to key the
   radio. TeleRig asks once more before it saves that.

The settings are stored in `config.json` (`"bridge"`) and take effect at once. The bridge needs a
radio connected in TeleRig: with no radio it closes every incoming connection.

## Set up WSJT-X

**File > Settings > Radio**:

| Setting | Value |
|---|---|
| Rig | `Hamlib NET rigctl` |
| Network Server | `localhost:4532` |
| PTT Method | `CAT` (or `VOX` while transmit over the bridge is off) |
| Mode | `Data/Pkt` (or `USB`, or `None` to leave the mode alone) |
| Split Operation | `Fake It` or `None` (not `Rig`) |
| Poll Interval | 1 s is fine; keep it well below the silence limit |

Press **Test CAT**: it turns green and WSJT-X shows TeleRig's frequency. With transmit allowed,
**Test PTT** keys the radio and TeleRig shows **ON AIR · WSJT-X BRIDGE** to everyone.

JS8Call uses the same settings. Fldigi and loggers that can use a Hamlib "NET rigctl" rig should
work the same way with the address `127.0.0.1:4532`; they have not been tried here.

When WSJT-X starts it nudges the frequency a few Hz up and back to learn the radio's tuning
resolution. That is normal.

## Who transmits: the rules

A bridge program is one more user. TeleRig applies the rules it applies to a browser:

- **One transmitter at a time.** While someone else is on the air the program cannot key or
  retune (WSJT-X shows a rig error). While the program is on the air everyone sees
  **ON AIR · WSJT-X BRIDGE**, other users get "busy", and any operator can press **STOP**.
- **STOP stays a stop.** A digital-mode program keys again by itself a few seconds later. So when
  a bridge transmission has to be ended for it (an operator's STOP, also one pressed a moment
  after the program unkeyed by itself; the 120 s limit; the silence limit; an admin switching or
  disconnecting the radio under it; the server locking) TeleRig **pauses** transmit over the
  bridge. The program can still tune and read; it cannot key until an operator presses
  **Resume transmit**, or an admin switches transmit over the bridge off and on again. Nothing
  else ends a pause: not turning the bridge off and on, not a restart of TeleRig (the pause is
  kept in config.json as `bridge_paused`).
- **The 120 s limit** applies to every transmission, as for a browser. FT8, FT4, JT65, JT9, MSK144
  and WSPR fit; a mode that transmits longer in one go (FST4W-300 and longer, Q65-300) is cut.
- **The program must keep talking.** While it has the radio keyed, it must send TeleRig something
  at least every *silence limit* seconds (10 by default; WSJT-X polls every second). A program
  that hangs is unkeyed after that time. If its connection closes, the radio is unkeyed at once.
- **A radio change closes the connection.** Switching or disconnecting the radio in TeleRig drops
  every bridge program; in WSJT-X press **Retry** and it reads the new radio.
- **Keying is rate-limited.** However often a program (or one that reconnects in a loop) asks, the
  bridge keys at most a few times in a row and then once every two seconds; a refused key answers
  "rejected" and is in the log once.
- **One limit is the radio's.** TeleRig sees a transmission started at a Yaesu itself (front-panel
  PTT, a hand mic) as **ON AIR · FRONT PANEL**, and the bridge waits for it like for any other
  user. An Icom does not report that yet: while someone talks on the Icom's own microphone,
  TeleRig does not know, and a bridge program can retune or key the radio under that
  transmission. With an Icom, disable Tx in WSJT-X (or pause the bridge) before you use the
  radio's own microphone.
- **You are the control operator.** TeleRig never starts a transmission itself. It relays what the
  program asks, and WSJT-X transmits on its own schedule once you enable Tx there. Stay at the
  station, as with any WSJT-X setup.

Every bridge transmission is in the TX log (**Administration > Activity**) with the address the
program connected from, and so are saves of these settings, pauses and resumes.

## What you see in TeleRig

While the bridge is on, a button with two linked boxes sits in the top bar:

| Button | Meaning |
|---|---|
| grey dot | on, no program connected |
| green | a program is connected |
| amber | transmit over the bridge is paused |
| red | a bridge program is transmitting |
| red outline | the port could not be opened |

Click it for the details: the port, who is connected (operators and admins see the addresses),
whether transmit is allowed, and **Pause transmit** / **Resume transmit**. Pausing while the
program is transmitting stops that transmission first.

## A program on another computer

By default only programs on the PC that runs TeleRig can connect. To let another computer in,
choose **Also the computers listed below** and list its address (or a network such as
`192.168.1.0/24`). In the program use the TeleRig PC's address, for example `100.101.102.103:4532`.

Think before you do this:

- **The protocol has no password.** Every program on a listed computer can tune the radio, and key
  it if transmit is allowed, without signing in to TeleRig. List single addresses of computers you
  control, and prefer a private network such as Tailscale.
- Tailscale **Serve** (Administration > Remote) publishes only the web page. The bridge port is
  reached directly at the PC's Tailscale address, and Windows Firewall has to allow it.
- Audio for a program on another computer is not provided yet.

## When something goes wrong

| WSJT-X says | Why |
|---|---|
| IO error while opening the connection | The bridge is off, the port is wrong, no radio is connected in TeleRig, or this computer is not on the list. |
| Access denied (or permission denied) while setting PTT | Transmit over the bridge is off, or paused: look at the bridge button. |
| Command rejected by the rig | Someone else is transmitting, or the radio is being switched. Try again when they finish. |
| Feature not available | Split Operation is set to `Rig`, or the program asked for something the bridge does not offer. |
| Invalid parameter while setting the mode | The connected radio has no such mode (for example Data/Pkt on an IC-728). Use `USB` or `None`. |
| The connection drops | The radio was changed or disconnected in TeleRig, or an admin changed the bridge settings. Press Retry. |
| A transmission stops early and the button turns amber | An operator pressed STOP, the 120 s limit was reached, the program went silent, an admin switched the radio, or the server locked. The button's panel says which. An operator resumes it. |
| WSJT-X keeps showing a rig error after a restart of TeleRig | Transmit over the bridge was paused when TeleRig stopped, and still is: resume it from the bridge button. |

In TeleRig, "the port could not be opened" means another program holds it; the status line in
**Administration > WSJT-X bridge** says so and the bridge retries every ten seconds.

## For developers

| File | Role |
|---|---|
| `backend/rigctl.py` | the rigctld text protocol, pure: parsing, replies, the capability dump, the mode tables |
| `backend/bridge.py` | the TCP server, the connections, pause / resume, the settings |
| `backend/server.py` (`_bridge_apply`) | every bridge command goes through the same policy table and TX arbiter as a WebSocket frame |
| `frontend/bridge.js`, `frontend/admin.js` | the top-bar indicator and the settings tab |

A bridge connection is a connection object like a browser's, with the fixed role `operator` and
the fixed name `WSJT-X BRIDGE` (it has a space, so it can never be an account). It never calls the
radio: each command becomes the canonical command a browser would send (`set_freq`, `set_mode`,
`data_mode`, `ptt`, ...) and is decided by `policy.evaluate`. Its liveness for the TX arbiter is
"a command arrived" with the connection's own limit (`hb_timeout`).

Replies use Hamlib's error codes: `RPRT -1` invalid, `-4` not implemented, `-6` no radio, `-9`
rejected (busy, or the keying rate), `-11` not available, `-22` transmit off or paused. A
connection whose first line is an HTTP request is closed before anything is read as a command: a
web page can make a browser send a request to a local port. A client is expected to wait for each
answer, as Hamlib does: more than 32 unanswered lines close the connection, and a closed
connection's queued lines are dropped, never run (a queued `T 1` cannot key after the program is
gone). With every one of the 6 places taken, the connection silent longest (at least 60 s, never
the transmitting one) makes room for a new one. Setters are paced per address, keying for the
whole bridge. An allow-list entry may name at most a /8 (IPv4) or /32 (IPv6) network.

A `T 0` (unkey) is never refused and never waits: it is answered before any other check, and from the
fast path even while the server is locked; it writes to the radio only when something is keyed.
At shutdown the bridge stops serving and drops its connections before the arbiter's final
release. The admin form sends `expect` (the settings it showed); a save over settings that changed
meanwhile gets `409 conflict`.

Adding a radio: give its mode names a Hamlib name in `rigctl._CIV` / `rigctl._YAESU` and its Hamlib
model number in `rigctl.HAMLIB_MODEL` (see [ADDING-A-RADIO.md](ADDING-A-RADIO.md)).

Tests: `python -m backend.rigctl_test`, `python -m backend.bridge_test`, and the `bridge` group of
`tests/run_phase1.py` (real server, simulator only; it also runs the real Hamlib client when one
is installed, or the one named in `TELERIG_TEST_RIGCTL`).
