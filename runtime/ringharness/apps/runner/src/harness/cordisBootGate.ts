/**
 * Cordis boot：返回真实 Context，供后续挂薄 llm（非 agent loop）。
 */
import {existsSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {requireHarnessCheckout} from './harnessPlanAdapter.js';
import {resolveHarnessPin} from './pin.js';

/** Cordis Context 最小可读面（proxied）；避免把上游类型拉进 runner 编译图。 */
export type CordisContext = {
  provide: (name: string, value?: unknown) => () => void;
  get: (name: string, strict?: boolean) => unknown;
  llm?: KernelLlmService;
};

export type KernelLlmService = {
  stream: (options: KernelLlmStreamOptions) => AsyncIterable<KernelLlmChunk>;
  /** 标记：ring 薄适配器，≠ 官方 LlmRuntime */
  implementation: 'ring-kernel-bridge';
};

export type KernelLlmStreamOptions = {
  provider: string;
  model: string;
  messages: ReadonlyArray<{role: string; content: string}>;
  tools?: ReadonlyArray<{name: string}>;
};

export type KernelLlmChunk =
  | {type: 'text-delta'; text: string}
  | {type: 'finish'; reason: 'stop' | 'error'}
  | {type: 'tool-call'; name: string; arguments?: string};

export type CordisBootResult = {
  status: 'cordis-booted';
  pin: string;
  packageName: string;
  entry: string;
  exportKeys: string[];
  /** 真实 Cordis Context；调用方负责挂 llm，勿当业务权威。 */
  ctx: CordisContext;
};

export const CORDIS_LIB_ENTRY = join('vendor', 'cordis', 'lib', 'index.js');

export function cordisEntryPath(checkoutDir: string): string {
  return join(checkoutDir, CORDIS_LIB_ENTRY);
}

/**
 * 钉扎 checkout → import Cordis → new Context()。
 * 未 build → HARNESS_CORDIS_NOT_BUILT。
 */
export async function bootPinnedCordis(
  checkoutDir: string | undefined = process.env.RING_HARNESS_CHECKOUT,
): Promise<CordisBootResult> {
  requireHarnessCheckout(checkoutDir);
  const root = checkoutDir as string;
  const entry = cordisEntryPath(root);
  if (!existsSync(entry)) {
    throw new Error(
      `HARNESS_CORDIS_NOT_BUILT: 缺少 ${CORDIS_LIB_ENTRY}；请在钉扎 checkout 内构建 @deepseek-ai/cordis`,
    );
  }
  const cosmokit = join(root, 'vendor', 'cosmokit', 'lib', 'index.js');
  if (!existsSync(cosmokit)) {
    throw new Error(
      'HARNESS_CORDIS_NOT_BUILT: 缺少 vendor/cosmokit/lib/index.js（cordis 依赖）',
    );
  }

  const mod = (await import(pathToFileURL(entry).href)) as {
    Context?: new () => CordisContext;
  };
  if (typeof mod.Context !== 'function') {
    throw new Error('HARNESS_CORDIS_INVALID: 模块未导出 Context 构造器');
  }
  const ctx = new mod.Context();
  if (ctx === null || typeof ctx !== 'object') {
    throw new Error('HARNESS_CORDIS_INVALID: Context 实例化失败');
  }

  let packageName = '@deepseek-ai/cordis';
  const pkgPath = join(root, 'vendor', 'cordis', 'package.json');
  try {
    const pkg = JSON.parse(readFileSync(pkgPath, 'utf8')) as {name?: string};
    if (pkg.name) {
      packageName = pkg.name;
    }
  } catch {
    // 保留默认包名
  }

  return {
    status: 'cordis-booted',
    pin: resolveHarnessPin(),
    packageName,
    entry,
    exportKeys: Object.keys(mod).sort(),
    ctx,
  };
}
