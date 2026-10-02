// The fly-around page's camera fit (static/fly/fit.mjs): given where named
// stars landed in a photo and where they are on the sky, recover which way
// the camera pointed and its focal length. A solve's result has no WCS in
// it, so this is how the page lines itself up with the photo.
import assert from 'node:assert/strict';
import test from 'node:test';
import { fitCamera } from '../../static/fly/fit.mjs';

// Deterministic noise, so a failure reproduces.
function rng(seed) {
  return () => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 2 ** 32;
  };
}

function rotate([qx, qy, qz, qw], [x, y, z]) {
  const cx = qy * z - qz * y, cy = qz * x - qx * z, cz = qx * y - qy * x;
  return [
    x + 2 * (qw * cx + qy * cz - qz * cy),
    y + 2 * (qw * cy + qz * cx - qx * cz),
    z + 2 * (qw * cz + qx * cy - qy * cx),
  ];
}

function randomQuaternion(rand) {
  const q = [rand() - 0.5, rand() - 0.5, rand() - 0.5, rand() - 0.5];
  const len = Math.hypot(...q);
  return q.map((v) => v / len);
}

// Stars scattered over the frame of a camera with a known pose.
function scene(rand, { width, height, f, n, noise = 0 }) {
  const quaternion = randomQuaternion(rand);
  const pairs = [];
  for (let i = 0; i < n; i++) {
    const x = rand() * width, y = rand() * height;
    const ray = [x - width / 2, -(y - height / 2), -f];
    const len = Math.hypot(...ray);
    pairs.push({
      dir: rotate(quaternion, ray.map((v) => v / len)),
      px: [x + (rand() - 0.5) * 2 * noise, y + (rand() - 0.5) * 2 * noise],
    });
  }
  return { quaternion, pairs };
}

// Angle between two orientations, in degrees; q and -q are the same one.
function angleBetween(a, b) {
  const dot = Math.abs(a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]);
  return (2 * Math.acos(Math.min(1, dot)) * 180) / Math.PI;
}

test('recovers pose and focal length across fields of view', () => {
  const rand = rng(1);
  const width = 3072, height = 4080;
  // An ultrawide phone lens, a main lens, a short telephoto, a long one.
  for (const f of [1400, 3000, 9000, 40000]) {
    const { quaternion, pairs } = scene(rand, { width, height, f, n: 30 });
    const fit = fitCamera(pairs, width, height);
    assert.ok(Math.abs(fit.f / f - 1) < 1e-4, `f ${fit.f} vs ${f}`);
    assert.ok(angleBetween(fit.quaternion, quaternion) < 0.01, `pose off at f=${f}`);
    assert.ok(fit.rmsPx < 0.05, `rms ${fit.rmsPx} at f=${f}`);
  }
});

test('tolerates pixel noise', () => {
  const rand = rng(2);
  const width = 4000, height = 3000, f = 2800;
  const { quaternion, pairs } = scene(rand, { width, height, f, n: 40, noise: 4 });
  const fit = fitCamera(pairs, width, height);
  assert.ok(Math.abs(fit.f / f - 1) < 0.01, `f ${fit.f}`);
  assert.ok(angleBetween(fit.quaternion, quaternion) < 0.2);
  assert.ok(fit.rmsPx < 6);
});

test('drops a label that points at the wrong star', () => {
  const rand = rng(3);
  const width = 4000, height = 3000, f = 2800;
  const { quaternion, pairs } = scene(rand, { width, height, f, n: 25, noise: 1 });
  pairs[4].px = [pairs[4].px[0] + 900, pairs[4].px[1] - 700];
  const fit = fitCamera(pairs, width, height);
  assert.equal(fit.rejected, 1);
  assert.ok(angleBetween(fit.quaternion, quaternion) < 0.1);
});

test('works from three stars and refuses two', () => {
  const rand = rng(4);
  const width = 4000, height = 3000, f = 2800;
  const { quaternion, pairs } = scene(rand, { width, height, f, n: 3 });
  const fit = fitCamera(pairs, width, height);
  assert.ok(angleBetween(fit.quaternion, quaternion) < 0.05);
  assert.throws(() => fitCamera(pairs.slice(0, 2), width, height), /at least 3/);
});
