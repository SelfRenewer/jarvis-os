// Jarvis OS — the iPhone check page. It only shows the report that device-check.mjs builds on this Mac
// (POST /api/os/device/iphone). The serial number and the IMEIs are masked until «показать» — nothing is stored.

const $ = (id) => document.getElementById(id);
const DASH = '—';
const GRADE = { good: 'is-good', fair: 'is-fair', bad: 'is-bad' };
let revealed = false;
let last = null;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children.flat()) if (child !== null && child !== undefined && child !== false) node.append(child);
  return node;
}

const show = (v) => (v === null || v === undefined || v === '' ? DASH : String(v));
const mask = (v) => (v ? `•••• ${String(v).slice(-4)}` : DASH);
const fmt = (n, digits = 0) => (n === null || n === undefined ? DASH : n.toLocaleString('ru-RU', { maximumFractionDigits: digits, minimumFractionDigits: digits }));
const grade = (g) => (g ? el('span', { class: `grade ${GRADE[g.level] || ''}` }, g.text) : null);
const meter = (value, level) =>
  value === null || value === undefined
    ? null
    : el('span', { class: `meter ${GRADE[level] || ''}`, role: 'presentation' }, el('span', { style: `width:${Math.max(0, Math.min(100, value))}%` }));

function rows(target, list) {
  $(target).replaceChildren(
    ...list.filter(Boolean).flatMap(([label, value, extra]) => [el('dt', {}, label), el('dd', {}, value, extra ? el('span', { class: 'kv-note' }, extra) : null)]),
  );
}

function secret(value) {
  if (!value) return DASH;
  return el('span', { class: 'secret' }, revealed ? value : mask(value));
}

function renderDevice(d) {
  const ios = d.ios ? `iOS ${d.ios}${d.build ? ` (${d.build})` : ''}` : null;
  rows('device', [
    ['Имя', show(d.name)],
    ['Модель', show(d.productType), d.hardwareModel],
    ['Система', show(ios)],
    ['Номер модели', show(d.modelNumber)],
    ['Тип устройства', grade(d.kind) || DASH, d.modelNumber ? `буква «${d.modelNumber[0]}» в начале номера` : null],
    ['Регион', show(d.regionName)],
    ['Серийный номер', secret(d.serial)],
    ['IMEI', secret(d.imei)],
    d.imei2 ? ['IMEI 2', secret(d.imei2)] : null,
    ['Активация', d.activation ? grade({ level: d.activated ? 'good' : 'fair', text: d.activated ? 'активирован' : d.activation }) : DASH],
  ]);
  const toggle = el('button', { type: 'button', class: 'mini', 'aria-pressed': String(revealed), onclick: () => {
    revealed = !revealed;
    render(last);
  } }, revealed ? 'скрыть номера' : 'показать номера'); // prettier-ignore
  $('device').append(el('dt', {}, ''), el('dd', {}, toggle));
}

function renderStorage(s) {
  if (!s) return rows('storage', [['Объём', DASH], ['Свободно', DASH]]);
  rows('storage', [
    ['Объём', s.totalGB === null ? DASH : `${fmt(s.totalGB)} ГБ`],
    ['Свободно', s.freeGB === null ? DASH : `${fmt(s.freeGB, 1)} ГБ`, s.usedPercent === null ? null : `занято ${s.usedPercent}%`],
  ]);
  if (s.usedPercent !== null) $('storage').append(el('dt', {}, ''), el('dd', {}, meter(s.usedPercent, s.usedPercent > 90 ? 'fair' : 'good')));
}

