// The fly-around page: a solve's stars at their real distances, seen from
// outside. See the README ("From outside") for what it is and what it gets
// wrong; the comments here are about why it moves the way it does.
import * as THREE from 'three';
import { fitCamera } from './fit.mjs';

const PC_TO_LY = 3.26156;
// One lap of the tour, in seconds: rest on the photo, rise and back away,
// circle once, come back in. It ends where it began, so it loops.
const TOUR = { hold: 2.5, pull: 7, orbit: 26 };
const TOUR_SECONDS = TOUR.hold + 2 * TOUR.pull + TOUR.orbit;
// Backing away, the camera also rises this far over the line of sight, and
// it rises first. Straight back from someone looking up at the sky is down
// through the ground: the planet would come out of the camera and shrink
// into the middle of its own photo. Rising clears it, and puts Earth where
// it belongs in the shot from behind, low in the foreground with the photo
// beyond.
const TOUR_RISE_DEG = 18;
// Earth and the Sun are signs, not bodies. From where the photo's stars
// fit in one view, home at true scale is a hundredth of a pixel, and
// leaving it at true scale is a million-fold zoom during which the photo
// and every star stand still. So Earth is a ball some light-years across:
// sized so that from where the backing away ends its radius is this
// fraction of the screen's shorter side, and fixed at that size in space,
// so that it shrinks and grows with the camera's distance like everything
// else in the scene.
const EARTH_SIZE = 0.0135, EARTH_MIN_PX = 6;
// Up close a ball that size would fill the view, and backing away from it
// would be the planet ballooning out of the camera. It is drawn only from
// a distance, coming in between these fractions of the way to where the
// backing away ends: it is never seen much more than twice its final size.
const HOME_FADE_FROM = 0.4, HOME_FADE_TO = 0.7;
// The Sun, in Earth radii: how far from Earth, the bright disc, the glow.
// The real one is 109 Earths across and 23,000 Earth radii away. At 109,
// either Earth is under a pixel or the Sun is wider than the screen, so
// this keeps the proportion in spirit: a Sun that plainly dwarfs Earth,
// with Earth still a ball that can be seen. It used to be drawn a little
// smaller than Earth, which is the one thing everybody knows it is not.
const SUN_AWAY = 7.5, SUN_DISC = 4, SUN_GLOW = 5.6;
// Where Earth sits in the shot the backing away ends on, as a fraction of
// the way down the screen: under the photo, clear of the controls along
// the bottom. The camera is tipped by whatever that takes.
const EARTH_AT_HEIGHT = 0.66;
// The photo hangs in space as a sheet facing Earth. Seen square-on it is a
// picture of the sky; seen from the side it is a smear, so it fades
// between these two angles off its face, and is gone from behind.
const PHOTO_CLEAR_DEG = 30, PHOTO_GONE_DEG = 72;
// From far back it dims a little so the stars in front of it still read.
const PHOTO_FAR_OPACITY = 0.7;
const HOLD_BACK_MAG = 4;
// The lens changes as the camera backs away. At home it has to be the
// photo's own, which on a wide or a long lens, or a phone held upright
// with a landscape photo across it, can be anything from 15 to 130
// degrees. Outside, that is either a keyhole or a fisheye in which a ball
// off-centre is an egg. So outside, the screen's narrower side is given a
// field between the first two of these, and the wider side no more than
// the third.
const OUT_FOV_MIN = 38, OUT_FOV_MAX = 55, OUT_FOV_WIDEST = 80;
// In the shot the backing away ends on, the photo takes up no more than
// this much of the screen either way. A wide-angle photo is a big sheet:
// through the lens used outside it would otherwise run off the screen.
const PHOTO_FILLS = 0.55;
// With no solve to show: Orion, north up.
const DEMO = { ra: 83.8, dec: -1.5, vfov: 50 };
// The photo is a texture no bigger than this on a side. An upload can be
// 100 megapixels and a phone's GPU stops at 4096 on a side or less; the
// sheet is never drawn larger than the screen anyway.
const TEXTURE_MAX = 2048;

const $ = (id) => document.getElementById(id);
const stage = $('stage'), labelLayer = $('labels');
const params = new URLSearchParams(location.search);
// ?speed=4 runs the tour four times as fast: for the browser tests, and
// for anyone in a hurry.
const SPEED = Math.min(Math.max(Number(params.get('speed')) || 1, 0.1), 100);

const rad = THREE.MathUtils.degToRad;
const smooth = (x) => x * x * (3 - 2 * x);

// three.js needs WebGL 2. A browser with hardware acceleration turned off,
// or a GPU it has blocklisted, has none, and the renderer throws. Left to
// throw here, the module stops before anything is drawn and the page is a
// black screen saying it is loading, for good. So it is caught, and the
// page says what is wrong instead (see the end of the file).
let renderer = null;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setClearColor(0x02030a);
  stage.prepend(renderer.domElement);
} catch {
  renderer = null;
}
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(DEMO.vfov, 1, 1e-4, 1e7);

const state = {
  aspect: null,          // the photo's width over height, when there is one
  vfov: DEMO.vfov,       // across the photo's height, or the window's without one
  homeFov: DEMO.vfov,    // the camera's own, top to bottom of the screen: at Earth,
  outFov: DEMO.vfov,     // and from outside
  base: new THREE.Quaternion(),
  // The camera rises and backs away from Earth until the photo's nearer
  // stars fit in view as one object, circles a point in their midst, and
  // returns. u is the place in that tour, 0 to 1; swing, over and zoom are
  // what dragging and the wheel add on top of it.
  u: 0, swing: 0, over: 0, zoom: 1,
  out: 0,                // how far backed away the tour is now, 0 to 1
  pivot: 100,            // parsecs from Earth to that point, along the view
  spread: 100,           // parsecs around that point that have to fit in view
  reach: 20,             // how many times as far as Earth the camera backs off to
  far: 1000,             // parsecs from that point past which nothing is joined up
  playing: false,
  labels: [],
  dirty: true,
};

