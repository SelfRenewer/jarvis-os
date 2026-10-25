/* Jarvis OS — the page: the map (a 2D scheme by default, map2d.js; the 3D graph by 3d-force-graph on demand), the live
   layer, search, the node card and «Спросить».
   Everything is built with DOM APIs from data the local server proxies; no HTML from the data is trusted. */
'use strict';

(() => {
  const $ = (selector) => document.querySelector(selector);
  const root = getComputedStyle(document.documentElement);
  const cssVar = (name) => root.getPropertyValue(name).trim();

  const TYPE_ORDER = ['core', 'agent', 'command', 'job', 'integration', 'data', 'channel'];
  const COLORS = Object.fromEntries(TYPE_ORDER.map((t) => [t, cssVar(`--t-${t}`)]));
  const FAIL = cssVar('--fail');
  const BG = '#060a12';
  const ONE = {
    core: 'Ядро',
    agent: 'Агент',
    command: 'Команда',
    job: 'Фоновая задача',
    integration: 'Интеграция',
    data: 'Таблица',
    channel: 'Канал',
  };
  const INCOMING = {
    routes: 'Распознаёт его',
    handled_by: 'Его вызывают',
    uses: 'Его используют',
    enqueues: 'Ставят в очередь',
    schedules: 'Запускает по расписанию',
    runs: 'Работают через него',
    stores: 'Хранят здесь',
    refers: 'Ссылаются сюда',
    feeds: 'Получает данные от',
  };
  const STATE_TEXT = {
    ok: 'Работает',
    failed: 'Сбой',
    warn: 'Есть проблемы',
    idle: 'Нет активности',
    off: 'Не подключено',
  };
  const ROLES = { worker: 'исполнитель', scheduler: 'расписание', bot: 'бот', caller: 'звонарь' };
  const COUNTS = [
    ['open_tasks', 'Открытых задач'],
    ['events_today', 'Событий сегодня'],
    ['reminders_today', 'Напоминаний сегодня'],
    ['pending_approvals', 'Ждут подтверждения'],
    ['running_jobs', 'Выполняется сейчас'],
    ['dead_jobs_24h', 'Сбоев за сутки'],
    ['sent_24h', 'Сообщений за сутки'],
    ['shopping_open', 'В списке покупок'],
  ];
  const HUD_TITLES = {
    listening: 'Jarvis слушает',
    thinking: 'Jarvis думает над вопросом',
    speaking: 'Jarvis отвечает голосом',
    offline: 'Нет связи с Jarvis',
  };
  const LIVE_EVERY_MS = 10000;
  const VIEW_KEY = 'jarvis-os-view';
  const MAX_CHIPS = 18;

  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const phone = matchMedia('(max-width: 760px)');

  const state = {
    graph: null,
    nodes: new Map(), // id → node object (the graph library adds x, y, z)
    neighbors: new Map(), // id → Set of ids
    outgoing: new Map(), // id → [{other, kinds}]
    incoming: new Map(),
    hidden: new Set(),
    selected: null,
    matches: null, // Set of ids while searching
    live: null,
    liveError: null,
    view: 'none', // 'node' | 'ask' | 'none'
    lastAsk: null,
    hud: 'listening',
    overlay: new Map(), // id → {label, beacon}
    mode: '2d', // '2d' | '3d'
  };
  let FG = null;
  let MAP2D = null;
  let GRAPH2D = null; // the Obsidian-like graph (graph2d.js, 17.10)

  // --- small helpers ------------------------------------------------------------------------------------------

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class') el.className = value;
      else if (key === 'style') for (const [p, v] of Object.entries(value)) el.style.setProperty(p, v);
      else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  const escapeHtml = (text) =>
    String(text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const fold = (text) => String(text || '').toLowerCase().replace(/ё/g, 'е');
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  function mix(hex, other, t) {
    const a = parseInt(hex.slice(1), 16);
    const b = parseInt(other.slice(1), 16);
    const ch = (x, s) => (x >> s) & 255;
    const m = (s) => Math.round(ch(a, s) * (1 - t) + ch(b, s) * t);
    return `#${((m(16) << 16) | (m(8) << 8) | m(0)).toString(16).padStart(6, '0')}`;
  }

  const relative = new Intl.RelativeTimeFormat('ru', { numeric: 'auto' });
  function ago(iso) {
    if (!iso) return '—';
    const seconds = (new Date(iso).getTime() - Date.now()) / 1000;
    const abs = Math.abs(seconds);
    if (abs < 60) return relative.format(Math.round(seconds), 'second');
    if (abs < 3600) return relative.format(Math.round(seconds / 60), 'minute');
    if (abs < 86400) return relative.format(Math.round(seconds / 3600), 'hour');
    return relative.format(Math.round(seconds / 86400), 'day');
  }

  function plural(n, one, few, many) {
    const m10 = n % 10;
    const m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }

  async function getJSON(url, options) {
    const response = await fetch(url, { cache: 'no-store', headers: { accept: 'application/json' }, ...options });
    let body = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    if (!response.ok) throw new Error((body && body.error) || `Ошибка ${response.status}`);
    return body;
  }

  const live = (id) => (state.live && state.live.nodes && state.live.nodes[id]) || null;
  const idOf = (end) => (typeof end === 'object' ? end.id : end);

  // --- data -----------------------------------------------------------------------------------------------------

  function ingest(graph) {
    state.graph = graph;
    const pairs = new Set(graph.links.map((l) => `${l.source}|${l.target}`));
    for (const node of graph.nodes) {
      state.nodes.set(node.id, node);
      state.neighbors.set(node.id, new Set());
      state.outgoing.set(node.id, []);
      state.incoming.set(node.id, []);
    }
    for (const link of graph.links) {
      link.curved = pairs.has(`${link.target}|${link.source}`);
      state.neighbors.get(link.source).add(link.target);
      state.neighbors.get(link.target).add(link.source);
      state.outgoing.get(link.source).push({ other: link.target, kinds: link.kinds });
      state.incoming.get(link.target).push({ other: link.source, kinds: link.kinds });
    }
  }

  function focusSet() {
    if (state.selected) return new Set([state.selected, ...state.neighbors.get(state.selected)]);
    return state.matches;
  }

  // --- graph styling ---------------------------------------------------------------------------------------------

  const nodeVal = (n) => 0.5 + n.degree * 0.4;

  function baseColor(n) {
    const info = live(n.id);
    if (info && info.state === 'failed') return FAIL;
    const color = COLORS[n.type] || '#ffffff';
    return info && info.state === 'off' ? mix(color, BG, 0.55) : color;
  }

  let currentFocus = null;
  function nodeColor(n) {
    const color = baseColor(n);
    if (currentFocus && !currentFocus.has(n.id)) return mix(color, BG, 0.84);
    return color;
  }

  function jobActive(id) {
    const info = live(id);
    return Boolean(info && (info.active || info.running));
  }

  function linkActive(l) {
    const s = idOf(l.source);
    const t = idOf(l.target);
    return (s.startsWith('job:') && jobActive(s)) || (t.startsWith('job:') && jobActive(t));
  }

  function linkFailed(l) {
    const s = live(idOf(l.source));
    const t = live(idOf(l.target));
    return (s && s.state === 'failed' && idOf(l.source).startsWith('job:')) ||
      (t && t.state === 'failed' && idOf(l.target).startsWith('job:'));
  }

  function linkColor(l) {
    const s = idOf(l.source);
    const t = idOf(l.target);
    if (currentFocus) {
      const lit = state.selected ? s === state.selected || t === state.selected : currentFocus.has(s) && currentFocus.has(t);
      return lit ? 'rgba(127,216,255,0.85)' : 'rgba(120,150,190,0.035)';
    }
    if (linkFailed(l)) return 'rgba(255,77,77,0.55)';
    if (linkActive(l)) return 'rgba(127,216,255,0.55)';
    return 'rgba(140,170,210,0.15)';
  }

  const linkWidth = (l) =>
    state.selected && (idOf(l.source) === state.selected || idOf(l.target) === state.selected) ? 0.45 : 0;
  const particles = (l) => (!reduced.matches && linkActive(l) ? 3 : 0);
  const particleColor = (l) => (linkFailed(l) ? FAIL : cssVar('--hud'));
  const visible = (n) => !state.hidden.has(n.type);

  function tooltip(n) {
    const degree = `${n.degree} ${plural(n.degree, 'связь', 'связи', 'связей')}`;
    return `<b>${escapeHtml(n.label)}</b><span class="tip-kind">${escapeHtml(ONE[n.type] || n.type)}, ${degree}</span>`;
  }

  function restyle() {
    if (MAP2D && state.mode === '2d') MAP2D.restyle();
    if (GRAPH2D && state.mode === 'graph') GRAPH2D.restyle();
    if (!FG) return;
    currentFocus = focusSet();
    FG.nodeColor(nodeColor)
      .linkColor(linkColor)
      .linkWidth(linkWidth)
      .linkDirectionalParticles(particles)
      .linkDirectionalParticleColor(particleColor);
  }

  function webglAvailable() {
    try {
      const canvas = document.createElement('canvas');
      return Boolean(canvas.getContext('webgl2') || canvas.getContext('webgl'));
    } catch {
      return false;
    }
  }

  function startGraph() {
    if (typeof ForceGraph3D !== 'function' || !webglAvailable()) {
      $('#updated').textContent = 'В этом браузере нет WebGL: 3D-карта не включилась, поиск и карточки работают.';
      $('#updated').classList.add('is-error');
      return;
    }
    const el = $('#graph');
    const options = { controlType: 'orbit' };
    try {
      FG = new ForceGraph3D(el, options);
    } catch {
      FG = ForceGraph3D(options)(el);
    }
    const nodes = state.graph.nodes;
    const links = state.graph.links.map((l) => ({ ...l }));
    FG.backgroundColor('rgba(0,0,0,0)')
      .showNavInfo(false)
      .width(window.innerWidth)
      .height(window.innerHeight)
      .nodeId('id')
      .nodeRelSize(4)
      .nodeVal(nodeVal)
      .nodeOpacity(0.95)
      .nodeResolution(14)
      .nodeLabel(tooltip)
      .nodeVisibility(visible)
      .linkVisibility((l) => visible(state.nodes.get(idOf(l.source))) && visible(state.nodes.get(idOf(l.target))))
      .linkOpacity(1)
      .linkCurvature((l) => (l.curved ? 0.22 : 0))
      .linkDirectionalParticleWidth(2.2)
      .linkDirectionalParticleSpeed(0.006)
      .onNodeClick((n) => focusNode(n.id))
      .onBackgroundClick(() => clearSelection())
      .warmupTicks(reduced.matches ? 300 : 30)
      .cooldownTime(reduced.matches ? 0 : 8000)
      .onEngineStop(() => fit(700));
    FG.d3Force('charge').strength(-75);
    FG.d3Force('link').distance((l) => (idOf(l.source) === 'core:bot' ? 70 : 34));
    restyle();
    FG.graphData({ nodes, links });
    const controls = FG.controls();
    if (controls && controls.addEventListener) controls.addEventListener('start', () => (state.userMoved = true));
    if (reduced.matches) requestAnimationFrame(() => fit(0));
    else setTimeout(() => fit(900), 2200);
    window.addEventListener('resize', sizeGraph);
    requestAnimationFrame(frame);
  }

  /** The whole map in view — until the person has turned or zoomed it themselves. */
  function fit(ms) {
    if (!FG || state.userMoved || state.selected) return;
    FG.zoomToFit(reduced.matches ? 0 : ms, phone.matches ? 16 : 70);
  }

  /** On a phone an open card covers the bottom: the canvas then shrinks to the part above it. */
  function sizeGraph() {
    if (!FG) return;
    const card = $('#card');
    let height = window.innerHeight;
    if (phone.matches && !card.hidden && card.classList.contains('is-open')) {
      height = Math.max(160, window.innerHeight - card.getBoundingClientRect().height);
    }
    FG.width(window.innerWidth).height(height);
  }

  /** How far the camera stays from a chosen node: a share of the map's size. */
  function viewDistance(node) {
    let radius = 0;
    for (const n of state.nodes.values()) {
      if (n.x !== undefined) radius = Math.max(radius, Math.hypot(n.x, n.y, n.z));
    }
    return clamp(radius * 0.65, 110, 480) + Math.cbrt(nodeVal(node)) * 4;
  }

  // --- labels over the graph (hubs, the selection, failures) and beacons on running jobs ------------------------

  function overlayIds() {
    const ids = new Map(); // id → {hub, failed, selected, running}
    const mark = (id, key) => {
      if (!state.nodes.has(id)) return;
      ids.set(id, { ...(ids.get(id) || {}), [key]: true });
    };
    (state.graph.hubs || []).slice(0, phone.matches ? 6 : 10).forEach((id) => mark(id, 'hub'));
    if (state.selected) {
      mark(state.selected, 'selected');
      const around = [...state.neighbors.get(state.selected)];
      if (around.length <= 24) around.forEach((id) => mark(id, 'near'));
    } else if (state.matches && state.matches.size <= 14) {
      state.matches.forEach((id) => mark(id, 'near'));
    }
    for (const [id, info] of Object.entries((state.live && state.live.nodes) || {})) {
      if (info.state === 'failed' && id.startsWith('job:')) mark(id, 'failed');
      if (info.running) mark(id, 'running');
    }
    return ids;
  }

  function rebuildOverlay() {
    if (!FG) return;
    const layer = $('#labels');
    const wanted = overlayIds();
    for (const [id, entry] of state.overlay) {
      if (!wanted.has(id)) {
        entry.label.remove();
        if (entry.beacon) entry.beacon.remove();
        state.overlay.delete(id);
      }
    }
    for (const [id, flags] of wanted) {
      const node = state.nodes.get(id);
      let entry = state.overlay.get(id);
      if (!entry) {
        entry = { label: h('div', { class: 'label' }), beacon: null };
        layer.append(entry.label);
        state.overlay.set(id, entry);
      }
      entry.flags = flags;
      entry.width = 0; // measured again on the next frame
      entry.label.textContent = node.label.length > 34 ? `${node.label.slice(0, 33)}…` : node.label;
      entry.label.className = [
        'label',
        flags.hub ? 'is-hub' : '',
        flags.selected ? 'is-selected' : '',
        flags.failed ? 'is-failed' : '',
      ].join(' ').trim();
      if (flags.running && !entry.beacon) {
        entry.beacon = h('div', { class: 'beacon' });
        layer.append(entry.beacon);
      } else if (!flags.running && entry.beacon) {
        entry.beacon.remove();
        entry.beacon = null;
      }
    }
  }

  const PRIORITY = (flags) => (flags.selected ? 4 : flags.failed ? 3 : flags.hub ? 2 : 1);

  function frame() {
    if (FG && !document.hidden && state.overlay.size) {
      const camera = FG.camera();
      const e = camera.matrixWorldInverse.elements;
      const orbit = Math.hypot(camera.position.x, camera.position.y, camera.position.z) || 1;
      const width = window.innerWidth;
      const height = FG.height();
      const placed = []; // boxes of labels already shown this frame
      const order = [...state.overlay].sort(
        (a, b) => PRIORITY(b[1].flags) - PRIORITY(a[1].flags) || state.nodes.get(b[0]).degree - state.nodes.get(a[0]).degree,
      );
      for (const [id, entry] of order) {
        const n = state.nodes.get(id);
        const hide = () => {
          entry.label.style.display = 'none';
          if (entry.beacon) entry.beacon.style.display = 'none';
        };
        if (!n || n.x === undefined || !visible(n)) {
          hide();
          continue;
        }
        const depth = e[2] * n.x + e[6] * n.y + e[10] * n.z + e[14]; // < 0: in front of the camera
        if (depth >= 0) {
          hide();
          continue;
        }
        const p = FG.graph2ScreenCoords(n.x, n.y, n.z);
        if (p.x < -40 || p.y < -40 || p.x > width + 40 || p.y > height + 40) {
          hide();
          continue;
        }
        const fade = entry.flags.selected || entry.flags.failed ? 1 : clamp(1.35 - -depth / orbit / 1.6, 0.45, 1);
        entry.width = entry.width || entry.label.textContent.length * 6.8 + 6; // no layout reads per frame
        const box = { l: p.x - entry.width / 2, r: p.x + entry.width / 2, t: p.y - 26, b: p.y - 8 };
        const covered = placed.some((o) => box.l < o.r && box.r > o.l && box.t < o.b && box.b > o.t);
        entry.label.style.display = covered && !entry.flags.selected ? 'none' : '';
        if (!covered || entry.flags.selected) placed.push(box);
        entry.label.style.opacity = String(fade);
        entry.label.style.transform = `translate(${p.x.toFixed(1)}px, ${p.y.toFixed(1)}px) translate(-50%, -100%)`;
        if (entry.beacon) {
          entry.beacon.style.display = '';
          entry.beacon.style.setProperty('--at', `translate(${p.x.toFixed(1)}px, ${p.y.toFixed(1)}px)`);
          if (reduced.matches) entry.beacon.style.transform = `translate(${p.x.toFixed(1)}px, ${p.y.toFixed(1)}px)`;
        }
      }
    }
    requestAnimationFrame(frame);
  }

  // --- selection and the camera ------------------------------------------------------------------------------------

  function focusNode(id, { fly = true } = {}) {
    const node = state.nodes.get(id);
    if (!node) return;
    if (state.hidden.has(node.type)) {
      state.hidden.delete(node.type);
      applyVisibility();
    }
    state.selected = id;
    state.view = 'node';
    if (MAP2D && state.mode === '2d') MAP2D.select(id);
    if (GRAPH2D && state.mode === 'graph') GRAPH2D.select(id);
    restyle();
    rebuildOverlay();
    renderCard();
    if (FG && state.mode === '3d' && fly && node.x !== undefined) {
      const distance = viewDistance(node);
      const radius = Math.hypot(node.x, node.y, node.z) || 1;
      const k = 1 + distance / radius;
      FG.cameraPosition(
        { x: node.x * k, y: node.y * k, z: node.z * k },
        { x: node.x, y: node.y, z: node.z },
        reduced.matches ? 0 : 1100,
      );
    }
  }

  function clearSelection() {
    if (MAP2D && state.mode === '2d') MAP2D.clear();
    if (GRAPH2D && state.mode === 'graph') GRAPH2D.clear();
    if (!state.selected && state.view !== 'node') return;
    state.selected = null;
    if (state.view === 'node') closeCard();
    restyle();
    rebuildOverlay();
  }

  function applyVisibility() {
    if (FG) FG.nodeVisibility(visible).linkVisibility(FG.linkVisibility());
    if (MAP2D) MAP2D.rebuild();
    if (GRAPH2D) GRAPH2D.rebuild();
    renderLegend();
  }

  // --- the map panel: search, layers, hubs ------------------------------------------------------------------------

  function kindDot(type) {
    return h('span', { class: 'swatch', style: { '--c': COLORS[type] || '#fff' } });
  }

  function nodeRow(id, extra) {
    const n = state.nodes.get(id);
    return h(
      'button',
      { type: 'button', class: 'row', onclick: () => focusNode(id) },
      kindDot(n.type),
      h('span', { class: 'name' }, n.label),
      extra,
    );
  }

  function renderLegend() {
    const list = $('#legend');
    list.replaceChildren(
      ...state.graph.types
        .slice()
        .sort((a, b) => TYPE_ORDER.indexOf(a.id) - TYPE_ORDER.indexOf(b.id))
        .map((t) =>
          h(
            'li',
            null,
            h(
              'button',
              {
                type: 'button',
                class: 'row',
                'aria-pressed': String(!state.hidden.has(t.id)),
                title: state.hidden.has(t.id) ? 'Показать слой' : 'Скрыть слой',
                onclick: () => {
                  if (state.hidden.has(t.id)) state.hidden.delete(t.id);
                  else state.hidden.add(t.id);
                  if (state.selected && state.hidden.has(state.nodes.get(state.selected).type)) clearSelection();
                  applyVisibility();
                },
              },
              kindDot(t.id),
              h('span', { class: 'name' }, t.label),
              h('span', { class: 'num' }, t.count),
            ),
          ),
        ),
    );
  }

  function renderHubs() {
    $('#hubs').replaceChildren(
      ...(state.graph.hubs || []).slice(0, 8).map((id) => {
        const n = state.nodes.get(id);
        return h('li', null, nodeRow(id, h('span', { class: 'num' }, n.degree)));
      }),
    );
  }

  function searchText(n) {
    const extra = n.extra || {};
    const examples = (extra.examples || []).map((e) => e.text).join(' ');
    return fold([n.label, n.id, n.description, examples, extra.intent, extra.job_type, extra.model].join(' '));
  }

  function runSearch(query) {
    const q = fold(query).trim();
    const results = $('#results');
    if (!q) {
      state.matches = null;
      results.replaceChildren();
    } else {
      const scored = [];
      for (const n of state.nodes.values()) {
        const label = fold(n.label);
        let score = 0;
        if (label.startsWith(q)) score = 3;
        else if (label.includes(q)) score = 2;
        else if (n.search.includes(q)) score = 1;
        if (score) scored.push([score, n]);
      }
      scored.sort((a, b) => b[0] - a[0] || b[1].degree - a[1].degree);
      state.matches = new Set(scored.map(([, n]) => n.id));
      results.replaceChildren(
        ...(scored.length
          ? scored.slice(0, 8).map(([, n]) => h('li', null, nodeRow(n.id, h('span', { class: 'kind' }, ONE[n.type]))))
          : [h('li', { class: 'hint' }, 'Ничего не нашлось. Попробуйте другое слово.')]),
      );
    }
    if (!state.selected) {
      restyle();
      rebuildOverlay();
    }
  }

  // --- the card ---------------------------------------------------------------------------------------------------

  function openSheet(id) {
    for (const sheet of document.querySelectorAll('.sheet')) sheet.classList.toggle('is-open', sheet.id === id);
  }

  function showCard({ keepScroll = false } = {}) {
    const card = $('#card');
    const wasHidden = card.hidden;
    card.hidden = false;
    document.body.classList.add('card-open');
    if (state.mode !== '3d' && !phone.matches) $('#panel').classList.remove('is-open'); // the card takes its place
    if (phone.matches) openSheet('card');
    if (!keepScroll) card.scrollTop = 0;
    if (wasHidden || !keepScroll) sizeGraph();
  }

  function closeCard() {
    $('#card').hidden = true;
    $('#card').classList.remove('is-open');
    document.body.classList.remove('card-open');
    sizeGraph();
    state.view = 'none';
    if (state.selected) {
      state.selected = null;
      if (MAP2D && state.mode === '2d') MAP2D.clear();
      if (GRAPH2D && state.mode === 'graph') GRAPH2D.clear();
      restyle();
      rebuildOverlay();
    }
  }

  // --- the view: the 2D scheme or the 3D graph -----------------------------------------------------------------------

  function savedView() {
    try {
      const saved = localStorage.getItem(VIEW_KEY);
      return saved === '3d' || saved === 'graph' ? saved : '2d';
    } catch {
      return '2d';
    }
  }

  function setView(mode) {
    state.mode = mode;
    try {
      localStorage.setItem(VIEW_KEY, mode);
    } catch {
      /* private window: the choice lasts for this page only */
    }
    document.body.classList.toggle('view-2d', mode === '2d' || mode === 'graph'); // the flat views share the layout
    document.body.classList.toggle('view-graph', mode === 'graph');
    document.body.classList.toggle('view-3d', mode === '3d');
    for (const button of document.querySelectorAll('[data-view]')) button.setAttribute('aria-pressed', String(button.dataset.view === mode));
    $('#graph').setAttribute('aria-hidden', String(mode !== '3d'));
    if (mode !== 'graph' && GRAPH2D) GRAPH2D.hide();
    if (mode === 'graph') {
      if (FG) FG.pauseAnimation();
      if (MAP2D) MAP2D.hide();
      if (!GRAPH2D) {
        GRAPH2D = window.JarvisGraph2D.create($('#graph2d'), {
          nodes: state.nodes,
          outgoing: state.outgoing,
          incoming: state.incoming,
          hidden: state.hidden,
          live,
          selected: () => state.selected,
          matches: () => state.matches,
          select: (id) => focusNode(id),
          clear: () => clearSelection(),
          reduced,
        });
      }
      GRAPH2D.show();
    } else if (mode === '2d') {
      if (FG) FG.pauseAnimation();
      if (!MAP2D) {
        MAP2D = window.JarvisMap2D.create($('#map2d'), {
          nodes: state.nodes,
          outgoing: state.outgoing,
          incoming: state.incoming,
          linkKinds: state.graph.link_kinds || {},
          hidden: state.hidden,
          live,
          selected: () => state.selected,
          matches: () => state.matches,
          select: (id) => focusNode(id),
          clear: () => clearSelection(),
        });
      }
      MAP2D.show();
      if (state.selected) MAP2D.select(state.selected);
    } else {
      if (MAP2D) MAP2D.hide();
      if (!FG) startGraph();
      else FG.resumeAnimation();
    }
    restyle();
    rebuildOverlay();
  }

  function facts(rows) {
    const items = rows.filter(([, value]) => value !== undefined && value !== null && value !== '' && !(Array.isArray(value) && !value.length));
    if (!items.length) return null;
    return h('dl', { class: 'facts' }, items.flatMap(([term, value]) => [h('dt', null, term), h('dd', null, Array.isArray(value) ? value.join(', ') : value)]));
  }

  function chipsFor(entries) {
    const ids = entries.map((e) => e.other).filter((id) => state.nodes.has(id));
    ids.sort((a, b) => TYPE_ORDER.indexOf(state.nodes.get(a).type) - TYPE_ORDER.indexOf(state.nodes.get(b).type));
    const chip = (id) =>
      h('li', null, h('button', { type: 'button', class: 'chip', onclick: () => focusNode(id) }, kindDot(state.nodes.get(id).type), h('span', null, state.nodes.get(id).label)));
    const list = h('ul', { class: 'chips' }, ids.slice(0, MAX_CHIPS).map(chip));
    if (ids.length <= MAX_CHIPS) return list;
    return h('div', null, list, h('details', null, h('summary', null, `Ещё ${ids.length - MAX_CHIPS}`), h('ul', { class: 'chips' }, ids.slice(MAX_CHIPS).map(chip))));
  }

  function linkGroups(id) {
    const groups = [];
    const byKind = (entries) => {
      const map = new Map();
      for (const entry of entries) for (const kind of entry.kinds) map.set(kind, [...(map.get(kind) || []), entry]);
      return map;
    };
    for (const [kind, entries] of byKind(state.outgoing.get(id))) {
      const label = (state.graph.link_kinds[kind] || kind);
      groups.push(h('div', { class: 'links-group' }, h('p', null, label[0].toUpperCase() + label.slice(1)), chipsFor(entries)));
    }
    for (const [kind, entries] of byKind(state.incoming.get(id))) {
      groups.push(h('div', { class: 'links-group' }, h('p', null, INCOMING[kind] || kind), chipsFor(entries)));
    }
    return groups;
  }

  function liveLine(n) {
    const info = live(n.id);
    if (!info || !info.state) return null;
    const bits = [STATE_TEXT[info.state] || info.state];
    if (info.running) bits.push('выполняется сейчас');
    else if (info.active) bits.push('недавно работал');
    return h('p', { class: 'state' }, h('span', { class: `dot ${info.state}` }), bits.join(', '));
  }

  function nodeFacts(n) {
    const x = n.extra || {};
    const info = live(n.id) || {};
    switch (n.type) {
      case 'command':
        return facts([
          ['Обработчик', x.handler],
          ['Команды', x.slash],
          ['Кнопки', x.buttons],
          ['Как работает', x.flags],
          ['Интент', x.intent],
        ]);
      case 'job':
        return facts([
          ['Расписание', x.schedule || 'по событию'],
          ['Последний раз', info.last_run ? `${ago(info.last_run)}${info.last_status === 'dead' ? ', сбой' : ''}` : null],
          ['Следующий', info.next_run ? ago(info.next_run) : null],
          ['За сутки', info.runs_24h !== undefined ? `${info.runs_24h} ${plural(info.runs_24h, 'запуск', 'запуска', 'запусков')}${info.failed_24h ? `, сбоев: ${info.failed_24h}` : ''}` : null],
          ['В очереди', info.pending || null],
          ['Повторяется после ошибки', info.retrying || null],
          ['Ошибка', info.error],
          ['Тип', x.job_type],
        ]);
      case 'integration':
        return facts([
          ['Статус', info.status],
          ['Подробно', info.detail],
          ['Синхронизация', info.synced_at ? ago(info.synced_at) : null],
          ['Ошибок подряд', info.failures],
        ]);
      case 'data':
        return facts([
          ['Модель', x.model],
          ['Колонок', x.columns],
          ['Строк', info.rows !== undefined ? `≈ ${info.rows.toLocaleString('ru-RU')}` : null],
        ]);
      case 'channel':
        return facts([
          ['Приходит через', x.route],
          ['Mac на связи', n.id === 'channel:mac' && info.online !== undefined ? (info.online ? 'да' : 'нет') : null],
          ['Данные с телефона', info.last_seen ? ago(info.last_seen) : null],
        ]);
      default:
        return facts([
          ['Процессы', (info.processes || []).map((r) => ROLES[r] || r)],
          ['Модулей', x.modules ? x.modules.length : null],
          ['Строк кода', x.lines ? x.lines.toLocaleString('ru-RU') : null],
        ]);
    }
  }

  let cardLive = '';
  function renderNodeCard({ keepScroll = false } = {}) {
    const n = state.nodes.get(state.selected);
    if (!n) return;
    const now = JSON.stringify(live(n.id));
    if (keepScroll && now === cardLive) return; // nothing new for this node: leave the card (and its scroll) alone
    cardLive = now;
    const x = n.extra || {};
    $('#card-kind').replaceChildren(kindDot(n.type), ONE[n.type] || n.type);
    const body = [h('h2', { class: 'title' }, n.label), h('p', { class: 'desc' }, n.description), liveLine(n), nodeFacts(n)];
    if (n.type === 'command' && x.examples && x.examples.length) {
      body.push(
        h('h3', null, 'Что сказать'),
        h(
          'ul',
          { class: 'say' },
          x.examples.map((e) =>
            h('li', null, `«${e.text}»`, h('span', { class: 'tag' }, e.no_ai ? 'понимаю сразу, без ИИ' : 'понимаю через ИИ')),
          ),
        ),
      );
    }
    body.push(h('h3', null, 'Связи'), ...linkGroups(n.id));
    if (x.modules && x.modules.length > 1) {
      body.push(h('details', null, h('summary', null, `Модули: ${x.modules.length}`), h('ul', null, x.modules.map((m) => h('li', null, `${m.path} — ${m.lines}`)))));
    }
    if (x.column_names && x.column_names.length) {
      body.push(h('details', null, h('summary', null, `Колонки: ${x.columns}`), h('ul', null, x.column_names.map((c) => h('li', null, c)))));
    }
    body.push(h('code', { class: 'code', title: 'Где это в коде' }, n.path));
    $('#card-body').replaceChildren(...body.filter(Boolean));
    showCard({ keepScroll });
  }

  function renderCard() {
    if (state.view === 'node') renderNodeCard();
    else if (state.view === 'ask') renderAskCard();
  }

  // --- «Спросить» ---------------------------------------------------------------------------------------------------

  const ALLOWED = new Set(['B', 'STRONG', 'I', 'EM', 'U', 'INS', 'S', 'STRIKE', 'DEL', 'CODE', 'PRE', 'BLOCKQUOTE', 'BR', 'A', 'TG-SPOILER', 'SPAN']);

  /** Telegram HTML (the bot's own subset) → safe DOM: allowed tags rebuilt, links only http(s), the rest as text. */
  function telegramHtml(html) {
    const doc = new DOMParser().parseFromString(`<div>${html}</div>`, 'text/html');
    const walk = (from, into) => {
      for (const child of from.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) {
          into.append(child.textContent);
        } else if (child.nodeType === Node.ELEMENT_NODE) {
          if (!ALLOWED.has(child.tagName)) {
            walk(child, into);
            continue;
          }
          const tag = child.tagName === 'TG-SPOILER' || child.tagName === 'SPAN' ? 'span' : child.tagName.toLowerCase();
          const el = document.createElement(tag);
          if (tag === 'a') {
            const href = child.getAttribute('href') || '';
            if (/^https?:\/\//i.test(href)) {
              el.href = href;
              el.target = '_blank';
              el.rel = 'noopener noreferrer';
            }
          }
          walk(child, el);
          into.append(el);
        }
      }
    };
    const out = document.createDocumentFragment();
    walk(doc.body.firstChild, out);
    return out;
  }

  function announce(text) {
    $('#ask-status').textContent = text;
  }

  function renderAskCard() {
    const ask = state.lastAsk;
    if (!ask) return;
    $('#card-kind').replaceChildren(h('span', { class: 'swatch', style: { '--c': cssVar('--hud') } }), 'Ответ Jarvis');
    const body = [h('p', { class: 'asked' }, ask.text)];
    if (ask.pending) body.push(h('p', { class: 'answer' }, 'Думаю…'));
    if (ask.error) body.push(h('p', { class: 'answer error-text' }, ask.error));
    if (ask.reply) {
      const messages = ask.reply.messages || [];
      if (!messages.length) body.push(h('p', { class: 'answer' }, ask.reply.toast || 'Jarvis принял запрос, но ничего не ответил.'));
      for (const m of messages) {
        const p = h('div', { class: 'answer' });
        if (m.format === 'html') p.append(telegramHtml(m.text));
        else p.textContent = m.text;
        body.push(p);
      }
      const buttons = messages.reduce((total, m) => total + (m.buttons || 0), 0);
      if (buttons) {
        body.push(h('p', { class: 'hint' }, `В ответе ${buttons} ${plural(buttons, 'кнопка', 'кнопки', 'кнопок')}: нажать их можно в приложении на Mac или в Telegram.`));
      }
    }
    if (ask.from && state.nodes.has(ask.from)) {
      body.push(h('button', { type: 'button', class: 'back', onclick: () => focusNode(ask.from, { fly: false }) }, `Вернуться к «${state.nodes.get(ask.from).label}»`));
    }
    $('#card-body').replaceChildren(...body);
    showCard();
  }

  function setHud(mode) {
    state.hud = mode;
    const hud = $('#hud');
    hud.dataset.state = mode;
    $('#hud-state').textContent = mode.toUpperCase();
    $('#hud-state').title = HUD_TITLES[mode] || '';
  }

  function playAudio(reply) {
    const message = (reply.messages || []).find((m) => m.audio);
    if (!message) return;
    const audio = new Audio(`data:audio/mpeg;base64,${message.audio}`);
    audio.addEventListener('ended', () => setHud(state.liveError ? 'offline' : 'listening'));
    audio.addEventListener('error', () => setHud(state.liveError ? 'offline' : 'listening'));
    audio.play().then(() => setHud('speaking')).catch(() => setHud(state.liveError ? 'offline' : 'listening'));
  }

  let asking = false;
  async function ask(event) {
    event.preventDefault();
    const input = $('#ask-text');
    const text = input.value.trim();
    if (!text || asking) return;
    asking = true;
    $('#ask-send').disabled = true;
    setHud('thinking');
    state.lastAsk = { text, pending: true, from: state.selected };
    state.view = 'ask';
    renderAskCard();
    try {
      const signal = typeof AbortSignal.timeout === 'function' ? AbortSignal.timeout(95000) : undefined;
      const reply = await getJSON('/api/system/ask', {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'application/json' },
        body: JSON.stringify({ text }),
        signal,
      });
      state.lastAsk = { text, reply, from: state.lastAsk.from };
      announce('Jarvis ответил, ответ в карточке.');
      input.value = '';
      setHud(state.liveError ? 'offline' : 'listening');
      playAudio(reply);
    } catch (err) {
      const message = err && err.name === 'TimeoutError' ? 'Jarvis не ответил за 95 секунд.' : (err && err.message) || 'Не получилось.';
      state.lastAsk = { text, error: message, from: state.lastAsk.from };
      announce(message);
      setHud(state.liveError ? 'offline' : 'listening');
    } finally {
      asking = false;
      $('#ask-send').disabled = false;
      if (state.view === 'ask') renderAskCard();
    }
  }

  // --- the live layer -----------------------------------------------------------------------------------------------

  function renderPulse() {
    const data = state.live;
    const processes = $('#processes');
    const counts = $('#counts');
    const attention = $('#attention');
    const updated = $('#updated');
    if (state.liveError) {
      updated.textContent = `Нет связи с Jarvis: ${state.liveError}`;
      updated.classList.add('is-error');
    }
    if (!data) return;
    processes.replaceChildren(
      ...(data.processes || []).map((p) =>
        h('li', { title: p.age_s !== null && p.age_s !== undefined ? `сигнал ${p.age_s} с назад` : 'сигнала не было' }, h('span', { class: `dot ${p.ok ? 'ok' : 'failed'}` }), ROLES[p.role] || p.role),
      ),
      h('li', null, h('span', { class: `dot ${data.mac_online ? 'ok' : ''}` }), data.mac_online ? 'Mac на связи' : 'Mac не на связи'),
    );
    const c = data.counts || {};
    counts.replaceChildren(
      ...COUNTS.filter(([key]) => c[key] !== undefined).flatMap(([key, label]) => [
        h('dt', null, label),
        h('dd', { class: key === 'dead_jobs_24h' && c[key] ? 'is-bad' : null }, c[key]),
      ]),
    );
    const failed = Object.entries(data.nodes || {})
      .filter(([id, info]) => info.state === 'failed' && state.nodes.has(id))
      .map(([id]) => id);
    attention.replaceChildren(
      ...(failed.length ? [h('p', null, 'Требует внимания'), ...failed.slice(0, 6).map((id) => nodeRow(id, h('span', { class: 'dot failed' })))] : []),
    );
    if (!state.liveError) {
      updated.classList.remove('is-error');
      updated.textContent = `Обновлено ${new Date(data.now || Date.now()).toLocaleTimeString('ru-RU')}`;
    }
    const hud = $('#hud');
    const warn = Object.values(data.nodes || {}).some((info) => info.state === 'warn');
    hud.dataset.health = failed.length ? 'failed' : warn ? 'warn' : 'ok';
  }

  let liveTimer = null;
  async function pollLive() {
    clearTimeout(liveTimer);
    if (!document.hidden) {
      try {
        state.live = await getJSON('/api/system/live');
        state.liveError = null;
        if (state.hud === 'offline') setHud('listening');
      } catch (err) {
        state.liveError = (err && err.message) || 'нет ответа';
        if (state.hud === 'listening') setHud('offline');
      }
      renderPulse();
      restyle();
      rebuildOverlay();
      if (state.view === 'node') renderNodeCard({ keepScroll: true });
    }
    liveTimer = setTimeout(pollLive, LIVE_EVERY_MS);
  }

  // --- start ----------------------------------------------------------------------------------------------------------

  function wire() {
    if (phone.matches) $('#ask-text').placeholder = 'Спросите Jarvis…';
    $('#ask').addEventListener('submit', ask);
    $('#search').addEventListener('input', (e) => runSearch(e.target.value));
    $('#search').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const first = $('#results .row');
        if (first) first.click();
      }
    });
    for (const button of document.querySelectorAll('[data-open]')) {
      button.addEventListener('click', () => openSheet(button.dataset.open));
    }
    for (const button of document.querySelectorAll('[data-view]')) {
      button.addEventListener('click', () => setView(button.dataset.view));
    }
    for (const button of document.querySelectorAll('[data-toggle]')) {
      button.addEventListener('click', () => {
        const sheet = $(`#${button.dataset.toggle}`);
        const open = !sheet.classList.contains('is-open');
        openSheet(open ? sheet.id : null);
        if (open && sheet.id === 'panel') $('#search').focus();
      });
    }
    for (const button of document.querySelectorAll('[data-close]')) {
      button.addEventListener('click', () => {
        const id = button.dataset.close;
        if (id === 'card') closeCard();
        else $(`#${id}`).classList.remove('is-open');
      });
    }
    document.addEventListener('keydown', (e) => {
      const typing = e.target instanceof HTMLInputElement;
      if (e.key === 'Escape') {
        if (!$('#card').hidden) closeCard();
        for (const sheet of document.querySelectorAll('.sheet.is-open')) sheet.classList.remove('is-open');
      } else if (e.key === '/' && !typing) {
        e.preventDefault();
        if (phone.matches) openSheet('panel');
        $('#search').focus();
      }
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) pollLive();
    });
  }

  async function boot() {
    wire();
    try {
      const graph = await getJSON('/api/system/graph');
      ingest(graph);
    } catch (err) {
      state.liveError = (err && err.message) || 'нет ответа';
      setHud('offline');
      $('#updated').textContent = `Карта не загрузилась: ${state.liveError}`;
      $('#updated').classList.add('is-error');
      setTimeout(() => location.reload(), 30000);
      return;
    }
    for (const n of state.nodes.values()) n.search = searchText(n);
    renderLegend();
    renderHubs();
    setView(savedView());
    await pollLive();
  }

  boot();
})();
