// Jarvis OS — diagnostics of an iPhone on the cable (17.10). The logic is the user's reference script
// /opt/homebrew/bin/iphone-check, carried over here (not its terminal output): libimobiledevice's tools reach the phone
// through macOS's usbmuxd and lockdownd on the phone, which answers only when the phone is unlocked and has trusted
// this Mac. The tools run without a shell (execFile with a list of arguments), one at a time, each within 40 s. Their
// XML plists are parsed here into one typed result; the page only shows it.
// Privacy: the serial number, the IMEIs and the UDID go only into the result for the signed-in page — never into a
// log, never anywhere else. The phone number is not asked for.

import { execFile } from 'node:child_process';
import { constants } from 'node:fs';
import { access } from 'node:fs/promises';

/**
 * @typedef {'ok' | 'no_tool' | 'no_device' | 'not_trusted' | 'error'} Status
 * @typedef {'good' | 'fair' | 'bad'} Level
 * @typedef {{level: Level, text: string}} Grade
 * @typedef {{
 *   name: string|null, productType: string|null, hardwareModel: string|null, ios: string|null, build: string|null,
 *   modelNumber: string|null, region: string|null, regionName: string|null, kind: Grade|null,
 *   serial: string|null, imei: string|null, imei2: string|null, activation: string|null, activated: boolean|null,
 * }} Device
 * @typedef {{totalGB: number|null, freeGB: number|null, usedPercent: number|null}} Storage
 * @typedef {{
 *   level: number|null, charging: boolean|null, designCapacity: number|null, nominalCapacity: number|null,
 *   health: number|null, healthGrade: Grade|null, cycles: number|null, cyclesGrade: Grade|null,
 *   temperature: number|null, hot: boolean|null, voltage: number|null, gaugeStatus: string|null, gaugeOk: boolean|null,
 * }} Battery
 * @typedef {{
 *   status: Status, message: string, hint: string, devices: number,
 *   device: Device|null, storage: Storage|null, battery: Battery|null,
 *   warnings: string[], manualChecklist: string[], limits: string[],
 * }} Report
 */

export const TOOLS = ['idevice_id', 'ideviceinfo', 'idevicediagnostics'];
/** Where Homebrew puts them — the page server runs with a short PATH (the Mac app starts it). */
export const TOOL_DIRS = ['/opt/homebrew/bin', '/usr/local/bin'];
export const TIMEOUT_MS = 40_000;
const UDID = /^[0-9A-Fa-f-]{20,64}$/;

export const REGIONS = {
  'LL/A': 'США', 'ZA/A': 'Сингапур', 'ZP/A': 'Гонконг', 'CH/A': 'Китай', 'J/A': 'Япония', 'RU/A': 'Россия',
  'B/A': 'Великобритания', 'ZD/A': 'Европа', 'KH/A': 'Корея', 'AE/A': 'ОАЭ', 'HN/A': 'Индия',
}; // prettier-ignore
/** The first letter of the model number: how the phone was sold. */
export const KINDS = {
  M: { level: 'good', text: 'новый, обычная продажа' },
  F: { level: 'fair', text: 'восстановленный (refurbished)' },
  N: { level: 'fair', text: 'замена по гарантии' },
  P: { level: 'good', text: 'новый, с гравировкой' },
};
export const MANUAL_CHECKLIST = [
  'Настройки → Основные → Об этом устройстве → «История деталей и обслуживания»: все детали оригинальные',
  'Настройки → Аккумулятор → Состояние: совпадает с ёмкостью и циклами выше',
  'Экран и тач: битые пиксели, полосы, отклик по всей площади',
  'Face ID, все камеры, вспышка, динамики, микрофоны, вибро, кнопки',
  '«Локатор» выключен, и продавец вышел из Apple ID — при вас',
];
export const LIMITS = [
  'Данные о памяти NAND и Wi-Fi на новых iOS закрыты — через кабель их не получить.',
  'Экран, камеры, Face ID, динамики и кнопки через кабель не проверяются — только руками, по списку ниже.',
  'Телефон, подключённый по Wi-Fi (его видит Finder), эти утилиты обычно не находят — нужен кабель.',
];

