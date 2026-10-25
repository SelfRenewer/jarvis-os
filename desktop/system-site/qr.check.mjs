// node qr.check.mjs — the pairing QR codes (qr.js) read back by macOS's own QR reader (CoreImage, through a small
// Swift script in a temp folder): every length from version 1 to 10 must decode to exactly the text it was made from.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { deflateSync } from 'node:zlib';
import { qrMatrix, qrSvg } from './qr.js';

/** A plain grayscale PNG of the matrix (8 px a module, a 4-module white border). */
function png(matrix, scale = 8) {
  const n = (matrix.length + 8) * scale;
  const rows = [];
  for (let y = 0; y < n; y += 1) {
    const row = Buffer.alloc(n + 1);
    for (let x = 0; x < n; x += 1) {
      const my = Math.floor(y / scale) - 4;
      const mx = Math.floor(x / scale) - 4;
      const on = my >= 0 && mx >= 0 && my < matrix.length && mx < matrix.length && matrix[my][mx];
      row[x + 1] = on ? 0 : 255;
    }
    rows.push(row);
  }
  const crcTable = Array.from({ length: 256 }, (_, k) => {
    let c = k;
    for (let i = 0; i < 8; i += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  const crc = (buf) => {
    let c = 0xffffffff;
    for (const b of buf) c = crcTable[(c ^ b) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };
  const chunk = (type, data) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(type), data]);
    const sum = Buffer.alloc(4);
    sum.writeUInt32BE(crc(body));
    return Buffer.concat([len, body, sum]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(n, 0);
  ihdr.writeUInt32BE(n, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 0; // grayscale
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(Buffer.concat(rows))),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

const DECODE = `
import CoreImage
import Foundation
let detector = CIDetector(ofType: CIDetectorTypeQRCode, context: nil, options: [CIDetectorAccuracy: CIDetectorAccuracyHigh])!
for path in CommandLine.arguments.dropFirst() {
  let image = CIImage(contentsOf: URL(fileURLWithPath: path))!
  let text = (detector.features(in: image).first as? CIQRCodeFeature)?.messageString ?? "<none>"
  print(Data(text.utf8).base64EncodedString())
}
`;

const texts = [
  'hi',
  'http://192.168.1.20:39110/pair#c=AbCdEf0123456789_-AbCdEf0123456789',
  'https://quiet-river-sample-word.trycloudflare.com/pair#c=AbCdEf0123456789_-AbCdEf0123456789xy',
  'Кириллица и знаки: «Джарвис» — 100%',
  'x'.repeat(120), // version 7: version bits
  'y'.repeat(150), // version 8: two block sizes
  'z'.repeat(205), // version 10: 16-bit length
];
const dir = mkdtempSync(join(tmpdir(), 'jarvis-qr-'));
try {
  const files = texts.map((text, i) => {
    const file = join(dir, `qr-${i}.png`);
    writeFileSync(file, png(qrMatrix(text)));
    return file;
  });
  writeFileSync(join(dir, 'decode.swift'), DECODE);
  const out = execFileSync('/usr/bin/swift', [join(dir, 'decode.swift'), ...files], { encoding: 'utf8', timeout: 120000 });
  const decoded = out.trim().split('\n').map((line) => Buffer.from(line, 'base64').toString('utf8'));
  texts.forEach((text, i) => assert.equal(decoded[i], text, `QR ${i} (${text.length} chars) reads back`));
  const sizes = texts.map((t) => qrMatrix(t).length);
  assert.deepEqual([sizes[0], sizes[4], sizes[6]], [21, 45, 57], 'versions 1, 7 and 10');
  assert.throws(() => qrMatrix('w'.repeat(300)), /too long/, 'longer than version 10 → an error, not a broken code');
  assert.ok(qrSvg('hi').startsWith('<svg') && qrSvg('hi').includes('<path d="M'), 'the SVG');
  console.log(`qr: ${texts.length} codes read back by CoreImage (versions ${[...new Set(sizes.map((s) => (s - 17) / 4))].join(', ')})`);
} finally {
  rmSync(dir, { recursive: true, force: true });
}
