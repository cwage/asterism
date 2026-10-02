// A made-up solve, so these tests need neither the solver nor anyone's
// photo: a camera with a known pose looks at Orion, and every bright named
// star it would see becomes a label at the pixel a pinhole puts it. The
// page's camera fit should hand the same pose back.
//
// The stars themselves are the app's own: asked of the web service the way
// the page asks, so the fixture and the page cannot disagree about which
// star is which.

import zlib from 'node:zlib';

const BASE = process.env.BASE_URL || 'http://web:8000';

export const JOB_ID = '0123456789abcdef0123456789abcdef';
export const POSE = { ra: 83.8, dec: -1.5, vfov: 60, width: 1200, height: 1600 };
// The same sky through a camera held the other way: the photo a phone
// held upright has the least room for.
export const LANDSCAPE = { ra: 83.8, dec: -1.5, vfov: 45, width: 1600, height: 1200 };
// And through an ultrawide: a photo that takes in a hundred degrees.
export const WIDE = { ra: 83.8, dec: -1.5, vfov: 100, width: 1200, height: 1600 };

const rad = (deg) => (deg * Math.PI) / 180;
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const unit = (v) => v.map((c) => c / Math.hypot(...v));

// The catalog, fetched once. The first ask is also what waits for the web
// service to come up, and what has it build the star files (fly.py).
let sky = null;
async function loadSky() {
  for (let attempt = 0; ; attempt++) {
    try {
      const catalog = await (await fetch(`${BASE}/fly/catalog.json`)).json();
      const bin = await (await fetch(`${BASE}/fly/stars.bin?v=${catalog.version}`)).arrayBuffer();
      return { catalog, stars: new Float32Array(bin) };
    } catch (e) {
      if (attempt >= 60) throw new Error(`no star catalog from ${BASE}: ${e.message}`);
      await new Promise((again) => setTimeout(again, 1000));
    }
  }
}

async function labels(pose) {
  const { catalog, stars } = await (sky ??= loadSky());

  const { ra, dec, vfov, width, height } = pose;
  const forward = [Math.cos(rad(dec)) * Math.cos(rad(ra)), Math.cos(rad(dec)) * Math.sin(rad(ra)), Math.sin(rad(dec))];
  const up = unit([0, 0, 1].map((c, i) => c - forward[2] * forward[i]));   // north, squared to the view
  const right = cross(forward, up);
  const f = height / 2 / Math.tan(rad(vfov) / 2);

  const out = [];
  for (const [name, index] of Object.entries(catalog.names)) {
    if (name === 'Sun' || index > 400) continue;   // sorted brightest first
    const dir = unit([...stars.subarray(index * 5, index * 5 + 3)]);
    const depth = dot(dir, forward);
    if (depth <= 0.1) continue;
    const x = width / 2 + (f * dot(dir, right)) / depth;
    const y = height / 2 - (f * dot(dir, up)) / depth;
    if (x < 0 || x > width || y < 0 || y > height) continue;
    out.push({ name, x, y, mag: 0, kind: 'star', status: 'matched' });
  }
  return out;
}

export async function job(pose = POSE) {
  return {
    id: JOB_ID,
    status: 'done',
    exif: {
      width: pose.width, height: pose.height,
      // A winter evening: the Sun is well behind a camera facing Orion.
      datetime_original: '2026:01:15 22:00:00', offset_time_original: '+00:00',
    },
    result: { success: true, labels: await labels(pose) },
  };
}

// The "photo": a small dark-blue gradient with the photo's shape. Written
// out by hand because a PNG this plain is not worth a dependency.
export function image(pose = POSE) {
  const width = pose.width / 4, height = pose.height / 4;
  const raw = Buffer.alloc(height * (1 + width * 3));
  for (let y = 0; y < height; y++) {
    const row = y * (1 + width * 3);
    for (let x = 0; x < width; x++) {
      raw.set([10 + (40 * x) / width, 16 + (30 * y) / height, 60], row + 1 + x * 3);
    }
  }
  const table = Array.from({ length: 256 }, (_, n) => {
    for (let k = 0; k < 8; k++) n = n & 1 ? 0xedb88320 ^ (n >>> 1) : n >>> 1;
    return n >>> 0;
  });
  const crc = (buf) => {
    let c = 0xffffffff;
    for (const byte of buf) c = table[(c ^ byte) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };
  const chunk = (type, data) => {
    const body = Buffer.concat([Buffer.from(type), data]);
    const out = Buffer.alloc(body.length + 8);
    out.writeUInt32BE(data.length, 0);
    body.copy(out, 4);
    out.writeUInt32BE(crc(body), body.length + 4);
    return out;
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header.set([8, 2, 0, 0, 0], 8);   // 8 bits, RGB
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', header), chunk('IDAT', zlib.deflateSync(raw)), chunk('IEND', Buffer.alloc(0)),
  ]);
}

// Answer the page's two requests about the job from the fixture. Everything
// else it asks for (its scripts, the star data) goes to the app.
export async function serveFixture(page, pose = POSE) {
  const body = JSON.stringify(await job(pose)), png = image(pose);
  await page.route(`**/jobs/${JOB_ID}`, (route) => route.fulfill({ contentType: 'application/json', body }));
  await page.route(`**/jobs/${JOB_ID}/image`, (route) => route.fulfill({ contentType: 'image/png', body: png }));
}