// ---- catalog ---------------------------------------------------------------

// B-V colour index to an approximate star colour: Ballesteros' formula for
// the temperature, then a blackbody fit, pulled toward white because a
// saturated point of light reads as a coloured dot rather than a star.
function starColor(bv) {
  bv = Math.min(2, Math.max(-0.4, bv));
  const k = (4600 * (1 / (0.92 * bv + 1.7) + 1 / (0.92 * bv + 0.62))) / 100;
  const r = k <= 66 ? 255 : 329.698727446 * (k - 60) ** -0.1332047592;
  const g = k <= 66 ? 99.4708025861 * Math.log(k) - 161.1195681661
    : 288.1221695283 * (k - 60) ** -0.0755148492;
  const b = k >= 66 ? 255 : k <= 19 ? 0 : 138.5177312231 * Math.log(k - 10) - 305.0447927307;
  return [r, g, b].map((v) => 0.4 + 0.6 * Math.min(1, Math.max(0, v / 255)));
}

async function loadCatalog() {
  const catalog = await fetch('/fly/catalog.json').then((r) => {
    if (!r.ok) throw new Error(`the star catalog is not available (${r.status})`);
    return r.json();
  });
  // The star file is cached for good under its version (fly.py), so the
  // version is part of what is asked for.
  const buffer = await fetch(`/fly/stars.bin?v=${catalog.version}`).then((r) => {
    if (!r.ok) throw new Error(`the star catalog is not available (${r.status})`);
    return r.arrayBuffer();
  });
  // Five float32 to a star. Anything else is not the file the index
  // describes: an error page read as numbers, or a download cut short.
  if (buffer.byteLength !== catalog.count * 20) throw new Error('the star catalog did not arrive whole');
  const raw = new Float32Array(buffer);
  const n = catalog.count;
  const position = new Float32Array(n * 3), absmag = new Float32Array(n), tint = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    position.set(raw.subarray(i * 5, i * 5 + 3), i * 3);
    absmag[i] = raw[i * 5 + 3];
    tint.set(starColor(raw[i * 5 + 4]), i * 3);
  }
  // The catalog's Sun sits at the origin, which here is Earth, where the
  // photo was taken. It is taken out of the star field; home is drawn on
  // its own (buildHome).
  absmag[catalog.names.Sun] = 99;
  return { ...catalog, position, absmag, tint };
}

const starMaterial = new THREE.ShaderMaterial({
  uniforms: { uMagLimit: { value: 6.5 }, uPx: { value: 1 }, uGain: { value: 0 } },
  // Brightness is worked out per frame from where the camera is: a star's
  // apparent magnitude at distance d parsecs is its absolute magnitude plus
  // 5 log10(d) - 5, so stars brighten as they are approached. uGain is the
  // exposure, in magnitudes: from far enough back to see the whole spread,
  // nothing in it would be visible to the eye, so the exposure rises with
  // the distance.
  vertexShader: `
    attribute float absmag;
    attribute vec3 tint;
    uniform float uMagLimit;
    uniform float uPx;
    uniform float uGain;
    varying vec3 vTint;
    varying float vAlpha;
    void main() {
      vec4 mv = modelViewMatrix * vec4(position, 1.0);
      float d = max(length(mv.xyz), 1e-6);
      float headroom = uMagLimit + uGain - (absmag + 5.0 * log(d) / log(10.0) - 5.0);
      vTint = tint;
      vAlpha = clamp(headroom / 1.5, 0.0, 1.0);
      gl_PointSize = headroom <= 0.0 ? 0.0 : uPx * min(2.0 + 1.6 * headroom, 48.0);
      gl_Position = projectionMatrix * mv;
    }`,
  fragmentShader: `
    varying vec3 vTint;
    varying float vAlpha;
    void main() {
      vec2 p = gl_PointCoord * 2.0 - 1.0;
      float r2 = dot(p, p);
      if (r2 > 1.0) discard;
      gl_FragColor = vec4(vTint, exp(-r2 * 3.5) * vAlpha);
    }`,
  blending: THREE.AdditiveBlending,
  transparent: true,
  depthTest: false,
  depthWrite: false,
});

const lineMaterial = new THREE.LineBasicMaterial({
  color: 0x6f9bd1, transparent: true, opacity: 0.6, depthTest: false,
});
const markerMaterial = new THREE.PointsMaterial({
  color: 0xfff4e0, size: 5, sizeAttenuation: false, transparent: true, opacity: 0, depthTest: false,
});
const sightMaterial = new THREE.LineBasicMaterial({
  color: 0xffb86b, transparent: true, opacity: 0.16, depthTest: false,
});

let catalog, figures, sightlines;

function buildStars() {
  const stars = new THREE.BufferGeometry();
  stars.setAttribute('position', new THREE.BufferAttribute(catalog.position, 3));
  stars.setAttribute('absmag', new THREE.BufferAttribute(catalog.absmag, 1));
  stars.setAttribute('tint', new THREE.BufferAttribute(catalog.tint, 3));
  const points = new THREE.Points(stars, starMaterial);
  points.frustumCulled = false;
  scene.add(points);
}

