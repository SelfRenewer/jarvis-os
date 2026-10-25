// Jarvis OS — how a phone reaches it (17.10, the user: «с телефона по открытому порту — только через QR-код или QR
// туннель»):
//   · at home: a second listener on this Mac's own Wi-Fi address, only while «дома» is on (the choice survives
//     restarts); the usual one stays on 127.0.0.1;
//   · away: a cloudflared quick tunnel (https://….trycloudflare.com) to this server's loopback port, closed by itself
//     after the chosen hours (and when the server stops).
// Neither lets anyone in by itself: a phone needs a session from a one-time QR (auth.mjs Pairing), and a request that
// came over Wi-Fi or the tunnel is never «this Mac» (its Host is not loopback). Fixed commands only: cloudflared
// from the Homebrew folders with fixed arguments.

import { execFile, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { access, chmod, mkdir, readFile, writeFile } from 'node:fs/promises';
import http from 'node:http';
import { networkInterfaces } from 'node:os';
import { dirname } from 'node:path';

const TUNNEL_URL = /https:\/\/[a-z0-9-]+\.trycloudflare\.com/;
const TUNNEL_READY = /Registered tunnel connection/; // the name answers only after this — before it a phone gets «not found»
export const TUNNEL_HOURS = [1, 4, 12];

/** This Mac's address on the home network (Wi-Fi / Ethernet), or null. */
export function lanAddress(interfaces = networkInterfaces()) {
  const names = ['en0', 'en1', ...Object.keys(interfaces).filter((n) => n !== 'en0' && n !== 'en1')];
  for (const name of names) {
    if (/^(lo|utun|awdl|llw|bridge|anpi|ap)\d*/.test(name)) continue;
    for (const a of interfaces[name] || []) {
      if (a.family === 'IPv4' && !a.internal && !a.address.startsWith('169.254.')) return a.address;
    }
  }
  return null;
}

function run(file, args) {
  return new Promise((resolve) => {
    execFile(file, args, { timeout: 3000, env: { PATH: '/usr/bin:/bin:/usr/sbin:/sbin' } }, (err, out) => resolve(err ? '' : String(out)));
  });
}

/** Which network the Mac is on: a hash of the router's hardware address (route + arp, fixed commands, no permission
 * prompt), or null when it cannot be told. Not the Wi-Fi name: macOS hides it from apps without Location access. */
export async function homeNetwork() {
  const gateway = /gateway:\s*([0-9.]+)/.exec(await run('/sbin/route', ['-n', 'get', 'default']))?.[1];
  if (!gateway) return null;
  const mac = /\sat\s+([0-9a-f]{1,2}(?::[0-9a-f]{1,2}){5})\s/i.exec(await run('/usr/sbin/arp', ['-n', gateway]))?.[1];
  return mac ? createHash('sha256').update(`jarvis-os-home:${mac.toLowerCase()}`).digest('hex').slice(0, 16) : null;
}

async function readState(file) {
  try {
    return JSON.parse(await readFile(file, 'utf8')) || {};
  } catch {
    return {};
  }
}
async function writeState(file, data) {
  await mkdir(dirname(file), { recursive: true, mode: 0o700 });
  await writeFile(file, JSON.stringify(data), { mode: 0o600 });
  await chmod(file, 0o600);
}

/** The home-network listener: the same request handler on <Wi-Fi address>:<port>.
 *
 * Plain HTTP, so it belongs to the home network only (M7, review 17.10: on someone else's Wi-Fi a neighbour could
 * read or change the traffic). Turning it on remembers the network (homeNetwork()); a minute-by-minute watch closes
 * the listener when the Mac is on another network and opens it again back home — also on the new address when DHCP
 * gave the Mac another one. A restart away from home does not open it. */
export class Lan {
  constructor(handler, port, stateFile, { network = homeNetwork, address = lanAddress, everyMs = 60_000 } = {}) {
    Object.assign(this, { handler, port, stateFile, network, addressOf: address, everyMs });
    Object.assign(this, { server: null, address: null, away: false, timer: null });
  }

  status() {
    return {
      on: Boolean(this.server),
      away: this.away, // wanted on, but the Mac is not on the home network now
      address: this.address,
      url: this.server ? `http://${this.address}:${this.port}` : null,
    };
  }

  async restore() {
    if ((await readState(this.stateFile)).lan) await this.check().catch(() => {});
    this.watch();
  }

  watch() {
    if (this.timer) return;
    this.timer = setInterval(() => this.check().catch(() => {}), this.everyMs);
    this.timer.unref?.();
  }

  /** Listening exactly when it is wanted and the Mac is home; on the Mac's current address. */
  async check() {
    const state = await readState(this.stateFile);
    if (!state.lan) return this.status();
    if (!state.home) { // fail closed: on from before the network was remembered → off until turned on at home
      await this.close();
      this.away = false;
      await writeState(this.stateFile, { ...state, lan: false });
      return this.status();
    }
    const here = await this.network();
    const home = here !== null && here === state.home; // an unknown network is not home
    this.away = !home;
    if (!home) {
      await this.close();
    } else if (this.server && this.addressOf() !== this.address) {
      await this.close();
      await this.listen().catch(() => {});
    } else if (!this.server) {
      await this.listen().catch(() => {});
    }
    return this.status();
  }

  async listen() {
    const address = this.addressOf();
    if (!address) throw Object.assign(new Error('Mac не в домашней сети: нет Wi-Fi-адреса.'), { status: 409 });
    const server = http.createServer(this.handler);
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(this.port, address, resolve);
    });
    Object.assign(this, { server, address });
  }

  async close() {
    const server = this.server;
    this.server = null;
    if (server) await new Promise((resolve) => server.close(() => resolve()));
  }

  /** Turned on by hand on this Mac: this network becomes «home». Refused when the network cannot be told — the
   * door could not be closed elsewhere then. */
  async on() {
    const home = await this.network();
    if (!home) throw Object.assign(new Error('Не удалось определить домашнюю сеть (роутер не отвечает) — вход по Wi-Fi не включён.'), { status: 409 });
    if (!this.server) await this.listen();
    this.away = false;
    await writeState(this.stateFile, { ...(await readState(this.stateFile)), lan: true, home });
    this.watch();
    return this.status();
  }

  async off() {
    await this.close();
    this.away = false;
    await writeState(this.stateFile, { ...(await readState(this.stateFile)), lan: false });
    return this.status();
  }

  stop() {
    clearInterval(this.timer);
    this.timer = null;
    return this.close();
  }
}

