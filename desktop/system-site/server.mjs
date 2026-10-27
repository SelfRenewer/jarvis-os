// Jarvis OS — the system page's tiny server (run by the Jarvis Mac app; no dependencies, node:http only).
//
// Serves the page and proxies exactly three calls to the local Jarvis API, adding the Mac app's bearer token:
//   GET  /api/system/graph → GET  {base}/api/desktop/v1/system/graph
//   GET  /api/system/live  → GET  {base}/api/desktop/v1/system/live
//   POST /api/system/ask   → POST {base}/api/desktop/v1/system/ask   (the command bar; it does not mark the Mac online)
// The token and base_url are read at request time from ~/Library/Application Support/Jarvis/config.json (written by
// `jarvis desktop-setup`); the token never reaches the browser and is never logged. Nothing else is proxied.
//
// Who gets in (auth.mjs, 17.10): this server runs only on the Mac it was first started on (hardware.mjs — another
// Mac sees just a door). Holding the token makes it a door into Jarvis, and Host / Origin / X-Forwarded-For are
// whatever a local process or a Docker container chooses to send, so the page and every /api/* call need a session:
//   · this Mac — the personal link /?key=<site key>, which the Jarvis Mac app opens by itself (only from loopback);
//   · a phone — a one-time QR from «Телефон» (/devices), over the home network (a second listener on this Mac's Wi-Fi
//     address, only while it is on) or a cloudflared quick tunnel (closed by itself); remote.mjs.
// Sessions live a year and renew themselves while used; they are kept server-side (hashed) in a 0600 file next to
// the Mac app's config. Only the static code is served without one — it holds no data. Pages and buttons that steer
// this Mac (/sites, /iphone, /devices, every button) answer only on this Mac, never to a phone.
// On top of the session, «Спросить» also needs: JSON, its own Origin (or, without Origin, Sec-Fetch-Site:
// same-origin), a body ≤ 4 KB, ≤ 10 questions a minute, and this Mac (Host and forwarded hops on loopback) unless
// JARVIS_SYSTEM_ASK_FROM_NETWORK=1.
//
// It also keeps TLT running (services.mjs) and shows it inside at /tlt/ with LMF's token added here (17.10, no
// password); /sites is the LocalSites panel (sites.mjs); /crm goes on to the CRM on its server; /iphone checks
// an iPhone on the cable (device-check.mjs). Jarvis in Docker wakes TLT through POST /api/os/services/tlt/ensure with
// `Authorization: Bearer <site key>` (no session; nothing else uses a bearer).
// The personal link may name the page to land on: /?key=…&to=/lab#explode (only the pages below).
//
// Listens on 127.0.0.1:$PORT (the Jarvis Mac app passes PORT; default 39110) — and on the Wi-Fi address only while
// «дома» is on (remote.mjs). JARVIS_OS_DEVICE_FILE / JARVIS_OS_HARDWARE_ID: the hardware binding for tests.
// JARVIS_DESKTOP_CONFIG=<file> reads another config.json; JARVIS_SYSTEM_SESSIONS=<file> keeps sessions elsewhere.
// JARVIS_SYSTEM_FIXTURE=<file.json> serves a saved map ({graph, live}) without Jarvis — for working on the page; it
// prints its own link (the key of a fixed demo token, or of JARVIS_DESKTOP_CONFIG's token when that is set).

import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import { isIP } from 'node:net';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { COOKIE, KEY_PARAM, Pairing, RateLimit, Sessions, readCookie, sameSecret, sessionCookie, siteKey } from './auth.mjs';
import { checkHardware } from './hardware.mjs';
import { Lan, TUNNEL_HOURS, Tunnel, lanAddress } from './remote.mjs';
import { Service, TLT_SPEC, portOpen } from './services.mjs';
import { SitesError, launchLocalSites, siteAction, sitesState } from './sites.mjs';
import { LIMITS, MANUAL_CHECKLIST, checkIphone } from './device-check.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT) || 39110;
const HOST = '127.0.0.1';
const CONFIG =
  process.env.JARVIS_DESKTOP_CONFIG || join(homedir(), 'Library', 'Application Support', 'Jarvis', 'config.json');
const FIXTURE = process.env.JARVIS_SYSTEM_FIXTURE || '';
// The demo never touches the real config's folder unless told to.
const SESSIONS =
  process.env.JARVIS_SYSTEM_SESSIONS ||
  (FIXTURE && !process.env.JARVIS_DESKTOP_CONFIG
    ? join(tmpdir(), 'jarvis-os-demo-sessions.json')
    : join(dirname(CONFIG), 'system-site-sessions.json'));
const FIXTURE_TOKEN = 'fixture-mode-token';
const ASK_FROM_NETWORK = process.env.JARVIS_SYSTEM_ASK_FROM_NETWORK === '1';
const MAX_BODY = 4096;
const MAX_FRAME_BODY = 2_900_000; // a base64 camera frame for the lab (≤ 2 MB decoded)
const MAX_UPSTREAM = 8 * 1024 * 1024;

const sessions = new Sessions(SESSIONS);
const pairing = new Pairing();
const pairLimit = new RateLimit(10, 60000);
const STATE_DIR = dirname(SESSIONS);
// 17.10: this server runs on the Mac it was first started on (hardware.mjs); elsewhere it shows only a door
const hardware = await checkHardware(process.env.JARVIS_OS_DEVICE_FILE || join(STATE_DIR, 'os-device.json'));
const tunnel = new Tunnel(PORT);
let lan = null; // the home-network listener (remote.mjs), made once the request handler exists
const askLimit = new RateLimit(10, 60000);
const buttonLimit = new RateLimit(60, 60000);
const tlt = new Service(TLT_SPEC);

