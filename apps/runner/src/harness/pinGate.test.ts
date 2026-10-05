import {mkdirSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {expect, test} from 'vitest';
import {DEEPSEEK_HARNESS_COMMIT, resolveHarnessPin} from './pin.js';
import {
  assertCheckoutMatchesPin,
  assertPinnedCommitFormat,
  HARNESS_PIN_FILE,
  runHarnessPinGate,
} from './pinGate.js';

test('钉扎 SHA 为 40 位小写十六进制', () => {
  assertPinnedCommitFormat(DEEPSEEK_HARNESS_COMMIT);
  expect(DEEPSEEK_HARNESS_COMMIT).toBe('c291e7961a515f6d7af9304e7fd1d257929aef26');
});

test('未配置 checkout 时门禁仅校验格式', () => {
  const prev = process.env.RING_HARNESS_CHECKOUT;
  delete process.env.RING_HARNESS_CHECKOUT;
  try {
    expect(runHarnessPinGate(undefined)).toBe('format-only');
  } finally {
    if (prev !== undefined) {
      process.env.RING_HARNESS_CHECKOUT = prev;
    }
  }
});

test('checkout 缺失时明确失败，不假装已核验', () => {
  expect(() => assertCheckoutMatchesPin('/tmp/ringharness-no-such-harness-checkout')).toThrow(
    /HARNESS_CHECKOUT_MISSING/,
  );
});

test('archive pin 文件匹配时门禁通过', () => {
  const dir = mkdtempSync(join(tmpdir(), 'ring-harness-pin-'));
  try {
    writeFileSync(join(dir, HARNESS_PIN_FILE), `${DEEPSEEK_HARNESS_COMMIT}\n`);
    expect(runHarnessPinGate(dir)).toBe('checkout-matched');
  } finally {
    rmSync(dir, {recursive: true, force: true});
  }
});

test('archive pin 不匹配时明确失败', () => {
  const dir = mkdtempSync(join(tmpdir(), 'ring-harness-pin-'));
  try {
    writeFileSync(join(dir, HARNESS_PIN_FILE), `${'a'.repeat(40)}\n`);
    expect(() => assertCheckoutMatchesPin(dir)).toThrow(/HARNESS_PIN_MISMATCH/);
  } finally {
    rmSync(dir, {recursive: true, force: true});
  }
});

test('无独立 .git 时用 pin 文件，不误读父仓 HEAD', () => {
  // 在仓库树内建无 .git 子目录，旧实现会 git -C 向上解析到 ringharness HEAD
  const repoRuntime = join(process.cwd(), '..', '..', '.runtime');
  mkdirSync(repoRuntime, {recursive: true});
  const nested = mkdtempSync(join(repoRuntime, 'pin-gate-'));
  try {
    writeFileSync(join(nested, HARNESS_PIN_FILE), `${DEEPSEEK_HARNESS_COMMIT}\n`);
    expect(() => assertCheckoutMatchesPin(nested)).not.toThrow();
  } finally {
    rmSync(nested, {recursive: true, force: true});
  }
});

// ---------------------------------------------- RING_HARNESS_PIN 运行时覆盖
//
// 该能力（换 checkout 后靠环境变量切换 pin，避免改代码+重建）此前**零测试**：
// `grep -c RING_HARNESS_PIN pinGate.test.ts` = 0，而 pin.ts 的 resolveHarnessPin
// 正是为此存在。补上，以便热更新路径有回归保护。

/** 另一个合法 SHA（本仓从未构建过它）—— 用于证明覆盖生效。 */
const OTHER_SHA = 'b'.repeat(40);

function withEnv<T>(key: string, value: string | undefined, fn: () => T): T {
  const prev = process.env[key];
  try {
    if (value === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = value;
    }
    return fn();
  } finally {
    if (prev === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = prev;
    }
  }
}

test('未配置 RING_HARNESS_PIN 时回退仓库默认 pin', () => {
  withEnv('RING_HARNESS_PIN', undefined, () => {
    expect(resolveHarnessPin()).toBe(DEEPSEEK_HARNESS_COMMIT);
  });
});

test('RING_HARNESS_PIN 覆盖仓库默认（热更新入口）', () => {
  withEnv('RING_HARNESS_PIN', OTHER_SHA, () => {
    expect(resolveHarnessPin()).toBe(OTHER_SHA);
  });
});

test('RING_HARNESS_PIN 格式非法时失败关闭，不静默回落 latest/默认', () => {
  for (const bad of ['not-a-sha', 'ABC', 'z'.repeat(40), 'a'.repeat(39), '']) {
    withEnv('RING_HARNESS_PIN', bad, () => {
      if (bad === '') {
        // 空串按「未配置」处理：回落默认，而非视为非法
        expect(resolveHarnessPin()).toBe(DEEPSEEK_HARNESS_COMMIT);
      } else {
        expect(() => resolveHarnessPin()).toThrow(/HARNESS_PIN_INVALID/);
      }
    });
  }
});

test('门禁用覆盖后的 pin 核对 checkout：改 env 即改期望版本，无需改代码', () => {
  const dir = mkdtempSync(join(tmpdir(), 'ring-harness-pin-'));
  try {
    // checkout 实际是 OTHER_SHA
    writeFileSync(join(dir, HARNESS_PIN_FILE), `${OTHER_SHA}\n`);
    // 默认 pin（仓库常量）与它对不上 → 必须失败（外部化不等于免校验）
    withEnv('RING_HARNESS_PIN', undefined, () => {
      expect(() => runHarnessPinGate(dir)).toThrow(/HARNESS_PIN_MISMATCH/);
    });
    // 仅把 env 指到 OTHER_SHA → 通过；本仓代码一字未改
    withEnv('RING_HARNESS_PIN', OTHER_SHA, () => {
      expect(runHarnessPinGate(dir)).toBe('checkout-matched');
    });
  } finally {
    rmSync(dir, {recursive: true, force: true});
  }
});

