/**
 * 第三百三十三批：诊断 seam 默认共享 host.gate（禁孤儿钟）。
 * Hard 必须关掉 Broker 工具准入；≠ Goal DONE。
 */
import {beforeEach, describe, expect, test, vi} from 'vitest';

vi.mock('./officialAgentLoopHost.js', () => ({
  runOfficialAgentLoopChatDrivenDiagnoseCycleTurn: vi.fn(async () => ({
    trail: [],
    modelRounds: 0,
    laterRoundsCitePriorToolResults: false,
    assistantTexts: [],
    pin: 'wiring-test',
    driver: 'official-dsh-agent-loop+openai-compatible' as const,
    marksGoalDone: false as const,
    scriptedOrder: false,
    chatDrivenOrder: true,
  })),
}));

import {runOfficialAgentLoopChatDrivenDiagnoseCycleTurn} from './officialAgentLoopHost.js';
import {runOfficialChatDrivenDiagnoseBrokerCycle} from './officialChatDrivenDiagnoseBrokerSeam.js';

const activation = {
  kind: 'EXECUTE' as const,
  projectId: '11111111-1111-1111-1111-111111111111',
  activityId: '22222222-2222-2222-2222-222222222222',
  lease: {
    activity_id: '22222222-2222-2222-2222-222222222222',
    attempt_id: '33333333-3333-3333-3333-333333333333',
    fencing_epoch: '7',
  },
};

beforeEach(() => {
  vi.mocked(runOfficialAgentLoopChatDrivenDiagnoseCycleTurn).mockClear();
});

describe('officialChatDrivenDiagnoseBrokerSeam gate wiring', () => {
  test('默认路径：seam.watchdog Hard 关 Broker 工具准入；交给 LLM 的钟即 tool.watchdog', async () => {
    let now = 0;
    const evidence = await runOfficialChatDrivenDiagnoseBrokerCycle({
      chat: {
        baseUrl: 'http://chat.test',
        apiKey: 'k',
        modelId: 'm',
      },
      userPrompt: 'diagnose',
      readPath: 'a.ts',
      writePath: 'a.ts',
      writeContent: 'fixed',
      goalId: 'goal-b333-seam',
      broker: {
        baseUrl: 'http://control.test',
        authorization: 'Bearer worker',
        activation,
        fetchImpl: vi.fn() as unknown as typeof fetch,
        poll: {maxAttempts: 1, delayMs: 0},
        watchdogConfig: {
          now: () => now,
          budgets: {
            awaiting_llm: {softIdleMs: 10, hardIdleMs: 40},
          },
        },
      },
    });

    const turnArg = vi.mocked(runOfficialAgentLoopChatDrivenDiagnoseCycleTurn).mock
      .calls[0]?.[0] as {watchdog: typeof evidence.watchdog};
    expect(turnArg.watchdog).toBe(evidence.watchdog);
    expect(evidence.watchdog).toBe(evidence.gateway.tool.watchdog);
    expect(evidence.progressGuard).toBe(evidence.gateway.tool.progressGuard);
    expect(evidence.marksGoalDone).toBe(false);

    evidence.watchdog.enterPhase('awaiting_llm', 'llm_stream');
    now = 50;
    const hard = evidence.watchdog.tick();
    expect(hard?.kind).toBe('hard_idle');
    expect(hard?.marksGoalDone).toBe(false);

    await expect(
      evidence.gateway.tool.execute({
        callId: 'after-hard',
        name: 'read_file',
        arguments: JSON.stringify({path: 'x.ts'}),
      }),
    ).rejects.toThrow(/TOOL_ADMISSION_CLOSED/);
  });
});
