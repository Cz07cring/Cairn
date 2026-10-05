/**
 * GitHub archive pull → 旁路 pin 目录（不走真网时可注入 fetch/extract）。
 */
import {mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {afterEach, describe, expect, test} from 'vitest';
import {
  githubHarnessArchiveUrl,
  harnessPinCheckoutDir,
  pullHarnessPinCheckout,
  writeCheckoutPinFile,
} from './harnessCheckoutPull.js';
import {HARNESS_PIN_FILE} from './pinGate.js';

const PIN = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const PIN2 = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';

describe('harnessCheckoutPull', () => {
  const roots: string[] = [];

  afterEach(() => {
    for (const r of roots.splice(0)) {
      rmSync(r, {recursive: true, force: true});
    }
  });

  test('archive URL 钉扎 SHA；非法 SHA 失败关闭', () => {
    expect(githubHarnessArchiveUrl(PIN)).toContain(`/archive/${PIN}.tar.gz`);
    expect(() => githubHarnessArchiveUrl('latest')).toThrow(/HARNESS_PIN_INVALID/);
  });

  test('已存在且 pin 匹配则 reused，不调 fetch', async () => {
    const pinsRoot = mkdtempSync(join(tmpdir(), 'ring-pins-'));
    roots.push(pinsRoot);
    const dir = harnessPinCheckoutDir(PIN, pinsRoot);
    mkdirSync(dir, {recursive: true});
    writeCheckoutPinFile(dir, PIN);
    writeFileSync(join(dir, 'marker.txt'), 'keep');

    let fetchCalls = 0;
    const out = await pullHarnessPinCheckout({
      sha: PIN,
      pinsRoot,
      fetchImpl: async () => {
        fetchCalls += 1;
        throw new Error('SHOULD_NOT_FETCH');
      },
    });
    expect(out.reused).toBe(true);
    expect(out.checkoutDir).toBe(dir);
    expect(fetchCalls).toBe(0);
    expect(readFileSync(join(dir, 'marker.txt'), 'utf8')).toBe('keep');
  });

  test('注入 fetch+extract 写入旁路目录与 pin 文件', async () => {
    const pinsRoot = mkdtempSync(join(tmpdir(), 'ring-pins-'));
    roots.push(pinsRoot);
    const workDir = mkdtempSync(join(tmpdir(), 'ring-pull-work-'));
    roots.push(workDir);

    const fakeBody = Buffer.alloc(120, 1);
    const out = await pullHarnessPinCheckout({
      sha: PIN2,
      pinsRoot,
      workDir,
      fetchImpl: async (url) => {
        expect(String(url)).toContain(PIN2);
        return new Response(fakeBody, {status: 200});
      },
      extractTarGz: async (_archive, destDir) => {
        const top = join(destDir, `deepseek-harness-${PIN2}`);
        mkdirSync(top, {recursive: true});
        writeFileSync(join(top, 'README.md'), 'pulled');
      },
    });

    expect(out.reused).toBe(false);
    expect(out.checkoutDir).toBe(harnessPinCheckoutDir(PIN2, pinsRoot));
    expect(readFileSync(join(out.checkoutDir, 'README.md'), 'utf8')).toBe(
      'pulled',
    );
    expect(
      readFileSync(join(out.checkoutDir, HARNESS_PIN_FILE), 'utf8').trim(),
    ).toBe(PIN2);
  });
});
