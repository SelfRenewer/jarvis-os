// node graph2d.check.mjs — the graph view's physics on a graph like Jarvis's (a big hub, small clusters, orphans):
// it settles, nothing flies off, hubs sit in the middle, orphans stand on a ring outside, neighbours stay near.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const sandbox = {};
vm.runInNewContext(readFileSync(join(HERE, 'graph2d.js'), 'utf8'), sandbox);
const { simulate, seed, step, ring, radiusOf } = sandbox.JarvisGraph2D;
let checks = 0;
const ok = (cond, what) => {
  assert.ok(cond, what);
  checks += 1;
};

// 1 hub with 120 leaves, 3 small clusters of 6 hanging off it, 12 orphans
const links = [];
let n = 1;
for (let i = 0; i < 120; i += 1) links.push([0, n++]);
const clusterHubs = [];
for (let c = 0; c < 3; c += 1) {
  const hub = n++;
  clusterHubs.push(hub);
  links.push([0, hub]);
  for (let i = 0; i < 6; i += 1) links.push([hub, n++]);
}
const orphanFrom = n;
n += 12;

const nodes = simulate(n, links, {}, 400);
const finite = nodes.every((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
ok(finite, 'every position is a number');
const dist = (p) => Math.hypot(p.x, p.y);
const connected = nodes.filter((p) => !p.orphan);
const reach = Math.max(...connected.map(dist));
ok(reach < 2000, `nothing flies off (reach ${Math.round(reach)})`);
ok(dist(nodes[0]) < reach * 0.15, 'the big hub sits in the middle');
const orphans = nodes.slice(orphanFrom);
ok(orphans.every((p) => p.orphan), 'nodes with no link are orphans');
const radii = orphans.map(dist);
ok(Math.max(...radii) - Math.min(...radii) < 1e-6, 'orphans on one circle');
ok(Math.min(...radii) > reach + 50, 'the circle is outside everything else');
const leafD = links.slice(0, 120).map(([a, b]) => Math.hypot(nodes[a].x - nodes[b].x, nodes[a].y - nodes[b].y));
const avgLeaf = leafD.reduce((s, d) => s + d, 0) / leafD.length;
ok(avgLeaf > 20 && avgLeaf < 400, `leaves near their hub (${Math.round(avgLeaf)})`);
const clusterSpread = clusterHubs.map((hub, c) => {
  const kids = links.filter(([a]) => a === hub).map(([, b]) => b);
  return kids.reduce((s, k) => s + Math.hypot(nodes[k].x - nodes[hub].x, nodes[k].y - nodes[hub].y), 0) / kids.length;
});
ok(clusterSpread.every((d) => d < avgLeaf * 2.5), 'a small cluster stays together');

// it settles: the motion of a tick goes down to almost nothing
const again = seed(n, links);
let alpha = 1;
const motion = [];
for (let t = 0; t < 500; t += 1) {
  motion.push(step(again, links, { alpha }));
  alpha = Math.max(0.02, alpha * 0.985);
}
ok(motion[499] < motion[10] * 0.05 && motion[499] < 0.05, `it settles (${motion[10].toFixed(2)} → ${motion[499].toFixed(4)})`);

// the same input → the same picture (no randomness)
const a = simulate(30, [[0, 1], [1, 2], [2, 0]]);
const b = simulate(30, [[0, 1], [1, 2], [2, 0]]);
ok(a.every((p, i) => p.x === b[i].x && p.y === b[i].y), 'deterministic');

// a held node stays where it is held
const held = seed(5, [[0, 1], [1, 2]]);
Object.assign(held[1], { x: 500, y: 500, fixed: true });
for (let t = 0; t < 50; t += 1) step(held, [[0, 1], [1, 2]], { alpha: 1 });
ok(held[1].x === 500 && held[1].y === 500, 'a dragged node stays put');

// forces: more repulsion spreads it wider
const tight = simulate(40, Array.from({ length: 39 }, (_, i) => [0, i + 1]), { repulsion: 0.5 });
const wide = simulate(40, Array.from({ length: 39 }, (_, i) => [0, i + 1]), { repulsion: 2.5 });
const spread = (ps) => ps.reduce((s, p) => s + dist(p), 0) / ps.length;
ok(spread(wide) > spread(tight) * 1.2, 'repulsion widens the graph');
ok(ring(seed(3, [[0, 1], [1, 2]])) === 0, 'no orphans → no ring');
ok(radiusOf(123) > radiusOf(1) * 3, 'a hub is drawn bigger');

console.log(`graph2d: ${checks} checks passed`);
