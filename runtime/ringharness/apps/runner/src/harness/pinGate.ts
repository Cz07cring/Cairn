/**
 * 官方 Harness 钉扎门禁：常量/环境 pin 格式始终校验；若提供 checkout 则核对 HEAD 或 archive pin。
 * 未 clone / 未安装上游 ≠ adapter 已验收。热更新：换 checkout + RING_HARNESS_PIN 后 reload 插件。
 */
import {execFileSync} from 'node:child_process';
import {existsSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {resolveHarnessPin} from './pin.js';

const SHA_RE = /^[0-9a-f]{40}$/;

/** archive 检出无稳定 .git 时的 pin 文件名（与 git HEAD 二选一）。 */
export const HARNESS_PIN_FILE = '.ringharness-pinned-commit' as const;

export function assertPinnedCommitFormat(commit: string = resolveHarnessPin()): void {
  if (!SHA_RE.test(commit)) {
    throw new Error(`HARNESS_PIN_INVALID: ${commit}`);
  }
}

function readArchivePin(checkoutDir: string): string | null {
  const pinPath = join(checkoutDir, HARNESS_PIN_FILE);
  if (!existsSync(pinPath)) {
    return null;
  }
  const raw = readFileSync(pinPath, 'utf8').trim();
  return SHA_RE.test(raw) ? raw : null;
}

/**
 * 核对本地 checkout：若目录自身有 `.git` 则核 HEAD；否则读 `.ringharness-pinned-commit`。
 * 禁止 `git -C` 向上找到父仓（例如 checkout 嵌在 ringharness 树内且无独立 .git）。
 * 目录不存在或无法核验时抛 HARNESS_CHECKOUT_MISSING，不假装已核验。
 */
export function assertCheckoutMatchesPin(
  checkoutDir: string,
  expected: string = resolveHarnessPin(),
): void {
  assertPinnedCommitFormat(expected);
  if (!existsSync(checkoutDir)) {
    throw new Error(`HARNESS_CHECKOUT_MISSING: ${checkoutDir}`);
  }
  let head: string | null = null;
  const ownGit = existsSync(join(checkoutDir, '.git'));
  if (ownGit) {
    try {
      head = execFileSync('git', ['-C', checkoutDir, 'rev-parse', 'HEAD'], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
      }).trim();
    } catch {
      head = null;
    }
  }
  if (!head) {
    head = readArchivePin(checkoutDir);
  }
  if (!head) {
    throw new Error(`HARNESS_CHECKOUT_MISSING: ${checkoutDir}`);
  }
  if (head !== expected) {
    throw new Error(`HARNESS_PIN_MISMATCH: head=${head} expected=${expected}`);
  }
}

/** 环境变量 RING_HARNESS_CHECKOUT 存在时才核对；否则仅校验当前 pin 格式。 */
export function runHarnessPinGate(
  checkoutDir: string | undefined = process.env.RING_HARNESS_CHECKOUT,
  expected: string = resolveHarnessPin(),
): 'format-only' | 'checkout-matched' {
  assertPinnedCommitFormat(expected);
  if (!checkoutDir) {
    return 'format-only';
  }
  assertCheckoutMatchesPin(checkoutDir, expected);
  return 'checkout-matched';
}
