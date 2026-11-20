/* The FT8 test signal maker for tests/ft8_test.js: a reference ENCODER, for the tests only (TeleRig ships no
 * FT8 transmit). Ported from ft8_lib by Kārlis Goba, YL3JG (https://github.com/kgoba/ft8_lib, commit
 * 9fec6ca, MIT licence; the notice is in frontend/ft8-decode.js and tests/data/ft8/NOTICE): the message
 * packing (ft8/message.c: standard, nonstandard type 4, free text, telemetry), the CRC (ft8/crc.c) and the
 * LDPC encoder and tone mapping (ft8/encode.c). The synthesiser (continuous-phase FSK) and the seeded noise
 * are TeleRig's own.
 *
 *   pack(text) -> 10-byte payload (77 bits) | null;  tones(payload) -> 79 tones;
 *   add(buf, rate, tones, { f0, start, snr })  adds one signal: f0 the lowest tone (Hz), start in s from the
 *   buffer's start, snr in dB in 2500 Hz against noise(buf, rate, sigma, seed)'s level
 */
"use strict";

const COSTAS = [3, 1, 4, 0, 6, 5, 2];
const GRAY = [0, 1, 3, 2, 5, 6, 4, 7];
  // LDPC (174,91) generator: 83 parity rows of 91 bits (MSB first), from ft8_lib's ft8/constants.c (MIT)
  const LDPC_GEN = new Uint8Array([
    131, 41, 206, 17, 191, 49, 234, 245, 9, 242, 127, 192, 118, 28, 38, 78, 37, 194, 89, 51, 84, 147, 19, 32,
    220, 38, 89, 2, 251, 39, 124, 100, 16, 161, 189, 192, 27, 63, 65, 120, 88, 205, 45, 211, 62, 199, 246, 32,
    9, 253, 164, 254, 224, 65, 149, 253, 3, 71, 131, 160, 7, 124, 204, 193, 27, 136, 115, 237, 92, 61, 72, 160,
    41, 182, 42, 254, 60, 160, 54, 244, 254, 26, 157, 160, 96, 84, 250, 245, 243, 93, 150, 211, 176, 200, 195, 224,
    226, 7, 152, 228, 49, 14, 237, 39, 136, 74, 233, 0, 119, 92, 156, 8, 232, 14, 38, 221, 174, 86, 49, 128,
    176, 184, 17, 2, 140, 43, 249, 151, 33, 52, 135, 192, 24, 160, 201, 35, 31, 198, 10, 223, 92, 94, 163, 32,
    118, 71, 30, 131, 2, 160, 114, 30, 1, 177, 43, 128, 255, 188, 203, 128, 202, 131, 65, 250, 251, 71, 178, 224,
    102, 167, 42, 21, 143, 147, 37, 162, 191, 103, 23, 0, 196, 36, 54, 137, 254, 133, 177, 197, 19, 99, 161, 128,
    13, 255, 115, 148, 20, 209, 161, 179, 75, 28, 39, 0, 21, 180, 136, 48, 99, 108, 139, 153, 137, 73, 114, 224,
    41, 168, 156, 13, 61, 232, 29, 102, 84, 137, 176, 224, 79, 18, 111, 55, 250, 81, 203, 230, 27, 214, 185, 64,
    153, 196, 114, 57, 208, 217, 125, 60, 132, 224, 148, 0, 25, 25, 183, 81, 25, 118, 86, 33, 187, 79, 30, 128,
    9, 219, 18, 215, 49, 250, 238, 11, 134, 223, 107, 128, 72, 143, 195, 61, 244, 63, 189, 238, 164, 234, 251, 64,
    130, 116, 35, 238, 64, 182, 117, 247, 86, 235, 95, 224, 171, 225, 151, 196, 132, 203, 116, 117, 113, 68, 169, 160,
    43, 80, 14, 75, 192, 236, 90, 109, 43, 219, 221, 0, 196, 116, 170, 83, 215, 2, 24, 118, 22, 105, 54, 0,
    142, 186, 26, 19, 219, 51, 144, 189, 103, 24, 206, 192, 117, 56, 68, 103, 58, 39, 120, 44, 196, 32, 18, 224,
    6, 255, 131, 161, 69, 195, 112, 53, 165, 193, 38, 128, 59, 55, 65, 120, 88, 204, 45, 211, 62, 195, 246, 32,
    154, 74, 90, 40, 238, 23, 202, 156, 50, 72, 66, 192, 188, 41, 244, 101, 48, 156, 151, 126, 137, 97, 10, 64,
    38, 99, 174, 109, 223, 139, 92, 226, 187, 41, 72, 128, 70, 242, 49, 239, 228, 87, 3, 76, 24, 20, 65, 128,
    63, 178, 206, 133, 171, 233, 176, 199, 46, 6, 251, 224, 222, 135, 72, 31, 40, 44, 21, 57, 113, 160, 162, 224,
    252, 215, 204, 242, 60, 105, 250, 153, 187, 161, 65, 32, 240, 38, 20, 71, 233, 73, 12, 168, 228, 116, 206, 192,
    68, 16, 17, 88, 24, 25, 111, 149, 205, 215, 1, 32, 8, 143, 195, 29, 244, 191, 189, 226, 164, 234, 251, 64,
    184, 254, 241, 182, 48, 119, 41, 251, 10, 7, 140, 0, 90, 254, 167, 172, 204, 183, 123, 188, 157, 153, 169, 0,
    73, 167, 1, 106, 198, 83, 246, 94, 205, 201, 7, 96, 25, 68, 208, 133, 190, 78, 125, 168, 214, 204, 125, 0,
    37, 31, 98, 173, 196, 3, 47, 14, 231, 20, 0, 32, 86, 71, 31, 135, 2, 160, 114, 30, 0, 177, 43, 128,
    43, 142, 73, 35, 242, 221, 81, 226, 213, 55, 250, 0, 107, 85, 10, 64, 166, 111, 71, 85, 222, 149, 194, 96,
    161, 138, 210, 141, 78, 39, 254, 146, 164, 246, 200, 64, 16, 194, 229, 134, 56, 140, 184, 42, 61, 128, 117, 128,
    239, 52, 164, 24, 23, 238, 2, 19, 61, 178, 235, 0, 126, 156, 12, 84, 50, 90, 156, 21, 131, 110, 0, 0,
    54, 147, 229, 114, 209, 253, 228, 205, 240, 121, 232, 96, 191, 178, 206, 197, 171, 225, 176, 199, 46, 7, 251, 224,
    126, 225, 130, 48, 197, 131, 204, 204, 87, 212, 176, 128, 160, 102, 203, 47, 237, 175, 201, 245, 38, 100, 18, 96,
    187, 35, 114, 90, 188, 71, 204, 95, 76, 196, 205, 32, 222, 217, 219, 163, 190, 228, 12, 89, 181, 96, 155, 64,
    217, 167, 1, 106, 198, 83, 230, 222, 205, 201, 3, 96, 154, 212, 106, 237, 95, 112, 127, 40, 10, 181, 252, 64,
    229, 146, 28, 119, 130, 37, 135, 49, 109, 125, 60, 32, 79, 20, 218, 130, 66, 168, 184, 109, 202, 115, 53, 32,
    139, 139, 80, 122, 212, 103, 212, 68, 29, 247, 112, 224, 34, 131, 28, 156, 241, 22, 148, 103, 173, 4, 182, 128,
    33, 59, 131, 143, 226, 174, 84, 195, 142, 231, 24, 0, 93, 146, 107, 109, 215, 31, 8, 81, 129, 164, 225, 32,
    102, 171, 121, 212, 178, 158, 230, 230, 149, 9, 229, 96, 149, 129, 72, 104, 45, 116, 138, 56, 221, 104, 186, 160,
    184, 206, 2, 12, 240, 105, 195, 42, 114, 58, 177, 64, 244, 51, 29, 109, 70, 22, 7, 233, 87, 82, 116, 96,
    109, 162, 59, 164, 36, 185, 89, 97, 51, 207, 156, 128, 166, 54, 188, 188, 123, 48, 197, 251, 234, 230, 127, 224,
    92, 176, 216, 106, 7, 223, 101, 74, 144, 137, 162, 0, 241, 31, 16, 104, 72, 120, 15, 201, 236, 221, 128, 160,
    31, 187, 83, 100, 251, 141, 44, 157, 115, 13, 91, 160, 252, 184, 107, 199, 10, 80, 201, 208, 42, 93, 3, 64,
    165, 52, 67, 48, 41, 234, 193, 95, 50, 46, 52, 192, 201, 137, 217, 199, 195, 211, 184, 197, 93, 117, 19, 0,
    123, 179, 139, 47, 1, 134, 212, 102, 67, 174, 150, 32, 38, 68, 235, 173, 235, 68, 185, 70, 125, 31, 66, 192,
    96, 140, 200, 87, 89, 75, 251, 181, 93, 105, 96, 0,
  ]);

