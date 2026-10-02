// Recover the camera from a solve's labels, for the fly-around page.
//
// A job's result gives each labelled star a name and a pixel position but
// not the WCS behind them (that stays on the worker's disk, and is swept).
// The catalog gives each name a direction on the sky. A pinhole camera at
// Earth is then one rotation and one focal length, fitted here: for a trial focal length the pixels become rays, the
// best rotation taking those rays onto the sky directions comes from Horn's
// closed form, and the focal length is searched for the tightest fit.
//
// Everything is in three.js camera conventions: the camera looks down -Z
// with +Y up, and the quaternion returned is the camera's world orientation.

// Largest eigenvalue and its eigenvector for a symmetric 4x4, by Jacobi
// rotations. Power iteration would be shorter but stalls on a narrow field,
// where roll about the axis is barely constrained and the top two
// eigenvalues nearly meet.
function topEigen(a) {
  const n = 4;
  const v = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]];
  for (let sweep = 0; sweep < 50; sweep++) {
    let off = 0;
    for (let p = 0; p < n; p++) for (let q = p + 1; q < n; q++) off += a[p][q] * a[p][q];
    if (off < 1e-22) break;
    for (let p = 0; p < n; p++) {
      for (let q = p + 1; q < n; q++) {
        if (Math.abs(a[p][q]) < 1e-300) continue;
        const theta = (a[q][q] - a[p][p]) / (2 * a[p][q]);
        const t = Math.sign(theta || 1) / (Math.abs(theta) + Math.sqrt(theta * theta + 1));
        const c = 1 / Math.sqrt(t * t + 1), s = t * c;
        for (let k = 0; k < n; k++) {
          const akp = a[k][p], akq = a[k][q];
          a[k][p] = c * akp - s * akq;
          a[k][q] = s * akp + c * akq;
        }
        for (let k = 0; k < n; k++) {
          const apk = a[p][k], aqk = a[q][k];
          a[p][k] = c * apk - s * aqk;
          a[q][k] = s * apk + c * aqk;
        }
        for (let k = 0; k < n; k++) {
          const vkp = v[k][p], vkq = v[k][q];
          v[k][p] = c * vkp - s * vkq;
          v[k][q] = s * vkp + c * vkq;
        }
      }
    }
  }
  let best = 0;
  for (let i = 1; i < n; i++) if (a[i][i] > a[best][best]) best = i;
  return { value: a[best][best], vector: v.map((row) => row[best]) };
}

// Rotation taking each `from` unit vector onto its `to` (Horn 1987). The
// eigenvalue is the sum of cosines between the rotated and target vectors,
// so n minus it measures the misfit.
function bestRotation(from, to) {
  const S = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
  for (let i = 0; i < from.length; i++) {
    for (let a = 0; a < 3; a++) for (let b = 0; b < 3; b++) S[a][b] += from[i][a] * to[i][b];
  }
  const [[xx, xy, xz], [yx, yy, yz], [zx, zy, zz]] = S;
  const { value, vector } = topEigen([
    [xx + yy + zz, yz - zy, zx - xz, xy - yx],
    [yz - zy, xx - yy - zz, xy + yx, zx + xz],
    [zx - xz, xy + yx, -xx + yy - zz, yz + zy],
    [xy - yx, zx + xz, yz + zy, -xx - yy + zz],
  ]);
  const [w, x, y, z] = vector;
  return { score: value, quaternion: [x, y, z, w] };
}

function rays(pairs, f, cx, cy) {
  return pairs.map(({ px }) => {
    const x = px[0] - cx, y = -(px[1] - cy), z = -f;
    const len = Math.hypot(x, y, z);
    return [x / len, y / len, z / len];
  });
}

function rotate([qx, qy, qz, qw], [x, y, z]) {
  // v + 2w(q x v) + 2 q x (q x v)
  const cx = qy * z - qz * y, cy = qz * x - qx * z, cz = qx * y - qy * x;
  return [
    x + 2 * (qw * cx + qy * cz - qz * cy),
    y + 2 * (qw * cy + qz * cx - qx * cz),
    z + 2 * (qw * cz + qx * cy - qy * cx),
  ];
}