// The page (needs a session) and the static code (no data in it: served to anyone who passes the Host check).
const PAGE = ['index.html', 'text/html; charset=utf-8'];
const SITES_PAGE = ['sites.html', PAGE[1]];
const IPHONE_PAGE = ['iphone.html', PAGE[1]];
const DEVICES_PAGE = ['devices.html', PAGE[1]];
// pages that steer this Mac: not for a paired phone
const MAC_ONLY_PAGES = new Set(['/sites', '/iphone', '/devices']);
const PAGES = {
  '/': PAGE,
  '/index.html': PAGE,
  '/lab': ['lab.html', PAGE[1]],
  '/lab.html': ['lab.html', PAGE[1]],
  '/sites': SITES_PAGE,
  '/iphone': IPHONE_PAGE, // the iPhone check over the cable (device-check.mjs)
  '/devices': DEVICES_PAGE, // «Телефон»: pairing a phone by QR, home network, the tunnel
  '/tlt': ['tlt', PAGE[1]], // not a file: starts TLT and goes on to it
  '/crm': ['crm', PAGE[1]], // not a file: goes on to the CRM on its server (CRM_URL)
};
// Where the personal link may land (?to=…): these pages, optionally with a lab command after #.
const LANDING = /^\/(?:lab|sites|tlt|crm|iphone|devices)?(?:#[a-z0-9=_-]{1,40})?$/;
const JS = 'text/javascript; charset=utf-8';
const PUBLIC = {
  '/app.js': ['app.js', JS],
  '/map2d.js': ['map2d.js', JS],
  '/graph2d.js': ['graph2d.js', JS],
  '/sites.js': ['sites.js', JS],
  '/iphone.js': ['iphone.js', JS],
  '/os-bar.css': ['os-bar.css', 'text/css; charset=utf-8'],
  '/devices.js': ['devices.js', JS],
  '/pair.js': ['pair.js', JS],
  '/qr.js': ['qr.js', JS],
  '/style.css': ['style.css', 'text/css; charset=utf-8'],
  '/vendor/3d-force-graph-1.80.1.min.js': ['vendor/3d-force-graph-1.80.1.min.js', JS],
  // the lab: code and pinned libraries (no data in them); the two binaries come from vendor/fetch-lab.sh
  '/lab.js': ['lab.js', JS],
  '/lab-model.js': ['lab-model.js', JS],
  '/lab-gestures.js': ['lab-gestures.js', JS],
  '/lab-model-a.js': ['lab-model-a.js', JS],
  '/lab-ring.js': ['lab-ring.js', JS],
  '/lab-hand.js': ['lab-hand.js', JS],
  '/lab.css': ['lab.css', 'text/css; charset=utf-8'],
  '/vendor/three/three.module.js': ['vendor/three/three.module.js', JS],
  '/vendor/three/three.core.js': ['vendor/three/three.core.js', JS],
  '/vendor/mediapipe/vision_bundle.mjs': ['vendor/mediapipe/vision_bundle.mjs', JS],
  '/vendor/mediapipe/wasm/vision_wasm_internal.js': ['vendor/mediapipe/wasm/vision_wasm_internal.js', JS],
  '/vendor/mediapipe/wasm/vision_wasm_internal.wasm': ['vendor/mediapipe/wasm/vision_wasm_internal.wasm', 'application/wasm'],
  '/vendor/mediapipe/hand_landmarker.task': ['vendor/mediapipe/hand_landmarker.task', 'application/octet-stream'],
};

const SECURITY = {
  'x-content-type-options': 'nosniff',
  'referrer-policy': 'no-referrer',
  'x-frame-options': 'DENY',
  // The graph library injects one <style> element and builds its shaders at runtime: styles need 'unsafe-inline'.
  'content-security-policy':
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; " +
    "media-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; " +
    "form-action 'none'",
};
// The lab runs MediaPipe's WebAssembly ('wasm-unsafe-eval' — compiling wasm, not JS eval) and uses the camera;
// connect-src stays 'self', so nothing (no frame, no stats) can be sent anywhere else.
const LAB_SECURITY = {
  ...SECURITY,
  'content-security-policy': SECURITY['content-security-policy'].replace(
    "script-src 'self'",
    "script-src 'self' 'wasm-unsafe-eval'",
  ),
  'permissions-policy': 'camera=(self), microphone=(), geolocation=()',
};

// --- request guards ---------------------------------------------------------------------------------------------

function splitHost(host) {
  let name = host;
  let port = '';
  if (name.startsWith('[')) {
    const end = name.indexOf(']');
    port = name.slice(end + 2);
    name = name.slice(1, end);
  } else if (name.includes(':')) {
    port = name.slice(name.lastIndexOf(':') + 1);
    name = name.slice(0, name.lastIndexOf(':'));
  }
  return { name: name.toLowerCase(), port };
}

// host.docker.internal: Jarvis in Docker waking TLT (with the bearer key; pages still need a session).
const localName = (name) => name === 'localhost' || name === 'host.docker.internal' || isIP(name) !== 0;
const loopback = (name) => name === 'localhost' || name === '127.0.0.1' || name === '::1';

/** DNS rebinding: a browser coming straight to this port always sends it in Host («evil.com:39110») — refused.
 * localhost and IPs (this Mac, the home-network listener) pass; a domain only when it is the open tunnel's own. */
function allowedHost(host) {
  if (!host) return false;
  const { name, port } = splitHost(host);
  if (localName(name)) return true;
  return port === '' && tunnel.host !== null && name === tunnel.host; // the open tunnel's own name — nothing else
}

const loopbackAddress = (a) => a === '127.0.0.1' || a === '::1' || a === '::ffff:127.0.0.1';

/** Commands are for this Mac (17.10, after a security review): judged by the connection itself, not only by headers a
 * client writes — the request came to the loopback listener (never the home-network one) from a loopback address, its
 * Host is a loopback name, every forwarded hop is loopback, and its session (when there is one) is not a phone's.
 * A request through the tunnel also arrives from 127.0.0.1 (cloudflared), but carries the tunnel's Host — refused. */
function askAllowed(req) {
  if (req.viaLan || req.session?.kind === 'device') return false;
  if (ASK_FROM_NETWORK) return true;
  if (!loopbackAddress(req.socket?.localAddress) || !loopbackAddress(req.socket?.remoteAddress)) return false;
  const { name } = splitHost(String(req.headers.host || ''));
  if (!loopback(name)) return false;
  const forwarded = String(req.headers['x-forwarded-for'] || '')
    .split(',')
    .map((s) => s.trim().replace(/^::ffff:/, ''))
    .filter(Boolean);
  return forwarded.every((ip) => loopback(ip));
}

const secure = (req) => String(req.headers['x-forwarded-proto'] || '').toLowerCase() === 'https';
const ownOrigin = (req) => `${secure(req) ? 'https' : 'http'}://${String(req.headers.host || '').toLowerCase()}`;

/** «Спросить» comes from this page: JSON (a cross-site form or text/plain is refused — JSON needs a preflight, which
 * is never allowed), Origin equal to this page's own, or no Origin only with Sec-Fetch-Site: same-origin. */
function fromThisPage(req) {
  if (!String(req.headers['content-type'] || '').toLowerCase().startsWith('application/json')) return false;
  const site = req.headers['sec-fetch-site'];
  if (site && site !== 'same-origin') return false;
  const origin = req.headers.origin;
  if (origin) return String(origin).toLowerCase() === ownOrigin(req);
  return site === 'same-origin';
}

// --- responses ----------------------------------------------------------------------------------------------------

function send(res, status, body, type, extra = {}) {
  res.writeHead(status, { ...SECURITY, 'content-type': type, 'cache-control': 'no-store', ...extra });
  res.end(body);
}

const json = (res, status, value) => send(res, status, JSON.stringify(value), 'application/json; charset=utf-8');

const escapeHtml = (text) =>
  String(text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

function gate(res, status, title, text) {
  const body =
    '<!doctype html><html lang="ru"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="dark">' +
    `<title>Jarvis OS</title><link rel="stylesheet" href="/style.css"></head><body class="gate"><main class="gate-box">` +
    `<h1>${escapeHtml(title)}</h1><p>${text}</p></main></body></html>`;
  send(res, status, body, 'text/html; charset=utf-8');
}

const LINK_HINT = 'Ссылку печатает команда <code>uv run jarvis system-link</code> в папке Jarvis.';

async function readBody(req, limit = MAX_BODY) {
  const declared = Number(req.headers['content-length'] || 0);
  if (declared > limit) return { tooLarge: true };
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) return { tooLarge: true };
    chunks.push(chunk);
  }
  return { text: Buffer.concat(chunks).toString('utf8') };
}

