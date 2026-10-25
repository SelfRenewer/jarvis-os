// Jarvis OS — bound to the user's Mac (17.10, the user: «JarvisOS перед запуском проверяет железо, и если оно
// соответствует моему маку — работает»). The first start keeps a fingerprint of this Mac's hardware UUID
// (IOPlatformUUID; SHA-256 of it — the UUID itself is not stored) in a 0600 file next to the Mac app's config; a later
// start finds a different one on another Mac and the server shows only a «bound to another Mac» door. Moving to a new
// Mac: delete os-device.json there (it binds again on the next start).

import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { chmod, mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

const UUID = /"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]{36})"/;

/** This Mac's hardware UUID (a fixed command, no shell), or null. JARVIS_OS_HARDWARE_ID stands in for tests. */
export function readHardwareId() {
  if (process.env.JARVIS_OS_HARDWARE_ID) return Promise.resolve(process.env.JARVIS_OS_HARDWARE_ID);
  return new Promise((resolve) =>
    execFile('/usr/sbin/ioreg', ['-rd1', '-c', 'IOPlatformExpertDevice'], { timeout: 5000 }, (err, out) => {
      const m = UUID.exec(String(out || ''));
      resolve(err || !m ? null : m[1].toUpperCase());
    }),
  );
}

export const fingerprint = (id) => createHash('sha256').update(`jarvis-os-hardware:${id}`).digest('hex');

/** {ok: true, bound: 'now' | 'before'} or {ok: false, reason: 'no-id' | 'other-mac'}. */
export async function checkHardware(file, readId = readHardwareId) {
  const id = await readId();
  if (!id) return { ok: false, reason: 'no-id' };
  const mine = fingerprint(id);
  let saved = null;
  try {
    saved = JSON.parse(await readFile(file, 'utf8'));
  } catch {
    saved = null;
  }
  if (!saved || typeof saved.hardware !== 'string') {
    await mkdir(dirname(file), { recursive: true, mode: 0o700 });
    await writeFile(file, JSON.stringify({ hardware: mine, boundAt: new Date().toISOString() }), { mode: 0o600 });
    await chmod(file, 0o600);
    return { ok: true, bound: 'now' };
  }
  return saved.hardware === mine ? { ok: true, bound: 'before' } : { ok: false, reason: 'other-mac' };
}
