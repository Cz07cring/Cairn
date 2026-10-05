/**
 * Tool Result Envelope：返回模型前的双层预算与持久外溢指针。
 *
 * 截断是销毁，外溢是搬家。完整字节留在已授权 Artifact（通常为 effect evidence）；
 * Prompt 只放 rune 安全预览 + 可取回指针。外溢不证明验收 PASS，也不写 Goal DONE。
 */

import {contentDigestSha256} from './artifactPutHttpPorts.js';

export type ToolResultEnvelopeBudgets = {
  /** 单条结果写入 Prompt 的上限（Unicode code point / rune） */
  maxResultRunes: number;
  /** 同一 Turn / 工具批写入 Prompt 的聚合上限 */
  maxTurnAggregateRunes: number;
  /** 外溢时预览 rune 数 */
  previewRunes: number;
};

export const DEFAULT_TOOL_RESULT_ENVELOPE_BUDGETS: ToolResultEnvelopeBudgets = {
  maxResultRunes: 4_000,
  maxTurnAggregateRunes: 12_000,
  previewRunes: 240,
};

export type ToolResultEnvelopeInput = {
  callId: string;
  effectId: string;
  /** Kernel 已登记的可信结果工件；外溢优先复用，禁止本机明文 spill 目录 */
  evidenceIds: string[];
  fullText: string;
  budgets?: Partial<ToolResultEnvelopeBudgets>;
  /** 聚合预算逼迫外溢时置 true，无视单结果上限 */
  forceSpill?: boolean;
  /**
   * 跨调用 Seen：同一 digest 复用已生成的外溢正文，避免 CAN/重试重复「搬家」语义。
   * 键为 content digest（sha256:…）。
   */
  seen?: Map<string, string>;
};

export type ToolResultEnvelope = {
  promptText: string;
  spilled: boolean;
  digest: string;
  runeCount: number;
  previewRunes: number;
  evidenceIds: string[];
};

export type AggregateEnvelopeItem = {
  callId: string;
  effectId: string;
  evidenceIds: string[];
  fullText: string;
  promptText: string;
  spilled: boolean;
  digest: string;
  runeCount: number;
};

/** Unicode code point 计数（中文 / emoji 各算 1，避免 UTF-16 半码元）。 */
export function countRunes(text: string): number {
  return [...text].length;
}

/** 按 rune 边界截取预览，不劈开 surrogate / 组合序列的码点。 */
export function runeSafePreview(text: string, maxRunes: number): string {
  if (!Number.isInteger(maxRunes) || maxRunes < 0) {
    throw new Error('PREVIEW_RUNES_INVALID');
  }
  const chars = [...text];
  if (chars.length <= maxRunes) {
    return text;
  }
  return `${chars.slice(0, maxRunes).join('')}…`;
}

function resolveBudgets(
  partial?: Partial<ToolResultEnvelopeBudgets>,
): ToolResultEnvelopeBudgets {
  return {
    maxResultRunes:
      partial?.maxResultRunes ?? DEFAULT_TOOL_RESULT_ENVELOPE_BUDGETS.maxResultRunes,
    maxTurnAggregateRunes:
      partial?.maxTurnAggregateRunes ??
      DEFAULT_TOOL_RESULT_ENVELOPE_BUDGETS.maxTurnAggregateRunes,
    previewRunes:
      partial?.previewRunes ?? DEFAULT_TOOL_RESULT_ENVELOPE_BUDGETS.previewRunes,
  };
}

function assertBudgets(budgets: ToolResultEnvelopeBudgets): void {
  for (const [name, value] of Object.entries(budgets) as Array<
    [keyof ToolResultEnvelopeBudgets, number]
  >) {
    if (!Number.isInteger(value) || value < 1) {
      throw new Error(`TOOL_RESULT_BUDGET_INVALID:${name}`);
    }
  }
  if (budgets.previewRunes > budgets.maxResultRunes) {
    throw new Error('TOOL_RESULT_BUDGET_INVALID:previewRunes');
  }
}

/**
 * 构造可取回外溢替身。evidenceIds 必须非空——无 Artifact 指针不得静默截断正文。
 */
export function buildSpillPromptText(input: {
  callId: string;
  effectId: string;
  evidenceIds: string[];
  digest: string;
  runeCount: number;
  fullText: string;
  previewRunes: number;
}): string {
  if (input.evidenceIds.length === 0) {
    throw new Error('TOOL_RESULT_SPILL_REQUIRES_ARTIFACT');
  }
  const preview = runeSafePreview(input.fullText, input.previewRunes);
  const ids = input.evidenceIds.join(',');
  // 替身必须短于常见中等正文；元数据压成少行，避免「外溢反而胀 Prompt」
  return [
    `[tool_result_spilled] call=${input.callId} effect=${input.effectId} artifacts=${ids} digest=${input.digest} runes=${input.runeCount}`,
    `preview: ${preview}`,
    'retrieve: GET /api/v1/artifacts/{artifact_id}/content（须租约；外溢≠PASS/DONE）',
  ].join('\n');
}

