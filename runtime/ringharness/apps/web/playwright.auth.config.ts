import {createSign, generateKeyPairSync} from 'node:crypto';
import {defineConfig, devices} from '@playwright/test';

function base64url(value: string): string {
  return Buffer.from(value).toString('base64url');
}

function issueTestBearer(privateKey: string): string {
  const now = Math.floor(Date.now() / 1000);
  const header = base64url(JSON.stringify({alg: 'RS256', typ: 'JWT'}));
  const payload = base64url(JSON.stringify({
    sub: 'web-e2e-operator',
    roles: ['admin', 'operator', 'viewer', 'approver', 'ops'],
    project_ids: [],
    iss: 'ring-web-e2e',
    aud: 'ring-api',
    iat: now,
    exp: now + 600,
  }));
  const unsigned = `${header}.${payload}`;
  const signer = createSign('RSA-SHA256');
  signer.update(unsigned);
  signer.end();
  return `${unsigned}.${signer.sign(privateKey, 'base64url')}`;
}

if (!process.env.RING_WEB_E2E_DATABASE_URL) {
  throw new Error('RING_WEB_E2E_DATABASE_URL 必须指向已迁移的隔离测试数据库');
}

let bearer = process.env.RING_WEB_E2E_BEARER;
let publicKeyB64 = process.env.RING_WEB_E2E_JWT_PUBLIC_KEY_B64;
if (!bearer || !publicKeyB64) {
  const {privateKey, publicKey} = generateKeyPairSync('rsa', {
    modulusLength: 2048,
    publicKeyEncoding: {type: 'spki', format: 'pem'},
    privateKeyEncoding: {type: 'pkcs8', format: 'pem'},
  });
  bearer = `Bearer ${issueTestBearer(privateKey)}`;
  publicKeyB64 = Buffer.from(publicKey).toString('base64');
  // Playwright 会在 worker 中再次加载配置；继承同一对短期凭据，避免二次生成后公私钥错配。
  process.env.RING_WEB_E2E_BEARER = bearer;
  process.env.RING_WEB_E2E_JWT_PUBLIC_KEY_B64 = publicKeyB64;
}
const apiPort = process.env.RING_WEB_E2E_API_PORT || '58121';
const webPort = process.env.RING_WEB_E2E_WEB_PORT || '58114';
const apiUrl = `http://127.0.0.1:${apiPort}`;
const webUrl = `http://127.0.0.1:${webPort}`;

export default defineConfig({
  testDir: './e2e',
  testMatch: 'authenticated.spec.ts',
  outputDir: '../../.runtime/web-e2e-auth/artifacts',
  fullyParallel: false,
  workers: 1,
  reporter: [['list'], ['html', {outputFolder: '../../.runtime/web-e2e-auth/report', open: 'never'}]],
  use: {
    baseURL: webUrl,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [{name: 'chromium-authenticated', use: {...devices['Desktop Chrome']}}],
  webServer: [
    {
      command: 'uv run python e2e/support/control_server.py',
      url: `${apiUrl}/health/live`,
      timeout: 60_000,
      reuseExistingServer: false,
      env: {
        ...process.env,
        RING_WEB_E2E_JWT_PUBLIC_KEY_B64: publicKeyB64,
        RING_WEB_E2E_API_PORT: apiPort,
      },
    },
    {
      command: `pnpm dev --port ${webPort} --strictPort`,
      url: `${webUrl}/health/live`,
      timeout: 30_000,
      reuseExistingServer: false,
      env: {
        ...process.env,
        VITE_RING_CONTROL_URL: apiUrl,
        VITE_RING_DEV_BEARER: bearer,
      },
    },
  ],
});