// --- Jarvis API ---------------------------------------------------------------------------------------------------

class UpstreamError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

/** Token and API address from the Mac app's config, read now (a new `jarvis desktop-setup` needs no restart). */
async function jarvisConfig() {
  let config;
  try {
    config = JSON.parse(await readFile(CONFIG, 'utf8'));
  } catch {
    throw new UpstreamError(503, 'Нет настроек Jarvis для Mac: выполните «uv run jarvis desktop-setup» в папке Jarvis.');
  }
  const token = typeof config.token === 'string' ? config.token : '';
  if (!/^[A-Za-z0-9_-]{16,200}$/.test(token)) {
    throw new UpstreamError(503, 'В настройках Jarvis для Mac нет токена: выполните «uv run jarvis desktop-setup».');
  }
  // Only a loopback http address may receive the token, whatever the file says.
  let base = new URL('http://127.0.0.1:8080');
  try {
    const parsed = new URL(String(config.base_url || ''));
    if (parsed.protocol === 'http:' && loopback(parsed.hostname.replace(/^\[|\]$/g, ''))) base = parsed;
  } catch {
    /* keep the default */
  }
  return { token, base };
}

/** The site key the link must carry, derived from the token. The demo uses a fixed token unless a config is named
 * explicitly (JARVIS_DESKTOP_CONFIG): it never reads the real one. */
async function currentKey() {
  if (FIXTURE && !process.env.JARVIS_DESKTOP_CONFIG) return siteKey(FIXTURE_TOKEN);
  return siteKey((await jarvisConfig()).token);
}

function upstream(method, path, body, timeoutMs) {
  return jarvisConfig().then(
    ({ token, base }) =>
      new Promise((resolve, reject) => {
        const payload = body === undefined ? null : Buffer.from(JSON.stringify(body));
        const req = http.request(
          {
            host: base.hostname.replace(/^\[|\]$/g, ''),
            port: Number(base.port) || 80,
            method,
            path,
            headers: {
              authorization: `Bearer ${token}`,
              accept: 'application/json',
              ...(payload ? { 'content-type': 'application/json', 'content-length': payload.length } : {}),
            },
            timeout: timeoutMs,
          },
          (res) => {
            const chunks = [];
            let size = 0;
            res.on('data', (chunk) => {
              size += chunk.length;
              if (size > MAX_UPSTREAM) {
                req.destroy();
                reject(new UpstreamError(502, 'Jarvis прислал слишком большой ответ.'));
                return;
              }
              chunks.push(chunk);
            });
            res.on('end', () => {
              const status = res.statusCode || 502;
              if (status === 401) {
                reject(new UpstreamError(502, 'Токен не подошёл: выполните «uv run jarvis desktop-setup» ещё раз.'));
                return;
              }
              if (status >= 400) {
                reject(new UpstreamError(502, `Jarvis ответил ошибкой ${status}.`));
                return;
              }
              try {
                resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
              } catch {
                reject(new UpstreamError(502, 'Jarvis прислал не JSON.'));
              }
            });
          },
        );
        req.on('timeout', () => req.destroy(new UpstreamError(504, 'Jarvis не ответил вовремя.')));
        req.on('error', (err) =>
          reject(
            err instanceof UpstreamError
              ? err
              : new UpstreamError(502, 'Jarvis не отвечает: запущен ли Docker с Jarvis (make up)?'),
          ),
        );
        if (payload) req.write(payload);
        req.end();
      }),
  );
}

