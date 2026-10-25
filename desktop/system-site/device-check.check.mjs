// node device-check.check.mjs — the iPhone diagnostics on fixtures (no phone, no tools needed): parsing the plists
// (noise before them, keys at any depth, missing fields, empty answers), the numbers, every threshold, the five
// statuses, and running a real program without a shell within its timeout.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  buildReport, checkIphone, findKey, findTool, gradeCycles, gradeHealth, LIMITS, MANUAL_CHECKLIST, parsePlist, runTool,
} from './device-check.mjs'; // prettier-ignore

const HERE = dirname(fileURLToPath(import.meta.url));
const fixture = (name) => readFileSync(join(HERE, 'fixtures', 'iphone', name), 'utf8');
let checks = 0;
const ok = (cond, what) => {
  assert.ok(cond, what);
  checks += 1;
};
const eq = (a, b, what) => {
  assert.deepEqual(a, b, what);
  checks += 1;
};

// --- the plist --------------------------------------------------------------------------------------------------------
const info = parsePlist(fixture('info.xml'));
eq(info.ProductType, 'iPhone18,2', 'noise before <?xml is skipped');
eq(info.DeviceName, 'Тестовый iPhone & кейс', 'XML entities are decoded');
eq(info.PasswordProtected, true, '<true/>');
eq(info.SupportedDeviceFamilies, [1], 'arrays and integers');
eq(info.WiFiAddressData, 'AAECAwQF', 'data without its whitespace');
eq(parsePlist(''), {}, 'an empty answer');
eq(parsePlist(fixture('not-trusted.txt')), {}, 'an error line, no plist');
eq(parsePlist('<?xml version="1.0"?><plist><dict><key>A</key><integer>1</integer>'), {}, 'a cut-off plist');
eq(parsePlist('<?xml version="1.0"?><plist><dict><key>A</key><string>x</string></dict></plist>trailing junk'), { A: 'x' }, 'junk after it');
eq(parsePlist(null), {}, 'not text at all');
const disk = parsePlist(fixture('disk.xml'));
eq(findKey(disk, 'TotalDiskCapacity'), 256000000000, 'a key two levels down');
eq(findKey(disk, 'Nope'), null, 'a missing key');
eq(findKey(parsePlist(fixture('gasgauge.xml')), 'GasGauge').Status, 'Success', 'a nested dict');

// --- the full report (the values of the task) -------------------------------------------------------------------------
const full = buildReport({
  info,
  disk,
  batt: parsePlist(fixture('battery.xml')),
  ioreg: parsePlist(fixture('ioreg.xml')),
  gauge: parsePlist(fixture('gasgauge.xml')),
});
eq(full.status, 'ok', 'ok');
eq(full.device.productType, 'iPhone18,2', 'the model');
eq(full.device.ios, '27.0.1', 'the iOS version');
eq(full.device.modelNumber, 'MFYQ4ZA/A', 'the model number with its region');
eq(full.device.regionName, 'Сингапур', 'ZA/A → Singapore');
eq(full.device.kind, { level: 'good', text: 'новый, обычная продажа' }, 'M → new');
eq(full.device.serial, 'FAKESERIAL01', 'the serial (fictional)');
eq(full.device.imei2, '350000000000002', 'the second IMEI');
eq(full.device.activated, true, 'activated');
eq(full.storage, { totalGB: 256, freeGB: 100.5, usedPercent: 61 }, 'storage in decimal GB');
eq(full.battery.designCapacity, 4768, 'design capacity');
eq(full.battery.nominalCapacity, 4798, 'NominalChargeCapacity before AppleRawMaxCapacity');
eq(full.battery.health, 100.6, 'health ≈ 100.6 %');
eq(full.battery.healthGrade.level, 'good', '≥ 90 % → excellent');
eq(full.battery.cycles, 171, 'cycles from the controller');
eq(full.battery.cyclesGrade.level, 'good', '< 400 → normal');
eq(full.battery.temperature, 29.5, 'hundredths of °C');
eq(full.battery.hot, false, 'not hot');
eq(full.battery.voltage, 4.01, 'mV → V');
eq([full.battery.level, full.battery.charging], [87, true], 'the charge now');
eq(full.battery.gaugeOk, true, 'the gauge says Success');
eq(full.warnings, [], 'nothing to warn about');
eq(full.manualChecklist, MANUAL_CHECKLIST, 'the manual checklist');
ok(full.manualChecklist.some((s) => s.includes('История деталей')) && full.manualChecklist.some((s) => s.includes('Локатор')), 'parts history and Find My');
eq(full.limits, LIMITS, 'the limits are said');

