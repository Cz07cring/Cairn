import {defineConfig, devices} from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  testMatch: 'workbench.spec.ts',
  outputDir: '../../.runtime/web-e2e/artifacts',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [['list'], ['html', {outputFolder: '../../.runtime/web-e2e/report', open: 'never'}]],
  use: {
    baseURL: process.env.RING_WEB_E2E_URL || 'http://127.0.0.1:58114',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {name: 'chromium', use: {...devices['Desktop Chrome']}},
    {name: 'mobile', use: {...devices['Pixel 7']}},
  ],
  webServer: process.env.RING_WEB_E2E_EXTERNAL === '1' ? undefined : {
    command: 'pnpm dev --port 58114 --strictPort',
    url: 'http://127.0.0.1:58114/health/live',
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
