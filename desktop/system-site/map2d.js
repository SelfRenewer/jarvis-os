/* Jarvis OS — the 2D scheme (16.10, the user: «в 3D ничего не понятно, как будто накидано»). The same graph laid out
   in columns, left to right the way a request travels: where it comes from → the core → the agents, grouped by area →
   the services outside. Long chains are folded into one arrow (the bot → a command → an agent = «the bot routes to the
   agent»; the scheduler → a job → an agent = «runs it on a schedule»). A chosen agent opens in place: what to say to
   it, its background jobs and its tables. Built with DOM APIs and one SVG for the arrows; no data is read as HTML. */
'use strict';

window.JarvisMap2D = (() => {
  const COLUMNS = [
    { type: 'channel', title: 'Откуда приходит' },
    { type: 'core', title: 'Ядро' },
    { type: 'agent', title: 'Агенты' },
    { type: 'integration', title: 'Внешние сервисы' },
  ];
  const SHOWN = new Set(COLUMNS.map((c) => c.type));
  // Areas of the agents column; an agent the code adds later lands in «Другое» until it is placed here.
  const GROUPS = [
    ['Время и дела', ['agent:tasks', 'agent:agenda', 'agent:planner', 'agent:reminders', 'agent:sync', 'agent:weather']],
    ['Разговор и знания', ['agent:agent', 'agent:profile', 'agent:inbox', 'agent:ideas', 'agent:search', 'agent:research']],
    ['Здоровье и дневник', ['agent:tracker', 'agent:health', 'agent:meds', 'agent:labs', 'agent:photos']],
    ['Деньги и покупки', ['agent:finance', 'agent:calc', 'agent:shopping', 'agent:usage']],
    ['Mac, сайты, звонки', ['agent:desktop', 'agent:sites', 'agent:calls', 'agent:voice']],
    ['Разработка', ['agent:development', 'agent:dev_worker']],
  ];
  const INNER = [
    ['command', 'Что сказать'],
    ['job', 'Фоновые задачи'],
    ['data', 'Таблицы'],
  ];
  const DERIVED = {
    routes: 'распознаёт команды для',
    schedules: 'запускает по расписанию',
  };
  const COL_W = 212;
  const COL_GAP = 104;
  const ROW_GAP = 8;
  const GROUP_GAP = 14;
  const TOP = 44; // room for the column titles
  const MIN_K = 0.35;
  const MAX_K = 2.2;

  function plural(n, one, few, many) {
    const m10 = n % 10;
    const m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }

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

  const SVG = 'http://www.w3.org/2000/svg';
  const svg = (tag, attrs) => {
    const el = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    return el;
  };

  /**
   * host: an empty element covering the screen. api: {nodes, outgoing, incoming, live(id), select(id), clear(),
   * focus() → Set|null (search matches / the selection's neighbours from the page), hidden: Set of node types}.
   */
  function create(host, api) {
    const world = h('div', { class: 'm2-world' });
    const wires = svg('svg', { class: 'm2-wires', 'aria-hidden': 'true' });
    const defs = svg('defs');
    for (const [id, cls] of [['m2-arrow', 'm2-head'], ['m2-arrow-lit', 'm2-head is-lit']]) {
      const marker = svg('marker', { id, viewBox: '0 0 8 8', refX: '7', refY: '4', markerWidth: '7', markerHeight: '7', orient: 'auto-start-reverse' });
      marker.append(svg('path', { d: 'M0,0 L8,4 L0,8 z', class: cls }));
      defs.append(marker);
    }
    wires.append(defs);
    const paths = svg('g');
    wires.append(paths);
    world.append(wires);
    const controls = h(
      'div',
      { class: 'm2-zoom', role: 'group', 'aria-label': 'Масштаб схемы' },
      h('button', { type: 'button', 'aria-label': 'Приблизить', onclick: () => zoomBy(1.25) }, '+'),
      h('button', { type: 'button', 'aria-label': 'Отдалить', onclick: () => zoomBy(0.8) }, '−'),
      h('button', { type: 'button', 'aria-label': 'Показать всю схему', title: 'Вся схема', onclick: () => fit(true) }, '⤢'),
    );
    host.append(world, controls);

    // --- the model: who owns which command / job / table, and the arrows between what is shown -------------------
    const owners = new Map(); // inner id → [agent ids]
    const inner = new Map(); // agent id → {command: [], job: [], data: []}
    const edges = new Map(); // "s|t" → {s, t, kinds: Set}
    const agentsOf = (id) => owners.get(id) || [];

    const own = (agent, id, type) => {
      if (!inner.has(agent)) inner.set(agent, { command: [], job: [], data: [] });
      const box = inner.get(agent)[type];
      if (!box.includes(id)) box.push(id);
      owners.set(id, [...new Set([...(owners.get(id) || []), agent])]);
    };
    const typeOf = (id) => (api.nodes.get(id) || {}).type;
    const edge = (s, t, kind) => {
      if (s === t || !api.nodes.has(s) || !api.nodes.has(t)) return;
      const key = `${s}|${t}`;
      if (!edges.has(key)) edges.set(key, { s, t, kinds: new Set() });
      edges.get(key).kinds.add(kind);
    };

    for (const [id, node] of api.nodes) {
      if (node.type === 'command') for (const o of api.outgoing.get(id)) if (typeOf(o.other) === 'agent') own(o.other, id, 'command');
      if (node.type === 'job') for (const o of api.outgoing.get(id)) if (typeOf(o.other) === 'agent') own(o.other, id, 'job');
      if (node.type === 'data') for (const i of api.incoming.get(id)) if (typeOf(i.other) === 'agent') own(i.other, id, 'data');
    }
    for (const [id, node] of api.nodes) {
      for (const o of api.outgoing.get(id)) {
        const other = typeOf(o.other);
        if (SHOWN.has(node.type) && SHOWN.has(other)) for (const k of o.kinds) edge(id, o.other, k);
      }
      if (node.type === 'command') {
        // folded: the core that routes it → its agent; its agent → the services it uses
        const routers = api.incoming.get(id).filter((i) => typeOf(i.other) === 'core').map((i) => i.other);
        for (const agent of agentsOf(id)) {
          for (const core of routers) edge(core, agent, 'routes');
          for (const o of api.outgoing.get(id)) if (typeOf(o.other) === 'integration') edge(agent, o.other, 'uses');
        }
      }
      if (node.type === 'job') {
        const starters = api.incoming.get(id);
        for (const agent of agentsOf(id)) {
          for (const i of starters) {
            const kind = i.kinds.includes('schedules') ? 'schedules' : 'enqueues';
            if (SHOWN.has(typeOf(i.other)) && i.other !== agent) edge(i.other, agent, kind);
          }
          for (const o of api.outgoing.get(id)) if (typeOf(o.other) === 'integration') edge(agent, o.other, 'uses');
        }
      }
    }

    // --- cards -------------------------------------------------------------------------------------------------------
    const cards = new Map(); // id → element
    const chips = new Map(); // inner id → [chip elements]
    let expanded = null;
    let hovered = null;

    function meta(agent) {
      const box = inner.get(agent);
      if (!box) return '';
      const bits = [];
      if (box.command.length) bits.push(`${box.command.length} ${plural(box.command.length, 'команда', 'команды', 'команд')}`);
      if (box.job.length) bits.push(`${box.job.length} ${plural(box.job.length, 'задача', 'задачи', 'задач')}`);
      if (box.data.length) bits.push(`${box.data.length} ${plural(box.data.length, 'таблица', 'таблицы', 'таблиц')}`);
      return bits.join(' · ');
    }

    function card(id) {
      const n = api.nodes.get(id);
      const el = h(
        'div',
        { class: `m2-card t-${n.type}`, 'data-id': id },
        h(
          'button',
          {
            type: 'button',
            class: 'm2-head-btn',
            'aria-expanded': n.type === 'agent' ? 'false' : null,
            onclick: (e) => {
              e.stopPropagation();
              if (n.type === 'agent' && expanded === id && api.selected() === id) {
                api.clear();
                return;
              }
              api.select(id);
            },
            onmouseenter: () => hover(id),
            onmouseleave: () => hover(null),
            onfocus: () => hover(id),
            onblur: () => hover(null),
          },
          h('span', { class: 'm2-dot' }),
          h('span', { class: 'm2-name' }, n.label),
          n.type === 'agent' ? h('span', { class: 'm2-meta' }, meta(id)) : null,
        ),
      );
      cards.set(id, el);
      return el;
    }

    function innerBlock(agent) {
      const box = inner.get(agent) || { command: [], job: [], data: [] };
      const out = h('div', { class: 'm2-inner' });
      for (const [type, title] of INNER) {
        const ids = box[type];
        if (!ids.length || api.hidden.has(type)) continue;
        const list = h('ul', { class: 'm2-chips' });
        for (const id of ids) {
          const n = api.nodes.get(id);
          const example = type === 'command' && n.extra && n.extra.examples && n.extra.examples[0] ? n.extra.examples[0].text : null;
          const chip = h(
            'button',
            {
              type: 'button',
              class: `m2-chip t-${type}`,
              'data-id': id,
              title: n.description || n.label,
              onclick: (e) => {
                e.stopPropagation();
                api.select(id);
              },
              onmouseenter: () => hover(id),
              onmouseleave: () => hover(null),
            },
            h('span', { class: 'm2-dot' }),
            h('span', null, example ? `«${example}»` : n.label),
          );
          chips.set(id, [...(chips.get(id) || []), chip]);
          list.append(h('li', null, chip));
        }
        out.append(h('p', { class: 'm2-inner-title' }, `${title} · ${ids.length}`), list);
      }
      return out;
    }

    let size = { w: 0, h: 0 };
    let anchors = new Map(); // id → {x, y, w, h} in world coordinates (the card head for an open agent)

    function build() {
      for (const el of world.querySelectorAll('.m2-col-title, .m2-group, .m2-card')) el.remove();
      cards.clear();
      chips.clear();
      anchors = new Map();
      let x = 0;
      let height = 0;
      const columns = COLUMNS.filter((c) => !api.hidden.has(c.type));
      for (const column of columns) {
        const ids = [...api.nodes.values()].filter((n) => n.type === column.type).sort((a, b) => b.degree - a.degree).map((n) => n.id);
        world.append(h('p', { class: 'm2-col-title', style: { left: `${x}px`, width: `${COL_W}px` } }, `${column.title} · ${ids.length}`));
        let y = TOP;
        const place = (el) => {
          el.style.left = `${x}px`;
          el.style.top = `${y}px`;
          el.style.width = `${COL_W}px`;
          world.append(el);
          y += el.offsetHeight + ROW_GAP;
        };
        if (column.type === 'agent') {
          const placed = new Set();
          const groups = GROUPS.map(([title, members]) => [title, members.filter((id) => api.nodes.has(id))]);
          const rest = ids.filter((id) => !GROUPS.some(([, members]) => members.includes(id)));
          if (rest.length) groups.push(['Другое', rest]);
          for (const [title, members] of groups) {
            if (!members.length) continue;
            if (y > TOP) y += GROUP_GAP - ROW_GAP;
            place(h('p', { class: 'm2-group' }, title));
            for (const id of members) {
              const el = card(id);
              if (id === expanded) {
                el.classList.add('is-open');
                el.querySelector('.m2-head-btn').setAttribute('aria-expanded', 'true');
                el.append(innerBlock(id));
              }
              place(el);
              placed.add(id);
            }
          }
        } else {
          for (const id of ids) place(card(id));
        }
        for (const [id, el] of cards) {
          if (anchors.has(id) || typeOf(id) !== column.type) continue;
          const head = el.querySelector('.m2-head-btn');
          anchors.set(id, { x: el.offsetLeft, y: el.offsetTop, w: COL_W, h: head.offsetHeight });
        }
        height = Math.max(height, y);
        x += COL_W + COL_GAP;
      }
      size = { w: Math.max(0, x - COL_GAP), h: height };
      world.style.width = `${size.w}px`;
      world.style.height = `${size.h}px`;
      wires.setAttribute('width', String(size.w + 80));
      wires.setAttribute('height', String(size.h));
      wires.setAttribute('viewBox', `0 0 ${size.w + 80} ${size.h}`);
      drawWires();
      restyle();
    }

    const colOf = (id) => COLUMNS.findIndex((c) => c.type === typeOf(id));

    function wirePath(e) {
      const a = anchors.get(e.s);
      const b = anchors.get(e.t);
      if (!a || !b) return null;
      const ay = a.y + a.h / 2;
      const by = b.y + b.h / 2;
      const ca = colOf(e.s);
      const cb = colOf(e.t);
      if (ca === cb) {
        const x = a.x + a.w;
        const bend = 26 + Math.min(60, Math.abs(by - ay) * 0.12);
        return `M${x},${ay} C${x + bend},${ay} ${x + bend},${by} ${x + 2},${by}`;
      }
      if (cb > ca) {
        const x1 = a.x + a.w;
        const x2 = b.x - 2;
        const dx = (x2 - x1) / 2;
        return `M${x1},${ay} C${x1 + dx},${ay} ${x2 - dx},${by} ${x2},${by}`;
      }
      const x1 = a.x;
      const x2 = b.x + b.w + 2;
      const dx = (x1 - x2) / 2;
      return `M${x1},${ay} C${x1 - dx},${ay} ${x2 + dx},${by} ${x2},${by}`;
    }

    const wireEls = new Map(); // "s|t" → path
    function drawWires() {
      paths.replaceChildren();
      wireEls.clear();
      for (const [key, e] of edges) {
        const d = wirePath(e);
        if (!d) continue;
        const path = svg('path', { d, class: `m2-wire from-${typeOf(e.s)}`, 'marker-end': 'url(#m2-arrow)' });
        const title = svg('title');
        const kinds = [...e.kinds].map((k) => DERIVED[k] || (api.linkKinds[k] || k));
        title.textContent = `${api.nodes.get(e.s).label} → ${api.nodes.get(e.t).label}: ${kinds.join(', ')}`;
        path.append(title);
        paths.append(path);
        wireEls.set(key, path);
      }
    }

    // --- highlight -----------------------------------------------------------------------------------------------------

    /** What lights up: the hovered / chosen node, its shown neighbours, and for an inner item its agent. */
    function litSet() {
      const center = hovered || api.selected();
      if (!center) {
        const matches = api.matches();
        if (!matches) return null;
        const lit = new Set();
        for (const id of matches) {
          if (cards.has(id)) lit.add(id);
          for (const agent of agentsOf(id)) lit.add(agent);
        }
        return { nodes: lit, wires: new Set(), center: null };
      }
      const ends = cards.has(center) ? [center] : agentsOf(center);
      const nodes = new Set(ends);
      const wiresLit = new Set();
      for (const [key, e] of edges) {
        if (ends.includes(e.s) || ends.includes(e.t)) {
          nodes.add(e.s);
          nodes.add(e.t);
          wiresLit.add(key);
        }
      }
      if (!cards.has(center)) {
        // a command / job / table: its own services and core light up too
        for (const o of [...api.outgoing.get(center), ...api.incoming.get(center)]) if (cards.has(o.other)) nodes.add(o.other);
      }
      return { nodes, wires: wiresLit, center };
    }

    function restyle() {
      const lit = litSet();
      host.classList.toggle('is-focused', Boolean(lit));
      for (const [id, el] of cards) {
        const info = api.live(id) || {};
        el.classList.toggle('is-lit', Boolean(lit && lit.nodes.has(id)));
        el.classList.toggle('is-center', Boolean(lit && lit.center === id));
        el.classList.toggle('is-selected', api.selected() === id);
        el.dataset.state = info.state || '';
        el.classList.toggle('is-running', Boolean(info.running));
      }
      for (const [id, list] of chips) {
        const info = api.live(id) || {};
        for (const chip of list) {
          chip.classList.toggle('is-selected', api.selected() === id);
          chip.classList.toggle('is-match', Boolean(api.matches() && api.matches().has(id)));
          chip.dataset.state = info.state || '';
          chip.classList.toggle('is-running', Boolean(info.running));
        }
      }
      for (const [key, path] of wireEls) {
        const on = Boolean(lit && lit.wires.has(key));
        path.classList.toggle('is-lit', on);
        path.setAttribute('marker-end', on ? 'url(#m2-arrow-lit)' : 'url(#m2-arrow)');
        if (on) paths.append(path); // lit arrows on top
      }
    }

    function hover(id) {
      hovered = id;
      restyle();
    }

    // --- pan and zoom --------------------------------------------------------------------------------------------------

    const view = { x: 0, y: 0, k: 1 };
    let userMoved = false;
    const apply = () => {
      world.style.transform = `translate(${view.x.toFixed(1)}px, ${view.y.toFixed(1)}px) scale(${view.k.toFixed(3)})`;
    };

    /** The free part of the screen: under the menu, above the «Спросить» bar, beside open panels. */
    function freeRect() {
      const r = host.getBoundingClientRect();
      if (matchMedia('(max-width: 760px)').matches) return { left: 12, top: 120, right: r.width - 12, bottom: r.height - 130 };
      const card = document.getElementById('card');
      const open = card && !card.hidden ? card.getBoundingClientRect().left - 16 : r.width - 24; // the card on the right
      return { left: 188, top: 76, right: Math.max(420, open), bottom: r.height - 90 }; // beside the HUD, under the menu
    }

    function fit(animated) {
      const free = freeRect();
      const w = free.right - free.left;
      // by width: the text stays readable and the long agents column scrolls (fitting its height made it tiny)
      view.k = Math.min(1, Math.max(0.55, w / (size.w + 40)));
      view.x = free.left + Math.max(0, (w - size.w * view.k) / 2);
      view.y = free.top;
      userMoved = false;
      world.classList.toggle('is-animated', Boolean(animated));
      apply();
      if (animated) setTimeout(() => world.classList.remove('is-animated'), 320);
    }

    function zoomAt(cx, cy, factor) {
      const k = Math.min(MAX_K, Math.max(MIN_K, view.k * factor));
      const f = k / view.k;
      view.x = cx - (cx - view.x) * f;
      view.y = cy - (cy - view.y) * f;
      view.k = k;
      userMoved = true;
      apply();
    }

    function zoomBy(factor) {
      const r = host.getBoundingClientRect();
      world.classList.add('is-animated');
      zoomAt(r.width / 2, r.height / 2, factor);
      setTimeout(() => world.classList.remove('is-animated'), 320);
    }

    /** Bring a card or chip into the free area (without changing the zoom). */
    function reveal(id) {
      const el = (chips.get(id) || [])[0] || cards.get(id);
      if (!el || !host.isConnected) return;
      const free = freeRect();
      const box = el.getBoundingClientRect();
      const hostBox = host.getBoundingClientRect();
      const top = box.top - hostBox.top;
      const left = box.left - hostBox.left;
      let dx = 0;
      let dy = 0;
      if (left < free.left) dx = free.left - left + 24;
      else if (left + box.width > free.right) dx = free.right - (left + box.width) - 24;
      if (top < free.top) dy = free.top - top + 24;
      else if (top + Math.min(box.height, 260) > free.bottom) dy = free.bottom - (top + Math.min(box.height, 260)) - 24;
      if (!dx && !dy) return;
      view.x += dx;
      view.y += dy;
      world.classList.add('is-animated');
      apply();
      setTimeout(() => world.classList.remove('is-animated'), 320);
    }

    host.addEventListener(
      'wheel',
      (e) => {
        e.preventDefault();
        if (e.ctrlKey || e.metaKey) {
          zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * 0.0085));
        } else {
          view.x -= e.deltaX;
          view.y -= e.deltaY;
          userMoved = true;
          apply();
        }
      },
      { passive: false },
    );

    const pointers = new Map();
    let pinch = null;
    let dragged = false;
    host.addEventListener('pointerdown', (e) => {
      if (e.target.closest('button, a, input')) return;
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      host.setPointerCapture(e.pointerId);
      dragged = false;
      if (pointers.size === 2) {
        const [p, q] = [...pointers.values()];
        pinch = { d: Math.hypot(p.x - q.x, p.y - q.y), k: view.k };
      }
    });
    host.addEventListener('pointermove', (e) => {
      const last = pointers.get(e.pointerId);
      if (!last) return;
      const now = { x: e.clientX, y: e.clientY };
      pointers.set(e.pointerId, now);
      if (pointers.size === 2 && pinch) {
        const [p, q] = [...pointers.values()];
        const d = Math.hypot(p.x - q.x, p.y - q.y);
        zoomAt((p.x + q.x) / 2, (p.y + q.y) / 2, (pinch.k * (d / pinch.d)) / view.k);
      } else if (pointers.size === 1) {
        view.x += now.x - last.x;
        view.y += now.y - last.y;
        if (Math.abs(now.x - last.x) + Math.abs(now.y - last.y) > 1) dragged = true;
        userMoved = true;
        apply();
      }
    });
    const release = (e) => {
      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinch = null;
    };
    host.addEventListener('pointerup', (e) => {
      const tap = !dragged && pointers.size === 1;
      release(e);
      if (tap && !e.target.closest('.m2-card, .m2-zoom')) api.clear();
    });
    host.addEventListener('pointercancel', release);

    let resizeTimer = null;
    window.addEventListener('resize', () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (!host.hidden && !userMoved) fit(false);
      }, 120);
    });

    // --- the outside -----------------------------------------------------------------------------------------------------

    /** The page chose a node (a click here, search, a chip in the card): open its agent, light it, bring it into view. */
    function select(id) {
      const type = typeOf(id);
      const target = type === 'agent' ? id : ['command', 'job', 'data'].includes(type) ? agentsOf(id)[0] || null : expanded;
      const reflow = target !== expanded;
      expanded = target;
      if (reflow) build();
      else restyle();
      if (id) requestAnimationFrame(() => reveal(id));
    }

    function clear() {
      if (expanded) {
        expanded = null;
        build();
      } else {
        restyle();
      }
    }

    let built = false;
    function show() {
      host.hidden = false;
      if (!built) {
        build();
        built = true;
        fit(false);
      } else {
        restyle();
      }
    }

    function hide() {
      host.hidden = true;
    }

    function rebuild() {
      if (!built) return;
      if (host.hidden) built = false; // measured on the next show: a hidden scheme has no sizes
      else build();
    }

    return { show, hide, restyle, select, clear, rebuild, fit: () => fit(true) };
  }

  return { create };
})();