/** What the page needs from the bot's reply: texts (HTML of Telegram's subset or plain), the voice, a button count. */
function reply(result) {
  const messages = Array.isArray(result?.messages) ? result.messages : [];
  return {
    messages: messages.slice(0, 10).map((m) => ({
      text: String(m?.text || '').slice(0, 20000),
      format: m?.format === 'html' ? 'html' : 'plain',
      buttons: Array.isArray(m?.buttons) ? m.buttons.reduce((n, row) => n + (Array.isArray(row) ? row.length : 0), 0) : 0,
      audio: typeof m?.audio === 'string' && m.audio.length < MAX_UPSTREAM ? m.audio : null,
    })),
    toast: typeof result?.toast === 'string' ? result.toast.slice(0, 300) : null,
  };
}

// --- the fixture (work on the page without Jarvis) ----------------------------------------------------------------

let fixtureCache = null;
async function fixture() {
  if (!fixtureCache) {
    const data = JSON.parse(await readFile(FIXTURE, 'utf8'));
    fixtureCache = data.graph ? data : { graph: data, live: { nodes: {}, counts: {}, processes: [], running: [] } };
  }
  return fixtureCache;
}

// --- routes -------------------------------------------------------------------------------------------------------

async function api(req, res, path) {
  if (req.method === 'GET' && (path === '/api/system/graph' || path === '/api/system/live')) {
    const which = path.endsWith('graph') ? 'graph' : 'live';
    if (FIXTURE) {
      const data = await fixture();
      return json(res, 200, which === 'live' ? { ...data.live, now: new Date().toISOString() } : data.graph);
    }
    return json(res, 200, await upstream('GET', `/api/desktop/v1/system/${which}`, undefined, which === 'graph' ? 20000 : 8000));
  }
  if (req.method === 'POST' && path === '/api/system/ask') {
    if (!fromThisPage(req)) return json(res, 403, { error: 'Запрос отклонён: нужен JSON с этой же страницы.' });
    if (!askAllowed(req)) {
      return json(res, 403, { error: 'Команды отсюда — только с этого Mac. По сети страница работает для просмотра.' });
    }
    const body = await readBody(req);
    if (body.tooLarge) return json(res, 413, { error: 'Слишком длинный запрос (до 4 КБ).' });
    let text = '';
    try {
      text = String(JSON.parse(body.text).text || '').trim();
    } catch {
      return json(res, 400, { error: 'Ожидался JSON: {"text": "…"}.' });
    }
    if (!text) return json(res, 400, { error: 'Пустой запрос.' });
    if (!askLimit.take()) return json(res, 429, { error: 'Слишком часто: не больше 10 вопросов в минуту.' });
    if (FIXTURE) {
      return json(res, 200, { messages: [{ text: `Демо без Jarvis: «${text.slice(0, 200)}» дошло бы до бота.`, format: 'plain', buttons: 0, audio: null }], toast: null });
    }
    return json(res, 200, reply(await upstream('POST', '/api/desktop/v1/system/ask', { text: text.slice(0, 4000) }, 90000)));
  }
  if (req.method === 'POST' && path === '/api/system/identify') {
    // Jarvis Lab «Что это за деталь?»: one camera frame (base64, ≤ ~2 MB) — the same checks as «Спросить».
    if (!fromThisPage(req)) return json(res, 403, { error: 'Запрос отклонён: нужен JSON с этой же страницы.' });
    if (!askAllowed(req)) return json(res, 403, { error: 'Распознавание — только с этого Mac.' });
    const body = await readBody(req, MAX_FRAME_BODY);
    if (body.tooLarge) return json(res, 413, { error: 'Кадр слишком большой (до 2 МБ).' });
    let frame;
    try {
      frame = JSON.parse(body.text);
    } catch {
      return json(res, 400, { error: 'Ожидался JSON: {"image": "…", "mime": "image/jpeg"}.' });
    }
    if (typeof frame.image !== 'string' || !/^image\/(jpeg|png|webp)$/.test(String(frame.mime))) {
      return json(res, 400, { error: 'Нужен кадр JPEG, PNG или WebP.' });
    }
    if (!askLimit.take()) return json(res, 429, { error: 'Слишком часто: не больше 10 запросов в минуту.' });
    if (FIXTURE) {
      return json(res, 200, { name: 'Демо: деталь', kind: 'other', markings: [], purpose: 'Без Jarvis распознавания нет.', device: null, confidence: 'low', caution: null });
    }
    return json(res, 200, await upstream('POST', '/api/desktop/v1/system/identify', { image: frame.image, mime: frame.mime }, 60000));
  }
  if (path.startsWith('/api/os/')) return osApi(req, res, path);
  return json(res, 404, { error: 'Нет такого адреса.' });
}

/** A button on these pages: JSON from this page, this Mac, ≤ 60 a minute. False when allowed; true once the refusal
 * is sent. R39: it returned json()'s undefined, so `if (buttonRefused(…)) return` never stopped — the 403/429 went
 * out and the action ran anyway (a paired phone, or any page on 127.0.0.1 with the same-site cookie). */
