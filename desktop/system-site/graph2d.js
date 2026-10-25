/* Jarvis OS — the graph view (17.10, the user: «2D граф ещё в таком формате, как в Обсидиане, оставляя текущий
   формат»). The same nodes and links as the scheme and 3D, laid out by forces on a <canvas>: links pull, nodes push
   apart, hubs end up in the middle with their rays, nodes with nothing shown around them sit on an outer ring.
   Hovering a node lights it and its neighbours and dims the rest; a click opens the usual card. A small panel sets the
   forces, the orphans, colours by type and the local graph (only around the chosen node, depth 1 or 2).
   The physics (`simulate`, `step`) is plain math with no DOM — checked by `node graph2d.check.mjs`. The simulation
   sleeps once it settles, and with «reduce motion» the graph is laid out at once and stands still. */
'use strict';

(function (root) {
  const DEFAULTS = { repulsion: 1, distance: 1, orphans: true, colors: false, depth: 0 };
  const SETTINGS_KEY = 'jarvis-os-graph2d';
  const TYPE_VAR = {
    core: '--t-core', agent: '--t-agent', command: '--t-command', job: '--t-job',
    integration: '--t-integration', data: '--t-data', channel: '--t-channel',
  }; // prettier-ignore

  // --- physics (pure) -------------------------------------------------------------------------------------------------

  /** A node's size from how connected it is (world units). */
  const radiusOf = (degree) => 2 + Math.sqrt(degree) * 0.95; // a leaf ≈ 3, the bot hub ≈ 12 — small dots, as in Obsidian

  /**
   * Positions for `ids` with `links` ([[a, b], …] indexes into ids). Connected nodes are placed by forces; nodes with no
   * shown link go onto a ring outside them (in id order). Returns {x, y, vx, vy, orphan, degree} per node.
   */
  function seed(count, links, previous = []) {
    const degree = new Array(count).fill(0);
    for (const [a, b] of links) {
      degree[a] += 1;
      degree[b] += 1;
    }
    const nodes = [];
    for (let i = 0; i < count; i += 1) {
      const old = previous[i];
      const angle = i * 2.399963229728653; // the golden angle: an even first spread, the same every time
      const r = 12 * Math.sqrt(i + 1);
      nodes.push({
        x: old ? old.x : Math.cos(angle) * r,
        y: old ? old.y : Math.sin(angle) * r,
        vx: 0,
        vy: 0,
        degree: degree[i],
        orphan: degree[i] === 0,
        fixed: false,
      });
    }
    return nodes;
  }

  /** One tick: repulsion between all connected nodes, springs on links, a pull to the middle. Returns the motion. */
  function step(nodes, links, { repulsion = 1, distance = 1, alpha = 1 } = {}) {
    const n = nodes.length;
    const live = [];
    for (let i = 0; i < n; i += 1) if (!nodes[i].orphan) live.push(i);
    const charge = 2600 * repulsion;
    for (let p = 0; p < live.length; p += 1) {
      const a = nodes[live[p]];
      for (let q = p + 1; q < live.length; q += 1) {
        const b = nodes[live[q]];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) {
          dx = (live[p] - live[q]) * 0.01; // two on one spot: apart in a fixed direction
          dy = 0.01;
          d2 = dx * dx + dy * dy;
        }
        if (d2 > 640000) continue; // 800 units apart: too far to matter
        const f = (charge * alpha) / d2;
        const d = Math.sqrt(d2);
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
    }
    const rest = 55 * distance;
    for (const [i, j] of links) {
      const a = nodes[i];
      const b = nodes[j];
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const k = ((d - rest) / d) * 0.06 * alpha;
      const wa = b.degree / (a.degree + b.degree || 1); // the lighter end moves more: hubs stay put
      a.vx += dx * k * wa;
      a.vy += dy * k * wa;
      b.vx -= dx * k * (1 - wa);
      b.vy -= dy * k * (1 - wa);
    }
    let motion = 0;
    for (const i of live) {
      const node = nodes[i];
      node.vx -= node.x * 0.0025 * alpha; // gravity to the middle
      node.vy -= node.y * 0.0025 * alpha;
      node.vx *= 0.6; // friction
      node.vy *= 0.6;
      if (node.fixed) {
        node.vx = 0;
        node.vy = 0;
        continue;
      }
      const speed = Math.hypot(node.vx, node.vy);
      if (speed > 40) {
        node.vx *= 40 / speed;
        node.vy *= 40 / speed;
      }
      node.x += node.vx;
      node.y += node.vy;
      motion += Math.abs(node.vx) + Math.abs(node.vy);
    }
    return live.length ? motion / live.length : 0;
  }

  /** The orphans on a ring around the rest (called after the rest has moved). */
  function ring(nodes) {
    let reach = 0;
    let orphans = 0;
    for (const node of nodes) {
      if (node.orphan) orphans += 1;
      else reach = Math.max(reach, Math.hypot(node.x, node.y));
    }
    if (!orphans) return 0;
    const radius = Math.max(reach + 70, (orphans * 16) / (2 * Math.PI));
    let k = 0;
    for (const node of nodes) {
      if (!node.orphan) continue;
      const angle = (k / orphans) * 2 * Math.PI - Math.PI / 2;
      node.x = Math.cos(angle) * radius;
      node.y = Math.sin(angle) * radius;
      k += 1;
    }
    return radius;
  }

  /** The whole layout at once (reduced motion; the check). */
  function simulate(count, links, opts = {}, ticks = 300) {
    const nodes = seed(count, links);
    let alpha = 1;
    for (let t = 0; t < ticks; t += 1) {
      step(nodes, links, { ...opts, alpha });
      alpha = Math.max(0.02, alpha * 0.985);
    }
    ring(nodes);
    return nodes;
  }

  // --- the view -------------------------------------------------------------------------------------------------------

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

  function loadSettings() {
    try {
      return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}') };
    } catch {
      return { ...DEFAULTS };
    }
  }
  function saveSettings(s) {
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
    } catch {
      /* a private window: for this page only */
    }
  }

  /**
   * host: an empty element covering the screen. api: {nodes: Map id → node, outgoing, hidden: Set of types, live(id),
   * selected() → id|null, matches() → Set|null, select(id), clear(), reduced: MediaQueryList}.
   */
  function create(host, api) {
    const canvas = h('canvas', { class: 'g2-canvas', 'aria-hidden': 'true' });
    const settings = loadSettings();
    const view = { x: 0, y: 0, k: 1 }; // screen = world × k + (x, y), around the middle of the host
    let ids = [];
    let index = new Map();
    let links = [];
    let nodes = [];
    let neighbours = [];
    let alpha = 1;
    let running = false;
    let hovered = -1;
    let dragging = null; // {i, moved} | {pan, x, y}
    let shown = false;
    let fitted = false;
    const ctx = canvas.getContext('2d');
    const css = getComputedStyle(document.documentElement);
    const color = (type) => css.getPropertyValue(TYPE_VAR[type] || '--t-data').trim() || '#9aa3b0';

    // the panel
    const range = (name, label, min, max, stepValue) =>
      h('label', { class: 'g2-field' }, label,
        h('input', { type: 'range', min, max, step: stepValue, value: settings[name], oninput: (e) => {
          settings[name] = Number(e.target.value);
          saveSettings(settings);
          reheat(0.6);
        } })); // prettier-ignore
    const check = (name, label) =>
      h('label', { class: 'g2-check' },
        h('input', { type: 'checkbox', checked: settings[name] || undefined, onchange: (e) => {
          settings[name] = e.target.checked;
          saveSettings(settings);
          if (name === 'orphans') rebuild();
          else draw();
        } }), label); // prettier-ignore
    const depthButtons = [0, 1, 2].map((d) =>
      h('button', { type: 'button', 'aria-pressed': String(settings.depth === d), onclick: () => {
        settings.depth = d;
        saveSettings(settings);
        for (const [k, b] of depthButtons.entries()) b.setAttribute('aria-pressed', String(k === d));
        rebuild();
      } }, d === 0 ? 'весь' : `±${d}`)); // prettier-ignore
    const panel = h('div', { class: 'g2-panel', hidden: true, role: 'group', 'aria-label': 'Настройки графа' },
      h('p', { class: 'g2-title' }, 'Граф'),
      range('repulsion', 'Отталкивание', 0.3, 3, 0.1),
      range('distance', 'Длина связей', 0.4, 3, 0.1),
      check('orphans', 'Узлы без связей'),
      check('colors', 'Цвета по типам'),
      h('div', { class: 'g2-field' }, 'Вокруг выбранного', h('div', { class: 'g2-seg' }, depthButtons))); // prettier-ignore
    const controls = h('div', { class: 'm2-zoom g2-zoom', role: 'group', 'aria-label': 'Граф' },
      h('button', { type: 'button', 'aria-label': 'Приблизить', onclick: () => zoomAt(1.25) }, '+'),
      h('button', { type: 'button', 'aria-label': 'Отдалить', onclick: () => zoomAt(0.8) }, '−'),
      h('button', { type: 'button', 'aria-label': 'Показать весь граф', title: 'Весь граф', onclick: () => fit() }, '⤢'),
      h('button', { type: 'button', 'aria-label': 'Настройки графа', title: 'Настройки', 'aria-expanded': 'false', onclick: (e) => {
        panel.hidden = !panel.hidden;
        e.currentTarget.setAttribute('aria-expanded', String(!panel.hidden));
      } }, '⚙')); // prettier-ignore
    host.append(canvas, controls, panel);

    // --- what is shown: the types not hidden, the orphans if wanted, the local graph around the selection ----------
    function rebuild() {
      const previous = new Map(ids.map((id, i) => [id, nodes[i]]));
      let chosen = [...api.nodes.values()].filter((n) => !api.hidden.has(n.type)).map((n) => n.id);
      const sel = api.selected();
      if (settings.depth && sel && api.nodes.has(sel)) {
        const near = new Set([sel]);
        let edge = [sel];
        for (let d = 0; d < settings.depth; d += 1) {
          const next = [];
          for (const id of edge) {
            for (const { other } of api.outgoing.get(id) || []) if (!near.has(other)) near.add(other), next.push(other);
            for (const { other } of api.incoming.get(id) || []) if (!near.has(other)) near.add(other), next.push(other);
          }
          edge = next;
        }
        chosen = chosen.filter((id) => near.has(id));
      }
      const set = new Set(chosen);
      const raw = [];
      for (const id of chosen) for (const { other } of api.outgoing.get(id) || []) if (set.has(other) && other !== id) raw.push([id, other]);
      if (!settings.orphans) {
        const linked = new Set(raw.flat());
        chosen = chosen.filter((id) => linked.has(id));
      }
      ids = chosen;
      index = new Map(ids.map((id, i) => [id, i]));
      const seen = new Set();
      links = [];
      for (const [a, b] of raw) {
        const key = a < b ? `${a}|${b}` : `${b}|${a}`;
        if (seen.has(key) || !index.has(a) || !index.has(b)) continue;
        seen.add(key);
        links.push([index.get(a), index.get(b)]);
      }
      nodes = seed(ids.length, links, ids.map((id) => previous.get(id)));
      neighbours = ids.map(() => new Set());
      for (const [a, b] of links) {
        neighbours[a].add(b);
        neighbours[b].add(a);
      }
      hovered = -1;
      fitted = false;
      reheat(1);
    }

    function reheat(value) {
      alpha = Math.max(alpha, value);
      if (api.reduced.matches) {
        for (let t = 0; t < 300; t += 1) step(nodes, links, { ...settings, alpha: Math.max(0.02, 0.985 ** t) });
        ring(nodes);
        alpha = 0;
        if (!fitted) fit(false);
        draw();
        return;
      }
      if (!running && shown) {
        running = true;
        requestAnimationFrame(tick);
      }
    }

    function tick() {
      if (!shown) {
        running = false;
        return;
      }
      const motion = step(nodes, links, { ...settings, alpha });
      ring(nodes);
      alpha *= 0.985;
      if (!fitted && alpha < 0.55) fit(false);
      draw();
      if (alpha < 0.02 || (motion < 0.02 && alpha < 0.3 && !dragging)) {
        running = false; // settled: no frames until something changes
        return;
      }
      requestAnimationFrame(tick);
    }

    // --- drawing ------------------------------------------------------------------------------------------------------
    function size() {
      const r = host.getBoundingClientRect();
      const ratio = Math.min(devicePixelRatio || 1, 2);
      canvas.width = Math.round(r.width * ratio);
      canvas.height = Math.round(r.height * ratio);
      canvas.style.width = `${r.width}px`;
      canvas.style.height = `${r.height}px`;
      return { w: r.width, hgt: r.height, ratio };
    }
    let box = { w: 1, hgt: 1, ratio: 1 };

    const toScreen = (n) => [box.w / 2 + view.x + n.x * view.k, box.hgt / 2 + view.y + n.y * view.k];
    const toWorld = (sx, sy) => [(sx - box.w / 2 - view.x) / view.k, (sy - box.hgt / 2 - view.y) / view.k];

    function lit() {
      const sel = api.selected();
      const focus = hovered >= 0 ? hovered : sel && index.has(sel) ? index.get(sel) : -1;
      if (focus >= 0) return { focus, set: new Set([focus, ...neighbours[focus]]) };
      const matches = api.matches();
      if (matches && matches.size) return { focus: -1, set: new Set([...matches].filter((id) => index.has(id)).map((id) => index.get(id))) };
      return null;
    }

    function draw() {
      if (!shown) return;
      ctx.setTransform(box.ratio, 0, 0, box.ratio, 0, 0);
      ctx.clearRect(0, 0, box.w, box.hgt);
      const light = lit();
      const sel = api.selected();
      // links
      ctx.lineWidth = Math.max(0.5, Math.min(1.2, view.k * 0.8));
      for (const [a, b] of links) {
        const on = light && (a === light.focus || b === light.focus);
        if (light && !on && light.focus >= 0) ctx.strokeStyle = 'rgba(160, 175, 195, 0.035)';
        else ctx.strokeStyle = on ? 'rgba(127, 216, 255, 0.75)' : 'rgba(160, 175, 195, 0.16)';
        const [x1, y1] = toScreen(nodes[a]);
        const [x2, y2] = toScreen(nodes[b]);
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
      }
      // nodes
      for (let i = 0; i < nodes.length; i += 1) {
        const node = api.nodes.get(ids[i]);
        const [x, y] = toScreen(nodes[i]);
        const r = Math.max(1.6, radiusOf(nodes[i].degree) * view.k);
        if (x < -r || y < -r || x > box.w + r || y > box.hgt + r) continue;
        const dim = light && !light.set.has(i);
        const failed = api.live(ids[i])?.state === 'failed';
        ctx.globalAlpha = dim ? 0.18 : 1;
        ctx.fillStyle = failed ? '#ff4d4d' : settings.colors ? color(node.type) : i === light?.focus || ids[i] === sel ? '#e8f4ff' : '#aab2bd';
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill();
        if (ids[i] === sel) {
          ctx.strokeStyle = '#7fd8ff';
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(x, y, r + 3, 0, Math.PI * 2);
          ctx.stroke();
        }
      }
      ctx.globalAlpha = 1;
      // labels: hubs always, everything once zoomed in, the lit ones always
      ctx.font = `${Math.round(Math.max(10, Math.min(14, 11 * Math.sqrt(view.k))))}px -apple-system, "SF Pro Text", "Segoe UI", sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      const zoomed = Math.max(0, Math.min(1, (view.k - 1.9) / 0.6)); // labels fade in as you zoom in (none at the overview)
      for (let i = 0; i < nodes.length; i += 1) {
        const isLit = light && light.set.has(i);
        const show = isLit ? light.set.size <= 40 || i === light.focus : zoomed > 0 || nodes[i].degree >= 40;
        if (!show || (light && !isLit && light.focus >= 0)) continue;
        ctx.globalAlpha = isLit || nodes[i].degree >= 40 ? 1 : zoomed;
        const [x, y] = toScreen(nodes[i]);
        if (x < 0 || y < 0 || x > box.w || y > box.hgt) continue;
        const r = Math.max(1.6, radiusOf(nodes[i].degree) * view.k);
        const text = api.nodes.get(ids[i]).label;
        const label = text.length > 30 ? `${text.slice(0, 29)}…` : text;
        ctx.fillStyle = 'rgba(6, 10, 18, 0.75)';
        const w = ctx.measureText(label).width;
        ctx.fillRect(x - w / 2 - 3, y + r + 3, w + 6, 16);
        ctx.fillStyle = i === light?.focus ? '#ffffff' : 'rgba(217, 229, 245, 0.85)';
        ctx.fillText(label, x, y + r + 4);
      }
      ctx.globalAlpha = 1;
    }

    // --- camera -------------------------------------------------------------------------------------------------------
    function fit(animate = true) {
      if (!nodes.length) return;
      let minX = Infinity;
      let minY = Infinity;
      let maxX = -Infinity;
      let maxY = -Infinity;
      for (const n of nodes) {
        minX = Math.min(minX, n.x);
        minY = Math.min(minY, n.y);
        maxX = Math.max(maxX, n.x);
        maxY = Math.max(maxY, n.y);
      }
      const pad = 90;
      const k = Math.min(3, Math.max(0.15, Math.min((box.w - pad * 2) / (maxX - minX || 1), (box.hgt - pad * 2) / (maxY - minY || 1))));
      const target = { k, x: -((minX + maxX) / 2) * k, y: -((minY + maxY) / 2) * k + 20 };
      fitted = true;
      if (!animate || api.reduced.matches) {
        Object.assign(view, target);
        draw();
        return;
      }
      const from = { ...view };
      const t0 = performance.now();
      const run = (now) => {
        const t = Math.min(1, (now - t0) / 400);
        const e = 1 - (1 - t) ** 3;
        view.k = from.k + (target.k - from.k) * e;
        view.x = from.x + (target.x - from.x) * e;
        view.y = from.y + (target.y - from.y) * e;
        draw();
        if (t < 1) requestAnimationFrame(run);
      };
      requestAnimationFrame(run);
    }

    function zoomAt(factor, sx = box.w / 2, sy = box.hgt / 2) {
      const [wx, wy] = toWorld(sx, sy);
      view.k = Math.min(6, Math.max(0.1, view.k * factor));
      view.x = sx - box.w / 2 - wx * view.k;
      view.y = sy - box.hgt / 2 - wy * view.k;
      draw();
    }

    function nodeAt(sx, sy) {
      let best = -1;
      let bestD = Infinity;
      for (let i = 0; i < nodes.length; i += 1) {
        const [x, y] = toScreen(nodes[i]);
        const d = Math.hypot(x - sx, y - sy);
        const r = Math.max(6, radiusOf(nodes[i].degree) * view.k + 4);
        if (d < r && d < bestD) {
          best = i;
          bestD = d;
        }
      }
      return best;
    }

    // --- pointer: hover, drag a node, pan, pinch, wheel ----------------------------------------------------------
    const pointers = new Map();
    let pinch = null;
    const local = (e) => {
      const r = canvas.getBoundingClientRect();
      return [e.clientX - r.left, e.clientY - r.top];
    };
    canvas.addEventListener('pointerdown', (e) => {
      canvas.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, local(e));
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), k: view.k };
        dragging = null;
        return;
      }
      const [sx, sy] = local(e);
      const i = nodeAt(sx, sy);
      if (i >= 0) {
        dragging = { i, moved: 0, sx, sy };
        nodes[i].fixed = true;
      } else dragging = { pan: true, x: sx, y: sy, moved: 0 };
    });
    canvas.addEventListener('pointermove', (e) => {
      const [sx, sy] = local(e);
      if (pointers.has(e.pointerId)) pointers.set(e.pointerId, [sx, sy]);
      if (pinch && pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
        const k = Math.min(6, Math.max(0.1, (pinch.k * d) / (pinch.d || 1)));
        zoomAt(k / view.k, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2);
        return;
      }
      if (!dragging) {
        const i = nodeAt(sx, sy);
        if (i !== hovered) {
          hovered = i;
          canvas.style.cursor = i >= 0 ? 'pointer' : '';
          draw();
        }
        return;
      }
      if (dragging.pan) {
        dragging.moved += Math.abs(sx - dragging.x) + Math.abs(sy - dragging.y);
        view.x += sx - dragging.x;
        view.y += sy - dragging.y;
        dragging.x = sx;
        dragging.y = sy;
        draw();
        return;
      }
      dragging.moved += Math.abs(sx - dragging.sx) + Math.abs(sy - dragging.sy);
      dragging.sx = sx;
      dragging.sy = sy;
      const [wx, wy] = toWorld(sx, sy);
      Object.assign(nodes[dragging.i], { x: wx, y: wy, vx: 0, vy: 0 });
      hovered = dragging.i;
      reheat(0.3);
      draw();
    });
    const release = (e) => {
      pointers.delete(e.pointerId);
      if (pinch) {
        if (pointers.size < 2) pinch = null;
        return;
      }
      const d = dragging;
      dragging = null;
      if (!d) return;
      if (d.i !== undefined) {
        nodes[d.i].fixed = false;
        if (d.moved < 5) api.select(ids[d.i]);
        else reheat(0.2);
      } else if (d.moved < 5) {
        api.clear();
      }
    };
    canvas.addEventListener('pointerup', release);
    canvas.addEventListener('pointercancel', release);
    canvas.addEventListener('pointerleave', () => {
      if (hovered >= 0 && !dragging) {
        hovered = -1;
        draw();
      }
    });
    canvas.addEventListener('wheel', (e) => {
      e.preventDefault();
      const [sx, sy] = local(e);
      zoomAt(Math.exp(-e.deltaY * 0.0015), sx, sy);
    }, { passive: false }); // prettier-ignore
    addEventListener('resize', () => {
      if (!shown) return;
      box = size();
      draw();
    });

    // --- the page's calls ---------------------------------------------------------------------------------------------
    return {
      show() {
        host.hidden = false;
        shown = true;
        box = size();
        if (!ids.length) rebuild();
        else reheat(0.05);
        draw();
      },
      hide() {
        host.hidden = true;
        shown = false;
      },
      restyle: draw,
      rebuild,
      select(id) {
        if (settings.depth) return rebuild();
        if (!index.has(id)) return draw();
        const n = nodes[index.get(id)];
        const [x, y] = toScreen(n);
        if (x < 120 || y < 90 || x > box.w - 420 || y > box.hgt - 90) {
          view.x = -n.x * view.k - 150; // beside the card on the right
          view.y = -n.y * view.k;
        }
        draw();
      },
      clear() {
        if (settings.depth) return rebuild();
        draw();
      },
      fit,
    };
  }

  root.JarvisGraph2D = { create, simulate, seed, step, ring, radiusOf };
})(typeof window !== 'undefined' ? window : globalThis);