function segments(ends, material) {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(ends, 3));
  const lines = new THREE.LineSegments(geometry, material);
  lines.frustumCulled = false;
  scene.add(lines);
  return lines;
}

function starPosition(i) {
  return new THREE.Vector3().fromArray(catalog.position, i * 3);
}

// HYG's placeholder for "no usable parallax" is not a place to draw to.
const hasDistance = (position) => position.length() < 90000;

const projected = new THREE.Vector3();
function inFrame(position) {
  projected.copy(position).project(camera);
  return projected.z < 1 && Math.abs(projected.x) < 1 && Math.abs(projected.y) < 1;
}

// Radians per screen pixel at the middle of the frame, through a lens
// with this field top to bottom.
const perPixel = (fov = camera.fov) => (2 * Math.tan(rad(fov) / 2)) / stage.clientHeight;

// Only the figures in the opening view are drawn. The rest of the sky's
// are behind or beside the camera, and a line with one end behind the
// camera is a streak across the screen once the camera moves.
function buildFigures() {
  const ends = [];
  for (const pairs of Object.values(catalog.lines)) {
    const positions = pairs.map(starPosition);
    const inside = positions.filter(inFrame).length;
    if (!inside || inside < positions.length / 2) continue;
    for (let i = 0; i < positions.length; i += 2) {
      if (inReach(positions[i]) && inReach(positions[i + 1])) ends.push(...positions[i], ...positions[i + 1]);
    }
  }
  figures = segments(ends, lineMaterial);
}

// A line from Earth to each named star: the photo flattened these onto
// one sky, and from the side they are the different lengths they are.
function buildSightlines(names) {
  const ends = [];
  for (const name of names) {
    const position = starPosition(catalog.names[name]);
    if (inReach(position)) ends.push(0, 0, 0, ...position);
  }
  sightlines = segments(ends, sightMaterial);

  const dots = new THREE.BufferGeometry();
  dots.setAttribute('position', new THREE.Float32BufferAttribute(
    names.flatMap((name) => [...starPosition(catalog.names[name])]), 3));
  const markers = new THREE.Points(dots, markerMaterial);
  markers.frustumCulled = false;
  scene.add(markers);
}

// ---- home: Earth and the Sun -------------------------------------------------

function iconLabel(text) {
  const el = document.createElement('div');
  el.className = 'label home';
  el.textContent = text;
  return el;
}

