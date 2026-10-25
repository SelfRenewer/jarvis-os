// Jarvis Lab: models in 3D, taken apart and turned with hands in the air (MediaPipe hand landmarks from the camera,
// all on this Mac — no frame leaves the browser; the page's CSP allows no outside connections). Mouse, touch and keys
// work too. URL hash commands for Jarvis: #explode, #assemble, #reset, #part=<id>, #model=<macbook|ring>.
// 16.10 (the user: «разные модели выбирать … чёткое разделение: в одной части разобранная модель, в другой крутить
// деталь … крутить с разных сторон, и снизу … кольцо отдельно, надеть на виртуальную руку»):
//   · a model that comes apart gets two halves — the whole model on the left, the chosen part on the right (a copy:
//     the model keeps it, lit), each turned on its own; a hand or the mouse works on the half it is over;
//   · turning is a trackball: any side, the bottom too;
//   · the ring is a model of its own; with the camera on, the tracked hand appears as a hologram and the ring goes onto
//     its ring finger when the finger comes to it (lab-hand.js does the math).
import * as THREE from './vendor/three/three.module.js';
import { applyExplode, PARTS_INFO } from './lab-model.js';
import { buildMacBookA, studioEnvironment } from './lab-model-a.js';
import { buildRing, R_IN, RING_INFO } from './lab-ring.js';
import { GestureMachine, pose } from './lab-gestures.js';
import { BONES, distanceToSegment, handPoints, ringOnFinger, vec } from './lab-hand.js';

const $ = (id) => document.getElementById(id);
const stage = $('stage');
const paneModel = $('pane-model');
const panePart = $('pane-part');
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
const V = (x = 0, y = 0, z = 0) => new THREE.Vector3(x, y, z);
const Q = () => new THREE.Quaternion();

// --- renderer and the two scenes ------------------------------------------------------------------------------------
// a logarithmic depth buffer: thin layers a fraction of a mm apart (shields, labels, pads) stop flickering when the camera
// zooms far in or out (17.10, the user: «при изменении масштаба багается текстура»)
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, logarithmicDepthBuffer: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.setScissorTest(true);
stage.appendChild(renderer.domElement);
const environment = studioEnvironment(renderer); // reflections for metal and glass (a studio made in code)

function lights(scene) {
  scene.environment = environment;
  scene.add(new THREE.HemisphereLight(0xbfe6ff, 0x0b1220, 0.35));
  const key = new THREE.DirectionalLight(0xffffff, 1.1);
  key.position.set(3, 6, 4);
  const rim = new THREE.DirectionalLight(0x3cc8ff, 0.7);
  rim.position.set(-4, 2, -5);
  const under = new THREE.DirectionalLight(0xcfe3ff, 0.6); // the bottom is lit too: models turn over now
  under.position.set(1, -6, 2);
  scene.add(key, rim, under);
}

const scene = new THREE.Scene(); // the left half: the whole model (and the hands in the ring mode)
lights(scene);
const camera = new THREE.PerspectiveCamera(40, 1, 0.03, 200);
const grid = new THREE.GridHelper(12, 24, 0x1f6f8b, 0x12324a);
grid.material.transparent = true;
grid.material.opacity = 0.35;
scene.add(grid);
const pivot = new THREE.Group(); // turned by hands / mouse (a trackball: any side)
scene.add(pivot);

const partScene = new THREE.Scene(); // the right half: one part
lights(partScene);
const front = new THREE.DirectionalLight(0xffffff, 0.9);
front.position.set(0.5, 1.5, 5);
partScene.add(front);
const partCam = new THREE.PerspectiveCamera(35, 1, 0.005, 200);
const partHolder = new THREE.Group();
partScene.add(partHolder);

// --- models ---------------------------------------------------------------------------------------------------------
const MODELS = {
  macbook: {
    title: 'MacBook Pro 16″ · M5 Pro',
    label: 'Трёхмерная модель MacBook Pro 16 дюймов на M5 Pro',
    build: () => buildMacBookA({ finish: 'silver' }),
    info: PARTS_INFO,
    split: true,
    start: [0.45, -0.6],
  },
  ring: {
    title: 'Кольцо',
    label: 'Трёхмерная модель кольца с бриллиантом',
    build: () => buildRing(),
    info: RING_INFO,
    split: false,
    start: [0.2, -0.45],
  },
};
let current = null;

/** Once per model: own materials per mesh (a part is lit alone), its resting pose, its size, centred on the pivot. */
function prepare(key) {
  const m = MODELS[key];
  const root = m.build();
  root.traverse((o) => {
    if (!o.isMesh) return;
    o.material = o.material.clone();
    o.userData.baseOpacity = o.material.opacity;
    o.userData.baseEmissive ??= o.material.emissive ? o.material.emissive.getHex() : 0;
    o.userData.partId ??= key;
  });
  for (const part of root.children) {
    part.userData.base ??= part.position.clone();
    part.userData.baseQuat = part.quaternion.clone();
    part.userData.baseScale = part.scale.clone();
  }
  root.updateMatrixWorld(true);
  const sphere = new THREE.Box3().setFromObject(root).getBoundingSphere(new THREE.Sphere());
  m.radius = sphere.radius;
  m.center = sphere.center.clone().sub(root.position); // in the model's own frame
  root.position.sub(sphere.center); // turning goes around the middle of the model
  m.root = root;
  if (key === 'ring') {
    const body = root.getObjectByName('ringBody');
    m.body = body;
    m.home = { parent: body.parent, pos: body.position.clone(), quat: body.quaternion.clone(), scale: body.scale.clone() };
  }
  return m;
}

