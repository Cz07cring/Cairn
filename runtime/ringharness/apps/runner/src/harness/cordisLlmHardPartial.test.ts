/**
 * 第三百三十六批：Cordis Kernel LLM 流 Hard 时累计 partialAssistantText。
 */
import {describe, expect, test, vi} from 'vitest';
import {
  createActivationWatchdog,
  WatchdogHardIdleError,
} from './activationWatchdog.js';
import {invokeKernelLlmStreamWithWatchdog} from './cordisLlmBridge.js';
import {createHeartbeatLinkedAdmissionGate} from './brokerToolHttpPorts.js';

describe('cordisLlmBridge Hard partial', () => {
  test('streaming_llm Hard 时错误携带已产出正文；≠DONE', async () => {
    const nowRef = {now: 0};
    const gate = createHeartbeatLinkedAdmissionGate();
    const wd = createActivationWatchdog({
      now: () => nowRef.now,
      gate,
      budgets: {
        awaiting_llm: {softIdleMs: 60_000, hardIdleMs: 120_000},
        streaming_llm: {softIdleMs: 5, hardIdleMs: 10},
      },
    });

    const chunks: unknown[] = [];
    let caught: unknown;
    try {
      for await (const c of invokeKernelLlmStreamWithWatchdog(
        {
          createInvocation: async () => ({invocationId: 'inv-p'}),
          completeInvocation: async () => ({
            text: '',
            chunks: [
              {type: 'text-delta' as const, text: 'hel'},
              {type: 'text-delta' as const, text: 'lo'},
              {type: 'finish' as const, reason: 'stop' as const},
            ],
          }),
        },
        {
          lease: {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
          contextDigest: 'sha256:' + 'c'.repeat(64),
          providerRef: 'ring-kernel',
          modelId: 'm',
          messages: [{role: 'user', content: 'hi'}],
          toolsExposedToModel: [],
        },
        {watchdog: wd},
      )) {
        chunks.push(c);
        // 首个 text-delta 后推进时钟逼 Hard，下一轮 throwIfWatchdogHard
        if (
          typeof c === 'object' &&
          c &&
          'type' in c &&
          (c as {type: string}).type === 'text-delta'
        ) {
          nowRef.now = 20;
        }
      }
    } catch (err) {
      caught = err;
    }

    expect(caught).toBeInstanceOf(WatchdogHardIdleError);
    const hard = caught as WatchdogHardIdleError;
    expect(hard.partialAssistantText).toBe('hel');
    expect(hard.outcome.marksGoalDone).toBe(false);
    expect(gate.allowed()).toBe(false);
    expect(chunks.length).toBeGreaterThanOrEqual(1);
  });

  test('无 Hard 时完整流过；mock create 被调用', async () => {
    const createInvocation = vi.fn(async () => ({invocationId: 'inv-ok'}));
    const out: Array<{type: string}> = [];
    for await (const c of invokeKernelLlmStreamWithWatchdog(
      {
        createInvocation,
        completeInvocation: async () => ({text: 'ok'}),
      },
      {
        lease: {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
        contextDigest: 'sha256:' + 'd'.repeat(64),
        providerRef: 'ring-kernel',
        modelId: 'm',
        messages: [{role: 'user', content: 'hi'}],
        toolsExposedToModel: [],
      },
    )) {
      out.push(c);
    }
    expect(createInvocation).toHaveBeenCalledOnce();
    expect(out.some((c) => c.type === 'finish')).toBe(true);
  });
});
