// Jarvis Lab: the tracked hand as 3D points and where a ring sits on it. Pure math (no DOM, no three.js) — checked by
// `node lab-hand.check.mjs`. MediaPipe gives every hand 21 landmarks twice: in the picture (x, y in 0…1, the camera
// not mirrored) and in metres around the hand's centre (worldLandmarks). The page shows a mirror like a selfie, so x
// is flipped; y goes up; z comes towards the viewer. Points are [x, y, z] in scene units (1 = 100 mm).

export const PALM = [0, 5, 9, 13, 17];
export const TIPS = [8, 12, 16, 20];
/** The bones to draw: the five fingers from the wrist and the knuckle line. */
export const BONES = [
  [0, 1], [1, 2], [2, 3], [3, 4],
  [0, 5], [5, 6], [6, 7], [7, 8],
  [9, 10], [10, 11], [11, 12],
  [13, 14], [14, 15], [15, 16],
  [0, 17], [17, 18], [18, 19], [19, 20],
  [5, 9], [9, 13], [13, 17],
]; // prettier-ignore

const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const mul = (a, k) => [a[0] * k, a[1] * k, a[2] * k];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const len = (a) => Math.hypot(a[0], a[1], a[2]);
const unit = (a) => mul(a, 1 / (len(a) || 1e-9));
const mean = (pts) => mul(pts.reduce(add, [0, 0, 0]), 1 / pts.length);
export const vec = { sub, add, mul, dot, cross, len, unit, mean };

/**
 * The hand in the scene: 21 points placed by where the hand is in the picture (its palm centre maps into `space`, a
 * w × h box around (x, y) at depth z), shaped by its metric landmarks (× 10 → units, × space.scale).
 */
export function handPoints(image, world, space) {
  const c = PALM.reduce((s, i) => [s[0] + image[i].x / 5, s[1] + image[i].y / 5], [0, 0]);
  const center = [(0.5 - c[0]) * space.w + space.x, (0.5 - c[1]) * space.h + space.y, space.z];
  const w0 = PALM.reduce((s, i) => add(s, mul([world[i].x, world[i].y, world[i].z || 0], 1 / 5)), [0, 0, 0]);
  const k = 10 * (space.scale || 1);
  return world.map((p) => [center[0] - (p.x - w0[0]) * k, center[1] - (p.y - w0[1]) * k, center[2] - ((p.z || 0) - w0[2]) * k]);
}

/**
 * Where a ring sits on the ring finger: on the first bone, 42 % of the way from the knuckle (13) to the middle joint
 * (14); its axis along the finger; «up» — where the stone looks — to the back of the hand; its size from the finger's
 * thickness, which is taken as 0.45 of the knuckle spacing. The back of the hand is the side the fingertips do not
 * curl to; a flat hand keeps the side it had (`prevUp`), at first the one facing the viewer.
 */
export function ringOnFinger(P, prevUp = null) {
  const along = sub(P[14], P[13]);
  const bone = len(along) || 1e-6;
  const axis = mul(along, 1 / bone);
  const at = add(P[13], mul(along, 0.42));
  const span = (len(sub(P[13], P[9])) + len(sub(P[13], P[17]))) / 2;
  const radius = span * 0.45;
  let n = unit(cross(sub(P[5], P[0]), sub(P[17], P[0])));
  const curl = dot(sub(mean(TIPS.map((i) => P[i])), mean(PALM.map((i) => P[i]))), n) / (len(sub(P[9], P[0])) || 1);
  if (Math.abs(curl) > 0.12) {
    if (curl > 0) n = mul(n, -1); // the fingertips curl to the palm: the back is the other side
  } else if (prevUp ? dot(n, prevUp) < 0 : n[2] < 0) {
    n = mul(n, -1);
  }
  const up = unit(sub(n, mul(axis, dot(n, axis))));
  const side = cross(up, axis); // x = y × z: a right-handed frame with z along the finger
  return { at, axis, up, side, radius, bone };
}

/** The shortest distance from a point to the segment a–b (is the ring finger through the ring?). */
export function distanceToSegment(p, a, b) {
  const ab = sub(b, a);
  const t = Math.max(0, Math.min(1, dot(sub(p, a), ab) / (dot(ab, ab) || 1e-9)));
  return len(sub(p, add(a, mul(ab, t))));
}
