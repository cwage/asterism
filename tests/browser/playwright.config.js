import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  outputDir: './out/results',
  reporter: [['list']],
  timeout: 60_000,
  use: {
    baseURL: process.env.BASE_URL || 'http://web:8000',
    viewport: { width: 1280, height: 800 },
    // No GPU in the container: WebGL comes from SwiftShader, in software.
    launchOptions: { args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] },
    screenshot: 'only-on-failure',
  },
});
