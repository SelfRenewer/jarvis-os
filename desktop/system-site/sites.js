/* Jarvis OS — «Сайты»: TLT (Jarvis OS keeps it running) and the LocalSites sites with their buttons. Built with DOM
   APIs from JSON the local server returns; every link is checked to be http(s) before it becomes a link. */
'use strict';

(() => {
  const $ = (s) => document.querySelector(s);
  const REFRESH_MS = 5000;
  const STATE = {
    running: ['Работает', 'ok'],
    starting: ['Запускается…', 'warn'],
    stopped: ['Остановлен', ''],
    external: ['Запущен не Jarvis', 'warn'],
    failed: ['Сбой', 'failed'],
    error: ['Ошибка', 'failed'],
  };
  let busy = false;
  let last = null;

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class') el.className = value;
      else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  const safeUrl = (u) => (typeof u === 'string' && /^https?:\/\//i.test(u) ? u : null);

  async function call(url, options) {
    const res = await fetch(url, {
      cache: 'no-store',
      headers: { accept: 'application/json', ...(options && options.body ? { 'content-type': 'application/json' } : {}) },
      ...options,
    });
    let body = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    if (!res.ok) {
      const err = new Error((body && body.error) || `Ошибка ${res.status}`);
      err.body = body;
      err.status = res.status;
      throw err;
    }
    return body;
  }

  const post = (url) => call(url, { method: 'POST', body: '{}' });

  function announce(text) {
    $('#status').textContent = text;
  }

  async function act(button, run, done) {
    if (busy) return;
    busy = true;
    const label = button.textContent;
    button.disabled = true;
    button.textContent = '…';
    try {
      await run();
      announce(done);
    } catch (err) {
      announce(err.message);
      showError(err.message);
    } finally {
      busy = false;
      button.disabled = false;
      button.textContent = label;
      refresh();
    }
  }

  function showError(text) {
    const banner = $('#banner');
    banner.hidden = false;
    banner.className = 'banner is-error';
    banner.replaceChildren(h('p', null, text));
    setTimeout(() => {
      if (banner.classList.contains('is-error')) banner.hidden = true;
    }, 6000);
  }

  function stateBadge(key) {
    const [text, cls] = STATE[key] || [key, ''];
    return h('span', { class: 'site-state' }, h('span', { class: `dot ${cls}` }), text);
  }

  function linkRow(label, url) {
    const safe = safeUrl(url);
    if (!safe) return null;
    return h(
      'p',
      { class: 'site-link' },
      h('span', { class: 'site-link-label' }, label),
      h('a', { href: safe, target: '_blank', rel: 'noopener noreferrer' }, safe.replace(/\?.*$/, '')),
      h(
        'button',
        {
          type: 'button',
          class: 'mini',
          title: 'Скопировать ссылку (с ключом)',
          onclick: () =>
            navigator.clipboard
              .writeText(safe)
              .then(() => announce('Ссылка скопирована'))
              .catch(() => announce('Не получилось скопировать')),
        },
        'Копировать',
      ),
    );
  }

  // --- TLT -----------------------------------------------------------------------------------------------------------

  function serviceCard(s) {
    const running = s.state === 'running' || s.state === 'external';
    const buttons = [];
    if (running) buttons.push(h('a', { class: 'btn primary', href: '/tlt' }, 'Открыть'));
    if (s.state === 'stopped' || s.state === 'failed') {
      buttons.push(h('button', { type: 'button', class: 'btn primary', onclick: (e) => act(e.currentTarget, () => post(`/api/os/services/${s.id}/start`), `${s.name} запущен`) }, 'Запустить'));
    }
    if (s.state === 'running' || s.state === 'starting') {
      buttons.push(h('button', { type: 'button', class: 'btn', onclick: (e) => act(e.currentTarget, () => post(`/api/os/services/${s.id}/restart`), `${s.name} перезапущен`) }, 'Перезапустить'));
      buttons.push(h('button', { type: 'button', class: 'btn', onclick: (e) => act(e.currentTarget, () => post(`/api/os/services/${s.id}/stop`), `${s.name} остановлен`) }, 'Остановить'));
    }
    return h(
      'article',
      { class: 'site-card is-own' },
      h('div', { class: 'site-top' }, h('h3', null, s.title), stateBadge(s.state)),
      h('p', { class: 'site-about' }, s.about),
      h('p', { class: 'site-meta' }, `Порт ${s.port} · запускает Jarvis OS, перезапускает при сбое`),
      s.state === 'external' ? h('p', { class: 'site-note' }, 'Сейчас его запустил кто-то другой (например, LocalSites). Остановите его там — дальше TLT будет держать Jarvis OS.') : null,
      s.error ? h('p', { class: 'site-error' }, s.error) : null,
      h('div', { class: 'site-actions' }, buttons),
    );
  }

  // --- LocalSites ----------------------------------------------------------------------------------------------------

  function siteCard(site) {
    const running = site.status === 'running';
    const buttons = [];
    if (running && safeUrl(site.url)) buttons.push(h('a', { class: 'btn primary', href: safeUrl(site.url), target: '_blank', rel: 'noopener noreferrer' }, 'Открыть'));
    if (!site.missing) {
      buttons.push(
        running
          ? h('button', { type: 'button', class: 'btn', onclick: (e) => act(e.currentTarget, () => post(`/api/os/sites/${encodeURIComponent(site.id)}/stop`), `${site.name} остановлен`) }, 'Остановить')
          : h('button', { type: 'button', class: 'btn primary', onclick: (e) => act(e.currentTarget, () => post(`/api/os/sites/${encodeURIComponent(site.id)}/start`), `${site.name} запущен`) }, 'Запустить'),
      );
    }
    if (running && !site.lan) {
      buttons.push(h('button', { type: 'button', class: 'btn', title: 'Открыть доступ из домашней сети (ссылка с ключом)', onclick: (e) => act(e.currentTarget, () => post(`/api/os/sites/${encodeURIComponent(site.id)}/lan-on`), 'Доступ из дома включён') }, '🏠 Для дома'));
    }
    if (running) {
      buttons.push(
        site.tunnelStatus === 'on' || site.tunnelStatus === 'starting'
          ? h('button', { type: 'button', class: 'btn', onclick: (e) => act(e.currentTarget, () => post(`/api/os/sites/${encodeURIComponent(site.id)}/tunnel-off`), 'Туннель закрыт') }, '🌍 Закрыть туннель')
          : h('button', { type: 'button', class: 'btn', title: 'Ссылка из интернета через cloudflared (с ключом)', onclick: (e) => act(e.currentTarget, () => post(`/api/os/sites/${encodeURIComponent(site.id)}/tunnel-on`), 'Туннель открыт') }, '🌍 Не дома'),
      );
    }
    return h(
      'article',
      { class: `site-card${running ? ' is-running' : ''}`, 'data-name': site.name.toLowerCase() },
      h('div', { class: 'site-top' }, h('h3', null, site.name), stateBadge(site.error ? 'error' : site.status)),
      h('p', { class: 'site-meta' }, `${site.kind === 'command' ? 'Приложение' : 'Сайт'}${site.port ? ` · порт ${site.port}` : ''}`),
      site.missing ? h('p', { class: 'site-error' }, 'Папка не найдена — укажите новую в LocalSites.') : null,
      site.error ? h('p', { class: 'site-error' }, site.error) : null,
      running && site.lan ? linkRow('Дома', site.lanUrl) : null,
      site.tunnelStatus === 'on' ? linkRow('Из интернета', site.tunnelUrl) : null,
      site.tunnelStatus === 'starting' ? h('p', { class: 'site-meta' }, 'Туннель поднимается…') : null,
      site.tunnelError ? h('p', { class: 'site-error' }, `Туннель: ${site.tunnelError}`) : null,
      h('div', { class: 'site-actions' }, buttons),
    );
  }

  function renderSites(state) {
    const filter = $('#filter').value.trim().toLowerCase();
    const sites = state.sites.filter((s) => !s.moved && (!filter || s.name.toLowerCase().includes(filter)));
    const live = sites.filter((s) => !s.archived);
    const groups = new Map();
    for (const site of live) {
      const key = site.group || 'Без группы';
      groups.set(key, [...(groups.get(key) || []), site]);
    }
    const order = [...groups.keys()].sort((a, b) => (a === 'Без группы') - (b === 'Без группы') || a.localeCompare(b, 'ru'));
    const blocks = order.map((group) => {
      const list = groups.get(group).sort((a, b) => (b.status === 'running') - (a.status === 'running') || a.name.localeCompare(b.name, 'ru'));
      return h('section', { class: 'site-group' }, h('h3', { class: 'group-title' }, `${group} · ${list.length}`), h('div', { class: 'site-grid' }, list.map(siteCard)));
    });
    const archived = sites.filter((s) => s.archived);
    if (archived.length) {
      blocks.push(h('details', { class: 'site-group' }, h('summary', null, `В архиве · ${archived.length}`), h('div', { class: 'site-grid' }, archived.map(siteCard))));
    }
    const moved = state.sites.filter((s) => s.moved);
    if (moved.length) {
      blocks.unshift(
        h('p', { class: 'site-note' }, `${moved.map((s) => s.name).join(', ')} теперь запускает Jarvis OS (раздел выше). Карточку в LocalSites можно удалить, чтобы два запуска не спорили за порт.`),
      );
    }
    if (!blocks.length) blocks.push(h('p', { class: 'site-meta' }, filter ? 'Ничего не нашлось.' : 'В LocalSites нет сайтов.'));
    $('#groups').replaceChildren(...blocks);
    const running = state.sites.filter((s) => s.status === 'running' && !s.moved).length;
    return running;
  }

  function renderLocalSitesDown(err) {
    const banner = $('#banner');
    banner.hidden = false;
    banner.className = 'banner';
    const children = [h('p', null, err.message)];
    if (err.body && err.body.launch) {
      children.push(
        h('button', { type: 'button', class: 'btn primary', onclick: (e) => act(e.currentTarget, () => post('/api/os/localsites/launch'), 'LocalSites запускается') }, 'Запустить LocalSites'),
      );
    }
    banner.replaceChildren(...children);
    $('#groups').replaceChildren();
  }

  let timer = null;
  async function refresh() {
    clearTimeout(timer);
    if (!document.hidden) {
      let services = [];
      try {
        services = (await call('/api/os/services')).services || [];
        $('#services').replaceChildren(...services.map(serviceCard));
      } catch (err) {
        $('#services').replaceChildren(h('p', { class: 'site-error' }, err.message));
      }
      let running = null;
      try {
        last = await call('/api/os/sites');
        if (!$('#banner').classList.contains('is-error')) $('#banner').hidden = true;
        running = renderSites(last);
      } catch (err) {
        if (err.status === 401) {
          location.reload();
          return;
        }
        renderLocalSitesDown(err);
      }
      const own = services.filter((s) => s.state === 'running').length;
      $('#sites-sub').textContent =
        running === null
          ? `Jarvis OS: ${own ? 'TLT работает' : 'TLT остановлен'} · LocalSites не отвечает`
          : `Запущено: ${running + own} · обновляется каждые 5 секунд`;
    }
    timer = setTimeout(refresh, REFRESH_MS);
  }

  $('#filter').addEventListener('input', () => last && renderSites(last));
  document.addEventListener('visibilitychange', () => !document.hidden && refresh());
  refresh();
})();
