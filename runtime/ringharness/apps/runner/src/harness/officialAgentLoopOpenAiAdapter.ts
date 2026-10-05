/**
 * 官方 LlmAdapter ← OpenAI 兼容 chat（本机 Qwen / DeepSeek）。
 * 把 GenerateOptions 译成 chat/completions，再合成上游 StreamChunk。
 * ≠ Goal DONE；副作用仍经 executeTool。
 *
 * AB05：可选共享 ActivationWatchdog——有 watchdog 时走真实 SSE（stream:true），
 * 首字节前进 streaming_llm；chunk 间隙 Hard 关准入；可带 partialAssistantText。
 * 无 watchdog 时仍用非流式 chatCompletionWithTools。
 */
import {
  throwIfWatchdogHard,
  type ActivationWatchdog,
} from './activationWatchdog.js';
import {
  chatCompletionWithTools,
  chatCompletionWithToolsStream,
  type ChatMessage,
  type ChatRoundResult,
  type ChatToolDef,
  type OpenAiCompatibleToolChatConfig,
} from './openaiCompatibleToolChat.js';
import {
  createLedgeredChatRunner,
  type ModelInvocationLedgerPorts,
} from './modelInvocationLedger.js';

/** 与 officialAgentLoopHost 脚本块同形的最小 chunk 面。 */
export type OfficialLlmChunk =
  | {type: 'block-start'; index: number; blockType: string}
  | {type: 'text-delta'; index: number; text: string}
  | {type: 'block-end'; index: number; block: unknown}
  | {
      type: 'tool-call-delta';
      index: number;
      id: string;
      name?: string;
      argumentsDelta?: string;
    }
  | {type: 'usage'; usage: {inputTokens: number; outputTokens: number}}
  | {type: 'finish'; reason: {kind: string}};

type HarnessContentBlock = {
  type: string;
  text?: string;
  id?: string;
  name?: string;
  arguments?: string;
  toolCallId?: string;
  content?: HarnessContentBlock[];
  isError?: boolean;
};

type HarnessMessage = {
  role: 'system' | 'user' | 'assistant';
  content: HarnessContentBlock[];
};

type HarnessToolSchema = {
  name: string;
  description?: string;
  parameters?: Record<string, unknown>;
};

type HarnessGenerateOptions = {
  provider: string;
  model: string;
  messages: HarnessMessage[];
  system?: string;
  tools?: HarnessToolSchema[];
  maxTokens?: number;
  signal?: AbortSignal;
};

function flattenText(blocks: readonly HarnessContentBlock[]): string {
  return blocks
    .filter((b) => b.type === 'text' && typeof b.text === 'string')
    .map((b) => b.text as string)
    .join('');
}

function toolResultText(blocks: readonly HarnessContentBlock[]): string {
  return blocks
    .map((b) => {
      if (b.type === 'text') return b.text ?? '';
      if (b.type === 'tool-result' && Array.isArray(b.content)) {
        return toolResultText(b.content);
      }
      return '';
    })
    .join('');
}

/**
 * 上游 Message[] → OpenAI chat messages（tool-result → role:tool）。
 */
export function harnessMessagesToOpenAiChat(
  messages: readonly HarnessMessage[],
  system?: string,
): ChatMessage[] {
  const out: ChatMessage[] = [];
  if (system?.trim()) {
    out.push({role: 'system', content: system.trim()});
  }
  for (const message of messages) {
    if (message.role === 'system') {
      const text = flattenText(message.content);
      if (text) out.push({role: 'system', content: text});
      continue;
    }
    if (message.role === 'assistant') {
      const text = flattenText(message.content);
      const toolCalls = message.content.filter((b) => b.type === 'tool-call');
      if (toolCalls.length === 0) {
        out.push({role: 'assistant', content: text});
        continue;
      }
      out.push({
        role: 'assistant',
        content: text || null,
        tool_calls: toolCalls.map((b) => ({
          id: String(b.id ?? ''),
          type: 'function' as const,
          function: {
            name: String(b.name ?? ''),
            arguments: String(b.arguments ?? '{}'),
          },
        })),
      });
      continue;
    }
    // user：普通文本 + 可能的 tool-result 块
    const text = flattenText(message.content);
    const results = message.content.filter((b) => b.type === 'tool-result');
    if (text.length > 0 || results.length === 0) {
      out.push({role: 'user', content: text});
    }
    for (const result of results) {
      const callId = String(result.toolCallId ?? '');
      if (!callId) {
        throw new Error('OFFICIAL_LLM_TOOL_RESULT_NO_CALL_ID');
      }
      out.push({
        role: 'tool',
        tool_call_id: callId,
        content: toolResultText(result.content ?? []) || '(no output)',
      });
    }
  }
  return out;
}

export function harnessToolsToOpenAi(
  tools: readonly HarnessToolSchema[] | undefined,
): ChatToolDef[] {
  if (!tools?.length) return [];
  return tools.map((t) => ({
    type: 'function',
    function: {
      name: t.name,
      description: t.description,
      parameters: t.parameters ?? {type: 'object', properties: {}},
    },
  }));
}