/** 单结果预算：超限或 forceSpill 则外溢到 evidence Artifact 指针，否则原样返回。 */
export function envelopeSingleResult(
  input: ToolResultEnvelopeInput,
): ToolResultEnvelope {
  const budgets = resolveBudgets(input.budgets);
  assertBudgets(budgets);
  const digest = contentDigestSha256(input.fullText);
  const runeCount = countRunes(input.fullText);
  const mustSpill = Boolean(input.forceSpill) || runeCount > budgets.maxResultRunes;

  if (!mustSpill) {
    return {
      promptText: input.fullText,
      spilled: false,
      digest,
      runeCount,
      previewRunes: budgets.previewRunes,
      evidenceIds: input.evidenceIds,
    };
  }

  const cached = input.seen?.get(digest);
  if (cached !== undefined) {
    return {
      promptText: cached,
      spilled: true,
      digest,
      runeCount,
      previewRunes: budgets.previewRunes,
      evidenceIds: input.evidenceIds,
    };
  }

  const promptText = buildSpillPromptText({
    callId: input.callId,
    effectId: input.effectId,
    evidenceIds: input.evidenceIds,
    digest,
    runeCount,
    fullText: input.fullText,
    previewRunes: budgets.previewRunes,
  });
  input.seen?.set(digest, promptText);
  return {
    promptText,
    spilled: true,
    digest,
    runeCount,
    previewRunes: budgets.previewRunes,
    evidenceIds: input.evidenceIds,
  };
}

/**
 * 单 Turn 聚合预算：各项可先通过单结果预算，再从最大未外溢项开始外溢，
 * 直到 Prompt 总 rune ≤ maxTurnAggregateRunes（或无可外溢项）。
 */
export function applyTurnAggregateBudget(
  items: AggregateEnvelopeItem[],
  budgets?: Partial<ToolResultEnvelopeBudgets>,
  seen?: Map<string, string>,
): AggregateEnvelopeItem[] {
  const resolved = resolveBudgets(budgets);
  assertBudgets(resolved);
  const out = items.map((item) => ({...item}));

  const promptRunes = () =>
    out.reduce((sum, item) => sum + countRunes(item.promptText), 0);

  while (promptRunes() > resolved.maxTurnAggregateRunes) {
    let bestIdx = -1;
    let bestSaving = 0;
    for (let i = 0; i < out.length; i += 1) {
      const item = out[i]!;
      if (item.spilled) {
        continue;
      }
      if (item.runeCount === 0) {
        continue;
      }
      const candidate = envelopeSingleResult({
        callId: item.callId,
        effectId: item.effectId,
        evidenceIds: item.evidenceIds,
        fullText: item.fullText,
        budgets: resolved,
        forceSpill: true,
        seen,
      });
      const saving =
        countRunes(item.promptText) - countRunes(candidate.promptText);
      // 外溢须真正缩小 Prompt；否则跳过（书：总量是目标而非硬天花板）
      if (saving > bestSaving) {
        bestSaving = saving;
        bestIdx = i;
      }
    }
    if (bestIdx < 0 || bestSaving <= 0) {
      break;
    }
    const target = out[bestIdx]!;
    const spilled = envelopeSingleResult({
      callId: target.callId,
      effectId: target.effectId,
      evidenceIds: target.evidenceIds,
      fullText: target.fullText,
      budgets: resolved,
      forceSpill: true,
      seen,
    });
    out[bestIdx] = {
      ...target,
      promptText: spilled.promptText,
      spilled: true,
      digest: spilled.digest,
    };
  }

  return out;
}

/**
 * Turn 级累加器：串行工具调用时跟踪已返回 Prompt rune，决定下一条是否须强制外溢。
 */
export function createTurnEnvelopeTracker(
  budgets?: Partial<ToolResultEnvelopeBudgets>,
): {
  budgets: ToolResultEnvelopeBudgets;
  seen: Map<string, string>;
  usedPromptRunes: () => number;
  envelopeNext: (input: Omit<ToolResultEnvelopeInput, 'budgets' | 'seen'>) => ToolResultEnvelope;
} {
  const resolved = resolveBudgets(budgets);
  assertBudgets(resolved);
  const seen = new Map<string, string>();
  let used = 0;

  return {
    budgets: resolved,
    seen,
    usedPromptRunes: () => used,
    envelopeNext(input) {
      let final = envelopeSingleResult({
        ...input,
        budgets: resolved,
        seen,
      });
      if (
        !final.spilled &&
        used + countRunes(final.promptText) > resolved.maxTurnAggregateRunes
      ) {
        const forced = envelopeSingleResult({
          ...input,
          budgets: resolved,
          forceSpill: true,
          seen,
        });
        // 仅当外溢真正缩小 Prompt 时采用；否则软超出（禁止胀 Prompt）
        if (countRunes(forced.promptText) < countRunes(final.promptText)) {
          final = forced;
        }
      }
      used += countRunes(final.promptText);
      return final;
    },
  };
}