// --- XML plist --------------------------------------------------------------------------------------------------------

const ENTITIES = { lt: '<', gt: '>', amp: '&', quot: '"', apos: "'" };
const decode = (s) =>
  s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) =>
    e[0] === '#' ? String.fromCodePoint(e[1] === 'x' || e[1] === 'X' ? parseInt(e.slice(2), 16) : Number(e.slice(1))) : ENTITIES[e] ?? m,
  );

/** An XML plist → JS values. Output before `<?xml` (the tools may print noise) is skipped; anything unreadable → {}. */
export function parsePlist(text) {
  const raw = typeof text === 'string' ? text : '';
  const start = raw.indexOf('<?xml');
  if (start < 0) return {};
  const tokens = [];
  const re = /<!--[\s\S]*?-->|<!DOCTYPE[^>]*>|<\?[\s\S]*?\?>|<(\/?)([A-Za-z]+)[^>]*?(\/?)>|([^<]+)/g;
  for (const m of raw.slice(start).matchAll(re)) {
    if (m[2]) tokens.push({ tag: m[2], close: m[1] === '/', empty: m[3] === '/' });
    else if (m[4] !== undefined) tokens.push({ text: m[4] });
    if (m[2] === 'plist' && m[1] === '/') break; // whatever follows the plist is not ours
  }
  let i = 0;
  const textUntil = (tag) => {
    let s = '';
    while (i < tokens.length && !(tokens[i].tag === tag && tokens[i].close)) s += tokens[i++].text ?? '';
    if (i >= tokens.length) throw new Error(`plist: no </${tag}>`); // cut off: unreadable, like plistlib
    i += 1;
    return decode(s);
  };
  const value = () => {
    while (i < tokens.length && tokens[i].text !== undefined) i += 1; // whitespace between elements
    const t = tokens[i++];
    if (!t) throw new Error('plist ended');
    if (t.tag === 'dict') {
      const out = {};
      if (t.empty) return out;
      for (;;) {
        while (tokens[i] && tokens[i].text !== undefined) i += 1;
        const k = tokens[i];
        if (!k) throw new Error('plist: no </dict>');
        if (k.tag === 'dict' && k.close) {
          i += 1;
          return out;
        }
        if (k.tag !== 'key') throw new Error(`plist: <${k.tag}> where a key was expected`);
        i += 1;
        const key = k.empty ? '' : textUntil('key');
        out[key] = value();
      }
    }
    if (t.tag === 'array') {
      const out = [];
      if (t.empty) return out;
      for (;;) {
        while (tokens[i] && tokens[i].text !== undefined) i += 1;
        if (!tokens[i]) throw new Error('plist: no </array>');
        if (tokens[i].tag === 'array' && tokens[i].close) {
          i += 1;
          return out;
        }
        out.push(value());
      }
    }
    if (t.tag === 'true' || t.tag === 'false') {
      if (!t.empty) textUntil(t.tag);
      return t.tag === 'true';
    }
    const s = t.empty ? '' : textUntil(t.tag);
    if (t.tag === 'integer') return Number.parseInt(s.trim(), 10);
    if (t.tag === 'real') return Number.parseFloat(s.trim());
    if (t.tag === 'data') return s.replace(/\s+/g, '');
    return s; // string, date
  };
  try {
    while (i < tokens.length && tokens[i].tag !== 'plist') i += 1;
    if (i >= tokens.length) return {};
    i += 1;
    const out = value();
    return out && typeof out === 'object' ? out : {};
  } catch {
    return {};
  }
}

/** A key at any depth of a plist (dicts first by their own keys, then down), like the reference script. */
export function findKey(obj, key) {
  if (Array.isArray(obj)) {
    for (const v of obj) {
      const r = findKey(v, key);
      if (r !== undefined && r !== null) return r;
    }
  } else if (obj && typeof obj === 'object') {
    if (Object.hasOwn(obj, key)) return obj[key];
    for (const v of Object.values(obj)) {
      const r = findKey(v, key);
      if (r !== undefined && r !== null) return r;
    }
  }
  return null;
}