// ---- text tables (ft8/text.c) ----
const T_FULL = 0, T_ANS = 1, T_AN_SP = 2, T_LET_SP = 3, T_AN = 4, T_NUM = 5;
function nchar(ch, t) {
  let n = 0;
  if (t !== T_AN && t !== T_NUM) { if (ch === " ") return n; n += 1; }
  if (t !== T_LET_SP) { if (ch >= "0" && ch <= "9") return n + ch.charCodeAt(0) - 48; n += 10; }
  if (t !== T_NUM) { if (ch >= "A" && ch <= "Z") return n + ch.charCodeAt(0) - 65; n += 26; }
  if (t === T_FULL) { const i = "+-./?".indexOf(ch); if (i >= 0) return n + i; } else if (t === T_ANS) { if (ch === "/") return n; }
  return -1;
}

// ---- message packing (ft8/message.c) ----
const MAX22 = 4194304, NTOKENS = 2063592, MAXGRID4 = 32400;
const M64 = (1n << 64n) - 1n;
function hash22(call) {
  let n58 = 0n, i = 0;
  for (; i < call.length && i < 11; i++) { const j = nchar(call[i], T_ANS); if (j < 0) return -1; n58 = n58 * 38n + BigInt(j); }
  for (; i < 11; i++) n58 *= 38n;
  return Number((((47055833459n * n58) & M64) >> 42n) & 0x3FFFFFn);
}
function packBasecall(c, len) {
  if (len <= 2) return -1;
  const c6 = [" ", " ", " ", " ", " ", " "];
  const put = (s, at) => { for (let i = 0; i < s.length && at + i < 6; i++) c6[at + i] = s[i]; };
  if (c.startsWith("3DA0") && len > 4 && len <= 7) { put("3D0", 0); put(c.slice(4, len), 3); }
  else if (c.startsWith("3X") && /[A-Z]/.test(c[2]) && len <= 7) { put("Q", 0); put(c.slice(2, len), 1); }
  else if (/[0-9]/.test(c[2]) && len <= 6) put(c.slice(0, len), 0);
  else if (/[0-9]/.test(c[1]) && len <= 5) put(c.slice(0, len), 1);
  const i0 = nchar(c6[0], T_AN_SP), i1 = nchar(c6[1], T_AN), i2 = nchar(c6[2], T_NUM);
  const i3 = nchar(c6[3], T_LET_SP), i4 = nchar(c6[4], T_LET_SP), i5 = nchar(c6[5], T_LET_SP);
  if (i0 < 0 || i1 < 0 || i2 < 0 || i3 < 0 || i4 < 0 || i5 < 0) return -1;
  return ((((i0 * 36 + i1) * 10 + i2) * 27 + i3) * 27 + i4) * 27 + i5;
}
function cqModifier(s) {                         // "CQ nnn" or "CQ a[bcd]"
  let nnum = 0, nlet = 0, m = 0;
  for (let i = 3; i < 8 && i < s.length && s[i] !== " "; i++) {
    if (/[0-9]/.test(s[i])) nnum++;
    else if (/[A-Z]/.test(s[i])) { nlet++; m = 27 * m + (s.charCodeAt(i) - 64); }
    else return -1;
  }
  if (nnum === 3 && nlet === 0) return parseInt(s.slice(3), 10);
  if (nnum === 0 && nlet <= 4) return 1000 + m;
  return -1;
}
function pack28(call) {                          // -> [n28, ip] | null
  if (call === "DE") return [0, 0];
  if (call === "QRZ") return [1, 0];
  if (call === "CQ") return [2, 0];
  if (call.startsWith("CQ ") && call.length < 8) { const v = cqModifier(call); return v < 0 ? null : [3 + v, 0]; }
  let ip = 0, lenBase = call.length;
  if (call.endsWith("/P") || call.endsWith("/R")) { ip = 1; lenBase -= 2; }
  const n = packBasecall(call, lenBase);
  if (n >= 0) return [NTOKENS + MAX22 + n, ip];
  if (call.length >= 3 && call.length <= 11) { const h = hash22(call); return h < 0 ? null : [NTOKENS + h, 0]; }
  return null;
}
function packGrid(g) {
  if (!g) return MAXGRID4 + 1;
  if (g === "RRR") return MAXGRID4 + 2;
  if (g === "RR73") return MAXGRID4 + 3;
  if (g === "73") return MAXGRID4 + 4;
  if (/^[A-R][A-R][0-9][0-9]$/.test(g)) return (((g.charCodeAt(0) - 65) * 18 + (g.charCodeAt(1) - 65)) * 10 + (g.charCodeAt(2) - 48)) * 10 + (g.charCodeAt(3) - 48);
  if (g[0] === "R") return (MAXGRID4 + 35 + parseInt(g.slice(1), 10)) | 0x8000;
  return MAXGRID4 + 35 + parseInt(g, 10);
}
function encodeStd(to, de, extra) {
  const a = pack28(to), b = pack28(de);
  if (!a || !b) return null;
  let i3 = 1;
  if (to.endsWith("/P") || de.endsWith("/P")) i3 = 2;
  const slash = de.indexOf("/");
  if (slash >= 2 && (to === "CQ" || to.startsWith("CQ ")) && de.slice(slash) !== "/P" && de.slice(slash) !== "/R") return null;
  const g = packGrid(extra);
  const n29a = (a[0] * 2 + a[1]) >>> 0, n29b = (b[0] * 2 + b[1]) >>> 0;
  const p = new Uint8Array(10);
  p[0] = n29a >>> 21; p[1] = n29a >>> 13; p[2] = n29a >>> 5;
  p[3] = ((n29a << 3) & 0xF8) | (n29b >>> 26); p[4] = n29b >>> 18; p[5] = n29b >>> 10; p[6] = n29b >>> 2;
  p[7] = ((n29b << 6) & 0xC0) | (g >>> 10); p[8] = g >>> 2; p[9] = ((g << 6) & 0xC0) | (i3 << 3);
  return p;
}
function encodeNonstd(to, de, extra) {
  const icq = to === "CQ" || to.startsWith("CQ ") ? 1 : 0;
  if ((!icq && to.length < 3) || de.length < 3) return null;
  let n12 = 0, call58 = de;
  if (!icq) { const h = hash22(to); if (h < 0) return null; n12 = h >> 10; }
  let n58 = 0n;
  for (const ch of call58.replace(/^</, "").slice(0, 11)) { const j = nchar(ch, T_ANS); if (j < 0) return null; n58 = n58 * 38n + BigInt(j); }
  const nrpt = icq ? 0 : extra === "RRR" ? 1 : extra === "RR73" ? 2 : extra === "73" ? 3 : 0, iflip = 0, i3 = 4;
  const B = (v) => Number(v & 0xFFn);
  const p = new Uint8Array(10);
  p[0] = n12 >> 4; p[1] = ((n12 << 4) & 0xF0) | B(n58 >> 54n); p[2] = B(n58 >> 46n); p[3] = B(n58 >> 38n); p[4] = B(n58 >> 30n);
  p[5] = B(n58 >> 22n); p[6] = B(n58 >> 14n); p[7] = B(n58 >> 6n); p[8] = (B(n58 << 2n) & 0xFC) | (iflip << 1) | (nrpt >> 1);
  p[9] = ((nrpt & 1) << 7) | (icq << 6) | (i3 << 3);
  return p;
}
function telemetryPayload(b71) {                 // 71 bits (9 bytes, right-aligned) -> payload, i3.n3 = 0.0 left to the caller
  const p = new Uint8Array(10);
  let carry = 0;
  for (let i = 8; i >= 0; i--) { p[i] = ((b71[i] << 1) | (carry >> 7)) & 0xFF; carry = b71[i] & 0x80; }
  return p;
}
function encodeFree(text) {
  if (text.length > 13) return null;
  const b71 = new Uint8Array(9);
  for (let idx = 0; idx < 13; idx++) {
    const cid = nchar(idx < text.length ? text[idx] : " ", T_FULL);
    if (cid < 0) return null;
    let rem = cid;
    for (let i = 8; i >= 0; i--) { rem += b71[i] * 42; b71[i] = rem & 0xFF; rem >>= 8; }
  }
  const p = telemetryPayload(b71);
  p[9] = 0;
  return p;
}
function encodeTelemetry(hex) {                  // 18 hex digits; the first must be 0..7 (71 bits)
  const b = new Uint8Array(9);
  for (let i = 0; i < 9; i++) b[i] = parseInt(hex.slice(2 * i, 2 * i + 2), 16);
  const p = telemetryPayload(b);                 // payload bits 0..70; bit 71 is 0
  p[8] |= 1;                                     // n3 = 5 (101): its top bit is payload bit 71,
  p[9] = 1 << 6;                                 // its low bits (01) bits 72-73; i3 = 0
  return p;
}
function pack(text) {                            // ftx_message_encode: up to 3 tokens std / nonstd, else free text
  const t = text.trim().replace(/\s+/g, " ").toUpperCase();
  if (/^[0-7][0-9A-F]{17}$/.test(t)) return encodeTelemetry(t);
  const tok = t.split(" ");
  let to, i;
  if (t.startsWith("CQ ")) {                     // "CQ nnn" / "CQ a[bcd]" is one token
    if (cqModifier(t) >= 0) { to = "CQ " + tok[1]; i = 2; } else { to = "CQ"; i = 1; }
  } else { to = tok[0]; i = 1; }
  if (tok.length <= i + 2) {                     // nothing left over: a standard or a type 4 message
    const de = tok[i] || "", extra = tok[i + 1] || "";
    const p = encodeStd(to, de, extra) || encodeNonstd(to, de, extra);
    if (p) return p;
  }
  return encodeFree(t);
}

