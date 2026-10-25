// Jarvis Lab — what the MacBook's parts are and how it comes apart. The model itself is lab-model-a.js (the user's
// MacBook Pro 16″ M5 Pro, from their photos and from teardown frames of the 16″, 17.10); the first 14″ schematic that
// lived here is gone (17.10, the user: «убери старую модель 14, которая была в самом начале как тестовая»).
// Every part is a THREE.Group with userData { id, title, about, explode: [dx, dy, dz] } — the explode vector is where
// the part flies when the laptop opens up. 1 unit = 100 mm. x — left/right, y — up, z — towards the viewer.

export const PARTS_INFO = {
  bottom: [
    'Нижний корпус',
    'Алюминиевое дно на восьми винтах Pentalobe, четыре ножки. Изнутри — тёмная изоляционная плёнка под процессором, ' +
      'экранирующие пластины с пружинными контактами у петли и резиновые упоры. Снимается первым.',
  ],
  battery: [
    'Аккумулятор',
    '100 Вт·ч, шесть ячеек: по две у каждого бока и две большие в центре, между ними проходит шлейф трекпада. ' +
      'Приклеен к корпусу. Самая опасная деталь при разборке: прокол или изгиб ячейки может вызвать возгорание — ' +
      'перед любой работой отключают разъём аккумулятора от платы.',
  ],
  board: [
    'Материнская плата',
    'Во всю ширину корпуса, с вырезами под два вентилятора. В центре — M5 Pro: CPU, GPU, Neural Engine и объединённая ' +
      'память в одном корпусе; по краям — SSD и контроллеры разъёмов. Память и SSD распаяны — отдельно не меняются.',
  ],
  heatsink: [
    'Радиатор и тепловые трубки',
    'Пластина над процессором и U-образные тепловые трубки несут тепло к двум блокам рёбер у задней кромки, ' +
      'откуда его выдувают вентиляторы.',
  ],
  fans: [
    'Вентиляторы',
    'Два центробежных вентилятора-турбины по бокам: втягивают воздух снизу и выдувают через рёбра радиатора под петлёй.',
  ],
  speakers: [
    'Динамики',
    'Шесть динамиков: пары вуферов с подавлением вибрации — в нижних углах у аккумулятора, твитеры — у решёток по ' +
      'бокам клавиатуры.',
  ],
  topcase: [
    'Верхний корпус',
    'Алюминиевая рама с клавиатурой и трекпадом; изнутри под клавишами — стальная пластина на заклёпках. На неё ' +
      'крепится вся начинка.',
  ],
  keyboard: ['Клавиатура', 'Magic Keyboard с ножничным механизмом, подсветкой и подписями EN/RU; Touch ID — в кнопке питания справа вверху.'],
  trackpad: [
    'Трекпад Force Touch',
    'Стеклянная панель без механической кнопки: «щелчок» имитирует вибромотор Taptic Engine под ней. Шлейф идёт к ' +
      'плате между ячейками аккумулятора.',
  ],
  ports_left: ['Разъёмы слева', 'От петли: MagSafe 3 (зарядка), два Thunderbolt 5 (USB-C) и 3,5-мм аудиоразъём.'],
  ports_right: ['Разъёмы справа', 'От петли: HDMI, Thunderbolt 5 (USB-C) и слот для карт SDXC.'],
  display: [
    'Дисплей',
    'Liquid Retina XDR 16,2″ (mini-LED, 3456 × 2234) с вырезом для камеры 12 Мп. Шлейфы дисплея и камеры проходят ' +
      'через петли к плате.',
  ],
};

/** Explode factor 0…1 → part positions (speakers also spread sideways). */
export function applyExplode(root, f) {
  const shell = root.getObjectByName('topcase');
  const solid = shell?.userData.restOpacity ?? 0.92; // solid when assembled
  shell?.traverse((o) => {
    if (o.isMesh) o.material.opacity = solid - (solid - 0.2) * f; // the inside shows through as the laptop opens up
  });
  for (const child of root.children) {
    const [dx, dy, dz] = child.userData.explode || [0, 0, 0];
    child.position.set(child.userData.base.x + dx * f, child.userData.base.y + dy * f, child.userData.base.z + dz * f);
    const sides = child.userData.explodeSides;
    if (sides) {
      for (const piece of child.children) {
        const base = piece.userData.base;
        piece.position.x = base.x + Math.sign(base.x) * sides * f;
      }
    }
  }
}
