// The fly-around page, driven in a real browser. What these pin down is
// mostly how the camera leaves Earth and what the shot it ends on holds:
// that took several tries to get right (README, "From outside"), and each
// wrong version looked fine in stills.
import { expect, test } from '@playwright/test';
import { JOB_ID, LANDSCAPE, POSE, WIDE, serveFixture } from '../fixture.js';

// What the page's handle says about where things are, in the scene and on
// the screen.
const read = (page) => page.evaluate(() => {
  const { state, camera, parts, screen } = window.fly;
  const { photo, earth, sun } = parts();
  const perPixel = (2 * Math.tan((camera.fov * Math.PI) / 360)) / innerHeight;
  const onScreen = (p) => p.ahead && p.x >= 0 && p.x <= innerWidth && p.y >= 0 && p.y <= innerHeight;
  const earthAt = screen(earth.ball.position), sunAt = screen(sun.glow.position);
  const earthDistance = camera.position.distanceTo(earth.ball.position);
  const earthRadius = earth.radius / earthDistance / perPixel;
  // How far from round Earth is drawn: its height over its width on
  // screen, taken from its top, bottom and sides as the camera sees them.
  const step = (x, y) => screen(earth.ball.position.clone()
    .add(camera.position.clone().set(x, y, 0).applyQuaternion(camera.quaternion).multiplyScalar(earth.radius)));
  const [l, r, t, b] = [step(-1, 0), step(1, 0), step(0, 1), step(0, -1)];
  const sunSize = sun.glow.scale.x / 2 / camera.position.distanceTo(sun.glow.position) / perPixel;
  // The photo's top and bottom edges, through its middle.
  const edge = (y) => screen(photo.sheet.localToWorld(camera.position.clone().set(0, y, 0)));
  const top = edge(photo.height / 2), bottom = edge(-photo.height / 2);
  return {
    u: state.u, swing: state.swing, over: state.over, zoom: state.zoom, playing: state.playing,
    distance: camera.position.length(),
    fov: camera.fov,
    width: innerWidth, height: innerHeight,
    photo: photo.sheet.material.opacity,
    frame: {
      height: Math.hypot(top.x - bottom.x, top.y - bottom.y),
      onScreen: onScreen(top) && onScreen(bottom) && [[-1, -1], [1, -1], [1, 1], [-1, 1]].every(([x, y]) =>
        onScreen(screen(photo.sheet.localToWorld(camera.position.clone().set((x * photo.width) / 2, (y * photo.height) / 2, 0))))),
      bottom: bottom.y,
    },
    earth: {
      opacity: earth.material.uniforms.uOpacity.value,
      x: earthAt.x, y: earthAt.y,
      radius: earthRadius,                         // on screen, in pixels
      clearance: earthDistance / earth.radius,     // 1 is standing on it
      onScreen: onScreen(earthAt),
      // Whether any of the disc is in the picture.
      showing: earthAt.ahead && earthAt.x > -earthRadius && earthAt.x < innerWidth + earthRadius
        && earthAt.y > -earthRadius && earthAt.y < innerHeight + earthRadius,
      squash: Math.hypot(t.x - b.x, t.y - b.y) / Math.hypot(r.x - l.x, r.y - l.y),
    },
    sun: {
      opacity: sun.glow.material.opacity, x: sunAt.x, y: sunAt.y, onScreen: onScreen(sunAt),
      // Whether the whole of its glow is in the picture.
      whole: sunAt.ahead && sunAt.x - sunSize > 0 && sunAt.x + sunSize < innerWidth
        && sunAt.y - sunSize > 0 && sunAt.y + sunSize < innerHeight,
    },
  };
});

// The page moves the camera when it next draws, not when it is told to.
const nextFrame = (page) =>
  page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));

async function seek(page, u) {
  await page.locator('#trip').evaluate((el, value) => {
    el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }, u);
  await nextFrame(page);
}

