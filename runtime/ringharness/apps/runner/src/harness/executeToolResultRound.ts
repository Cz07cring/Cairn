/**
 * Cordis EXECUTE：tool-call 已 DISPATCHED 后，观察 effect 终态 → ToolResult 回灌 → Harness chat 第二轮。
 *
 * 边界：
 * - 循环在 Harness（openaiCompatibleToolChat），副作用仍只经已 dispatch 的 Effect；
 * - ≠ 官方 AgentLoop；≠ Goal DONE；不得写入「Harness 接入完成」。
 * - UNKNOWN/超时诚实上报，不把 DISPATCHED 冒充 SUCCEEDED。
 * - SUCCEEDED 时优先拉 evidence 工件正文回灌，避免仅 status 摘要冒充 ToolResult。
 */
import {assistantCitesToolResult} from './ab01TwoTurnLoop.js';
import type {KernelLlmChunk} from './cordisBootGate.js';
import {
  observeDispatchedEffect,
  type ExecuteToolHost,
} from './executeToolHost.js';
import {
  chatCompletionWithTools,
  type ChatMessage,
  type OpenAiCompatibleToolChatConfig,
} from './openaiCompatibleToolChat.js';

const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'UNKNOWN', 'CANCELLED']);

export type ObservedEffect = {
  tool: string;
  effectId: string;
  status: string;
  toolCallId: string;
  arguments: string;
  toolResultText: string;
  evidenceIds: string[];
};

export type ToolResultRoundEvidence = {
  observed: ObservedEffect[];
  round2AssistantText: string;
  round2CitesToolResult: boolean;
  /** 任一对账未决时为 true */
  requiresReconciliation: boolean;
  marksGoalDone: false;
};

export type FormatToolResultInput = {
  tool: string;
  effectId: string;
  status: string;
  evidenceIds: string[];
  host: ExecuteToolHost;
};

export type ToolResultRoundOpts = {
  chat: OpenAiCompatibleToolChatConfig;
  /** 第二轮追问；默认要求引用工具结果 */
  round2Prompt?: string;
  observe?: {
    maxAttempts?: number;
    delayMs?: number;
    sleep?: (ms: number) => Promise<void>;
  };
  /** 测试注入：覆盖 tool 文本（生产默认拉 evidence） */
  formatToolResult?: (
    input: FormatToolResultInput,
  ) => string | Promise<string>;
};

/** 与 brokerBackedHarnessTool 默认 poll 对齐：给 Broker 回执窗口留足时间 */
export const DEFAULT_TOOL_RESULT_OBSERVE = {
  maxAttempts: 120,
  delayMs: 500,
} as const;

/**
 * live ToolResult 观察窗口：环境可覆盖；非法值失败关闭。
 * RING_TOOL_RESULT_OBSERVE_MAX_ATTEMPTS / RING_TOOL_RESULT_OBSERVE_DELAY_MS
 */
export function resolveToolResultObserveFromEnv(
  env: Record<string, string | undefined> = process.env,
): {maxAttempts: number; delayMs: number} {
  const maxRaw = (env.RING_TOOL_RESULT_OBSERVE_MAX_ATTEMPTS || '').trim();
  const delayRaw = (env.RING_TOOL_RESULT_OBSERVE_DELAY_MS || '').trim();
  const maxAttempts = maxRaw
    ? Number.parseInt(maxRaw, 10)
    : DEFAULT_TOOL_RESULT_OBSERVE.maxAttempts;
  const delayMs = delayRaw
    ? Number.parseInt(delayRaw, 10)
    : DEFAULT_TOOL_RESULT_OBSERVE.delayMs;
  if (!Number.isInteger(maxAttempts) || maxAttempts < 1) {
    throw new Error(
      `TOOL_RESULT_OBSERVE_INVALID: maxAttempts=${maxRaw || String(maxAttempts)}`,
    );
  }
  if (!Number.isInteger(delayMs) || delayMs < 0) {
    throw new Error(
      `TOOL_RESULT_OBSERVE_INVALID: delayMs=${delayRaw || String(delayMs)}`,
    );
  }
  return {maxAttempts, delayMs};
}

function toolCallsFromChunks(
  chunks: readonly KernelLlmChunk[],
): Array<{id: string; name: string; arguments: string}> {
  const out: Array<{id: string; name: string; arguments: string}> = [];
  let i = 0;
  for (const c of chunks) {
    if (c.type !== 'tool-call') continue;
    i += 1;
    out.push({
      id: `call_${i}`,
      name: c.name,
      arguments: c.arguments ?? '{}',
    });
  }
  return out;
}

function assistantTextFromChunks(chunks: readonly KernelLlmChunk[]): string {
  return chunks
    .filter((c): c is Extract<KernelLlmChunk, {type: 'text-delta'}> => c.type === 'text-delta')
    .map((c) => c.text)
    .join('');
}

