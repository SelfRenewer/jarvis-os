// node desktop/system-site/lab-gestures.check.mjs — synthetic hands through the gesture rules (no camera, no deps).
import assert from 'node:assert/strict';
import { GestureMachine, pose } from './lab-gestures.js';

function hand({ up = [], pinch = false, dx = 0, thumbUp = false } = {}) {
  const lm = Array.from({ length: 21 }, () => ({ x: 0.5 + dx, y: 0.7, z: 0 }));
  lm[0] = { x: 0.5 + dx, y: 0.8, z: 0 }; // wrist
  lm[9] = { x: 0.5 + dx, y: 0.6, z: 0 }; // middle MCP → hand size 0.2
  const cols = { index: 0.44, middle: 0.5, ring: 0.56, pinky: 0.62 };
  const ids = { index: [6, 8], middle: [10, 12], ring: [14, 16], pinky: [18, 20] };
  for (const [finger, [pip, tip]] of Object.entries(ids)) {
    lm[pip] = { x: cols[finger] + dx, y: 0.5, z: 0 };
    lm[tip] = { x: cols[finger] + dx, y: up.includes(finger) ? 0.35 : 0.62, z: 0 };
  }
  lm[4] = pinch ? { x: lm[8].x + 0.01, y: lm[8].y, z: 0 } : { x: 0.3 + dx, y: 0.6, z: 0 };
  if (thumbUp) lm[4] = { x: 0.4 + dx, y: 0.4, z: 0 }; // straight up, well above the knuckles
  return lm;
}

const ALL = ['index', 'middle', 'ring', 'pinky'];
assert.equal(pose(hand({ up: ALL })).kind, 'palm');
assert.equal(pose(hand({ up: [] })).kind, 'fist');
assert.equal(pose(hand({ up: ['index'] })).kind, 'point');
assert.equal(pose(hand({ up: ['index'], pinch: true })).kind, 'pinch');
assert.equal(pose(hand({ up: ['index', 'middle'] })).kind, 'other');
assert.ok(Math.abs(pose(hand({ up: ['index'] })).point.x - (1 - 0.44)) < 1e-9, 'x is mirrored');

// a pose counts only after `stable` frames; a held palm disassembles once
let m = new GestureMachine({ stable: 3, holdMs: 500 });
const palm = [pose(hand({ up: ALL }))];
assert.equal(m.update(palm, 0).type, 'idle');
assert.equal(m.update(palm, 100).type, 'idle');
assert.equal(m.update(palm, 200).type, 'idle');
assert.deepEqual(m.update(palm, 600), { type: 'explodeTo', value: 1 });
assert.equal(m.update(palm, 700).type, 'idle', 'fires once per hold');
const fist = [pose(hand({ up: [] }))];
for (const t of [800, 900, 1000]) m.update(fist, t);
assert.deepEqual(m.update(fist, 1400), { type: 'explodeTo', value: 0 });

// pinch-drag turns the model
m = new GestureMachine({ stable: 1 });
m.update([pose(hand({ up: ['index'], pinch: true }))], 0);
const turn = m.update([pose(hand({ up: ['index'], pinch: true, dx: -0.1 }))], 33);
assert.equal(turn.type, 'rotate');
assert.ok(turn.dx > 0, 'hand moved right on screen (mirrored) → positive yaw');

// two pinches → zoom; two palms spread → explode gradually
m = new GestureMachine({ stable: 1 });
const pinchAt = (dx) => pose(hand({ up: ['index'], pinch: true, dx }));
m.update([pinchAt(-0.1), pinchAt(0.1)], 0);
const zoom = m.update([pinchAt(-0.2), pinchAt(0.2)], 33);
assert.equal(zoom.type, 'zoom');
assert.ok(zoom.factor < 1, 'hands apart → closer');
m = new GestureMachine({ stable: 1 });
const spread = m.update([pose(hand({ up: ALL, dx: -0.3 })), pose(hand({ up: ALL, dx: 0.3 }))], 0);
assert.equal(spread.type, 'explodeTo');
assert.ok(spread.value > 0.5);

// pointing still for the dwell time selects
m = new GestureMachine({ stable: 1, dwellMs: 800 });
const point = [pose(hand({ up: ['index'] }))];
assert.equal(m.update(point, 0).type, 'cursor');
assert.equal(m.update(point, 500).type, 'cursor');
assert.equal(m.update(point, 900).type, 'select');
assert.equal(m.update(point, 1000).type, 'cursor', 'selects once per dwell');

// 👍 held → lock (once per hold); a plain fist is not a thumb
assert.equal(pose(hand({ up: [], thumbUp: true })).kind, 'thumb');
assert.equal(pose(hand({ up: [] })).kind, 'fist');
m = new GestureMachine({ stable: 1, lockMs: 500 });
const thumb = [pose(hand({ up: [], thumbUp: true }))];
assert.equal(m.update(thumb, 0).type, 'idle');
assert.deepEqual(m.update(thumb, 600), { type: 'lock' });
assert.equal(m.update(thumb, 700).type, 'idle', 'locks once per hold');

// spreading two palms and letting go → spreadEnd (the page locks that spread); without a spread → nothing
m = new GestureMachine({ stable: 1 });
m.update([pose(hand({ up: ALL, dx: -0.1 })), pose(hand({ up: ALL, dx: 0.1 }))], 0);
m.update([pose(hand({ up: ALL, dx: -0.3 })), pose(hand({ up: ALL, dx: 0.3 }))], 33);
assert.deepEqual(m.update([], 66), { type: 'spreadEnd' });
m = new GestureMachine({ stable: 1 });
m.update([pose(hand({ up: ALL, dx: -0.2 })), pose(hand({ up: ALL, dx: 0.2 }))], 0);
assert.equal(m.update([], 33).type, 'idle', 'palms shown but not moved: no spreadEnd');

// point at a part, then pinch → grab it there; moving the pinch drags; letting go drops
m = new GestureMachine({ stable: 2 });
const pointAt = [pose(hand({ up: ['index'] }))];
m.update(pointAt, 0);
m.update(pointAt, 33);
const pinchHere = [pose(hand({ up: ['index'], pinch: true }))];
assert.equal(m.update(pinchHere, 66).type, 'idle', 'the pinch must be stable first');
const grab = m.update(pinchHere, 99);
assert.equal(grab.type, 'grab');
assert.ok(Math.abs(grab.x - (1 - 0.44)) < 1e-9, 'grabbed where the finger pointed');
const drag = m.update([pose(hand({ up: ['index'], pinch: true, dx: -0.1 }))], 132);
assert.equal(drag.type, 'drag');
assert.ok(drag.x > grab.x, 'carried right (mirrored)');
m.update([pose(hand({ up: ALL }))], 165); // one flickering frame…
assert.equal(m.update(pinchHere, 198).type, 'idle'); // …back to the pinch: not stable yet, nothing dropped
assert.equal(m.update(pinchHere, 231).type, 'drag', 'still carried after a flicker');
m.update([], 264);
assert.deepEqual(m.update([], 297), { type: 'drop' });

// a pinch without pointing first turns the model (no grab)
m = new GestureMachine({ stable: 1 });
assert.equal(m.update(pinchHere, 0).type, 'idle');
assert.equal(m.update([pose(hand({ up: ['index'], pinch: true, dx: -0.1 }))], 33).type, 'rotate');

console.log('lab gestures: all checks passed');