function glowTexture() {
  const size = 256, canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const context = canvas.getContext('2d');
  const glow = context.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  const disc = SUN_DISC / SUN_GLOW;
  // A disc with an edge to it, a little yellower toward the limb, and a
  // halo that is gone by the sprite's rim.
  // Yellow rather than white: at this size a white disc is a hole burnt
  // in the picture, and Earth beside it cannot be seen for the glare.
  glow.addColorStop(0, 'rgba(255, 244, 214, 1)');
  glow.addColorStop(disc * 0.7, 'rgba(255, 232, 170, 1)');
  glow.addColorStop(disc * 0.97, 'rgba(255, 204, 110, 1)');
  glow.addColorStop(disc, 'rgba(255, 190, 90, 0.4)');
  glow.addColorStop(1, 'rgba(255, 160, 60, 0)');
  context.fillStyle = glow;
  context.fillRect(0, 0, size, size);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

let earth = null, sun = null;

// Earth as a ball under the photographer's feet, and the Sun beside it.
function buildHome() {
  const material = new THREE.ShaderMaterial({
    uniforms: { uSun: { value: new THREE.Vector3(1, 0, 0) }, uOpacity: { value: 0 } },
    vertexShader: `
      varying vec3 vNormal;
      varying vec3 vView;
      void main() {
        vec4 world = modelMatrix * vec4(position, 1.0);
        vNormal = normalize(mat3(modelMatrix) * normal);
        vView = normalize(cameraPosition - world.xyz);
        gl_Position = projectionMatrix * viewMatrix * world;
      }`,
    // Day and night sides from where the Sun is, and a rim of air. No map.
    // The night side is far lighter than night: black, it is a hole in the
    // stars rather than a planet.
    fragmentShader: `
      uniform vec3 uSun;
      uniform float uOpacity;
      varying vec3 vNormal;
      varying vec3 vView;
      void main() {
        vec3 n = normalize(vNormal);
        float day = smoothstep(-0.15, 0.3, dot(n, uSun));
        float facing = max(dot(n, normalize(vView)), 0.0);
        float rim = pow(1.0 - facing, 3.0);
        vec3 ground = mix(vec3(0.03, 0.075, 0.17), vec3(0.16, 0.38, 0.75) * (0.5 + 0.5 * facing), day);
        vec3 air = vec3(0.45, 0.7, 1.0) * rim * (0.3 + 0.7 * day);
        gl_FragColor = vec4(ground + air, uOpacity);
      }`,
    transparent: true, depthTest: false, depthWrite: false,
  });
  // A unit ball and a unit glow, sized and put in place by placeHome.
  const ball = new THREE.Mesh(new THREE.SphereGeometry(1, 64, 32), material);
  ball.frustumCulled = false;
  scene.add(ball);
  earth = { ball, material, radius: 1, tilt: 0, vantage: 1, el: iconLabel('Earth') };

  const glow = new THREE.Sprite(new THREE.SpriteMaterial({
    map: glowTexture(), transparent: true, opacity: 0, blending: THREE.AdditiveBlending,
    depthTest: false, depthWrite: false,
  }));
  glow.frustumCulled = false;
  scene.add(glow);
  sun = { glow, radius: 1, el: iconLabel('Sun') };
  placeHome();
}

// Size Earth and the Sun and put them where they go, for the screen as it
// is now: both depend on how far the camera backs away and through what
// lens, and those depend on the screen's shape.
function placeHome() {
  const forward = axis(0, 0, -1), up = axis(0, 1, 0), right = axis(1, 0, 0);
  // Which way was up is not known without a location. The ground is put
  // just under the bottom of the opening view: the camera stands on the
  // ball with the whole of it out of sight below, as the ground is in a
  // photo of the sky.
  const altitude = rad(THREE.MathUtils.clamp(state.homeFov / 2 + 3, 10, 80));
  const zenith = forward.clone().multiplyScalar(Math.sin(altitude)).addScaledVector(up, Math.cos(altitude));

  // Where the backing away ends, and which way the camera faces there.
  const lift = new THREE.Quaternion().setFromAxisAngle(right, -rad(TOUR_RISE_DEG));
  const pivot = forward.clone().multiplyScalar(state.pivot);
  const from = pivot.clone().multiplyScalar(-state.reach).applyQuaternion(lift).add(pivot);
  const ahead = forward.clone().applyQuaternion(lift), above = up.clone().applyQuaternion(lift);

  const pixels = Math.max(EARTH_MIN_PX, EARTH_SIZE * Math.min(stage.clientWidth, stage.clientHeight));
  const radius = pixels * perPixel(state.outFov) * from.length();
  const centre = zenith.clone().multiplyScalar(-radius);   // the photographer, at the origin, is the top of the ball
  // The Sun goes where it shows in that shot: on the far side of Earth
  // from the photo, which is where it is at night, and off to one side so
  // that neither hides the other. Where it really was is not used: at
  // this scale that is a matter of a few millionths of a pixel.
  const toSun = right.clone().multiplyScalar(0.9).addScaledVector(above, 0.12).addScaledVector(ahead, -0.42).normalize();
  const toEarth = centre.clone().sub(from);

  earth.radius = radius;
  earth.vantage = toEarth.length();
  // How far the camera has to be tipped down for Earth to sit where it
  // should in that shot.
  earth.tilt = Math.atan2(-toEarth.dot(above), toEarth.dot(ahead))
    - Math.atan((2 * EARTH_AT_HEIGHT - 1) * Math.tan(rad(state.outFov) / 2));
  earth.ball.position.copy(centre);
  earth.ball.scale.setScalar(radius);
  earth.material.uniforms.uSun.value.copy(toSun);
  sun.radius = SUN_DISC * radius;
  sun.glow.position.copy(centre).addScaledVector(toSun, SUN_AWAY * radius);
  sun.glow.scale.setScalar(2 * SUN_GLOW * radius);
}

// ---- labels ----------------------------------------------------------------

function setLabels(names) {
  labelLayer.replaceChildren();
  state.labels = [];
  for (const name of names) {
    const index = catalog.names[name];
    // With no Earth to draw, home is the origin with a ring round it.
    const home = name === 'Sun';
    const position = home ? new THREE.Vector3() : starPosition(index);
    const el = document.createElement('div');
    el.className = home ? 'label home ring' : 'label';
    el.textContent = home ? 'Earth' : name;
    if (!home && hasDistance(position)) {
      const ly = document.createElement('small');
      ly.textContent = `${Math.round(position.length() * PC_TO_LY)} ly`;
      el.append(ly);
    }
    labelLayer.append(el);
    state.labels.push({ el, index, position, absmag: catalog.absmag[index], width: el.offsetWidth, home });
  }
  // Brightest first: when two names would overprint, the brighter star's
  // is the one that stays.
  state.labels.sort((a, b) => a.index - b.index);
  for (const icon of [earth, sun]) {
    if (!icon) continue;
    labelLayer.append(icon.el);
    icon.width = icon.el.offsetWidth;
  }
}

const LABEL_HEIGHT = 15;

// Earth's and the Sun's names sit under them.
function placeIconLabel(icon, centre, opacity, show, w, h, placed) {
  const framed = inFrame(centre);
  const size = icon.radius / centre.distanceTo(camera.position) / perPixel();
  if (!show || !framed || opacity < 0.5) { icon.el.style.display = 'none'; return; }
  const x = (projected.x + 1) * 0.5 * w - icon.width / 2, y = (1 - projected.y) * 0.5 * h + size + 3;
  placed.push([x, y, icon.width]);
  icon.el.style.display = '';
  icon.el.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)`;
}

function placeLabels() {
  const show = $('showlabels').checked;
  const w = stage.clientWidth, h = stage.clientHeight;
  const limit = starMaterial.uniforms.uMagLimit.value + starMaterial.uniforms.uGain.value;
  const placed = [];
  if (earth) placeIconLabel(earth, earth.ball.position, earth.material.uniforms.uOpacity.value, show, w, h, placed);
  if (sun) placeIconLabel(sun, sun.glow.position, sun.glow.material.opacity, show, w, h, placed);
  for (const { el, position, absmag, width, home } of state.labels) {
    const d = position.distanceTo(camera.position);
    // Away from home every named star is marked, since it has a dot
    // whether or not it is bright. At home the names wait for a distance
    // at which any of them could have moved.
    const visible = show && d > 1e-3 && (home || state.out > 0.02 || absmag + 5 * Math.log10(d) - 5 < limit);
    const framed = inFrame(position);
    const x = (projected.x + 1) * 0.5 * w + 7, y = (1 - projected.y) * 0.5 * h - 9;
    const clear = placed.every(([px, py, pw]) =>
      x > px + pw || x + width < px || Math.abs(y - py) > LABEL_HEIGHT);
    if (!visible || !framed || !clear) { el.style.display = 'none'; continue; }
    placed.push([x, y, width]);
    el.style.display = '';
    el.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)`;
  }
}