const view = {
  zoom: 1, // × the distance that fits the model
  explode: 0,
  explodeTarget: 0,
  selected: null,
  hovered: null,
  locked: false, // gestures do not change the disassembly (buttons and Jarvis still do)
  carry: null, // a part taken by the hand or the mouse: {id, x, y}
  auto: false, // the ring turns by itself until someone touches it
  look: V(),
  cam: V(0, 2, 10),
};

const info = (id) => (current && id ? current.info[id] : null);
const partGroup = (id) => (current && id ? current.root.getObjectByName(id) : null);

function useModel(key) {
  const m = MODELS[key];
  if (!m || current === m) return;
  if (current) {
    view.carry = null;
    select(null);
    if (ring.worn) unwear(true);
    pivot.remove(current.root);
  }
  if (!m.root) prepare(key);
  current = m;
  pivot.add(m.root);
  document.body.classList.toggle('split', m.split);
  document.body.classList.toggle('single', !m.split);
  for (const b of document.querySelectorAll('[data-model]')) b.setAttribute('aria-pressed', String(b.dataset.model === key));
  paneModel.setAttribute('aria-label', m.label);
  grid.scale.setScalar(m.radius / 2.3);
  grid.position.y = -m.radius * 0.95;
  // the parts as chips under the model
  $('parts').replaceChildren(
    ...(m.split ? m.root.children.map((c) => c.name).filter((id) => m.info[id]) : []).map((id) => {
      const li = document.createElement('li');
      li.dataset.id = id;
      li.textContent = m.info[id][0];
      li.tabIndex = 0;
      li.addEventListener('click', () => select(id));
      li.addEventListener('keydown', (e) => (e.key === 'Enter' || e.key === ' ') && select(id));
      return li;
    }),
  );
  reset();
  if (!m.split) {
    const [title, about] = RING_INFO.ring;
    showCard(title, ['Модель в натуральную величину', about]);
  } else $('card').hidden = true;
  view.auto = !m.split && !reduced;
  hud(stream ? 'TRACKING' : 'LISTENING', m.split ? 'Крутите модель, выберите деталь — она появится справа' : ringHint());
}
for (const b of document.querySelectorAll('[data-model]')) b.addEventListener('click', () => useModel(b.dataset.model));

// --- the halves on screen -------------------------------------------------------------------------------------------
function resize() {
  renderer.setSize(innerWidth, innerHeight, false);
}
addEventListener('resize', resize);
resize();

const rectOf = (el) => el.getBoundingClientRect();
/** Which half is under a point (x, y in 0…1 of the window): 'model' | 'part' | null. */
function paneAt(x, y) {
  const px = x * innerWidth;
  const py = y * innerHeight;
  for (const [el, name] of [[panePart, 'part'], [paneModel, 'model']]) {
    const r = rectOf(el);
    if (r.width > 1 && px >= r.left && px <= r.right && py >= r.top && py <= r.bottom) return name;
  }
  return null;
}
/** A window point → the model half's normalized device coordinates. */
function modelNdc(x, y) {
  const r = rectOf(paneModel);
  return new THREE.Vector2(((x * innerWidth - r.left) / r.width) * 2 - 1, -(((y * innerHeight - r.top) / r.height) * 2 - 1));
}
/** A point of the model scene → the window (px). */
function toWindow(p) {
  const r = rectOf(paneModel);
  const s = p.clone().project(camera);
  return { x: r.left + (s.x * 0.5 + 0.5) * r.width, y: r.top + (-s.y * 0.5 + 0.5) * r.height, behind: s.z > 1 };
}

const raycaster = new THREE.Raycaster();
function partAt(x, y) {
  if (!current?.split || paneAt(x, y) !== 'model') return null;
  raycaster.setFromCamera(modelNdc(x, y), camera);
  const hit = raycaster.intersectObject(current.root, true)[0];
  const id = hit?.object?.userData?.partId;
  return info(id) ? id : null;
}

// --- turning: a trackball (any side, the bottom too) ----------------------------------------------------------------
const qa = Q();
const qb = Q();
function trackball(obj, cam, dx, dy) {
  const up = V().setFromMatrixColumn(cam.matrixWorld, 1).normalize();
  const right = V().setFromMatrixColumn(cam.matrixWorld, 0).normalize();
  obj.quaternion.premultiply(qa.setFromAxisAngle(up, dx)).premultiply(qb.setFromAxisAngle(right, dy)).normalize();
}
function turn(dx, dy) {
  view.auto = false;
  if (ring.worn) return; // the hand holds it now
  trackball(pivot, camera, dx, dy);
}
function zoom(factor) {
  view.zoom = Math.max(0.3, Math.min(3, view.zoom * factor));
}
function resetView() {
  const [x, y] = current.start;
  pivot.quaternion.setFromEuler(new THREE.Euler(x, y, 0, 'YXZ'));
  view.zoom = 1;
}
function reset() {
  resetView();
  explodeTo(0);
  setLock(false);
  if (current.split) select(null);
}

// --- parts: highlight, the label, the right half ---------------------------------------------------------------------
function setGlow(id, color) {
  partGroup(id)?.traverse((o) => {
    if (o.isMesh && o.material.emissive) o.material.emissive.setHex(color ?? o.userData.baseEmissive);
  });
}

/** Choose a part: lit in the model, its copy on the right. */
function select(id) {
  if (view.selected) setGlow(view.selected, null);
  view.selected = info(id) ? id : null;
  if (view.selected) setGlow(view.selected, 0x0d5f86);
  for (const li of $('parts').children) li.classList.toggle('on', li.dataset.id === view.selected);
  showPart(view.selected);
}

