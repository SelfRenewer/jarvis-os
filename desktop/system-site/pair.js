// Jarvis OS — the phone side of pairing: the one-time code is in the link's #fragment (never sent in the address
// to a server or a log); it is taken off the address at once and posted here once.
const $ = (id) => document.getElementById(id);
const code = new URLSearchParams(location.hash.slice(1)).get('c') || '';
history.replaceState(null, '', location.pathname); // the code leaves the address bar and the history

const ua = navigator.userAgent;
$('pair-name').value = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android' : 'Телефон';

function fail(text) {
  $('pair-error').textContent = text;
  $('pair-error').hidden = false;
}

if (!code) {
  $('pair-form').hidden = true;
  $('pair-text').textContent = 'Откройте ссылку из QR-кода: на Mac — Jarvis OS → «Телефон».';
}

$('pair-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('pair-go').disabled = true;
  $('pair-error').hidden = true;
  try {
    const response = await fetch('/api/pair', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ code, name: $('pair-name').value }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    location.replace('/');
  } catch (err) {
    fail(err.message);
    $('pair-go').disabled = false;
  }
});