// ---- camera ----------------------------------------------------------------

// Looking at (ra, dec) from Earth with north up.
function skyQuaternion(raDeg, decDeg) {
  const ra = rad(raDeg), dec = rad(decDeg);
  const forward = new THREE.Vector3(Math.cos(dec) * Math.cos(ra), Math.cos(dec) * Math.sin(ra), Math.sin(dec));
  const north = new THREE.Vector3(0, 0, 1);
  const up = north.addScaledVector(forward, -north.dot(forward)).normalize();
  const right = new THREE.Vector3().crossVectors(forward, up);
  const basis = new THREE.Matrix4().makeBasis(right, up, forward.clone().negate());
  return new THREE.Quaternion().setFromRotationMatrix(basis);
}

// A direction given in the photo's own axes (x right, y up, -z into the
// picture), in the scene's.
const axis = (x, y, z) => new THREE.Vector3(x, y, z).applyQuaternion(state.base);

// The point the camera circles: straight ahead of the photo, as far out as
// its typical named star. And how much around that point has to fit in
// view from outside: the nearer half or so of those stars, and Earth. The
// far supergiants are left to run off the edge: sized to hold them,
// everything near home shrinks to a knot at Earth.
function choosePivot(names) {
  const forward = axis(0, 0, -1);
  const positions = names.map((name) => starPosition(catalog.names[name])).filter(hasDistance);
  const depths = positions.map((p) => p.dot(forward)).filter((d) => d > 1).sort((a, b) => a - b);
  if (!depths.length) return;
  state.pivot = depths[Math.floor(depths.length / 2)];
  const pivot = forward.multiplyScalar(state.pivot);
  const spread = positions.map((p) => p.distanceTo(pivot)).sort((a, b) => a - b);
  state.spread = Math.max(spread[Math.floor(spread.length * 0.6)], state.pivot);
  state.far = 3 * state.spread;
}

// How far back the camera has to stand, through the lens it has outside,
// for that much to fit across the screen's narrower side, and for the
// photo to fit with room round it.
function chooseReach() {
  const tanV = Math.tan(rad(state.outFov) / 2), tanH = tanV * camera.aspect;
  const stars = (1.15 * state.spread) / Math.min(tanV, tanH) / state.pivot;
  const sheetV = Math.tan(rad(state.vfov) / 2), sheetH = sheetV * (state.aspect || 0);
  const sheet = state.aspect ? Math.max(sheetV / tanV, sheetH / tanH) / PHOTO_FILLS : 0;
  state.reach = Math.max(stars, sheet, 2);
}

// The field, top to bottom, of the lens used outside, for a screen of
// this shape whose lens at home has this field.
function outsideFov(homeFov, aspect) {
  const tanHalf = (degrees) => Math.tan(rad(degrees) / 2);
  // The screen's narrower side as a fraction of its height, and the wider
  // as a multiple of the narrower.
  const narrow = Math.min(1, aspect), wider = Math.max(aspect, 1 / aspect);
  const field = Math.min(
    THREE.MathUtils.clamp(tanHalf(homeFov) * narrow, tanHalf(OUT_FOV_MIN), tanHalf(OUT_FOV_MAX)),
    tanHalf(OUT_FOV_WIDEST) / wider);
  return THREE.MathUtils.radToDeg(2 * Math.atan(field / narrow));
}

// Whether a star is close enough to the circled point to draw lines to.
// One far outside it is off screen for the whole lap, and its line is a
// long stroke leading the eye out of the picture.
function inReach(position) {
  const pivot = axis(0, 0, -1).multiplyScalar(state.pivot);
  return hasDistance(position) && position.distanceTo(pivot) < state.far;
}

function describeDistance(pc) {
  const ly = pc * PC_TO_LY;
  return `${ly < 10 ? ly.toFixed(2) : ly < 100 ? ly.toFixed(1) : Math.round(ly).toLocaleString()} light-years from Earth`;
}

// Where the tour has the camera at u: how far backed away (0 to 1) and how
// many degrees around.
function tourPose(u) {
  let t = u * TOUR_SECONDS;
  if (t < TOUR.hold) return { out: 0, around: 0, phase: 'at Earth' };
  t -= TOUR.hold;
  if (t < TOUR.pull) return { out: t / TOUR.pull, around: 0, phase: 'backing away' };
  t -= TOUR.pull;
  if (t < TOUR.orbit) {
    const around = (t / TOUR.orbit) * 360;
    return { out: 1, around, phase: `${Math.round(around)}° around` };
  }
  t -= TOUR.orbit;
  return { out: Math.max(0, 1 - t / TOUR.pull), around: 0, phase: 'heading home' };
}

