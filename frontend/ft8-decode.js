/* TeleRig FT8 receive: the decoder. A JavaScript port of the decode path of ft8_lib by Kārlis Goba, YL3JG
 * (https://github.com/kgoba/ft8_lib, commit 9fec6ca, MIT licence, notice below): the waterfall
 * (common/monitor.c), the Costas sync search and the bit likelihoods (ft8/decode.c), the belief-propagation
 * LDPC decoder (ft8/ldpc.c), the CRC (ft8/crc.c), the message unpacking and the callsign hash table
 * (ft8/message.c, ft8/text.c, demo/decode_ft8.c). The FFT and the SNR estimate are TeleRig's own.
 * Receive only: nothing here makes a signal.
 *
 * window.FT8 = { decodeSlot(samples, rate) -> [{ snr, dt, freq, text }], reset(), makeFft(n) }
 *   samples: one 15 s slot of the radio's audio (Float32, -1..1, any rate whose 0.16 s symbol is a whole
 *   number of samples whose factors are 2, 3 and 5: 12000, 16000, 48000 Hz), from the slot's start;
 *   dt is WSJT-X's (the start time less 0.5 s), freq the lowest tone in Hz, snr in a 2500 Hz bandwidth.
 *   The callsign hash table (for <...> calls) lives on across slots, as in WSJT-X; reset() empties it.
 * As a worker (new Worker(this file)): { id, rate, samples } in, { id, decodes } or { id, error } out.
 *
 * ft8_lib's licence:
 *   MIT License
 *
 *   Copyright (c) 2018 Kārlis Goba
 *
 *   Permission is hereby granted, free of charge, to any person obtaining a copy
 *   of this software and associated documentation files (the "Software"), to deal
 *   in the Software without restriction, including without limitation the rights
 *   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 *   copies of the Software, and to permit persons to whom the Software is
 *   furnished to do so, subject to the following conditions:
 *
 *   The above copyright notice and this permission notice shall be included in all
 *   copies or substantial portions of the Software.
 *
 *   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 *   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 *   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 *   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 *   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 *   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 *   SOFTWARE.
 */