function hover(id) {
  if (view.hovered === id) return;
  if (view.hovered && view.hovered !== view.selected) setGlow(view.hovered, null);
  view.hovered = id;
  if (id && id !== view.selected) setGlow(id, 0x083a52);
}

const PART_START = Q().setFromEuler(new THREE.Euler(0.55, -0.6, 0, 'YXZ'));
const part = { id: null, clone: null, radius: 1, zoom: 1, auto: true, pop: 1 };

function showPart(id) {
  if (part.clone) {
    partHolder.remove(part.clone);
    part.clone.traverse((o) => o.isMesh && o.material.dispose());
    part.clone = null;
  }
  part.id = id;
  $('part-empty').hidden = Boolean(id);
  $('part-info').hidden = !id;
  $('part-title').textContent = id ? info(id)[0] : 'Деталь не выбрана';
  if (!id) return;
  $('part-about').textContent = info(id)[1];
  const src = partGroup(id);
  const clone = src.clone(true);
  clone.position.set(0, 0, 0);
  clone.quaternion.copy(src.userData.baseQuat);
  clone.scale.copy(src.userData.baseScale);
  for (const piece of clone.children) if (piece.userData.base) piece.position.copy(piece.userData.base); // speakers home
  clone.traverse((o) => {
    if (!o.isMesh) return;
    o.material = o.material.clone(); // its own colours: no highlight, not see-through
    if (o.material.emissive) o.material.emissive.setHex(o.userData.baseEmissive);
    o.material.opacity = 1;
  });
  clone.updateMatrixWorld(true);
  const sphere = new THREE.Box3().setFromObject(clone).getBoundingSphere(new THREE.Sphere());
  clone.position.sub(sphere.center);
  part.radius = Math.max(0.05, sphere.radius);
  part.clone = clone;
  partHolder.add(clone);
  partHolder.quaternion.copy(PART_START);
  part.zoom = 1;
  part.auto = !reduced;
  part.pop = reduced ? 1 : 0;
}
function turnPart(dx, dy) {
  if (!part.clone) return;
  part.auto = false;
  trackball(partHolder, partCam, dx, dy);
}
function zoomPart(factor) {
  part.zoom = Math.max(0.25, Math.min(3, part.zoom * factor));
}
$('btn-part-view').addEventListener('click', () => {
  partHolder.quaternion.copy(PART_START);
  part.zoom = 1;
});
$('btn-part-clear').addEventListener('click', () => select(null));

// --- explode / lock -------------------------------------------------------------------------------------------------
function explodeTo(value) {
  view.explodeTarget = Math.max(0, Math.min(1, value));
  $('explode').value = String(Math.round(view.explodeTarget * 100));
}
function setLock(on, why) {
  view.locked = on;
  const button = $('btn-lock');
  button.textContent = on ? '🔓 Снять фиксацию' : '🔒 Зафиксировать';
  button.setAttribute('aria-pressed', String(on));
  document.body.classList.toggle('locked', on);
  if (why) hud(on ? 'LOCKED' : 'TRACKING', why);
}
$('btn-explode').addEventListener('click', () => explodeTo(1));
$('btn-assemble').addEventListener('click', () => explodeTo(0));
$('btn-reset').addEventListener('click', reset);
$('btn-lock').addEventListener('click', () => setLock(!view.locked));
$('explode').addEventListener('input', (e) => explodeTo(Number(e.target.value) / 100));
$('card-close').addEventListener('click', () => ($('card').hidden = true));

// --- carrying a part to the right half (a hand: point, then pinch; the mouse: drag it) ------------------------------
const chip = $('chip');
function startCarry(id, x, y) {
  if (!info(id)) return false;
  view.carry = { id, x, y };
  chip.textContent = `${info(id)[0]} →`;
  chip.hidden = false;
  panePart.classList.add('drop');
  document.body.classList.add('carrying');
  setGlow(id, 0x0d5f86);
  moveCarry(x, y);
  return true;
}
function moveCarry(x, y) {
  if (!view.carry) return;
  view.carry.x = x;
  view.carry.y = y;
  chip.style.left = `${x * 100}%`;
  chip.style.top = `${y * 100}%`;
  panePart.classList.toggle('over', paneAt(x, y) === 'part');
}
function dropCarry() {
  const carry = view.carry;
  if (!carry) return;
  view.carry = null;
  chip.hidden = true;
  panePart.classList.remove('drop', 'over');
  document.body.classList.remove('carrying');
  if (paneAt(carry.x, carry.y) === 'part') {
    select(carry.id);
    hud('INSPECT', 'Деталь справа — крутите её щипком над правой половиной');
  } else {
    if (carry.id !== view.selected) setGlow(carry.id, null);
    hud('TRACKING', 'Отпустите деталь над правой половиной, чтобы рассмотреть');
  }
}

// --- the ring on the hand -------------------------------------------------------------------------------------------
const ring = { worn: false, hand: null, fit: null, anim: null, armed: true, up: null, lost: 0 };
const ringBody = () => MODELS.ring.body;
const ringHint = () => (stream ? 'Поднесите безымянный палец к кольцу — оно наденется · кулак — снять' : 'Включите камеру, чтобы надеть кольцо на руку · мышью — крутить');
const Y = V(0, 1, 0);

