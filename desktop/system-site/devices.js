// Jarvis OS — «Телефон» (on this Mac only): the home-network door, the tunnel, one-time QR codes, paired devices.
import { qrSvg } from './qr.js';

const $ = (id) => document.getElementById(id);
let state = null;
let qrTimer = null;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  node.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

const when = (iso) =>
  iso ? new Date(iso).toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—';

async function call(path, body) {
  const response = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

async function act(path, body, button) {
  $('banner').hidden = true;
  if (button) button.disabled = true;
  try {
    render(await call(path, body));
  } catch (err) {
    $('banner').textContent = err.message;
    $('banner').hidden = false;
  } finally {
    if (button) button.disabled = false;
  }
}

function render(s) {
  state = s;
  $('lan-state').textContent = s.lan.on
    ? `Включено: ${s.lan.url}`
    : s.lan.away ? 'Mac сейчас не в домашней сети — вход закрыт, дома откроется сам.'
    : s.lanAddress ? `Выключено. Адрес этого Mac в сети: ${s.lanAddress}` : 'Mac сейчас не в домашней сети.';
  $('lan-toggle').textContent = s.lan.on || s.lan.away ? 'Выключить' : 'Включить';
  $('lan-toggle').disabled = !s.lan.on && !s.lanAddress;
  $('lan-qr').disabled = !s.lan.on;

  $('tunnel-state').textContent = s.tunnel.on ? `Открыт до ${when(s.tunnel.until)}: ${s.tunnel.url}` : 'Закрыт.';
  $('tunnel-open').replaceChildren(
    ...s.tunnelHours.map((h) =>
      el('button', { type: 'button', class: 'btn', onclick: (e) => act('/api/os/remote/tunnel', { hours: h }, e.target) }, `Открыть на ${h} ч`),
    ),
  );
  $('tunnel-qr').disabled = !s.tunnel.on;
  $('tunnel-close').disabled = !s.tunnel.on;

  $('devices').replaceChildren(
    ...(s.devices.length
      ? s.devices.map((d) =>
          el('li', { class: 'device' },
            el('div', {}, el('b', {}, d.name), el('span', { class: 'site-meta' }, ` · ${d.via === 'tunnel' ? 'вне дома' : 'дома'} · был ${when(d.seen && new Date(d.seen).toISOString())}`)),
            el('button', { type: 'button', class: 'mini', onclick: (e) => act(`/api/os/remote/devices/${d.id}/revoke`, {}, e.target) }, 'Отвязать')),
        ) // prettier-ignore
      : [el('li', { class: 'site-meta' }, 'Пока ни одного.')]),
  );
}

async function showQr(via, button) {
  $('banner').hidden = true;
  button.disabled = true;
  try {
    const { url, expiresIn } = await call('/api/os/remote/pair', { via });
    const svg = new DOMParser().parseFromString(qrSvg(url, { dark: '#04121c', light: '#ffffff' }), 'image/svg+xml').documentElement;
    $('qr').replaceChildren(svg); // our own SVG of squares: no text from anywhere in it
    $('qr-link').textContent = url;
    $('qr-title').textContent = via === 'tunnel' ? 'Отсканируйте камерой телефона — вне дома' : 'Отсканируйте камерой телефона в домашнем Wi-Fi';
    $('qr-box').hidden = false;
    const until = Date.now() + expiresIn * 1000;
    clearInterval(qrTimer);
    const tick = () => {
      const left = Math.max(0, Math.round((until - Date.now()) / 1000));
      $('qr-timer').textContent = left ? `Код действует ещё ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}, один раз.` : 'Код истёк — покажите новый.';
      if (!left) {
        clearInterval(qrTimer);
        $('qr').replaceChildren();
        $('qr-link').textContent = '';
        refresh();
      }
    };
    tick();
    qrTimer = setInterval(tick, 1000);
  } catch (err) {
    $('banner').textContent = err.message;
    $('banner').hidden = false;
  } finally {
    render(state);
  }
}

async function refresh() {
  try {
    render(await call('/api/os/remote'));
  } catch (err) {
    $('banner').textContent = err.message;
    $('banner').hidden = false;
  }
}

$('lan-toggle').addEventListener('click', (e) => act('/api/os/remote/lan', { on: !(state?.lan.on || state?.lan.away) }, e.target));
$('lan-qr').addEventListener('click', (e) => showQr('lan', e.target));
$('tunnel-qr').addEventListener('click', (e) => showQr('tunnel', e.target));
$('tunnel-close').addEventListener('click', (e) => act('/api/os/remote/tunnel/close', {}, e.target));
$('qr-hide').addEventListener('click', () => {
  clearInterval(qrTimer);
  $('qr-box').hidden = true;
  $('qr').replaceChildren();
});
refresh();
setInterval(() => document.visibilityState === 'visible' && refresh(), 15000); // a phone that just paired shows up
