// The lab's MacBook: the user's MacBook Pro 16″, M5 Pro, silver (17.10: the only one — the first 14″ schematic and the
// 14″ size are gone). Built from code (no downloaded model, no image in it). Outside: Apple's tech specs (35.57 × 24.81
// × 1.68 cm, 16.2″ 3456 × 2234 display, MagSafe 3 + three Thunderbolt 5 + HDMI + SDXC + headphone jack, six speakers,
// 100 Wh), key pitch 18.5 mm, and the user's 11 photos (16.10): the keyboard well 21 mm from the hinge edge, the
// trackpad 161 × 103 mm, grilles 34 mm wide, all ports in the rear half, four 20 mm feet, eight screws, the intake and
// exhaust slits, the EN/RU legends, the logo on the lid, the user's main screen on the display.
// Inside (17.10): teardown frames of the 16″ the user sent — the bottom cover's inner face (insulation sheet, the
// gasket and three shield plates with spring contacts at the hinge, foam, rubber stops), two blower fans with the fin
// stacks behind them and U-shaped heat pipes over the processor, the logic board across the whole width cut out round
// the fans (the processor on its underside), six cells in a steel frame with the trackpad's flex between the middle
// ones and its copper contacts at the front, a tweeter and a woofer box at each front corner, the keyboard's riveted
// plate under the top case, the hinge mounts and the antenna bar. Positions are read off the frames (≈ 4 px a mm), so
// they are close, not exact. Also (17.10): Apple's render of the 16″ inside (the M5 Pro board's underside: the
// processor's silver plate, two rimmed shields, gold screw pads, side cable covers), the iFixit logic board guide
// (the heatsink is on the board's top side; eight P5 screws, the antenna bracket, cable covers) and a photo of the
// trackpad's underside (frame, Taptic Engine, three boards, sensors, the bracket with copper contacts). 1 unit = 100 mm. x — left/right, y — up, z — towards the viewer.
import * as THREE from './vendor/three/three.module.js';
import { PARTS_INFO } from './lab-model.js';

export const SIZES = {
  16: { // from the user's photos: kbTop = the first key row from the hinge edge, gap = last key row → trackpad
    label: 'MacBook Pro 16″',
    W: 3.557, D: 2.481, BASE: 0.118, LID: 0.05, R: 0.12,
    kbTop: 0.23, trackpad: [1.61, 1.03], gap: 0.05, grille: 0.34,
    screen: [3.456, 2.234], chin: 0.18, notch: 0.4, logo: 0.37,
    feet: [0.24, 0.1], screwsX: 0.58,
    left: { magsafe: 0.35, tb: [0.53, 0.68], jack: 0.815 }, // port centres from the hinge edge
    right: { hdmi: 0.37, tb: 0.57, sd: 0.84 },
    intake: [0.9, 2.07], // the slits under the sides, from the hinge edge
  },
}; // prettier-ignore
const KEY_PITCH = 0.185; // 18.5 mm
const KB_ROWS = 6;

export const FINISHES = {
  black: { name: 'Космический чёрный', body: 0x2c2e31, edge: 0x3a3d41 },
  silver: { name: 'Серебристый', body: 0xc8cbcf, edge: 0xe2e4e6 },
};

const std = (color, extra = {}) => new THREE.MeshStandardMaterial({ color, roughness: 0.5, metalness: 0.1, ...extra });
const alu = (finish) => std(FINISHES[finish].body, { metalness: 0.8, roughness: 0.36 });
const DARK = () => std(0x16181b, { roughness: 0.85 });
const BOARD = () => std(0x0f2e24, { roughness: 0.65 });
const CHIP = () => std(0x0c0d10, { roughness: 0.35, metalness: 0.45 });
const COPPER = () => std(0xc07a45, { metalness: 0.95, roughness: 0.28 });
const KEY = () => std(0x101113, { roughness: 0.62, metalness: 0.05 });

function roundedRect(w, d, r) {
  const s = new THREE.Shape();
  const x = -w / 2;
  const y = -d / 2;
  s.moveTo(x + r, y);
  s.lineTo(x + w - r, y);
  s.quadraticCurveTo(x + w, y, x + w, y + r);
  s.lineTo(x + w, y + d - r);
  s.quadraticCurveTo(x + w, y + d, x + w - r, y + d);
  s.lineTo(x + r, y + d);
  s.quadraticCurveTo(x, y + d, x, y + d - r);
  s.lineTo(x, y + r);
  s.quadraticCurveTo(x, y, x + r, y);
  return s;
}

/** A rounded slab w × h × d lying flat (thickness along y), with a soft bevelled edge. */
function slab(w, h, d, r, material, x = 0, y = 0, z = 0, bevel = Math.min(0.012, h / 3)) {
  const geometry = new THREE.ExtrudeGeometry(roundedRect(w - bevel * 2, d - bevel * 2, Math.max(0.002, r - bevel)), {
    depth: Math.max(0.0005, h - bevel * 2),
    bevelEnabled: bevel > 0,
    bevelThickness: bevel,
    bevelSize: bevel,
    bevelSegments: 3,
    curveSegments: 10,
  });
  geometry.rotateX(-Math.PI / 2); // extruded along y; the shape's y becomes −z
  geometry.translate(0, bevel - h / 2, 0);
  const mesh = new THREE.Mesh(geometry, material);
  mesh.position.set(x, y, z);
  return mesh;
}

function box(w, h, d, material, x = 0, y = 0, z = 0) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), material);
  mesh.position.set(x, y, z);
  return mesh;
}

function cylinder(r, h, material, x = 0, y = 0, z = 0, segments = 32) {
  const mesh = new THREE.Mesh(new THREE.CylinderGeometry(r, r, h, segments), material);
  mesh.position.set(x, y, z);
  return mesh;
}

/** A pill-shaped port hole on a side wall (x = the wall), long along z. */
function pill(len, high, material, x, y, z) {
  const geometry = new THREE.CapsuleGeometry(high / 2, Math.max(0.001, len - high), 6, 12);
  geometry.rotateX(Math.PI / 2);
  const mesh = new THREE.Mesh(geometry, material);
  mesh.scale.x = 0.35;
  mesh.position.set(x, y, z);
  return mesh;
}

function canvasTexture(w, h, draw) {
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  draw(canvas.getContext('2d'), w, h);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  return texture;
}

/** Speaker grille: ~1 mm holes in the aluminium (a texture tinted by the body colour — not thousands of meshes). */
const grille = () =>
  canvasTexture(128, 416, (g, w, h) => {
    g.fillStyle = '#ffffff';
    g.fillRect(0, 0, w, h);
    g.fillStyle = '#26282c';
    for (let y = 2; y < h; y += 4) for (let x = 2; x < w; x += 4) g.fillRect(x - 1, y - 1, 2, 2);
  });

/** A soft studio for reflections, made in code (no image is loaded): bright panels around a dark room. */
export function studioEnvironment(renderer) {
  const room = new THREE.Scene();
  room.background = new THREE.Color(0x2c323c); // a dim room: silver reads as silver from every side
  const panel = (w, h, color, x, y, z, ry = 0, rx = 0) => {
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ color, side: THREE.DoubleSide }));
    mesh.position.set(x, y, z);
    mesh.rotation.set(rx, ry, 0);
    room.add(mesh);
  };
  panel(8, 3, 0xffffff, 0, 6, 0, 0, Math.PI / 2); // a soft box above
  panel(4, 6, 0xdbe9f7, -7, 1, 0, Math.PI / 2); // a slightly cool side light
  panel(4, 6, 0xf7ece0, 7, 1, 0, -Math.PI / 2); // a slightly warm one
  panel(14, 9, 0xa3acb8, 0, 2.5, -8); // the far wall (a desk seen from above reflects it)
  panel(12, 4, 0xb4bcc6, 0, 1, 8); // behind the viewer
  panel(16, 16, 0x6b7480, 0, -4, 0, 0, Math.PI / 2); // the floor (a grey desk)
  const pmrem = new THREE.PMREMGenerator(renderer);
  return pmrem.fromScene(room, 0.04).texture;
}

