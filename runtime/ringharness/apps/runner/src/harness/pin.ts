/**
 * 官方 deepseek-harness 钉扎。
 *
 * - `DEEPSEEK_HARNESS_COMMIT`：仓库默认 pin（doc/09）
 * - `RING_HARNESS_PIN`：运行时可覆盖，便于换 checkout / 热更新上游而不改代码
 * 未 clone/安装/运行上游 ≠ adapter 已验收；禁止 `latest`。
 */
export const DEEPSEEK_HARNESS_COMMIT =
  'c291e7961a515f6d7af9304e7fd1d257929aef26' as const;

export const DEEPSEEK_HARNESS_REPO =
  'https://github.com/deepseek-ai/deepseek-harness' as const;

/** 上游根清单要求；本仓库 runner 仍可用更高 Node，正式 adapter 构建须对齐。 */
export const DEEPSEEK_HARNESS_NODE = '^22.19.0 || >=24.0.0' as const;
export const DEEPSEEK_HARNESS_PNPM = '11.7.0' as const;

const SHA_RE = /^[0-9a-f]{40}$/;

/**
 * 解析当前生效 pin：环境变量优先，否则默认常量。
 * 非法 SHA → 失败关闭（不回落到 latest）。
 */
export function resolveHarnessPin(
  environ: NodeJS.ProcessEnv = process.env,
): string {
  const raw = environ.RING_HARNESS_PIN?.trim();
  if (raw === undefined || raw === '') {
    return DEEPSEEK_HARNESS_COMMIT;
  }
  if (!SHA_RE.test(raw)) {
    throw new Error(`HARNESS_PIN_INVALID: ${raw}`);
  }
  return raw;
}
