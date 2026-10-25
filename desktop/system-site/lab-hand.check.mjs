// node lab-hand.check.mjs — the hand-and-ring math of the lab on synthetic hands (no camera, no browser).
import assert from 'node:assert/strict';
import { distanceToSegment, handPoints, ringOnFinger } from './lab-hand.js';

/** A hand with the fingers up; `curl` > 0 bends the fingertips away from the camera, < 0 towards it. */
function hand(curl, shiftX = 0) {
  const world = [];
  const set = (i, x, y, z = 0) => (world[i] = { x, y, z });
  set(0, 0, 0.08);
  set(1, 0.035, 0.06);
  set(2, 0.05, 0.04);
  set(3, 0.06, 0.02);
  set(4, 0.065, 0);
  [0.03, 0.01, -0.01, -0.03].forEach((x, f) => {
    const base = 5 + f * 4;
    set(base, x, 0); // the knuckle
    set(base + 1, x, -0.04, curl * 0.3);
    set(base + 2, x, -0.065, curl * 0.7);
    set(base + 3, x, -0.085, curl);
  });
  const image = world.map((p) => ({ x: 0.5 + shiftX + p.x * 2, y: 0.5 + p.y * 2, z: p.z }));
  return { world, image };
}

const space = { w: 3.2, h: 2.4, x: 0, y: 0.6, z: 0, scale: 1 };
let checks = 0;
const ok = (cond, what) => {
  assert.ok(cond, what);
  checks += 1;
};

{
  const { world, image } = hand(0.02);
  const P = handPoints(image, world, space);
  const palm = [0, 5, 9, 13, 17].map((i) => P[i]).reduce((s, p) => [s[0] + p[0] / 5, s[1] + p[1] / 5, s[2] + p[2] / 5], [0, 0, 0]);
  const cy = [0, 5, 9, 13, 17].reduce((sum, i) => sum + image[i].y / 5, 0);
  const cx = [0, 5, 9, 13, 17].reduce((sum, i) => sum + image[i].x / 5, 0);
  ok(Math.abs(palm[0] - (0.5 - cx) * 3.2) < 1e-9 && Math.abs(palm[1] - ((0.5 - cy) * 2.4 + 0.6)) < 1e-9, 'the palm centre lands where it is in the picture');
  ok(P[12][1] > P[0][1], 'the fingers up in the picture → up in the scene');
  const r = ringOnFinger(P);
  ok(r.axis[1] > 0.9, 'the ring axis runs along the finger');
  ok(r.up[2] > 0.5, 'fingertips curling away from the camera: the back of the hand (the stone) faces the viewer');
  ok(Math.abs(r.radius - 0.45 * 0.2) < 0.01, 'the finger thickness from the knuckle spacing (≈ 9 mm radius)');
  const between = (a, b, p) => (p - a) * (p - b) <= 0;
  ok(between(P[13][1], P[14][1], r.at[1]), 'the ring sits between the knuckle and the middle joint');
  const d = r.side[0] * r.up[0] + r.side[1] * r.up[1] + r.side[2] * r.up[2];
  ok(Math.abs(d) < 1e-9, 'a right-angled frame');
}
{
  const { world, image } = hand(-0.02);
  const r = ringOnFinger(handPoints(image, world, space));
  ok(r.up[2] < -0.5, 'fingertips curling towards the camera: the palm faces the viewer, the stone looks away');
}
{
  const { world, image } = hand(0);
  const P = handPoints(image, world, space);
  ok(ringOnFinger(P).up[2] >= 0, 'a flat hand at first: the stone towards the viewer');
  ok(ringOnFinger(P, [0, 0, -1]).up[2] < 0, 'a flat hand keeps the side it had');
}
{
  const left = handPoints(hand(0.02, -0.3).image, hand(0.02, -0.3).world, space);
  ok(left[9][0] > 0.5, 'a hand on the left of the camera picture shows on the right (a mirror)');
}
ok(Math.abs(distanceToSegment([0, 1, 0], [0, 0, 0], [0, 2, 0])) < 1e-9, 'a point on the segment');
ok(Math.abs(distanceToSegment([1, 3, 0], [0, 0, 0], [0, 2, 0]) - Math.SQRT2) < 1e-9, 'past the end: to the end point');

console.log(`lab hand: ${checks} checks passed`);
