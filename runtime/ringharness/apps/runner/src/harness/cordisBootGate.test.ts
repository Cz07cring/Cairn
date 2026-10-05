import {mkdtempSync, rmSync, writeFileSync, mkdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {expect, test} from 'vitest';
import {existsSync} from 'node:fs';
import {bootPinnedCordis, cordisEntryPath, CORDIS_LIB_ENTRY} from './cordisBootGate.js';
import {DEEPSEEK_HARNESS_COMMIT} from './pin.js';
import {HARNESS_PIN_FILE} from './pinGate.js';

const checkout = process.env.RING_HARNESS_CHECKOUT;
const cordisBuilt =
  Boolean(checkout) && existsSync(cordisEntryPath(checkout as string));

test('未配置 checkout 时 boot 明确失败', async () => {
  const prev = process.env.RING_HARNESS_CHECKOUT;
  delete process.env.RING_HARNESS_CHECKOUT;
  try {
    await expect(bootPinnedCordis(undefined)).rejects.toThrow(/HARNESS_CHECKOUT_REQUIRED/);
  } finally {
    if (prev !== undefined) {
      process.env.RING_HARNESS_CHECKOUT = prev;
    }
  }
});

test('pin 对但未构建 Cordis 时失败关闭', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'ring-cordis-nobuild-'));
  try {
    writeFileSync(join(dir, HARNESS_PIN_FILE), `${DEEPSEEK_HARNESS_COMMIT}\n`);
    await expect(bootPinnedCordis(dir)).rejects.toThrow(/HARNESS_CORDIS_NOT_BUILT/);
  } finally {
    rmSync(dir, {recursive: true, force: true});
  }
});

test.skipIf(!cordisBuilt)('钉扎 checkout 可真实 import Cordis 并 new Context', async () => {
  const result = await bootPinnedCordis(checkout);
  expect(result.status).toBe('cordis-booted');
  expect(result.pin).toBe(DEEPSEEK_HARNESS_COMMIT);
  expect(result.packageName).toBe('@deepseek-ai/cordis');
  expect(result.entry.endsWith(CORDIS_LIB_ENTRY)).toBe(true);
  expect(result.exportKeys).toContain('Context');
  expect(typeof result.ctx.provide).toBe('function');
  expect(typeof result.ctx.get).toBe('function');
});

test('占位：无 lib 的假目录不能冒充 booted', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'ring-cordis-fake-'));
  try {
    writeFileSync(join(dir, HARNESS_PIN_FILE), `${DEEPSEEK_HARNESS_COMMIT}\n`);
    mkdirSync(join(dir, 'vendor', 'cordis', 'lib'), {recursive: true});
    // 空目录仍缺 index.js → NOT_BUILT
    await expect(bootPinnedCordis(dir)).rejects.toThrow(/HARNESS_CORDIS_NOT_BUILT/);
  } finally {
    rmSync(dir, {recursive: true, force: true});
  }
});
