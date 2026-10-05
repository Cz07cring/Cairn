/**
 * AB01 两轮缝：模型发 read_file → Broker-backed 工具 → ToolResult 按 tool_call_id 回灌 → 第二轮模型。
 *
 * 明确边界：
 * - 这是 Harness 侧最小串行两轮驱动，**不是**官方 AgentLoop，**不得**写入 RunActivation 默认路径冒充「Harness 接入完成」。
 * - marksGoalDone 恒为 false；ToolResult / 第二轮正文 ≠ Goal DONE。
 * - AB08：工具完成后、第二轮前经 TurnUserMessageGate seal；挂上的消息注入本轮，seal 后迟到消息只进新 Turn 种子。
 */
import type {HarnessToolCall, HarnessToolResult} from './brokerBackedHarnessTool.js';
import {
  chatCompletionWithTools,
  type ChatMessage,
  type ChatRoundResult,
  type ChatToolDef,
  type OpenAiCompatibleToolChatConfig,
} from './openaiCompatibleToolChat.js';
import {
  createTurnUserMessageGate,
  type TurnUserMessage,
  type TurnUserMessageGate,
} from './turnUserMessageGate.js';

export const AB01_READ_FILE_TOOL: ChatToolDef = {
  type: 'function',
  function: {
    name: 'read_file',
    description: '读取工作区内一个文本文件并返回内容',
    parameters: {
      type: 'object',
      properties: {
        path: {type: 'string', description: '相对路径'},
      },
      required: ['path'],
    },
  },
};

export type Ab01ExecuteTool = (call: HarnessToolCall) => Promise<HarnessToolResult>;

export type Ab01TwoTurnInput = {
  chat: OpenAiCompatibleToolChatConfig;
  executeTool: Ab01ExecuteTool;
  /** 要求模型调用 read_file 的用户提示 */
  userPrompt: string;
  /** 期望的工具路径（写入提示与断言） */
  expectedPath: string;
  /** 第二轮追问：要求引用工具返回的原文 */
  round2Prompt?: string;
  systemPrompt?: string;
  /** 本轮 Turn id；默认 ab01-turn */
  turnId?: string;
  /** 复用已有 gate（测竞态）；默认按 turnId 新建 */
  messageGate?: TurnUserMessageGate;
  /** 工具成功后、seal 前：可 offer 迟到用户消息（挂入本 Turn） */
  onBetweenToolAndSeal?: (gate: TurnUserMessageGate) => Promise<void>;
  /** seal 后、第二轮前：可 offer（只能 deferred_new_turn） */
  onAfterSeal?: (gate: TurnUserMessageGate) => Promise<void>;
};

export type Ab01TwoTurnEvidence = {
  toolCallId: string;
  toolName: string;
  toolArguments: string;
  effectId: string;
  effectStatus: string;
  toolResultText: string;
  round1: ChatRoundResult;
  round2: ChatRoundResult;
  /** 第二轮正文是否包含工具结果中的可识别片段 */
  round2CitesToolResult: boolean;
  turnId: string;
  /** seal 时挂上并注入第二轮的用户消息 */
  attachedInjected: readonly TurnUserMessage[];
  /** seal 后迟到消息形成的新 Turn 种子（本轮不跑） */
  deferredTurn: {
    turnId: string;
    messages: readonly TurnUserMessage[];
  } | null;
  marksGoalDone: false;
};

function toolResultText(result: HarnessToolResult): string {
  return result.content.map((c) => c.text).join('\n');
}

/** 从工具结果抽出用于「是否引用」判定的候选片段（跳过 spill / nudge）。 */
export function citationNeedlesFromToolResult(text: string): string[] {
  return text
    .split('\n')
    .map((l) => l.trim())
    .filter(
      (l) =>
        l.length >= 6 &&
        !l.startsWith('[tool_result_spilled]') &&
        !l.startsWith('[no_progress'),
    );
}

export function citationNeedleFromToolResult(text: string): string {
  const needles = citationNeedlesFromToolResult(text);
  const candidate = needles[0] || text.trim();
  if (candidate.length <= 48) {
    return candidate;
  }
  return candidate.slice(0, 48);
}

export function assistantCitesToolResult(assistantText: string, toolResultText: string): boolean {
  const needles = citationNeedlesFromToolResult(toolResultText);
  if (needles.length === 0) {
    return assistantText.includes(toolResultText.trim().slice(0, 48));
  }
  return needles.some((n) => assistantText.includes(n));
}

/**
 * 组装第二轮 messages：assistant.tool_calls + tool(tool_call_id) + 可选追问 + AB08 挂入消息。
 */
