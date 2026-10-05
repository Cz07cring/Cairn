import {defineConfig, devices} from '@playwright/test';

const baseURL = process.env.RING_WEB_PREVIEW_URL;
if (!baseURL) {
  throw new Error('RING_WEB_PREVIEW_URL 必须指向已经启动的只读驾驶舱');
}

export default defineConfig({
  testDir: './e2e',
  testMatch: 'readonly-preview.spec.ts',
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  use: {baseURL, trace: 'retain-on-failure', screenshot: 'only-on-failure'},
  projects: [{name: 'chromium-readonly-preview', use: {...devices['Desktop Chrome']}}],
});
