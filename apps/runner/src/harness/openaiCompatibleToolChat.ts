/**
 * Harness 侧 OpenAI 兼容 chat（可带 tools）。
 * 官方 AgentLoop 经 modelInvocationLedger 登记 create→runner_owned dispatch→receipt；
 * 副作用仍须走 Effect Gateway。≠ Goal DONE。
 */
import {
  WatchdogHardIdleError,
  type ActivationWatchdog,
  type HardIdleOutcome,
} from './activationWatchdog.js';
export type ChatMessage =
  | {role: 'system' | 'user' | 'assistant'; content: string}
  | {
      role: 'assistant';
      content: string | null;
      tool_calls: Array<{
        id: string;
        type: 'function';
        function: {name: string; arguments: string};
      }>;
    }
  | {role: 'tool'; tool_call_id: string; content: string};

export type ChatToolDef = {
  type: 'function';
  function: {
    name: string;
    description?: string;
    parameters?: Record<string, unknown>;
  };
};

export type ChatToolCall = {
  id: string;
  name: string;
  arguments: string;
};

export type ChatRoundResult = {
  assistantText: string;
  toolCalls: ChatToolCall[];
  finishReason: string | null;
  raw: unknown;
};

export type OpenAiCompatibleToolChatConfig = {
  baseUrl: string;
  apiKey: string;
  modelId: string;
  maxTokens?: number;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
  /**
   * 传给 chat/completions 的 tool_choice。
   * live 多步可设 required；缺省 auto。与 resolveToolChoice 并存时以 resolve 为准。
   */
  toolChoice?: 'auto' | 'required' | 'none';
  /** 按本轮已回灌 messages 动态选择 tool_choice（如诊断未完成则 required）。 */
  resolveToolChoice?: (
    messages: readonly ChatMessage[],
  ) => 'auto' | 'required' | 'none';
};

function requireConfigured(config: OpenAiCompatibleToolChatConfig): void {
  if (!config.baseUrl?.trim() || !config.apiKey?.trim() || !config.modelId?.trim()) {
    throw new Error('CHAT_NOT_CONFIGURED: baseUrl/apiKey/modelId 缺一不可');
  }
}

/**
 * 一次 chat/completions；tools 非空时模型可返回 tool_calls。
 */
export async function chatCompletionWithTools(
  config: OpenAiCompatibleToolChatConfig,
  messages: readonly ChatMessage[],
  tools: readonly ChatToolDef[] = [],
): Promise<ChatRoundResult> {
  requireConfigured(config);
  const fetchFn = config.fetchImpl ?? fetch;
  const base = config.baseUrl.replace(/\/$/, '');
  const payload: Record<string, unknown> = {
    model: config.modelId,
    messages,
    max_tokens: config.maxTokens ?? 512,
    temperature: 0,
  };
  if (tools.length > 0) {
    payload.tools = tools;
    payload.tool_choice =
      config.resolveToolChoice?.(messages) ?? config.toolChoice ?? 'auto';
  }

  const controller = new AbortController();
  const timeoutMs = config.timeoutMs ?? 120_000;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetchFn(`${base}/v1/chat/completions`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${config.apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timer);
  }

  if (!res.ok) {
    const detail = (await res.text()).slice(0, 500);
    throw new Error(`CHAT_HTTP_${res.status}: ${detail}`);
  }
  const body = (await res.json()) as {
    choices?: Array<{
      finish_reason?: string | null;
      message?: {
        content?: string | null;
        tool_calls?: Array<{
          id?: string;
          type?: string;
          function?: {name?: string; arguments?: string};
        }>;
      };
    }>;
  };
  const choice = body.choices?.[0];
  const message = choice?.message;
  if (!message) {
    throw new Error('CHAT_EMPTY_CHOICE: 响应无 message');
  }
  const toolCalls: ChatToolCall[] = [];
  for (const raw of message.tool_calls ?? []) {
    const id = (raw.id || '').trim();
    const name = (raw.function?.name || '').trim();
    if (!id || !name) {
      throw new Error('CHAT_TOOL_CALL_MALFORMED: 缺 id 或 name');
    }
    toolCalls.push({
      id,
      name,
      arguments: raw.function?.arguments ?? '{}',
    });
  }
  return {
    assistantText: (message.content || '').trim(),
    toolCalls,
    finishReason: choice?.finish_reason ?? null,
    raw: body,
  };
}

/** AB05：SSE 流间隙 Watchdog 钩子（与 non-stream chatCompletionWithTools 并存）。 */
export type ChatCompletionStreamWatchdogOpts = {
  watchdog: ActivationWatchdog;
  tickEveryMs?: number;
  setIntervalFn?: typeof setInterval;
  clearIntervalFn?: typeof clearInterval;
};

type SseDeltaToolCall = {
  index?: number;
  id?: string;
  type?: string;
  function?: {name?: string; arguments?: string};
};

