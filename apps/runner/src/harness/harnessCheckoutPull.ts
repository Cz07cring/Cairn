/**
 * 旁路 pin 目录布局：每个上游 SHA 独立 checkout，热切换靠换路径（避免 ESM 双实例）。
 *
 * `.runtime/harness-pins/<sha>/` + `.ringharness-pinned-commit`
 * 当前激活可用 symlink / env RING_HARNESS_CHECKOUT 指向其一。
 */
import {existsSync, mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {DEEPSEEK_HARNESS_REPO} from './pin.js';
import {HARNESS_PIN_FILE} from './pinGate.js';

const SHA_RE = /^[0-9a-f]{40}$/;

export function assertHarnessSha(sha: string): string {
  const s = sha.trim().toLowerCase();
  if (!SHA_RE.test(s)) {
    throw new Error(`HARNESS_PIN_INVALID: ${sha}`);
  }
  return s;
}

/** 默认 pins 根：RING_HARNESS_PINS_ROOT 或 <cwd>/.runtime/harness-pins */
export function resolveHarnessPinsRoot(
  environ: NodeJS.ProcessEnv = process.env,
  cwd: string = process.cwd(),
): string {
  const raw = environ.RING_HARNESS_PINS_ROOT?.trim();
  if (raw) return raw;
  return join(cwd, '.runtime', 'harness-pins');
}

export function harnessPinCheckoutDir(
  sha: string,
  pinsRoot: string = resolveHarnessPinsRoot(),
): string {
  return join(pinsRoot, assertHarnessSha(sha));
}

export function readCheckoutPinFile(checkoutDir: string): string | null {
  const p = join(checkoutDir, HARNESS_PIN_FILE);
  if (!existsSync(p)) return null;
  const raw = readFileSync(p, 'utf8').trim().toLowerCase();
  return SHA_RE.test(raw) ? raw : null;
}

export function writeCheckoutPinFile(checkoutDir: string, sha: string): void {
  mkdirSync(checkoutDir, {recursive: true});
  writeFileSync(
    join(checkoutDir, HARNESS_PIN_FILE),
    `${assertHarnessSha(sha)}\n`,
    'utf8',
  );
}

export function githubHarnessArchiveUrl(
  sha: string,
  repoUrl: string = DEEPSEEK_HARNESS_REPO,
): string {
  const pin = assertHarnessSha(sha);
  // https://github.com/deepseek-ai/deepseek-harness/archive/<sha>.tar.gz
  const base = repoUrl.replace(/\.git$/, '').replace(/\/$/, '');
  return `${base}/archive/${pin}.tar.gz`;
}

export type PullHarnessPinResult = {
  checkoutDir: string;
  pin: string;
  reused: boolean;
  archiveUrl: string;
};

/**
 * 从 GitHub archive 拉取固定 SHA 到旁路目录（不覆盖已存在且 pin 匹配的目录）。
 * 不自动 build:lib:host；热切换前须自行构建或复用已有 lib。
 */
export async function pullHarnessPinCheckout(input: {
  sha: string;
  pinsRoot?: string;
  repoUrl?: string;
  /** 注入 fetch（测网关）；默认 globalThis.fetch */
  fetchImpl?: typeof fetch;
  /** 解压实现；默认用系统 tar（测可注入） */
  extractTarGz?: (archivePath: string, destDir: string) => Promise<void>;
  /** 写入临时 archive 的目录 */
  workDir?: string;
}): Promise<PullHarnessPinResult> {
  const pin = assertHarnessSha(input.sha);
  const pinsRoot = input.pinsRoot ?? resolveHarnessPinsRoot();
  const checkoutDir = harnessPinCheckoutDir(pin, pinsRoot);
  const archiveUrl = githubHarnessArchiveUrl(pin, input.repoUrl);

  const existing = readCheckoutPinFile(checkoutDir);
  if (existing === pin && existsSync(checkoutDir)) {
    return {checkoutDir, pin, reused: true, archiveUrl};
  }

  mkdirSync(pinsRoot, {recursive: true});
  const fetchImpl = input.fetchImpl ?? globalThis.fetch;
  if (typeof fetchImpl !== 'function') {
    throw new Error('HARNESS_PULL_NO_FETCH: 运行时无 fetch');
  }
  const res = await fetchImpl(archiveUrl);
  if (!res.ok) {
    throw new Error(
      `HARNESS_PULL_HTTP_${res.status}: ${archiveUrl}`,
    );
  }
  const buf = Buffer.from(await res.arrayBuffer());
  if (buf.byteLength < 100) {
    throw new Error('HARNESS_PULL_EMPTY_ARCHIVE');
  }

  const {mkdtempSync, writeFileSync: writeFs, rmSync} = await import('node:fs');
  const {tmpdir} = await import('node:os');
  const {join: pathJoin} = await import('node:path');
  const ownsWork = input.workDir === undefined;
  const work =
    input.workDir ?? mkdtempSync(pathJoin(tmpdir(), 'ring-harness-pull-'));
  mkdirSync(work, {recursive: true});
  const archivePath = pathJoin(work, `${pin}.tar.gz`);
  writeFs(archivePath, buf);

  try {
    // 先解到 staging，再原子换成目标名（archive 顶层常为 repo-sha/）
    const staging = pathJoin(work, 'staging');
    mkdirSync(staging, {recursive: true});
    if (input.extractTarGz) {
      await input.extractTarGz(archivePath, staging);
    } else {
      const {execFileSync} = await import('node:child_process');
      execFileSync('tar', ['-xzf', archivePath, '-C', staging], {
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    }
    const {readdirSync, cpSync, rmSync: rm} = await import('node:fs');
    const kids = readdirSync(staging);
    if (kids.length !== 1) {
      throw new Error(
        `HARNESS_PULL_LAYOUT: 期望单一顶层目录，got ${kids.join(',')}`,
      );
    }
    const extracted = pathJoin(staging, kids[0]!);
    if (existsSync(checkoutDir)) {
      rm(checkoutDir, {recursive: true, force: true});
    }
    mkdirSync(pinsRoot, {recursive: true});
    cpSync(extracted, checkoutDir, {recursive: true});
    writeCheckoutPinFile(checkoutDir, pin);
  } finally {
    if (ownsWork) {
      try {
        rmSync(work, {recursive: true, force: true});
      } catch {
        // 临时目录清理失败不挡主路径
      }
    }
  }

  return {checkoutDir, pin, reused: false, archiveUrl};
}