// --- missing fields: «—» on the page, no crash -------------------------------------------------------------------------
const bare = buildReport({ info: parsePlist(fixture('info-missing.xml')) });
eq(bare.device.serial, null, 'no serial → null');
eq(bare.device.kind, null, 'no model number → no kind');
eq(bare.device.activated, null, 'no activation state → unknown');
eq(bare.storage, null, 'no disk data');
eq(bare.battery.health, null, 'no capacities');
eq(bare.battery.cycles, null, 'no cycles');
eq(bare.warnings, [], 'missing is not a warning');

// --- fallbacks and warnings -------------------------------------------------------------------------------------------
const worn = buildReport({ info, ioreg: parsePlist(fixture('ioreg-raw.xml')) });
eq(worn.battery.nominalCapacity, 3700, 'AppleRawMaxCapacity when there is no NominalChargeCapacity');
eq(worn.battery.health, 77.6, '3700 / 4768');
eq(worn.battery.healthGrade.level, 'bad', '< 80 % → warning');
eq(worn.battery.cyclesGrade.level, 'bad', '850 cycles → warning');
eq(worn.battery.hot, true, '42.1 °C → hot');
eq(worn.warnings.length, 3, 'three warnings: wear, cycles, heat');
const gaugeOnly = buildReport({ info, gauge: parsePlist(fixture('gasgauge.xml')) });
eq(gaugeOnly.battery.health, 96, 'the gauge FullChargeCapacity as the health (as the reference does)');
eq(gaugeOnly.battery.cycles, 171, 'cycles from the gauge');

// --- thresholds -------------------------------------------------------------------------------------------------------
eq([gradeHealth(90).level, gradeHealth(89.9).level, gradeHealth(80).level, gradeHealth(79.9).level], ['good', 'fair', 'fair', 'bad'], 'health 90 / 80');
eq([gradeCycles(399).level, gradeCycles(400).level, gradeCycles(799).level, gradeCycles(800).level], ['good', 'fair', 'fair', 'bad'], 'cycles 400 / 800 (≥ 800 warns, as the reference)');
const kindOf = (ModelNumber) => buildReport({ info: { ModelNumber } });
eq(kindOf('FFYQ4').warnings, ['Номер модели начинается на «F»: восстановленный (refurbished).'], 'F → refurbished, a warning');
eq(kindOf('NFYQ4').warnings.length, 1, 'N → a warranty replacement, a warning');
eq([kindOf('PFYQ4').device.kind.level, kindOf('PFYQ4').warnings.length], ['good', 0], 'P → engraved, fine');
eq([kindOf('XFYQ4').device.kind.text, kindOf('XFYQ4').warnings.length], ['неизвестный тип', 0], 'another letter: shown, no warning');
eq(buildReport({ info: { RegionInfo: 'QQ/A', ModelNumber: 'MX' } }).device.regionName, 'QQ/A', 'an unknown region as it is');
const locked = buildReport({ info: { ActivationState: 'Unactivated' } });
eq([locked.device.activated, locked.warnings], [false, ['Телефон не активирован (ActivationState = Unactivated).']], 'not activated → a warning');