function buttonRefused(req, res) {
  const refusal = !fromThisPage(req)
    ? [403, 'Запрос отклонён: нужен JSON с этой же страницы.']
    : !askAllowed(req)
      ? [403, 'Кнопки — только с этого Mac.']
      : !buttonLimit.take()
        ? [429, 'Слишком часто.']
        : null;
  if (!refusal) return false;
  json(res, refusal[0], { error: refusal[1] });
  return true;
}

/** One iPhone check at a time: a second click while it runs gets the same answer. The report (serial, IMEI) goes only
 * to this page — it is not logged and not kept. */
let iphoneRun = null;
function iphoneCheck() {
  iphoneRun ??= checkIphone().finally(() => (iphoneRun = null));
  return iphoneRun;
}

/** «Телефон» (17.10): what is open for a phone, and the controls — all on this Mac only. */
async function remoteState() {
  return {
    lan: lan.status(),
    lanAddress: lanAddress(),
    tunnel: tunnel.status(),
    tunnelHours: TUNNEL_HOURS,
    devices: await sessions.devices(),
  };
}

async function jsonBody(req) {
  const body = await readBody(req);
  if (body.tooLarge) throw Object.assign(new Error('Слишком большой запрос.'), { status: 413 });
  try {
    const value = JSON.parse(body.text || '{}');
    return value && typeof value === 'object' ? value : {};
  } catch {
    throw Object.assign(new Error('Нужен JSON.'), { status: 400 });
  }
}

async function remoteApi(req, res, path) {
  if (!askAllowed(req)) return json(res, 403, { error: 'Только на этом Mac.' });
  if (req.method === 'GET' && path === '/api/os/remote') return json(res, 200, await remoteState());
  if (req.method !== 'POST') return json(res, 405, { error: 'Только POST.' });
  if (buttonRefused(req, res)) return undefined;
  const body = await jsonBody(req);
  if (path === '/api/os/remote/lan') {
    if (body.on) await lan.on();
    else await lan.off();
  } else if (path === '/api/os/remote/tunnel') {
    await tunnel.open(Number(body.hours));
  } else if (path === '/api/os/remote/tunnel/close') {
    tunnel.close();
  } else if (path === '/api/os/remote/pair') {
    const via = body.via === 'tunnel' ? 'tunnel' : 'lan';
    const base = via === 'tunnel' ? tunnel.url : lan.status().url;
    if (!base) return json(res, 409, { error: via === 'tunnel' ? 'Сначала откройте доступ вне дома.' : 'Сначала включите доступ из домашней сети.' });
    const { code, expiresIn } = pairing.start(via);
    return json(res, 200, { url: `${base}/pair#c=${code}`, expiresIn, via });
  } else if (path.startsWith('/api/os/remote/devices/') && path.endsWith('/revoke')) {
    const id = path.split('/')[5] || '';
    if (!/^[0-9a-f]{16}$/.test(id) || !(await sessions.revoke(id))) return json(res, 404, { error: 'Нет такого устройства.' });
  } else return json(res, 404, { error: 'Нет такого адреса.' });
  return json(res, 200, await remoteState());
}

/** /api/pair — a phone with a one-time QR code gets its own session (no other way in for it). The code must have been
 * made for the way the phone came: the home network (its Wi-Fi address) or the open tunnel. */