export function chatRoundToOfficialLlmChunks(
  round: ChatRoundResult,
  ToolCallId: (raw: string) => string,
): OfficialLlmChunk[] {
  if (round.toolCalls.length > 0) {
    const chunks: OfficialLlmChunk[] = [];
    round.toolCalls.forEach((call, index) => {
      const callId = ToolCallId(call.id);
      chunks.push({type: 'block-start', index, blockType: 'tool-call'});
      chunks.push({
        type: 'tool-call-delta',
        index,
        id: callId,
        name: call.name,
        argumentsDelta: call.arguments,
      });
      chunks.push({
        type: 'block-end',
        index,
        block: {
          type: 'tool-call',
          id: callId,
          name: call.name,
          arguments: call.arguments,
        },
      });
    });
    chunks.push({
      type: 'usage',
      usage: {inputTokens: 1, outputTokens: round.toolCalls.length},
    });
    chunks.push({type: 'finish', reason: {kind: 'tool-calls'}});
    return chunks;
  }
  const text = round.assistantText || '';
  return [
    {type: 'block-start', index: 0, blockType: 'text'},
    {type: 'text-delta', index: 0, text},
    {type: 'block-end', index: 0, block: {type: 'text', text}},
    {
      type: 'usage',
      usage: {inputTokens: 1, outputTokens: Math.max(1, text.length)},
    },
    {type: 'finish', reason: {kind: 'stop'}},
  ];
}

export type OpenAiCompatibleAdapterHandle = {
  adapter: unknown;
  /** 已完成的 chat 轮次（含工具回灌后的第二轮） */
  rounds: ChatRoundResult[];
  /** 每轮发给模型的 messages（用于证明 ToolResult 回灌，非只看响应） */
  requestMessages: ChatMessage[][];
  /**
   * Issue #70：账本/chat 在 stream 内抛错时，AgentLoop 可能吞掉并 idle，
   * 导致 loopError=(none)。此处保留最近一次 stream 错误供宿主失败关闭。
   */
  lastStreamError: () => unknown;
};

/** AB05：官方 adapter 与 Broker 工具共用同一 ActivationWatchdog。 */
export type OfficialAdapterWatchdogOpts = {
  watchdog: ActivationWatchdog;
  tickEveryMs?: number;
  setIntervalFn?: typeof setInterval;
  clearIntervalFn?: typeof clearInterval;
};

/**
 * 动态子类化上游 LlmAdapter：每轮 stream → 一次 chatCompletionWithTools。
 * 可选 ledger：Runner 自持 chat 仍登记 ModelInvocation（Codex P0-1）；≠ DONE。
 */
export function createOpenAiCompatibleOfficialAdapter(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  LlmAdapterBase: any,
  ToolCallId: (raw: string) => string,
  chat: OpenAiCompatibleToolChatConfig,
  watchdogOpts?: OfficialAdapterWatchdogOpts,
  ledgerPorts?: ModelInvocationLedgerPorts,
): OpenAiCompatibleAdapterHandle {
  const rounds: ChatRoundResult[] = [];
  const requestMessages: ChatMessage[][] = [];
  let lastError: unknown;
  const tickOpts = watchdogOpts
    ? {
        tickEveryMs: watchdogOpts.tickEveryMs,
        setIntervalFn: watchdogOpts.setIntervalFn,
        clearIntervalFn: watchdogOpts.clearIntervalFn,
      }
    : undefined;
  const ledger = ledgerPorts
    ? createLedgeredChatRunner(ledgerPorts)
    : null;

  class OpenAiCompatibleOfficialAdapter extends LlmAdapterBase {
    async *stream(options: HarnessGenerateOptions) {
      if (options.signal?.aborted) {
        throw new Error('OFFICIAL_LLM_ABORTED');
      }
      const messages = harnessMessagesToOpenAiChat(
        options.messages,
        options.system,
      );
      requestMessages.push(messages);
      const tools = harnessToolsToOpenAi(options.tools);
      const watchdog = watchdogOpts?.watchdog;
      if (watchdog) {
        watchdog.enterPhase('awaiting_llm', 'llm_provider');
      }
      const chatCfg = {
        ...chat,
        modelId: options.model || chat.modelId,
        maxTokens: options.maxTokens ?? chat.maxTokens ?? 512,
      };
      const runChat = () =>
        watchdog
          ? chatCompletionWithToolsStream(chatCfg, messages, tools, {
              watchdog,
              tickEveryMs: tickOpts?.tickEveryMs,
              setIntervalFn: tickOpts?.setIntervalFn,
              clearIntervalFn: tickOpts?.clearIntervalFn,
            })
          : chatCompletionWithTools(chatCfg, messages, tools);
      let round: ChatRoundResult;
      try {
        round = ledger
          ? await ledger.runRound(runChat, messages, tools)
          : await runChat();
      } catch (err) {
        lastError = err;
        throw err;
      }
      // 非 SSE 路径仍需在假 yield 前进 streaming；SSE 已在首字节进入
      if (watchdog && watchdog.heartbeatDetails().phase !== 'streaming_llm') {
        watchdog.enterPhase('streaming_llm', 'llm_stream');
      }
      watchdog?.reportProgress('llm_stream');
      rounds.push(round);
      for (const chunk of chatRoundToOfficialLlmChunks(round, ToolCallId)) {
        if (watchdog) {
          watchdog.reportProgress('llm_stream');
          throwIfWatchdogHard(watchdog);
        }
        yield chunk;
      }
    }
  }

  return {
    adapter: new OpenAiCompatibleOfficialAdapter(),
    rounds,
    requestMessages,
    lastStreamError: () => lastError,
  };
}