function updateCamera() {
  const pose = tourPose(state.u);
  state.out = pose.out;
  // Backing off multiplies the distance by a constant amount per step, so
  // the scene shrinks at a steady rate. The rise is over by the time the
  // backing off is half done: up off the ground first, then away.
  const eased = smooth(pose.out);
  const rise = smooth(Math.min(1, 2 * pose.out));
  const around = rad(pose.around + state.swing);
  const over = rad(THREE.MathUtils.clamp(state.over - TOUR_RISE_DEG * rise, -85, 85));
  const pivot = axis(0, 0, -1).multiplyScalar(state.pivot);
  const swing = new THREE.Quaternion().setFromAxisAngle(axis(0, 1, 0), around)
    .multiply(new THREE.Quaternion().setFromAxisAngle(axis(1, 0, 0), over));
  // The camera is always turned to the point it circles. Not out, not
  // swung and not zoomed, this is Earth, facing as the photo did.
  const stand = state.reach ** eased * state.zoom;
  // The lens goes from the photo's to the one for outside as it does.
  camera.fov = THREE.MathUtils.radToDeg(2 * Math.atan(THREE.MathUtils.lerp(
    Math.tan(rad(state.homeFov) / 2), Math.tan(rad(state.outFov) / 2), eased)));
  camera.updateProjectionMatrix();
  camera.position.copy(pivot).multiplyScalar(-stand).applyQuaternion(swing).add(pivot);
  camera.quaternion.copy(swing).multiply(state.base);
  if (earth) {
    // Tipped down as it backs away, so that home ends up framed under the
    // photo. With the backing away rather than the rise: while the photo
    // is still most of the screen, tipping pushes its top off the edge.
    camera.quaternion.multiply(new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), -earth.tilt * eased));
    // Dragging and the wheel can ask for a place underground. The tour's
    // own path never does.
    const from = camera.position.clone().sub(earth.ball.position);
    if (from.length() < earth.radius * 0.999) camera.position.copy(earth.ball.position).add(from.setLength(earth.radius));
  }
  camera.updateMatrixWorld();
  // Full compensation would show every star as if from Earth's distance to
  // the pivot, and from outside the camera sees the whole catalog at once:
  // a blizzard. Holding some back leaves the luminous stars as a backdrop,
  // and the named ones get their own dots, since a nearby dwarf like
  // Sirius is nothing at this range.
  starMaterial.uniforms.uGain.value = Math.max(0, 5 * Math.log10(Math.max(1, stand)) - HOLD_BACK_MAG * eased);
  markerMaterial.opacity = eased;
  // The lines from home all meet at the camera when it is home: a burst
  // in mid-frame. They come in with the distance.
  const drawn = Math.min(1, eased * 2.5);
  sightMaterial.opacity = 0.16 * drawn;

  if (earth) {
    const there = THREE.MathUtils.smoothstep(
      camera.position.distanceTo(earth.ball.position) / earth.vantage, HOME_FADE_FROM, HOME_FADE_TO);
    earth.material.uniforms.uOpacity.value = there;
    sun.glow.material.opacity = there;
    // Nothing here is depth-tested; the nearer of the two is drawn last.
    const sunNearer = camera.position.distanceTo(sun.glow.position) < camera.position.distanceTo(earth.ball.position);
    earth.ball.renderOrder = sunNearer ? 1 : 2;
    sun.glow.renderOrder = sunNearer ? 2 : 1;
  }
  const moved = camera.position.length();
  const nudged = state.swing !== 0 || state.over !== 0 || state.zoom !== 1;
  if (photo) {
    // How square-on the camera is to the sheet: 1 facing it from Earth's
    // side, 0 edge-on, negative behind.
    const facing = camera.position.clone().sub(pivot).normalize().dot(axis(0, 0, 1));
    const clear = THREE.MathUtils.smoothstep(facing, Math.cos(rad(PHOTO_GONE_DEG)), Math.cos(rad(PHOTO_CLEAR_DEG)));
    photo.sheet.material.opacity = clear * THREE.MathUtils.lerp(1, PHOTO_FAR_OPACITY, eased);
    // The outline and the edges from Earth to its corners stay when the
    // picture has faded: they are what says the photo is Earth's view.
    photo.frame.material.opacity = THREE.MathUtils.lerp(0.3, 0.6, drawn);
    photo.edges.material.opacity = 0.45 * drawn;
  }
  $('where').textContent = moved < 1e-9 ? 'at Earth' : `${pose.phase} · ${describeDistance(moved)}`;
  $('home').hidden = state.u === 0 && !nudged;
}

function layout() {
  if (!renderer) return;
  const w = innerWidth, h = innerHeight;
  // From Earth the photo sits whole in the window and the sky carries on
  // around it: the camera's field is widened until the photo's own field
  // spans the largest rectangle of its shape the window holds.
  let ph = h;
  if (state.aspect && w / h < state.aspect) ph = w / state.aspect;
  const ratio = Math.min(devicePixelRatio, 2);
  renderer.setPixelRatio(ratio);
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  state.homeFov = THREE.MathUtils.radToDeg(2 * Math.atan(Math.tan(rad(state.vfov) / 2) * (h / ph)));
  state.outFov = outsideFov(state.homeFov, camera.aspect);
  camera.fov = state.homeFov;
  camera.updateProjectionMatrix();
  // How far the camera backs away, and with it the size and place of
  // Earth and the Sun, are worked out for the screen's shape.
  chooseReach();
  if (earth) placeHome();
  // Stars are sized in pixels; keep them in proportion on a small window.
  starMaterial.uniforms.uPx.value = ratio * THREE.MathUtils.clamp(h / 900, 0.6, 1.4);
  state.dirty = true;
}

// ---- a solve from asterism --------------------------------------------------

let photo = null, fit = null;