// ---- CRC + LDPC + tones (ft8/crc.c, ft8/encode.c) ----
function crc14(bytes, bits) {
  let r = 0, idx = 0;
  for (let b = 0; b < bits; b++) {
    if (b % 8 === 0) r ^= bytes[idx++] << 6;
    r = (r & 0x2000 ? (r << 1) ^ 0x2757 : r << 1) & 0xFFFF;
  }
  return r & 0x3FFF;
}
function parity8(x) { x ^= x >> 4; x ^= x >> 2; x ^= x >> 1; return x & 1; }
function tones(payload) {
  const a91 = new Uint8Array(12);
  for (let i = 0; i < 10; i++) a91[i] = payload[i];
  a91[9] &= 0xF8; a91[10] = 0;
  const crc = crc14(a91, 82);
  a91[9] |= crc >> 11; a91[10] = (crc >> 3) & 0xFF; a91[11] = (crc << 5) & 0xFF;
  const cw = new Uint8Array(22);
  for (let j = 0; j < 12; j++) cw[j] = a91[j];
  let mask = 0x80 >> (91 % 8), idx = 11;
  for (let i = 0; i < 83; i++) {
    let nsum = 0;
    for (let j = 0; j < 12; j++) nsum ^= parity8(a91[j] & LDPC_GEN[i * 12 + j]);
    if (nsum) cw[idx] |= mask;
    mask >>= 1;
    if (!mask) { mask = 0x80; idx++; }
  }
  const out = new Uint8Array(79);
  let m = 0x80, ib = 0;
  const bit = () => { const v = cw[ib] & m ? 1 : 0; m >>= 1; if (!m) { m = 0x80; ib++; } return v; };
  for (let s = 0; s < 79; s++) {
    if (s < 7) out[s] = COSTAS[s];
    else if (s >= 36 && s < 43) out[s] = COSTAS[s - 36];
    else if (s >= 72) out[s] = COSTAS[s - 72];
    else { const b3 = (bit() << 2) | (bit() << 1) | bit(); out[s] = GRAY[b3]; }
  }
  return out;
}