// Open paused at the start of the tour. Returns the tour's timings, and a
// way to go to a moment of it given in seconds.
async function open(page, extra = '', pose = POSE) {
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  await serveFixture(page, pose);
  await page.goto(`/fly?job=${JOB_ID}&u=0${extra}`);
  await expect(page.locator('#status')).toContainText('of its stars');
  const { tour, total } = await page.evaluate(() => ({ tour: window.fly.tour, total: window.fly.tourSeconds }));
  return { errors, tour, total, at: (seconds) => seek(page, seconds / total) };
}

const earthLabel = (page) => page.locator('#labels .label.home', { hasText: /^Earth$/ });
const sunLabel = (page) => page.locator('#labels .label.home', { hasText: /^Sun$/ });

test('opens on the photo and nothing else, with the camera where the fixture put it', async ({ page }) => {
  const { errors } = await open(page);
  const now = await read(page);
  expect(now.distance).toBe(0);
  expect(now.photo).toBe(1);
  // The fixture's labels are exactly where a pinhole puts them, so the
  // camera the page recovers should agree with them to a fraction of a
  // pixel, using every one.
  const fit = await page.evaluate(() => window.fly.fit());
  expect(fit.rmsPx).toBeLessThan(0.5);
  expect(fit.rejected).toBe(0);
  await expect(page.locator('#status a')).toHaveAttribute('href', `/?job=${JOB_ID}`);
  // The window is wider than the photo, so the camera's field is the
  // photo's own: the fit found the fixture's focal length.
  expect(now.fov).toBeCloseTo(POSE.vfov, 1);
  expect(now.frame.height).toBeCloseTo(now.height, 0);
  await expect(page.locator('#where')).toHaveText('at Earth');
  // Home is underfoot: not drawn, and not in the picture if it were.
  expect(now.earth.opacity).toBe(0);
  expect(now.sun.opacity).toBe(0);
  expect(now.earth.showing).toBe(false);
  expect(now.earth.clearance).toBeCloseTo(1, 6);
  await expect(earthLabel(page)).toBeHidden();
  await expect(sunLabel(page)).toBeHidden();
  expect(errors).toEqual([]);
});

test('backing away is one move: the photo recedes, and Earth is there below it without ballooning', async ({ page }) => {
  const { tour, at } = await open(page);
  const start = await read(page);
  const steps = 28, seen = [];
  for (let i = 1; i <= steps; i++) {
    await at(tour.hold + (tour.pull * i) / steps);
    seen.push(await read(page));
  }
  const end = seen.at(-1);

  // The camera never goes through the planet.
  for (const now of seen) expect(now.earth.clearance).toBeGreaterThanOrEqual(0.999);

  // The photo shrinks the whole way: the camera is seen to be leaving.
  let before = start.frame.height;
  for (const now of seen) {
    expect(now.frame.height).toBeLessThan(before + 0.5);
    before = now.frame.height;
  }

  // Whenever Earth can be seen at all it is low in the picture, never in
  // the middle where its own photo is, and never much bigger than it ends
  // up: it does not come out of the camera and shrink into the distance.
  const visible = seen.filter((now) => now.earth.showing && now.earth.opacity > 0.05);
  expect(visible.length).toBeGreaterThan(steps / 4);
  for (const now of visible) {
    expect(now.earth.y).toBeGreaterThan(now.height * 0.55);
    expect(now.earth.radius).toBeLessThan(end.earth.radius * 2.4);
  }
  // By the time it is there, the photo has visibly receded.
  expect(visible[0].frame.height).toBeLessThan(start.frame.height * 0.85);

  // Where it ends: behind Earth, with Earth, the Sun and the whole photo
  // in one shot, Earth clear of the controls and the Sun beside it.
  expect(end.earth.opacity).toBe(1);
  expect(end.sun.opacity).toBe(1);
  expect(end.earth.onScreen).toBe(true);
  expect(end.sun.onScreen).toBe(true);
  expect(end.frame.onScreen).toBe(true);
  expect(end.photo).toBeGreaterThan(0.6);
  expect(end.earth.radius).toBeGreaterThan(30);
  expect(end.earth.radius).toBeLessThan(38);
  expect(end.earth.squash).toBeGreaterThan(0.9);
  expect(end.earth.squash).toBeLessThan(1.15);
  expect(end.sun.whole).toBe(true);
  expect(end.earth.y / end.height).toBeCloseTo(0.66, 1);
  expect(end.earth.x / end.width).toBeCloseTo(0.5, 1);
  expect(end.sun.y).toBeLessThan(end.height * 0.8);
  expect(Math.abs(end.sun.x - end.earth.x)).toBeGreaterThan(end.earth.radius * 1.5);
  await expect(earthLabel(page)).toBeVisible();
  await expect(sunLabel(page)).toBeVisible();
});

