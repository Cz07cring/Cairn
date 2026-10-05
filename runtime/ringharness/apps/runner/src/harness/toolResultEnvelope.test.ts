import {expect, test} from 'vitest';
import {contentDigestSha256} from './artifactPutHttpPorts.js';
import {
  applyTurnAggregateBudget,
  countRunes,
  createTurnEnvelopeTracker,
  envelopeSingleResult,
  runeSafePreview,
} from './toolResultEnvelope.js';

test('countRunes：中文与 emoji 按 code point 计，不按 UTF-16', () => {
  expect(countRunes('你好')).toBe(2);
  expect(countRunes('🙂')).toBe(1);
  expect(countRunes('中文🙂测')).toBe(4);
});

test('runeSafePreview：不劈开 surrogate，超限加省略号', () => {
  expect(runeSafePreview('你好世界', 2)).toBe('你好…');
  expect(runeSafePreview('a🙂b', 2)).toBe('a🙂…');
  expect(runeSafePreview('短', 10)).toBe('短');
});

test('envelopeSingleResult：未超预算原样返回', () => {
  const env = envelopeSingleResult({
    callId: 'c1',
    effectId: 'e1',
    evidenceIds: ['art-1'],
    fullText: '文件正文',
    budgets: {maxResultRunes: 100, previewRunes: 10},
  });
  expect(env.spilled).toBe(false);
  expect(env.promptText).toBe('文件正文');
  expect(env.digest).toBe(contentDigestSha256('文件正文'));
});

test('envelopeSingleResult：超预算外溢到 evidence 指针且含 rune 安全预览', () => {
  const fullText = '甲'.repeat(20) + '乙'.repeat(5);
  const env = envelopeSingleResult({
    callId: 'call-zh',
    effectId: 'effect-zh',
    evidenceIds: ['result-artifact'],
    fullText,
    budgets: {maxResultRunes: 10, previewRunes: 4},
  });
  expect(env.spilled).toBe(true);
  expect(env.promptText).toContain('[tool_result_spilled]');
  expect(env.promptText).toContain('artifacts=result-artifact');
  expect(env.promptText).toContain(`digest=${contentDigestSha256(fullText)}`);
  expect(env.promptText).toContain('preview: 甲甲甲甲…');
  expect(env.promptText).toContain('外溢≠PASS/DONE');
  // 外溢正文不得等于全文（禁止静默截断冒充完整）
  expect(env.promptText).not.toBe(fullText);
  expect(env.promptText.includes(fullText)).toBe(false);
});

test('envelopeSingleResult：无 evidence 不得外溢截断', () => {
  expect(() =>
    envelopeSingleResult({
      callId: 'c',
      effectId: 'e',
      evidenceIds: [],
      fullText: 'x'.repeat(100),
      budgets: {maxResultRunes: 1, previewRunes: 1},
    }),
  ).toThrow(/TOOL_RESULT_SPILL_REQUIRES_ARTIFACT/);
});

test('Seen：同一 digest 复用外溢正文', () => {
  const seen = new Map<string, string>();
  const fullText = '大'.repeat(50);
  const a = envelopeSingleResult({
    callId: 'c1',
    effectId: 'e1',
    evidenceIds: ['a1'],
    fullText,
    budgets: {maxResultRunes: 5, previewRunes: 2},
    seen,
  });
  const b = envelopeSingleResult({
    callId: 'c2',
    effectId: 'e2',
    evidenceIds: ['a1'],
    fullText,
    budgets: {maxResultRunes: 5, previewRunes: 2},
    seen,
  });
  expect(a.spilled).toBe(true);
  expect(b.promptText).toBe(a.promptText);
  expect(seen.size).toBe(1);
});

test('applyTurnAggregateBudget：十个中等结果单项合规、总量超标时从最大项外溢', () => {
  const items = Array.from({length: 10}, (_, i) => {
    // 正文须明显长于紧凑外溢替身，否则外溢无节省（书：总量是目标）
    const fullText = `块${i}:` + '文'.repeat(400);
    return {
      callId: `call-${i}`,
      effectId: `effect-${i}`,
      evidenceIds: [`art-${i}`],
      fullText,
      promptText: fullText,
      spilled: false,
      digest: contentDigestSha256(fullText),
      runeCount: countRunes(fullText),
    };
  });
  const originalTotal = items.reduce((s, x) => s + countRunes(x.promptText), 0);
  // 单项约 404 < 500；10*404≈4040 超 2800 → 外溢后 Prompt 须压到预算内
  const out = applyTurnAggregateBudget(items, {
    maxResultRunes: 500,
    maxTurnAggregateRunes: 2800,
    previewRunes: 20,
  });
  const spilled = out.filter((x) => x.spilled);
  expect(spilled.length).toBeGreaterThan(0);
  const promptTotal = out.reduce((s, x) => s + countRunes(x.promptText), 0);
  expect(promptTotal).toBeLessThan(originalTotal);
  expect(promptTotal).toBeLessThanOrEqual(2800);
  for (const item of spilled) {
    expect(item.promptText).toContain('[tool_result_spilled]');
    expect(item.promptText).toContain(`artifacts=${item.evidenceIds[0]}`);
    expect(item.promptText.includes(item.fullText)).toBe(false);
  }
  for (const item of out.filter((x) => !x.spilled)) {
    expect(item.promptText).toBe(item.fullText);
  }
});

test('createTurnEnvelopeTracker：串行累加逼近聚合上限时强制外溢', () => {
  const tracker = createTurnEnvelopeTracker({
    maxResultRunes: 500,
    maxTurnAggregateRunes: 600,
    previewRunes: 8,
  });
  const first = tracker.envelopeNext({
    callId: 'a',
    effectId: 'ea',
    evidenceIds: ['art-a'],
    fullText: '甲'.repeat(350),
  });
  expect(first.spilled).toBe(false);
  const second = tracker.envelopeNext({
    callId: 'b',
    effectId: 'eb',
    evidenceIds: ['art-b'],
    fullText: '乙'.repeat(350),
  });
  expect(second.spilled).toBe(true);
  expect(tracker.usedPromptRunes()).toBe(
    countRunes(first.promptText) + countRunes(second.promptText),
  );
});