/**
 * 默认 ToolResult：status 行 + SUCCEEDED 时按 evidence_ids 拉工件正文。
 * 拉失败诚实写入 error 行，不静默丢弃。
 */
export async function defaultFormatToolResult(
  input: FormatToolResultInput,
): Promise<string> {
  const lines = [
    `tool=${input.tool}`,
    `effect_id=${input.effectId}`,
    `status=${input.status}`,
  ];
  if (input.evidenceIds.length === 0) {
    return lines.join('\n');
  }
  if (input.status !== 'SUCCEEDED') {
    lines.push(`evidence_ids=${input.evidenceIds.join(',')}`);
    return lines.join('\n');
  }
  for (const id of input.evidenceIds) {
    lines.push(`evidence_id=${id}`);
    try {
      const text = await input.host.artifacts.getArtifactContent(id);
      lines.push(text);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      lines.push(`evidence_fetch_error=${msg}`);
    }
  }
  return lines.join('\n');
}

/**
 * 观察已 dispatch 的 effects，经 Harness chat 跑 ToolResult 第二轮。
 * effects 与 chunks 中的 tool-call 按顺序对齐；多工具时全部挂上 assistant.tool_calls。
 */
export async function runToolResultSecondTurn(input: {
  host: ExecuteToolHost;
  chunks: readonly KernelLlmChunk[];
  effects: ReadonlyArray<{tool: string; effectId: string; dispatchStatus: string}>;
  userPrompt: string;
  opts: ToolResultRoundOpts;
}): Promise<ToolResultRoundEvidence> {
  const calls = toolCallsFromChunks(input.chunks);
  if (calls.length === 0) {
    throw new Error('TOOL_RESULT_ROUND_NO_TOOL_CALLS');
  }
  if (calls.length !== input.effects.length) {
    throw new Error(
      `TOOL_RESULT_ROUND_MISMATCH: calls=${calls.length} effects=${input.effects.length}`,
    );
  }

  const format = input.opts.formatToolResult ?? defaultFormatToolResult;
  const observed: ObservedEffect[] = [];
  let requiresReconciliation = false;

  for (let i = 0; i < input.effects.length; i += 1) {
    const effect = input.effects[i]!;
    const call = calls[i]!;
    const viewed = await observeDispatchedEffect(input.host, effect.effectId, {
      maxAttempts:
        input.opts.observe?.maxAttempts ?? DEFAULT_TOOL_RESULT_OBSERVE.maxAttempts,
      delayMs: input.opts.observe?.delayMs ?? DEFAULT_TOOL_RESULT_OBSERVE.delayMs,
      sleep: input.opts.observe?.sleep,
    });
    if (!TERMINAL.has(viewed.status)) {
      requiresReconciliation = true;
    }
    if (viewed.status === 'UNKNOWN') {
      requiresReconciliation = true;
    }
    const evidenceIds = viewed.evidenceIds ?? [];
    const toolResultText = await format({
      tool: effect.tool,
      effectId: effect.effectId,
      status: viewed.status,
      evidenceIds,
      host: input.host,
    });
    observed.push({
      tool: effect.tool,
      effectId: effect.effectId,
      status: viewed.status,
      toolCallId: call.id,
      arguments: call.arguments,
      toolResultText,
      evidenceIds,
    });
  }

  const prior: ChatMessage[] = [
    {
      role: 'system',
      content:
        '你是执行助手。根据 tool 回执回答；不要臆造未回执内容。回答中须引用回执原文片段。',
    },
    {role: 'user', content: input.userPrompt},
  ];

  // 多工具：全部 tool_calls 挂在同一条 assistant 上，再按序附 tool 消息（OpenAI 同形）
  const assistantWithCalls: ChatMessage = {
    role: 'assistant',
    content: assistantTextFromChunks(input.chunks) || null,
    tool_calls: calls.map((c) => ({
      id: c.id,
      type: 'function',
      function: {name: c.name, arguments: c.arguments},
    })),
  };
  const toolMsgs: ChatMessage[] = observed.map((o) => ({
    role: 'tool',
    tool_call_id: o.toolCallId,
    content: o.toolResultText,
  }));
  const followUp =
    input.opts.round2Prompt ??
    '请用一句话引用工具回执中的 status= 行，证明你读到了结果。';
  const messages: ChatMessage[] = [
    ...prior,
    assistantWithCalls,
    ...toolMsgs,
    {role: 'user', content: followUp},
  ];

  const round2 = await chatCompletionWithTools(
    input.opts.chat,
    messages,
    // 第二轮不再暴露工具，强制用回执作答
    [],
  );

  const combinedToolText = observed.map((o) => o.toolResultText).join('\n');
  const cites = assistantCitesToolResult(round2.assistantText, combinedToolText);

  return {
    observed,
    round2AssistantText: round2.assistantText,
    round2CitesToolResult: cites,
    requiresReconciliation,
    marksGoalDone: false,
  };
}
