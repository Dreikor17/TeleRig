/* The Spots panel's Calling view (spots.js): fixed frequencies on the US amateur bands that are worth a
 * click. Each band's calling frequencies (SSB / CW and FM simplex on 6 m and up, low-power CW and SSB
 * on HF), its FT8 dial frequency, the SSTV frequencies, and a few things to listen to (the 14.300 nets,
 * the NCDXF beacons, APRS, the space station). United States only (IARU Region 2), like the band plan
 * (bandplan.js); a click only tunes, it never transmits.
 *
 * Each band: its name and its rows, lowest frequency first: [kHz, mode, name, note]. The mode is a
 * spot's (CW | SSB | DIGI | FM), so a click sets the frequency, the mode in the radio's own names (SSB
 * by band, DIGI as DATA-USB or USB with the Icom DATA switch) and the filter for it, exactly as a
 * click on a spot does. A band shows only on a radio that has it. Names are at most 14 characters and
 * notes 34, so a row reads whole in the narrowest panel (tests/spots_ui_test.js checks both).
 *
 * Sources, read 2026-11-21:
 *   ARRL band plan (arrl.org/band-plan): 1.810 and 1.910 QRP; SSTV 3.845, 7.171, 14.230, 21.340, 28.680;
 *     NCDXF 14.100; 29.600 FM simplex; 50.125 SSB calling; 52.525 primary FM simplex; 144.200 and 146.52
 *     national calling; 432.10 calling; 446.00 national simplex; 1294.500 FM simplex calling; 1296.100 CW,
 *     SSB calling
 *   ARRL Considerate Operator's Frequency Guide (arrl.org/considerate-operator): the QRP calling
 *     frequencies on 80, 40, 20, 15 and 10 m (CW 3.560, 7.030, 14.060, 21.060, 28.060; SSB 3.985, 7.285,
 *     14.285, 21.385, 28.385) and the beacons on 18.110, 21.150, 24.930, 28.200
 *   QRP ARCI (qrparci.org/qrp-operating-frequencies): 30, 17 and 12 m, which the ARRL lists leave out
 *     (CW 10.116, 18.086, 24.906; SSB 18.130, 24.950)
 *   WSJT-X 3.0's default frequency list (models/FrequencyList.cpp): the FT8 dial frequencies
 *   NCDXF / IARU International Beacon Project (ncdxf.org/beacon): 18 beacons, 10 s each, in turn
 *   ARISS (ariss.org/contact-the-iss.html): the space station's voice downlink 145.800 and its FM
 *     repeater's downlink 437.800
 *   APRS in North America: 144.390 (aprs.org; the IARU Region 2 band plan)
 *   14.300: the Maritime Mobile Service Network (mmsn.org), the Intercon net and the Pacific Seafarer's Net
 *   60 m: 5.357 is inside the 5351.5-5366.5 kHz segment US amateurs have had since 2026-02-13 (47 CFR
 *     97.303(h)), where the limit is 9.15 W ERP
 */
(function (global) {
  "use strict";
  const QRP_CW = "Low-power CW", QRP_SSB = "Low-power SSB", FT8 = "Dial frequency, USB", SSTV = "Analog pictures";
  const BEACON = "18 beacons in turn, 10 s each", SIMPLEX = "National FM simplex", WEAK = "Weak-signal SSB and CW";

  global.CALL_FREQS = [
    { band: "160 m", rows: [
      [1810, "CW", "QRP calling", QRP_CW],
      [1840, "DIGI", "FT8", FT8],
      [1910, "SSB", "QRP calling", QRP_SSB],
    ] },
    { band: "80 m", rows: [
      [3560, "CW", "QRP calling", QRP_CW],
      [3573, "DIGI", "FT8", FT8],
      [3845, "SSB", "SSTV", SSTV],
      [3985, "SSB", "QRP calling", QRP_SSB],
    ] },
    { band: "60 m", rows: [
      [5357, "DIGI", "FT8", "Dial, USB. 9.15 W ERP at most."],
    ] },
    { band: "40 m", rows: [
      [7030, "CW", "QRP calling", QRP_CW],
      [7074, "DIGI", "FT8", FT8],
      [7171, "SSB", "SSTV", SSTV],
      [7285, "SSB", "QRP calling", QRP_SSB],
    ] },
    { band: "30 m", rows: [
      [10116, "CW", "QRP calling", QRP_CW],
      [10136, "DIGI", "FT8", FT8],
    ] },
    { band: "20 m", rows: [
      [14060, "CW", "QRP calling", QRP_CW],
      [14074, "DIGI", "FT8", FT8],
      [14100, "CW", "NCDXF beacons", BEACON],
      [14230, "SSB", "SSTV", SSTV],
      [14285, "SSB", "QRP calling", QRP_SSB],
      [14300, "SSB", "Maritime net", "Maritime nets. Listen first."],
    ] },
    { band: "17 m", rows: [
      [18086, "CW", "QRP calling", QRP_CW],
      [18100, "DIGI", "FT8", FT8],
      [18110, "CW", "NCDXF beacons", BEACON],
      [18130, "SSB", "QRP calling", QRP_SSB],
    ] },
    { band: "15 m", rows: [
      [21060, "CW", "QRP calling", QRP_CW],
      [21074, "DIGI", "FT8", FT8],
      [21150, "CW", "NCDXF beacons", BEACON],
      [21340, "SSB", "SSTV", SSTV],
      [21385, "SSB", "QRP calling", QRP_SSB],
    ] },
    { band: "12 m", rows: [
      [24906, "CW", "QRP calling", QRP_CW],
      [24915, "DIGI", "FT8", FT8],
      [24930, "CW", "NCDXF beacons", BEACON],
      [24950, "SSB", "QRP calling", QRP_SSB],
    ] },
    { band: "10 m", rows: [
      [28060, "CW", "QRP calling", QRP_CW],
      [28074, "DIGI", "FT8", FT8],
      [28200, "CW", "NCDXF beacons", BEACON],
      [28385, "SSB", "QRP calling", QRP_SSB],
      [28680, "SSB", "SSTV", SSTV],
      [29600, "FM", "FM calling", SIMPLEX],
    ] },
    { band: "6 m", rows: [
      [50125, "SSB", "SSB calling", "Call here, then move up the band"],
      [50313, "DIGI", "FT8", FT8],
      [52525, "FM", "FM calling", SIMPLEX],
    ] },
    { band: "2 m", rows: [
      [144174, "DIGI", "FT8", FT8],
      [144200, "SSB", "SSB calling", WEAK],
      [144390, "FM", "APRS", "Packet positions, North America"],
      [145800, "FM", "ISS downlink", "Space station voice. Listen only."],
      [146520, "FM", "FM calling", SIMPLEX],
    ] },
    { band: "70 cm", rows: [
      [432100, "SSB", "SSB/CW calling", WEAK],
      [432174, "DIGI", "FT8", FT8],
      [437800, "FM", "ISS repeater", "Repeater downlink. Listen only."],
      [446000, "FM", "FM calling", SIMPLEX],
    ] },
    { band: "23 cm", rows: [
      [1294500, "FM", "FM calling", SIMPLEX],
      [1296100, "SSB", "SSB/CW calling", WEAK],
      [1296174, "DIGI", "FT8", FT8],
    ] },
  ];
})(window);
