// Jarvis OS — services it keeps running itself (16.10: TLT moved here from LocalSites, the user's choice).
//
// TLT (the LMF tracker, ~/Sites/TLT) runs as `node app/server.mjs` with PORT=4321 — the same command
// LocalSites used — started when Jarvis OS starts (Jarvis in Docker writes pills, sleep and food into it at any
// hour) and restarted if it falls (at most 5 times in 5 minutes, then it waits for a button). Its PID is kept in a
// file, so a Jarvis OS restarted later finds the TLT it started (TLT outlives a restart of this page server on
// purpose). A TLT someone else started on the port (LocalSites, a terminal) is shown as «запущен не Jarvis».
// Only fixed commands run here: node with TLT's own script; nothing from a request reaches a command line.

import { execFile, spawn } from 'node:child_process';
import { closeSync, mkdirSync, openSync, readFileSync, statSync, truncateSync, unlinkSync, writeFileSync } from 'node:fs';
import net from 'node:net';
import { homedir } from 'node:os';
import { join } from 'node:path';

const LOGS = join(homedir(), 'Library', 'Logs', 'Jarvis');
const STATE = join(homedir(), 'Library', 'Application Support', 'Jarvis');
const MAX_LOG = 5 * 1024 * 1024;
const RESTARTS = 5;
const RESTART_WINDOW_MS = 5 * 60 * 1000;

export const TLT_SPEC = {
  id: 'tlt',
  name: 'TLT',
  title: 'Дневник TLT / TPL',
  about: 'Трекер LMF: дневник дня, TPL, еда, таблетки, анализы. Джарвис пишет в него по вашему «✅ Записать».',
  dir: process.env.JARVIS_TLT_DIR || join(homedir(), 'Sites', 'TLT'),
  script: join('app', 'server.mjs'),
  port: Number(process.env.JARVIS_TLT_PORT) || 4321,
};

/** True when something accepts connections on 127.0.0.1:port (≤ 600 ms). */
export function portOpen(port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: '127.0.0.1', port });
    const done = (ok) => {
      socket.destroy();
      resolve(ok);
    };
    socket.setTimeout(600, () => done(false));
    socket.once('connect', () => done(true));
    socket.once('error', () => done(false));
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

export class Service {
  constructor(spec) {
    this.spec = spec;
    this.child = null;
    this.pid = null; // a TLT this Jarvis OS started earlier (from the PID file)
    this.wanted = false;
    this.exits = [];
    this.error = null;
    this.pidFile = join(STATE, `${spec.id}.pid`);
    this.logFile = join(LOGS, `${spec.id}.log`);
  }

  /** A TLT started by an earlier Jarvis OS keeps running across its restarts: take it back by its PID file — only
   * when that PID is still node running TLT's own script (a PID reused by another program is never touched). */
  async adopt() {
    let pid = 0;
    try {
      pid = Number(readFileSync(this.pidFile, 'utf8').trim());
    } catch {
      return; // no file: nothing to adopt
    }
    if (!(pid > 1 && alive(pid))) return;
    const command = await new Promise((resolve) =>
      execFile('/bin/ps', ['-p', String(pid), '-o', 'command='], { timeout: 3000 }, (err, out) => resolve(err ? '' : String(out))),
    );
    if (command.includes('node') && command.includes(this.spec.script)) {
      this.pid = pid;
      this.wanted = true;
    }
  }

  /** Every 30 s: a wanted service that is down (its process died while this server was restarting) comes back. */
  watch() {
    setInterval(async () => {
      if (!this.wanted || this.child || this.managedPid()) return;
      if (await portOpen(this.spec.port)) return;
      this.start(0).catch(() => {});
    }, 30000).unref();
  }

  managedPid() {
    if (this.child && this.child.exitCode === null) return this.child.pid;
    if (this.pid && alive(this.pid)) return this.pid;
    return null;
  }

  async status() {
    const up = await portOpen(this.spec.port);
    const pid = this.managedPid();
    let state = 'stopped';
    if (up && pid) state = 'running';
    else if (up) state = 'external';
    else if (pid) state = 'starting';
    else if (this.error) state = 'failed';
    return {
      id: this.spec.id,
      name: this.spec.name,
      title: this.spec.title,
      about: this.spec.about,
      state,
      port: this.spec.port,
      url: `http://127.0.0.1:${this.spec.port}/`,
      error: state === 'failed' ? this.error : null,
    };
  }

  openLog() {
    mkdirSync(LOGS, { recursive: true, mode: 0o700 });
    try {
      if (statSync(this.logFile).size > MAX_LOG) truncateSync(this.logFile, 0);
    } catch {
      /* a new file */
    }
    return openSync(this.logFile, 'a', 0o600);
  }

  spawn() {
    const log = this.openLog();
    const child = spawn(process.execPath, [this.spec.script], {
      cwd: this.spec.dir,
      env: { ...process.env, PORT: String(this.spec.port) },
      stdio: ['ignore', log, log],
      detached: true, // its own process group: it outlives a restart of this page server
    });
    closeSync(log);
    child.unref();
    this.child = child;
    this.pid = null;
    this.error = null;
    mkdirSync(STATE, { recursive: true, mode: 0o700 });
    writeFileSync(this.pidFile, String(child.pid), { mode: 0o600 });
    child.once('error', (err) => {
      this.error = `не запустился: ${err.message}`;
    });
    child.once('exit', (code, signal) => this.exited(child, code, signal));
  }

  exited(child, code, signal) {
    if (this.child !== child) return;
    this.child = null;
    if (!this.wanted) return;
    const now = Date.now();
    this.exits = [...this.exits.filter((t) => now - t < RESTART_WINDOW_MS), now];
    if (this.exits.length > RESTARTS) {
      this.wanted = false;
      this.error = `падал ${RESTARTS} раз за 5 минут (последний код ${code ?? signal}); журнал: ~/Library/Logs/Jarvis/${this.spec.id}.log`;
      return;
    }
    setTimeout(() => {
      if (this.wanted && !this.child) this.start().catch(() => {});
    }, 1000 * 2 ** Math.min(this.exits.length, 5));
  }

  /** Start (or keep) it; resolves with the status once the port answers or after `waitMs`. */
  async start(waitMs = 10000) {
    this.wanted = true;
    this.error = null;
    if (!(await portOpen(this.spec.port)) && !this.managedPid()) this.spawn();
    const until = Date.now() + waitMs;
    while (Date.now() < until && !(await portOpen(this.spec.port))) await sleep(250);
    return this.status();
  }

  async stop() {
    this.wanted = false;
    const pid = this.managedPid();
    if (!pid) {
      if (await portOpen(this.spec.port)) {
        throw Object.assign(new Error('TLT запущен не Jarvis (например, LocalSites) — остановите его там.'), { status: 409 });
      }
      return this.status();
    }
    try {
      process.kill(-pid, 'SIGTERM'); // the whole group: node and anything it started
    } catch {
      try {
        process.kill(pid, 'SIGTERM');
      } catch {
        /* already gone */
      }
    }
    for (let i = 0; i < 20 && (await portOpen(this.spec.port)); i += 1) await sleep(250);
    this.pid = null;
    try {
      unlinkSync(this.pidFile);
    } catch {
      /* no file */
    }
    return this.status();
  }

  async restart() {
    await this.stop().catch(() => {});
    return this.start();
  }
}
