// Jarvis OS — who may use the page (not served to the browser).
//
// The server holds the Mac app's token, so a request only gets through with a session:
//   1. this Mac: the personal link http://127.0.0.1:<port>/?key=<site key> — the Jarvis Mac app opens it by itself
//      (it holds the token); site key = HMAC-SHA256(desktop token, "jarvis-system-site"), hex, first 32 — the same
//      as jarvis/system/site.py;
//   2. a phone (17.10): a one-time QR from the «Телефон» page on this Mac (Pairing, 5 minutes, single use);
//   3. → a random session id in an HttpOnly, SameSite=Strict cookie; it lives a year and renews itself while used, so
//      nobody types anything; sessions are kept server-side as SHA-256 of the id (a stolen file opens nothing) in a
//      0600 file next to the Mac app's config; they belong to the key that issued them, so a new token
//      (desktop-setup) ends them all; a phone can be unpaired on its own.
// Not «anything on this Mac without a session»: Jarvis's Docker containers reach this port too (host.docker.internal)
// and can send any Host / Origin, so a browser could not be told from a container.
// `?key=` and not `?k=`: LocalSites takes `k` for its own LAN key.

import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { chmod, mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

export const KEY_LABEL = 'jarvis-system-site';
export const KEY_PARAM = 'key';
export const COOKIE = 'jarvis_os_session';
export const SESSION_DAYS = 365;
const RENEW_MS = 24 * 3600 * 1000; // a used session is pushed a year ahead at most once a day
const MAX_SESSIONS = 50;
export const PAIR_MS = 5 * 60 * 1000;

const sha256 = (value) => createHash('sha256').update(value).digest('hex');

export function siteKey(token) {
  return createHmac('sha256', token).update(KEY_LABEL).digest('hex').slice(0, 32);
}

/** Constant-time comparison (a wrong length still costs one comparison). */
export function sameSecret(provided, expected) {
  const a = Buffer.from(String(provided ?? ''));
  const b = Buffer.from(String(expected));
  if (a.length !== b.length) {
    timingSafeEqual(b, b);
    return false;
  }
  return timingSafeEqual(a, b);
}

export function readCookie(header, name) {
  for (const part of String(header || '').split(';')) {
    const index = part.indexOf('=');
    if (index !== -1 && part.slice(0, index).trim() === name) return part.slice(index + 1).trim();
  }
  return null;
}

export function sessionCookie(id, secure) {
  const maxAge = SESSION_DAYS * 86400;
  return `${COOKIE}=${id}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${maxAge}${secure ? '; Secure' : ''}`;
}

/** One session record; the old files kept only the expiry (a number). */
const record = (value) =>
  typeof value === 'number' ? { exp: value, kind: 'mac', seen: 0, created: 0 } : value && typeof value === 'object' ? value : null;

export class Sessions {
  constructor(file) {
    this.file = file;
    this.data = null; // {key: fingerprint of the site key, sessions: {sha256(id): {exp, kind, name?, via?, created, seen}}}
  }

  async load() {
    if (this.data) return;
    try {
      this.data = JSON.parse(await readFile(this.file, 'utf8'));
    } catch {
      this.data = null;
    }
    if (!this.data || typeof this.data !== 'object' || typeof this.data.sessions !== 'object' || !this.data.sessions) {
      this.data = { key: '', sessions: {} };
    }
  }

  async save() {
    await mkdir(dirname(this.file), { recursive: true, mode: 0o700 });
    const tmp = `${this.file}.${process.pid}.tmp`;
    await writeFile(tmp, JSON.stringify(this.data), { mode: 0o600 });
    await chmod(tmp, 0o600);
    await rename(tmp, this.file);
  }

  /** A new session for the holder of `key`; returns the id for the cookie. meta: {kind: 'mac'|'device', name, via}. */
  async create(key, meta = {}) {
    await this.load();
    const fingerprint = sha256(key).slice(0, 16);
    if (this.data.key !== fingerprint) this.data = { key: fingerprint, sessions: {} };
    const now = Date.now();
    const kept = Object.entries(this.data.sessions)
      .map(([hash, value]) => [hash, record(value)])
      .filter(([, r]) => r && r.exp > now)
      .sort((a, b) => b[1].exp - a[1].exp)
      .slice(0, MAX_SESSIONS - 1);
    const id = randomBytes(32).toString('base64url');
    const fresh = {
      exp: now + SESSION_DAYS * 86400000,
      kind: meta.kind === 'device' ? 'device' : 'mac',
      ...(meta.name ? { name: String(meta.name).slice(0, 40) } : {}),
      ...(meta.via ? { via: String(meta.via).slice(0, 10) } : {}),
      created: now,
      seen: now,
    };
    this.data.sessions = Object.fromEntries([[sha256(id), fresh], ...kept]);
    await this.save();
    return id;
  }

  /** The session behind this cookie, or null. A used one is renewed (a year ahead) at most once a day:
   *  the result says `renewed` so the page can send the cookie again with a fresh Max-Age. */
  async valid(id, key) {
    if (!id || id.length > 100 || !key) return null;
    await this.load();
    if (this.data.key !== sha256(key).slice(0, 16)) return null;
    const hash = sha256(id);
    const r = record(this.data.sessions[hash]);
    const now = Date.now();
    if (!r || r.exp <= now) return null;
    let renewed = false;
    if (now - (r.seen || 0) > RENEW_MS) {
      Object.assign(r, { exp: now + SESSION_DAYS * 86400000, seen: now });
      this.data.sessions[hash] = r;
      await this.save();
      renewed = true;
    }
    return { ...r, hash: hash.slice(0, 16), renewed };
  }

  /** The paired phones (not this Mac's own sessions): what the «Телефон» page lists. */
  async devices() {
    await this.load();
    const now = Date.now();
    return Object.entries(this.data.sessions)
      .map(([hash, value]) => ({ hash, r: record(value) }))
      .filter(({ r }) => r && r.kind === 'device' && r.exp > now)
      .map(({ hash, r }) => ({ id: hash.slice(0, 16), name: r.name || 'Устройство', via: r.via || '', created: r.created, seen: r.seen }))
      .sort((a, b) => b.seen - a.seen);
  }

  /** Unpair one phone by its short id; true when one was removed. */
  async revoke(shortId) {
    await this.load();
    const hash = Object.keys(this.data.sessions).find((h) => h.slice(0, 16) === shortId && record(this.data.sessions[h])?.kind === 'device');
    if (!hash) return false;
    delete this.data.sessions[hash];
    await this.save();
    return true;
  }
}

/** One-time pairing codes for a phone (in memory only): one live code per way in, 5 minutes, used once. */
export class Pairing {
  constructor(ttl = PAIR_MS) {
    this.ttl = ttl;
    this.codes = new Map(); // via → {code, until}
  }

  start(via) {
    const code = randomBytes(24).toString('base64url');
    this.codes.set(via, { code, until: Date.now() + this.ttl });
    return { code, expiresIn: Math.round(this.ttl / 1000) };
  }

  /** The way in this code was made for, if it is live — and then it is gone. */
  consume(code) {
    const now = Date.now();
    for (const [via, entry] of this.codes) {
      if (entry.until <= now) {
        this.codes.delete(via);
        continue;
      }
      if (sameSecret(code, entry.code)) {
        this.codes.delete(via);
        return via;
      }
    }
    return null;
  }
}

/** At most `limit` calls per `windowMs` (one person uses the page: one shared window). */
export class RateLimit {
  constructor(limit, windowMs) {
    this.limit = limit;
    this.windowMs = windowMs;
    this.hits = [];
  }

  take() {
    const now = Date.now();
    this.hits = this.hits.filter((t) => now - t < this.windowMs);
    if (this.hits.length >= this.limit) return false;
    this.hits.push(now);
    return true;
  }
}