test('an ultrawide photo still fits in the shot from behind Earth', async ({ page }) => {
  const { tour, at } = await open(page, '', WIDE);
  const start = await read(page);
  expect(start.fov).toBeCloseTo(WIDE.vfov, 0);
  await at(tour.hold + tour.pull);
  const end = await read(page);
  expect(end.fov).toBeCloseTo(55, 0);
  expect(end.frame.onScreen).toBe(true);
  expect(end.frame.height).toBeLessThan(end.height * 0.6);
  expect(end.earth.onScreen).toBe(true);
  expect(end.sun.whole).toBe(true);
});

test('circling keeps Earth and the Sun in the shot', async ({ page }) => {
  const { tour, at } = await open(page);
  for (const turn of [0.1, 0.25, 0.5, 0.75, 0.9]) {
    await at(tour.hold + tour.pull + tour.orbit * turn);
    const now = await read(page);
    expect(now.earth.onScreen).toBe(true);
    expect(now.sun.onScreen).toBe(true);
    expect(now.earth.opacity).toBe(1);
    expect(now.distance).toBeGreaterThan(30);   // parsecs
  }
  await at(tour.hold + tour.pull + tour.orbit * 0.5);
  expect((await read(page)).photo).toBe(0);     // from behind, the picture is not drawn
});

test('the tour ends where it began', async ({ page }) => {
  const { total } = await open(page, '&speed=40');
  await page.locator('#play').click();
  // One lap at forty times speed is about a second. Watched from inside
  // the page, frame by frame: from outside, the last tenth of a lap is too
  // brief to be sure of catching.
  await page.waitForFunction(() => window.fly.state.u > 0.9);
  await page.waitForFunction((early) => window.fly.state.u < early, 2 / total);
  await page.locator('#play').click();
  await page.locator('#home').click();
  await nextFrame(page);
  const now = await read(page);
  expect(now.distance).toBe(0);
  expect(now.photo).toBe(1);
  expect(now.earth.opacity).toBe(0);
});

test('dragging mid-tour, then Play, settles back onto the tour', async ({ page }) => {
  const { tour, at } = await open(page);
  await at(tour.hold + tour.pull + tour.orbit * 0.3);
  const box = await page.locator('#stage').boundingBox();
  await page.mouse.move(box.x + 500, box.y + 300);
  await page.mouse.down();
  await page.mouse.move(box.x + 760, box.y + 380, { steps: 8 });
  await page.mouse.up();
  await page.mouse.wheel(0, 400);
  let now = await read(page);
  expect(Math.abs(now.swing)).toBeGreaterThan(30);
  expect(Math.abs(now.over)).toBeGreaterThan(10);
  expect(now.zoom).not.toBe(1);
  expect(now.playing).toBe(false);

  await page.locator('#play').click();
  await expect.poll(async () => {
    const later = await read(page);
    return [later.swing, later.over, later.zoom];
  }, { timeout: 15_000 }).toEqual([0, 0, 1]);
  now = await read(page);
  expect(now.playing).toBe(true);
});

