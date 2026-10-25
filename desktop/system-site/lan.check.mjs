// Jarvis OS «дома» listener (M7, review 17.10): open only on the network where it was turned on.
// Run: node lan.check.mjs (a temporary state file, loopback addresses, a fake network id).
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Lan } from './remote.mjs';

const dir = mkdtempSync(join(tmpdir(), 'jarvis-lan-'));
const file = join(dir, 'os-remote.json');
let net = 'home';
let addr = '127.0.0.1';
const opts = { network: async () => net, address: () => addr, everyMs: 3_600_000 };
const handler = (_req, res) => res.end('ok');
let passed = 0;
const check = async (name, fn) => { await fn(); passed += 1; console.log('✓', name); };

try {
  const lan = new Lan(handler, 0, file, opts);
  await check('turned on at home: listening, the network remembered', async () => {
    const s = await lan.on();
    assert.equal(s.on, true);
    assert.equal(JSON.parse(readFileSync(file, 'utf8')).home, 'home');
  });
  await check('another network: the door closes by itself, still wanted', async () => {
    net = 'cafe';
    const s = await lan.check();
    assert.deepEqual([s.on, s.away], [false, true]);
    assert.equal(JSON.parse(readFileSync(file, 'utf8')).lan, true);
  });
  await check('back home: open again', async () => {
    net = 'home';
    assert.equal((await lan.check()).on, true);
  });
  await check('a new address from DHCP: listening on it', async () => {
    addr = '::1';
    const s = await lan.check();
    assert.deepEqual([s.on, s.address], [true, '::1']);
  });
  await lan.stop();
  await check('a restart away from home does not open it', async () => {
    net = 'cafe';
    const again = new Lan(handler, 0, file, opts);
    await again.restore();
    assert.deepEqual([again.status().on, again.status().away], [false, true]);
    await again.stop();
  });
  await check('turned on by hand elsewhere: that network becomes home', async () => {
    const other = new Lan(handler, 0, file, opts);
    assert.equal((await other.on()).on, true);
    assert.equal(JSON.parse(readFileSync(file, 'utf8')).home, 'cafe');
    await other.off();
    assert.equal(JSON.parse(readFileSync(file, 'utf8')).lan, false);
    await other.stop();
  });
  await check('fail closed: a state from before the network was remembered is turned off', async () => {
    writeFileSync(file, JSON.stringify({ lan: true }));
    const legacy = new Lan(handler, 0, file, opts);
    await legacy.restore();
    assert.deepEqual([legacy.status().on, legacy.status().away], [false, false]);
    assert.equal(JSON.parse(readFileSync(file, 'utf8')).lan, false);
    await legacy.stop();
  });
  await check('fail closed: an unknown network is not home, and cannot be made home', async () => {
    net = 'home';
    const lan2 = new Lan(handler, 0, file, opts);
    await lan2.on();
    net = null;
    assert.deepEqual([(await lan2.check()).on, lan2.status().away], [false, true]);
    await lan2.off();
    await assert.rejects(() => lan2.on(), /домашнюю сеть/);
    assert.equal(lan2.status().on, false);
    await lan2.stop();
  });
  console.log(`lan.check.mjs: ${passed} checks passed`);
} finally {
  rmSync(dir, { recursive: true, force: true });
}
