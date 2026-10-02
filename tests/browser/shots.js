// Stills of the same moments of the tour for several solves, to see how a
// change holds up across lenses and skies without filming each one.
//
//   shots.js <job id> [<job id> ...]
//
// For each solve, one PNG per moment in out/shots/ (data/browser/shots/ on
// the host), named <job>-<n>.png,
// and one line of JSON saying how the shot the backing away ends on came
// out: where Earth and the Sun landed, and how big the photo is in it.

import { chromium } from '@playwright/test';

const jobs = process.argv.slice(2);
const base = process.env.BASE_URL || 'http://web:8000';
// VIEWPORT=390x844 takes them as a phone would show it: that size, a
// touch screen, three device pixels to the CSS pixel.
const [width, height] = (process.env.VIEWPORT || '1280x800').split('x').map(Number);
const size = { width, height };
const phone = width < 600;

// Moments of the tour, as fractions of each phase: the photo; two points
// in the backing away; the shot it ends on; four points round the circle.
const moments = (tour) => [
  0,
  tour.hold + tour.pull * 0.55,
  tour.hold + tour.pull * 0.8,
  tour.hold + tour.pull,
  ...[0.12, 0.25, 0.5, 0.75].map((turn) => tour.hold + tour.pull + tour.orbit * turn),
];

const browser = await chromium.launch({
  args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
});
const page = await browser.newPage({ viewport: size, hasTouch: phone, isMobile: phone, deviceScaleFactor: phone ? 3 : 1 });
for (const job of jobs) {
  await page.goto(`${base}/fly?job=${job}&u=0`);
  await page.waitForFunction(() => /of its stars|Could not/.test(document.getElementById('status').textContent),
    null, { timeout: 60_000 });
  const status = await page.locator('#status').textContent();
  if (!status.includes('of its stars')) {
    console.log(JSON.stringify({ job, problem: status }));
    continue;
  }
  const { tour, total } = await page.evaluate(() => ({ tour: window.fly.tour, total: window.fly.tourSeconds }));
  let n = 0;
  for (const seconds of moments(tour)) {
    await page.locator('#trip').evaluate((el, value) => {
      el.value = value;
      el.dispatchEvent(new Event('input', { bubbles: true }));
    }, seconds / total);
    await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
    await page.screenshot({ path: `out/shots/${job.slice(0, 8)}-${n}.png` });
    if (n === 3) {
      console.log(JSON.stringify(await page.evaluate((id) => {
        const { state, camera, parts, screen } = window.fly;
        const { photo, earth, sun } = parts();
        const perPixel = (2 * Math.tan((camera.fov * Math.PI) / 360)) / innerHeight;
        const at = (p) => { const s = screen(p); return [Math.round(s.x), Math.round(s.y)]; };
        const edge = (y) => screen(photo.sheet.localToWorld(camera.position.clone().set(0, y, 0)));
        return {
          job: id.slice(0, 8),
          status: document.getElementById('status').title,
          fov: +camera.fov.toFixed(1), reach: +state.reach.toFixed(2), pivot_ly: Math.round(state.pivot * 3.26156),
          earth_ly: +(earth.radius * 3.26156).toFixed(1),
          earth_px: Math.round(earth.radius / camera.position.distanceTo(earth.ball.position) / perPixel),
          earth_at: at(earth.ball.position), sun_at: at(sun.glow.position),
          photo_y: [Math.round(edge(photo.height / 2).y), Math.round(edge(-photo.height / 2).y)],
          photo_opacity: +photo.sheet.material.opacity.toFixed(2),
        };
      }, job)));
    }
    n += 1;
  }
}
await browser.close();