// --- the five statuses (the tools replaced) ---------------------------------------------------------------------------
const UDID = '00008150-0011223344556677';
const answers = {
  '-l': `garbage line\n${UDID}\n`,
  '-x': fixture('info.xml'),
  'com.apple.disk_usage': fixture('disk.xml'),
  'com.apple.mobile.battery': fixture('battery.xml'),
  ioregentry: fixture('ioreg.xml'),
  diagnostics: fixture('gasgauge.xml'),
};
const calls = [];
const fakeRun = (overrides = {}) => async (path, args) => {
  calls.push([path, args]);
  const a = { ...answers, ...overrides };
  const key = ['-l', 'com.apple.disk_usage', 'com.apple.mobile.battery', 'ioregentry', 'diagnostics'].find((k) => args.includes(k)) || '-x';
  return a[key];
};
const find = async (name) => `/opt/homebrew/bin/${name}`;

eq((await checkIphone({ find: async (n) => (n === 'idevicediagnostics' ? null : `/x/${n}`), run: fakeRun() })).status, 'no_tool', 'a tool missing → no_tool');
const noTool = await checkIphone({ find: async () => null, run: fakeRun() });
eq([noTool.status, noTool.hint], ['no_tool', 'brew install libimobiledevice'], 'the install hint, nothing installed');
eq((await checkIphone({ find, run: fakeRun({ '-l': '' }) })).status, 'no_device', 'idevice_id prints nothing → no_device');
eq((await checkIphone({ find, run: fakeRun({ '-l': 'not-a-udid\n' }) })).status, 'no_device', 'a line that is not a UDID');
eq((await checkIphone({ find, run: fakeRun({ '-x': fixture('not-trusted.txt') }) })).status, 'not_trusted', 'ideviceinfo gives nothing → not_trusted');
eq((await checkIphone({ find, run: fakeRun({ '-x': '' }) })).status, 'not_trusted', 'an empty ideviceinfo');
const thrown = await checkIphone({ find, run: async () => { throw new Error('boom'); } }); // prettier-ignore
eq([thrown.status, thrown.hint], ['error', 'boom'], 'anything else → error');
const quiet = [];
const logs = ['log', 'error', 'warn', 'info'].map((k) => [k, console[k]]);
for (const [k] of logs) console[k] = (...a) => quiet.push(a.join(' '));
calls.length = 0;
const good = await checkIphone({ find, run: fakeRun() });
for (const [k, f] of logs) console[k] = f;
eq(good.status, 'ok', 'all well → ok');
eq(good.battery.health, 100.6, 'the same numbers through the runner');
eq(quiet, [], 'nothing is logged (no serial, IMEI or UDID in logs)');
eq(calls.map(([path, args]) => [path.split('/').pop(), args]), [
  ['idevice_id', ['-l']],
  ['ideviceinfo', ['-u', UDID, '-x']],
  ['ideviceinfo', ['-u', UDID, '-q', 'com.apple.disk_usage', '-x']],
  ['ideviceinfo', ['-u', UDID, '-q', 'com.apple.mobile.battery', '-x']],
  ['idevicediagnostics', ['-u', UDID, 'ioregentry', 'AppleSmartBattery']],
  ['idevicediagnostics', ['-u', UDID, 'diagnostics', 'GasGauge']],
], 'the six commands, argument lists, the first valid UDID'); // prettier-ignore

// --- a real program: no shell, a timeout -----------------------------------------------------------------------------
eq(await runTool('/bin/echo', ['$HOME', ';', 'ls']), '$HOME ; ls\n', 'arguments are not seen by a shell');
const t0 = Date.now();
eq(await runTool('/bin/sleep', ['5'], 300), '', 'a hung tool is stopped by the timeout');
ok(Date.now() - t0 < 3000, 'and quickly');
eq(await runTool('/nonexistent/tool', []), '', 'a missing program → empty, no throw');
eq(await findTool('echo', ['/nonexistent', '/bin']), '/bin/echo', 'a tool found in the given folders');
eq(await findTool('idevice_id_nope'), null, 'not found → null');

console.log(`device check: ${checks} checks passed`);