/** The away door: a quick tunnel, closed after `hours`. */
export class Tunnel {
  constructor(port, { dirs = ['/opt/homebrew/bin', '/usr/local/bin'] } = {}) {
    Object.assign(this, { port, dirs, child: null, url: null, until: 0, timer: null });
  }

  get host() {
    return this.url ? new URL(this.url).host : null;
  }

  status() {
    return { on: Boolean(this.url), url: this.url, until: this.url ? new Date(this.until).toISOString() : null };
  }

  async tool() {
    for (const dir of this.dirs) {
      try {
        await access(`${dir}/cloudflared`, constants.X_OK);
        return `${dir}/cloudflared`;
      } catch {
        /* not here */
      }
    }
    return null;
  }

  async open(hours) {
    if (!TUNNEL_HOURS.includes(hours)) throw Object.assign(new Error('Можно на 1, 4 или 12 часов.'), { status: 400 });
    if (this.url) {
      this.close();
    }
    const tool = await this.tool();
    if (!tool) throw Object.assign(new Error('Нет cloudflared. Установите: brew install cloudflared'), { status: 409 });
    const child = spawn(tool, ['tunnel', '--no-autoupdate', '--url', `http://127.0.0.1:${this.port}`], {
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    this.child = child;
    const url = await new Promise((resolve) => {
      let text = '';
      let found = null;
      const done = (value) => {
        clearTimeout(timer);
        resolve(value);
      };
      const timer = setTimeout(() => done(null), 45000);
      const read = (chunk) => {
        text = (text + chunk).slice(-16000);
        found ??= TUNNEL_URL.exec(text)?.[0] ?? null;
        if (found && TUNNEL_READY.test(text)) done(found); // the address and a live connection to Cloudflare
      };
      child.stdout.on('data', read);
      child.stderr.on('data', read);
      child.once('exit', () => done(null));
    });
    if (!url || this.child !== child) {
      child.kill();
      if (this.child === child) this.child = null;
      throw Object.assign(new Error('Туннель не открылся — попробуйте ещё раз.'), { status: 502 });
    }
    child.stdout.resume();
    child.stderr.resume();
    child.once('exit', () => {
      if (this.child === child) this.clear();
    });
    this.url = url;
    this.until = Date.now() + hours * 3600 * 1000;
    this.timer = setTimeout(() => this.close(), hours * 3600 * 1000);
    this.timer.unref?.();
    return this.status();
  }

  clear() {
    clearTimeout(this.timer);
    Object.assign(this, { child: null, url: null, until: 0, timer: null });
  }

  close() {
    const child = this.child;
    this.clear();
    child?.kill();
    return this.status();
  }
}