function renderBattery(b) {
  if (!b) return rows('battery', [['Состояние', DASH]]);
  rows('battery', [
    ['Заряд сейчас', b.level === null ? DASH : `${b.level}%`, b.charging ? 'заряжается' : null],
    ['Состояние', b.health === null ? DASH : el('span', {}, `${fmt(b.health, 1)}% `, grade(b.healthGrade))],
    b.health === null ? null : ['', meter(Math.min(100, b.health), b.healthGrade?.level)],
    ['Заводская ёмкость', b.designCapacity === null ? DASH : `${fmt(b.designCapacity)} мА·ч`],
    ['Реальная ёмкость', b.nominalCapacity === null ? DASH : `${fmt(b.nominalCapacity)} мА·ч`],
    ['Циклы зарядки', b.cycles === null ? DASH : el('span', {}, `${fmt(b.cycles)} `, grade(b.cyclesGrade))],
    ['Температура', b.temperature === null ? DASH : el('span', { class: b.hot ? 'grade is-bad' : '' }, `${fmt(b.temperature, 1)} °C`)],
    ['Напряжение', b.voltage === null ? DASH : `${fmt(b.voltage, 2)} В`],
    ['Датчик батареи', b.gaugeStatus === null ? DASH : grade({ level: b.gaugeOk ? 'good' : 'fair', text: b.gaugeOk ? 'исправен' : b.gaugeStatus })],
  ]);
}

function renderGuide(guide) {
  $('manual').replaceChildren(
    ...guide.manualChecklist.map((item, i) =>
      el('li', {}, el('label', {}, el('input', { type: 'checkbox', id: `manual-${i}` }), el('span', {}, item))),
    ),
  );
  $('limits').replaceChildren(...guide.limits.map((item) => el('li', {}, item)));
}

const STATUS_CLASS = { no_tool: 'is-warn', no_device: 'is-warn', not_trusted: 'is-warn', error: 'is-error' };

function render(report) {
  last = report;
  const banner = $('banner');
  const ok = report.status === 'ok';
  banner.hidden = ok;
  $('result').hidden = !ok;
  $('summary').hidden = !ok;
  if (!ok) {
    banner.className = `banner ${STATUS_CLASS[report.status] || 'is-error'}`;
    const hint =
      report.status === 'no_tool'
        ? el('p', {}, 'Установите в Терминале и проверьте снова: ', el('code', {}, report.hint), ' ',
            el('button', { type: 'button', class: 'mini', onclick: (e) => navigator.clipboard?.writeText(report.hint).then(() => (e.target.textContent = 'скопировано')) }, 'копировать'))
        : el('p', {}, report.hint); // prettier-ignore
    banner.replaceChildren(el('p', { class: 'banner-title' }, report.message), hint);
    return;
  }
  const warn = report.warnings.length > 0;
  $('summary').className = `summary ${warn ? 'is-warn' : 'is-ok'}`;
  $('summary').replaceChildren(
    ...[
      el('p', { class: 'summary-title' }, warn ? '⚠ ' : '✔ ', report.message),
      warn ? el('ul', {}, report.warnings.map((w) => el('li', {}, w))) : null,
      report.devices > 1 ? el('p', { class: 'kv-note' }, `Подключено телефонов: ${report.devices} — проверен первый.`) : null,
    ].filter(Boolean),
  );
  renderDevice(report.device);
  renderStorage(report.storage);
  renderBattery(report.battery);
}

async function check() {
  const button = $('check');
  button.disabled = true;
  const started = Date.now();
  $('run-state').textContent = 'Проверяю… обычно 5–15 секунд, телефон должен оставаться разблокированным.';
  try {
    const response = await fetch('/api/os/device/iphone', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
    const report = await response.json();
    if (!response.ok) throw new Error(report.error || `HTTP ${response.status}`);
    revealed = false;
    render(report);
    const time = new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
    $('run-state').textContent = `Проверено в ${time} за ${Math.round((Date.now() - started) / 1000)} с.`;
  } catch (err) {
    render({ status: 'error', message: 'Не получилось проверить', hint: String(err?.message || err) });
    $('run-state').textContent = '';
  } finally {
    button.disabled = false;
  }
}

$('check').addEventListener('click', check);
fetch('/api/os/device/iphone/guide')
  .then((r) => (r.ok ? r.json() : null))
  .then((guide) => guide && renderGuide(guide))
  .catch(() => {});
