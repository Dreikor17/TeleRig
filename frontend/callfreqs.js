/* The Spots panel's Calling view (spots.js): fixed frequencies on the US amateur bands that are worth a
 * click. Each band's calling frequencies (SSB / CW and FM simplex on 6 m and up, low-power CW and SSB
 * on HF), its FT8 dial frequency, the SSTV frequencies, W1AW's CW, the 60 m channels, and a few things to
 * listen to (the 14.300 nets, the NCDXF beacons, APRS, the space station); then the time signals, which
 * are off the amateur bands. United States only (IARU Region 2), like the band plan (bandplan.js); a
 * click tunes, it never transmits.
 *
 * Each group: its name and its rows, lowest frequency first: [kHz, mode, name, note, modem]. The mode is
 * a spot's (CW | SSB | DIGI | FM | AM), so a click sets the frequency, the mode in the radio's own names
 * (SSB by band, LSB below 10 MHz but USB on 60 m; DIGI as DATA-USB or USB with the Icom DATA switch)
 * and the filter for it, exactly as a click on a spot does. modem, where a row has one, is the Modem
 * window's mode for it (cw | ft8 | sstv): the click shows that mode there too, receive only. An SSTV row
 * is its own category (its tag and the mode filter say SSTV) and tunes by its mode: SSB by band on HF,
 * FM for the space station's picture events. A group shows only the rows on the radio's bands; a group
 * with listen: true (the time signals) shows the rows the radio receives (its rx_ranges), off the
 * amateur bands too. A row may share its frequency with one of another category (the space station's
 * voice and pictures). Names are at most 14 characters and notes 34, so a row reads whole in the
 * narrowest panel (tests/spots_ui_test.js checks both).
 *
 * Sources, read 2026-11-21 (and 2026-11-22: QRP ARCI, WSJT-X, W1AW, 60 m and the SSTV frequencies again):
 *   ARRL band plan (arrl.org/band-plan): 1.810 and 1.910 QRP; SSTV 3.845, 7.171, 14.230, 21.340, 28.680;
 *     NCDXF 14.100; 29.600 FM simplex; 50.125 SSB calling; 52.525 primary FM simplex; 144.200 and 146.52
 *     national calling; 432.10 calling; 446.00 national simplex; 1294.500 FM simplex calling; 1296.100 CW,
 *     SSB calling
 *   SSTV: LSB below 10 MHz, USB above (the voice sideband); 14.227 and 14.233 take 14.230's overflow (3 kHz
 *     either side), and 14.233 carries digital SSTV too (qsl.net/w4csl/sstvfreqs.htm, qsl.net/w9fe)
 *   ARRL Considerate Operator's Frequency Guide (arrl.org/considerate-operator): the QRP calling
 *     frequencies on 80, 40, 20, 15 and 10 m (CW 3.560, 7.030, 14.060, 21.060, 28.060; SSB 3.985, 7.285,
 *     14.285, 21.385, 28.385) and the beacons on 18.110, 21.150, 24.930, 28.200
 *   QRP ARCI (qrparci.org/qrp-update/qrp-frequency, "centers of activity"): 30, 17 and 12 m, which the
 *     ARRL lists leave out (CW 10.106, 18.096, 24.906; SSB 18.130, 24.950). It lists 10.116 on 30 m too,
 *     the European one; 18.086 is G-QRP's, not QRP ARCI's.
 *   WSJT-X 3.0's default frequency list (models/FrequencyList.cpp): the FT8 dial frequencies from 160 m to
 *     2 m. It has none for 60 m, 70 cm or 23 cm: 5.357 is the US 60 m one, and 432.174 and 1296.174 follow
 *     144.174 as used in North America (sigidwiki.com/wiki/FT8)
 *   W1AW (arrl.org/w1aw-operating-schedule, updated 2026-03-09): code practice, CW bulletins and
 *     qualifying runs on 1.8025, 3.5815, 7.0475, 14.0475, 18.0775, 21.0675, 28.0675 MHz (and 50.350 and
 *     147.555, whose mode the page doesn't give: left out)
 *   NCDXF / IARU International Beacon Project (ncdxf.org/beacon): 18 beacons, 10 s each, in turn
 *   ARISS (ariss.org/contact-the-iss.html): the space station's voice downlink 145.800 (its SSTV events
 *     too, in FM) and its FM repeater's downlink 437.800
 *   APRS in North America: 144.390 (aprs.org; the IARU Region 2 band plan)
 *   14.300: the Maritime Mobile Service Network (mmsn.org), the Intercon net and the Pacific Seafarer's Net
 *   60 m (arrl.org/news/new-60-meter-frequencies-available-as-of-february-13; 47 CFR 97.303(h)): since
 *     2026-02-13 the 5351.5-5366.5 kHz segment, where 5.357 is, at 9.15 W ERP, and four channels outside it
 *     at 100 W ERP, centered on 5332, 5348, 5373 and 5405 kHz: USB with the dial 1.5 kHz below the center
 *     (arrl.org/frequency-allocations). The old 5358.5 channel is part of the segment now.
 *   NIST: WWV (and WWVH, the four lower ones) on 2.5, 5, 10, 15 and 20 MHz, in AM
 */
