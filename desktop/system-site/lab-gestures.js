// Hand gestures from MediaPipe's 21 landmarks (x, y in 0…1 of the frame; the camera image is mirrored like a
// selfie, so x is flipped here: moving the hand right moves things right). Pure functions + a small state machine,
// no DOM — checked by `node lab-gestures.check.mjs`.
//
//   pinch (thumb + index)        one hand: grab and turn the model; two hands: zoom (and turn)
//   open palm, held              disassemble          · two open palms: spread them apart → disassemble gradually
//   fist, held                   assemble
//   point (only the index up)    a cursor; holding it on a part for ~1 s opens its card
//   point, then pinch            take the part under the cursor and carry it (drag); let go → drop
//   thumb up 👍, held            lock / unlock the disassembly (16.10: spread, then zoom without undoing it)
//   two palms spread, then let go → «spreadEnd»: the page locks the spread it reached

const TIP = { thumb: 4, index: 8, middle: 12, ring: 16, pinky: 20 };
const PIP = { index: 6, middle: 10, ring: 14, pinky: 18 };
const WRIST = 0;
const THUMB_IP = 3;
const INDEX_MCP = 5;
const MIDDLE_MCP = 9;

const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y, (a.z || 0) - (b.z || 0));

export function handSize(lm) {
  return dist(lm[WRIST], lm[MIDDLE_MCP]) || 1e-6;
}

export function extended(lm, finger) {
  return dist(lm[TIP[finger]], lm[WRIST]) > dist(lm[PIP[finger]], lm[WRIST]) * 1.12;
}

/** One hand's pose: pinch | point | palm | fist | other, with a screen point (mirrored x). */
export function pose(lm) {
  const size = handSize(lm);
  const fingers = ['index', 'middle', 'ring', 'pinky'].filter((f) => extended(lm, f));
  const pinchGap = dist(lm[TIP.thumb], lm[TIP.index]) / size;
  const at = (i) => ({ x: 1 - lm[i].x, y: lm[i].y });
  if (pinchGap < 0.35) {
    const p = { x: 1 - (lm[TIP.thumb].x + lm[TIP.index].x) / 2, y: (lm[TIP.thumb].y + lm[TIP.index].y) / 2 };
    return { kind: 'pinch', point: p, size };
  }
  if (fingers.length === 1 && fingers[0] === 'index') return { kind: 'point', point: at(TIP.index), size };
  if (fingers.length >= 4) return { kind: 'palm', point: at(MIDDLE_MCP), size };
  if (fingers.length === 0) {
    // 👍: the thumb straight and well above the knuckles (in a fist it lies across the fingers)
    const up =
      lm[TIP.thumb].y < lm[INDEX_MCP].y - 0.5 * size &&
      dist(lm[TIP.thumb], lm[WRIST]) > dist(lm[THUMB_IP], lm[WRIST]) * 1.1;
    return { kind: up ? 'thumb' : 'fist', point: at(MIDDLE_MCP), size };
  }
  return { kind: 'other', point: at(MIDDLE_MCP), size };
}

/**
 * Turns frames of poses into actions. Every pose must hold for `stable` frames before it counts (no flicker);
 * held gestures fire once per hold. Actions: rotate {dx, dy} · zoom {factor} · explodeTo {value} · cursor {x, y} ·
 * select {x, y} · grab {x, y} · drag {x, y} · drop · lock · spreadEnd · idle. A change of pose is acted on only once
 * the new pose is stable too, so a one-frame flicker never drops a carried part or ends a spread.
 */
export class GestureMachine {
  constructor({ stable = 3, holdMs = 700, dwellMs = 1000, rotateGain = 4.2, lockMs = 600, grabWindowMs = 700 } = {}) {
    Object.assign(this, { stable, holdMs, dwellMs, rotateGain, lockMs, grabWindowMs });
    this.last = null; // the last stable pose: {key, point, time}
    this.grabbing = false;
    this.spread = false;
    this.kinds = [];
    this.count = 0;
    this.since = 0;
    this.fired = false;
    this.lastPinch = null;
    this.lastSpan = null;
    this.palmSpan0 = null;
    this.dwell = null;
  }

  update(poses, now) {
    const kinds = poses.map((p) => p.kind).sort();
    const same = kinds.join() === this.kinds.join();
    this.count = same ? this.count + 1 : 1;
    if (!same) {
      this.kinds = kinds;
      this.since = now;
      this.fired = false;
      this.lastPinch = null;
      this.lastSpan = null;
      this.palmSpan0 = null;
      this.dwell = null;
    }
    if (this.count < this.stable) return { type: 'idle' };
    const key = kinds.join('+');
    const held = now - this.since;
    const prev = this.last;
    const first = this.count === this.stable; // the first frame this pose counts
    this.last = { key, point: poses[0] ? poses[0].point : null, time: now };

    if (first && prev && prev.key !== key) {
      if (this.grabbing) {
        this.grabbing = false;
        return { type: 'drop' };
      }
      if (prev.key === 'palm+palm' && this.spread) {
        this.spread = false;
        return { type: 'spreadEnd' };
      }
      if (key === 'pinch' && prev.key === 'point' && prev.point && now - prev.time <= this.grabWindowMs) {
        this.grabbing = true;
        this.grabAt = prev.point;
        this.grabFrom = poses[0].point;
        return { type: 'grab', x: prev.point.x, y: prev.point.y };
      }
    }

    if (key === 'pinch' && this.grabbing) {
      const p = poses[0].point;
      return { type: 'drag', x: this.grabAt.x + (p.x - this.grabFrom.x), y: this.grabAt.y + (p.y - this.grabFrom.y) };
    }
    if (key === 'thumb' && held >= this.lockMs && !this.fired) {
      this.fired = true;
      return { type: 'lock' };
    }
    if (key === 'pinch') {
      const p = poses[0].point;
      const last = this.lastPinch;
      this.lastPinch = p;
      if (!last) return { type: 'idle' };
      return { type: 'rotate', dx: (p.x - last.x) * this.rotateGain, dy: (p.y - last.y) * this.rotateGain };
    }
    if (key === 'pinch+pinch') {
      const span = dist(poses[0].point, poses[1].point);
      const last = this.lastSpan;
      this.lastSpan = span;
      if (!last) return { type: 'idle' };
      return { type: 'zoom', factor: last / Math.max(span, 1e-3) };
    }
    if (key === 'palm+palm') {
      const span = dist(poses[0].point, poses[1].point);
      if (this.palmSpan0 === null) this.palmSpan0 = span;
      if (Math.abs(span - this.palmSpan0) > 0.04) this.spread = true;
      return { type: 'explodeTo', value: Math.max(0, Math.min(1, (span - 0.18) / 0.45)) };
    }
    if ((key === 'palm' || key === 'fist') && held >= this.holdMs && !this.fired) {
      this.fired = true;
      return { type: 'explodeTo', value: key === 'palm' ? 1 : 0 };
    }
    if (key === 'point') {
      const p = poses[0].point;
      if (!this.dwell || dist(p, this.dwell.at) > 0.035) this.dwell = { at: p, since: now, fired: false };
      if (!this.dwell.fired && now - this.dwell.since >= this.dwellMs) {
        this.dwell.fired = true;
        return { type: 'select', x: p.x, y: p.y };
      }
      return { type: 'cursor', x: p.x, y: p.y, progress: Math.min(1, (now - this.dwell.since) / this.dwellMs) };
    }
    return { type: 'idle' };
  }
}
