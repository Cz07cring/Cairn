/**
 * AB06：硬 ForceStop 后一次零工具 LLM 收口。
 * 消费 NO_PROGRESS_FORCE_STOP_SUMMARY（或文案中的 summary_artifact=），
 * 禁止再暴露工具；输出仅为收口证据，≠ Audit PASS / Goal DONE。
 */
import {
  chatCompletionWithTools,
  type ChatMessage,
  type OpenAiCompatibleToolChatConfig,
} from './openaiCompatibleToolChat.js';

const SUMMARY_ARTIFACT_RE = /summary_artifact=([A-Za-z0-9._:-]+)/;

export type ForceStopZeroToolCloseoutResult = {
  assistantText: string;
  summaryArtifactId: string | null;
  closeoutArtifactId?: string;
  /** 恒空：本回合不得再调工具 */
  toolCalls: [];
  marksGoalDone: false;
  /** chat 失败时用确定性兜底正文 */
  usedFallback: boolean;
};

/**
 * 从 ForceStop 错误 / ToolResult 文案解析 summary_artifact=…。
 */
export function parseSummaryArtifactIdFromForceStopText(
  text: string | null | undefined,
): string | null {
  if (!text) return null;
  const m = text.match(SUMMARY_ARTIFACT_RE);
  const id = m?.[1]?.trim();
  return id || null;
}

export function isHardForceStopCloseoutSignal(texts: readonly string[]): boolean {
  return texts.some(
    (t) =>
      t.includes('TOOL_ADMISSION_CLOSED') || t.includes('禁止再调工具'),
  );
}

function buildCloseoutMessages(input: {
  summaryArtifactId: string | null;
  summaryBody: string | null;
  priorMessages?: readonly ChatMessage[];
}): ChatMessage[] {
  const prior = [...(input.priorMessages ?? [])];
  const summaryBlock =
    input.summaryBody?.trim() ||
    (input.summaryArtifactId
      ? `summary_artifact_id=${input.summaryArtifactId}（正文未取到，请据 id 说明无进展停工）`
      : '（无 summary_artifact；请说明工具准入已因无进展关闭）');
  return [
    ...prior,
    {
      role: 'user',
      content:
        '工具准入已因无进展硬 ForceStop 关闭。请用一两句话做零工具收口总结：' +
        '说明为何停工、禁止再调工具，并明确这不是 Goal DONE / Audit PASS。\n' +
        `ForceStop 总结证据：\n${summaryBlock}`,
    },
  ];
}

const FALLBACK_ASSISTANT =
  '无进展硬 ForceStop：工具准入已关闭；本轮仅作零工具收口证据，≠ Goal DONE / Audit PASS。';

/**
 * 硬 ForceStop 后一次 chat（tools=[]）；可选落盘收口 Artifact。
 */
export async function runForceStopZeroToolCloseout(input: {
  chat: OpenAiCompatibleToolChatConfig;
  summaryArtifactId?: string | null;
  /** 已取到的总结 JSON/正文 */
  summaryBody?: string | null;
  getSummaryContent?: (artifactId: string) => Promise<string>;
  priorMessages?: readonly ChatMessage[];
  /** 可选：把收口正文 PUT 进 collector；失败不阻断 */
  putCloseout?: (body: string) => Promise<{artifactId: string}>;
}): Promise<ForceStopZeroToolCloseoutResult> {
  const summaryArtifactId = input.summaryArtifactId?.trim() || null;
  let summaryBody = input.summaryBody?.trim() || null;
  if (!summaryBody && summaryArtifactId && input.getSummaryContent) {
    try {
      summaryBody = await input.getSummaryContent(summaryArtifactId);
    } catch {
      summaryBody = null;
    }
  }

  const messages = buildCloseoutMessages({
    summaryArtifactId,
    summaryBody,
    priorMessages: input.priorMessages,
  });

  let assistantText = FALLBACK_ASSISTANT;
  let usedFallback = true;
  try {
    const round = await chatCompletionWithTools(input.chat, messages, []);
    const text = (round.assistantText || '').trim();
    if (text) {
      assistantText = text;
      usedFallback = false;
    }
  } catch {
    /* 模型失败：确定性兜底，仍 ≠ DONE */
  }

  let closeoutArtifactId: string | undefined;
  if (input.putCloseout) {
    try {
      const doc = {
        schema_version: 1 as const,
        kind: 'NO_PROGRESS_FORCE_STOP_CLOSEOUT' as const,
        marks_goal_done: false as const,
        allow_tools: false as const,
        summary_artifact_id: summaryArtifactId,
        assistant_text: assistantText,
        used_fallback: usedFallback,
      };
      const put = await input.putCloseout(JSON.stringify(doc));
      closeoutArtifactId = put.artifactId;
    } catch {
      /* 落盘失败不吞收口正文 */
    }
  }

  return {
    assistantText,
    summaryArtifactId,
    ...(closeoutArtifactId ? {closeoutArtifactId} : {}),
    toolCalls: [],
    marksGoalDone: false,
    usedFallback,
  };
}

/**
 * 官方 Loop：若硬 ForceStop 信号成立则跑零工具收口；否则 null。
 * 供 host 在覆盖校验前短路；≠ DONE。
 */
export async function maybeRunHardForceStopCloseout(input: {
  signalTexts: readonly string[];
  chat: OpenAiCompatibleToolChatConfig;
  getSummaryContent?: (artifactId: string) => Promise<string>;
  putCloseout?: (body: string) => Promise<{artifactId: string}>;
  priorMessages?: readonly ChatMessage[];
}): Promise<ForceStopZeroToolCloseoutResult | null> {
  if (!isHardForceStopCloseoutSignal(input.signalTexts)) {
    return null;
  }
  const summaryArtifactId =
    input.signalTexts
      .map((t) => parseSummaryArtifactIdFromForceStopText(t))
      .find((id): id is string => Boolean(id)) ?? null;
  return runForceStopZeroToolCloseout({
    chat: input.chat,
    summaryArtifactId,
    getSummaryContent: input.getSummaryContent,
    putCloseout: input.putCloseout,
    priorMessages: input.priorMessages,
  });
}

export type CordisHardForceStopCloseoutHandled = {
  reason: string;
  closeout: ForceStopZeroToolCloseoutResult;
  marksGoalDone: false;
};

/**
 * Cordis EXECUTE：若错误为硬 ForceStop，跑零工具收口并返回；否则 null（由调用方再抛/转 Hard Idle）。
 */
export async function handleCordisHardForceStopCloseout(input: {
  error: unknown;
  chat: OpenAiCompatibleToolChatConfig;
  getSummaryContent?: (artifactId: string) => Promise<string>;
  putCloseout?: (body: string) => Promise<{artifactId: string}>;
}): Promise<CordisHardForceStopCloseoutHandled | null> {
  const reason =
    input.error instanceof Error ? input.error.message : String(input.error);
  const closeout = await maybeRunHardForceStopCloseout({
    signalTexts: [reason],
    chat: input.chat,
    getSummaryContent: input.getSummaryContent,
    putCloseout: input.putCloseout,
  });
  if (!closeout) {
    return null;
  }
  return {
    reason,
    closeout,
    marksGoalDone: false,
  };
}