/** The ring's place on a hand: position, turn and scale in the scene. */
function fitOn(points) {
  const f = ringOnFinger(points, ring.up);
  ring.up = f.up;
  const basis = new THREE.Matrix4().makeBasis(V(...f.side), V(...f.up), V(...f.axis));
  return { pos: V(...f.at), quat: Q().setFromRotationMatrix(basis), scale: (f.radius * 0.85) / R_IN, f };
}

function wear(handKey) {
  const hand = handKey ? hands.get(handKey) : [...hands.values()].find((h) => h.visible);
  if (current !== MODELS.ring) return;
  if (!hand?.visible) {
    hud(stream ? 'TRACKING' : 'LISTENING', stream ? 'Покажите руку в камеру — тогда надену' : 'Сначала включите камеру');
    return;
  }
  const body = ringBody();
  scene.attach(body);
  ring.worn = true;
  ring.hand = hand.key;
  ring.anim = { t: 0, pos: body.position.clone(), quat: body.quaternion.clone(), scale: body.scale.x };
  MODELS.ring.root.visible = false; // the cushion steps away while the ring is on the hand
  view.auto = false;
  $('btn-wear').disabled = true;
  $('btn-unwear').disabled = false;
  hud('WORN', '💍 Кольцо на пальце · кулак или «Снять» — снять');
}

function unwear(now = false) {
  const body = ringBody();
  if (!ring.worn || !body) return;
  const home = MODELS.ring.home;
  home.parent.attach(body);
  ring.worn = false;
  ring.armed = false; // the finger must leave before it can go on again
  MODELS.ring.root.visible = true; // the cushion comes back for it
  ring.anim = now ? null : { t: 0, pos: body.position.clone(), quat: body.quaternion.clone(), scale: body.scale.x, home: true };
  if (now) {
    body.position.copy(home.pos);
    body.quaternion.copy(home.quat);
    body.scale.copy(home.scale);
  }
  $('btn-wear').disabled = false;
  $('btn-unwear').disabled = true;
  hud(stream ? 'TRACKING' : 'LISTENING', 'Кольцо на подушечке');
}
$('btn-wear').addEventListener('click', () => wear());
$('btn-unwear').addEventListener('click', () => unwear());
$('btn-reset-ring').addEventListener('click', () => {
  if (ring.worn) unwear(true);
  reset();
  view.auto = !reduced;
});

/** Each frame: the worn ring follows its finger (eased in when it goes on, eased home when it comes off). */
function updateRing(dt) {
  const body = ringBody();
  if (!body) return;
  const anim = ring.anim;
  if (ring.worn) {
    const hand = hands.get(ring.hand);
    if (hand?.visible) {
      const fit = fitOn(hand.points);
      ring.fit = fit;
      ring.palm = V(...vec.mean([0, 5, 9, 13, 17].map((i) => hand.points[i])));
      if (anim) {
        anim.t = Math.min(1, anim.t + dt / 0.35);
        const k = 1 - (1 - anim.t) ** 3;
        body.position.lerpVectors(anim.pos, fit.pos, k);
        body.quaternion.slerpQuaternions(anim.quat, fit.quat, k);
        body.scale.setScalar(anim.scale + (fit.scale - anim.scale) * k);
        if (anim.t >= 1) ring.anim = null;
      } else {
        body.position.lerp(fit.pos, 0.6); // a little smoothing against the tracker's jitter
        body.quaternion.slerp(fit.quat, 0.5);
        body.scale.setScalar(body.scale.x + (fit.scale - body.scale.x) * 0.5);
      }
    } // the hand is gone: the ring waits where it was
  } else if (anim?.home) {
    const home = MODELS.ring.home;
    anim.t = Math.min(1, anim.t + dt / 0.35);
    const k = 1 - (1 - anim.t) ** 3;
    body.position.lerpVectors(anim.pos, home.pos, k);
    body.quaternion.slerpQuaternions(anim.quat, home.quat, k);
    body.scale.setScalar(anim.scale + (home.scale.x - anim.scale) * k);
    if (anim.t >= 1) ring.anim = null;
  } else if (current === MODELS.ring && [...hands.values()].some((h) => h.visible)) {
    // goes on by itself when a ring finger passes through it
    const at = body.getWorldPosition(V());
    for (const hand of hands.values()) {
      if (!hand.visible) continue;
      const d = distanceToSegment([at.x, at.y, at.z], hand.points[13], hand.points[16]);
      if (!ring.armed && d > 0.35) ring.armed = true;
      if (ring.armed && d < 0.16) {
        wear(hand.key);
        break;
      }
    }
  }
}

// --- the hands as holograms (the ring mode) -------------------------------------------------------------------------
const TRY = { w: 3.2, h: 2.4, x: 0, y: 0.35, z: 0, scale: 1 }; // where a hand moves: a 4:3 box like the camera
const holo = (opacity) =>
  new THREE.MeshBasicMaterial({ color: 0x5ee7ff, transparent: true, opacity, depthWrite: false, blending: THREE.AdditiveBlending });
const sphereGeo = new THREE.SphereGeometry(1, 14, 10);
const boneGeo = new THREE.CylinderGeometry(1, 1, 1, 12, 1, true);
const hands = new Map(); // key (handedness) → {key, points, visible, joints, bones, palm}
const tmpM = new THREE.Matrix4();

function handView(key) {
  if (hands.has(key)) return hands.get(key);
  const joints = new THREE.InstancedMesh(sphereGeo, holo(0.85), 21);
  const bones = new THREE.InstancedMesh(boneGeo, holo(0.26), BONES.length);
  const palmGeo = new THREE.BufferGeometry();
  palmGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(5 * 9), 3));
  const palm = new THREE.Mesh(palmGeo, holo(0.1));
  palm.material.side = THREE.DoubleSide;
  for (const m of [joints, bones, palm]) {
    m.frustumCulled = false;
    m.visible = false;
    scene.add(m);
  }
  const h = { key, points: null, visible: false, joints, bones, palm };
  hands.set(key, h);
  return h;
}

