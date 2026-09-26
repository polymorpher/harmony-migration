/*
 * Shared random-sample verification core (JavaScript engine).
 *
 * This file mirrors sample_core.py. Claim formulas, schemas, totals, and
 * cross-file rules are NOT written here: both engines interpret rules.json.
 * Only procedural work lives here (streaming CSV, SHA-256, Keccak-256,
 * sampling, identity checks, airdrop Merkle recomputation, evidence
 * comparison). Keep it in step with sample_core.py; the conformance test in
 * toolkit/tests/test_random_sample_verify.py runs both engines on the same
 * bundles and compares every check status.
 *
 * All amounts are BigInt atto-ONE. No floating-point arithmetic is used for
 * amounts; decimal strings are only derived from integers.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.SampleCore = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const PASS = 'PASS';
  const FAIL = 'FAIL';
  const NOT_VERIFIED = 'NOT VERIFIED';
  const WARNING = 'WARNING';
  const EXIT = { PASS: 0, FAIL: 1, INPUT: 2, INCOMPLETE: 3 };
  const MAX_EXAMPLES = 20;
  const MANIFEST_NAME = 'bundle-manifest.json';
  const CHUNK = 4 * 1024 * 1024;

  class BundleError extends Error {}
  class EvalError extends Error {}
  class MissingRow extends EvalError {}

  // -------------------------------------------------------------------------
  // hashing
  // -------------------------------------------------------------------------

  const SHA_K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);

  class Sha256 {
    constructor() {
      this.h = new Uint32Array([
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
      ]);
      this.buffer = new Uint8Array(64);
      this.buffered = 0;
      this.length = 0;
      this.w = new Uint32Array(64);
    }

    update(data) {
      let offset = 0;
      this.length += data.length;
      if (this.buffered) {
        const take = Math.min(64 - this.buffered, data.length);
        this.buffer.set(data.subarray(0, take), this.buffered);
        this.buffered += take;
        offset = take;
        if (this.buffered === 64) {
          this.block(this.buffer, 0);
          this.buffered = 0;
        }
      }
      for (; offset + 64 <= data.length; offset += 64) this.block(data, offset);
      if (offset < data.length) {
        this.buffer.set(data.subarray(offset), 0);
        this.buffered = data.length - offset;
      }
      return this;
    }

    block(data, offset) {
      const w = this.w;
      for (let i = 0; i < 16; i++) {
        const j = offset + 4 * i;
        w[i] = (data[j] << 24) | (data[j + 1] << 16) | (data[j + 2] << 8) | data[j + 3];
      }
      for (let i = 16; i < 64; i++) {
        const a = w[i - 15];
        const b = w[i - 2];
        const s0 = ((a >>> 7) | (a << 25)) ^ ((a >>> 18) | (a << 14)) ^ (a >>> 3);
        const s1 = ((b >>> 17) | (b << 15)) ^ ((b >>> 19) | (b << 13)) ^ (b >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
      }
      let [a, b, c, d, e, f, g, h] = this.h;
      for (let i = 0; i < 64; i++) {
        const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        const ch = (e & f) ^ (~e & g);
        const t1 = (h + S1 + ch + SHA_K[i] + w[i]) | 0;
        const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        const maj = (a & b) ^ (a & c) ^ (b & c);
        const t2 = (S0 + maj) | 0;
        h = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      const H = this.h;
      H[0] += a; H[1] += b; H[2] += c; H[3] += d; H[4] += e; H[5] += f; H[6] += g; H[7] += h;
    }

    digest() {
      const bits = this.length * 8;
      const pad = new Uint8Array(((this.buffered < 56 ? 56 : 120) - this.buffered) + 8);
      pad[0] = 0x80;
      const high = Math.floor(bits / 0x100000000);
      const low = bits >>> 0;
      const n = pad.length;
      pad[n - 8] = high >>> 24; pad[n - 7] = high >>> 16; pad[n - 6] = high >>> 8; pad[n - 5] = high;
      pad[n - 4] = low >>> 24; pad[n - 3] = low >>> 16; pad[n - 2] = low >>> 8; pad[n - 1] = low;
      this.update(pad);
      const out = new Uint8Array(32);
      for (let i = 0; i < 8; i++) {
        out[4 * i] = this.h[i] >>> 24; out[4 * i + 1] = this.h[i] >>> 16;
        out[4 * i + 2] = this.h[i] >>> 8; out[4 * i + 3] = this.h[i];
      }
      return out;
    }

    hexdigest() { return toHex(this.digest()); }
  }

  const KECCAK_RC = [
    [0x00000000, 0x00000001], [0x00000000, 0x00008082], [0x80000000, 0x0000808a], [0x80000000, 0x80008000],
    [0x00000000, 0x0000808b], [0x00000000, 0x80000001], [0x80000000, 0x80008081], [0x80000000, 0x00008009],
    [0x00000000, 0x0000008a], [0x00000000, 0x00000088], [0x00000000, 0x80008009], [0x00000000, 0x8000000a],
    [0x00000000, 0x8000808b], [0x80000000, 0x0000008b], [0x80000000, 0x00008089], [0x80000000, 0x00008003],
    [0x80000000, 0x00008002], [0x80000000, 0x00000080], [0x00000000, 0x0000800a], [0x80000000, 0x8000000a],
    [0x80000000, 0x80008081], [0x80000000, 0x00008080], [0x00000000, 0x80000001], [0x80000000, 0x80008008],
  ];
  const KECCAK_ROT = [0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43, 25, 39, 41, 45, 15, 21, 8, 18, 2, 61, 56, 14];

  function keccakF(hi, lo) {
    const cHi = new Uint32Array(5), cLo = new Uint32Array(5);
    const bHi = new Uint32Array(25), bLo = new Uint32Array(25);
    for (let round = 0; round < 24; round++) {
      for (let x = 0; x < 5; x++) {
        cHi[x] = hi[x] ^ hi[x + 5] ^ hi[x + 10] ^ hi[x + 15] ^ hi[x + 20];
        cLo[x] = lo[x] ^ lo[x + 5] ^ lo[x + 10] ^ lo[x + 15] ^ lo[x + 20];
      }
      for (let x = 0; x < 5; x++) {
        const nHi = cHi[(x + 1) % 5], nLo = cLo[(x + 1) % 5];
        const dHi = cHi[(x + 4) % 5] ^ ((nHi << 1) | (nLo >>> 31));
        const dLo = cLo[(x + 4) % 5] ^ ((nLo << 1) | (nHi >>> 31));
        for (let y = 0; y < 25; y += 5) { hi[x + y] ^= dHi; lo[x + y] ^= dLo; }
      }
      for (let x = 0; x < 5; x++) {
        for (let y = 0; y < 5; y++) {
          const index = x + 5 * y;
          let r = KECCAK_ROT[index];
          let h = hi[index], l = lo[index];
          if (r >= 32) { const t = h; h = l; l = t; r -= 32; }
          const target = y + 5 * ((2 * x + 3 * y) % 5);
          if (r === 0) { bHi[target] = h; bLo[target] = l; } else {
            bHi[target] = (h << r) | (l >>> (32 - r));
            bLo[target] = (l << r) | (h >>> (32 - r));
          }
        }
      }
      for (let y = 0; y < 25; y += 5) {
        for (let x = 0; x < 5; x++) {
          hi[x + y] = bHi[x + y] ^ (~bHi[((x + 1) % 5) + y] & bHi[((x + 2) % 5) + y]);
          lo[x + y] = bLo[x + y] ^ (~bLo[((x + 1) % 5) + y] & bLo[((x + 2) % 5) + y]);
        }
      }
      hi[0] ^= KECCAK_RC[round][0];
      lo[0] ^= KECCAK_RC[round][1];
    }
  }

  function keccak256(data) {
    const rate = 136;
    const padded = new Uint8Array(Math.ceil((data.length + 1) / rate) * rate);
    padded.set(data);
    padded[data.length] ^= 0x01;
    padded[padded.length - 1] ^= 0x80;
    const hi = new Uint32Array(25), lo = new Uint32Array(25);
    for (let offset = 0; offset < padded.length; offset += rate) {
      for (let i = 0; i < rate / 8; i++) {
        const j = offset + 8 * i;
        lo[i] ^= padded[j] | (padded[j + 1] << 8) | (padded[j + 2] << 16) | (padded[j + 3] << 24);
        hi[i] ^= padded[j + 4] | (padded[j + 5] << 8) | (padded[j + 6] << 16) | (padded[j + 7] << 24);
      }
      keccakF(hi, lo);
    }
    const out = new Uint8Array(32);
    for (let i = 0; i < 4; i++) {
      for (let b = 0; b < 4; b++) {
        out[8 * i + b] = (lo[i] >>> (8 * b)) & 0xff;
        out[8 * i + 4 + b] = (hi[i] >>> (8 * b)) & 0xff;
      }
    }
    return out;
  }

  function toHex(bytes) {
    let text = '';
    for (let i = 0; i < bytes.length; i++) text += (bytes[i] < 16 ? '0' : '') + bytes[i].toString(16);
    return text;
  }

  function fromHex(text) {
    const clean = text.startsWith('0x') ? text.slice(2) : text;
    const out = new Uint8Array(clean.length / 2);
    for (let i = 0; i < out.length; i++) out[i] = parseInt(clean.substr(2 * i, 2), 16);
    return out;
  }

  /** Python int(value) for the values the airdrop tool converts. */
  function pyInt(value) {
    if (typeof value === 'bigint') return value;
    if (typeof value === 'boolean') return value ? 1n : 0n;
    if (typeof value === 'number') {
      if (!Number.isFinite(value)) throw new Error('cannot convert float to integer');
      return BigInt(Math.trunc(value));
    }
    if (value instanceof JsonNumber) {
      if (/^-?\d+$/.test(value.text)) return BigInt(value.text);
      return pyInt(Number(value.text));
    }
    if (typeof value === 'string') {
      const text = value.trim();
      if (!/^[+-]?\d+(?:_\d+)*$/.test(text)) throw new Error(`invalid literal for int() with base 10: ${JSON.stringify(value)}`);
      return BigInt(text.replace(/_/g, ''));
    }
    throw new Error(`int() argument must be a string or a number, not ${value === null ? 'NoneType' : typeof value}`);
  }

  const encoder = new TextEncoder();
  function utf8(text) { return encoder.encode(text); }
  function sha256Hex(text) { return new Sha256().update(utf8(text)).hexdigest(); }

  function toChecksum(address) {
    const raw = address.toLowerCase().replace(/^0x/, '');
    const digest = toHex(keccak256(utf8(raw)));
    let out = '0x';
    for (let i = 0; i < raw.length; i++) out += parseInt(digest[i], 16) >= 8 ? raw[i].toUpperCase() : raw[i];
    return out;
  }

  const BECH32_CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l';

  function bech32Polymod(values) {
    const generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3];
    let chk = 1;
    for (const value of values) {
      const top = chk >>> 25;
      chk = (((chk & 0x1ffffff) << 5) ^ value) >>> 0;
      for (let i = 0; i < 5; i++) if ((top >>> i) & 1) chk = (chk ^ generator[i]) >>> 0;
    }
    return chk;
  }

  /** Mirrors contract_review_lib.hex_to_bech32 (hrp "one"). */
  function hexToBech32(address, hrp) {
    const prefix = hrp || 'one';
    if (typeof address !== 'string' || !/^(0x)?[0-9a-fA-F]{40}$/.test(address)) throw new Error('invalid hex address');
    const raw = fromHex(address.toLowerCase().replace(/^0x/, ''));
    let acc = 0;
    let bits = 0;
    const values = [];
    for (const byte of raw) {
      acc = ((acc << 8) | byte) & 0xffff;
      bits += 8;
      while (bits >= 5) { bits -= 5; values.push((acc >>> bits) & 31); }
    }
    if (bits) values.push((acc << (5 - bits)) & 31);
    const expanded = [...prefix].map((c) => c.charCodeAt(0) >> 5).concat([0], [...prefix].map((c) => c.charCodeAt(0) & 31));
    const polymod = (bech32Polymod(expanded.concat(values, [0, 0, 0, 0, 0, 0])) ^ 1) >>> 0;
    const checksum = [0, 1, 2, 3, 4, 5].map((i) => (polymod >>> (5 * (5 - i))) & 31);
    return prefix + '1' + values.concat(checksum).map((v) => BECH32_CHARSET[v]).join('');
  }

  /** Problem text when the one1 address is not the bech32 form of the hex address. */
  function bech32Problem(one1, eth, context) {
    let expected;
    try { expected = hexToBech32(eth); } catch (error) { return `${context}: invalid hex address ${JSON.stringify(eth)}`; }
    return one1 === expected ? null : `${context}: ${one1} is not the one1 form of ${eth} (${expected})`;
  }

  /** Mirrors contract_review_lib.require_address_secure_key. */
  function requireAddressSecureKey(address, secureKey, context) {
    if (typeof address !== 'string' || !/^0x[0-9a-fA-F]{40}$/.test(address)) {
      throw new Error(`${context}: invalid address`);
    }
    const expected = '0x' + toHex(keccak256(fromHex(address.toLowerCase())));
    if (String(secureKey).toLowerCase() !== expected) throw new Error(`${context}: address does not match secure key`);
  }

  // -------------------------------------------------------------------------
  // exact decimal rendering (mirrors verify-wone-allocation.fixed / usd_value)
  // -------------------------------------------------------------------------

  const ATTO = 10n ** 18n;

  function fixed18(value) {
    const whole = value / ATTO;
    const fraction = value % ATTO;
    return `${whole}.${fraction.toString().padStart(18, '0')}`;
  }

  function usd(amount, price) {
    if (typeof price !== 'string' || !/^(?:0|[1-9][0-9]*)\.[0-9]+$/.test(price)) {
      throw new EvalError(`usd: invalid valuation price ${JSON.stringify(price)}`);
    }
    const [whole, fraction] = price.split('.');
    const numerator = BigInt(whole + fraction);
    const scale = 18 + fraction.length;
    const denominator = 10n ** BigInt(scale);
    const product = amount * numerator;
    return `${product / denominator}.${(product % denominator).toString().padStart(scale, '0')}`;
  }

  function displayOne(value) {
    if (typeof value !== 'bigint') return String(value);
    return value < 0n ? '-' + fixed18(-value) : fixed18(value);
  }

  function utcToUnix(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$/.exec(value);
    if (!match) throw new Error(`invalid UTC time ${value}`);
    return Date.UTC(+match[1], +match[2] - 1, +match[3], +match[4], +match[5], +match[6]) / 1000;
  }

  // -------------------------------------------------------------------------
  // strict JSON (duplicate keys and non-finite numbers are rejected)
  // -------------------------------------------------------------------------

  /** A JSON number that is not a safe integer (fraction, exponent, or > 2^53). */
  class JsonNumber {
    constructor(text) { this.text = text; }
    toString() { return this.text; }
    toJSON() { return this.text; }
  }

  function parseJsonStrict(text, label) {
    let i = 0;
    const fail = (message) => { throw new Error(`${label}: invalid JSON: ${message} at offset ${i}`); };
    const ws = () => { while (i < text.length && ' \t\n\r'.includes(text[i])) i++; };
    function value() {
      ws();
      const c = text[i];
      if (c === '{') return object();
      if (c === '[') return array();
      if (c === '"') return string();
      if (c === 't' && text.startsWith('true', i)) { i += 4; return true; }
      if (c === 'f' && text.startsWith('false', i)) { i += 5; return false; }
      if (c === 'n' && text.startsWith('null', i)) { i += 4; return null; }
      return number();
    }
    function object() {
      i++;
      const out = {};
      const seen = new Set();
      ws();
      if (text[i] === '}') { i++; return out; }
      for (;;) {
        ws();
        if (text[i] !== '"') fail('expected a string key');
        const key = string();
        if (seen.has(key)) throw new Error(`${label}: duplicate JSON key ${JSON.stringify(key)}`);
        seen.add(key);
        ws();
        if (text[i] !== ':') fail('expected ":"');
        i++;
        Object.defineProperty(out, key, { value: value(), enumerable: true, writable: true, configurable: true });
        ws();
        if (text[i] === ',') { i++; continue; }
        if (text[i] === '}') { i++; return out; }
        fail('expected "," or "}"');
      }
    }
    function array() {
      i++;
      const out = [];
      ws();
      if (text[i] === ']') { i++; return out; }
      for (;;) {
        out.push(value());
        ws();
        if (text[i] === ',') { i++; continue; }
        if (text[i] === ']') { i++; return out; }
        fail('expected "," or "]"');
      }
    }
    function string() {
      const start = i;
      i++;
      for (;;) {
        if (i >= text.length) fail('unterminated string');
        const c = text[i];
        if (c === '"') { i++; break; }
        if (c === '\\') i += 2; else if (c < ' ') fail('control character in string'); else i++;
      }
      return JSON.parse(text.slice(start, i));
    }
    function number() {
      const match = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(text.slice(i, i + 400));
      if (!match) fail('unexpected character');
      i += match[0].length;
      const parsed = Number(match[0]);
      if (/^-?\d+$/.test(match[0]) && Number.isSafeInteger(parsed)) return parsed;
      return new JsonNumber(match[0]);
    }
    const result = value();
    ws();
    if (i !== text.length) fail('trailing data');
    return result;
  }

  // -------------------------------------------------------------------------
  // streaming CSV (RFC 4180, blank lines skipped like Python's csv module)
  // -------------------------------------------------------------------------

  /*
   * Mirrors Python's csv.reader. strict=true matches csv.reader(strict=True)
   * plus NUL rejection (used for every bundle CSV); strict=false matches the
   * default reader used by airdrop/tools. keepBlank yields [] for blank lines
   * (Python does; DictReader-style consumers skip them).
   */
  class CsvParser {
    constructor(options) {
      const o = options || {};
      this.strict = o.strict !== false;
      this.keepBlank = !!o.keepBlank;
      this.carry = '';
      this.skipLF = false;
    }

    feed(text, final, out) {
      const data = this.carry + text;
      this.carry = '';
      let i = 0;
      const n = data.length;
      if (this.skipLF && n) { if (data.charCodeAt(0) === 10) i = 1; this.skipLF = false; }
      while (i < n) {
        const start = i;
        const fields = [];
        let field = '';
        let quoted = false;
        let started = false;
        let closed = false;
        let complete = false;
        while (i < n) {
          const c = data.charCodeAt(i);
          if (c === 0 && this.strict) throw new Error('line contains NUL');
          if (quoted) {
            const j = data.indexOf('"', i);
            if (j === -1) {
              if (this.strict && data.indexOf('\0', i) !== -1) throw new Error('line contains NUL');
              field += data.slice(i);
              i = n;
              break;
            }
            if (this.strict && data.slice(i, j).indexOf('\0') !== -1) throw new Error('line contains NUL');
            field += data.slice(i, j);
            if (j + 1 < n) {
              if (data.charCodeAt(j + 1) === 34) { field += '"'; i = j + 2; } else { quoted = false; closed = true; i = j + 1; }
            } else if (final) { quoted = false; closed = true; i = j + 1; } else { i = n; break; }
            continue;
          }
          if (c === 44) { fields.push(field); field = ''; started = false; closed = false; i++; continue; }
          if (c === 10 || c === 13) {
            if (c === 13) {
              if (i + 1 < n) { if (data.charCodeAt(i + 1) === 10) i++; } else if (!final) this.skipLF = true;
            }
            i++;
            complete = true;
            break;
          }
          if (closed && this.strict) throw new Error("',' expected after '\"'");
          if (c === 34 && field === '' && !started) { quoted = true; started = true; i++; continue; }
          let j = i;
          while (j < n) { const d = data.charCodeAt(j); if (d === 44 || d === 10 || d === 13 || (d === 0 && this.strict)) break; j++; }
          field += data.slice(i, j);
          started = true;
          i = j;
        }
        if (!complete) {
          if (!final) { this.carry = data.slice(start); this.skipLF = false; return; }
          if (quoted && this.strict) throw new Error('unexpected end of data');
          if (fields.length || field !== '' || started) { fields.push(field); out.push(fields); }
          return;
        }
        if (fields.length === 0 && field === '' && !started) { if (this.keepBlank) out.push([]); continue; }
        fields.push(field);
        out.push(fields);
      }
    }
  }

  async function* csvRecords(fs, path, options) {
    const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });
    const parser = new CsvParser(options);
    // Like Python's csv.reader, deliver every record parsed before an error, then raise it.
    const run = function* (text, final) {
      const out = [];
      let failure = null;
      try { parser.feed(text, final, out); } catch (error) { failure = error; }
      yield* out;
      if (failure) throw failure;
    };
    for await (const chunk of fs.readChunks(path)) yield* run(decoder.decode(chunk, { stream: true }), false);
    yield* run(decoder.decode(), true);
  }

  // -------------------------------------------------------------------------
  // expression engine (mirrors sample_core.Engine)
  // -------------------------------------------------------------------------

  const SYMBOL = { le: '<=', lt: '<', ge: '>=', gt: '>', ne: '!=' };

  function kindOf(value) {
    if (Array.isArray(value)) return 'list';
    if (typeof value === 'bigint') return 'int';
    if (typeof value === 'boolean') return 'bool';
    if (typeof value === 'string') return 'str';
    return typeof value;
  }

  function compare(op, left, right) {
    if (kindOf(left) !== kindOf(right)) throw new EvalError(`cannot compare ${kindOf(left)} with ${kindOf(right)}`);
    if (Array.isArray(left)) {
      const same = left.length === right.length && left.every((item, i) => item === right[i]);
      if (op === 'eq') return same;
      if (op === 'ne') return !same;
      throw new EvalError('cannot order lists');
    }
    if (op === 'eq') return left === right;
    if (op === 'ne') return left !== right;
    if (typeof left === 'boolean') throw new EvalError('cannot order booleans');
    if (op === 'lt') return left < right;
    if (op === 'le') return left <= right;
    if (op === 'gt') return left > right;
    return left >= right;
  }

  function describe(value) {
    if (typeof value === 'bigint') return `${value} atto (${displayOne(value)} ONE)`;
    if (typeof value === 'boolean') return value ? 'yes' : 'no';
    return JSON.stringify(value);
  }

  class Engine {
    constructor(rules, snapshot) {
      this.rules = rules;
      this.snapshot = snapshot;
      this.types = {};
      for (const [name, pattern] of Object.entries(rules.types)) this.types[name] = pattern ? new RegExp(pattern) : null;
      this.constants = {};
      for (const [name, literal] of Object.entries(rules.constants)) this.constants[name] = Engine.literal(literal);
      for (const [name, path] of Object.entries(rules.snapshot_constants)) {
        let value = snapshot;
        for (const part of path) value = value[part];
        this.constants[name] = typeof value === 'number' ? BigInt(value) : value;
      }
      this.fieldTypes = {};
      for (const [role, spec] of Object.entries(rules.roles)) this.fieldTypes[role] = spec.fields;
      this.varExprs = rules.row_vars;
      this.rowChecks = rules.row_checks.concat(this.displayChecks());
    }

    static literal(value) {
      if ('int' in value) return BigInt(value.int);
      if ('list' in value) return value.list.slice();
      return value.str;
    }

    displayChecks() {
      const checks = [];
      for (const [role, spec] of Object.entries(this.rules.roles)) {
        const pairs = spec.decimal_pairs;
        if (!pairs || !pairs.length) continue;
        checks.push({
          id: `${role}.decimal_display`,
          group: 'display',
          needs_rows: [role],
          title: 'Decimal ONE values are derived exactly from the atto integers',
          assert: { and: pairs.map((name) => ({ eq: [{ f: `${role}.${name}_one` }, { fixed18: { f: `${role}.${name}_atto` } }] })) },
          explain: 'Every *_one column is a display rendering; it must equal the atto integer divided by 10^18 with exactly 18 decimals.',
        });
      }
      return checks;
    }

    typed(role, field, raw) {
      if (field === '_category') return raw;
      if (raw === undefined || raw === null) throw new EvalError(`${role}.${field} is missing`);
      const kind = (this.fieldTypes[role] || {})[field];
      if (kind === undefined) throw new EvalError(`${role}.${field} is not a known field`);
      if (kind === 'uint') {
        if (!this.types.uint.test(raw)) throw new EvalError(`${role}.${field} is not a canonical unsigned integer: ${JSON.stringify(raw)}`);
        return BigInt(raw);
      }
      return raw;
    }

    static intValue(value) {
      if (typeof value !== 'bigint') throw new EvalError(`expected an integer, found ${JSON.stringify(String(value))}`);
      return value;
    }

    static boolValue(value) {
      if (typeof value !== 'boolean') throw new EvalError(`expected a boolean, found ${JSON.stringify(String(value))}`);
      return value;
    }

    eval(expr, ctx, row, rowRole) {
      if (expr === true || expr === false) return expr;
      if (expr === null || typeof expr !== 'object' || Object.keys(expr).length !== 1) {
        throw new EvalError(`invalid expression ${JSON.stringify(expr)}`);
      }
      const op = Object.keys(expr)[0];
      const arg = expr[op];
      const ev = (item) => this.eval(item, ctx, row, rowRole);
      switch (op) {
        case 'int': return BigInt(arg);
        case 'str': return arg;
        case 'const':
          if (!(arg in this.constants)) throw new EvalError(`unknown constant ${arg}`);
          return this.constants[arg];
        case 'f': {
          const dot = arg.indexOf('.');
          const role = arg.slice(0, dot);
          const field = arg.slice(dot + 1);
          const rows = ctx.roleRows[role] || [];
          if (!rows.length) throw new MissingRow(`no ${role} row for this identifier`);
          if (rows.length > 1) throw new EvalError(`${rows.length} ${role} rows for this identifier`);
          return this.typed(role, field, rows[0][field]);
        }
        case 'row':
          if (!row) throw new EvalError('row reference outside an aggregate');
          return this.typed(rowRole, arg, row[arg]);
        case 'var': return ctx.variable(arg, this);
        case 'total':
          if (!(arg in ctx.totals)) throw new EvalError(`total ${arg} is unavailable`);
          return ctx.totals[arg];
        case 'exists': return (ctx.roleRows[arg] || []).length > 0;
        case 'add': return arg.reduce((sum, item) => sum + Engine.intValue(ev(item)), 0n);
        case 'max': case 'min': {
          const values = arg.map((item) => Engine.intValue(ev(item)));
          return values.reduce((best, value) => (op === 'max' ? (value > best ? value : best) : (value < best ? value : best)));
        }
        case 'sub': return Engine.intValue(ev(arg[0])) - Engine.intValue(ev(arg[1]));
        case 'eq': case 'ne': case 'lt': case 'le': case 'gt': case 'ge': {
          const left = ev(arg[0]);
          const right = ev(arg[1]);
          return compare(op, left, right);
        }
        case 'and':
          for (const item of arg) if (!Engine.boolValue(ev(item))) return false;
          return true;
        case 'or':
          for (const item of arg) if (Engine.boolValue(ev(item))) return true;
          return false;
        case 'not': return !Engine.boolValue(ev(arg));
        case 'if': return Engine.boolValue(ev(arg[0])) ? ev(arg[1]) : ev(arg[2]);
        case 'in': {
          const value = ev(arg[0]);
          const options = Array.isArray(arg[1]) ? arg[1] : ev(arg[1]);
          if (!Array.isArray(options)) throw new EvalError('in() needs a list');
          return options.includes(value);
        }
        case 'list': return arg.slice();
        case 'minus': {
          const left = ev(arg[0]);
          const right = ev(arg[1]);
          if (!Array.isArray(left) || !Array.isArray(right)) throw new EvalError('minus() needs two lists');
          return left.filter((item) => !right.includes(item));
        }
        case 'all_match': {
          const items = ev(arg[0]);
          const pattern = ev(arg[1]);
          if (!Array.isArray(items) || typeof pattern !== 'string') throw new EvalError('all_match() needs a list and a pattern');
          const regex = new RegExp(pattern);
          return items.every((item) => typeof item === 'string' && regex.test(item));
        }
        case 'or_zero': {
          const value = ev(arg);
          if (value === '') return 0n;
          if (typeof value === 'bigint') return value;
          if (typeof value === 'string' && this.types.uint.test(value)) return BigInt(value);
          throw new EvalError(`expected an integer or blank, found ${JSON.stringify(String(value))}`);
        }
        case 'split': {
          const value = ev(arg);
          if (typeof value !== 'string') throw new EvalError('split() needs text');
          return value.split(';').filter((part) => part);
        }
        case 'round_one': return (Engine.intValue(ev(arg)) + 5n * 10n ** 17n) / 10n ** 18n;
        case 'ymd': {
          const value = ev(arg);
          if (typeof value !== 'string') throw new EvalError('ymd() needs text');
          return value.slice(0, 10).replace(/-/g, '');
        }
        case 'lower': {
          const value = ev(arg);
          if (typeof value !== 'string') throw new EvalError('lower() needs text');
          return value.toLowerCase();
        }
        case 'fixed18': return fixed18(Engine.intValue(ev(arg)));
        case 'usd': return usd(Engine.intValue(ev(arg[0])), ev(arg[1]));
        case 'sum': case 'count': case 'all': {
          const role = arg.role;
          const selected = [];
          for (const item of ctx.roleRows[role] || []) {
            if (arg.where === undefined || Engine.boolValue(this.eval(arg.where, ctx, item, role))) selected.push(item);
          }
          if (op === 'count') return BigInt(selected.length);
          if (op === 'sum') return selected.reduce((sum, item) => sum + Engine.intValue(this.typed(role, arg.field, item[arg.field])), 0n);
          for (const item of selected) if (!Engine.boolValue(this.eval(arg.assert, ctx, item, role))) return false;
          return true;
        }
        default: throw new EvalError(`unknown operator ${op}`);
      }
    }

    explainFailure(expr, ctx, row, rowRole) {
      try {
        if (expr && typeof expr === 'object' && Object.keys(expr).length === 1) {
          const op = Object.keys(expr)[0];
          const arg = expr[op];
          if (op === 'eq') {
            return `found ${describe(this.eval(arg[0], ctx, row, rowRole))}, expected ${describe(this.eval(arg[1], ctx, row, rowRole))}`;
          }
          if (op in SYMBOL) {
            return `${describe(this.eval(arg[0], ctx, row, rowRole))} ${SYMBOL[op]} ${describe(this.eval(arg[1], ctx, row, rowRole))} is false`;
          }
          if (op === 'and') {
            for (const item of arg) if (!this.eval(item, ctx, row, rowRole)) return this.explainFailure(item, ctx, row, rowRole);
          }
          if (op === 'if') {
            const condition = this.eval(arg[0], ctx, row, rowRole);
            return this.explainFailure(condition ? arg[1] : arg[2], ctx, row, rowRole);
          }
          if (op === 'all') {
            const items = ctx.roleRows[arg.role] || [];
            for (let index = 0; index < items.length; index++) {
              const item = items[index];
              if (arg.where !== undefined && !this.eval(arg.where, ctx, item, arg.role)) continue;
              if (!this.eval(arg.assert, ctx, item, arg.role)) {
                return `${arg.role} row ${item._line || index}: ${this.explainFailure(arg.assert, ctx, item, arg.role)}`;
              }
            }
          }
        }
      } catch (error) {
        if (error instanceof EvalError) return error.message;
        throw error;
      }
      return 'the condition is false';
    }
  }

  class RowContext {
    constructor(roleRows, totals) { this.roleRows = roleRows; this.totals = totals || {}; this.vars = {}; }
    variable(name, engine) {
      if (!(name in this.vars)) {
        if (!(name in engine.varExprs)) throw new EvalError(`unknown variable ${name}`);
        this.vars[name] = engine.eval(engine.varExprs[name], this);
      }
      return this.vars[name];
    }
  }

  function tierStatus(checks, scope) {
    const relevant = checks.filter((check) => check.scope === scope);
    if (relevant.some((check) => check.status === FAIL)) return FAIL;
    if (relevant.some((check) => check.status === NOT_VERIFIED && check.required)) return NOT_VERIFIED;
    if (!relevant.length) return NOT_VERIFIED;
    return PASS;
  }

  // -------------------------------------------------------------------------
  // sampling (mirrors sample_core.Sampler)
  // -------------------------------------------------------------------------

  function sampleScore(rules, seed, identifier) {
    return BigInt('0x' + sha256Hex(`${rules.sample_algorithm.domain}|${seed}|${identifier}`));
  }

  function before(a, b) { return a.score < b.score || (a.score === b.score && a.id < b.id); }

  class Sampler {
    constructor(rules, seed, size) {
      this.rules = rules; this.seed = seed; this.size = size;
      this.kept = []; // ascending (score, id)
      this.members = new Set();
      this.population = 0;
    }

    offer(identifier, payload) {
      this.population += 1;
      if (this.size <= 0 || this.members.has(identifier)) return;
      const entry = { score: sampleScore(this.rules, this.seed, identifier), id: identifier, payload };
      if (this.kept.length >= this.size && !before(entry, this.kept[this.kept.length - 1])) return;
      let lo = 0, hi = this.kept.length;
      while (lo < hi) { const mid = (lo + hi) >> 1; if (before(this.kept[mid], entry)) lo = mid + 1; else hi = mid; }
      this.kept.splice(lo, 0, entry);
      this.members.add(identifier);
      if (this.kept.length > this.size) this.members.delete(this.kept.pop().id);
    }

    result() { return this.kept.map((entry) => [entry.id, entry.payload, entry.score]); }
  }

  function drawSample(rules, seed, identifiers, size) {
    const sampler = new Sampler(rules, seed, size);
    for (const identifier of identifiers) sampler.offer(identifier.toLowerCase(), null);
    return sampler.result().map((item) => item[0]);
  }

  function generateSeed() {
    const bytes = new Uint8Array(16);
    const source = (typeof crypto !== 'undefined' && crypto.getRandomValues) ? crypto : null;
    if (!source) throw new Error('no secure random source; enter a seed');
    source.getRandomValues(bytes);
    return toHex(bytes);
  }

  // -------------------------------------------------------------------------
  // airdrop run (mirrors airdrop/tools/common.py and verify.py)
  // -------------------------------------------------------------------------

  function word(value) {
    if (value < 0n || value >= (1n << 256n)) throw new Error('value does not fit in 256 bits');
    return fromHex(value.toString(16).padStart(64, '0'));
  }

  function concat(parts) {
    const total = parts.reduce((sum, part) => sum + part.length, 0);
    const out = new Uint8Array(total);
    let offset = 0;
    for (const part of parts) { out.set(part, offset); offset += part.length; }
    return out;
  }

  function batchLeaf(index, recipients, amounts) {
    const n = BigInt(recipients.length);
    const parts = [word(BigInt(index)), word(96n), word(96n + 32n + 32n * n), word(n)];
    for (const address of recipients) parts.push(word(BigInt('0x' + String(address).replace(/^0x/i, ''))));
    parts.push(word(n));
    for (const amount of amounts) parts.push(word(amount));
    return keccak256(keccak256(concat(parts)));
  }

  function lessBytes(a, b) {
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] < b[i];
    return false;
  }

  function hashPair(a, b) { return lessBytes(a, b) ? keccak256(concat([a, b])) : keccak256(concat([b, a])); }

  function treeLevels(leaves) {
    if (!leaves.length) throw new Error('no leaves');
    const levels = [leaves.slice()];
    while (levels[levels.length - 1].length > 1) {
      const prev = levels[levels.length - 1];
      const next = [];
      for (let i = 0; i < prev.length; i += 2) next.push(i + 1 < prev.length ? hashPair(prev[i], prev[i + 1]) : prev[i]);
      levels.push(next);
    }
    return levels;
  }

  function treeProof(levels, index) {
    const proof = [];
    let idx = index;
    for (const level of levels.slice(0, -1)) {
      const sibling = idx ^ 1;
      if (sibling < level.length) proof.push(level[sibling]);
      idx >>= 1;
    }
    return proof;
  }

  /** Python == between a JSON value and an integer index (0 == 0.0 == False). */
  function pyEq(value, index) {
    if (typeof value === 'number' || typeof value === 'boolean') return Number(value) === index;
    if (value instanceof JsonNumber) return Number(value.text) === index;
    return false;
  }

  /** bytes.fromhex(text.removeprefix("0x")): odd length or non-hex raises. */
  function unhexStrict(text) {
    if (typeof text !== 'string') throw new Error("AttributeError: proof entries must be strings");
    const clean = text.startsWith('0x') ? text.slice(2) : text;
    if (clean.length % 2 || !/^[0-9a-fA-F]*$/.test(clean)) throw new Error(`ValueError: non-hexadecimal number found in fromhex() arg`);
    return fromHex(clean);
  }

  function sameBytes(a, b) { return a.length === b.length && a.every((value, i) => value === b[i]); }

  // -------------------------------------------------------------------------
  // verifier (mirrors sample_core.Verifier)
  // -------------------------------------------------------------------------

  const CLAIM_COMPONENTS = [
    'liquid_shard0_atto', 'liquid_shard1_atto', 'active_staked_or_delegated_atto', 'pending_undelegation_atto',
    'unclaimed_staking_reward_atto', 'pending_cross_shard_atto', 'wone_balance_atto',
  ];
  const ACCOUNT_EVIDENCE_FIELDS = [
    'balance_atto', 'active_staked_or_delegated_atto', 'pending_undelegation_atto', 'unclaimed_staking_reward_atto',
    'pending_cross_shard_atto', 'wone_balance_atto',
  ];
  function accountComponents(shard) {
    return [
      ['balance_atto', `liquid_shard${shard}_atto`],
      ['active_staked_or_delegated_atto', 'active_staked_or_delegated_atto'],
      ['pending_undelegation_atto', 'pending_undelegation_atto'],
      ['unclaimed_staking_reward_atto', 'unclaimed_staking_reward_atto'],
      ['pending_cross_shard_atto', 'pending_cross_shard_atto'],
      ['wone_balance_atto', 'wone_balance_atto'],
    ];
  }

  const SNAPSHOT_SUMMARY_FILES = {
    full_snapshot: 'cutoff_snapshot',
    snapshot_breakdown: 'snapshot_breakdown',
    snapshot: 'snapshot_public',
    snapshot_small: 'snapshot_small',
  };

  const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
  const jsonInt = (value) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
  const normalizedPath = (text) => typeof text === 'string' && text.length > 0 && !text.includes('\\')
    && text.split('/').every((part) => part !== '' && part !== '.' && part !== '..');

  function validateSeed(seed) {
    if (typeof seed !== 'string' || !seed) throw new BundleError('the seed must be non-empty text');
    if (seed !== seed.trim()) throw new BundleError('the seed must not start or end with whitespace');
    return seed;
  }
  const repr = (value) => (value === undefined ? 'None' : JSON.stringify(value));

  class Verifier {
    constructor(fs, options) {
      const o = options || {};
      this.fs = fs;
      this.rules = o.rules;
      this.rulesSha256 = o.rulesSha256 || '';
      this.snapshot = o.snapshot;
      this.engine = new Engine(this.rules, this.snapshot);
      this.sampleSize = o.sampleSize === undefined ? null : o.sampleSize;
      this.seedGenerated = o.seed === undefined || o.seed === null;
      this.seed = this.seedGenerated ? generateSeed() : validateSeed(o.seed);
      this.sampleBy = o.sampleBy || 'secure_key';
      if (!['secure_key', 'address'].includes(this.sampleBy)) throw new BundleError('sample_by must be secure_key or address');
      this.verifyAllMetadata = !!o.verifyAllMetadata;
      this.expectedManifestSha256 = o.expectedManifestSha256 || '';
      this.requireChainTruth = !!o.requireChainTruth;
      this.showRow = o.showRow ? o.showRow.toLowerCase() : null;
      this.progress = o.progress || (() => {});
      this.checks = [];
      this.roles = {};
      for (const role of Object.keys(this.rules.roles)) this.roles[role] = { files: [], present: false, errors: 0, examples: [] };
      this.jsonDocs = {};
      this.fileHashes = {};
      this.fileSizes = {};
      this.totals = {};
      this.tainted = new Set();
      this.identityState = {};
      this.utf8Errors = {};
      this.sources = {};
    }

    add(scope, id, subject, status, title, message, explain, required) {
      this.checks.push({
        scope, id, subject, status, required: required === undefined ? true : !!required,
        title, message: message || '', explain: explain || '',
      });
    }
    bundle(id, subject, status, title, message, explain, required) { this.add('bundle', id, subject, status, title, message, explain, required); }
    chain(id, subject, status, title, message, explain, required) { this.add('chain', id, subject, status, title, message, explain, required); }

    typeOk(kind, value) {
      if (typeof value !== 'string') return false;
      if (isObject(kind)) return kind.enum.includes(value);
      const pattern = this.engine.types[kind];
      return pattern === null || pattern.test(value);
    }

    // -- manifest -------------------------------------------------------------

    async readManifest() {
      const names = await this.fs.list();
      if (!names.length) throw new BundleError('bundle directory is empty or not found');
      if (!names.includes(MANIFEST_NAME)) throw new BundleError(`${MANIFEST_NAME} not found; is this an evidence bundle?`);
      const bytes = await this.fs.readBytes(MANIFEST_NAME);
      this.manifestSha256 = new Sha256().update(bytes).hexdigest();
      let manifest;
      try {
        manifest = parseJsonStrict(new TextDecoder('utf-8', { fatal: true }).decode(bytes), MANIFEST_NAME);
      } catch (error) {
        throw new BundleError(error.message);
      }
      if (!isObject(manifest)) throw new BundleError(`${MANIFEST_NAME}: top-level value must be an object`);
      if (!Array.isArray(manifest.files)) throw new BundleError(`${MANIFEST_NAME}: files must be a list`);
      this.manifest = manifest;
      this.allFiles = names;
    }

    checkManifest() {
      const m = this.manifest;
      const rules = this.rules;
      let title = 'Bundle manifest has the expected format';
      if (m.format === rules.bundle_format) this.bundle('manifest.format', MANIFEST_NAME, PASS, title, m.format);
      else this.bundle('manifest.format', MANIFEST_NAME, FAIL, title, `found ${repr(m.format)}, expected ${repr(rules.bundle_format)}`, 'The manifest is not a version this verifier understands.');
      const problems = [];
      if (typeof m.bundle_id !== 'string' || !m.bundle_id) problems.push('bundle_id must be a non-empty string');
      if (typeof m.created_utc !== 'string' || !this.typeOk('utc_or_empty', m.created_utc) || !m.created_utc) problems.push('created_utc must be YYYY-MM-DDTHH:MM:SSZ');
      if (!Array.isArray(m.sources)) problems.push('sources must be a list');
      const declared = m.declared_totals;
      if (!isObject(declared) || !Object.values(declared).every((value) => typeof value === 'string' && this.typeOk('uint', value))) {
        problems.push('declared_totals must map names to canonical unsigned integer strings');
      }
      const scope = m.airdrop_scope === undefined ? 'partial' : m.airdrop_scope;
      if (!['complete', 'partial'].includes(scope)) problems.push('airdrop_scope must be complete or partial');
      this.bundle('manifest.schema', MANIFEST_NAME, problems.length ? FAIL : PASS, 'Bundle manifest has every required field',
        problems.join('; '), problems.length ? 'A malformed manifest cannot identify the frozen evidence.' : '');
      title = 'Bundle manifest hash matches the published value';
      if (this.expectedManifestSha256) {
        const expected = this.expectedManifestSha256.toLowerCase().replace(/^0x/, '');
        if (expected === this.manifestSha256) this.bundle('manifest.pinned_sha256', MANIFEST_NAME, PASS, title, this.manifestSha256);
        else this.bundle('manifest.pinned_sha256', MANIFEST_NAME, FAIL, title, `found ${this.manifestSha256}, expected ${expected}`, 'This is not the bundle whose hash was published; its contents may differ.');
      } else {
        this.bundle('manifest.pinned_sha256', MANIFEST_NAME, NOT_VERIFIED, title,
          `manifest SHA-256 is ${this.manifestSha256}; no expected value was supplied`,
          'Compare this hash with the published release value, or enter it before running.', false);
      }
    }

    checkCutoff() {
      const m = this.manifest;
      const pinned = this.snapshot.cutoff;
      const c = this.engine.constants;
      const chainIds = m.chain_ids;
      let ok = m.network === c.NETWORK && isObject(chainIds)
        && jsonInt(chainIds.shard0) && BigInt(chainIds.shard0) === c.CHAIN_ID_SHARD0
        && jsonInt(chainIds.shard1) && BigInt(chainIds.shard1) === c.CHAIN_ID_SHARD1;
      this.bundle('cutoff.chain_id', 'chain', ok ? PASS : FAIL, 'Network and chain IDs are Harmony mainnet',
        `network=${repr(m.network)} chain_ids=${repr(chainIds)}`,
        ok ? '' : `Expected network ${c.NETWORK} with shard chain IDs ${c.CHAIN_ID_SHARD0} and ${c.CHAIN_ID_SHARD1}.`);
      const cutoff = m.cutoff;
      if (!isObject(cutoff)) {
        this.bundle('cutoff.metadata', 'cutoff', FAIL, 'Cutoff metadata is present', 'manifest has no cutoff object', 'Without cutoff metadata the snapshot cannot be identified.');
        return;
      }
      const requested = cutoff.requested_time_utc;
      ok = requested === pinned.requested_time_utc;
      this.bundle('cutoff.requested_time', 'cutoff', ok ? PASS : FAIL, 'Requested cutoff time matches the published snapshot',
        `found ${repr(requested)}, expected ${repr(pinned.requested_time_utc)}`, ok ? '' : 'The bundle claims a different cutoff time than the published snapshot.');
      for (const shard of ['shard0', 'shard1']) {
        const found = isObject(cutoff[shard]) ? cutoff[shard] : {};
        const expected = pinned[shard];
        for (const field of ['block', 'timestamp_utc', 'hash', 'state_root']) {
          let value = found[field];
          if (typeof value === 'string' && (field === 'hash' || field === 'state_root')) value = value.toLowerCase();
          ok = value === expected[field];
          this.bundle(`cutoff.${field}`, shard, ok ? PASS : FAIL, `${shard} cutoff ${field} matches the published snapshot`,
            `found ${repr(found[field])}, expected ${repr(expected[field])}`, ok ? '' : "The bundle's cutoff block identity differs from the published snapshot manifest.");
        }
        const stamp = found.timestamp_utc;
        ok = typeof stamp === 'string' && this.typeOk('utc_or_empty', stamp) && !!stamp && typeof requested === 'string' && stamp <= requested;
        this.bundle('cutoff.timestamp_order', shard, ok ? PASS : FAIL, `${shard} cutoff block is at or before the requested cutoff time`,
          `block time ${repr(stamp)}, requested ${repr(requested)}`, ok ? '' : 'A cutoff block after the requested time would include post-cutoff activity.');
      }
      const threshold = c.THRESHOLD_ATTO;
      const pinnedThreshold = this.snapshot.eligibility_1000_one.threshold_atto;
      ok = threshold === 1000n * c.ATTO_PER_ONE && String(threshold) === pinnedThreshold
        && m.threshold_atto === String(threshold) && this.snapshot.eligibility_1000_one.equality_policy === 'include';
      this.bundle('policy.threshold', 'threshold', ok ? PASS : FAIL, 'Threshold is exactly 1,000 ONE in atto units',
        `rules=${threshold} snapshot=${pinnedThreshold} bundle=${repr(m.threshold_atto)}`,
        ok ? '' : 'The bundle, the published snapshot, and the verifier must all use exactly 1,000,000,000,000,000,000,000 atto-ONE with an inclusive comparison.');
      const since = m.initial_window_since_utc;
      ok = since === c.INITIAL_SINCE_UTC;
      this.bundle('policy.initial_window', 'initial window', ok ? PASS : FAIL, 'Initial-stage activity window starts six calendar months before cutoff',
        `found ${repr(since)}, expected ${repr(c.INITIAL_SINCE_UTC)}`, ok ? '' : 'The six-month activity window in the bundle differs from the published policy.');
    }

    checkSources() {
      const kinds = this.rules.source_kinds;
      const sources = this.manifest.sources;
      if (!Array.isArray(sources)) return;
      const problems = [];
      sources.forEach((source, index) => {
        const where = `sources[${index}]`;
        if (!isObject(source)) { problems.push(`${where} must be an object`); return; }
        const id = source.id;
        if (typeof id !== 'string' || !id) { problems.push(`${where}.id must be a non-empty string`); return; }
        if (id in this.sources) problems.push(`duplicate source id ${id}`);
        const kind = source.kind;
        if (!(typeof kind === 'string' && kind in kinds)) problems.push(`${id}: unknown kind ${repr(kind)}`);
        else if (source.authoritative === true && kinds[kind].stale_prone) problems.push(`${id}: ${kind} sources are stale or inconsistent and cannot be authoritative`);
        if (typeof source.authoritative !== 'boolean') problems.push(`${id}: authoritative must be true or false`);
        const retrieved = source.retrieved_utc;
        if (typeof retrieved !== 'string' || !retrieved || !this.typeOk('utc_or_empty', retrieved)) problems.push(`${id}: retrieved_utc must be YYYY-MM-DDTHH:MM:SSZ`);
        if (typeof source.description !== 'string') problems.push(`${id}: description must be text`);
        this.sources[id] = source;
      });
      for (const entry of this.manifest.files) {
        if (isObject(entry) && !(typeof entry.source === 'string' && entry.source in this.sources)) {
          problems.push(`${repr(entry.path)} names unknown source ${repr(entry.source)}`);
        }
      }
      this.bundle('sources.metadata', 'sources', problems.length ? FAIL : PASS,
        'Source metadata is complete and does not treat RPC or explorer data as authoritative',
        problems.slice(0, MAX_EXAMPLES).join('; '),
        problems.length ? "Harmony's historical RPC and explorer infrastructure is stale or inconsistent; evidence from it can only corroborate, never decide." : '');
    }

    // -- files ----------------------------------------------------------------

    async hashFile(path) {
      const hash = new Sha256();
      const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });
      let utf8Error = null;
      for await (const chunk of this.fs.readChunks(path)) {
        hash.update(chunk);
        if (utf8Error === null) { try { decoder.decode(chunk, { stream: true }); } catch (error) { utf8Error = error.message; } }
      }
      if (utf8Error === null) { try { decoder.decode(); } catch (error) { utf8Error = error.message; } }
      return [hash.hexdigest(), utf8Error];
    }

    async checkFiles() {
      const roleSpecs = this.rules.roles;
      const jsonSpecs = this.rules.json_roles;
      const seenPaths = new Set();
      const declared = new Set();
      const available = new Set(this.allFiles);
      const entries = this.manifest.files;
      for (let index = 0; index < entries.length; index++) {
        const entry = entries[index];
        if (!isObject(entry)) {
          this.bundle('file.entry', `files[${index}]`, FAIL, 'Manifest file entry is well formed', 'entry must be an object', 'Each bundle file must be declared with role, path, sha256, bytes and source.');
          continue;
        }
        const subject = String(entry.path);
        const problems = [];
        const role = entry.role;
        const pathText = entry.path;
        if (!(typeof role === 'string' && (role in roleSpecs || role in jsonSpecs))) problems.push(`unknown role ${repr(role)}`);
        if (!normalizedPath(pathText)) {
          problems.push('path must be a relative path inside the bundle');
        } else if (seenPaths.has(pathText)) problems.push('path is declared twice');
        const sha = entry.sha256;
        if (typeof sha !== 'string' || !/^[0-9a-f]{64}$/.test(sha)) problems.push('sha256 must be 64 lowercase hex characters');
        const size = entry.bytes;
        if (!jsonInt(size)) problems.push('bytes must be a non-negative integer');
        if (typeof role === 'string' && role in roleSpecs) {
          if (!jsonInt(entry.rows)) problems.push('rows must be a non-negative integer');
          const categories = roleSpecs[role].categories;
          if (categories && !categories.includes(entry.category)) problems.push(`category must be one of ${JSON.stringify(categories)}`);
        }
        if (problems.length) {
          this.bundle('file.entry', subject, FAIL, 'Manifest file entry is well formed', problems.join('; '), 'The file cannot be trusted without a complete, valid declaration.');
          continue;
        }
        seenPaths.add(pathText);
        declared.add(pathText);
        if (!available.has(pathText)) {
          this.bundle('file.present', pathText, FAIL, 'Declared file is present', 'file is missing', 'A file named by the manifest is missing, so its checks cannot run.');
          continue;
        }
        this.bundle('file.present', pathText, PASS, 'Declared file is present');
        this.progress(`hashing ${pathText}`);
        const [actual, utf8Error] = await this.hashFile(pathText);
        if (utf8Error && role in roleSpecs) this.utf8Errors[pathText] = utf8Error;
        const actualSize = await this.fs.size(pathText);
        this.fileHashes[pathText] = actual;
        const ok = actual === sha && actualSize === size;
        this.bundle('file.sha256', pathText, ok ? PASS : FAIL, 'File SHA-256 and size match the manifest',
          `sha256 ${actual}, ${actualSize} bytes` + (ok ? '' : `; manifest says ${sha}, ${size} bytes`),
          ok ? '' : 'The file was changed after the bundle was frozen, or the manifest is wrong.');
        if (role in roleSpecs) {
          this.roles[role].present = true;
          this.roles[role].files.push(entry);
        } else {
          (this.jsonDocs[role] = this.jsonDocs[role] || []).push(entry);
        }
      }
      const missing = Object.entries(roleSpecs).filter(([role, spec]) => spec.required && !this.roles[role].present).map(([role]) => role);
      for (const [role, spec] of Object.entries(roleSpecs)) {
        for (const category of spec.categories || []) {
          if (this.roles[role].present && !this.roles[role].files.some((entry) => entry.category === category)) missing.push(`${role}:${category}`);
        }
      }
      this.bundle('files.required_roles', 'roles', missing.length ? FAIL : PASS, 'Every required evidence file is present',
        missing.length ? 'missing: ' + missing.join(', ') : '', missing.length ? 'Required files are missing, so the matching cross-file checks cannot run.' : '');
      const undeclared = this.allFiles.filter((path) => {
        const name = path.split('/').pop();
        return !name.startsWith('.') && path !== MANIFEST_NAME && !declared.has(path);
      }).sort();
      this.bundle('files.undeclared', 'bundle', undeclared.length ? FAIL : PASS, 'Bundle contains no undeclared files',
        undeclared.slice(0, MAX_EXAMPLES).join(', '), undeclared.length ? 'A frozen bundle must be closed: every file is declared and hashed.' : '');
    }

    rolePresent(role) {
      if (role in this.roles) return this.roles[role].present;
      return !!(this.parsedJson[role] && this.parsedJson[role].length);
    }

    roleRequired(role) {
      const spec = this.rules.roles[role] || this.rules.json_roles[role] || {};
      return !!spec.required;
    }

    checkWoneSummary() {
      const docs = this.parsedJson.wone_overlay_summary || [];
      if (!docs.length) return;
      const [entry, summary] = docs[0];
      const problems = [];
      const overlayFiles = this.roles.wone_overlay.files;
      const overlayHash = overlayFiles.length ? this.fileHashes[overlayFiles[0].path] : undefined;
      if (summary.output_sha256 !== overlayHash) problems.push('output_sha256 does not identify the WONE overlay file');
      const requested = summary.excluded_addresses_requested;
      let addresses = [];
      if (!Array.isArray(requested) || !requested.every((item) => typeof item === 'string' && this.typeOk('address', item))) {
        problems.push('excluded_addresses_requested must be a list of addresses');
      } else {
        addresses = [...new Set(requested.map((item) => item.toLowerCase()))].sort();
        const missing = this.engine.constants.WONE_REQUIRED_EXCLUSIONS.filter((item) => !addresses.includes(item)).sort();
        if (missing.length) problems.push('excluded_addresses_requested lacks required exclusions: ' + missing.join(', '));
      }
      if (problems.length) delete this.parsedJson.wone_overlay_summary;
      else this.engine.constants.WONE_EXCLUDED_ADDRESSES = addresses;
      this.bundle('summary.wone_overlay', entry.path, problems.length ? FAIL : PASS,
        'WONE overlay summary identifies the overlay file and its exclusion set',
        problems.length ? problems.join('; ') : `${addresses.length} excluded address(es)`,
        problems.length ? 'The WONE exclusion set cannot be trusted, so WONE census checks are not run.' : '');
    }

    async checkJsonDocs() {
      this.parsedJson = {};
      for (const [role, entries] of Object.entries(this.jsonDocs)) {
        const spec = this.rules.json_roles[role];
        if (entries.length > 1 && !spec.multiple) {
          this.bundle('file.entry', role, FAIL, 'JSON role appears once', `${entries.length} files declared`, 'Only one file of this kind may be part of a bundle.');
        }
        for (const entry of entries) {
          let value;
          try {
            const bytes = await this.fs.readBytes(entry.path);
            let text;
            try { text = new TextDecoder('utf-8', { fatal: true }).decode(bytes); } catch (error) { throw new Error(`${entry.path}: not UTF-8`); }
            value = parseJsonStrict(text, entry.path);
            if (!isObject(value)) throw new Error(`${entry.path}: top-level value must be an object`);
          } catch (error) {
            this.bundle('file.json', entry.path, FAIL, 'JSON file parses strictly', error.message, 'Malformed evidence cannot be used and is never skipped silently.');
            continue;
          }
          (this.parsedJson[role] = this.parsedJson[role] || []).push([entry, value]);
        }
      }
    }

    // -- CSV scanning ---------------------------------------------------------

    static typeProblem(name, kind, value) {
      if (isObject(kind)) return `${name} ${repr(value)} is not one of ${JSON.stringify(kind.enum)}`;
      if (kind.startsWith('uint') && value.startsWith('-')) return `${name} is negative (${value})`;
      if (kind.startsWith('uint')) return `${name} ${repr(value)} is not a canonical unsigned integer`;
      if (kind.startsWith('address')) return `${name} ${repr(value)} is not a 0x-prefixed 20-byte address`;
      if (kind.startsWith('key')) return `${name} ${repr(value)} is not a lowercase 0x-prefixed 32-byte secure key`;
      return `${name} ${repr(value)} is not a valid ${kind}`;
    }

    async iterRoleRows(role, collector) {
      const spec = this.rules.roles[role];
      const data = this.roles[role];
      const unique = {};
      for (const name of spec.unique || []) unique[name] = new Set();
      const files = data.files.slice().sort((a, b) => {
        const ka = [a.category || '', a.path], kb = [b.category || '', b.path];
        return ka[0] < kb[0] ? -1 : ka[0] > kb[0] ? 1 : ka[1] < kb[1] ? -1 : ka[1] > kb[1] ? 1 : 0;
      });
      for (const entry of files) {
        this.progress(`reading ${entry.path}`);
        if (entry.path in this.utf8Errors) {
          this.bundle('file.schema', entry.path, FAIL, 'File has the required columns', `cannot parse: ${this.utf8Errors[entry.path]}`, 'The file is not valid UTF-8 CSV, so its rows cannot be read.');
          this.tainted.add(role);
          continue;
        }
        let count;
        try {
          count = await this.scanFile(role, spec, unique, entry, collector);
        } catch (error) {
          if (error instanceof EvalError || error instanceof BundleError) throw error;
          this.bundle('file.schema', entry.path, FAIL, 'File has the required columns', `cannot parse: ${error.message}`, 'The file is not valid UTF-8 CSV, so its rows cannot be read.');
          this.tainted.add(role);
          continue;
        }
        if (count === null) continue;
        const ok = entry.rows === count;
        this.bundle('file.rows', entry.path, ok ? PASS : FAIL, 'Row count matches the manifest',
          `${count} data rows` + (ok ? '' : `; manifest says ${entry.rows}`), ok ? '' : 'Rows were added or removed after the bundle was frozen.');
      }
      if (data.present) {
        const ok = data.errors === 0 && !this.tainted.has(role);
        this.bundle('file.rows_valid', role, ok ? PASS : FAIL,
          'Every row is well formed (types, non-negative integers, ordering, no duplicates)',
          ok ? '' : `${data.errors} invalid row(s); ` + data.examples.join(' | '),
          ok ? '' : 'Invalid records are reported, never skipped; the bundle cannot pass while any record is malformed.');
        if (data.errors) this.tainted.add(role);
      }
    }

    async scanFile(role, spec, unique, entry, collector) {
      const data = this.roles[role];
      const fields = spec.fields;
      const pathText = entry.path;
      let header = null;
      let count = 0;
      let line = 1;
      let previous = null;
      for await (const record of csvRecords(this.fs, pathText)) {
        if (header === null) {
          header = record;
          let problem = null;
          if (new Set(header).size !== header.length) problem = 'header repeats a column name';
          else if (spec.exact_header && JSON.stringify(header) !== JSON.stringify(spec.exact_header)) problem = `header must be exactly ${JSON.stringify(spec.exact_header)}`;
          else {
            const missing = Object.keys(fields).filter((name) => !header.includes(name));
            if (missing.length) problem = 'missing columns: ' + missing.join(', ');
          }
          if (problem) {
            this.bundle('file.schema', pathText, FAIL, 'File has the required columns', problem, 'Rows cannot be read without the documented columns.');
            this.tainted.add(role);
            return null;
          }
          continue;
        }
        line += 1;
        count += 1;
        const problems = [];
        const row = {};
        for (let i = 0; i < Math.min(header.length, record.length); i++) row[header[i]] = record[i];
        if (record.length !== header.length) {
          problems.push(`has ${record.length} fields, header has ${header.length}`);
        } else {
          for (const [name, kind] of Object.entries(fields)) {
            if (!this.typeOk(kind, row[name])) problems.push(Verifier.typeProblem(name, kind, row[name]));
          }
          for (const name of spec.positive || []) {
            if (name in row && this.typeOk('uint', row[name]) && BigInt(row[name]) <= 0n) problems.push(`${name} must be positive`);
          }
          const sortField = spec.sorted_by;
          if (sortField && !problems.length) {
            const key = row[sortField].toLowerCase();
            if (previous !== null && key <= previous) problems.push(`${sortField} is not strictly increasing`);
            previous = key;
          }
          for (const [name, values] of Object.entries(unique)) {
            const value = (row[name] || '').toLowerCase();
            if (values.has(value)) problems.push(`duplicate ${name} ${value}`);
            values.add(value);
          }
        }
        row._line = `${pathText}:${line}`;
        if (entry.category) row._category = entry.category;
        if (problems.length) {
          data.errors += 1;
          row._problems = problems;
          if (data.examples.length < MAX_EXAMPLES) data.examples.push(`${pathText}:${line}: ` + problems.join('; '));
        }
        await collector(row, problems);
      }
      if (header === null) {
        this.bundle('file.schema', pathText, FAIL, 'File has the required columns', 'file is empty (no header)', 'Rows cannot be read without the documented columns.');
        this.tainted.add(role);
        return null;
      }
      return count;
    }

    makeAccumulator(role) {
      const specs = this.rules.totals.filter((spec) => spec.role === role);
      const sums = {};
      for (const spec of specs) sums[spec.id] = 0n;
      const empty = new RowContext({});
      const add = (row, problems) => {
        if (problems.length) return;
        for (const spec of specs) {
          try {
            if (spec.where !== undefined && !this.engine.eval(spec.where, empty, row, role)) continue;
            if (spec.count) sums[spec.id] += 1n;
            else if (spec.value !== undefined) sums[spec.id] += Engine.intValue(this.engine.eval(spec.value, empty, row, role));
            else sums[spec.id] += this.engine.typed(role, spec.field, row[spec.field]);
          } catch (error) {
            if (!(error instanceof EvalError)) throw error;
            this.tainted.add(role);
          }
        }
      };
      return [sums, add];
    }

    checkIdentityInline(role, row) {
      const state = this.identityState[role] = this.identityState[role] || { rows: 0, failures: 0, examples: [] };
      state.rows += 1;
      for (const problem of this.identityProblems(role, row)) {
        state.failures += 1;
        if (state.examples.length < MAX_EXAMPLES) state.examples.push(problem);
      }
    }

    /** keccak256(address) == secure_key and one1 == bech32(hex) for one record. */
    identityProblems(role, row) {
      const problems = [];
      const spec = this.rules.roles[role];
      for (const [addressField, keyField] of spec.identity || []) {
        const address = row[addressField] || '';
        const key = row[keyField] || '';
        if (!address && !key) continue;
        try { requireAddressSecureKey(address, key, row._line); } catch (error) { problems.push(error.message); }
      }
      for (const [one1Field, ethField] of spec.bech32_identity || []) {
        const problem = bech32Problem(row[one1Field] || '', row[ethField] || '', row._line);
        if (problem) problems.push(problem);
      }
      return problems;
    }

    hasIdentity(role) {
      const spec = this.rules.roles[role];
      return !!((spec.identity && spec.identity.length) || (spec.bech32_identity && spec.bech32_identity.length));
    }

    finishIdentities(role) {
      const state = this.identityState[role] || { rows: 0, failures: 0, examples: [] };
      const failures = state.failures;
      this.bundle('metadata.identity_all', role, failures ? FAIL : PASS,
        "Every row's address hashes to its secure key (keccak256(address) == secure_key)",
        failures ? `${failures} mismatch(es): ` + state.examples.join(' | ') : `${state.rows} rows checked`,
        failures ? 'An address that does not hash to its secure key belongs to a different account.' : '');
    }

    // -- main flow ------------------------------------------------------------

    async run() {
      await this.readManifest();
      this.checkManifest();
      this.checkCutoff();
      this.checkSources();
      await this.checkFiles();
      await this.checkJsonDocs();
      this.checkWoneSummary();
      this.checkSnapshotSummaryLinks();

      if (this.sampleSize !== null && this.sampleSize < 0) throw new BundleError('sample size must be non-negative');
      const size = this.sampleSize || 0;
      const sampler = new Sampler(this.rules, this.seed, size);
      const showPayload = {};
      const overlayPresent = this.roles.wone_overlay.present;
      const [sums, addTotal] = this.makeAccumulator('wone_overlay');
      if (overlayPresent) {
        await this.iterRoleRows('wone_overlay', (row, problems) => {
          addTotal(row, problems);
          const key = (row.secure_key || '').toLowerCase();
          const address = (row.address || '').toLowerCase();
          const identifier = this.sampleBy === 'secure_key' ? key : address;
          if (!identifier) return;
          sampler.offer(identifier, row);
          if (this.showRow && (this.showRow === key || this.showRow === address)) showPayload.row = row;
          if (this.verifyAllMetadata && !problems.length) this.checkIdentityInline('wone_overlay', row);
        });
        Object.assign(this.totals, sums);
      }
      const population = sampler.population;
      const drawn = sampler.result();
      const overlayReadable = overlayPresent && !(population === 0 && this.tainted.has('wone_overlay'));
      if (overlayReadable && size > population) {
        throw new BundleError(`sample size ${size} exceeds the population of ${population} rows; the verifier never reduces a requested sample`);
      }
      if (overlayReadable && drawn.length < size) {
        throw new BundleError(`only ${drawn.length} distinct identifiers exist for a sample of ${size}: the current-claim ledger repeats identifiers (run with "check every row" to list the duplicates); the verifier never reduces a requested sample`);
      }
      const sample = drawn.map(([identifier, row, score]) => ({ identifier, score: score.toString(16).padStart(64, '0'), row }));
      if (this.showRow && !showPayload.row) throw new BundleError(`show row ${this.showRow}: no current-claim row has that secure key or address`);

      const focus = {};
      for (const item of sample) focus[item.row.secure_key.toLowerCase()] = item.row;
      if (this.showRow) focus[showPayload.row.secure_key.toLowerCase()] = showPayload.row;
      const addresses = {};
      const matched = {};
      for (const item of sample.concat(this.showRow ? [{ row: showPayload.row }] : [])) {
        const address = item.row.address.toLowerCase();
        const keys = addresses[address] = addresses[address] || [];
        const key = item.row.secure_key.toLowerCase();
        if (!keys.includes(key)) keys.push(key);
      }
      for (const [key, row] of Object.entries(focus)) matched[key] = { wone_overlay: [row] };
      if (this.verifyAllMetadata && overlayPresent) this.finishIdentities('wone_overlay');

      await this.scanOtherRoles(matched, addresses);
      this.rowResults = sample.map((item) => this.verifyRow(item.identifier,
        Object.assign({}, matched[item.row.secure_key.toLowerCase()], { wone_overlay: [item.row] })));
      this.showResult = null;
      if (this.showRow) {
        this.showResult = this.verifyRow(this.showRow,
          Object.assign({}, matched[showPayload.row.secure_key.toLowerCase()], { wone_overlay: [showPayload.row] }));
      }

      this.checkTotals();
      this.checkSummaries();
      await this.checkAirdropRun();
      this.checkChain(sample);
      return this.buildReport(sample, population);
    }

    async scanOtherRoles(matched, addresses) {
      this.readyByDestination = new Map();
      this.airdropAmounts = new Map();
      this.airdropProblems = [];
      const order = this.rules.role_order.filter((role) => role !== 'wone_overlay');
      for (const role of order) {
        const data = this.roles[role];
        if (!data.present) continue;
        const spec = this.rules.roles[role];
        const matchField = spec.match;
        const [sums, addTotal] = this.makeAccumulator(role);
        const byAddress = spec.join === 'address';
        let nativeJoin = null;
        if (role === 'native_claims' && this.roles.wone_overlay.present) {
          nativeJoin = new SortedKeyCursor(this.fs, this.roles.wone_overlay.files[0].path);
          await nativeJoin.init();
          this.nativeMissing = [];
        }
        await this.iterRoleRows(role, async (row, problems) => {
          addTotal(row, problems);
          const value = (row[matchField] || '').toLowerCase();
          const keys = byAddress ? (addresses[value] || []) : [value];
          for (const target of keys) if (target in matched) (matched[target][role] = matched[target][role] || []).push(row);
          const key = value;
          if (this.verifyAllMetadata && !problems.length && this.hasIdentity(role)) this.checkIdentityInline(role, row);
          if (nativeJoin && !problems.length) {
            if (!(await nativeJoin.contains(value))) this.nativeMissing.push(row._line);
          }
          if (role === 'wallet_allocations' && !problems.length && row.destination_status === 'ready') {
            const destination = row.destination_address.toLowerCase();
            this.readyByDestination.set(destination, (this.readyByDestination.get(destination) || 0n) + BigInt(row.amount_atto));
            if (key !== undefined && key in matched) (matched[key]._destinations = matched[key]._destinations || new Set()).add(destination);
          }
          if (role === 'airdrop_distribution' && !problems.length) {
            const address = row.address.toLowerCase();
            const amount = BigInt(row.amount);
            this.airdropAmounts.set(address, amount);
            const expected = this.readyByDestination.get(address);
            if (expected !== amount) this.airdropProblems.push(`${row._line}: ${address} receives ${amount}, ready wallet allocations to it total ${expected || 0n}`);
          }
        });
        Object.assign(this.totals, sums);
        if (this.verifyAllMetadata && this.hasIdentity(role)) this.finishIdentities(role);
        if (nativeJoin) {
          this.bundle('global.native_in_overlay', 'native_claims', this.nativeMissing.length ? FAIL : PASS,
            'Every native claim row appears in the current claim ledger', this.nativeMissing.slice(0, MAX_EXAMPLES).join(', '),
            this.nativeMissing.length ? 'A native claim missing from the current ledger would silently drop an account.' : '');
        }
      }
      this.checkAirdropGlobal();
    }

    checkAirdropGlobal() {
      if (!this.roles.airdrop_distribution.present) return;
      const problems = this.airdropProblems.slice();
      if ((this.manifest.airdrop_scope || 'partial') === 'complete') {
        for (const destination of [...this.readyByDestination.keys()].sort()) {
          if (!this.airdropAmounts.has(destination)) problems.push(`ready destination ${destination} (${this.readyByDestination.get(destination)}) is missing from a complete airdrop`);
        }
      }
      this.bundle('global.airdrop_destinations', 'airdrop_distribution', problems.length ? FAIL : PASS,
        'Every airdrop recipient is a ready wallet destination with the exact allocated amount',
        problems.slice(0, MAX_EXAMPLES).join(' | '),
        problems.length ? 'The airdrop list must be built only from ready wallet allocations, paying each destination its full ready amount (held destinations are never paid).' : '');
    }

    // -- per row --------------------------------------------------------------

    verifyRow(identifier, roleRows) {
      const engine = this.engine;
      const ctx = new RowContext(roleRows, this.totals);
      const checks = [];
      const overlay = roleRows.wone_overlay[0];
      const add = (id, status, title, message, explain, required) => checks.push({
        scope: 'row', id, subject: identifier, status, required: required === undefined ? true : !!required,
        title, message: message || '', explain: explain || '',
      });
      for (const role of Object.keys(roleRows).sort()) {
        if (role.startsWith('_')) continue;
        const rows = roleRows[role];
        const spec = this.rules.roles[role];
        const problems = rows.flatMap((row) => row._problems || []);
        add(`schema.${role}`, problems.length ? FAIL : PASS, `${spec.label} record is well formed`, problems.join('; '),
          problems.length ? 'The record has a malformed, negative, or missing value, so it cannot be trusted.' : '');
        const identityProblems = rows.flatMap((row) => this.identityProblems(role, row));
        if (this.hasIdentity(role)) {
          add(`identity.${role}`, identityProblems.length ? FAIL : PASS, 'Address identity is consistent (keccak256(address) == secure_key, one1 == bech32(hex))',
            identityProblems.join('; '), identityProblems.length ? 'The address and secure key belong to different accounts.' : '');
        }
        if (rows.length > 1 && (spec.unique || []).includes(spec.match)) {
          add(`duplicate.${role}`, FAIL, 'Record appears once', `${rows.length} records: ` + rows.map((row) => row._line).join(', '),
            'Duplicate records for one account could pay it twice.');
        }
      }
      for (const rule of engine.rowChecks) {
        const needsFiles = (rule.needs_files || []).concat(rule.needs_rows || []);
        const absent = needsFiles.filter((role) => !this.rolePresent(role));
        if (absent.length) {
          const required = absent.every((role) => this.roleRequired(role));
          add(rule.id, NOT_VERIFIED, rule.title, 'not in bundle: ' + [...new Set(absent)].sort().join(', '),
            'The evidence file needed for this check is not part of the bundle.', required);
          continue;
        }
        if ((rule.needs_rows || []).some((role) => !(roleRows[role] || []).length)) continue;
        try {
          if (rule.when !== undefined && !Engine.boolValue(engine.eval(rule.when, ctx))) continue;
          if (rule.field_equal) {
            const spec = rule.field_equal;
            const left = roleRows[spec.left][0];
            const right = roleRows[spec.right][0];
            const differ = spec.fields
              .filter((name) => (left[name] || '').toLowerCase() !== (right[name] || '').toLowerCase())
              .map((name) => `${name}: ${repr(left[name])} vs ${repr(right[name])}`);
            add(rule.id, differ.length ? FAIL : PASS, rule.title, differ.join('; '), differ.length ? rule.explain : '');
            continue;
          }
          const ok = Engine.boolValue(engine.eval(rule.assert, ctx));
          add(rule.id, ok ? PASS : FAIL, rule.title, ok ? '' : engine.explainFailure(rule.assert, ctx), ok ? '' : rule.explain);
        } catch (error) {
          if (error instanceof MissingRow) add(rule.id, FAIL, rule.title, error.message, rule.explain);
          else if (error instanceof EvalError) add(rule.id, FAIL, rule.title, `could not evaluate: ${error.message}`, rule.explain);
          else throw error;
        }
      }
      this.verifyRowAirdrop(roleRows, add);
      const values = {};
      for (const field of [
        'liquid_shard0_atto', 'liquid_shard1_atto', 'liquid_total_atto', 'active_staked_or_delegated_atto',
        'pending_undelegation_atto', 'unclaimed_staking_reward_atto', 'pending_cross_shard_atto',
        'native_wallet_airdrop_atto', 'wone_balance_atto', 'wone_airdrop_atto', 'wallet_airdrop_atto',
        'staked_to_vault_atto', 'qualification_total_atto', 'native_total_claim_atto', 'total_claim_atto',
      ]) {
        const raw = overlay[field] === undefined ? '' : overlay[field];
        values[field] = { atto: raw, one: this.typeOk('uint', raw) ? fixed18(BigInt(raw)) : '' };
      }
      const stage = (roleRows.stage_policy || [{}])[0];
      const eligibility = (roleRows.eligibility || [{}])[0];
      const qualification = overlay.qualification_total_atto;
      const records = {};
      for (const role of Object.keys(roleRows).sort()) if (!role.startsWith('_')) records[role] = roleRows[role].map((row) => row._line);
      return {
        identifier,
        secure_key: overlay.secure_key || '',
        address: overlay.address || '',
        status: tierStatus(checks, 'row'),
        qualifies: this.typeOk('uint', qualification || '') ? BigInt(qualification) >= engine.constants.THRESHOLD_ATTO : null,
        eligibility_category: eligibility._category || '',
        migration_stage: stage.migration_stage || '',
        issuance_treatment: stage.issuance_treatment || '',
        routing_category: stage.routing_category || '',
        values,
        records,
        checks,
      };
    }

    verifyRowAirdrop(roleRows, add) {
      const title = 'Airdrop amount and destination match the ready wallet allocation';
      if (!this.roles.airdrop_distribution.present) {
        add('airdrop.row', NOT_VERIFIED, title, 'no airdrop distribution in bundle', 'No airdrop list was included, so delivery amounts were not compared.', false);
        return;
      }
      const destinations = [...(roleRows._destinations || new Set())].sort();
      const address = roleRows.wone_overlay[0].address.toLowerCase();
      const problems = [];
      const paid = [];
      for (const destination of destinations) {
        const expected = this.readyByDestination.get(destination) || 0n;
        if (this.airdropAmounts.has(destination)) {
          const amount = this.airdropAmounts.get(destination);
          if (amount !== expected) problems.push(`${destination} receives ${amount}, expected ${expected}`);
          else paid.push(destination);
        } else if (this.manifest.airdrop_scope === 'complete') problems.push(`${destination} is missing from a complete airdrop`);
      }
      if (this.airdropAmounts.has(address) && !this.readyByDestination.has(address)) problems.push(`${address} is in the airdrop but has no ready wallet allocation`);
      if (problems.length) add('airdrop.row', FAIL, title, problems.join('; '), 'The airdrop pays an amount or destination that the wallet allocation does not authorize.');
      else if (!destinations.length && !this.airdropAmounts.has(address)) add('airdrop.row', PASS, title, 'row has no ready wallet destination and is not in the airdrop');
      else if (paid.length === destinations.length) add('airdrop.row', PASS, title, 'paid: ' + paid.join(', '));
      else {
        add('airdrop.row', NOT_VERIFIED, title, 'ready destination not in this (partial) airdrop run: ' + destinations.filter((d) => !paid.includes(d)).join(', '),
          'This airdrop run is declared partial; the destination may be paid in a later batch.', false);
      }
    }

    // -- totals and summaries -------------------------------------------------

    checkTotals() {
      const declared = isObject(this.manifest.declared_totals) ? this.manifest.declared_totals : {};
      const known = {};
      for (const spec of this.rules.totals) known[spec.id] = spec;
      for (const [id, spec] of Object.entries(known)) {
        const role = spec.role;
        const title = 'Declared total matches the recomputed total';
        if (!this.roles[role].present) {
          if (id in declared) this.bundle('totals.declared', id, FAIL, title, `declared ${declared[id]} but the ${role} file is not in the bundle`, 'A total was published for evidence that is missing.');
          continue;
        }
        if (this.tainted.has(role)) {
          this.bundle('totals.declared', id, FAIL, title, `cannot recompute: ${role} has invalid rows`, 'Totals are not trustworthy while any record is malformed.');
          continue;
        }
        const computed = this.totals[id];
        if (!(id in declared)) {
          this.bundle('totals.declared', id, FAIL, title, `recomputed ${computed}; manifest declares no value`, 'Every bundle total must be declared so it can be published and compared.');
          continue;
        }
        const ok = declared[id] === String(computed);
        this.bundle('totals.declared', id, ok ? PASS : FAIL, title, `recomputed ${computed}` + (ok ? '' : `, manifest declares ${declared[id]}`), ok ? '' : "The bundle's published total differs from its own files.");
      }
      const unknown = Object.keys(declared).filter((id) => !(id in known)).sort();
      if (unknown.length) this.bundle('totals.unknown', 'declared_totals', FAIL, 'Declared totals are all recognized', unknown.join(', '), 'The manifest declares totals this verifier cannot recompute.');
      const ctx = new RowContext({}, this.totals);
      for (const rule of this.rules.total_checks) {
        const absent = rule.roles.filter((role) => !this.roles[role].present);
        if (absent.length) {
          const required = absent.every((role) => this.rules.roles[role].required);
          this.bundle(rule.id, 'totals', NOT_VERIFIED, rule.title, 'not in bundle: ' + absent.join(', '), 'The evidence file needed for this total is not part of the bundle.', required);
          continue;
        }
        const tainted = rule.roles.filter((role) => this.tainted.has(role));
        if (tainted.length) {
          this.bundle(rule.id, 'totals', FAIL, rule.title, 'cannot recompute: invalid rows in ' + tainted.join(', '), 'Totals are not trustworthy while any record is malformed.');
          continue;
        }
        let ok;
        let message;
        try {
          ok = Engine.boolValue(this.engine.eval(rule.assert, ctx));
          message = ok ? '' : this.engine.explainFailure(rule.assert, ctx);
        } catch (error) {
          if (!(error instanceof EvalError)) throw error;
          ok = false;
          message = `could not evaluate: ${error.message}`;
        }
        this.bundle(rule.id, 'totals', ok ? PASS : FAIL, rule.title, message, ok ? '' : 'Bundle-level totals do not close, so at least one file disagrees with another.');
      }
    }

    /** Hash links, cutoff, and WONE overrides from the cutoff snapshot summary. */
    checkSnapshotSummaryLinks() {
      const docs = this.parsedJson.snapshot_summary || [];
      if (!docs.length) return;
      const [entry, summary] = docs[0];
      const problems = [];
      const pinned = this.snapshot.cutoff;
      if (summary.cutoff_time_utc !== pinned.requested_time_utc) problems.push(`cutoff_time_utc is ${repr(summary.cutoff_time_utc)}`);
      const cutoff = isObject(summary.cutoff) ? summary.cutoff : {};
      for (const shard of ['shard0', 'shard1']) {
        const item = isObject(cutoff[shard]) ? cutoff[shard] : {};
        if (!(jsonInt(item.block) && item.block === pinned[shard].block)) problems.push(`cutoff.${shard}.block does not match the published snapshot`);
        if (typeof item.hash !== 'string' || item.hash.toLowerCase() !== pinned[shard].hash) problems.push(`cutoff.${shard}.hash does not match the published snapshot`);
      }
      for (const [key, role] of Object.entries(SNAPSHOT_SUMMARY_FILES)) {
        const record = isObject(summary[key]) ? summary[key] : {};
        for (const bundleEntry of this.roles[role].files) {
          const recorded = typeof record.sha256 === 'string' ? record.sha256.toLowerCase().replace(/^0x/, '') : record.sha256;
          if (recorded !== this.fileHashes[bundleEntry.path]) problems.push(`${key}.sha256 does not identify ${bundleEntry.path}`);
        }
      }
      const overrides = summary.ledger_wone_overrides;
      const addresses = [];
      if (!Array.isArray(overrides)) problems.push('ledger_wone_overrides must be a list');
      else {
        overrides.forEach((item, index) => {
          const ok = isObject(item) && typeof item.address === 'string' && this.typeOk('address', item.address)
            && ['ledger_wone_atto', 'census_wone_atto'].every((field) => typeof item[field] === 'string' && this.typeOk('uint', item[field]));
          if (!ok) problems.push(`ledger_wone_overrides[${index}] needs address, ledger_wone_atto, census_wone_atto`);
          else addresses.push(item.address.toLowerCase());
        });
      }
      if (problems.length) delete this.parsedJson.snapshot_summary;
      else this.engine.constants.SNAPSHOT_WONE_OVERRIDES = [...new Set(addresses)].sort();
      this.snapshotSummaryEntry = [entry, summary, problems];
    }

    /** Row counts and reconciliation entries of the cutoff snapshot summary. */
    checkSnapshotSummaryTotals() {
      if (!this.snapshotSummaryEntry) return;
      const [entry, summary, earlier] = this.snapshotSummaryEntry;
      const problems = earlier.slice();
      for (const [key, role] of Object.entries(SNAPSHOT_SUMMARY_FILES)) {
        const record = isObject(summary[key]) ? summary[key] : {};
        for (const bundleEntry of this.roles[role].files) {
          if (!(jsonInt(record.rows) && record.rows === bundleEntry.rows)) problems.push(`${key}.rows ${repr(record.rows)} != ${bundleEntry.rows}`);
        }
      }
      const reconciliation = isObject(summary.reconciliation) ? summary.reconciliation : {};
      if (!Object.keys(reconciliation).length) problems.push('reconciliation is missing');
      const number = (value) => {
        if (jsonInt(value)) return BigInt(value);
        if (typeof value === 'string' && this.typeOk('uint', value)) return BigInt(value);
        return null;
      };
      for (const name of Object.keys(reconciliation).sort()) {
        const item = reconciliation[name];
        const value = isObject(item) ? number(item.value) : null;
        const expected = isObject(item) ? number(item.expected) : null;
        if (value === null || expected === null || value !== expected) problems.push(`reconciliation.${name}: value ${repr(item)} does not equal expected`);
      }
      for (const [name, total] of [['ledger_rows', 'wone_overlay.rows'], ['native_total_atto', 'wone_overlay.native_total_claim_atto']]) {
        const item = isObject(reconciliation[name]) ? reconciliation[name] : {};
        if (total in this.totals && number(item.value) !== this.totals[total]) {
          problems.push(`reconciliation.${name} is ${repr(item.value)}; the bundle gives ${this.totals[total]}`);
        }
      }
      this.bundle('summary.snapshot', entry.path, problems.length ? FAIL : PASS,
        'Cutoff snapshot summary matches the cutoff, the bundled snapshot files, and the claim ledger',
        problems.slice(0, MAX_EXAMPLES).join('; '),
        problems.length ? 'The published snapshot summary disagrees with the bundled files or the claim ledger.' : '');
    }

    checkSummaries() {
      this.checkSnapshotSummaryTotals();
      const docs = this.parsedJson;
      const stageFiles = this.roles.stage_policy.files;
      const stageHash = stageFiles.length ? this.fileHashes[stageFiles[0].path] : undefined;
      for (const [entry, summary] of docs.stage_summary || []) {
        const problems = [];
        if (summary.output_sha256 !== stageHash) problems.push('output_sha256 does not identify the stage policy file');
        if ('stage_policy.rows' in this.totals && !(jsonInt(summary.qualified_rows) && BigInt(summary.qualified_rows) === this.totals['stage_policy.rows'])) {
          problems.push(`qualified_rows ${repr(summary.qualified_rows)} != ${this.totals['stage_policy.rows']}`);
        }
        this.bundle('summary.stage', entry.path, problems.length ? FAIL : PASS, 'Stage summary identifies the stage policy file and row count', problems.join('; '),
          problems.length ? "The pipeline's own summary disagrees with the bundled stage policy." : '');
      }
      for (const [entry, summary] of docs.routing_summary || []) {
        const problems = [];
        if (summary.migration_stages_sha256 !== stageHash) problems.push('migration_stages_sha256 does not identify the stage policy file');
        const routingFiles = this.roles.routing_exceptions.files;
        const routingHash = routingFiles.length ? this.fileHashes[routingFiles[0].path] : undefined;
        const outputs = isObject(summary.outputs) ? summary.outputs : {};
        const recorded = isObject(outputs.routing_exceptions) ? outputs.routing_exceptions.sha256 : undefined;
        if (recorded !== routingHash) problems.push('outputs.routing_exceptions.sha256 does not identify the routing exceptions file');
        this.bundle('summary.routing', entry.path, problems.length ? FAIL : PASS, 'Routing summary identifies the stage policy and routing exceptions', problems.join('; '),
          problems.length ? "The pipeline's own summary disagrees with the bundled routing files." : '');
      }
      for (const [entry, summary] of docs.eligibility_summary || []) {
        const problems = [];
        if (summary.comparison !== 'ge') problems.push(`comparison is ${repr(summary.comparison)}, expected 'ge' (inclusive)`);
        if (summary.minimum_atto !== String(this.engine.constants.THRESHOLD_ATTO)) problems.push(`minimum_atto is ${repr(summary.minimum_atto)}`);
        const categories = isObject(summary.categories) ? summary.categories : {};
        for (const fileEntry of this.roles.eligibility.files) {
          const record = isObject(categories[fileEntry.category]) ? categories[fileEntry.category] : {};
          if (record.output_sha256 !== this.fileHashes[fileEntry.path]) problems.push(`${fileEntry.category}: output_sha256 does not identify ${fileEntry.path}`);
          if (!(jsonInt(record.rows) && record.rows === fileEntry.rows)) problems.push(`${fileEntry.category}: rows ${repr(record.rows)} != ${fileEntry.rows}`);
        }
        this.bundle('summary.eligibility', entry.path, problems.length ? FAIL : PASS,
          'Eligibility summary uses the inclusive 1,000 ONE threshold and identifies every category file', problems.join('; '),
          problems.length ? "The pipeline's eligibility summary disagrees with the bundled category files." : '');
      }
    }

    async checkAirdropRun() {
      const manifests = (this.parsedJson.airdrop_manifest || []);
      const distribution = this.roles.airdrop_distribution.files;
      const title = 'Airdrop Merkle root, list hash, and totals recompute';
      if (!manifests.length && !distribution.length) {
        this.bundle('airdrop.run', 'airdrop', NOT_VERIFIED, title, 'no airdrop manifest in bundle', 'No airdrop run is part of this bundle.', false);
        return;
      }
      if (!manifests.length || !distribution.length) {
        this.bundle('airdrop.run', 'airdrop', FAIL, title, 'an airdrop run needs both airdrop_manifest and airdrop_distribution', 'A partial airdrop run cannot be verified.');
        return;
      }
      const [entry, manifest] = manifests[0];
      const runDir = entry.path.includes('/') ? entry.path.slice(0, entry.path.lastIndexOf('/') + 1) : '';
      const problems = [];
      let message = '';
      if (distribution[0].path !== `${runDir}distribution.csv`) problems.push('airdrop_distribution must be distribution.csv next to the airdrop manifest');
      try {
        this.progress('recomputing airdrop Merkle root');
        const rows = [];
        let header = null;
        let line = 1;
        for await (const record of csvRecords(this.fs, `${runDir}distribution.csv`, { strict: false, keepBlank: true })) {
          if (header === null) {
            header = record;
            if (JSON.stringify(header) !== JSON.stringify(['address', 'amount'])) throw new Error(`${runDir}distribution.csv: unexpected header ${JSON.stringify(header)}`);
            continue;
          }
          line += 1;
          if (record.length < 2) throw new Error('IndexError: list index out of range');
          rows.push({ address: record[0], amount: pyInt(record[1]), source: `${runDir}distribution.csv:${line}` });
        }
        if (header === null) throw new Error('StopIteration: distribution.csv is empty');
        if (manifest.format !== 'committed-batch-airdrop/v1') throw new Error(`unexpected format ${repr(manifest.format)}`);
        const seen = new Set();
        for (const row of rows) {
          if (row.address !== toChecksum(row.address)) problems.push(`${row.source}: address is not in checksum form`);
          if (seen.has(row.address.toLowerCase())) problems.push(`${row.source}: duplicate address ${row.address}`);
          seen.add(row.address.toLowerCase());
          if (row.amount <= 0n) problems.push(`${row.source}: non-positive amount`);
        }
        const batchSizeBig = pyInt(manifest.batch_size);
        if (batchSizeBig < 1n) throw new Error(`invalid batch_size ${batchSizeBig}`);
        const batchSize = Number(batchSizeBig);
        const batches = [];
        for (let i = 0; i < rows.length; i += batchSize) batches.push(rows.slice(i, i + batchSize));
        const leaves = batches.map((batch, index) => batchLeaf(index, batch.map((row) => row.address), batch.map((row) => row.amount)));
        const levels = treeLevels(leaves);
        const root = '0x' + toHex(levels[levels.length - 1][0]);
        const total = rows.reduce((sum, row) => sum + row.amount, 0n);
        const listSha256 = '0x' + this.fileHashes[`${runDir}distribution.csv`];
        if (BigInt(batches.length) !== pyInt(manifest.batch_count)) problems.push(`batch_count: manifest says ${manifest.batch_count}, recomputed ${batches.length}`);
        if (BigInt(rows.length) !== pyInt(manifest.recipient_count)) problems.push(`recipient_count: manifest says ${manifest.recipient_count}, recomputed ${rows.length}`);
        if (String(total) !== String(manifest.total_amount)) problems.push(`total_amount: manifest says ${manifest.total_amount}, recomputed ${total}`);
        if (root !== manifest.root) problems.push(`root: manifest says ${manifest.root}, recomputed ${root}`);
        if (listSha256 !== manifest.list_sha256) problems.push(`list_sha256: manifest says ${manifest.list_sha256}, file is ${listSha256}`);
        const available = new Set(this.allFiles);
        const declaredBatches = (this.jsonDocs.airdrop_batch || []).map((item) => item.path).sort();
        const expectedBatches = batches.map((_batch, index) => `${runDir}batches/batch-${String(index).padStart(5, '0')}.json`);
        if (JSON.stringify(expectedBatches.slice().sort()) !== JSON.stringify(declaredBatches)) problems.push("declared airdrop_batch files do not match the run's batch count");
        for (let index = 0; index < batches.length; index++) {
          const batch = batches[index];
          const path = expectedBatches[index];
          if (!available.has(path)) { problems.push(`batch ${index}: [Errno 2] No such file or directory: ${path}`); continue; }
          const payload = JSON.parse(new TextDecoder('utf-8').decode(await this.fs.readBytes(path)));
          if (!isObject(payload) || !(pyEq(payload.batch_index, index))) {
            problems.push(`batch ${index}: batch_index is ${isObject(payload) ? payload.batch_index : undefined}, expected ${index}`);
            continue;
          }
          if (JSON.stringify(payload.recipients) !== JSON.stringify(batch.map((row) => row.address))) problems.push(`batch ${index}: recipients differ from distribution.csv`);
          const amounts = payload.amounts.map(pyInt);
          if (amounts.length !== batch.length || amounts.some((amount, i) => amount !== batch[i].amount)) problems.push(`batch ${index}: amounts differ from distribution.csv`);
          if (payload.leaf !== '0x' + toHex(leaves[index])) problems.push(`batch ${index}: leaf differs`);
          const proof = treeProof(levels, index);
          const given = payload.proof.map((item) => unhexStrict(item));
          if (given.length !== proof.length || !given.every((item, i) => sameBytes(item, proof[i]))) problems.push(`batch ${index}: proof differs from recomputed proof`);
        }
        message = `root ${root}, list_sha256 ${listSha256}, ${rows.length} recipients, total ${total}`;
      } catch (error) {
        problems.push(error.message);
      }
      this.bundle('airdrop.run', entry.path, problems.length ? FAIL : PASS, title, problems.length ? problems.join('; ') : message,
        problems.length ? 'The airdrop manifest does not describe the bundled distribution list.' : '');
    }

    // -- historical chain evidence ---------------------------------------------

    evidenceProblems(doc) {
      const problems = [];
      if (doc.format !== this.rules.evidence_format) problems.push(`format must be ${this.rules.evidence_format}`);
      if (!(typeof doc.source_id === 'string' && doc.source_id in this.sources)) problems.push(`source_id ${repr(doc.source_id)} is not declared in the manifest`);
      const retrieved = doc.retrieved_utc;
      if (typeof retrieved !== 'string' || !retrieved || !this.typeOk('utc_or_empty', retrieved)) problems.push('retrieved_utc must be YYYY-MM-DDTHH:MM:SSZ');
      const headers = doc.block_headers === undefined ? [] : doc.block_headers;
      const accounts = doc.accounts === undefined ? [] : doc.accounts;
      if (!Array.isArray(headers) || !Array.isArray(accounts)) return problems.concat(['block_headers and accounts must be lists']);
      if (!headers.length && !accounts.length) problems.push('evidence has neither block_headers nor accounts');
      const hashRe = /^0x[0-9a-fA-F]{64}$/;
      headers.forEach((header, index) => {
        const where = `block_headers[${index}]`;
        if (!isObject(header)) { problems.push(`${where} must be an object`); return; }
        if (!(jsonInt(header.shard) && (header.shard === 0 || header.shard === 1))) problems.push(`${where}.shard must be 0 or 1`);
        for (const field of ['number', 'timestamp']) if (!jsonInt(header[field])) problems.push(`${where}.${field} must be a non-negative integer`);
        for (const field of ['hash', 'parent_hash', 'state_root', 'transactions_root']) {
          if (typeof header[field] !== 'string' || !hashRe.test(header[field])) problems.push(`${where}.${field} must be a 0x-prefixed 32-byte hash`);
        }
      });
      accounts.forEach((account, index) => {
        const where = `accounts[${index}]`;
        if (!isObject(account)) { problems.push(`${where} must be an object`); return; }
        if (!(jsonInt(account.shard) && (account.shard === 0 || account.shard === 1))) problems.push(`${where}.shard must be 0 or 1`);
        if (!jsonInt(account.block)) problems.push(`${where}.block must be an integer`);
        if (typeof account.address !== 'string' || !this.typeOk('address', account.address)) problems.push(`${where}.address is not an address`);
        for (const field of ACCOUNT_EVIDENCE_FIELDS) {
          if (field in account && (typeof account[field] !== 'string' || !this.typeOk('uint', account[field]))) problems.push(`${where}.${field} must be a canonical unsigned integer string`);
        }
        if (!ACCOUNT_EVIDENCE_FIELDS.some((field) => field in account)) problems.push(`${where} has no balance or claim component`);
      });
      return problems;
    }

    checkChain(sample) {
      const kinds = this.rules.source_kinds;
      const pinned = this.snapshot.cutoff;
      const docs = this.parsedJson.historical_evidence || [];
      const parsedPaths = new Set(docs.map((item) => item[0].path));
      const malformed = (this.jsonDocs.historical_evidence || []).map((entry) => entry.path).filter((path) => !parsedPaths.has(path));
      const headers = { 0: [], 1: [] };
      const accounts = [];
      for (const [entry, doc] of docs) {
        const problems = this.evidenceProblems(doc);
        if (problems.length) {
          this.bundle('chain.evidence_schema', entry.path, FAIL, 'Archived evidence file is well formed', problems.slice(0, MAX_EXAMPLES).join('; '), 'Malformed archived evidence is rejected, never skipped silently.');
          malformed.push(entry.path);
          continue;
        }
        this.bundle('chain.evidence_schema', entry.path, PASS, 'Archived evidence file is well formed');
        const kind = (this.sources[doc.source_id] || {}).kind || '';
        for (const header of doc.block_headers || []) headers[header.shard].push([doc.source_id, kind, header]);
        for (const account of doc.accounts || []) accounts.push([doc.source_id, kind, account]);
      }
      const validDocs = docs.length;
      if (!validDocs && !malformed.length) {
        this.chain('chain.headers', 'cutoff', NOT_VERIFIED, 'Cutoff block headers are corroborated by independent archived sources',
          'no archived historical evidence in bundle',
          "Without archived headers, state proofs, or database-derived evidence, the bundle's agreement with the chain is asserted, not independently verified.",
          this.requireChainTruth);
      }
      for (const shard of [0, 1]) {
        const expected = pinned[`shard${shard}`];
        const expectedValues = { number: expected.block, hash: expected.hash, state_root: expected.state_root, timestamp: utcToUnix(expected.timestamp_utc) };
        const agreeing = [];
        for (const [sourceId, kind, header] of headers[shard]) {
          const differ = [];
          for (const [field, wanted] of Object.entries(expectedValues)) {
            let value = header[field];
            if (typeof value === 'string') value = value.toLowerCase();
            if (value !== wanted) differ.push(`${field}: ${repr(header[field])} vs pinned ${repr(wanted)}`);
          }
          const subject = `${sourceId} shard${shard}`;
          const title = `Source header matches the pinned shard-${shard} cutoff block`;
          if (differ.length) {
            const stale = (kinds[kind] || { stale_prone: true }).stale_prone;
            this.chain('chain.header', subject, stale ? WARNING : FAIL, title, differ.join('; '),
              stale ? 'Stale or inconsistent RPC/explorer data: recorded as an infrastructure warning, not as verification.' : 'An independent archived source contradicts the published cutoff block.', !stale);
          } else {
            this.chain('chain.header', subject, PASS, title, `${kind} source agrees on number, hash, state root, and timestamp`);
            agreeing.push([sourceId, kind, header]);
          }
        }
        for (const field of ['parent_hash', 'transactions_root']) {
          const values = new Map();
          for (const [sourceId, kind, header] of agreeing) {
            const key = header[field].toLowerCase();
            if (!values.has(key)) values.set(key, []);
            values.get(key).push([sourceId, kind]);
          }
          if (values.size > 1) {
            const independent = [...values.entries()].filter(([, owners]) => owners.some(([, kind]) => (kinds[kind] || {}).independent)).length;
            const detail = [...values.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1))
              .map(([value, owners]) => `${value}: ` + owners.map(([source]) => source).join(', ')).join('; ');
            this.chain('chain.source_agreement', `shard${shard} ${field}`, independent > 1 ? FAIL : WARNING,
              `Independent sources agree on shard-${shard} ${field}`, detail,
              'Sources disagree about the cutoff block; the disagreement is reported, not resolved.', independent > 1);
          }
        }
        const independentSources = new Set(agreeing.filter(([, kind]) => (kinds[kind] || {}).independent).map(([source]) => source));
        const distinct = new Set(agreeing.map(([source]) => source));
        const corroborated = distinct.size >= 2 && independentSources.size >= 1;
        if (validDocs) {
          this.chain('chain.headers', `shard${shard}`, corroborated ? PASS : NOT_VERIFIED,
            `Shard-${shard} cutoff header is corroborated by at least two sources, one of them independent of RPC/explorer infrastructure`,
            `${distinct.size} agreeing source(s), ${independentSources.size} independent`,
            corroborated ? '' : 'Not enough independent archived evidence to call the cutoff header verified.', this.requireChainTruth);
        }
      }
      const byAddress = {};
      for (const [sourceId, kind, account] of accounts) (byAddress[account.address.toLowerCase()] = byAddress[account.address.toLowerCase()] || []).push([sourceId, kind, account]);
      for (const item of sample) this.checkRowChain(item.identifier, item.row, byAddress[item.row.address.toLowerCase()] || []);
    }

    checkRowChain(identifier, row, records) {
      const kinds = this.rules.source_kinds;
      const pinnedBlocks = { 0: this.snapshot.cutoff.shard0.block, 1: this.snapshot.cutoff.shard1.block };
      const evidenced = {};
      const problems = [];
      const warnings = [];
      const excluded = this.engine.constants.WONE_EXCLUDED_ADDRESSES || this.engine.constants.WONE_REQUIRED_EXCLUSIONS;
      const woneExcluded = excluded.includes((row.address || '').toLowerCase());
      for (const [sourceId, kind, account] of records) {
        if (account.block !== pinnedBlocks[account.shard]) {
          warnings.push(`${sourceId}: shard${account.shard} record is for block ${account.block}, not the cutoff`);
          continue;
        }
        const independent = (kinds[kind] || {}).independent || false;
        for (const [field, component] of accountComponents(account.shard)) {
          if (!(field in account)) continue;
          const expected = row[component] === undefined ? '' : row[component];
          if (component === 'wone_balance_atto' && woneExcluded) {
            // The claim zeroes an excluded holder's WONE by policy; the chain
            // balance is not expected to match, only to exist.
            if (independent) evidenced[component] = `${sourceId} (excluded holder)`;
            continue;
          }
          if (account[field] === expected) {
            if (independent) evidenced[component] = sourceId;
          } else {
            const text = `${sourceId} (${kind}) ${component}: ${account[field]} vs claim ${expected}`;
            (independent ? problems : warnings).push(text);
          }
        }
      }
      const title = 'Sampled claim components are corroborated by independent archived chain evidence';
      const missing = CLAIM_COMPONENTS.filter((component) => !(component in evidenced));
      if (problems.length) this.chain('chain.row', identifier, FAIL, title, problems.join('; '), 'Independent archived evidence contradicts the claim row.');
      else if (warnings.length) {
        this.chain('chain.row', identifier, WARNING, title, warnings.join('; '),
          'Only stale or inconsistent RPC/explorer data disagrees; recorded as an infrastructure warning, not as verification.', false);
      }
      if (!problems.length && missing.length) {
        this.chain('chain.row', identifier, NOT_VERIFIED, title, 'no independent evidence for: ' + missing.join(', '),
          'The claim components were checked against the bundle only; the chain values were not independently evidenced.', this.requireChainTruth);
      } else if (!problems.length && !missing.length) {
        this.chain('chain.row', identifier, PASS, title, 'all components match: ' + CLAIM_COMPONENTS.map((c) => `${c}=${evidenced[c]}`).join(', '));
      }
    }

    // -- report -----------------------------------------------------------------

    chainTier() {
      const chainChecks = this.checks.filter((check) => check.scope === 'chain');
      if (chainChecks.some((check) => check.status === FAIL)) return FAIL;
      if (!chainChecks.length || chainChecks.some((check) => check.status === NOT_VERIFIED || check.status === WARNING)) return NOT_VERIFIED;
      return PASS;
    }

    buildReport(sample, population) {
      const rowChecks = this.rowResults.flatMap((result) => result.checks);
      const checks = this.checks.concat(rowChecks);
      const requested = !!this.sampleSize;
      const sampled = requested ? tierStatus(rowChecks, 'row') : 'NOT REQUESTED';
      const metadata = tierStatus(this.checks, 'bundle');
      const chain = this.chainTier();
      const counts = {};
      for (const check of checks) counts[check.status] = (counts[check.status] || 0) + 1;
      let exitCode;
      if (checks.some((check) => check.status === FAIL)) exitCode = EXIT.FAIL;
      else if ((requested && sampled !== PASS) || metadata !== PASS || (this.requireChainTruth && chain !== PASS) || (!requested && !this.verifyAllMetadata)) exitCode = EXIT.INCOMPLETE;
      else exitCode = EXIT.PASS;
      const totals = {};
      for (const key of Object.keys(this.totals).sort()) totals[key] = String(this.totals[key]);
      return {
        format: this.rules.report_format,
        engine: 'javascript',
        rules_sha256: this.rulesSha256,
        bundle: {
          path: this.fs.name || '',
          bundle_id: this.manifest.bundle_id,
          manifest_sha256: this.manifestSha256,
          cutoff: this.manifest.cutoff,
          network: this.manifest.network,
          chain_ids: this.manifest.chain_ids,
          threshold_atto: this.manifest.threshold_atto,
          airdrop_scope: this.manifest.airdrop_scope || 'partial',
          files: this.manifest.files.filter(isObject).map((entry) => ({
            role: entry.role, category: entry.category || '', path: entry.path, declared_sha256: entry.sha256,
            actual_sha256: this.fileHashes[entry.path] || '', rows: entry.rows, source: entry.source,
          })),
        },
        sample: {
          algorithm: this.rules.sample_algorithm.name,
          seed: this.seed,
          seed_generated: this.seedGenerated,
          sample_by: this.sampleBy,
          requested: this.sampleSize || 0,
          population,
          identifiers: sample.map((item) => item.identifier),
          reproduce: `python3 toolkit/scripts/random-sample-verify.py --bundle <bundle> --sample-size ${this.sampleSize || 0} --seed ${this.seed}`
            + (this.sampleBy !== 'secure_key' ? ` --sample-by ${this.sampleBy}` : ''),
        },
        tiers: { sampled_rows: sampled, bundle_metadata: metadata, chain_truth: chain },
        verify_all_metadata: this.verifyAllMetadata,
        totals,
        rows: this.rowResults,
        show_row: this.showResult,
        checks,
        counts,
        exit_code: exitCode,
      };
    }
  }

  class SortedKeyCursor {
    constructor(fs, path) { this.iterator = csvRecords(fs, path)[Symbol.asyncIterator](); this.index = null; this.current = null; }
    async init() {
      const first = await this.iterator.next();
      const header = first.done ? [] : first.value;
      this.index = header.includes('secure_key') ? header.indexOf('secure_key') : null;
      await this.advance();
    }
    async advance() {
      this.current = null;
      if (this.index === null) return;
      for (;;) {
        const next = await this.iterator.next();
        if (next.done) return;
        if (next.value.length > this.index) { this.current = next.value[this.index].toLowerCase(); return; }
      }
    }
    async contains(key) {
      while (this.current !== null && this.current < key) await this.advance();
      return this.current === key;
    }
  }

  async function verifyBundle(fs, options) {
    return new Verifier(fs, options).run();
  }

  return {
    PASS, FAIL, NOT_VERIFIED, WARNING, EXIT, MANIFEST_NAME, CHUNK,
    BundleError, Sha256, keccak256, toHex, toChecksum, fixed18, usd, sha256Hex,
    parseJsonStrict, CsvParser, Engine, RowContext, Sampler, drawSample, generateSeed,
    batchLeaf, treeLevels, hexToBech32, Verifier, verifyBundle,
  };
});