// ---- signals and noise (TeleRig's own) ----
function rng(seed) {                             // mulberry32
  let a = seed >>> 0;
  return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
function noise(buf, rate, sigma, seed) {         // white Gaussian noise of standard deviation sigma
  const r = rng(seed || 1);
  for (let i = 0; i < buf.length; i += 2) {
    const u = Math.max(1e-12, r()), v = r(), m = sigma * Math.sqrt(-2 * Math.log(u));
    buf[i] += m * Math.cos(2 * Math.PI * v);
    if (i + 1 < buf.length) buf[i + 1] += m * Math.sin(2 * Math.PI * v);
  }
  return buf;
}
function add(buf, rate, tn, o) {                 // continuous-phase FSK, 6.25 Hz spacing, 0.16 s symbols
  const sigma = o.sigma || 0.05, nb = (sigma * sigma * 2500) / (rate / 2);    // noise power in 2500 Hz
  const amp = Math.sqrt(2 * nb * Math.pow(10, o.snr / 10)), sym = Math.round(rate * 0.16);
  let ph = 0, i = Math.round(o.start * rate);
  for (let s = 0; s < 79; s++) {
    const w = (2 * Math.PI * (o.f0 + tn[s] * 6.25)) / rate;
    for (let k = 0; k < sym; k++, i++) { if (i >= 0 && i < buf.length) buf[i] += amp * Math.sin(ph); ph += w; }
  }
  return buf;
}

// a type 4 message straight away (ft8_lib's pack() tries the standard form first, with a hashed call)
const packNonstd = (to, de, extra) => encodeNonstd(to, de, extra || "");

module.exports = { pack, packNonstd, tones, add, noise, hash22 };
