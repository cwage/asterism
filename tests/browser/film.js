// Record the page as a video, to watch what a change did without anyone
// having to screen-record it.
//
//   film.js [job id] [seconds]
//
// With a job id it films that solve, which has to be one the web service
// this runs against knows. With none it films the tests' fixture. The video
// lands in out/film/, which compose maps to data/browser/film/.

import { chromium } from '@playwright/test';
import { JOB_ID, serveFixture } from './fixture.js';

const [job, seconds] = process.argv.slice(2);
const base = process.env.BASE_URL || 'http://web:8000';
// VIEWPORT=390x844 films it as a phone would show it.
const [width, height] = (process.env.VIEWPORT || '1280x800').split('x').map(Number);
const size = { width, height };
const phone = width < 600;

const browser = await chromium.launch({
  args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
});
const context = await browser.newContext({
  viewport: size, hasTouch: phone, isMobile: phone, recordVideo: { dir: 'out/film', size },
});
const page = await context.newPage();
if (!job) await serveFixture(page);
await page.goto(`${base}/fly?job=${job || JOB_ID}`);
await page.waitForFunction(() => window.fly?.state.playing, null, { timeout: 30_000 });
const lap = await page.evaluate(() => window.fly.tourSeconds);
await page.waitForTimeout(1000 * (Number(seconds) || lap + 2));
await context.close();
console.log(await page.video().path());
await browser.close();