// Most of asterism's photos come from phones, and so do most of its
// visitors. Upright, a phone's screen is far taller than any photo fitted
// across it, which at home makes the camera's field enormous.
test.describe('on a phone held upright', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 3 });

  test('the shot from behind Earth still holds Earth, the Sun and the photo, and Earth is round', async ({ page }) => {
    const { tour, at } = await open(page, '', LANDSCAPE);
    const start = await read(page);
    expect(start.fov).toBeGreaterThan(95);          // the fisheye this is about
    await at(tour.hold + tour.pull);
    const end = await read(page);
    expect(end.fov).toBeLessThanOrEqual(80.5);
    expect(end.earth.onScreen).toBe(true);
    expect(end.earth.squash).toBeGreaterThan(0.9);
    expect(end.earth.squash).toBeLessThan(1.15);
    expect(end.sun.whole).toBe(true);
    expect(end.frame.onScreen).toBe(true);
    expect(end.earth.y / end.height).toBeCloseTo(0.66, 1);
    expect(end.frame.height).toBeLessThan(start.frame.height);
    await expect(earthLabel(page)).toBeVisible();
    await expect(sunLabel(page)).toBeVisible();
  });

  test('turned on its side part-way round, the shot is framed again for the new shape', async ({ page }) => {
    const { tour, at } = await open(page, '', LANDSCAPE);
    await at(tour.hold + tour.pull);
    await page.setViewportSize({ width: 844, height: 390 });
    await nextFrame(page);
    const end = await read(page);
    expect(end.earth.onScreen).toBe(true);
    expect(end.sun.whole).toBe(true);
    expect(end.frame.onScreen).toBe(true);
    expect(end.earth.squash).toBeGreaterThan(0.9);
    expect(end.earth.squash).toBeLessThan(1.15);
    expect(end.earth.y / end.height).toBeCloseTo(0.66, 1);
  });
});

// The star file is asked for separately from its index and can fail on its
// own. Read as numbers, an error page is either an exception about array
// lengths or a sky of garbage; the page should say what happened instead.
test.describe('when the star file does not arrive', () => {
  const answer = (page, response) => page.route('**/fly/stars.bin*', (route) => route.fulfill(response));

  test('an error from the server is reported as the catalog being unavailable', async ({ page }) => {
    await serveFixture(page);
    await answer(page, { status: 503, contentType: 'application/json', body: '{"detail":"the star catalog has not been fetched"}' });
    await page.goto(`/fly?job=${JOB_ID}&u=0`);
    await expect(page.locator('#status')).toHaveText('Could not start: the star catalog is not available (503)');
  });

  test('a file cut short is reported as not having arrived whole', async ({ page }) => {
    await serveFixture(page);
    await answer(page, { status: 200, contentType: 'application/octet-stream', body: Buffer.alloc(4000) });
    await page.goto(`/fly?job=${JOB_ID}&u=0`);
    await expect(page.locator('#status')).toHaveText('Could not start: the star catalog did not arrive whole');
  });

  test('the status line is announced to a screen reader when it changes', async ({ page }) => {
    await serveFixture(page);
    await page.goto(`/fly?job=${JOB_ID}&u=0`);
    await expect(page.locator('#status')).toHaveAttribute('aria-live', 'polite');
  });
});

test.describe('with two fingers', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 3 });

  // Playwright's touchscreen only taps. The browser's own input channel
  // takes whole touch points, which reach the page as the pointer events a
  // real pinch makes.
  test('spreading them brings the stars closer, and pinching backs away', async ({ page }) => {
    const { tour, at } = await open(page, '', LANDSCAPE);
    await at(tour.hold + tour.pull + tour.orbit * 0.25);
    const cdp = await page.context().newCDPSession(page);
    const touch = (type, points) => cdp.send('Input.dispatchTouchEvent', {
      type, touchPoints: points.map(([x, y], id) => ({ x, y, id })),
    });
    const pinch = async (from, to) => {
      await touch('touchStart', [[195 - from, 400], [195 + from, 400]]);
      for (let i = 1; i <= 6; i++) {
        const half = from + ((to - from) * i) / 6;
        await touch('touchMove', [[195 - half, 400], [195 + half, 400]]);
      }
      await touch('touchEnd', []);
      await nextFrame(page);
    };

    const before = await read(page);
    await pinch(40, 120);
    const closer = await read(page);
    expect(closer.zoom).toBeLessThan(before.zoom * 0.5);
    expect(closer.swing).toBe(before.swing);        // a pinch is not a drag
    expect(closer.playing).toBe(false);

    await pinch(120, 40);
    const back = await read(page);
    expect(back.zoom).toBeCloseTo(before.zoom, 1);
  });
});