const PALM_TRIS = [[0, 1, 5], [0, 5, 9], [0, 9, 13], [0, 13, 17], [1, 2, 5]];
function drawHolo(h, P) {
  const r = ringOnFinger(P, ring.up).radius; // the finger's thickness sets the hologram's
  const jointR = (i) => r * ([4, 8, 12, 16, 20].includes(i) ? 0.42 : i === 0 ? 0.8 : 0.5);
  P.forEach((p, i) => {
    const s = jointR(i);
    h.joints.setMatrixAt(i, tmpM.compose(V(...p), Q(), V(s, s, s)));
  });
  BONES.forEach(([a, b], i) => {
    const from = V(...P[a]);
    const dir = V(...P[b]).sub(from);
    const length = dir.length();
    const thick = r * ([a, b].every((k) => [0, 5, 9, 13, 17].includes(k)) ? 0.35 : a <= 4 && b <= 4 ? 0.8 : 0.72);
    h.bones.setMatrixAt(i, tmpM.compose(from.addScaledVector(dir, 0.5), Q().setFromUnitVectors(Y, dir.normalize()), V(thick, length, thick)));
  });
  h.joints.instanceMatrix.needsUpdate = true;
  h.bones.instanceMatrix.needsUpdate = true;
  const pos = h.palm.geometry.attributes.position;
  PALM_TRIS.forEach((tri, t) => tri.forEach((i, k) => pos.setXYZ(t * 3 + k, ...P[i])));
  pos.needsUpdate = true;
}

function showHands(result) {
  const seen = new Set();
  if (current === MODELS.ring) {
    (result.landmarks || []).forEach((lm, i) => {
      const world = result.worldLandmarks?.[i];
      if (!world) return;
      const key = result.handedness?.[i]?.[0]?.categoryName || `hand${i}`;
      const h = handView(seen.has(key) ? `${key}${i}` : key);
      seen.add(h.key);
      h.points = handPoints(lm, world, TRY);
      h.visible = true;
      drawHolo(h, h.points);
    });
  }
  if (current === MODELS.ring && seen.size) $('card').hidden = true;
  for (const h of hands.values()) {
    if (!seen.has(h.key)) h.visible = false;
    h.joints.visible = h.bones.visible = h.palm.visible = h.visible;
  }
}

// --- mouse / touch --------------------------------------------------------------------------------------------------
const winXY = (e) => [e.clientX / innerWidth, e.clientY / innerHeight];
let drag = null;
paneModel.addEventListener('pointerdown', (e) => {
  const [x, y] = winXY(e);
  drag = { x: e.clientX, y: e.clientY, moved: 0, part: partAt(x, y), carrying: false };
  paneModel.setPointerCapture(e.pointerId);
});
paneModel.addEventListener('pointermove', (e) => {
  const [x, y] = winXY(e);
  if (!drag) return hover(partAt(x, y));
  const dx = e.clientX - drag.x;
  const dy = e.clientY - drag.y;
  drag.moved += Math.abs(dx) + Math.abs(dy);
  drag.x = e.clientX;
  drag.y = e.clientY;
  if (drag.part && drag.moved > 6) {
    if (!drag.carrying) drag.carrying = startCarry(drag.part, x, y);
    if (drag.carrying) return moveCarry(x, y);
  }
  turn(dx * 0.008, dy * 0.008);
});
paneModel.addEventListener('pointerup', (e) => {
  if (drag?.carrying) dropCarry();
  else if (drag && drag.moved < 6) {
    const id = partAt(...winXY(e));
    if (id) select(id); // a click on empty space keeps the part on the right
  }
  drag = null;
});
paneModel.addEventListener('wheel', (e) => {
  e.preventDefault();
  zoom(Math.exp(e.deltaY * 0.0012));
}, { passive: false }); // prettier-ignore
paneModel.addEventListener('dblclick', () => resetView());

let pdrag = null;
panePart.addEventListener('pointerdown', (e) => {
  if (e.target.closest('button')) return;
  pdrag = { x: e.clientX, y: e.clientY };
  panePart.setPointerCapture(e.pointerId);
});
panePart.addEventListener('pointermove', (e) => {
  if (!pdrag) return;
  turnPart((e.clientX - pdrag.x) * 0.009, (e.clientY - pdrag.y) * 0.009);
  pdrag = { x: e.clientX, y: e.clientY };
});
panePart.addEventListener('pointerup', () => (pdrag = null));
panePart.addEventListener('wheel', (e) => {
  e.preventDefault();
  zoomPart(Math.exp(e.deltaY * 0.0012));
}, { passive: false }); // prettier-ignore
panePart.addEventListener('dblclick', (e) => {
  if (e.target.closest('button')) return;
  partHolder.quaternion.copy(PART_START);
  part.zoom = 1;
});

addEventListener('keydown', (e) => {
  if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return;
  if (!current?.split) return;
  if (e.key === 'e' || e.key === 'у') explodeTo(1);
  if (e.key === 'a' || e.key === 'ф') explodeTo(0);
  if (e.key === 'Escape') select(null);
  if (e.key === 'l' || e.key === 'д') setLock(!view.locked);
});