export function buildAb01Round2Messages(input: {
  prior: ChatMessage[];
  round1: ChatRoundResult;
  toolCallId: string;
  toolName: string;
  toolArguments: string;
  toolResultText: string;
  followUp?: string;
  /** seal 前挂上的用户消息；注入本轮，不得与 deferred 双跑 */
  attachedUserMessages?: readonly TurnUserMessage[];
}): ChatMessage[] {
  const assistantWithCalls: ChatMessage = {
    role: 'assistant',
    content: input.round1.assistantText || null,
    tool_calls: [
      {
        id: input.toolCallId,
        type: 'function',
        function: {name: input.toolName, arguments: input.toolArguments},
      },
    ],
  };
  const toolMsg: ChatMessage = {
    role: 'tool',
    tool_call_id: input.toolCallId,
    content: input.toolResultText,
  };
  const out: ChatMessage[] = [...input.prior, assistantWithCalls, toolMsg];
  if (input.followUp?.trim()) {
    out.push({role: 'user', content: input.followUp.trim()});
  }
  for (const m of input.attachedUserMessages ?? []) {
    const text = m.text.trim();
    if (text) {
      out.push({role: 'user', content: text});
    }
  }
  return out;
}

/**
 * 跑 AB01：真实 chat（须带 tools）→ 恰好一次 read_file → executeTool → 第二轮引用。
 */
export async function runAb01ReadFileTwoTurn(
  input: Ab01TwoTurnInput,
): Promise<Ab01TwoTurnEvidence> {
  const turnId = input.turnId?.trim() || input.messageGate?.turnId || 'ab01-turn';
  const gate =
    input.messageGate ?? createTurnUserMessageGate({turnId});

  const system =
    input.systemPrompt ??
    '你是执行助手。必须通过工具 read_file 读取文件；不要臆造文件内容。';
  const prior: ChatMessage[] = [
    {role: 'system', content: system},
    {
      role: 'user',
      content: input.userPrompt,
    },
  ];

  const round1 = await chatCompletionWithTools(input.chat, prior, [AB01_READ_FILE_TOOL]);
  const readCalls = round1.toolCalls.filter((c) => c.name === 'read_file');
  if (readCalls.length !== 1) {
    throw new Error(
      `AB01_NO_SINGLE_READ_FILE: toolCalls=${JSON.stringify(round1.toolCalls)} text=${round1.assistantText.slice(0, 200)}`,
    );
  }
  const call = readCalls[0]!;
  let argsObj: {path?: string};
  try {
    argsObj = JSON.parse(call.arguments || '{}') as {path?: string};
  } catch {
    throw new Error(`AB01_BAD_ARGUMENTS: ${call.arguments}`);
  }
  if ((argsObj.path || '').trim() !== input.expectedPath) {
    throw new Error(
      `AB01_PATH_MISMATCH: got=${argsObj.path} expected=${input.expectedPath}`,
    );
  }

  const toolResult = await input.executeTool({
    callId: call.id,
    name: call.name,
    arguments: call.arguments,
  });
  if (toolResult.isError) {
    throw new Error(
      `AB01_TOOL_ERROR: ${toolResult.error?.info.code ?? 'error'} ${toolResult.error?.message ?? ''}`,
    );
  }
  const resultText = toolResultText(toolResult);
  if (!resultText.trim()) {
    throw new Error('AB01_EMPTY_TOOL_RESULT');
  }

  if (input.onBetweenToolAndSeal) {
    await input.onBetweenToolAndSeal(gate);
  }
  const sealed = await gate.seal();
  if (sealed.marksGoalDone !== false) {
    throw new Error('AB08_SEAL_MARKED_GOAL_DONE');
  }
  if (input.onAfterSeal) {
    await input.onAfterSeal(gate);
  }
  const deferred = gate.takeDeferredTurn();
  // 不双跑：deferred 消息不得再出现在 attachedInjected
  if (deferred) {
    const deferredIds = new Set(deferred.messages.map((m) => m.messageId));
    for (const m of sealed.attached) {
      if (deferredIds.has(m.messageId)) {
        throw new Error(`AB08_DOUBLE_RUN: ${m.messageId}`);
      }
    }
  }

  const followUp =
    input.round2Prompt ??
    '根据工具返回的原文，原样复述其中以 RING_ 开头的标记字符串（禁止改写、翻译或省略）。';
  const round2Messages = buildAb01Round2Messages({
    prior,
    round1,
    toolCallId: call.id,
    toolName: call.name,
    toolArguments: call.arguments,
    toolResultText: resultText,
    followUp,
    attachedUserMessages: sealed.attached,
  });
  const round2 = await chatCompletionWithTools(input.chat, round2Messages, []);
  const cites = assistantCitesToolResult(round2.assistantText, resultText);

  return {
    toolCallId: call.id,
    toolName: call.name,
    toolArguments: call.arguments,
    effectId: toolResult.meta.effectId,
    effectStatus: toolResult.meta.status,
    toolResultText: resultText,
    round1,
    round2,
    round2CitesToolResult: cites,
    turnId: gate.turnId,
    attachedInjected: sealed.attached,
    deferredTurn: deferred,
    marksGoalDone: false,
  };
}