// The photo as a thing in space: a sheet square to the line of sight, out
// at the point the camera circles, sized so that from Earth it covers
// exactly the field the camera had. Every star's line from Earth then
// passes through the sheet at that star's place in the picture, and the
// four lines from Earth to its corners are the edges of what the camera
// saw.
function buildPhoto(texture) {
  const height = 2 * state.pivot * Math.tan(rad(state.vfov) / 2);
  const width = height * state.aspect;
  texture.colorSpace = THREE.SRGBColorSpace;
  // Drawn first and as itself, with the catalog's stars, the figures and
  // the lines laid over it. Adding it to the scene instead looked fine for
  // a dark sky and turned a hazy or moonlit one into a white slab.
  const sheet = new THREE.Mesh(new THREE.PlaneGeometry(width, height), new THREE.MeshBasicMaterial({
    map: texture, transparent: true, depthTest: false, depthWrite: false,
  }));
  const corners = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([x, y]) => [(x * width) / 2, (y * height) / 2, 0]);
  const outline = new THREE.BufferGeometry();
  outline.setAttribute('position', new THREE.Float32BufferAttribute(corners.flat(), 3));
  const accent = () => new THREE.LineBasicMaterial({ color: 0xffb86b, transparent: true, opacity: 0, depthTest: false });
  const frame = new THREE.LineLoop(outline, accent());
  // In the sheet's own axes Earth is straight behind its middle.
  const spokes = new THREE.BufferGeometry();
  spokes.setAttribute('position', new THREE.Float32BufferAttribute(
    corners.flatMap((corner) => [0, 0, state.pivot, ...corner]), 3));
  const edges = new THREE.LineSegments(spokes, accent());
  for (const object of [sheet, frame, edges]) {
    object.position.copy(axis(0, 0, -1).multiplyScalar(state.pivot));
    object.quaternion.copy(state.base);
    object.renderOrder = -1;
    object.frustumCulled = false;
    scene.add(object);
  }
  photo = { sheet, frame, edges, width, height };
}

// The photo as a texture, scaled down if it is bigger than a GPU is sure
// to take.
async function loadPhoto(src) {
  const image = new Image();
  image.src = src;
  await image.decode().catch(() => { throw new Error('the photo is no longer available'); });
  const scale = Math.min(1, TEXTURE_MAX / Math.max(image.naturalWidth, image.naturalHeight));
  let source = image;
  if (scale < 1) {
    source = document.createElement('canvas');
    source.width = Math.round(image.naturalWidth * scale);
    source.height = Math.round(image.naturalHeight * scale);
    source.getContext('2d').drawImage(image, 0, 0, source.width, source.height);
  }
  const texture = new THREE.Texture(source);
  texture.needsUpdate = true;
  return { texture, width: image.naturalWidth, height: image.naturalHeight };
}

async function loadJob(id) {
  const response = await fetch(`/jobs/${id}`);
  if (response.status === 404) throw new Error('asterism no longer has that solve');
  if (!response.ok) throw new Error(`asterism answered ${response.status}`);
  const job = await response.json();
  const labels = job.result?.labels;
  if (job.status !== 'done' || !labels) throw new Error('that photo has no solve to start from');

  const stars = labels.filter((l) => l.kind === 'star' && l.name in catalog.names);
  const picture = await loadPhoto(`/jobs/${id}/image`);
  const { texture } = picture;
  // Label positions are in the pixels the worker solved, which the job's
  // EXIF summary sizes.
  const width = job.exif?.width || picture.width;
  const height = job.exif?.height || picture.height;
  const pairs = stars.map((l) => ({
    dir: starPosition(catalog.names[l.name]).normalize().toArray(),
    px: [l.x, l.y],
  }));
  const fit = fitCamera(pairs, width, height);
  return { fit, texture, aspect: width / height, names: stars.map((l) => l.name) };
}

function link(href, text) {
  const a = document.createElement('a');
  a.href = href;
  a.textContent = text;
  return a;
}

async function start() {
  catalog = await loadCatalog();
  buildStars();

  // Job ids are uuid4 hex, as on the homepage; anything else is no job.
  const id = /^[0-9a-f]{32}$/.test(params.get('job') || '') ? params.get('job') : null;
  let names = null, solve = null, problem = null;
  if (id) {
    try {
      solve = await loadJob(id);
      ({ names, fit } = solve);
      state.aspect = solve.aspect;
      state.base.fromArray(solve.fit.quaternion);
      state.vfov = solve.fit.vfov;
      $('status').replaceChildren(link(`/?job=${id}`, '← the photo'),
        ` · ${names.length} of its stars, each at its measured distance`);
      // How well the camera was recovered is for whoever is debugging a
      // solve that looks misaligned, not for the page.
      $('status').title = `camera recovered from ${solve.fit.used} named stars, `
        + `agreeing with the photo to ${solve.fit.rmsPx.toFixed(1)} px`;
    } catch (e) {
      problem = e.message;
    }
  }
  if (!names) state.base.copy(skyQuaternion(DEMO.ra, DEMO.dec));

  // Stand at Earth facing as the photo did, to see what the opening view
  // holds before anything is built from it.
  layout();
  camera.position.set(0, 0, 0);
  camera.quaternion.copy(state.base);
  camera.updateMatrixWorld();
  if (!names) {
    // Sorted brightest first, so a small index is a star worth naming.
    names = catalog.proper.filter((name) =>
      catalog.names[name] < 400 && name !== 'Sun' && inFrame(starPosition(catalog.names[name])));
    if (problem) {
      $('status').replaceChildren(`Could not start from that photo: ${problem}. Here is Orion instead. `,
        link(id ? `/?job=${id}` : '/', '← back'));
    } else {
      $('status').replaceChildren('This is Orion. Solve a photo of your own on ', link('/', 'the front page'),
        ' to see its stars this way.');
    }
  }
  choosePivot(names);
  chooseReach();
  if (solve) {
    buildPhoto(solve.texture);
    buildHome();
  }
  buildFigures();
  buildSightlines(names);
  // The catalog's "Sun" is home; with an Earth drawn it needs no marker.
  setLabels(earth ? names : ['Sun', ...names]);

  const number = (key, fallback) => (params.has(key) ? Number(params.get(key)) || 0 : fallback);
  setView(number('u', 0), number('swing', 0), number('over', 0), number('zoom', 1));
  // A link to a particular moment stays on it; otherwise the tour runs.
  setPlaying(!params.has('u'));
  requestAnimationFrame(frame);
}

