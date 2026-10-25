// Jarvis OS — the LocalSites panel inside Jarvis OS (16.10, the user: «внедрить панель LocalSites полностью в
// Jarvis OS»). LocalSites stays the engine (it serves the sites, the home-network links and the tunnels); this module
// talks to its «Пульт» API on this Mac (127.0.0.1:7788 — from loopback it needs no password) with a closed set of
// actions: start, stop, home network on, tunnel on / off. Site keys and links go only to the signed-in page and are
// never logged; the commands and folders of the sites are not passed on at all.

import { execFile } from 'node:child_process';
import http from 'node:http';

const PULT_PORT = Number(process.env.JARVIS_LOCALSITES_PORT) || 7788;
const ACTIONS = new Set(['start', 'stop', 'lan-on', 'tunnel-on', 'tunnel-off']);
const ID = /^[A-Za-z0-9-]{8,64}$/;
// Sites Jarvis OS runs itself now: shown as moved, never controlled through LocalSites from here.
export const MOVED = new Set(['tlt']);

export class SitesError extends Error {
  constructor(status, message, extra = {}) {
    super(message);
    this.status = status;
    this.extra = extra;
  }
}

function pult(method, path) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        host: '127.0.0.1',
        port: PULT_PORT,
        method,
        path,
        headers: { host: `127.0.0.1:${PULT_PORT}`, accept: 'application/json' }, // LocalSites refuses other hosts
        timeout: method === 'GET' ? 5000 : 60000, // a tunnel takes a while to come up
      },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          let body = null;
          try {
            body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          } catch {
            body = null;
          }
          if ((res.statusCode || 500) >= 400) {
            reject(new SitesError(502, (body && body.error) || `LocalSites ответил ошибкой ${res.statusCode}.`));
          } else resolve(body);
        });
      },
    );
    req.on('timeout', () => req.destroy(new SitesError(504, 'LocalSites не ответил вовремя.')));
    req.on('error', (err) =>
      reject(
        err instanceof SitesError
          ? err
          : new SitesError(503, 'LocalSites не запущен.', { launch: true }),
      ),
    );
    req.end();
  });
}

const text = (v, max = 300) => (typeof v === 'string' ? v.slice(0, max) : '');

/** What the page shows of one site: no command, no folder, no raw token. */
function view(site) {
  const moved = MOVED.has(text(site.name).toLowerCase());
  return {
    id: text(site.id, 64),
    name: text(site.name, 120),
    group: text(site.group, 80),
    kind: site.kind === 'command' ? 'command' : 'static',
    status: text(site.status, 20) || 'stopped',
    error: text(site.error),
    port: Number(site.port) || null,
    url: text(site.url, 500),
    lan: Boolean(site.lan),
    lanUrl: text(site.lanUrl, 500),
    tunnelStatus: text(site.tunnelStatus, 20) || 'off',
    tunnelUrl: text(site.tunnelUrl, 500),
    tunnelError: text(site.tunnelError),
    archived: Boolean(site.archived),
    missing: site.exists === false,
    moved,
  };
}

export async function sitesState() {
  const state = await pult('GET', '/api/state');
  const sites = Array.isArray(state && state.sites) ? state.sites.map(view) : [];
  return { ip: text(state && state.ip, 60), sites };
}

export async function siteAction(id, action) {
  if (!ID.test(String(id)) || !ACTIONS.has(action)) throw new SitesError(400, 'Нет такого действия.');
  const { sites } = await sitesState();
  const site = sites.find((s) => s.id === id);
  if (!site) throw new SitesError(404, 'Нет такого сайта.');
  if (site.moved) throw new SitesError(409, `${site.name} теперь запускает Jarvis OS — кнопки в разделе выше.`);
  await pult('POST', `/api/site/${encodeURIComponent(id)}/${action}`);
  return sitesState();
}

/** «Запустить LocalSites»: the app itself, by its name (a fixed command). */
export function launchLocalSites() {
  return new Promise((resolve, reject) =>
    execFile('/usr/bin/open', ['-a', 'LocalSites'], { timeout: 15000 }, (err) =>
      err ? reject(new SitesError(500, 'Не получилось открыть LocalSites — он установлен в «Программы»?')) : resolve(),
    ),
  );
}