(function (G) {
  "use strict";

  // ---- the protocol (ft8/constants.h) ----
  const SYMBOL_PERIOD = 0.16, SLOT_TIME = 15.0;
  const ND = 58, NN = 79, LENGTH_SYNC = 7, NUM_SYNC = 3, SYNC_OFFSET = 36;
  const LDPC_N = 174, LDPC_K = 91, LDPC_M = 83;
  const CRC_POLY = 0x2757, CRC_TOP = 1 << 13;
  const COSTAS = [3, 1, 4, 0, 6, 5, 2];
  const GRAY = [0, 1, 3, 2, 5, 6, 4, 7];
    // LDPC (174,91) parity checks, from ft8_lib's ft8/constants.c (MIT; originally WSJT-X):
  // LDPC_NM: per check (83), the codeword bits it covers (1-based, 7 slots, 0 = unused);
  // LDPC_MN: per codeword bit (174), the 3 checks that cover it (1-based); LDPC_ROWS: bits per check.
  const LDPC_NM = new Uint8Array([
    4, 31, 59, 91, 92, 96, 153, 5, 32, 60, 93, 115, 146, 0, 6, 24, 61, 94, 122, 151, 0,
    7, 33, 62, 95, 96, 143, 0, 8, 25, 63, 83, 93, 96, 148, 6, 32, 64, 97, 126, 138, 0,
    5, 34, 65, 78, 98, 107, 154, 9, 35, 66, 99, 139, 146, 0, 10, 36, 67, 100, 107, 126, 0,
    11, 37, 67, 87, 101, 139, 158, 12, 38, 68, 102, 105, 155, 0, 13, 39, 69, 103, 149, 162, 0,
    8, 40, 70, 82, 104, 114, 145, 14, 41, 71, 88, 102, 123, 156, 15, 42, 59, 106, 123, 159, 0,
    1, 33, 72, 106, 107, 157, 0, 16, 43, 73, 108, 141, 160, 0, 17, 37, 74, 81, 109, 131, 154,
    11, 44, 75, 110, 121, 166, 0, 45, 55, 64, 111, 130, 161, 173, 8, 46, 71, 112, 119, 166, 0,
    18, 36, 76, 89, 113, 114, 143, 19, 38, 77, 104, 116, 163, 0, 20, 47, 70, 92, 138, 165, 0,
    2, 48, 74, 113, 128, 160, 0, 21, 45, 78, 83, 117, 121, 151, 22, 47, 58, 118, 127, 164, 0,
    16, 39, 62, 112, 134, 158, 0, 23, 43, 79, 120, 131, 145, 0, 19, 35, 59, 73, 110, 125, 161,
    20, 36, 63, 94, 136, 161, 0, 14, 31, 79, 98, 132, 164, 0, 3, 44, 80, 124, 127, 169, 0,
    19, 46, 81, 117, 135, 167, 0, 7, 49, 58, 90, 100, 105, 168, 12, 50, 61, 118, 119, 144, 0,
    13, 51, 64, 114, 118, 157, 0, 24, 52, 76, 129, 148, 149, 0, 25, 53, 69, 90, 101, 130, 156,
    20, 46, 65, 80, 120, 140, 170, 21, 54, 77, 100, 140, 171, 0, 35, 82, 133, 142, 171, 174, 0,
    14, 30, 83, 113, 125, 170, 0, 4, 29, 68, 120, 134, 173, 0, 1, 4, 52, 57, 86, 136, 152,
    26, 51, 56, 91, 122, 137, 168, 52, 84, 110, 115, 145, 168, 0, 7, 50, 81, 99, 132, 173, 0,
    23, 55, 67, 95, 172, 174, 0, 26, 41, 77, 109, 141, 148, 0, 2, 27, 41, 61, 62, 115, 133,
    27, 40, 56, 124, 125, 126, 0, 18, 49, 55, 124, 141, 167, 0, 6, 33, 85, 108, 116, 156, 0,
    28, 48, 70, 85, 105, 129, 158, 9, 54, 63, 131, 147, 155, 0, 22, 53, 68, 109, 121, 174, 0,
    3, 13, 48, 78, 95, 123, 0, 31, 69, 133, 150, 155, 169, 0, 12, 43, 66, 89, 97, 135, 159,
    5, 39, 75, 102, 136, 167, 0, 2, 54, 86, 101, 135, 164, 0, 15, 56, 87, 108, 119, 171, 0,
    10, 44, 82, 91, 111, 144, 149, 23, 34, 71, 94, 127, 153, 0, 11, 49, 88, 92, 142, 157, 0,
    29, 34, 87, 97, 147, 162, 0, 30, 50, 60, 86, 137, 142, 162, 10, 53, 66, 84, 112, 128, 165,
    22, 57, 85, 93, 140, 159, 0, 28, 32, 72, 103, 132, 166, 0, 28, 29, 84, 88, 117, 143, 150,
    1, 26, 45, 80, 128, 147, 0, 17, 27, 89, 103, 116, 153, 0, 51, 57, 98, 163, 165, 172, 0,
    21, 37, 73, 138, 152, 169, 0, 16, 47, 76, 130, 137, 154, 0, 3, 24, 30, 72, 104, 139, 0,
    9, 40, 90, 106, 134, 151, 0, 15, 58, 60, 74, 111, 150, 163, 18, 42, 79, 144, 146, 152, 0,
    25, 38, 65, 99, 122, 160, 0, 17, 42, 75, 129, 170, 172, 0,
  ]);
  const LDPC_MN = new Uint8Array([
    16, 45, 73, 25, 51, 62, 33, 58, 78, 1, 44, 45, 2, 7, 61, 3, 6, 54, 4, 35, 48, 5, 13, 21,
    8, 56, 79, 9, 64, 69, 10, 19, 66, 11, 36, 60, 12, 37, 58, 14, 32, 43, 15, 63, 80, 17, 28, 77,
    18, 74, 83, 22, 53, 81, 23, 30, 34, 24, 31, 40, 26, 41, 76, 27, 57, 70, 29, 49, 65, 3, 38, 78,
    5, 39, 82, 46, 50, 73, 51, 52, 74, 55, 71, 72, 44, 67, 72, 43, 68, 78, 1, 32, 59, 2, 6, 71,
    4, 16, 54, 7, 65, 67, 8, 30, 42, 9, 22, 31, 10, 18, 76, 11, 23, 82, 12, 28, 61, 13, 52, 79,
    14, 50, 51, 15, 81, 83, 17, 29, 60, 19, 33, 64, 20, 26, 73, 21, 34, 40, 24, 27, 77, 25, 55, 58,
    35, 53, 66, 36, 48, 68, 37, 46, 75, 38, 45, 47, 39, 57, 69, 41, 56, 62, 20, 49, 53, 46, 52, 63,
    45, 70, 75, 27, 35, 80, 1, 15, 30, 2, 68, 80, 3, 36, 51, 4, 28, 51, 5, 31, 56, 6, 20, 37,
    7, 40, 82, 8, 60, 69, 9, 10, 49, 11, 44, 57, 12, 39, 59, 13, 24, 55, 14, 21, 65, 16, 71, 78,
    17, 30, 76, 18, 25, 80, 19, 61, 83, 22, 38, 77, 23, 41, 50, 7, 26, 58, 29, 32, 81, 33, 40, 73,
    18, 34, 48, 13, 42, 64, 5, 26, 43, 47, 69, 72, 54, 55, 70, 45, 62, 68, 10, 63, 67, 14, 66, 72,
    22, 60, 74, 35, 39, 79, 1, 46, 64, 1, 24, 66, 2, 5, 70, 3, 31, 65, 4, 49, 58, 1, 4, 5,
    6, 60, 67, 7, 32, 75, 8, 48, 82, 9, 35, 41, 10, 39, 62, 11, 14, 61, 12, 71, 74, 13, 23, 78,
    11, 35, 55, 15, 16, 79, 7, 9, 16, 17, 54, 63, 18, 50, 57, 19, 30, 47, 20, 64, 80, 21, 28, 69,
    22, 25, 43, 13, 22, 37, 2, 47, 51, 23, 54, 74, 26, 34, 72, 27, 36, 37, 21, 36, 63, 29, 40, 44,
    19, 26, 57, 3, 46, 82, 14, 15, 58, 33, 52, 53, 30, 43, 52, 6, 9, 52, 27, 33, 65, 25, 69, 73,
    38, 55, 83, 20, 39, 77, 18, 29, 56, 32, 48, 71, 42, 51, 59, 28, 44, 79, 34, 60, 62, 31, 45, 61,
    46, 68, 77, 6, 24, 76, 8, 10, 78, 40, 41, 70, 17, 50, 53, 42, 66, 68, 4, 22, 72, 36, 64, 81,
    13, 29, 47, 2, 8, 81, 56, 67, 73, 5, 38, 50, 12, 38, 64, 59, 72, 80, 3, 26, 79, 45, 76, 81,
    1, 65, 74, 7, 18, 77, 11, 56, 59, 14, 39, 54, 16, 37, 66, 10, 28, 55, 15, 60, 70, 17, 25, 82,
    20, 30, 31, 12, 67, 68, 23, 75, 80, 27, 32, 62, 24, 69, 75, 19, 21, 71, 34, 53, 61, 35, 46, 47,
    33, 59, 76, 40, 43, 83, 41, 42, 63, 49, 75, 83, 20, 44, 48, 42, 49, 57,
  ]);
  const LDPC_ROWS = new Uint8Array([
    7, 6, 6, 6, 7, 6, 7, 6, 6, 7, 6, 6, 7, 7, 6, 6, 6, 7, 6, 7, 6, 7, 6, 6, 6, 7, 6, 6,
    6, 7, 6, 6, 6, 6, 7, 6, 6, 6, 7, 7, 6, 6, 6, 6, 7, 7, 6, 6, 6, 6, 7, 6, 6, 6, 7, 6,
    6, 6, 6, 7, 6, 6, 6, 7, 6, 6, 6, 7, 7, 6, 6, 7, 6, 6, 6, 6, 6, 6, 6, 7, 6, 6, 6,
  ]);

  // ---- the decoder's settings (demo/decode_ft8.c) ----
  const MIN_SCORE = 10, MAX_CANDIDATES = 140, LDPC_ITERATIONS = 25, MAX_DECODED = 50;
  const TIME_OSR = 2, FREQ_OSR = 2, F_MIN = 200, F_MAX = 3000;
  const NOISE_PCT = 0.8, SNR_OFFSET = -24.5;   // the SNR estimate's noise floor and offset (see snrOf)

  // ---- the FFT: mixed radix 2 / 3 / 5, decimation in time (TeleRig's own) ----
  function makeFft(n) {
    const factors = [];
    let m = n;
    for (const p of [2, 3, 5]) while (m % p === 0) { factors.push(p); m /= p; }
    if (m !== 1 || n < 2) throw new Error("FFT size " + n + " must be a product of 2, 3 and 5");
    const twr = new Float64Array(n), twi = new Float64Array(n);
    for (let k = 0; k < n; k++) { const a = (-2 * Math.PI * k) / n; twr[k] = Math.cos(a); twi[k] = Math.sin(a); }
    const outR = new Float64Array(n), outI = new Float64Array(n), sr = new Float64Array(5), si = new Float64Array(5);
    let inR = null, inI = null;
    // X = DFT_N of x[off + q * stride] (q = 0..N-1), N = n / stride, into out[base .. base + N - 1]
    function work(base, off, stride, fi) {
      const p = factors[fi], m2 = n / (stride * p);
      if (m2 === 1) {
        for (let q = 0; q < p; q++) { outR[base + q] = inR[off + q * stride]; outI[base + q] = inI ? inI[off + q * stride] : 0; }
      } else {
        for (let q = 0; q < p; q++) work(base + q * m2, off + q * stride, stride * p, fi + 1);
      }
      const np = n / p;
      for (let k1 = 0; k1 < m2; k1++) {         // X[k1 + m2 k2] = sum_q (W_N^(q k1) Y_q[k1]) W_p^(q k2)
        for (let q = 0; q < p; q++) {
          const pos = base + q * m2 + k1, t = (stride * q * k1) % n, a = outR[pos], b = outI[pos];
          sr[q] = a * twr[t] - b * twi[t];
          si[q] = a * twi[t] + b * twr[t];
        }
        for (let k2 = 0; k2 < p; k2++) {
          let ar = 0, ai = 0;
          for (let q = 0; q < p; q++) {
            const t = np * ((q * k2) % p);
            ar += sr[q] * twr[t] - si[q] * twi[t];
            ai += sr[q] * twi[t] + si[q] * twr[t];
          }
          outR[base + k1 + m2 * k2] = ar;
          outI[base + k1 + m2 * k2] = ai;
        }
      }
    }
    // fft(re, im?) -> [re, im] (the output arrays are reused by the next call)
    return function fft(re, im) { inR = re; inI = im || null; work(0, 0, 1, 0); return [outR, outI]; };
  }

  // ---- the waterfall (common/monitor.c): log magnitudes, uint8 0.5 dB steps from -120 dB ----
  const ffts = new Map();
  function waterfall(samples, rate) {
    const block = Math.round(rate * SYMBOL_PERIOD);
    if (!(block >= 32) || !(Math.abs(block - rate * SYMBOL_PERIOD) <= 1e-6)) throw new Error("FT8 needs a whole number of samples per symbol");
    const sub = block / TIME_OSR, nfft = block * FREQ_OSR, norm = 2 / nfft;
    if (!ffts.has(nfft)) ffts.set(nfft, makeFft(nfft));
    const fft = ffts.get(nfft);
    const win = new Float64Array(nfft);
    for (let i = 0; i < nfft; i++) { const x = Math.sin((Math.PI * i) / nfft); win[i] = norm * x * x; }
    const minBin = Math.floor(F_MIN * SYMBOL_PERIOD), maxBin = Math.floor(F_MAX * SYMBOL_PERIOD) + 1;
    const numBins = maxBin - minBin, maxBlocks = Math.floor(SLOT_TIME / SYMBOL_PERIOD);
    const stride = TIME_OSR * FREQ_OSR * numBins;
    const mag = new Uint8Array(maxBlocks * stride);
    const last = new Float64Array(nfft), td = new Float64Array(nfft);
    let blocks = 0;
    for (let pos = 0; pos + block <= samples.length && blocks < maxBlocks; pos += block, blocks++) {
      let off = blocks * stride, fp = pos;
      for (let ts = 0; ts < TIME_OSR; ts++) {
        last.copyWithin(0, sub);                 // shift the new subblock into the analysis frame
        for (let i = nfft - sub; i < nfft; i++) last[i] = samples[fp++];
        for (let i = 0; i < nfft; i++) td[i] = win[i] * last[i];
        const [re, im] = fft(td);
        for (let fs = 0; fs < FREQ_OSR; fs++) {
          for (let bin = minBin; bin < maxBin; bin++) {
            const k = bin * FREQ_OSR + fs, db = 10 * Math.log10(1e-12 + re[k] * re[k] + im[k] * im[k]);
            const v = Math.trunc(2 * db + 240);
            mag[off++] = v < 0 ? 0 : v > 255 ? 255 : v;
          }
        }
      }
    }
    return { mag, blocks, numBins, stride, minBin };
  }

  // ---- candidates (ft8/decode.c ft8_sync_score, ftx_find_candidates) ----
  const candBase = (wf, c) => ((c.t * TIME_OSR + c.ts) * FREQ_OSR + c.fs) * wf.numBins + c.f;
  function syncScore(wf, c) {
    const M = wf.mag, base = candBase(wf, c);
    let score = 0, n = 0;
    for (let m = 0; m < NUM_SYNC; m++) {
      for (let k = 0; k < LENGTH_SYNC; k++) {
        const block = SYNC_OFFSET * m + k, abs = c.t + block;
        if (abs < 0) continue;
        if (abs >= wf.blocks) break;
        const p = base + block * wf.stride, sm = COSTAS[k], v = M[p + sm];
        if (sm > 0) { score += v - M[p + sm - 1]; n++; }
        if (sm < 7) { score += v - M[p + sm + 1]; n++; }
        if (k > 0 && abs > 0) { score += v - M[p + sm - wf.stride]; n++; }
        if (k + 1 < LENGTH_SYNC && abs + 1 < wf.blocks) { score += v - M[p + sm + wf.stride]; n++; }
      }
    }
    return n > 0 ? Math.trunc(score / n) : 0;
  }
  function heapDown(h, size) {
    let cur = 0;
    for (;;) {
      const l = 2 * cur + 1, r = l + 1;
      let s = cur;
      if (l < size && h[l].score < h[s].score) s = l;
      if (r < size && h[r].score < h[s].score) s = r;
      if (s === cur) break;
      const t = h[s]; h[s] = h[cur]; h[cur] = t; cur = s;
    }
  }
  function heapUp(h, size) {
    let cur = size - 1;
    while (cur > 0) {
      const p = (cur - 1) >> 1;
      if (!(h[cur].score < h[p].score)) break;
      const t = h[p]; h[p] = h[cur]; h[cur] = t; cur = p;
    }
  }
  function findCandidates(wf) {
    const heap = [];
    let size = 0;
    for (let ts = 0; ts < TIME_OSR; ts++) {
      for (let fs = 0; fs < FREQ_OSR; fs++) {
        for (let t = -10; t < 20; t++) {
          for (let f = 0; f + 7 < wf.numBins; f++) {
            const c = { t, ts, f, fs, score: 0 };
            c.score = syncScore(wf, c);
            if (c.score < MIN_SCORE) continue;
            if (size === MAX_CANDIDATES && c.score > heap[0].score) { size--; heap[0] = heap[size]; heapDown(heap, size); }
            if (size < MAX_CANDIDATES) { heap[size++] = c; heapUp(heap, size); }
          }
        }
      }
    }
    heap.length = size;
    for (let n = size; n > 1; n--) { const t = heap[n - 1]; heap[n - 1] = heap[0]; heap[0] = t; heapDown(heap, n - 1); }
    return heap;                               // strongest sync first
  }

  // ---- bit likelihoods (ft8_extract_likelihood, ftx_normalize_logl) ----
  const dbOf = (v) => v * 0.5 - 120;
  const max4 = (a, b, c, d) => Math.max(Math.max(a, b), Math.max(c, d));
  function likelihoods(wf, c) {
    const M = wf.mag, base = candBase(wf, c), out = new Float64Array(LDPC_N), s = new Float64Array(8);
    for (let k = 0; k < ND; k++) {
      const sym = k + (k < 29 ? 7 : 14), bit = 3 * k, block = c.t + sym;
      if (block < 0 || block >= wf.blocks) { out[bit] = out[bit + 1] = out[bit + 2] = 0; continue; }
      const p = base + sym * wf.stride;
      for (let j = 0; j < 8; j++) s[j] = dbOf(M[p + GRAY[j]]);
      out[bit] = max4(s[4], s[5], s[6], s[7]) - max4(s[0], s[1], s[2], s[3]);
      out[bit + 1] = max4(s[2], s[3], s[6], s[7]) - max4(s[0], s[1], s[4], s[5]);
      out[bit + 2] = max4(s[1], s[3], s[5], s[7]) - max4(s[0], s[2], s[4], s[6]);
    }
    let sum = 0, sum2 = 0;
    for (let i = 0; i < LDPC_N; i++) { sum += out[i]; sum2 += out[i] * out[i]; }
    const variance = (sum2 - (sum * sum) / LDPC_N) / LDPC_N;
    if (!(variance > 0)) return null;
    const k = Math.sqrt(24 / variance);
    for (let i = 0; i < LDPC_N; i++) out[i] *= k;
    return out;
  }

  // ---- LDPC (ft8/ldpc.c bp_decode) ----
  function fastTanh(x) {
    if (x < -4.97) return -1;
    if (x > 4.97) return 1;
    const x2 = x * x;
    return (x * (945 + x2 * (105 + x2))) / (945 + x2 * (420 + x2 * 15));
  }
  function fastAtanh(x) {
    const x2 = x * x;
    return (x * (945 + x2 * (-735 + x2 * 64))) / (945 + x2 * (-1050 + x2 * 225));
  }
  function ldpcErrors(cw) {
    let errors = 0;
    for (let m = 0; m < LDPC_M; m++) {
      let x = 0;
      for (let i = 0; i < LDPC_ROWS[m]; i++) x ^= cw[LDPC_NM[m * 7 + i] - 1];
      if (x) errors++;
    }
    return errors;
  }
  function bpDecode(cw, iters, plain) {
    const tov = new Float64Array(LDPC_N * 3), toc = new Float64Array(LDPC_M * 7);
    let minErrors = LDPC_M;
    for (let it = 0; it < iters; it++) {
      let ones = 0;
      for (let n = 0; n < LDPC_N; n++) {
        plain[n] = cw[n] + tov[3 * n] + tov[3 * n + 1] + tov[3 * n + 2] > 0 ? 1 : 0;
        ones += plain[n];
      }
      if (ones === 0) break;                     // all zeros is not a codeword FT8 sends
      const errors = ldpcErrors(plain);
      if (errors < minErrors) { minErrors = errors; if (errors === 0) break; }
      for (let m = 0; m < LDPC_M; m++) {        // bits to checks
        for (let ni = 0; ni < LDPC_ROWS[m]; ni++) {
          const n = LDPC_NM[m * 7 + ni] - 1;
          let T = cw[n];
          for (let mi = 0; mi < 3; mi++) if (LDPC_MN[n * 3 + mi] - 1 !== m) T += tov[n * 3 + mi];
          toc[m * 7 + ni] = fastTanh(-T / 2);
        }
      }
      for (let n = 0; n < LDPC_N; n++) {        // checks to bits
        for (let mi = 0; mi < 3; mi++) {
          const m = LDPC_MN[n * 3 + mi] - 1;
          let T = 1;
          for (let ni = 0; ni < LDPC_ROWS[m]; ni++) if (LDPC_NM[m * 7 + ni] - 1 !== n) T *= toc[m * 7 + ni];
          tov[n * 3 + mi] = -2 * fastAtanh(T);
        }
      }
    }
    return minErrors;
  }

  // ---- CRC-14 (ft8/crc.c) ----
  function crc14(bytes, bits) {
    let r = 0, idx = 0;
    for (let b = 0; b < bits; b++) {
      if (b % 8 === 0) r ^= bytes[idx++] << 6;
      r = (r & CRC_TOP ? (r << 1) ^ CRC_POLY : r << 1) & 0xFFFF;
    }
    return r & 0x3FFF;
  }

  // ---- text (ft8/text.c) ----
  const T_FULL = 0, T_ANS = 1, T_AN_SP = 2, T_LET_SP = 3, T_AN = 4, T_NUM = 5;  // the character tables
  function charn(c, t) {
    if (t !== T_AN && t !== T_NUM) { if (c === 0) return " "; c -= 1; }
    if (t !== T_LET_SP) { if (c < 10) return String.fromCharCode(48 + c); c -= 10; }
    if (t !== T_NUM) { if (c < 26) return String.fromCharCode(65 + c); c -= 26; }
    if (t === T_FULL) { if (c < 5) return "+-./?"[c]; } else if (t === T_ANS) { if (c === 0) return "/"; }
    return "_";
  }
  function nchar(ch, t) {
    let n = 0;
    if (t !== T_AN && t !== T_NUM) { if (ch === " ") return n; n += 1; }
    if (t !== T_LET_SP) { if (ch >= "0" && ch <= "9") return n + ch.charCodeAt(0) - 48; n += 10; }
    if (t !== T_NUM) { if (ch >= "A" && ch <= "Z") return n + ch.charCodeAt(0) - 65; n += 26; }
    if (t === T_FULL) { const i = "+-./?".indexOf(ch); if (i >= 0) return n + i; } else if (t === T_ANS) { if (ch === "/") return n; }
    return -1;
  }
  function intToDd(value, width, fullSign) {     // int_to_dd: a sign, then `width` digits
    let s = "";
    if (value < 0) { s = "-"; value = -value; } else if (fullSign) s = "+";
    for (let d = Math.pow(10, width - 1); d >= 1; d = Math.floor(d / 10)) {
      const digit = Math.floor(value / d);
      s += String.fromCharCode(48 + digit);
      value -= digit * d;
    }
    return s;
  }

  // ---- the callsign hash table (demo/decode_ft8.c): 256 entries, aged out after 10 slots ----
  const HT_SIZE = 256;
  let table = new Array(HT_SIZE).fill(null);    // { call, hash (22 bits), age }
  const M64 = (1n << 64n) - 1n;
  function hash22(call) {                       // message.c save_callsign
    let n58 = 0n, i = 0;
    for (; i < call.length && i < 11; i++) {
      const j = nchar(call[i], T_ANS);
      if (j < 0) return -1;
      n58 = n58 * 38n + BigInt(j);
    }
    for (; i < 11; i++) n58 *= 38n;
    return Number((((47055833459n * n58) & M64) >> 42n) & 0x3FFFFFn);
  }
  function saveCall(call) {
    const h = hash22(call);
    if (h < 0) return false;
    let idx = ((((h >> 12) & 0x3FF) * 23) % HT_SIZE);
    for (let probe = 0; probe < HT_SIZE; probe++, idx = (idx + 1) % HT_SIZE) {
      const e = table[idx];
      if (!e) { table[idx] = { call: call.slice(0, 11), hash: h, age: 0 }; return true; }
      if (e.hash === h && e.call === call) { e.age = 0; return true; }
    }
    return true;                                // full: not stored (the C would loop forever)
  }
  function lookupCall(bits, hash) {             // bits = 22 | 12 | 10
    const shift = bits === 10 ? 12 : bits === 12 ? 10 : 0;
    let idx = ((((hash >> (12 - shift)) & 0x3FF) * 23) % HT_SIZE);
    for (let probe = 0; probe < HT_SIZE && table[idx]; probe++, idx = (idx + 1) % HT_SIZE) {
      if ((table[idx].hash >> shift) === hash) return "<" + table[idx].call + ">";
    }
    return "<...>";
  }
  function ageTable(maxAge) {
    for (let i = 0; i < HT_SIZE; i++) { const e = table[i]; if (e) { if (e.age > maxAge) table[i] = null; else e.age++; } }
  }

  // ---- messages (ft8/message.c) ----
  const MAX22 = 4194304, NTOKENS = 2063592, MAXGRID4 = 32400;
  function trim(s) { return s.replace(/^ +| +$/g, ""); }
  function unpack28(n28, ip, i3) {
    if (n28 < NTOKENS) {
      if (n28 <= 2) return ["DE", "QRZ", "CQ"][n28];
      if (n28 <= 1002) return "CQ " + intToDd(n28 - 3, 3, false);
      if (n28 <= 532443) {
        let n = n28 - 1003, a = "";
        for (let i = 3; i >= 0; i--) { a = charn(n % 27, T_LET_SP) + a; if (i === 0) break; n = Math.floor(n / 27); }
        return "CQ " + a.replace(/^ +/, "");
      }
      return null;
    }
    n28 -= NTOKENS;
    if (n28 < MAX22) return lookupCall(22, n28);
    let n = n28 - MAX22;
    const c = new Array(6);
    c[5] = charn(n % 27, T_LET_SP); n = Math.floor(n / 27);
    c[4] = charn(n % 27, T_LET_SP); n = Math.floor(n / 27);
    c[3] = charn(n % 27, T_LET_SP); n = Math.floor(n / 27);
    c[2] = charn(n % 10, T_NUM); n = Math.floor(n / 10);
    c[1] = charn(n % 36, T_AN); n = Math.floor(n / 36);
    c[0] = charn(n % 37, T_AN_SP);
    const cs = c.join("");
    let r;
    if (cs.startsWith("3D0") && cs[3] !== " ") r = "3DA0" + trim(cs.slice(3));
    else if (cs[0] === "Q" && /[A-Za-z]/.test(cs[1])) r = "3X" + trim(cs.slice(1));
    else r = trim(cs);
    if (r.length < 3) return null;
    if (ip) { if (i3 === 1) r += "/R"; else if (i3 === 2) r += "/P"; else return null; }
    saveCall(r);
    return r;
  }
  function unpackGrid(igrid4, ir) {
    if (igrid4 <= MAXGRID4) {
      let n = igrid4;
      const d3 = n % 10; n = Math.floor(n / 10);
      const d2 = n % 10; n = Math.floor(n / 10);
      const l1 = n % 18; n = Math.floor(n / 18);
      const l0 = n % 18;
      return (ir ? "R " : "") + String.fromCharCode(65 + l0, 65 + l1, 48 + d2, 48 + d3);
    }
    const irpt = igrid4 - MAXGRID4;
    if (irpt === 1) return "";
    if (irpt === 2) return "RRR";
    if (irpt === 3) return "RR73";
    if (irpt === 4) return "73";
    return (ir ? "R" : "") + intToDd(irpt - 35, 2, true);
  }
  function decodeStd(p) {
    const n29a = (p[0] << 21) | (p[1] << 13) | (p[2] << 5) | (p[3] >> 3);
    const n29b = ((p[3] & 7) << 26) | (p[4] << 18) | (p[5] << 10) | (p[6] << 2) | (p[7] >> 6);
    const ir = (p[7] & 0x20) >> 5, igrid4 = ((p[7] & 0x1F) << 10) | (p[8] << 2) | (p[9] >> 6), i3 = (p[9] >> 3) & 7;
    const a = unpack28(n29a >>> 1, n29a & 1, i3);
    if (a === null) return null;
    const b = unpack28(n29b >>> 1, n29b & 1, i3);
    if (b === null) return null;
    const x = unpackGrid(igrid4, ir);
    return a + " " + b + (x ? " " + x : "");
  }
  function decodeNonstd(p) {
    const n12 = (p[0] << 4) | (p[1] >> 4);
    let n58 = BigInt(p[1] & 0x0F) << 54n;
    n58 |= BigInt(p[2]) << 46n; n58 |= BigInt(p[3]) << 38n; n58 |= BigInt(p[4]) << 30n; n58 |= BigInt(p[5]) << 22n;
    n58 |= BigInt(p[6]) << 14n; n58 |= BigInt(p[7]) << 6n; n58 |= BigInt(p[8] >> 2);
    const iflip = (p[8] >> 1) & 1, nrpt = ((p[8] & 1) << 1) | (p[9] >> 7), icq = (p[9] >> 6) & 1;
    let c11 = "";
    for (let i = 10; i >= 0; i--) { c11 = charn(Number(n58 % 38n), T_ANS) + c11; if (i === 0) break; n58 /= 38n; }
    const plain = trim(c11);
    if (plain.length >= 3) saveCall(plain);
    const hashed = lookupCall(12, n12);
    const c1 = iflip ? plain : hashed, c2 = iflip ? hashed : plain;
    if (icq) return "CQ " + c2;
    const extra = ["", "RRR", "RR73", "73"][nrpt];
    return c1 + " " + c2 + (extra ? " " + extra : "");
  }
  function telemetry(p) {                       // ftx_message_decode_telemetry: 71 bits, right-aligned
    const b = new Uint8Array(9);
    let carry = 0;
    for (let i = 0; i < 9; i++) { b[i] = ((carry << 7) | (p[i] >> 1)) & 0xFF; carry = p[i] & 1; }
    return b;
  }
  function decodeFree(p) {
    const b = telemetry(p), c = new Array(13);
    for (let idx = 12; idx >= 0; idx--) {
      let rem = 0;
      for (let i = 0; i < 9; i++) { rem = (rem << 8) | b[i]; b[i] = Math.floor(rem / 42); rem %= 42; }
      c[idx] = charn(rem, T_FULL);
    }
    return trim(c.join(""));
  }
  function decodeMessage(p) {
    const i3 = (p[9] >> 3) & 7, n3 = ((p[8] << 2) & 4) | ((p[9] >> 6) & 3);
    if (i3 === 1 || i3 === 2) return decodeStd(p);
    if (i3 === 4) return decodeNonstd(p);
    if (i3 === 0 && n3 === 0) return decodeFree(p);
    if (i3 === 0 && n3 === 5) return Array.from(telemetry(p), (v) => v.toString(16).toUpperCase().padStart(2, "0")).join("");
    return null;                                // DXpedition, contest and other types: not decoded here (as ft8_lib)
  }

  // ---- SNR (TeleRig's own): the tones that were sent, against the slot's noise floor ----
  // The noise floor is the 80th percentile of the whole waterfall (most of an FT8 band, most of the
  // time, is noise; the percentile and SNR_OFFSET are fitted to WSJT-X's reports on ft8_lib's test
  // recordings, about 3 dB apart on average); the signal is the mean power of the tone each symbol
  // carried (decoded, so known).
  function noiseFloor(wf) {
    const hist = new Uint32Array(256), n = wf.blocks * wf.stride;
    for (let i = 0; i < n; i++) hist[wf.mag[i]]++;
    let acc = 0, v = 0;
    for (; v < 255; v++) { acc += hist[v]; if (acc >= n * NOISE_PCT) break; }
    return Math.pow(10, dbOf(v) / 10);
  }
  function tonesOf(plain) {                     // the 79 tones the message was sent on
    const t = new Uint8Array(NN);
    for (let s = 0; s < NN; s++) {
      if (s < 7) t[s] = COSTAS[s];
      else if (s >= 36 && s < 43) t[s] = COSTAS[s - 36];
      else if (s >= 72) t[s] = COSTAS[s - 72];
      else { const k = s < 36 ? s - 7 : s - 14; t[s] = GRAY[(plain[3 * k] << 2) | (plain[3 * k + 1] << 1) | plain[3 * k + 2]]; }
    }
    return t;
  }
  function snrOf(wf, c, plain, noise) {
    const M = wf.mag, base = candBase(wf, c), tones = tonesOf(plain);
    let sig = 0, n = 0;
    for (let s = 0; s < NN; s++) {
      const block = c.t + s;
      if (block < 0 || block >= wf.blocks) continue;
      sig += Math.pow(10, dbOf(M[base + s * wf.stride + tones[s]]) / 10);
      n++;
    }
    if (!n || !(noise > 0)) return -24;
    const r = sig / n / noise - 1;
    return Math.max(-24, Math.round(10 * Math.log10(Math.max(r, 1e-3)) + SNR_OFFSET));
  }

  // ---- one slot ----
  function decodeSlot(samples, rate) {
    if (!samples || !samples.length) return [];  // nothing heard: nothing to do
    const wf = waterfall(samples, rate);
    const cands = findCandidates(wf), noise = noiseFloor(wf);
    const seen = new Map(), out = [], plain = new Uint8Array(LDPC_N), a91 = new Uint8Array(12);
    for (const c of cands) {
      if (seen.size >= MAX_DECODED) break;
      const ll = likelihoods(wf, c);
      if (!ll) continue;
      if (bpDecode(ll, LDPC_ITERATIONS, plain) > 0) continue;
      a91.fill(0);
      for (let i = 0; i < LDPC_K; i++) if (plain[i]) a91[i >> 3] |= 0x80 >> (i & 7);
      const crc = ((a91[9] & 7) << 11) | (a91[10] << 3) | (a91[11] >> 5);
      a91[9] &= 0xF8; a91[10] = 0;
      if (crc !== crc14(a91, 82)) continue;
      const payload = a91.slice(0, 10), key = Array.from(payload).join(",");
      if (seen.has(key)) continue;              // the same message found again at another offset
      seen.set(key, true);
      const text = decodeMessage(payload);
      if (text === null) continue;
      // the waterfall frame (b, ts) ends (ts + 1) subblocks into block b, so it is centred on the symbol
      // that started one block earlier: the message starts at (t + ts / 2 - 1) symbols (ft8_lib reports
      // t + ts / 2); WSJT-X's DT counts from 0.5 s into the slot
      const start = (c.t + c.ts / TIME_OSR - 1) * SYMBOL_PERIOD;
      out.push({ snr: snrOf(wf, c, plain, noise), dt: Math.round((start - 0.5) * 10) / 10,
                 freq: Math.round((wf.minBin + c.f + c.fs / FREQ_OSR) / SYMBOL_PERIOD), text });
    }
    ageTable(10);
    return out;
  }

  const api = { decodeSlot, makeFft, reset: () => { table = new Array(HT_SIZE).fill(null); } };
  G.FT8 = api;

  // ---- as a worker ----
  if (typeof G.importScripts === "function" && typeof G.postMessage === "function" && typeof G.document === "undefined") {
    G.onmessage = (e) => {
      const d = (e && e.data) || {};
      try {
        const samples = d.samples instanceof Float32Array ? d.samples : new Float32Array(0);
        G.postMessage({ id: d.id, decodes: decodeSlot(samples, +d.rate) });
      } catch (err) {
        G.postMessage({ id: d.id, error: String((err && err.message) || err) });
      }
    };
  }
})(typeof self !== "undefined" ? self : this);