// Where the fitted camera puts each star, against where the label has it.
function residuals(pairs, quaternion, f, cx, cy) {
  const inverse = [-quaternion[0], -quaternion[1], -quaternion[2], quaternion[3]];
  return pairs.map(({ dir, px }) => {
    const c = rotate(inverse, dir);
    if (c[2] >= 0) return Infinity;
    return Math.hypot(cx + f * c[0] / -c[2] - px[0], cy - f * c[1] / -c[2] - px[1]);
  });
}

function fitOnce(pairs, width, height) {
  const cx = width / 2, cy = height / 2;
  const dirs = pairs.map((p) => p.dir);
  const score = (logF) => bestRotation(rays(pairs, Math.exp(logF), cx, cy), dirs).score;

  // Diagonal field of view from 170 degrees down to 1: a coarse pass to
  // find the right basin, then golden section inside it.
  const halfDiag = Math.hypot(width, height) / 2;
  const lo = Math.log(halfDiag / Math.tan((85 * Math.PI) / 180));
  const hi = Math.log(halfDiag / Math.tan((0.5 * Math.PI) / 180));
  const steps = 60;
  let bestI = 0, bestScore = -Infinity;
  for (let i = 0; i <= steps; i++) {
    const s = score(lo + ((hi - lo) * i) / steps);
    if (s > bestScore) { bestScore = s; bestI = i; }
  }
  let a = lo + ((hi - lo) * Math.max(bestI - 1, 0)) / steps;
  let b = lo + ((hi - lo) * Math.min(bestI + 1, steps)) / steps;
  const g = (Math.sqrt(5) - 1) / 2;
  let c = b - g * (b - a), d = a + g * (b - a);
  let sc = score(c), sd = score(d);
  for (let i = 0; i < 60; i++) {
    if (sc > sd) { b = d; d = c; sd = sc; c = b - g * (b - a); sc = score(c); }
    else { a = c; c = d; sc = sd; d = a + g * (b - a); sd = score(d); }
  }
  const f = Math.exp((a + b) / 2);
  const { quaternion } = bestRotation(rays(pairs, f, cx, cy), dirs);
  return { quaternion, f };
}

// pairs: [{ dir: [x, y, z] unit vector to the star, px: [x, y] in the image }]
// Returns the camera's world quaternion [x, y, z, w], the focal length in
// pixels, the vertical field of view in degrees, and how well it fits.
// Throws if there are too few stars to pin a camera down.
export function fitCamera(pairs, width, height) {
  if (pairs.length < 3) {
    throw new Error(`need at least 3 labelled stars to recover the camera, have ${pairs.length}`);
  }
  const cx = width / 2, cy = height / 2;
  const floor = 0.005 * Math.hypot(width, height);
  // Rejection: a label snapped to the wrong source, or a name that joined
  // to the wrong catalog row, should not tilt the whole camera. Each round
  // judges every star against the latest fit, so a star the bad one pushed
  // out of line in the first fit comes back once the bad one is gone.
  let used = pairs;
  let fit = fitOnce(used, width, height);
  for (let round = 0; round < 5; round++) {
    const errors = residuals(pairs, fit.quaternion, fit.f, cx, cy);
    const sorted = [...errors].sort((x, y) => x - y);
    const limit = Math.max(3 * sorted[Math.floor(sorted.length / 2)], floor);
    const kept = pairs.filter((_, i) => errors[i] <= limit);
    if (kept.length < 3) break;
    const same = kept.length === used.length && kept.every((p, i) => p === used[i]);
    used = kept;
    if (same) break;
    fit = fitOnce(used, width, height);
  }
  fit.errors = residuals(used, fit.quaternion, fit.f, cx, cy);
  const rms = Math.sqrt(fit.errors.reduce((s, e) => s + e * e, 0) / fit.errors.length);
  return {
    quaternion: fit.quaternion,
    f: fit.f,
    vfov: (2 * Math.atan(height / 2 / fit.f) * 180) / Math.PI,
    rmsPx: rms,
    used: used.length,
    rejected: pairs.length - used.length,
  };
}