type SseChunk = {
  choices?: Array<{
    finish_reason?: string | null;
    delta?: {
      content?: string | null;
      tool_calls?: SseDeltaToolCall[];
    };
  }>;
};

/**
 * OpenAI 兼容 SSE（stream:true）。首字节前进 streaming_llm；chunk 间隙由 Watchdog Hard 关准入。
 * Hard 时立即拒绝（即使 fetch/读流仍挂起），可带 partialAssistantText；≠ Goal DONE。
 */
export async function chatCompletionWithToolsStream(
  config: OpenAiCompatibleToolChatConfig,
  messages: readonly ChatMessage[],
  tools: readonly ChatToolDef[] = [],
  watchdogOpts?: ChatCompletionStreamWatchdogOpts,
): Promise<ChatRoundResult> {
  requireConfigured(config);
  const fetchFn = config.fetchImpl ?? fetch;
  const base = config.baseUrl.replace(/\/$/, '');
  const payload: Record<string, unknown> = {
    model: config.modelId,
    messages,
    max_tokens: config.maxTokens ?? 512,
    temperature: 0,
    stream: true,
  };
  if (tools.length > 0) {
    payload.tools = tools;
    payload.tool_choice =
      config.resolveToolChoice?.(messages) ?? config.toolChoice ?? 'auto';
  }

  const controller = new AbortController();
  const timeoutMs = config.timeoutMs ?? 120_000;
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let assistantText = '';
  const toolAcc = new Map<
    number,
    {id: string; name: string; arguments: string}
  >();
  let finishReason: string | null = null;
  const rawChunks: unknown[] = [];
  let firstByte = false;

  const watchdog = watchdogOpts?.watchdog;
  const tickEveryMs = watchdogOpts?.tickEveryMs ?? 250;
  const setI = watchdogOpts?.setIntervalFn ?? setInterval;
  const clearI = watchdogOpts?.clearIntervalFn ?? clearInterval;
  let tickTimer: ReturnType<typeof setInterval> | undefined;
  let rejectHard: ((err: WatchdogHardIdleError) => void) | undefined;
  const hardGate = watchdog
    ? new Promise<never>((_, reject) => {
        rejectHard = reject;
      })
    : null;

  const stopTick = () => {
    if (tickTimer !== undefined) {
      clearI(tickTimer);
      tickTimer = undefined;
    }
  };

  const fireHard = (outcome: HardIdleOutcome) => {
    stopTick();
    const err = new WatchdogHardIdleError(
      outcome,
      assistantText || undefined,
    );
    controller.abort();
    rejectHard?.(err);
  };

  if (watchdog) {
    tickTimer = setI(() => {
      const result = watchdog.tick();
      if (result?.kind === 'hard_idle') {
        fireHard(result);
      }
    }, tickEveryMs);
    (tickTimer as {unref?: () => void}).unref?.();
  }

  const readStream = async (): Promise<ChatRoundResult> => {
    const res = await fetchFn(`${base}/v1/chat/completions`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${config.apiKey}`,
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!res.ok) {
      const detail = (await res.text()).slice(0, 500);
      throw new Error(`CHAT_HTTP_${res.status}: ${detail}`);
    }
    if (!res.body) {
      throw new Error('CHAT_STREAM_EMPTY_BODY: 无 SSE body');
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    const onDataLine = (data: string) => {
      const trimmed = data.trim();
      if (!trimmed || trimmed === '[DONE]') {
        return;
      }
      let chunk: SseChunk;
      try {
        chunk = JSON.parse(trimmed) as SseChunk;
      } catch {
        throw new Error(`CHAT_STREAM_JSON_INVALID: ${trimmed.slice(0, 120)}`);
      }
      rawChunks.push(chunk);
      const choice = chunk.choices?.[0];
      if (!choice) {
        return;
      }
      if (choice.finish_reason) {
        finishReason = choice.finish_reason;
      }
      const delta = choice.delta;
      if (!delta) {
        return;
      }
      if (!firstByte) {
        firstByte = true;
        if (watchdog) {
          watchdog.enterPhase('streaming_llm', 'llm_stream');
          watchdog.reportProgress('llm_stream');
        }
      } else if (watchdog) {
        watchdog.reportProgress('llm_stream');
      }
      if (typeof delta.content === 'string' && delta.content.length > 0) {
        assistantText += delta.content;
      }
      for (const tc of delta.tool_calls ?? []) {
        const idx = tc.index ?? 0;
        const prev = toolAcc.get(idx) ?? {id: '', name: '', arguments: ''};
        if (tc.id) {
          prev.id = tc.id;
        }
        if (tc.function?.name) {
          prev.name = tc.function.name;
        }
        if (typeof tc.function?.arguments === 'string') {
          prev.arguments += tc.function.arguments;
        }
        toolAcc.set(idx, prev);
      }
      if (watchdog) {
        const tick = watchdog.tick();
        if (tick?.kind === 'hard_idle') {
          fireHard(tick);
        }
      }
    };

    while (true) {
      const {done, value} = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, {stream: true});
      const parts = buffer.split('\n');
      buffer = parts.pop() ?? '';
      for (const line of parts) {
        if (line.startsWith('data:')) {
          onDataLine(line.slice(5).trimStart());
        }
      }
    }
    if (buffer.trim().startsWith('data:')) {
      onDataLine(buffer.trim().slice(5).trimStart());
    }

    const toolCalls: ChatToolCall[] = [...toolAcc.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([, v]) => {
        if (!v.id.trim() || !v.name.trim()) {
          throw new Error('CHAT_TOOL_CALL_MALFORMED: 缺 id 或 name');
        }
        return {
          id: v.id,
          name: v.name,
          arguments: v.arguments || '{}',
        };
      });

    return {
      assistantText: assistantText.trim(),
      toolCalls,
      finishReason,
      raw: {stream: true, chunks: rawChunks},
    };
  };

  try {
    if (hardGate) {
      return await Promise.race([readStream(), hardGate]);
    }
    return await readStream();
  } finally {
    stopTick();
    clearTimeout(timer);
  }
}

/** 从环境装配（与 scripts/probe_local_qwen / chat.env 同名变量）。 */
export function toolChatConfigFromEnv(
  env: NodeJS.ProcessEnv = process.env,
): OpenAiCompatibleToolChatConfig | null {
  const baseUrl = (env.RING_LOCAL_QWEN_BASE || '').trim();
  const apiKey = (env.RING_LOCAL_QWEN_API_KEY || '').trim();
  const modelId = (env.RING_LOCAL_QWEN_MODEL || '').trim();
  if (!baseUrl || !apiKey || !modelId) {
    return null;
  }
  const timeoutRaw = (env.RING_LOCAL_QWEN_TIMEOUT || '').trim();
  const timeoutSec = timeoutRaw ? Number(timeoutRaw) : NaN;
  return {
    baseUrl,
    apiKey,
    modelId,
    timeoutMs: Number.isFinite(timeoutSec) ? Math.max(30, timeoutSec) * 1000 : 120_000,
  };
}

/**
 * 把非流式 chat/completions JSON 体转成 SSE Response（供 FSM mock / 测试双模）。
 * 请求未要求 stream 时请勿调用。
 */
export function jsonChatCompletionToSseResponse(payload: {
  choices?: Array<{
    finish_reason?: string | null;
    message?: {
      content?: string | null;
      tool_calls?: Array<{
        id?: string;
        type?: string;
        function?: {name?: string; arguments?: string};
      }>;
    };
  }>;
}): Response {
  const choice = payload.choices?.[0];
  const message = choice?.message;
  const events: string[] = [];
  const toolCalls = message?.tool_calls ?? [];
  if (toolCalls.length > 0) {
    for (let i = 0; i < toolCalls.length; i += 1) {
      const tc = toolCalls[i];
      events.push(
        JSON.stringify({
          choices: [
            {
              delta: {
                tool_calls: [
                  {
                    index: i,
                    id: tc.id,
                    function: {
                      name: tc.function?.name,
                      arguments: tc.function?.arguments ?? '{}',
                    },
                  },
                ],
              },
            },
          ],
        }),
      );
    }
    events.push(
      JSON.stringify({
        choices: [
          {
            delta: {},
            finish_reason: choice?.finish_reason ?? 'tool_calls',
          },
        ],
      }),
    );
  } else {
    const text = message?.content ?? '';
    if (text) {
      events.push(
        JSON.stringify({choices: [{delta: {content: text}}]}),
      );
    }
    events.push(
      JSON.stringify({
        choices: [
          {delta: {}, finish_reason: choice?.finish_reason ?? 'stop'},
        ],
      }),
    );
  }
  events.push('[DONE]');
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const e of events) {
        controller.enqueue(encoder.encode(`data: ${e}\n\n`));
      }
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: {'Content-Type': 'text/event-stream'},
  });
}

/** FSM/mock：按请求 stream 字段在 JSON 与 SSE 间切换。 */
export function chatCompletionMockResponse(
  init: RequestInit | undefined,
  payload: {
    choices?: Array<{
      finish_reason?: string | null;
      message?: {
        content?: string | null;
        tool_calls?: Array<{
          id?: string;
          type?: string;
          function?: {name?: string; arguments?: string};
        }>;
      };
    }>;
  },
): Response {
  let wantStream = false;
  try {
    wantStream = Boolean(
      (JSON.parse(String(init?.body ?? '{}')) as {stream?: boolean}).stream,
    );
  } catch {
    wantStream = false;
  }
  if (wantStream) {
    return jsonChatCompletionToSseResponse(payload);
  }
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: {'Content-Type': 'application/json'},
  });
}
