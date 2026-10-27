// Jarvis OS — a check of the page server's door (run: `node desktop/system-site/check.mjs`; exit code 0 = all good).
// Starts server.mjs in fixture mode with a throwaway config (a test token, never a real one) in a temporary folder:
// the key matches Python's (tests/unit/test_system_site_key.py has the same vector), no session → 401, a session → 200,
// a cross-origin or non-JSON «Спросить» → 403, more than 10 a minute → 429, sessions survive a restart and end with a
// new token.

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import http from 'node:http';
import net from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { Pairing, Sessions, siteKey } from './auth.mjs';

// The same vector as tests/unit/test_system_site_key.py.
const TOKEN = 'jarvis-test-token-0123456789abcdef';
const KEY = '97af05405566fe0a1b7685f4a1aa8cc5'; // gitleaks:allow (derived from TOKEN above)

const HERE = dirname(fileURLToPath(import.meta.url));
let passed = 0;
const ok = (condition, what) => {
  assert.ok(condition, what);
  passed += 1;
};

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

function request(port, path, { method = 'GET', headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: '127.0.0.1', port, path, method, headers: { host: `127.0.0.1:${port}`, ...headers } },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString() }));
      },
    );
    req.on('error', reject);
    if (body !== undefined) req.write(body);
    req.end();
  });
}