// --- the report -------------------------------------------------------------------------------------------------------

const str = (v) => (v === null || v === undefined || v === '' ? null : String(v));
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const round = (v, digits) => Math.round(v * 10 ** digits) / 10 ** digits;

/** @returns {Grade} */
export function gradeHealth(health) {
  if (health >= 90) return { level: 'good', text: 'отлично' };
  if (health >= 80) return { level: 'fair', text: 'нормально' };
  return { level: 'bad', text: 'стоит заменить' };
}
/** @returns {Grade} */
export function gradeCycles(cycles) {
  if (cycles < 400) return { level: 'good', text: 'норма' };
  if (cycles < 800) return { level: 'fair', text: 'средне' };
  return { level: 'bad', text: 'много' };
}

/**
 * The parsed plists → the report.
 * @param {{info: object, disk?: object, batt?: object, ioreg?: object, gauge?: object, devices?: number}} data
 * @returns {Report}
 */
export function buildReport({ info, disk = {}, batt = {}, ioreg = {}, gauge = {}, devices = 1 }) {
  const warnings = [];

  const modelNumber = str(info.ModelNumber);
  const region = str(info.RegionInfo);
  const letter = modelNumber ? modelNumber[0].toUpperCase() : null;
  const kind = letter ? KINDS[letter] || { level: 'fair', text: 'неизвестный тип' } : null;
  if (letter === 'F' || letter === 'N') warnings.push(`Номер модели начинается на «${letter}»: ${kind.text}.`);
  const activation = str(info.ActivationState);
  if (activation && activation !== 'Activated') warnings.push(`Телефон не активирован (ActivationState = ${activation}).`);
  const device = {
    name: str(info.DeviceName),
    productType: str(info.ProductType),
    hardwareModel: str(info.HardwareModel),
    ios: str(info.ProductVersion),
    build: str(info.BuildVersion),
    modelNumber: modelNumber ? modelNumber + (region || '') : null,
    region,
    regionName: region ? REGIONS[region] || region : null,
    kind,
    serial: str(info.SerialNumber),
    imei: str(info.InternationalMobileEquipmentIdentity),
    imei2: str(info.InternationalMobileEquipmentIdentity2),
    activation,
    activated: activation ? activation === 'Activated' : null,
  };

  // storage: decimal gigabytes, like Apple
  const total = num(findKey(disk, 'TotalDiskCapacity'));
  const free = num(findKey(disk, 'AmountDataAvailable'));
  const storage = total
    ? {
        totalGB: Math.round(total / 1e9),
        freeGB: free === null ? null : round(free / 1e9, 1),
        usedPercent: free === null ? null : Math.round(100 - (free / total) * 100),
      }
    : null;

  // the battery: its controller first (ioreg), the gas gauge as the fallback
  const level = num(findKey(batt, 'BatteryCurrentCapacity'));
  const charging = findKey(batt, 'BatteryIsCharging');
  const design = num(findKey(ioreg, 'DesignCapacity'));
  const nominal = num(findKey(ioreg, 'NominalChargeCapacity')) || num(findKey(ioreg, 'AppleRawMaxCapacity'));
  let health = null;
  if (design && nominal) health = round((nominal / design) * 100, 1);
  else if (num(findKey(gauge, 'FullChargeCapacity')) !== null) health = num(findKey(gauge, 'FullChargeCapacity'));
  const healthGrade = health === null ? null : gradeHealth(health);
  if (healthGrade?.level === 'bad') warnings.push(`Износ батареи: осталось ${Math.round(health)}% ёмкости.`);

  let cycles = num(findKey(ioreg, 'CycleCount'));
  if (cycles === null) cycles = num(findKey(gauge, 'CycleCount'));
  const cyclesGrade = cycles === null ? null : gradeCycles(cycles);
  if (cyclesGrade?.level === 'bad') warnings.push(`Много циклов зарядки: ${cycles}.`);

  const rawTemp = findKey(ioreg, 'Temperature');
  const temperature = Number.isInteger(rawTemp) && rawTemp > 0 ? round(rawTemp / 100, 1) : null; // hundredths of °C
  const hot = temperature === null ? null : temperature > 40;
  if (hot) warnings.push(`Батарея горячая: ${temperature.toFixed(1).replace('.', ',')} °C.`);
  const rawVolt = findKey(ioreg, 'Voltage');
  const voltage = Number.isInteger(rawVolt) && rawVolt > 0 ? round(rawVolt / 1000, 2) : null; // mV → V
  const gaugeStatus = str(findKey(findKey(gauge, 'GasGauge') || {}, 'Status'));

  const battery = {
    level,
    charging: typeof charging === 'boolean' ? charging : null,
    designCapacity: design,
    nominalCapacity: nominal,
    health,
    healthGrade,
    cycles,
    cyclesGrade,
    temperature,
    hot,
    voltage,
    gaugeStatus,
    gaugeOk: gaugeStatus ? gaugeStatus === 'Success' : null,
  };

  return {
    status: 'ok',
    message: warnings.length ? 'Есть на что обратить внимание' : 'По данным через кабель проблем не найдено',
    hint: '',
    devices,
    device,
    storage,
    battery,
    warnings,
    manualChecklist: MANUAL_CHECKLIST,
    limits: LIMITS,
  };
}