(function (global) {
  "use strict";
  const QRP_CW = "Low-power CW", QRP_SSB = "Low-power SSB", FT8 = "Dial frequency, USB", SSTV = "Analog pictures";
  const BEACON = "18 beacons in turn, 10 s each", SIMPLEX = "National FM simplex", WEAK = "Weak-signal SSB and CW";
  const W1AW = "ARRL code practice, bulletins", CH60 = "USB, 100 W ERP at most", TIME = "NIST time signals. Listen only.";

  global.CALL_FREQS = [
    { band: "160 m", rows: [
      [1802.5, "CW", "W1AW", W1AW, "cw"],
      [1810, "CW", "QRP calling", QRP_CW, "cw"],
      [1840, "DIGI", "FT8", FT8, "ft8"],
      [1910, "SSB", "QRP calling", QRP_SSB],
    ] },
    { band: "80 m", rows: [
      [3560, "CW", "QRP calling", QRP_CW, "cw"],
      [3573, "DIGI", "FT8", FT8, "ft8"],
      [3581.5, "CW", "W1AW", W1AW, "cw"],
      [3845, "SSB", "SSTV", SSTV, "sstv"],
      [3985, "SSB", "QRP calling", QRP_SSB],
    ] },
    { band: "60 m", rows: [
      [5330.5, "SSB", "Channel 5332", CH60],
      [5346.5, "SSB", "Channel 5348", CH60],
      [5357, "DIGI", "FT8", "Dial, USB. 9.15 W ERP at most.", "ft8"],
      [5371.5, "SSB", "Channel 5373", CH60],
      [5403.5, "SSB", "Channel 5405", CH60],
    ] },
    { band: "40 m", rows: [
      [7030, "CW", "QRP calling", QRP_CW, "cw"],
      [7047.5, "CW", "W1AW", W1AW, "cw"],
      [7074, "DIGI", "FT8", FT8, "ft8"],
      [7171, "SSB", "SSTV", SSTV, "sstv"],
      [7285, "SSB", "QRP calling", QRP_SSB],
    ] },
    { band: "30 m", rows: [
      [10106, "CW", "QRP calling", QRP_CW, "cw"],
      [10136, "DIGI", "FT8", FT8, "ft8"],
    ] },
    { band: "20 m", rows: [
      [14047.5, "CW", "W1AW", W1AW, "cw"],
      [14060, "CW", "QRP calling", QRP_CW, "cw"],
      [14074, "DIGI", "FT8", FT8, "ft8"],
      [14100, "CW", "NCDXF beacons", BEACON, "cw"],
      [14227, "SSB", "SSTV overflow", "When 14.230 is busy", "sstv"],
      [14230, "SSB", "SSTV", "Analog pictures, the busiest", "sstv"],
      [14233, "SSB", "SSTV overflow", "Digital SSTV too: not decoded", "sstv"],
      [14285, "SSB", "QRP calling", QRP_SSB],
      [14300, "SSB", "Maritime net", "Maritime nets. Listen first."],
    ] },
    { band: "17 m", rows: [
      [18077.5, "CW", "W1AW", W1AW, "cw"],
      [18096, "CW", "QRP calling", QRP_CW, "cw"],
      [18100, "DIGI", "FT8", FT8, "ft8"],
      [18110, "CW", "NCDXF beacons", BEACON, "cw"],
      [18130, "SSB", "QRP calling", QRP_SSB],
    ] },
    { band: "15 m", rows: [
      [21060, "CW", "QRP calling", QRP_CW, "cw"],
      [21067.5, "CW", "W1AW", W1AW, "cw"],
      [21074, "DIGI", "FT8", FT8, "ft8"],
      [21150, "CW", "NCDXF beacons", BEACON, "cw"],
      [21340, "SSB", "SSTV", SSTV, "sstv"],
      [21385, "SSB", "QRP calling", QRP_SSB],
    ] },
    { band: "12 m", rows: [
      [24906, "CW", "QRP calling", QRP_CW, "cw"],
      [24915, "DIGI", "FT8", FT8, "ft8"],
      [24930, "CW", "NCDXF beacons", BEACON, "cw"],
      [24950, "SSB", "QRP calling", QRP_SSB],
    ] },
    { band: "10 m", rows: [
      [28060, "CW", "QRP calling", QRP_CW, "cw"],
      [28067.5, "CW", "W1AW", W1AW, "cw"],
      [28074, "DIGI", "FT8", FT8, "ft8"],
      [28200, "CW", "NCDXF beacons", BEACON, "cw"],
      [28385, "SSB", "QRP calling", QRP_SSB],
      [28680, "SSB", "SSTV", SSTV, "sstv"],
      [29600, "FM", "FM calling", SIMPLEX],
    ] },
    { band: "6 m", rows: [
      [50125, "SSB", "SSB calling", "Call here, then move up the band"],
      [50313, "DIGI", "FT8", FT8, "ft8"],
      [52525, "FM", "FM calling", SIMPLEX],
    ] },
    { band: "2 m", rows: [
      [144174, "DIGI", "FT8", FT8, "ft8"],
      [144200, "SSB", "SSB calling", WEAK],
      [144390, "FM", "APRS", "Packet positions, North America"],
      [145800, "FM", "ISS downlink", "Space station voice. Listen only."],
      [145800, "FM", "ISS SSTV", "Picture events. Listen only.", "sstv"],
      [146520, "FM", "FM calling", SIMPLEX],
    ] },
    { band: "70 cm", rows: [
      [432100, "SSB", "SSB/CW calling", WEAK],
      [432174, "DIGI", "FT8", FT8, "ft8"],
      [437800, "FM", "ISS repeater", "Repeater downlink. Listen only."],
      [446000, "FM", "FM calling", SIMPLEX],
    ] },
    { band: "23 cm", rows: [
      [1294500, "FM", "FM calling", SIMPLEX],
      [1296100, "SSB", "SSB/CW calling", WEAK],
      [1296174, "DIGI", "FT8", FT8, "ft8"],
    ] },
    { band: "Time signals", listen: true, rows: [
      [2500, "AM", "WWV", TIME],
      [5000, "AM", "WWV", TIME],
      [10000, "AM", "WWV", TIME],
      [15000, "AM", "WWV", TIME],
      [20000, "AM", "WWV", TIME],
    ] },
  ];
})(window);