async function start(port, env) {
  const child = spawn(process.execPath, [join(HERE, 'server.mjs')], {
    env: { ...process.env, ...env, PORT: String(port) },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  for (let i = 0; i < 50; i += 1) {
    try {
      await request(port, '/style.css');
      return child;
    } catch {
      await new Promise((r) => setTimeout(r, 100));
    }
  }
  child.kill();
  throw new Error('server.mjs did not start');
}

async function main() {
  ok(siteKey(TOKEN) === KEY, 'the key derivation matches Python');

  const dir = await mkdtemp(join(tmpdir(), 'jarvis-os-check-'));
  const config = join(dir, 'config.json');
  const sessionsFile = join(dir, 'sessions.json');
  const fixture = join(dir, 'fixture.json');
  await writeFile(config, JSON.stringify({ token: TOKEN, base_url: 'http://127.0.0.1:9' }));
  await writeFile(
    fixture,
    JSON.stringify({ graph: { types: [], nodes: [], links: [], hubs: [], link_kinds: {} }, live: { nodes: {} } }),
  );
  // a fake LMF for TLT inside Jarvis OS: it answers with what it was sent
  const LMF_TOKEN = 'lmf-test-token-0123456789abcdef';
  await mkdir(join(dir, 'tlt', 'app', 'data'), { recursive: true });
  await writeFile(join(dir, 'tlt', 'app', 'data', 'api-token'), LMF_TOKEN);
  const lmfPort = await freePort();
  const lmf = http.createServer((q, s) => {
    const chunks = [];
    q.on('data', (d) => chunks.push(d));
    q.on('end', () => {
      if (q.url === '/') {
        s.writeHead(200, { 'content-type': 'text/html', 'set-cookie': 'lmf=1' });
        return s.end('<!doctype html><html><head><title>LMF</title></head><body><div id="app">LMF</div></body></html>');
      }
      s.writeHead(200, { 'content-type': 'application/json', 'set-cookie': 'lmf=1' });
      s.end(JSON.stringify({ method: q.method, url: q.url, headers: q.headers, body: Buffer.concat(chunks).toString() }));
    });
  });
  await new Promise((r) => lmf.listen(lmfPort, '127.0.0.1', r));
  const env = {
    JARVIS_DESKTOP_CONFIG: config, JARVIS_SYSTEM_SESSIONS: sessionsFile, JARVIS_SYSTEM_FIXTURE: fixture,
    JARVIS_TLT_DIR: join(dir, 'tlt'), JARVIS_TLT_PORT: String(lmfPort),
    JARVIS_OS_HARDWARE_ID: 'MAC-A', // not ioreg: the checks also run on Linux CI, where it does not exist
  }; // prettier-ignore
  const port = await freePort();
  let server = await start(port, env);
  const own = `http://127.0.0.1:${port}`;
  const json = { 'content-type': 'application/json' };
  const ask = (headers, body = '{"text":"что у меня завтра?"}') =>
    request(port, '/api/system/ask', { method: 'POST', headers, body });

  try {
    // no session: the page and the data are closed, the code is open
    let r = await request(port, '/');
    ok(r.status === 401 && r.body.includes('Откройте ссылку из Джарвиса'), 'page without a session → 401');
    ok((await request(port, '/api/system/graph')).status === 401, 'graph without a session → 401');
    ok((await request(port, '/api/system/live')).status === 401, 'live without a session → 401');
    ok((await request(port, '/api/lab/screen')).status === 401, 'the lab screen (wallpaper + Dock) without a session → 401');
    ok((await request(port, '/app.js')).status === 200, 'static code is served without a session');
    ok((await request(port, `/?key=${KEY.slice(0, -1)}0`)).status === 401, 'a wrong key → 401');
    ok((await request(port, '/?key=')).status === 401, 'an empty key → 401');

    // the personal link
    r = await request(port, `/?key=${KEY}`);
    const cookie = String(r.headers['set-cookie'] || '');
    ok(r.status === 200 && r.body.includes('url=/'), 'the link answers with a step to / without the key');
    ok(/HttpOnly/.test(cookie) && /SameSite=Strict/.test(cookie) && /Path=\//.test(cookie), 'cookie flags');
    ok(/Max-Age=31536000/.test(cookie) && !/Secure/.test(cookie), 'a year (renewed while used); not Secure over http');
    const session = cookie.split(';')[0];
    const secure = await request(port, `/?key=${KEY}`, { headers: { 'x-forwarded-proto': 'https' } });
    ok(/; Secure/.test(String(secure.headers['set-cookie'])), 'Secure behind https');
    const stored = await readFile(sessionsFile, 'utf8');
    ok(((await stat(sessionsFile)).mode & 0o777) === 0o600, 'sessions file is 0600');
    ok(!stored.includes(session.split('=')[1]) && !stored.includes(TOKEN), 'sessions are stored hashed, no token');

    // with the session
    r = await request(port, '/', { headers: { cookie: session } });
    ok(r.status === 200 && r.body.includes('id="graph"'), 'page with a session → 200');
    ok((await request(port, '/api/system/graph', { headers: { cookie: session } })).status === 200, 'graph → 200');
    ok([200, 404].includes((await request(port, '/api/lab/screen', { headers: { cookie: session } })).status), 'lab screen → 200 / 404');
    ok((await request(port, '/', { headers: { cookie: 'jarvis_os_session=forged' } })).status === 401, 'forged → 401');

    // «Проверка iPhone»: the page and the guide with a session; the check itself only by a JSON button press from the
    // page (a request without it is refused before any tool runs — no phone is touched by this check)
    ok((await request(port, '/iphone')).status === 401, 'the iPhone page without a session → 401');
    ok((await request(port, '/crm')).status === 401, 'the CRM jump without a session → 401');
    r = await request(port, '/iphone', { headers: { cookie: session } });
    ok(r.status === 200 && r.body.includes('Проверить iPhone'), 'the iPhone page with a session → 200');
    r = await request(port, '/api/os/device/iphone/guide', { headers: { cookie: session } });
    ok(r.status === 200 && JSON.parse(r.body).manualChecklist.length >= 4, 'the manual checklist and the limits');
    ok((await request(port, '/api/os/device/iphone', { method: 'POST' })).status === 401, 'the iPhone check without a session → 401');
    r = await request(port, '/api/os/device/iphone', { method: 'POST', headers: { cookie: session, 'content-type': 'text/plain' }, body: '{}' });
    ok(r.status === 403, 'the iPhone check not from the page → 403');

    // «Спросить»
    ok((await ask({ ...json, origin: own })).status === 401, 'ask without a session → 401');
    ok((await ask({ ...json, cookie: session, origin: own })).status === 200, 'ask from the page → 200');
    ok((await ask({ ...json, cookie: session, origin: 'http://evil.example' })).status === 403, 'cross-origin → 403');
    ok(
      (await ask({ 'content-type': 'text/plain', cookie: session, origin: own })).status === 403,
      'text/plain → 403',
    );
    ok((await ask({ ...json, cookie: session })).status === 403, 'no Origin, no Sec-Fetch-Site → 403');
    ok(
      (await ask({ ...json, cookie: session, origin: own, 'sec-fetch-site': 'cross-site' })).status === 403,
      'Sec-Fetch-Site cross-site → 403',
    );
    ok(
      (await ask({ ...json, cookie: session, 'sec-fetch-site': 'same-origin' })).status === 200,
      'no Origin but Sec-Fetch-Site same-origin → 200',
    );
    ok(
      (await ask({ ...json, cookie: session, origin: 'http://192.168.1.5:5190', host: '192.168.1.5:5190' })).status ===
        403,
      'from the network → 403',
    );
    ok((await ask({ ...json, cookie: session, origin: own }, `{"text":"${'я'.repeat(3000)}"}`)).status === 413, '> 4 KB');
    const statuses = [];
    for (let i = 0; i < 9; i += 1) statuses.push((await ask({ ...json, cookie: session, origin: own })).status);
    ok(statuses.slice(0, 8).every((s) => s === 200) && statuses[8] === 429, `10 a minute, then 429 (${statuses})`);

    // 17.10 — the personal link only on this Mac; a domain only when it is the open tunnel; pairing; the phone controls
    ok((await request(port, `/?key=${KEY}`, { headers: { host: `192.168.1.5:${port}` } })).status === 403, 'the personal link from the network → 403');
    ok((await request(port, '/', { headers: { host: 'some.trycloudflare.com', cookie: session } })).status === 403, 'a domain that is not the open tunnel → 403');
    r = await request(port, '/pair');
    ok(r.status === 200 && r.body.includes('Привязать'), 'the pairing page needs no session');
    r = await request(port, '/api/pair', { method: 'POST', headers: { ...json, origin: own }, body: '{"code":"guess"}' });
    ok(r.status === 403, 'a wrong pairing code → 403');
    ok((await request(port, '/api/os/remote', { headers: { cookie: session } })).status === 200, 'the phone controls on this Mac');
    ok((await request(port, '/api/os/remote', { headers: { cookie: session, host: `192.168.1.5:${port}` } })).status === 403, 'the phone controls from the network → 403');
    r = await request(port, '/api/os/remote/pair', { method: 'POST', headers: { ...json, cookie: session, origin: own }, body: '{"via":"tunnel"}' });
    ok(r.status === 409, 'no QR while the tunnel is closed');
    for (const page of ['/devices', '/sites', '/iphone']) {
      ok((await request(port, page, { headers: { cookie: session, host: `192.168.1.5:${port}` } })).status === 403, `${page} from the network → 403`);
    }
    ok((await request(port, '/devices', { headers: { cookie: session } })).status === 200, 'the «Телефон» page on this Mac');

    // sessions: a paired phone, the yearly renewal, unpairing, the old file format; one-time codes
    const devFile = join(dir, 'devices.json');
    const store = new Sessions(devFile);
    const phone = await store.create('k', { kind: 'device', name: 'iPhone', via: 'lan' });
    ok((await store.devices()).length === 1 && (await store.devices())[0].name === 'iPhone', 'a paired phone is listed');
    const aged = JSON.parse(await readFile(devFile, 'utf8'));
    for (const rec of Object.values(aged.sessions)) Object.assign(rec, { seen: rec.seen - 2 * 86400000, exp: rec.exp - 2 * 86400000 });
    await writeFile(devFile, JSON.stringify(aged));
    const used = await new Sessions(devFile).valid(phone, 'k');
    ok(used && used.renewed && used.exp > Date.now() + 364 * 86400000, 'a used session is renewed a year ahead');
    const again = new Sessions(devFile);
    ok(await again.revoke((await again.devices())[0].id), 'unpair');
    ok(!(await again.valid(phone, 'k')), 'an unpaired phone is out');
    const legacy = JSON.parse(await readFile(devFile, 'utf8'));
    const old = new Sessions(join(dir, 'old.json'));
    const oldId = await old.create('k');
    const oldData = JSON.parse(await readFile(join(dir, 'old.json'), 'utf8'));
    for (const h of Object.keys(oldData.sessions)) oldData.sessions[h] = Date.now() + 86400000; // the 16.10 format: a number
    await writeFile(join(dir, 'old.json'), JSON.stringify({ ...oldData, key: legacy.key }));
    ok(Boolean(await new Sessions(join(dir, 'old.json')).valid(oldId, 'k')), 'sessions in the old format still work');
    const pairs = new Pairing();
    const { code } = pairs.start('lan');
    ok(pairs.consume('x') === null && pairs.consume(code) === 'lan' && pairs.consume(code) === null, 'a pairing code works once');

    // TLT inside Jarvis OS: LMF's token added on the server, nothing crosses over, changes only from this page
    ok((await request(port, '/tlt/')).status === 401, 'TLT without a session → 401');
    ok((await request(port, '/tlt/api/day')).status === 401, 'TLT data without a session → 401');
    r = await request(port, '/tlt/', { headers: { cookie: session } });
    ok(r.status === 200 && r.body.includes('class="os-bar"') && r.body.includes('href="/sites"'), 'TLT with the Jarvis OS menu');
    ok(!String(r.headers['set-cookie'] || '').includes('lmf='), "LMF's cookies are not passed on");
    r = await request(port, '/tlt/api/day/2026-10-17?x=1', { headers: { cookie: session, origin: own } });
    let seen = JSON.parse(r.body);
    ok(seen.headers.authorization === `Bearer ${LMF_TOKEN}`, "LMF's token is added by the server");
    ok(!seen.headers.cookie && !seen.headers.origin && seen.headers.host === `127.0.0.1:${lmfPort}`, 'no cookie or Origin goes to LMF');
    ok(seen.url === '/api/day/2026-10-17?x=1', 'the path under /tlt/ with its query');
    ok(!r.body.includes(LMF_TOKEN) || seen.headers.authorization, 'the token only travels to LMF');
    r = await request(port, '/tlt/api/day/2026-10-17', { method: 'PUT', headers: { ...json, cookie: session, origin: 'http://evil.example' }, body: '{}' });
    ok(r.status === 403, 'a change from another site → 403');
    r = await request(port, '/tlt/api/day/2026-10-17', { method: 'PUT', headers: { ...json, cookie: session } , body: '{}' });
    ok(r.status === 403, 'a change with no Origin and no Sec-Fetch-Site → 403');
    r = await request(port, '/tlt/api/day/2026-10-17', { method: 'PUT', headers: { ...json, cookie: session, origin: own }, body: '{"score":8}' });
    seen = JSON.parse(r.body);
    ok(r.status === 200 && seen.method === 'PUT' && seen.body === '{"score":8}', 'a change from this page reaches LMF');
    const big = 'x'.repeat(1_200_000);
    r = await request(port, '/tlt/api/import', { method: 'POST', headers: { ...json, cookie: session, origin: own, 'content-length': String(big.length) }, body: big })
      .catch((err) => ({ status: `error: ${err.message}` }));
    ok(r.status === 413, `a body over 1.1 MB → 413 (${r.status})`);
    r = await request(port, '/tlt/', { headers: { cookie: session, host: `192.168.1.5:${port}` } });
    ok(r.status === 200 && !r.body.includes('href="/sites"'), 'a phone gets TLT, without the Mac-only menu items');
    r = await request(port, '/tlt', { headers: { cookie: session } });
    ok(r.status === 302 && r.headers.location === '/tlt/', '/tlt → /tlt/');

    // a restart keeps sessions; a new token ends them (and a phone's session made meanwhile is seen after it)
    const phoneId = await new Sessions(sessionsFile).create(KEY, { kind: 'device', name: 'iPhone', via: 'lan' });
    const phoneCookie = `jarvis_os_session=${phoneId}`;
    server.kill();
    await new Promise((r) => setTimeout(r, 200));
    server = await start(port, env);
    ok((await request(port, '/', { headers: { cookie: session } })).status === 200, 'session survives a restart');
    // a phone's session is never «this Mac», even with a loopback Host (security review 17.10)
    r = await request(port, '/crm', { headers: { cookie: session } });
    ok(r.status === 302 && r.headers.location === 'https://crm.example.com/', 'the CRM menu item goes to the CRM on its server');
    r = await request(port, '/crm', { headers: { cookie: phoneCookie } });
    ok(r.status === 302 && r.headers.location === 'https://crm.example.com/', 'a phone may go to the CRM on its server (24.10)');
    for (const page of ['/devices', '/sites', '/iphone']) {
      ok((await request(port, page, { headers: { cookie: phoneCookie } })).status === 403, `${page} with a phone session → 403`);
    }
    ok((await request(port, '/api/os/remote', { headers: { cookie: phoneCookie } })).status === 403, 'the phone controls with a phone session → 403');
    r = await request(port, '/api/os/remote/tunnel', { method: 'POST', headers: { ...json, cookie: phoneCookie, origin: own }, body: '{"hours":1}' });
    ok(r.status === 403, 'a phone cannot open the tunnel');

    // R39: a refused button used to answer 403/429 and still run its action. A page on another port of 127.0.0.1 is
    // the same site, so the Strict cookie goes along: its text/plain «дома: включить» must change nothing.
    const lanNow = async () => JSON.parse((await request(port, '/api/os/remote', { headers: { cookie: session } })).body).lan;
    const lanBefore = JSON.stringify(await lanNow());
    r = await request(port, '/api/os/remote/lan', { method: 'POST', headers: { cookie: session, origin: 'http://127.0.0.1:5181', 'content-type': 'text/plain' }, body: '{"on":true}' });
    ok(r.status === 403, 'R39: «дома» from another port on 127.0.0.1 → 403');
    await new Promise((res2) => setTimeout(res2, 300)); // the old code went on after the 403
    ok(JSON.stringify(await lanNow()) === lanBefore, 'R39: …and «дома» stays as it was');
    r = await request(port, '/tlt/', { headers: { cookie: phoneCookie } });
    ok(r.status === 200 && !r.body.includes('href="/sites"'), 'a phone gets TLT without the Mac-only menu');
    ok((await request(port, '/', { headers: { cookie: phoneCookie } })).status === 200, 'a phone gets the map');
    await writeFile(config, JSON.stringify({ token: `${TOKEN}-rotated`, base_url: 'http://127.0.0.1:9' }));
    ok((await request(port, '/', { headers: { cookie: session } })).status === 401, 'a new token ends sessions');

    // bound to a Mac: the first start binds; another Mac sees only the door
    const hwEnv = { ...env, JARVIS_OS_DEVICE_FILE: join(dir, 'hw-test-device.json'), JARVIS_OS_HARDWARE_ID: 'MAC-A' };
    server.kill();
    await new Promise((res2) => setTimeout(res2, 200));
    server = await start(port, hwEnv);
    ok((await request(port, '/pair')).status === 200, 'the Mac it was bound to');
    ok(((await stat(join(dir, 'hw-test-device.json'))).mode & 0o777) === 0o600, 'the binding file is 0600');
    ok(!(await readFile(join(dir, 'hw-test-device.json'), 'utf8')).includes('MAC-A'), 'only a fingerprint of the hardware is kept');
    server.kill();
    await new Promise((res2) => setTimeout(res2, 200));
    server = await start(port, { ...hwEnv, JARVIS_OS_HARDWARE_ID: 'MAC-B' });
    r = await request(port, '/pair');
    ok(r.status === 423 && r.body.includes('привязан к другому Mac'), 'another Mac → only the door');
    ok((await request(port, '/api/system/graph')).status === 423, 'and no data');
  } finally {
    server.kill();
    lmf.close();
    await rm(dir, { recursive: true, force: true });
  }
  console.log(`check.mjs: ${passed} checks passed`);
}

main().catch((err) => {
  console.error(`check.mjs: FAILED — ${err.message}`);
  process.exit(1);
});