/** @returns {Report} */
function failure(status, message, hint, devices = 0) {
  return { status, message, hint, devices, device: null, storage: null, battery: null, warnings: [], manualChecklist: MANUAL_CHECKLIST, limits: LIMITS };
}

// --- running the tools ------------------------------------------------------------------------------------------------

/** The full path of a tool in the Homebrew folders, or null. */
export async function findTool(name, dirs = TOOL_DIRS) {
  for (const dir of dirs) {
    const path = `${dir}/${name}`;
    try {
      await access(path, constants.X_OK);
      return path;
    } catch {
      /* not here */
    }
  }
  return null;
}

/** One tool, no shell, within the timeout; its stdout ('' when it failed with nothing to say). */
export function runTool(path, args, timeout = TIMEOUT_MS) {
  return new Promise((resolve) => {
    execFile(path, args, { timeout, maxBuffer: 16 * 1024 * 1024, encoding: 'utf8' }, (err, stdout) =>
      resolve(err && !stdout ? '' : String(stdout || '')),
    );
  });
}

/**
 * The check. `find` and `run` can be replaced (tests).
 * @returns {Promise<Report>}
 */
export async function checkIphone({ find = findTool, run = runTool } = {}) {
  try {
    const tools = {};
    for (const name of TOOLS) tools[name] = await find(name);
    if (TOOLS.some((name) => !tools[name])) {
      return failure('no_tool', 'Не установлен libimobiledevice', 'brew install libimobiledevice');
    }
    const udids = (await run(tools.idevice_id, ['-l'])).split(/\s+/).filter((u) => UDID.test(u));
    if (!udids.length) {
      return failure(
        'no_device',
        'iPhone не найден',
        'Подключите телефон кабелем и разблокируйте его. По Wi-Fi эти утилиты телефон обычно не видят.',
      );
    }
    const u = ['-u', udids[0]];
    const info = parsePlist(await run(tools.ideviceinfo, [...u, '-x']));
    if (!Object.keys(info).length) {
      return failure(
        'not_trusted',
        'Телефон виден, но не отдаёт данные',
        'Разблокируйте его и нажмите «Доверять этому компьютеру», затем проверьте снова.',
        udids.length,
      );
    }
    const disk = parsePlist(await run(tools.ideviceinfo, [...u, '-q', 'com.apple.disk_usage', '-x']));
    const batt = parsePlist(await run(tools.ideviceinfo, [...u, '-q', 'com.apple.mobile.battery', '-x']));
    const ioreg = parsePlist(await run(tools.idevicediagnostics, [...u, 'ioregentry', 'AppleSmartBattery']));
    const gauge = parsePlist(await run(tools.idevicediagnostics, [...u, 'diagnostics', 'GasGauge']));
    return buildReport({ info, disk, batt, ioreg, gauge, devices: udids.length });
  } catch (err) {
    return failure('error', 'Не получилось проверить', String(err?.message || err).slice(0, 200));
  }
}