/** Legends of the user's keyboard as in the photos: Latin top left, Russian (Mac layout) bottom right, backlit. */
const LEGENDS = [
  [['esc'], ['F1', '☼'], ['F2', '☀'], ['F3', '▭'], ['F4', '⌕'], ['F5', '●'], ['F6', '☾'], ['F7', '◀◀'], ['F8', '▶‖'],
    ['F9', '▶▶'], ['F10', '◁'], ['F11', '◁)'], ['F12', '◁))']],
  [['~`', '[]'], ['!1'], ['@2', '"'], ['#3', '№'], ['$4', '%'], ['%5', ':'], ['^6', ','], ['&7', '.'], ['*8', ';'],
    ['(9'], [')0'], ['_-'], ['+='], ['⌫', null, 'right']],
  [['⇥', null, 'mod'], 'QЙ', 'WЦ', 'EУ', 'RК', 'TЕ', 'YН', 'UГ', 'IШ', 'OЩ', 'PЗ', ['{[', 'Х'], ['}]', 'Ъ'], ['|\\', 'Ё']],
  [['⇪', null, 'mod'], 'AФ', 'SЫ', 'DВ', 'FА', 'GП', 'HР', 'JО', 'KЛ', 'LД', [':;', 'Ж'], ['"\'', 'Э'], ['↩', null, 'right']],
  [['⇧', null, 'mod'], 'ZЯ', 'XЧ', 'CС', 'VМ', 'BИ', 'NТ', 'MЬ', ['<,', 'Б'], ['>.', 'Ю'], ['?/'], ['⇧', null, 'right']],
  [['fn', '◍'], ['control', '⌃'], ['option', '⌥'], ['command', '⌘'], [''], ['command', '⌘'], ['option', '⌥'], 'arrows'],
]; // prettier-ignore

function legends(rows, kbW) {
  const px = 2048 / kbW; // pixels per unit
  const pitch = KEY_PITCH * px;
  const font = (size, weight = 500) => `${weight} ${Math.round(size)}px -apple-system, "Helvetica Neue", Arial, sans-serif`;
  return canvasTexture(2048, Math.round(KB_ROWS * pitch), (g) => {
    g.fillStyle = '#d6ecff';
    g.textBaseline = 'middle';
    rows.forEach((row, r) => {
      let x = 0;
      const y = r * pitch;
      row.forEach((width, i) => {
        const legend = LEGENDS[r][i];
        if (width === 'arrows') {
          g.font = font(pitch * 0.16);
          g.textAlign = 'center';
          const half = pitch / 2;
          g.fillText('▲', x + 1.5 * pitch, y + half * 0.5);
          for (const [k, s] of [[0.5, '◀'], [1.5, '▼'], [2.5, '▶']]) g.fillText(s, x + k * pitch, y + half * 1.5);
          return;
        }
        const w = width * pitch;
        if (typeof legend === 'string') { // a letter: Latin + Russian
          g.textAlign = 'left';
          g.font = font(pitch * 0.3);
          g.fillText(legend[0], x + pitch * 0.2, y + pitch * 0.36);
          g.textAlign = 'right';
          g.font = font(pitch * 0.24);
          g.fillText(legend[1], x + w - pitch * 0.16, y + pitch * 0.7);
        } else if (r === 0) { // the function row: an icon over the key name
          g.textAlign = 'center';
          g.font = font(pitch * 0.17);
          g.fillText(legend[0], x + w / 2 - (i === 0 ? w * 0.28 : 0), y + pitch * (i === 0 ? 0.68 : 0.74));
          if (legend[1]) {
            g.font = font(pitch * 0.2);
            g.fillText(legend[1], x + w / 2, y + pitch * 0.38);
          }
        } else if (r === 5) { // modifiers: a symbol over the word
          if (!legend[0]) return void (x += w);
          g.textAlign = i < 4 ? 'left' : 'right';
          const at = i < 4 ? x + pitch * 0.14 : x + w - pitch * 0.14;
          g.font = font(pitch * 0.15);
          g.fillText(legend[0], at, y + pitch * 0.74);
          g.font = font(pitch * 0.2);
          g.fillText(legend[1], at, y + pitch * 0.38);
        } else if (legend[2]) { // tab / caps / shift / return / delete: one symbol at the side
          g.textAlign = legend[2] === 'right' ? 'right' : 'left';
          g.font = font(pitch * 0.22);
          g.fillText(legend[0], legend[2] === 'right' ? x + w - pitch * 0.16 : x + pitch * 0.16, y + pitch * 0.7);
        } else { // two symbols on the left (shifted over plain), the Russian one on the right
          g.textAlign = 'left';
          g.font = font(pitch * 0.2);
          g.fillText(legend[0][0], x + pitch * 0.2, y + pitch * 0.3);
          g.fillText(legend[0][1], x + pitch * 0.2, y + pitch * 0.66);
          if (legend[1]) {
            g.textAlign = 'right';
            g.fillText(legend[1], x + w - pitch * 0.18, y + pitch * 0.3);
          }
        }
        x += w;
      });
    });
  });
}

/** The engraving on the bottom: the model name, as on the user's machine (no logo). */
const engraving = () =>
  canvasTexture(512, 96, (g, w, h) => {
    g.fillStyle = '#ffffff';
    g.fillRect(0, 0, w, h);
    g.fillStyle = '#8f9399';
    g.font = '400 60px -apple-system, "Helvetica Neue", Arial, sans-serif';
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillText('MacBook Pro', w / 2, h / 2);
  });

/** The Apple logo as on the user's lid (drawn from their photo; viewBox 0 0 100 120, leaf included). */
const APPLE =
  'M 50 31 C 56 27, 64 24, 72 24 C 81 24, 89 28, 94 35 C 86 41, 82 47, 82 54 C 82 63, 88 70, 97 73 ' +
  'C 94 84, 89 95, 82 104 C 76 112, 70 118, 62 118 C 57 118, 54 115, 50 115 C 46 115, 43 118, 38 118 ' +
  'C 30 118, 24 112, 18 104 C 9 92, 3 78, 3 62 C 3 40, 17 25, 33 25 C 40 25, 45 28, 50 31 Z ' +
  'M 51 23 C 50 12, 59 2, 71 0 C 72 12, 63 22, 51 23 Z';

const appleLogo = () =>
  canvasTexture(256, 304, (g) => {
    g.scale(2.4, 2.4);
    g.translate(3, 3);
    g.fillStyle = '#ffffff';
    g.fill(new Path2D(APPLE));
  });

/** Until the user's screen loads (or without it): soft colour fields. */
function fields(g, w, h) {
  const base = g.createLinearGradient(0, 0, w, h);
  base.addColorStop(0, '#0b1230');
  base.addColorStop(1, '#1d0b2e');
  g.fillStyle = base;
  g.fillRect(0, 0, w, h);
  for (const [x, y, r, c] of [[0.25, 0.7, 0.55, 'rgba(70,120,255,0.55)'], [0.75, 0.35, 0.5, 'rgba(190,80,255,0.45)'], [0.55, 0.85, 0.4, 'rgba(40,220,255,0.35)']]) {
    const glow = g.createRadialGradient(x * w, y * h, 0, x * w, y * h, r * w);
    glow.addColorStop(0, c);
    glow.addColorStop(1, 'rgba(0,0,0,0)');
    g.fillStyle = glow;
    g.fillRect(0, 0, w, h);
  }
}