// ---- input -----------------------------------------------------------------

function setView(u, swing, over, zoom) {
  state.u = THREE.MathUtils.clamp(u, 0, 1);
  // Kept within half a turn either way, so letting go of it takes the
  // short way back.
  state.swing = ((swing % 360) + 540) % 360 - 180;
  state.over = THREE.MathUtils.clamp(over, -85, 85);
  state.zoom = THREE.MathUtils.clamp(zoom, 0.05, 8);
  $('trip').value = state.u;
  state.dirty = true;
}

function setPlaying(playing) {
  state.playing = playing;
  $('play').textContent = playing ? 'Pause' : 'Play';
}

$('trip').addEventListener('input', (e) => {
  setPlaying(false);
  setView(Number(e.target.value), state.swing, state.over, state.zoom);
});
$('play').addEventListener('click', () => setPlaying(!state.playing));
$('home').addEventListener('click', () => { setPlaying(false); setView(0, 0, 0, 1); });
$('showlines').addEventListener('change', (e) => { figures.visible = e.target.checked; state.dirty = true; });
$('showsight').addEventListener('change', (e) => { sightlines.visible = e.target.checked; state.dirty = true; });
$('showlabels').addEventListener('change', () => { state.dirty = true; });
$('maglimit').addEventListener('input', (e) => {
  starMaterial.uniforms.uMagLimit.value = Number(e.target.value);
  $('magvalue').textContent = Number(e.target.value).toFixed(1);
  state.dirty = true;
});

// Scrolling moves the camera toward or away from the point it circles.
stage.addEventListener('wheel', (e) => {
  e.preventDefault();
  setView(state.u, state.swing, state.over, state.zoom * Math.exp(e.deltaY * 0.001));
}, { passive: false });

const DRAG_DEG_PER_PIXEL = 0.25;
const touches = new Map();   // pointers down on the scene: id -> where each last was
let pinch = 0;               // how far apart they were, when there are two
const apart = () => {
  const [a, b] = [...touches.values()];
  return Math.hypot(a.x - b.x, a.y - b.y);
};
stage.addEventListener('pointerdown', (e) => {
  touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
  stage.setPointerCapture(e.pointerId);
  stage.classList.add('dragging');
  setPlaying(false);
  if (touches.size === 2) pinch = apart();
});
stage.addEventListener('pointermove', (e) => {
  const was = touches.get(e.pointerId);
  if (!was) return;
  touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (touches.size === 2) {
    // Two fingers: spreading them brings the stars closer, as it would a
    // map. A phone has no wheel.
    const now = apart();
    if (pinch > 0 && now > 0) setView(state.u, state.swing, state.over, (state.zoom * pinch) / now);
    pinch = now;
    return;
  }
  if (touches.size > 1) return;
  // Dragging pulls the stars with the pointer, so the camera goes the
  // other way around them.
  setView(state.u, state.swing - (e.clientX - was.x) * DRAG_DEG_PER_PIXEL,
    state.over - (e.clientY - was.y) * DRAG_DEG_PER_PIXEL, state.zoom);
});
for (const type of ['pointerup', 'pointercancel']) {
  stage.addEventListener(type, (e) => {
    touches.delete(e.pointerId);
    if (!touches.size) stage.classList.remove('dragging');
  });
}
addEventListener('resize', layout);

let last = 0;
function frame(now) {
  requestAnimationFrame(frame);
  const dt = Math.min((now - last) / 1000, 0.1);
  last = now;
  if (state.playing) {
    // Whatever dragging and the wheel added is let go of as the tour runs,
    // so that it ends on the photo however it was left.
    const keep = Math.exp(-dt * 2.5);
    const settle = (value, rest) => (Math.abs(value - rest) < 1e-3 ? rest : rest + (value - rest) * keep);
    setView((state.u + (dt * SPEED) / TOUR_SECONDS) % 1, settle(state.swing, 0), settle(state.over, 0),
      Math.exp(settle(Math.log(state.zoom), 0)));
  }
  if (!state.dirty) return;
  state.dirty = false;
  updateCamera();
  renderer.render(scene, camera);
  placeLabels();
}

// For the browser tests and the console: where things are, read-only by
// convention. `screen` gives a point's place on the page in pixels, and
// whether it is in front of the camera.
window.fly = {
  state, camera, tour: TOUR, tourSeconds: TOUR_SECONDS,
  parts: () => ({ photo, earth, sun }),
  fit: () => fit,
  screen: (point) => {
    const p = point.clone().project(camera);
    return { x: (p.x + 1) * 0.5 * stage.clientWidth, y: (1 - p.y) * 0.5 * stage.clientHeight, ahead: p.z < 1 };
  },
};

if (renderer) {
  start().catch((e) => { $('status').textContent = `Could not start: ${e.message}`; });
} else {
  const id = /^[0-9a-f]{32}$/.test(params.get('job') || '') ? params.get('job') : null;
  $('controls').hidden = true;
  $('status').replaceChildren(
    'This needs WebGL 2, and this browser has none to give: hardware acceleration may be turned off in its settings, '
      + 'or it may not trust the graphics driver. ',
    link(id ? `/?job=${id}` : '/', '← back'));
}
