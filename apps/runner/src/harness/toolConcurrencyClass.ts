/**
 * 工具调用级并发分类（AB07 / AGENTS §7.1）。
 *
 * 并发安全是「本次调用」的属性，不是工具名或 replay_class 的静态标签。
 * 未完成调用级分类合同前：同 activation/workspace **一律 SERIAL**；
 * `READ_ONLY` 不得自动升为可并行。
 */

export type ConcurrencyClass = 'SERIAL' | 'PARALLEL_SAFE' | 'UNKNOWN';

export type ToolCallConcurrencyDecision = {
  class: ConcurrencyClass;
  /** 本次调用触及的资源键（如 workspace 相对 path）；用于序列化与未来分批 */
  resourceKeys: string[];
  reason:
    | 'CONCURRENCY_CLASSIFICATION_NOT_READY'
    | 'READ_ONLY_LABEL_DOES_NOT_GRANT_PARALLEL'
    | 'UNKNOWN_TOOL_FAIL_CLOSED';
  /** 红线：分类裁决绝不是 Goal DONE */
  marksGoalDone: false;
};

export type ClassifyToolCallConcurrencyInput = {
  toolRef: string;
  /** 规范化或原始 arguments JSON */
  argumentsJson: string;
  /** Kernel Manifest / effect 的 replay_class；READ_ONLY 也不授 PARALLEL */
  replayClass?: string | null;
};

function extractResourceKeys(toolRef: string, argumentsJson: string): string[] {
  try {
    const parsed = JSON.parse(argumentsJson) as Record<string, unknown>;
    if (toolRef === 'read_file') {
      // 扁平 {path} 或 ToolPayload {parameters:{path}}
      let path: unknown = parsed.path;
      if (
        path === undefined &&
        parsed.parameters !== null &&
        typeof parsed.parameters === 'object' &&
        !Array.isArray(parsed.parameters)
      ) {
        path = (parsed.parameters as Record<string, unknown>).path;
      }
      if (typeof path === 'string' && path) {
        return [`path:${path}`];
      }
    }
  } catch {
    // 解析失败 → 无键，仍 SERIAL 失败关闭于上层校验
  }
  return [];
}

/**
 * V1：永远返回 SERIAL。显式拒绝「因 READ_ONLY 标签并发」的捷径。
 */
export function classifyToolCallConcurrency(
  input: ClassifyToolCallConcurrencyInput,
): ToolCallConcurrencyDecision {
  const resourceKeys = extractResourceKeys(input.toolRef, input.argumentsJson);
  const replay = (input.replayClass ?? '').toUpperCase();
  if (replay === 'READ_ONLY') {
    return {
      class: 'SERIAL',
      resourceKeys,
      reason: 'READ_ONLY_LABEL_DOES_NOT_GRANT_PARALLEL',
      marksGoalDone: false,
    };
  }
  if (!input.toolRef) {
    return {
      class: 'SERIAL',
      resourceKeys,
      reason: 'UNKNOWN_TOOL_FAIL_CLOSED',
      marksGoalDone: false,
    };
  }
  return {
    class: 'SERIAL',
    resourceKeys,
    reason: 'CONCURRENCY_CLASSIFICATION_NOT_READY',
    marksGoalDone: false,
  };
}

/** 若未来误标 PARALLEL_SAFE，Runner 必须拒绝执行（失败关闭）。 */
export function assertSerialOnly(
  decision: ToolCallConcurrencyDecision,
): void {
  if (decision.class !== 'SERIAL') {
    throw new Error(
      `TOOL_CONCURRENCY_REJECTED:${decision.class}:${decision.reason}`,
    );
  }
}