// URL hash commands (Jarvis opens e.g. /lab#explode, /lab#part=battery, /lab#model=ring)
function fromHash() {
  const h = decodeURIComponent(location.hash.slice(1));
  if (h.startsWith('model=')) return useModel(h.slice(6));
  if (h === 'ring' || h === 'part=ring') return useModel('ring');
  if (!current.split && (h.startsWith('part=') || h === 'explode' || h === 'assemble')) useModel('macbook');
  if (h === 'explode') explodeTo(1);
  else if (h === 'assemble') explodeTo(0);
  else if (h === 'reset') reset();
  else if (h.startsWith('part=') && info(h.slice(5))) {
    explodeTo(1);
    select(h.slice(5));
  }
}
addEventListener('hashchange', fromHash);

// --- the camera and the hands ---------------------------------------------------------------------------------------
const video = $('video');
const overlay = $('hands');
const octx = overlay.getContext('2d');
const cursor = $('cursor');
const machine = new GestureMachine();
let landmarker = null;
let stream = null;
let lastVideoTime = -1;

function hud(state, hint) {
  $('hud-state').textContent = state;
  $('hud-hint').textContent = hint;
  document.body.dataset.state = state.toLowerCase();
}

async function cameras() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  const pick = $('camera-pick');
  const was = pick.value;
  pick.replaceChildren(
    ...devices.filter((d) => d.kind === 'videoinput').map((d, i) => new Option(d.label || `Камера ${i + 1}`, d.deviceId)),
  );
  if (was) pick.value = was;
  pick.hidden = pick.options.length < 2;
}

async function startCamera() {
  $('btn-camera').disabled = true;
  hud('LOADING', 'Загружаю распознавание рук…');
  try {
    if (!landmarker) {
      const { FilesetResolver, HandLandmarker } = await import('./vendor/mediapipe/vision_bundle.mjs');
      const fileset = await FilesetResolver.forVisionTasks(new URL('./vendor/mediapipe/wasm', location.href).href);
      const options = (delegate) => ({
        baseOptions: { modelAssetPath: './vendor/mediapipe/hand_landmarker.task', delegate },
        runningMode: 'VIDEO',
        numHands: 2,
        minHandDetectionConfidence: 0.6,
        minHandPresenceConfidence: 0.6,
        minTrackingConfidence: 0.5,
      });
      try {
        landmarker = await HandLandmarker.createFromOptions(fileset, options('GPU'));
      } catch {
        landmarker = await HandLandmarker.createFromOptions(fileset, options('CPU'));
      }
    }
    const deviceId = $('camera-pick').value;
    stream?.getTracks().forEach((t) => t.stop());
    stream = await navigator.mediaDevices.getUserMedia({
      video: deviceId ? { deviceId: { exact: deviceId }, width: 640, height: 480 } : { width: 640, height: 480, facingMode: 'user' },
      audio: false,
    });
    video.srcObject = stream;
    await video.play();
    document.body.classList.add('camera-on');
    $('btn-identify').disabled = false;
    $('btn-camera').disabled = false;
    $('btn-camera').textContent = 'Выключить камеру';
    $('btn-camera').classList.remove('primary');
    hud('TRACKING', current.split ? 'Покажите руки в камеру' : ringHint());
    await cameras();
  } catch (err) {
    $('btn-camera').disabled = false;
    hud('OFFLINE', err?.name === 'NotAllowedError' ? 'Нет доступа к камере — разрешите его в браузере' : `Камера недоступна: ${err?.message || err}`);
  }
}
/** The camera off by a button (16.10: «не закрывать вкладку, чтобы её выключить»): the light goes out at once. */
function stopCamera() {
  dropCarry();
  stream?.getTracks().forEach((t) => t.stop());
  stream = null;
  video.srcObject = null;
  lastVideoTime = -1;
  document.body.classList.remove('camera-on');
  octx.clearRect(0, 0, overlay.width, overlay.height);
  cursor.hidden = true;
  showHands({});
  $('btn-identify').disabled = true;
  $('btn-camera').textContent = 'Включить камеру';
  $('btn-camera').classList.add('primary');
  hud('LISTENING', 'Камера выключена — крутите мышью или включите её снова');
}
$('btn-camera').addEventListener('click', () => (stream ? stopCamera() : startCamera()));

// «Что это за деталь?»: one frame → Jarvis (a vision model reads markings as printed) → the card.
const KINDS = { chip: 'микросхема', passive: 'пассивный компонент', connector: 'разъём', battery: 'аккумулятор',
  board: 'плата', cable: 'шлейф / кабель', fastener: 'крепёж', mechanical: 'механика', module: 'модуль', other: 'деталь' }; // prettier-ignore
const SURE = { low: 'не уверен', medium: 'скорее всего', high: 'по маркировке' };

function showCard(title, lines) {
  $('card-title').textContent = title;
  $('card-about').replaceChildren(...lines.filter(Boolean).map((line) => Object.assign(document.createElement('span'), { textContent: line })));
  $('card').hidden = false;
}