async function pairDevice(req, res) {
  if (req.method !== 'POST') return json(res, 405, { error: 'Только POST.' });
  if (!fromThisPage(req)) return json(res, 403, { error: 'Запрос отклонён.' });
  if (!pairLimit.take()) return json(res, 429, { error: 'Слишком часто. Подождите минуту.' });
  let body;
  try {
    body = await jsonBody(req);
  } catch (err) {
    return json(res, err.status || 400, { error: err.message });
  }
  const { name } = splitHost(String(req.headers.host || ''));
  const came = tunnel.host && name === tunnel.host ? 'tunnel' : lan.status().address === name ? 'lan' : null;
  const via = came ? pairing.consume(String(body.code || '').slice(0, 64)) : null;
  if (!via || via !== came) return json(res, 403, { error: 'Код устарел или уже использован — покажите на Mac новый QR.' });
  const device = String(body.name || '').replace(/[\u0000-\u001f<>"'&]/g, '').trim().slice(0, 40) || 'Телефон';
  const id = await sessions.create(await currentKey(), { kind: 'device', name: device, via });
  return send(res, 200, JSON.stringify({ ok: true }), 'application/json; charset=utf-8', { 'set-cookie': sessionCookie(id, secure(req)) });
}

async function osApi(req, res, path) {
  const parts = path.split('/').filter(Boolean); // ['api', 'os', …]
  try {
    if (req.method === 'GET' && path === '/api/os/services') {
      return json(res, 200, { services: [await tlt.status()] });
    }
    if (req.method === 'POST' && parts[2] === 'services' && parts[3] === 'tlt' && parts.length === 5) {
      if (buttonRefused(req, res)) return undefined;
      const action = { start: () => tlt.start(), stop: () => tlt.stop(), restart: () => tlt.restart() }[parts[4]];
      if (!action) return json(res, 404, { error: 'Нет такого действия.' });
      return json(res, 200, { services: [await action()] });
    }
    if (req.method === 'GET' && path === '/api/os/sites') {
      return json(res, 200, await sitesState());
    }
    if (req.method === 'POST' && parts[2] === 'sites' && parts.length === 5) {
      if (buttonRefused(req, res)) return undefined;
      return json(res, 200, await siteAction(decodeURIComponent(parts[3]), parts[4]));
    }
    if (path.startsWith('/api/os/remote')) return remoteApi(req, res, path);
    if (req.method === 'GET' && path === '/api/os/device/iphone/guide') {
      return json(res, 200, { manualChecklist: MANUAL_CHECKLIST, limits: LIMITS }); // no phone needed
    }
    if (req.method === 'POST' && path === '/api/os/device/iphone') {
      if (buttonRefused(req, res)) return undefined;
      return json(res, 200, await iphoneCheck());
    }
    if (req.method === 'POST' && path === '/api/os/localsites/launch') {
      if (buttonRefused(req, res)) return undefined;
      await launchLocalSites();
      return json(res, 200, { ok: true });
    }
  } catch (err) {
    if (err instanceof SitesError || err?.status) {
      return json(res, err.status || 500, { error: err.message, ...(err.extra || {}) });
    }
    throw err;
  }
  return json(res, 404, { error: 'Нет такого адреса.' });
}

/** Jarvis in Docker: «TLT is down» → start it here. The bearer is the site key (derived from the Mac app's token,
 * which the Jarvis API also derives); nothing but this one call accepts it. */
async function ensureFromJarvis(req, res) {
  const auth = String(req.headers.authorization || '');
  const given = auth.startsWith('Bearer ') ? auth.slice(7).trim() : '';
  if (!given || !sameSecret(given, await currentKey())) return json(res, 401, { error: 'Нужен ключ.' });
  if (req.method !== 'POST') return json(res, 405, { error: 'Только POST.' });
  if (!buttonLimit.take()) return json(res, 429, { error: 'Слишком часто.' });
  return json(res, 200, await tlt.start(15000));
}

/** /tlt: TLT is started if it is not running, then shown inside Jarvis OS at /tlt/ (17.10 — no jump to its port). */
async function openTlt(req, res) {
  const status = await tlt.start(12000);
  if (status.state !== 'running' && status.state !== 'external') {
    return gate(res, 503, 'TLT не запустился', escapeHtml(status.error || 'Попробуйте ещё раз через минуту.'));
  }
  return send(res, 302, '', 'text/plain; charset=utf-8', { location: '/tlt/' });
}

/** TLT inside Jarvis OS (17.10, the user: «объединить Jarvis и LMF … без пароля»): /tlt/* is LMF on 127.0.0.1:4321 with
 * LMF's API token added here — read from LMF's own 0600 file when a request comes; it never reaches the browser.
 * Nothing crosses over between the two: no cookie either way, only content-type / accept / length go to LMF.
 * Changes need this page's own Origin (or Sec-Fetch-Site: same-origin) — LMF's own Origin check cannot see it here.
 * The Jarvis OS menu is put over LMF's page. */
const TLT_TOKEN_FILE = join(TLT_SPEC.dir, 'app', 'data', 'api-token');
const TLT_MAX_BODY = 1_100_000; // LMF takes ≤ 1 MB bodies
const TLT_HEADERS = ['content-type', 'cache-control', 'last-modified', 'etag', 'retry-after'];

function sameOriginChange(req) {
  const site = req.headers['sec-fetch-site'];
  if (site && site !== 'same-origin') return false;
  const origin = req.headers.origin;
  if (origin) return String(origin).toLowerCase() === ownOrigin(req);
  return site === 'same-origin';
}

/** The Jarvis OS menu over TLT — the same items, order and place as on its own pages (sites.html); a phone gets only
 * the pages it may open. */
function osBar(req) {
  const mac = askAllowed(req);
  const links = [
    ['/', 'Карта', true], ['/sites', 'Сайты', mac], ['/lab', 'Лаборатория', true], ['/iphone', 'iPhone', mac],
    ['/devices', 'Телефон', mac], ['/tlt/', 'TLT', true], ['/crm', 'CRM', true],
  ].filter(([, , shown]) => shown); // prettier-ignore
  const items = links.map(([href, text]) => `<a href="${href}"${href === '/tlt/' ? ' aria-current="page"' : ''}>${text}</a>`).join('');
  return `<div class="os-bar" role="navigation" aria-label="Меню Jarvis OS">${items}</div>`;
}

function tltRequest(req, path, token) {
  return http.request({
    host: '127.0.0.1',
    port: TLT_SPEC.port,
    method: req.method,
    path,
    timeout: 30000,
    headers: {
      host: `127.0.0.1:${TLT_SPEC.port}`,
      authorization: `Bearer ${token}`,
      accept: String(req.headers.accept || '*/*').slice(0, 200),
      ...(req.headers['content-type'] ? { 'content-type': String(req.headers['content-type']).slice(0, 100) } : {}),
      ...(req.headers['content-length'] ? { 'content-length': String(req.headers['content-length']) } : {}),
    },
  });
}

async function proxyTlt(req, res, url) {
  const method = req.method || 'GET';
  if (!['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) return json(res, 405, { error: 'Метод не поддерживается.' });
  if (method !== 'GET' && method !== 'HEAD' && !sameOriginChange(req)) {
    return json(res, 403, { error: 'Запрос отклонён: изменения — только с этой же страницы.' });
  }
  if (Number(req.headers['content-length'] || 0) > TLT_MAX_BODY) return json(res, 413, { error: 'Слишком большой запрос.' });
  let token;
  try {
    token = (await readFile(TLT_TOKEN_FILE, 'utf8')).trim();
  } catch {
    token = '';
  }
  if (!/^[A-Za-z0-9_-]{20,200}$/.test(token)) return gate(res, 503, 'TLT не готов', 'Нет токена LMF — запустите TLT один раз (он создаст его сам).');
  if (!(await portOpen(TLT_SPEC.port))) {
    const status = await tlt.start(12000);
    if (status.state !== 'running' && status.state !== 'external') {
      return gate(res, 503, 'TLT не запустился', escapeHtml(status.error || 'Попробуйте ещё раз через минуту.'));
    }
  }
  const path = (url.pathname.slice('/tlt'.length) || '/') + url.search;
  const page = method === 'GET' && (path === '/' || path.startsWith('/?') || path === '/index.html');
  await new Promise((resolve) => {
    const upstream = tltRequest(req, path, token);
    upstream.on('timeout', () => upstream.destroy(new Error('timeout')));
    upstream.on('error', () => {
      if (!res.headersSent) gate(res, 502, 'TLT не ответил', 'Попробуйте ещё раз.');
      else res.end();
      resolve();
    });
    upstream.on('response', (answer) => {
      const headers = { ...SECURITY };
      for (const name of TLT_HEADERS) if (answer.headers[name]) headers[name] = answer.headers[name];
      if (!page) {
        res.writeHead(answer.statusCode || 502, headers);
        answer.pipe(res);
        answer.on('end', resolve);
        answer.on('error', resolve);
        return;
      }
      const chunks = [];
      answer.on('data', (c) => chunks.push(c));
      answer.on('end', () => {
        const html = Buffer.concat(chunks).toString('utf8')
          .replace('</head>', '<link rel="stylesheet" href="/os-bar.css">\n</head>')
          .replace(/<body([^>]*)>/, (m, attrs) => `<body${attrs} class="with-os-bar">${osBar(req)}`);
        res.writeHead(answer.statusCode || 200, { ...headers, 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
        res.end(html);
        resolve();
      });
    });
    if (method === 'GET' || method === 'HEAD') return upstream.end();
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > TLT_MAX_BODY) {
        upstream.destroy();
        if (!res.headersSent) json(res, 413, { error: 'Слишком большой запрос.' });
        req.destroy();
        resolve();
      }
    });
    req.pipe(upstream);
  });
}

/** The lab MacBook's screen: the user's wallpaper + Dock (scripts/lab_screen.sh; git-ignored, only with a session). */
async function labScreen(req, res) {
  if (req.method !== 'GET' && req.method !== 'HEAD') return send(res, 405, 'Метод не поддерживается.', 'text/plain; charset=utf-8');
  let data;
  try {
    data = await readFile(join(HERE, 'data', 'lab-screen.jpg'));
  } catch {
    return send(res, 404, 'Картинки экрана нет: ./scripts/lab_screen.sh', 'text/plain; charset=utf-8');
  }
  res.writeHead(200, { ...SECURITY, 'content-type': 'image/jpeg', 'cache-control': 'no-store' });
  return res.end(req.method === 'HEAD' ? undefined : data);
}

/** /crm: the CRM («Воронка») lives on its own server since 24.10 (the user: «по этой ссылке я должен переходить в
 * джарвисе») — it has its own login, so a paired phone may go too. Only an https address is followed. */
function crmUrl() {
  const raw = process.env.JARVIS_CRM_URL || 'https://crm.example.com/';
  try {
    const url = new URL(raw);
    return url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}
function openCrm(res) {
  const target = crmUrl();
  if (!target) return gate(res, 503, 'CRM не настроена', 'Адрес CRM (JARVIS_CRM_URL) должен начинаться с https://.');
  res.writeHead(302, { ...SECURITY, location: target, 'cache-control': 'no-store' });
  return res.end();
}

async function handle(req, res, url) {
  const path = url.pathname;
  const pageFile = PAGES[path];
  const page = Boolean(pageFile);

  // The personal link: a session, then on to the page without the key in the address. A tiny page does the step (not
  // a 30x): after a link clicked on another site a SameSite=Strict cookie would not ride along a redirect.
  if (page && pageFile === PAGE && url.searchParams.has(KEY_PARAM)) {
    if (!askAllowed(req)) {
      return gate(res, 403, 'Ключ — только на этом Mac', 'Телефон привязывается одноразовым QR-кодом: на Mac откройте Jarvis OS → «Телефон».');
    }
    if (!sameSecret(url.searchParams.get(KEY_PARAM), await currentKey())) {
      return gate(res, 401, 'Ключ не подошёл', `Возьмите свежую ссылку. ${LINK_HINT}`);
    }
    const id = await sessions.create(await currentKey());
    const wanted = url.searchParams.get('to') || '/';
    const to = LANDING.test(wanted) ? wanted : '/';
    const body =
      `<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=${to}">` +
      '<title>Jarvis OS</title><link rel="stylesheet" href="/style.css"></head><body class="gate"><main class="gate-box">' +
      `<p><a href="${to}">Открыть Jarvis OS</a></p></main></body></html>`;
    return send(res, 200, body, 'text/html; charset=utf-8', { 'set-cookie': sessionCookie(id, secure(req)) });
  }

  const asset = PUBLIC[path];
  if (asset && (req.method === 'GET' || req.method === 'HEAD')) {
    const data = await readFile(join(HERE, asset[0]));
    const pinned = path.startsWith('/vendor/'); // versioned, verified files: cached for a day
    res.writeHead(200, { ...SECURITY, 'content-type': asset[1], 'cache-control': pinned ? 'max-age=86400' : 'no-cache' });
    return res.end(req.method === 'HEAD' ? undefined : data);
  }
  if (path === '/api/os/services/tlt/ensure') return ensureFromJarvis(req, res);
  if (path === '/pair' && (req.method === 'GET' || req.method === 'HEAD')) {
    const data = await readFile(join(HERE, 'pair.html'));
    res.writeHead(200, { ...SECURITY, 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
    return res.end(req.method === 'HEAD' ? undefined : data);
  }
  if (path === '/api/pair') return pairDevice(req, res);
  const inTlt = path.startsWith('/tlt/');
  if (!page && !inTlt && !path.startsWith('/api/')) return send(res, 404, 'Не найдено.', 'text/plain; charset=utf-8');

  const cookieId = readCookie(req.headers.cookie, COOKIE);
  const signedIn = await sessions.valid(cookieId, await currentKey());
  req.session = signedIn; // a phone's session is never «this Mac» (askAllowed)
  if (!signedIn) {
    if (inTlt && path.startsWith('/tlt/api/')) return json(res, 401, { error: 'Нужен вход в Jarvis OS.' });
    if (page || inTlt) return gate(res, 401, 'Откройте ссылку из Джарвиса', `Эта страница открывается по личной ссылке. ${LINK_HINT}`);
    return json(res, 401, { error: 'Откройте ссылку из Джарвиса: uv run jarvis system-link.' });
  }
  if (inTlt) return proxyTlt(req, res, url);
  if (path === '/api/lab/screen') return labScreen(req, res);
  if (path.startsWith('/api/')) return api(req, res, path);
  if (req.method !== 'GET' && req.method !== 'HEAD') return send(res, 405, 'Метод не поддерживается.', 'text/plain; charset=utf-8');
  if (MAC_ONLY_PAGES.has(path) && !askAllowed(req)) {
    return gate(res, 403, 'Только на этом Mac', 'Эта страница управляет Mac — откройте её на нём.');
  }
  if (path === '/tlt') return openTlt(req, res);
  if (path === '/crm') return openCrm(res);
  const data = await readFile(join(HERE, pageFile[0]));
  const headers = pageFile === PAGE || pageFile === SITES_PAGE || pageFile === IPHONE_PAGE || pageFile === DEVICES_PAGE ? SECURITY : LAB_SECURITY;
  const renew = signedIn.renewed && cookieId ? { 'set-cookie': sessionCookie(cookieId, secure(req)) } : {}; // a year ahead again
  res.writeHead(200, { ...headers, 'content-type': pageFile[1], 'cache-control': 'no-store', ...renew });
  return res.end(req.method === 'HEAD' ? undefined : data);
}

async function onRequest(req, res) {
  let url;
  try {
    url = new URL(req.url || '/', 'http://localhost');
  } catch {
    return send(res, 400, 'Bad request', 'text/plain; charset=utf-8');
  }
  if (!hardware.ok && url.pathname !== '/style.css') {
    const why = hardware.reason === 'no-id'
      ? 'Не удалось прочитать железо этого Mac.'
      : 'Этот Jarvis OS работает только на Mac, к которому привязан. Переезд на новый Mac: удалите там os-device.json рядом с настройками Jarvis.';
    return gate(res, 423, 'Jarvis OS привязан к другому Mac', escapeHtml(why));
  }
  if (!allowedHost(req.headers.host)) {
    return send(res, 403, 'Обращение по неизвестному имени хоста отклонено.', 'text/plain; charset=utf-8');
  }
  try {
    return await handle(req, res, url);
  } catch (err) {
    const known = err instanceof UpstreamError;
    if (!known) console.error(`jarvis-os: ${req.method} ${url.pathname} failed: ${err?.message || err}`);
    if (res.headersSent) return res.end();
    if (url.pathname.startsWith('/api/')) return json(res, known ? err.status : 500, { error: known ? err.message : 'Внутренняя ошибка страницы.' });
    return gate(res, known ? err.status : 500, 'Jarvis OS не готова', escapeHtml(known ? err.message : 'Внутренняя ошибка страницы.'));
  }
}

const server = http.createServer(onRequest);
lan = new Lan((req, res) => {
  req.viaLan = true; // the home-network listener: never «this Mac», whatever the headers say
  return onRequest(req, res);
}, PORT, join(STATE_DIR, 'os-remote.json'));
for (const signal of ['SIGTERM', 'SIGINT']) {
  process.once(signal, () => {
    tunnel.close(); // the tunnel never outlives this server
    process.exit(0);
  });
}

server.listen(PORT, HOST, async () => {
  if (!FIXTURE && hardware.ok) await lan.restore(); // «дома» stays on across restarts when it was on
  // TLT: the one this server started before (PID file) is taken back; otherwise started now — Jarvis in Docker
  // writes into it at any hour. JARVIS_TLT_AUTOSTART=0 (and the demo) leave it alone.
  if (!FIXTURE && process.env.JARVIS_TLT_AUTOSTART !== '0') {
    await tlt.adopt();
    tlt.watch();
    tlt.start(0).catch(() => {});
  }
  // The personal link right away in a terminal (the same one `uv run jarvis system-link` prints): a bare
  // http://localhost:PORT only shows the «open the link» door, which looked like «the site shows nothing» (14.10).
  // The key only to a live terminal: LocalSites (or any supervisor) keeps the output of its sites in logs.
  let link = `uv run jarvis system-link (в папке Jarvis)`;
  if (process.stdout.isTTY) {
    try {
      link = `http://127.0.0.1:${PORT}/?${KEY_PARAM}=${await currentKey()}`;
    } catch (err) {
      link = `нет ключа: ${err?.message || err} — выполните uv run jarvis desktop-setup`;
    }
  }
  console.log(`Jarvis OS работает${FIXTURE ? ' (демо-данные из файла)' : ''}.`);
  console.log(`Откройте один раз эту ссылку (дальше вход держится сам — просто http://127.0.0.1:${PORT}): ${link}`);
  if (!hardware.ok) console.log('Jarvis OS привязан к другому Mac — страницы закрыты.');
  console.log(`Лаборатория: http://127.0.0.1:${PORT}/lab · Сайты: /sites · TLT: /tlt · остановить: Ctrl+C`);
});