const DAYS = ['Вс', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'];
const MONTHS = ['янв.', 'февр.', 'мар.', 'апр.', 'мая', 'июн.', 'июл.', 'авг.', 'сент.', 'окт.', 'нояб.', 'дек.'];

/** The menu bar of the user's Mac (37 pt, Russian, a live clock); the notch of the model covers its middle. */
function menuBar(g, w) {
  const h = 44;
  const ui = (weight) => `${weight} 22px -apple-system, "Helvetica Neue", Arial, sans-serif`;
  g.fillStyle = 'rgba(0,0,0,0.25)';
  g.fillRect(0, 0, w, h);
  g.fillStyle = '#ffffff';
  g.textBaseline = 'middle';
  g.save();
  g.translate(30, 8);
  g.scale(0.22, 0.22);
  g.fill(new Path2D(APPLE));
  g.restore();
  let x = 78;
  for (const [i, item] of ['Finder', 'Файл', 'Правка', 'Вид', 'Переход', 'Окно', 'Справка'].entries()) {
    g.font = ui(i === 0 ? 700 : 500);
    g.textAlign = 'left';
    g.fillText(item, x, h / 2 + 1);
    x += g.measureText(item).width + 34;
  }
  const now = new Date();
  const clock = `${DAYS[now.getDay()]} ${now.getDate()} ${MONTHS[now.getMonth()]} ${now.getHours()}:${String(now.getMinutes()).padStart(2, '0')}`;
  g.font = ui(500);
  g.textAlign = 'right';
  g.fillText(clock, w - 28, h / 2 + 1);
  const right = w - 28 - g.measureText(clock).width - 34;
  g.strokeStyle = '#ffffff';
  g.lineWidth = 2;
  g.strokeRect(right - 40, 14, 34, 16); // the battery
  g.fillRect(right - 37, 17, 24, 10);
  g.fillRect(right - 5, 19, 3, 6);
  for (const r of [6, 12, 18]) { // Wi-Fi
    g.beginPath();
    g.arc(right - 80, 33, r, (-3 * Math.PI) / 4, -Math.PI / 4);
    g.stroke();
  }
}

/** The screen: the user's main screen — wallpaper + Dock from /api/lab/screen (scripts/lab_screen.sh, only with a
 *  session) — under a live menu bar; soft colour fields until it loads or when it is not made. One shared texture. */
let SCREEN = null;
function screenTexture() {
  if (SCREEN) return SCREEN;
  const canvas = document.createElement('canvas');
  canvas.width = 2048;
  canvas.height = 1324; // 3456 × 2234
  const g = canvas.getContext('2d');
  SCREEN = new THREE.CanvasTexture(canvas);
  SCREEN.colorSpace = THREE.SRGBColorSpace;
  SCREEN.anisotropy = 4;
  let shot = null;
  const draw = () => {
    if (shot) g.drawImage(shot, 0, 0, canvas.width, canvas.height);
    else fields(g, canvas.width, canvas.height);
    menuBar(g, canvas.width);
    SCREEN.needsUpdate = true;
  };
  draw();
  const img = new Image();
  img.onload = () => {
    shot = img;
    draw();
  };
  img.src = '/api/lab/screen';
  setInterval(draw, 30000); // the clock
  return SCREEN;
}

/** The SoC lid with its marking (a schematic label, not a copy of Apple's print). */
const socLabel = (text) =>
  canvasTexture(256, 256, (g, w, h) => {
    g.fillStyle = '#b9bdc2';
    g.fillRect(0, 0, w, h);
    g.fillStyle = '#4a4e54';
    g.font = '600 46px -apple-system, Helvetica, Arial, sans-serif';
    g.textAlign = 'center';
    g.fillText(text, w / 2, h / 2 + 16);
  });

/** The keyboard's steel plate seen from inside the top case: a grid of rivets (the teardown short). */
const rivets = () =>
  canvasTexture(512, 192, (g, w, h) => {
    g.fillStyle = '#4a4d52';
    g.fillRect(0, 0, w, h);
    g.fillStyle = '#9ca1a8';
    for (let y = 8; y < h; y += 12) for (let x = 8 + ((y / 12) % 2) * 6; x < w; x += 12) g.fillRect(x - 1.5, y - 1.5, 3, 3);
  });

/** The print on the middle cells: rows of small grey lines and a few symbols, like the real label (no logo, no text). */
const cellLabel = () =>
  canvasTexture(256, 320, (g, w, h) => {
    g.fillStyle = '#222428';
    g.fillRect(0, 0, w, h);
    g.fillStyle = 'rgba(160, 166, 175, 0.55)';
    for (let row = 0; row < 14; row += 1) {
      const len = 120 + ((row * 53) % 60);
      g.fillRect((w - len) / 2, 70 + row * 9, len, 3);
    }
    g.strokeStyle = 'rgba(160, 166, 175, 0.55)';
    g.lineWidth = 2;
    for (const [x, kind] of [[78, 'o'], [118, 'r'], [158, 'o'], [198, 'r']]) {
      g.beginPath();
      if (kind === 'o') g.arc(x, 230, 9, 0, Math.PI * 2);
      else g.rect(x - 8, 221, 16, 18);
      g.stroke();
    }
  });

function part(id, explode, build) {
  const group = new THREE.Group();
  group.name = id;
  const [title, about] = PARTS_INFO[id];
  group.userData = { id, title, about, explode };
  build(group);
  return group;
}

/** The keyboard rows of a US layout, in key widths (1 = a letter key); full-height function row since 2021. */
const ROWS = [
  [1.5, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], // esc, F1–F12 (Touch ID is drawn apart at the end)
  [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1.5],
  [1.5, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
  [1.75, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1.75],
  [2.25, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2.25],
  [1, 1, 1, 1.25, 5, 1.25, 1, 'arrows'],
];

export function buildMacBookA({ finish = 'silver', chip = 'M5 Pro' } = {}) { // the user's MacBook
  const S = SIZES[16];
  const { W, D, BASE, R } = S;
  const back = -D / 2; // the hinge edge
  const kbW = 14.5 * KEY_PITCH;
  const kbTop = back + S.kbTop;
  const kbBottom = kbTop + KB_ROWS * KEY_PITCH;
  const kbMid = (kbTop + kbBottom) / 2;
  const [tpW, tpD] = S.trackpad;
  const tpZ = kbBottom + S.gap + tpD / 2;
  const WELL = 0.025; // the black frame around the keys
  const grilleX = kbW / 2 + WELL + 0.025 + S.grille / 2; // 2.5 mm beside the keyboard well
  const root = new THREE.Group();
  root.name = 'macbook';
  root.userData.size = S;
  const body = () => alu(finish);

  // --- the inside, from teardown frames of the 16″ (17.10, the user's screenshots) ----------------------------------
  // IN: the inside seen from below with the bottom off (frame 2:01: the inside spans x 300→1715 px, y 170→1160 px,
  // ≈ 3.98 px a mm). Seen from below, left and right are swapped — mirrored back here. CV: the bottom cover's inner face
  // (frame 2:05: x 215→1835 px, y 95→1185 px). Heights: the floor (the cover's inner face) at 0.02, the top case's
  // keyboard plate at 0.092; the fans, the heatsink and the cells under the logic board, the board above them.
  const IN = (px, py) => [-((px - 1007.5) / 398), back + (py - 170) / 398];
  const CV = (px, py) => [(px - 1025) / 455, back + (py - 95) / 440];
  // AP: Apple's render of the 16″ inside from below (the user's picture, 17.10; the inside spans x 65→740 and
  // y 170→635 of 800 px) — the logic board's underside as on the M5 Pro: the processor's silver plate open, the
  // heatsink on the other side of the board (iFixit: it comes off only after the board is out and turned over).
  const AP = (px, py) => [-((px - 402.5) / 189.8), back + (py - 170) / 187.4];
  const area = (map, x1, y1, x2, y2) => {
    const [a, za] = map(x1, y1);
    const [b, zb] = map(x2, y2);
    return { x: (a + b) / 2, z: (za + zb) / 2, w: Math.abs(b - a), d: Math.abs(zb - za) };
  };
  const FLOOR = 0.02;
  const BOARD_Y = 0.074; // the board's underside: the fans, the heatsink and the cells stay below it
  const METAL = () => std(0xb7bbc1, { metalness: 0.9, roughness: 0.3 });
  const SHIELD = () => std(0x5c5651, { metalness: 0.75, roughness: 0.4 });
  const MATTE = () => std(0x1d1f22, { roughness: 0.8 });
  const BRASS = () => std(0xc9a25a, { metalness: 0.9, roughness: 0.35 });

  root.add(
    part('bottom', [0, -1.25, 0.4], (g) => {
      g.add(slab(W, 0.02, D, R, body(), 0, 0.01, 0));
      const [inset, footR] = S.feet;
      const rubber = std(0x0b0c0d, { roughness: 0.55 });
      const rim = std(0x9a9ea4, { metalness: 0.8, roughness: 0.3 });
      const screw = std(0x55595f, { metalness: 0.9, roughness: 0.35 });
      for (const sx of [-1, 1]) {
        for (const sz of [-1, 1]) {
          g.add(cylinder(footR, 0.008, rim, sx * (W / 2 - inset), -0.002, sz * (D / 2 - inset), 36));
          g.add(cylinder(footR - 0.012, 0.012, rubber, sx * (W / 2 - inset), -0.004, sz * (D / 2 - inset), 36)); // feet
          g.add(cylinder(footR - 0.01, 0.0015, rubber, sx * (W / 2 - inset), FLOOR + 0.0008, sz * (D / 2 - inset), 36)); // their mounts inside
          for (const x of [W / 2 - 0.085, S.screwsX]) g.add(cylinder(0.011, 0.002, screw, sx * x, -0.0005, sz * (D / 2 - 0.085), 12)); // screws
        }
        const [from, to] = S.intake;
        g.add(box(0.008, 0.004, to - from, DARK(), sx * (W / 2 - 0.03), -0.001, back + (from + to) / 2)); // intake slits
      }
      const name = new THREE.Mesh(new THREE.PlaneGeometry(0.62, 0.116), std(FINISHES[finish].body, { map: engraving(), metalness: 0.6, roughness: 0.45 }));
      name.geometry.rotateX(Math.PI / 2); // facing down…
      name.geometry.rotateY(Math.PI); // …and read with the laptop turned over sideways, as in the user's photo
      name.position.set(0, -0.0013, 0);
      g.add(name);
      // the inner face (frame 2:05): the insulation sheet under the processor, its corners cut at the front
      const ins = area(CV, 750, 185, 1280, 765);
      const cut = 0.11;
      const sheet = new THREE.Shape();
      sheet.moveTo(-ins.w / 2, ins.d / 2);
      sheet.lineTo(ins.w / 2, ins.d / 2);
      sheet.lineTo(ins.w / 2, -ins.d / 2 + cut);
      sheet.lineTo(ins.w / 2 - cut, -ins.d / 2);
      sheet.lineTo(-ins.w / 2 + cut, -ins.d / 2);
      sheet.lineTo(-ins.w / 2, -ins.d / 2 + cut);
      sheet.closePath();
      const sheetGeo = new THREE.ExtrudeGeometry(sheet, { depth: 0.0012, bevelEnabled: false });
      sheetGeo.rotateX(-Math.PI / 2); // the shape's y becomes −z: its top edge lies at the hinge side
      const insulation = new THREE.Mesh(sheetGeo, std(0x3a3c40, { roughness: 0.9 }));
      insulation.position.set(ins.x, FLOOR, ins.z);
      g.add(insulation);
      for (const [px, py] of [[895, 350], [1135, 345], [1020, 640]]) {
        const [x, z] = CV(px, py);
        g.add(cylinder(0.012, 0.0016, std(0xdfe3e8, { roughness: 0.6 }), x, FLOOR + 0.0008, z, 14)); // holes in the sheet
      }
      // the rear gasket and the three shield plates with their spring contacts at the hinge
      const gasket = area(CV, 370, 140, 1665, 205);
      g.add(box(gasket.w, 0.004, gasket.d, MATTE(), gasket.x, FLOOR + 0.002, gasket.z));
      for (let i = 0; i < 40; i += 1) g.add(box(0.006, 0.0045, gasket.d * 0.8, std(0x2a2c30), gasket.x - gasket.w / 2 + 0.05 + i * ((gasket.w - 0.1) / 39), FLOOR + 0.0025, gasket.z));
      for (const [rect, mat] of [[[415, 200, 700, 295], SHIELD], [[930, 190, 1095, 285], METAL], [[1330, 175, 1625, 265], SHIELD]]) {
        const s = area(CV, ...rect);
        g.add(slab(s.w, 0.002, s.d, 0.012, mat(), s.x, FLOOR + 0.001, s.z, 0));
        for (const k of [-0.32, -0.2, 0.2, 0.32]) {
          if (s.w < 0.5 && Math.abs(k) < 0.25) continue; // the small middle plate has one pair of contacts a side
          const clip = slab(0.03, 0.008, 0.045, 0.008, METAL(), s.x + k * s.w * (s.w < 0.5 ? 1.2 : 1), FLOOR + 0.005, s.z - s.d / 2 + 0.03, 0);
          g.add(clip);
        }
      }
      // foam along the sides, rubber stops and pads, the side clips
      for (const rect of [[220, 270, 262, 540], [240, 590, 290, 1120], [1800, 225, 1835, 505], [1795, 555, 1830, 1100]]) {
        const s = area(CV, ...rect);
        g.add(box(s.w, 0.004, s.d, MATTE(), s.x, FLOOR + 0.002, s.z));
      }
      for (const rect of [[340, 780, 665, 800], [1390, 760, 1730, 780], [718, 762, 772, 782], [1278, 752, 1333, 772], [570, 665, 596, 725], [1457, 650, 1483, 710]]) {
        const s = area(CV, ...rect);
        g.add(slab(s.w, 0.004, s.d, Math.min(s.w, s.d) / 2, std(0x26282c, { roughness: 0.7 }), s.x, FLOOR + 0.002, s.z, 0));
      }
      for (const [px, py] of [[280, 790], [1790, 765]]) {
        const [x, z] = CV(px, py);
        g.add(box(0.025, 0.012, 0.09, METAL(), x, FLOOR + 0.006, z));
      }
    }),
  );

  // the battery: six cells (two a side, two big ones in the middle), a steel frame, the trackpad's channel between
  root.add(
    part('battery', [0, -0.45, 0.85], (g) => {
      const cell = std(0x222428, { roughness: 0.42, metalness: 0.1 });
      const printed = std(0x222428, { roughness: 0.45, map: cellLabel() });
      const CELLS = [[395, 760, 675, 935], [445, 945, 680, 1135], [735, 755, 975, 1045], [1060, 755, 1300, 1045], [1355, 755, 1625, 925], [1360, 935, 1580, 1125]];
      CELLS.forEach((rect, i) => {
        const c = area(IN, ...rect);
        g.add(slab(c.w - 0.02, 0.05, c.d - 0.02, 0.035, i === 2 || i === 3 ? printed : cell, c.x, FLOOR + 0.03, c.z));
      });
      const rail = METAL();
      const top = IN(330, 745)[1];
      g.add(box(3.4, 0.022, 0.03, rail, 0, FLOOR + 0.015, top)); // the frame's rail along the board
      for (const px of [715, 985, 1055, 1315]) {
        const [x] = IN(px, 0);
        const z1 = top;
        const z2 = IN(0, px === 985 || px === 1055 ? 1080 : 1135)[1];
        g.add(box(0.035, 0.022, z2 - z1, rail, x, FLOOR + 0.015, (z1 + z2) / 2));
        for (let k = 0; k < 4; k += 1) g.add(cylinder(0.009, 0.024, std(0x3a3d42), x, FLOOR + 0.016, z1 + 0.08 + k * ((z2 - z1 - 0.16) / 3), 10)); // screw holes
      }
      for (const py of [470, 520, 560]) {
        const [x, z] = AP(400, py);
        g.add(box(0.035, 0.002, 0.03, std(0xd28654, { metalness: 0.9, roughness: 0.3 }), x, FLOOR + 0.0565, z)); // the battery board's data cable contacts
      }
      const plug = area(IN, 985, 640, 1065, 745);
      g.add(box(plug.w, 0.014, plug.d, std(0x2a2d33), plug.x, FLOOR + 0.045, plug.z)); // its plug at the board
    }),
  );

  // the logic board: the whole width, cut out round the two fans; the processor on its underside, over the heatsink
  const fanX = 1.09;
  const fanZ = back + 0.83;
  const FAN_R = 0.44;
  root.add(
    part('board', [0, 0.8, -0.15], (g) => {
      const z0 = back + 0.15;
      const z1 = back + 1.42;
      const pcb = new THREE.Shape();
      pcb.moveTo(-1.74, -z0);
      pcb.lineTo(1.74, -z0);
      pcb.lineTo(1.74, -z1);
      pcb.lineTo(-1.74, -z1);
      pcb.closePath();
      for (const side of [-1, 1]) {
        const hole = new THREE.Path();
        const xa = side * fanX - 0.47;
        const xb = side * fanX + 0.47;
        const za = -(back + 0.19);
        const zb = -(fanZ + 0.45);
        hole.moveTo(xa, za);
        hole.lineTo(xb, za);
        hole.lineTo(xb, zb + 0.3);
        hole.quadraticCurveTo(xb, zb, xb - 0.3, zb);
        hole.lineTo(xa + 0.3, zb);
        hole.quadraticCurveTo(xa, zb, xa, zb + 0.3);
        hole.closePath();
        pcb.holes.push(hole);
      }
      const geometry = new THREE.ExtrudeGeometry(pcb, { depth: 0.011, bevelEnabled: false, curveSegments: 16 });
      geometry.rotateX(-Math.PI / 2); // drawn with −z, it lands at z0…z1
      const board = new THREE.Mesh(geometry, std(0x1b1815, { roughness: 0.62, metalness: 0.15 }));
      board.position.y = BOARD_Y;
      g.add(board);
      const topY = BOARD_Y + 0.011;
      // the processor: its package on top (under the heatsink), its silver plate open underneath (Apple's render)
      const soc = AP(400, 299);
      g.add(slab(0.4, 0.006, 0.4, 0.02, CHIP(), soc[0], topY + 0.003, soc[1]));
      const plate = area(AP, 368, 268, 432, 330);
      g.add(slab(plate.w, 0.003, plate.d, 0.015, std(0xc4c8cd, { metalness: 0.95, roughness: 0.25 }), plate.x, BOARD_Y - 0.0015, plate.z, 0));
      const lid = new THREE.Mesh(new THREE.PlaneGeometry(plate.w * 0.8, plate.d * 0.8), std(0xc4c8cd, { metalness: 0.9, roughness: 0.3, map: socLabel(chip) }));
      lid.geometry.rotateX(Math.PI / 2); // facing down…
      lid.geometry.rotateY(Math.PI); // …and read from below with the hinge at the top, as in the render
      lid.position.set(plate.x, BOARD_Y - 0.0042, plate.z); // 0.12 mm under the plate
      g.add(lid);
      // the shields around it: two «wings» with a silver rim, the connector shield above, a small one below-left
      const shield = (rect, cut = false) => {
        const r = area(AP, ...rect);
        const c = cut ? 0.12 : 0;
        const inner = r.x > 0 ? -1 : 1; // the side towards the processor (shape x = model x)
        // the shape's +y is the hinge side; the corner by the processor, at the top, is cut off (the render)
        const pts = [[-r.w / 2, r.d / 2], [r.w / 2, r.d / 2], [r.w / 2, -r.d / 2], [-r.w / 2, -r.d / 2]];
        const sh = new THREE.Shape();
        const corner = inner > 0 ? 1 : 0; // top-right or top-left
        pts.forEach(([x, y], i) => {
          if (cut && i === corner) {
            const sx = Math.sign(x);
            const a = [x - sx * c, y];
            const b = [x, y - c];
            if (i === 0) sh.moveTo(...b), sh.lineTo(...a);
            else sh.lineTo(...a), sh.lineTo(...b);
          } else if (i === 0) sh.moveTo(x, y);
          else sh.lineTo(x, y);
        });
        sh.closePath();
        const geo = new THREE.ExtrudeGeometry(sh, { depth: 0.0035, bevelEnabled: false });
        geo.rotateX(-Math.PI / 2); // the shape's top edge lies at the hinge side
        const rim = new THREE.Mesh(geo, std(0xb9bdc2, { metalness: 0.9, roughness: 0.3 }));
        rim.position.set(r.x, BOARD_Y - 0.0035, r.z);
        g.add(rim);
        const face = new THREE.Mesh(geo, std(0x111214, { roughness: 0.55 }));
        face.scale.set(0.9, 1.15, 0.92);
        face.position.set(r.x, BOARD_Y - 0.0049, r.z); // 0.14 mm below the rim — never the same depth
        g.add(face);
      };
      shield([292, 248, 352, 425], true); // the top corner by the processor cut off
      shield([452, 248, 512, 425], true);
      shield([370, 196, 432, 234]);
      shield([283, 388, 330, 425]);
      for (const [px, py] of [[312, 243], [490, 243], [338, 285], [462, 285], [283, 368], [518, 368], [116, 212], [688, 212]]) {
        const [x, z] = AP(px, py);
        g.add(cylinder(0.034, 0.002, std(0xe8b923, { metalness: 0.8, roughness: 0.35 }), x, BOARD_Y - 0.0012, z, 24)); // gold pads for the screws
      }
      for (const px of [430, 445, 460]) {
        const [x, z] = AP(px, 396);
        g.add(box(0.05, 0.008, 0.05, std(0x6a6e75, { metalness: 0.6, roughness: 0.4 }), x, BOARD_Y - 0.004, z)); // inductors
      }
      for (const rect of [[90, 240, 110, 262], [78, 300, 100, 320], [78, 350, 100, 372], [700, 255, 722, 280], [705, 288, 735, 330]]) {
        const r = area(AP, ...rect);
        g.add(slab(r.w, 0.004, r.d, 0.008, METAL(), r.x, BOARD_Y - 0.002, r.z, 0)); // covers over the side cables (iFixit: 3 left, 5 right)
      }
      // on top: the SSD chips at both ends, port controllers under shields, power chips, the display connectors
      for (const [px, py, w, d] of [[365, 600, 0.14, 0.13], [365, 690, 0.14, 0.13], [1650, 600, 0.14, 0.13], [1650, 690, 0.14, 0.13]]) {
        const [x, z] = IN(px, py);
        g.add(box(w, 0.008, d, CHIP(), x, topY + 0.004, z)); // SSD NAND
      }
      for (const [px, py] of [[1660, 330], [1660, 450], [355, 330], [355, 450]]) {
        const [x, z] = IN(px, py);
        g.add(box(0.16, 0.009, 0.1, METAL(), x, topY + 0.0045, z)); // shields of the port controllers
      }
      for (const [px, py] of [[880, 665], [930, 665], [880, 705], [930, 705], [1220, 670]]) {
        const [x, z] = IN(px, py);
        g.add(box(px === 1220 ? 0.3 : 0.1, 0.007, px === 1220 ? 0.24 : 0.08, CHIP(), x, topY + 0.0035, z)); // power chips
      }
      for (const [px, py] of [[860, 230], [1180, 230]]) {
        const [x, z] = IN(px, py);
        g.add(slab(0.34, 0.01, 0.15, 0.02, METAL(), x, topY + 0.005, z, 0)); // the display / camera connectors under brackets
      }
      for (let i = 0; i < 90; i += 1) {
        const x = -1.6 + ((i * 37) % 320) / 100;
        const z = back + 1.07 + ((i * 53) % 33) / 100;
        if (Math.abs(x) < 0.12) continue; // the battery plug
        g.add(box(0.02 + (i % 3) * 0.008, 0.004, 0.012, std(i % 4 ? 0x8a6a3a : 0x2c2e33, { metalness: 0.5 }), x, topY + 0.002, z)); // passives
      }
      const under = BOARD_Y - 0.003;
      for (const [px, py] of [[350, 575], [350, 625], [385, 690], [440, 690], [1645, 575], [1645, 635], [1590, 690], [1640, 690]]) {
        const [x, z] = IN(px, py);
        g.add(box(0.1, 0.006, 0.1, CHIP(), x, under, z)); // chips on the underside at both ends
      }
      for (let i = 0; i < 120; i += 1) {
        const band = i % 3;
        const x = band === 0 ? -1.62 + ((i * 29) % 30) / 100 : band === 1 ? 1.32 + ((i * 31) % 30) / 100 : -1.4 + ((i * 37) % 280) / 100;
        const z = band === 2 ? back + 1.1 + ((i * 41) % 28) / 100 : back + 0.25 + ((i * 43) % 110) / 100;
        if (band === 2 && Math.abs(x) < 0.5) continue; // under the heatsink
        g.add(box(0.018 + (i % 4) * 0.006, 0.003, 0.012, std(i % 5 ? 0x7a6038 : 0x2c2e33, { metalness: 0.5 }), x, under + 0.0015, z));
      }
      for (const [px, py] of [[795, 355], [1235, 355], [775, 585], [1258, 585], [450, 385], [1570, 385]]) {
        const [x, z] = IN(px, py);
        g.add(cylinder(0.018, 0.004, BRASS(), x, topY + 0.002, z, 16)); // screw bosses
      }
    }),
  );

  // the heatsink: on the board's top side over the processor (iFixit: four screws hold it to the board, a little paste
  // sticks it; it comes off only after the board is out), heat pipes to the fin stacks behind the fans
  root.add(
    part('heatsink', [0, 1.2, -0.35], (g) => {
      const top = BOARD_Y + 0.011 + 0.006; // over the processor package
      const soc = AP(400, 299);
      g.add(slab(0.56, 0.004, 0.5, 0.03, COPPER(), soc[0], top + 0.002, soc[1])); // the cold plate
      for (const side of [-1, 1]) {
        g.add(box(0.12, 0.003, 0.03, METAL(), soc[0] + side * 0.33, top + 0.002, soc[1] - 0.2)); // its brackets
        g.add(box(0.12, 0.003, 0.03, METAL(), soc[0] + side * 0.33, top + 0.002, soc[1] + 0.2));
        for (const shift of [-0.035, 0.035]) {
          const curve = new THREE.CatmullRomCurve3([
            new THREE.Vector3(soc[0] + side * 0.22, top + 0.004, soc[1] + shift),
            new THREE.Vector3(side * 0.45, top + 0.004, back + 0.45 + shift),
            new THREE.Vector3(side * 0.7, top + 0.002, back + 0.3 + shift),
            new THREE.Vector3(side * 0.95, FLOOR + 0.055, back + 0.29 + shift),
          ]);
          const tube = new THREE.Mesh(new THREE.TubeGeometry(curve, 48, 0.018, 10, false), COPPER());
          g.add(tube); // heat pipes
        }
      }
      for (const rect of [[440, 225, 775, 345], [1260, 225, 1590, 345]]) {
        const f = area(IN, ...rect);
        g.add(box(f.w, 0.05, f.d, std(0x1a1c1f, { roughness: 0.7 }), f.x, FLOOR + 0.027, f.z)); // the fin stack's cover
        for (let i = 0; i < 34; i += 1) {
          g.add(box(0.006, 0.046, 0.04, std(0x4a4e55, { metalness: 0.8, roughness: 0.35 }), f.x - f.w / 2 + 0.03 + i * ((f.w - 0.06) / 33), FLOOR + 0.027, f.z - f.d / 2 - 0.012)); // its fins at the exhaust
        }
      }
    }),
  );

  // the fans: centrifugal blowers in black housings, a silver hub
  root.add(
    part('fans', [0, -0.25, -0.75], (g) => {
      for (const side of [-1, 1]) {
        const fan = new THREE.Group();
        fan.position.set(side * fanX, FLOOR, fanZ);
        const housing = std(0x18191c, { roughness: 0.75, side: THREE.DoubleSide });
        const wall = new THREE.Mesh(new THREE.CylinderGeometry(FAN_R, FAN_R, 0.05, 64, 1, true, Math.PI * 0.08, Math.PI * 1.84), housing);
        wall.position.y = 0.027;
        wall.rotation.y = Math.PI; // the open side towards the fin stack at the rear
        fan.add(wall);
        const cover = new THREE.Shape();
        cover.absarc(0, 0, FAN_R, 0, Math.PI * 2, false);
        const intake = new THREE.Path();
        intake.absarc(0, 0, 0.31, 0, Math.PI * 2, true);
        cover.holes.push(intake);
        const coverGeo = new THREE.ExtrudeGeometry(cover, { depth: 0.003, bevelEnabled: false, curveSegments: 48 });
        coverGeo.rotateX(-Math.PI / 2);
        for (const y of [0.002, 0.05]) {
          const lid = new THREE.Mesh(coverGeo, housing);
          lid.position.y = y;
          fan.add(lid); // the bottom and the top, each with its intake ring (the teardown shows the bottom one)
        }
        const rotor = new THREE.Group();
        rotor.name = 'rotor';
        rotor.position.y = 0.026;
        const bladeShape = new THREE.Shape();
        bladeShape.moveTo(0, 0);
        bladeShape.quadraticCurveTo(0.07, 0.012, 0.13, 0.045);
        bladeShape.lineTo(0.128, 0.052);
        bladeShape.quadraticCurveTo(0.065, 0.02, 0, 0.006);
        const bladeGeo = new THREE.ExtrudeGeometry(bladeShape, { depth: 0.04, bevelEnabled: false });
        bladeGeo.rotateX(Math.PI / 2);
        const bladeMat = std(0x2b2e33, { roughness: 0.55 });
        for (let i = 0; i < 48; i += 1) {
          const blade = new THREE.Mesh(bladeGeo, bladeMat);
          blade.position.set(0.165, 0.02, 0);
          const holder = new THREE.Group();
          holder.rotation.y = (i / 48) * Math.PI * 2;
          holder.add(blade);
          rotor.add(holder);
        }
        rotor.add(cylinder(0.165, 0.03, std(0x24262a, { roughness: 0.6 }), 0, 0, 0, 48)); // the hub
        rotor.add(cylinder(0.125, 0.034, std(0xc7cbd1, { metalness: 1, roughness: 0.22 }), 0, 0, 0, 48)); // its silver caps, top and bottom
        fan.add(rotor);
        g.add(fan);
      }
    }),
  );

  // the speakers: a tweeter and a woofer box at each front corner, beside the cells
  root.add(
    part('speakers', [0, 0.1, 0.2], (g) => {
      for (const [tall, woofer] of [[[335, 755, 390, 945], [335, 950, 430, 1130]], [[1630, 755, 1690, 930], [1600, 945, 1690, 1120]]]) {
        const t = area(IN, ...tall);
        g.add(slab(t.w, 0.045, t.d, 0.02, MATTE(), t.x, FLOOR + 0.025, t.z)); // tweeter
        const w = area(IN, ...woofer);
        g.add(slab(w.w, 0.05, w.d, 0.03, MATTE(), w.x, FLOOR + 0.027, w.z)); // the woofer box
        const ring = new THREE.Mesh(new THREE.TorusGeometry(1, 0.08, 8, 40), std(0x7d3426, { roughness: 0.5 }));
        ring.rotation.x = Math.PI / 2;
        ring.scale.set(w.w * 0.36, w.d * 0.4, 0.05);
        ring.position.set(w.x, FLOOR + 0.053, w.z);
        g.add(ring);
        const cone = new THREE.Mesh(new THREE.CircleGeometry(1, 40), std(0x101113, { roughness: 0.9 }));
        cone.rotation.x = -Math.PI / 2;
        cone.scale.set(w.w * 0.33, w.d * 0.37, 1);
        cone.position.set(w.x, FLOOR + 0.0525, w.z);
        g.add(cone);
      }
    }),
  );
  root.getObjectByName('speakers').userData.explodeSides = 0.45;

  root.add(
    part('topcase', [0, 1.75, -0.1], (g) => {
      const shell = () => std(FINISHES[finish].body, { metalness: 0.8, roughness: 0.34, transparent: true });
      g.add(slab(W, 0.02, D, R, shell(), 0, BASE - 0.01, 0));
      const ring = roundedRect(W, D, R);
      ring.holes.push(roundedRect(W - 0.03, D - 0.03, R - 0.015));
      const walls = new THREE.ExtrudeGeometry(ring, { depth: BASE - 0.04, bevelEnabled: false, curveSegments: 10 });
      walls.rotateX(-Math.PI / 2);
      const sides = new THREE.Mesh(walls, shell());
      sides.position.y = 0.02; // the sides with the ports, closed all round
      g.add(sides);
      const well = std(0x08090a, { roughness: 0.9, transparent: true });
      g.add(slab(kbW + WELL * 2, 0.002, KB_ROWS * KEY_PITCH + WELL * 2, 0.035, well, 0, BASE + 0.001, kbMid, 0)); // keyboard well
      const holes = grille();
      for (const side of [-1, 1]) {
        g.add(box(S.grille, 0.002, KB_ROWS * KEY_PITCH - 0.01, std(FINISHES[finish].body, { map: holes, metalness: 0.7, roughness: 0.5, transparent: true }), side * grilleX, BASE + 0.001, kbMid)); // grilles
      }
      g.add(slab(0.5, 0.001, 0.05, 0.024, std(FINISHES[finish].edge, { metalness: 0.9, roughness: 0.22, transparent: true }), 0, BASE + 0.0006, D / 2 - 0.022, 0)); // the thumb scoop at the front
      g.add(box(W - 0.62, 0.022, 0.006, std(0x16181b, { roughness: 0.85, transparent: true }), 0, BASE - 0.03, back - 0.001)); // exhaust slit between the hinge ends
      g.add(box(kbW + 0.1, 0.004, KB_ROWS * KEY_PITCH + 0.1, std(0xffffff, { metalness: 0.6, roughness: 0.45, map: rivets(), transparent: true }), 0, BASE - 0.022, kbMid)); // the keyboard plate on rivets, seen from inside
    }),
  );
  root.getObjectByName('topcase').userData.restOpacity = 1; // solid when assembled (the battery must not show through)

  root.add(
    part('keyboard', [0, 2.55, -0.45], (g) => {
      const gap = 0.024; // 16.1 mm keycaps on an 18.5 mm pitch
      const material = KEY();
      const geometries = new Map();
      const keyGeo = (w, h) => {
        const k = `${w.toFixed(4)}x${h.toFixed(4)}`;
        if (!geometries.has(k)) {
          const geometry = new THREE.ExtrudeGeometry(roundedRect(w, h, 0.014), { depth: 0.006, bevelEnabled: true, bevelThickness: 0.003, bevelSize: 0.003, bevelSegments: 2, curveSegments: 4 });
          geometry.rotateX(-Math.PI / 2);
          geometries.set(k, geometry);
        }
        return geometries.get(k);
      };
      const left = -kbW / 2;
      const y = BASE + 0.002;
      ROWS.forEach((row, r) => {
        const z = kbTop + r * KEY_PITCH;
        let x = left;
        for (const w of row) {
          if (w === 'arrows') {
            const half = (KEY_PITCH - gap) / 2;
            for (const [dx, dz] of [[1, 0], [0, 0.5], [1, 0.5], [2, 0.5]]) {
              const key = new THREE.Mesh(keyGeo(KEY_PITCH - gap, half - 0.004), material);
              key.position.set(x + dx * KEY_PITCH + KEY_PITCH / 2, y, z + dz * KEY_PITCH + half / 2 + 0.006);
              g.add(key);
            }
            continue;
          }
          const width = w * KEY_PITCH;
          const key = new THREE.Mesh(keyGeo(width - gap, KEY_PITCH - gap), material);
          key.position.set(x + width / 2, y, z + KEY_PITCH / 2);
          g.add(key);
          x += width;
        }
      });
      const id = new THREE.Mesh(keyGeo(KEY_PITCH - gap, KEY_PITCH - gap), std(0x1d2024, { metalness: 0.6, roughness: 0.25 }));
      id.position.set(left + 13.5 * KEY_PITCH + KEY_PITCH / 2, y, kbTop + KEY_PITCH / 2); // Touch ID
      g.add(id);
      const ring = new THREE.Mesh(new THREE.RingGeometry(0.052, 0.057, 40), std(0x5d6168, { metalness: 0.9, roughness: 0.3 }));
      ring.geometry.rotateX(-Math.PI / 2);
      ring.position.set(id.position.x, y + 0.0103, id.position.z); // 0.13 mm over the key top (y + 0.009)
      g.add(ring); // its steel ring
      const print = new THREE.Mesh(
        new THREE.PlaneGeometry(kbW, KB_ROWS * KEY_PITCH),
        new THREE.MeshBasicMaterial({ map: legends(ROWS, kbW), transparent: true, depthWrite: false }),
      );
      print.geometry.rotateX(-Math.PI / 2); // the canvas top lies at the hinge side
      print.position.set(0, y + 0.0103, kbMid);
      g.add(print); // the backlit legends, one texture for all 78 keys
    }),
  );

  root.add(
    part('trackpad', [0, 2.15, 1.0], (g) => {
      g.add(slab(tpW, 0.012, tpD, 0.045, std(finish === 'black' ? 0x24262a : 0xbfc2c6, { metalness: 0.4, roughness: 0.22 }), 0, BASE, tpZ));
      // its underside (the user's picture): a steel frame, the Taptic Engine along the rear, three small boards on a dark
      // panel, two force sensors, the bracket with the copper contacts, eight screws
      const tpRel = (rx, rz) => [rx * tpW, tpZ + rz * tpD]; // −0.5…0.5 of the trackpad
      const under = BASE - 0.006;
      g.add(slab(tpW - 0.04, 0.003, tpD - 0.04, 0.035, std(0x8d9197, { metalness: 0.75, roughness: 0.4 }), 0, under - 0.0015, tpZ, 0)); // the frame
      g.add(slab(tpW * 0.74, 0.008, tpD * 0.16, 0.012, std(0x1c1d20, { roughness: 0.6 }), 0, under - 0.007, tpZ - tpD * 0.36, 0)); // Taptic Engine
      g.add(box(tpW * 0.86, 0.002, tpD * 0.4, std(0x2a2c30, { roughness: 0.7 }), 0, under - 0.004, tpZ - tpD * 0.01)); // the panel
      for (const rx of [-0.205, 0, 0.2]) {
        const [x, z] = tpRel(rx, -0.01);
        g.add(box(tpW * 0.136, 0.003, tpD * 0.29, std(0x141517, { roughness: 0.5 }), x, under - 0.0055, z)); // a board
        g.add(box(0.05, 0.002, 0.05, CHIP(), x + 0.02, under - 0.0075, z - 0.03));
        g.add(box(0.03, 0.002, 0.025, CHIP(), x - 0.03, under - 0.0075, z + 0.06));
      }
      for (const rx of [-0.37, 0.355]) {
        const [x, z] = tpRel(rx, 0.2);
        g.add(box(0.05, 0.004, 0.06, METAL(), x, under - 0.004, z)); // force sensors
      }
      const [, bz] = tpRel(0, 0.31);
      g.add(box(tpW * 0.6, 0.004, 0.08, METAL(), 0, under - 0.004, bz)); // the bracket
      for (const rx of [-0.462, 0.462]) {
        for (const rz of [-0.443, -0.36, 0.357, 0.44]) {
          const [x, z] = tpRel(rx, rz);
          g.add(cylinder(0.012, 0.003, std(0x6f737a, { metalness: 0.9, roughness: 0.3 }), x, under - 0.004, z, 12)); // screws
        }
      }
      const flex = area(IN, 995, 745, 1045, 1085);
      g.add(box(flex.w, 0.003, flex.d, std(0x141517, { roughness: 0.7 }), flex.x, FLOOR + 0.054, flex.z)); // its flex between the middle cells
      const conn = area(IN, 885, 1085, 1150, 1135);
      g.add(slab(conn.w, 0.012, conn.d, conn.d / 2, std(0x141517, { roughness: 0.6 }), conn.x, FLOOR + 0.03, conn.z, 0));
      for (const [a, b] of [[922, 956], [966, 1002], [1024, 1060], [1080, 1114]]) {
        const pad = area(IN, a, 1095, b, 1125);
        for (const y of [FLOOR + 0.0225, FLOOR + 0.0375]) g.add(box(pad.w, 0.002, pad.d, std(0xd28654, { metalness: 0.9, roughness: 0.3 }), pad.x, y, pad.z)); // copper contacts
      }
    }),
  );

  const hole = () => std(0x050506, { roughness: 1 });
  const portY = BASE * 0.48;
  root.add(
    part('ports_left', [-0.75, 0.15, 0], (g) => {
      const x = -W / 2 + 0.004;
      const at = S.left; // all in the rear half, from the hinge: MagSafe, TB, TB, the jack (the user's photo)
      g.add(pill(0.17, 0.035, hole(), x, portY, back + at.magsafe)); // MagSafe 3
      g.add(pill(0.15, 0.02, std(0x9a9ea4, { metalness: 0.9, roughness: 0.3 }), x - 0.0012, portY, back + at.magsafe)); // its contacts
      for (const z of at.tb) g.add(pill(0.09, 0.03, hole(), x, portY, back + z)); // Thunderbolt 5
      const jack = cylinder(0.018, 0.02, hole(), x, portY, back + at.jack, 20);
      jack.rotation.z = Math.PI / 2;
      g.add(jack); // headphone jack
    }),
  );

  root.add(
    part('ports_right', [0.75, 0.15, 0], (g) => {
      const x = W / 2 - 0.004;
      const at = S.right; // from the hinge: HDMI, TB, SD
      g.add(box(0.012, 0.03, 0.15, hole(), x, portY, back + at.hdmi)); // HDMI
      g.add(pill(0.09, 0.03, hole(), x, portY, back + at.tb)); // Thunderbolt 5
      g.add(box(0.012, 0.012, 0.26, hole(), x, portY, back + at.sd)); // SDXC
    }),
  );

  root.add(
    part('display', [0, 1.95, -2.1], (g) => {
      const lid = new THREE.Group();
      lid.position.set(0, BASE, back);
      lid.rotation.x = -(105 * Math.PI) / 180;
      const [sw, sh] = S.screen;
      lid.add(slab(W, S.LID - 0.006, D, R, body(), 0, S.LID / 2, D / 2)); // back cover
      lid.add(slab(W - 0.02, 0.006, D - 0.02, R - 0.01, std(0x020203, { roughness: 0.08, metalness: 0.2 }), 0, -0.002, D / 2, 0)); // glass
      const screen = screenTexture();
      lid.add(box(sw, 0.002, sh, std(0x000000, { map: screen, emissive: 0xffffff, emissiveMap: screen, emissiveIntensity: 0.85 }), 0, -0.0062, S.chin + sh / 2));
      lid.add(slab(S.notch, 0.003, 0.14, 0.03, std(0x000000), 0, -0.0068, D - 0.07, 0)); // camera housing (into the menu bar)
      const logo = new THREE.Mesh(
        new THREE.PlaneGeometry(S.logo, S.logo * 1.2),
        std(0x17181b, { map: appleLogo(), transparent: true, alphaTest: 0.5, metalness: 0.9, roughness: 0.16 }),
      );
      logo.geometry.rotateX(-Math.PI / 2); // on the outer face…
      logo.geometry.rotateY(Math.PI); // …upright for someone looking at the open lid from behind
      logo.position.set(0, S.LID + 0.0006, D / 2);
      lid.add(logo); // the mirror-polished logo, as on the user's lid
      lid.add(cylinder(0.012, 0.002, std(0x0b1a2a, { roughness: 0.1 }), 0, -0.0082, D - 0.035, 16)); // 12MP camera
      g.add(lid);
      const hinge = new THREE.Mesh(new THREE.CylinderGeometry(0.035, 0.035, W - 0.62, 24), std(0x101113, { roughness: 0.6 }));
      hinge.rotation.z = Math.PI / 2;
      hinge.position.set(0, BASE - 0.01, back + 0.01);
      g.add(hinge);
      const bar = area(IN, 430, 175, 1590, 228);
      g.add(box(bar.w, 0.012, bar.d, METAL(), bar.x, FLOOR + 0.03, bar.z)); // the antenna bar over the hinge
      for (let i = 0; i < 12; i += 1) g.add(box(0.12, 0.013, 0.012, std(0x2a2c30), bar.x - bar.w / 2 + 0.15 + i * ((bar.w - 0.3) / 11), FLOOR + 0.0305, bar.z)); // its slots
      for (const side of [-1, 1]) {
        const mount = new THREE.Shape();
        mount.moveTo(0, 0);
        mount.lineTo(0.34, 0);
        mount.lineTo(0, 0.22);
        mount.closePath();
        const mountGeo = new THREE.ExtrudeGeometry(mount, { depth: 0.012, bevelEnabled: false });
        mountGeo.rotateX(Math.PI / 2); // flat, the right angle at the corner
        const m = new THREE.Mesh(mountGeo, METAL());
        m.scale.x = -side;
        m.position.set(side * (W / 2 - 0.03), FLOOR + 0.045, back + 0.03);
        g.add(m); // the hinge mount
      }
    }),
  );

  for (const child of root.children) {
    child.userData.base = child.position.clone();
    for (const piece of child.children) piece.userData.base = piece.position.clone();
    child.traverse((o) => {
      if (o.isMesh) {
        o.userData.partId = child.name;
        o.userData.baseEmissive = o.material.emissive ? o.material.emissive.getHex() : 0;
      }
    });
  }
  root.position.y = -0.06;
  return root;
}
