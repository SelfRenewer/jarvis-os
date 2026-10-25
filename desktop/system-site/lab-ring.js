// Jarvis Lab: the ring model (16.10, the user: «кольцо … отдельно, и его можно надеть на виртуальную руку») — a
// yellow-gold solitaire with a round brilliant in four platinum prongs, standing in a velvet cushion. Built from code
// at its real size (size 17.5, a 1 ct stone); the band with the head is the group «ringBody», which the lab moves
// onto the tracked hand. 1 unit = 100 mm; the ring stands upright (its axis along z), the stone on top (+y).
import * as THREE from './vendor/three/three.module.js';

export const RING_SCALE = 1;
export const RING_INFO = {
  ring: [
    'Кольцо',
    'Жёлтое золото 585 и бриллиант круглой огранки около 1 карата в четырёх платиновых лапках; ' +
      'размер 17,5, в натуральную величину. Включите камеру и поднесите безымянный палец — кольцо наденется.',
  ],
};

export const R_IN = 0.0875; // 17.5 mm inside
const R_OUT = R_IN + 0.018; // a 1.8 mm band
const HALF_W = 0.012; // 2.4 mm wide

/** The band: a comfort-fit D profile turned around the axis. */
function band(material) {
  const pts = [];
  const steps = 14;
  for (let i = 0; i <= steps; i += 1) {
    const a = -Math.PI / 2 + (Math.PI * i) / steps; // the rounded outside
    pts.push(new THREE.Vector2(R_IN + 0.004 + (R_OUT - R_IN - 0.004) * Math.cos(a), HALF_W * Math.sin(a)));
  }
  for (let i = 0; i <= 6; i += 1) {
    const a = Math.PI / 2 + (Math.PI * i) / 6; // the slightly domed inside
    pts.push(new THREE.Vector2(R_IN + 0.004 + 0.004 * Math.cos(a), HALF_W * Math.sin(a)));
  }
  const geometry = new THREE.LatheGeometry(pts, 128);
  geometry.rotateX(Math.PI / 2); // the axis along z: the ring stands
  return new THREE.Mesh(geometry, material);
}

/** A soft cushion: a rounded box w × h × d with bevelled edges, standing on y = 0. */
function cushionGeometry(w, h, d, r = 0.03, bevel = 0.018) {
  const s = new THREE.Shape();
  const x = -w / 2 + bevel;
  const y = -d / 2 + bevel;
  const W = w - bevel * 2;
  const D = d - bevel * 2;
  s.moveTo(x + r, y);
  s.lineTo(x + W - r, y);
  s.quadraticCurveTo(x + W, y, x + W, y + r);
  s.lineTo(x + W, y + D - r);
  s.quadraticCurveTo(x + W, y + D, x + W - r, y + D);
  s.lineTo(x + r, y + D);
  s.quadraticCurveTo(x, y + D, x, y + D - r);
  s.lineTo(x, y + r);
  s.quadraticCurveTo(x, y, x + r, y);
  const geometry = new THREE.ExtrudeGeometry(s, {
    depth: h - bevel * 2, bevelEnabled: true, bevelThickness: bevel, bevelSize: bevel, bevelSegments: 6, curveSegments: 12,
  }); // prettier-ignore
  geometry.rotateX(-Math.PI / 2);
  geometry.translate(0, bevel, 0);
  return geometry;
}

/** A round brilliant: pavilion, girdle, crown and table, faceted (16 sides, flat shading). */
function diamond() {
  const r = 0.032; // 6.4 mm across
  const pts = [
    new THREE.Vector2(0.0001, -0.0275), // the culet
    new THREE.Vector2(r, 0),
    new THREE.Vector2(r, 0.0012), // the girdle
    new THREE.Vector2(r * 0.57, 0.0108), // the table edge
    new THREE.Vector2(0.0001, 0.0108),
  ];
  const geometry = new THREE.LatheGeometry(pts, 16).toNonIndexed();
  geometry.computeVertexNormals();
  const material = new THREE.MeshPhysicalMaterial({
    color: 0xffffff,
    metalness: 0,
    roughness: 0,
    transmission: 1,
    thickness: 0.04,
    ior: 2.42,
    dispersion: 3,
    specularIntensity: 1,
    envMapIntensity: 3,
    flatShading: true,
  });
  return new THREE.Mesh(geometry, material);
}

export function buildRing() {
  const gold = new THREE.MeshStandardMaterial({ color: 0xf3c770, metalness: 1, roughness: 0.16 });
  const platinum = new THREE.MeshStandardMaterial({ color: 0xe9eaec, metalness: 1, roughness: 0.14 });
  const group = new THREE.Group();
  group.name = 'ring';
  const [title, about] = RING_INFO.ring;
  group.userData = { id: 'ring', title, about, explode: [0, 0, 0], item: true };

  // the cushion: 30 × 12 × 20 mm of dark velvet with a slit the ring stands in
  const cushion = new THREE.Mesh(
    cushionGeometry(0.3, 0.12, 0.2),
    new THREE.MeshPhysicalMaterial({ color: 0x16203a, roughness: 1, sheen: 1, sheenColor: 0x5a6f9e, sheenRoughness: 0.5 }),
  );
  group.add(cushion);
  const slit = new THREE.Mesh(new THREE.BoxGeometry(0.24, 0.002, 0.012), new THREE.MeshStandardMaterial({ color: 0x05070c, roughness: 1 }));
  slit.position.y = 0.1205;
  group.add(slit);

  const ring = new THREE.Group();
  ring.name = 'ringBody'; // the band and the head: this goes onto the hand
  ring.position.y = 0.12 + R_OUT - 0.03; // sunk 3 mm into the slit
  ring.add(band(gold));

  // the head: a little gallery ring under the stone, four prongs, the stone
  const top = R_OUT;
  const gallery = new THREE.Mesh(new THREE.TorusGeometry(0.019, 0.0025, 12, 48), platinum);
  gallery.rotation.x = Math.PI / 2;
  gallery.position.y = top + 0.006;
  ring.add(gallery);
  const girdleY = top + 0.03;
  for (let k = 0; k < 4; k += 1) {
    const a = Math.PI / 4 + (k * Math.PI) / 2;
    const from = new THREE.Vector3(Math.cos(a) * 0.012, top - 0.004, Math.sin(a) * 0.012);
    const to = new THREE.Vector3(Math.cos(a) * 0.031, girdleY + 0.0045, Math.sin(a) * 0.031);
    const prong = new THREE.Mesh(
      new THREE.TubeGeometry(new THREE.QuadraticBezierCurve3(from, new THREE.Vector3(Math.cos(a) * 0.03, top + 0.012, Math.sin(a) * 0.03), to), 16, 0.0028, 8),
      platinum,
    );
    ring.add(prong);
    const tip = new THREE.Mesh(new THREE.SphereGeometry(0.0034, 12, 8), platinum);
    tip.position.copy(to);
    ring.add(tip);
  }
  const stone = diamond();
  stone.position.y = girdleY;
  ring.add(stone);
  group.add(ring);

  group.scale.setScalar(RING_SCALE);
  return group;
}