async function identify() {
  if (!stream || video.readyState < 2) return;
  const button = $('btn-identify');
  button.disabled = true;
  hud('THINKING', 'Смотрю на деталь…');
  try {
    const scale = Math.min(1, 1280 / video.videoWidth);
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(video.videoWidth * scale);
    canvas.height = Math.round(video.videoHeight * scale);
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
    const dataUrl = canvas.toDataURL('image/jpeg', 0.85);
    const response = await fetch('/api/system/identify', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ image: dataUrl.split(',')[1], mime: 'image/jpeg' }),
    });
    const found = await response.json();
    if (!response.ok) throw new Error(found.error || `HTTP ${response.status}`);
    showCard(found.name, [
      `${KINDS[found.kind] || 'деталь'} · ${SURE[found.confidence] || ''}`,
      found.purpose,
      found.markings?.length ? `Маркировка: ${found.markings.join(' · ')}` : '',
      found.device ? `Откуда: ${found.device}` : '',
      found.caution ? `⚠️ ${found.caution}` : '',
    ]);
    hud('TRACKING', 'Готово — карточка внизу');
  } catch (err) {
    showCard('Не получилось распознать', [String(err?.message || err)]);
    hud('OFFLINE', 'Распознавание не удалось');
  } finally {
    button.disabled = false;
  }
}
$('btn-identify').addEventListener('click', identify);
$('camera-pick').addEventListener('change', () => stream && startCamera());

function drawHands(landmarks) {
  overlay.width = overlay.clientWidth * devicePixelRatio;
  overlay.height = overlay.clientHeight * devicePixelRatio;
  octx.clearRect(0, 0, overlay.width, overlay.height);
  octx.fillStyle = '#5ee7ff';
  octx.strokeStyle = 'rgba(94,231,255,.55)';
  octx.lineWidth = 2 * devicePixelRatio;
  for (const lm of landmarks) {
    const pt = (i) => [(1 - lm[i].x) * overlay.width, lm[i].y * overlay.height];
    for (const [a, b] of BONES) {
      octx.beginPath();
      octx.moveTo(...pt(a));
      octx.lineTo(...pt(b));
      octx.stroke();
    }
    for (let i = 0; i < 21; i += 1) {
      octx.beginPath();
      octx.arc(...pt(i), 3 * devicePixelRatio, 0, Math.PI * 2);
      octx.fill();
    }
  }
}

const HINTS = {
  pinch: 'Щипок — кручу ту половину, над которой рука',
  'pinch+pinch': 'Две руки щипком — ближе-дальше',
  'palm+palm': 'Разведите ладони — разборка',
  palm: 'Открытая ладонь — разбираю…',
  fist: 'Кулак — собираю…',
  point: 'Указатель — задержите на детали · сожмите щипком, чтобы взять',
  thumb: '👍 — фиксация разборки',
};

/** The test hook: synthetic hands for a check without a camera (`window.labHands({landmarks, worldLandmarks})`). */
window.labHands = (result) => handleHands(result, performance.now());

function trackHands(now) {
  if (!landmarker || !stream || video.readyState < 2 || video.currentTime === lastVideoTime) return;
  lastVideoTime = video.currentTime;
  handleHands(landmarker.detectForVideo(video, now), now);
}

function handleHands(result, now) {
  const landmarks = result.landmarks || [];
  drawHands(landmarks);
  showHands(result);
  const poses = landmarks.map(pose);
  const kinds = poses.map((p) => p.kind).sort().join('+');
  const action = machine.update(poses, now);
  cursor.hidden = action.type !== 'cursor';
  const over = poses[0] ? paneAt(poses[0].point.x, poses[0].point.y) : null;

  if (current === MODELS.ring) {
    if (ring.worn) hud('WORN', '💍 Кольцо на пальце · кулак — снять');
    else hud(landmarks.length ? 'TRACKING' : 'LISTENING', landmarks.length ? 'Поднесите безымянный палец к кольцу' : ringHint());
    if (action.type === 'explodeTo' && action.value === 0 && ring.worn) unwear(); // a fist takes it off
    else if (action.type === 'rotate' && !ring.worn) turn(action.dx, action.dy);
    else if (action.type === 'zoom' && !ring.worn) zoom(action.factor);
    return;
  }

  hud(landmarks.length ? 'TRACKING' : 'LISTENING', landmarks.length ? HINTS[kinds] || 'Руки видны' : 'Покажите руки в камеру');
  if (view.carry) hud('CARRY', 'Несу деталь — отпустите над правой половиной');
  else if (view.locked && landmarks.length && (kinds.includes('palm') || kinds === 'fist')) hud('LOCKED', '🔒 Разборка зафиксирована · 👍 — снять');

  if (action.type === 'grab') {
    if (!startCarry(partAt(action.x, action.y), action.x, action.y)) hud('TRACKING', 'Под пальцем нет детали — наведите на деталь и сожмите');
  } else if (action.type === 'drag') moveCarry(action.x, action.y);
  else if (action.type === 'drop') dropCarry();
  else if (action.type === 'lock') setLock(!view.locked, view.locked ? 'Фиксация снята' : '🔒 Зафиксировал — ладонь и кулак не трогают разборку');
  else if (action.type === 'spreadEnd') {
    if (!view.locked) setLock(true, '🔒 Зафиксировал разборку · 👍 — снять · щипками крутите и приближайте');
  } else if (action.type === 'rotate') {
    if (over === 'part' && part.clone) turnPart(action.dx * 1.2, action.dy * 1.2);
    else turn(action.dx, action.dy);
  } else if (action.type === 'zoom') {
    const mid = poses.length > 1 ? paneAt((poses[0].point.x + poses[1].point.x) / 2, (poses[0].point.y + poses[1].point.y) / 2) : over;
    if (mid === 'part' && part.clone) zoomPart(action.factor);
    else zoom(action.factor);
  } else if (action.type === 'explodeTo') {
    if (action.value === 0 && over === 'part' && part.clone) select(null); // a fist over the right half clears it
    else if (view.locked) hud('LOCKED', '🔒 Разборка зафиксирована · 👍 — снять');
    else explodeTo(action.value);
  } else if (action.type === 'cursor') {
    cursor.style.left = `${action.x * 100}%`;
    cursor.style.top = `${action.y * 100}%`;
    cursor.style.setProperty('--progress', String(action.progress));
    hover(partAt(action.x, action.y));
  } else if (action.type === 'select') {
    const id = partAt(action.x, action.y);
    if (id) select(id);
  }
}

// --- loop -----------------------------------------------------------------------------------------------------------
const label = $('label');
function placeLabel() {
  const id = view.hovered || view.selected;
  const group = partGroup(id);
  if (!current?.split || !group || view.carry) {
    label.hidden = true;
    return;
  }
  const at = toWindow(new THREE.Box3().setFromObject(group).getCenter(V()));
  label.hidden = at.behind;
  label.textContent = info(id)[0];
  label.style.left = `${at.x}px`;
  label.style.top = `${at.y}px`;
}

function spinFans(root) {
  const fans = root?.getObjectByName('fans');
  if (!fans || reduced) return;
  for (const side of fans.children) {
    const rotor = side.getObjectByName('rotor');
    if (rotor) rotor.rotation.y += 0.25;
  }
}

/** The distance at which a sphere of radius r fills the view (both ways) of a camera with this aspect. */
function fitDistance(r, cam) {
  const v = THREE.MathUtils.degToRad(cam.fov) / 2;
  const h = Math.atan(Math.tan(v) * cam.aspect);
  return r / Math.sin(Math.min(v, h));
}

/** The distance at which a w × h box (half sizes) fills the view of a camera with this aspect. */
function frameDistance(halfW, halfH, cam) {
  const v = THREE.MathUtils.degToRad(cam.fov) / 2;
  return Math.max(halfH / Math.tan(v), halfW / (Math.tan(v) * cam.aspect));
}

function draw(el, sc, cam, r = rectOf(el)) {
  if (r.width < 2 || r.height < 2) return;
  cam.aspect = r.width / r.height;
  cam.updateProjectionMatrix();
  const y = innerHeight - r.bottom;
  renderer.setViewport(r.left, y, r.width, r.height);
  renderer.setScissor(r.left, y, r.width, r.height);
  renderer.render(sc, cam);
}

let last = performance.now();
function frame(now) {
  const dt = Math.min(0.1, (now - last) / 1000);
  last = now;
  trackHands(now);
  const ease = reduced ? 1 : 0.07;
  if (current.split) {
    view.explode += (view.explodeTarget - view.explode) * (reduced ? 1 : 0.12);
    applyExplode(current.root, view.explode);
    spinFans(current.root);
  }
  if (view.auto && !ring.worn) trackball(pivot, camera, 0.004, 0);
  updateRing(dt);

  // the left camera: at the middle of the model, stepping back as it comes apart; in the ring mode it frames the
  // hands when they are seen and the hand that wears the ring
  const e = current.split ? view.explode : 0;
  let goal = current.root.localToWorld(current.center.clone().add(V(0, 0.55 * e, -0.3 * e)));
  let distance = fitDistance(current.radius * (1 + 0.45 * e), camera) * 1.08 * view.zoom;
  let tilt = 0.4;
  if (current === MODELS.ring) {
    tilt = 0.12;
    const seen = [...hands.values()].some((h) => h.visible);
    if (ring.worn && ring.fit) {
      goal = ring.fit.pos.clone().lerp(ring.palm || ring.fit.pos, 0.4);
      distance = frameDistance(1.05, 1.05, camera) * view.zoom;
    } else if (seen) {
      goal = V(TRY.x, TRY.y - 0.15, 0);
      distance = frameDistance(TRY.w / 2, TRY.h / 2, camera) * 1.1 * view.zoom;
    }
  }
  view.look.lerp(goal, ease);
  view.cam.lerp(goal.clone().add(V(0, Math.sin(tilt) * distance, Math.cos(tilt) * distance)), ease);
  camera.near = Math.max(0.005, distance * 0.01); // the depth range follows the zoom
  camera.far = distance * 30 + current.radius * 4;
  camera.position.copy(view.cam);
  camera.lookAt(view.look);
  camera.updateMatrixWorld();
  placeLabel();
  draw(paneModel, scene, camera);

  // the right camera: the part alone, turning slowly until touched, popping in when it arrives
  if (current.split && part.clone) {
    if (part.auto) trackball(partHolder, partCam, 0.005, 0);
    part.pop = Math.min(1, part.pop + dt / 0.35);
    partHolder.scale.setScalar(0.6 + 0.4 * (1 - (1 - part.pop) ** 3));
    spinFans(part.clone.name === 'fans' ? partHolder : null);
    const d = fitDistance(part.radius, partCam) * 0.95 * part.zoom; // a flat part is smaller than its sphere
    partCam.near = Math.max(0.001, d * 0.01);
    partCam.far = d * 20 + part.radius * 4;
    partCam.position.set(0, Math.sin(0.2) * d, Math.cos(0.2) * d);
    partCam.lookAt(0, 0, 0);
    partCam.updateMatrixWorld();
    const r = rectOf(panePart);
    const head = rectOf(panePart.querySelector('.pane-head')).bottom + 8;
    const box = $('part-info');
    const foot = box.hidden ? r.bottom : rectOf(box).top - 8;
    draw(panePart, partScene, partCam, { left: r.left, width: r.width, top: head, bottom: foot, height: foot - head });
  }
  requestAnimationFrame(frame);
}

useModel('macbook');
fromHash();
requestAnimationFrame(frame);
hud('LISTENING', 'Включите камеру или крутите мышью');
