/**
 * 第三百五十六批：read_file 短路径透传 ModelInvocation 账本；≠ DONE。
 */
import {describe, expect, test, vi} from 'vitest';

vi.mock('./officialAgentLoopHost.js', () => ({
  runOfficialAgentLoopLiveReadFileTurn: vi.fn(async (input: {
    modelInvocationLedger?: {contextDigest: string};
  }) => {
    expect(input.modelInvocationLedger?.contextDigest).toBe(
      'sha256:' + 'c'.repeat(64),
    );
    return {
      effectId: 'e1',
      effectStatus: 'SUCCEEDED',
      round2CitesToolResult: true,
      toolCallId: 'c1',
      toolName: 'read_file',
      toolArguments: '{}',
      toolResultText: 'ok',
      modelRounds: 2,
      marksGoalDone: false as const,
      driver: 'official-dsh-agent-loop+openai-compatible',
    };
  }),
}));

vi.mock('./ab01BrokerGateway.js', () => ({
  createAb01BrokerExecuteTool: () => ({
    executeTool: async () => ({
      content: [{type: 'text', text: 'x'}],
      isError: false,
      meta: {effectId: 'e1', status: 'SUCCEEDED', evidenceIds: [], requiresReconciliation: false},
    }),
    tool: {},
  }),
}));

import {runOfficialAb01BrokerReadFileTurn} from './officialAb01BrokerSeam.js';

describe('officialAb01BrokerSeam ledger 透传', () => {
  test('modelInvocationLedger 传到 LiveReadFileTurn；≠DONE', async () => {
    const out = await runOfficialAb01BrokerReadFileTurn({
      chat: {
        baseUrl: 'http://llm',
        apiKey: 'k',
        modelId: 'm',
      },
      userPrompt: 'read',
      expectedPath: 'a.txt',
      modelInvocationLedger: {
        baseUrl: 'http://control',
        authorization: 'Bearer t',
        lease: {
          activity_id: 'a',
          attempt_id: '00000000-0000-4000-8000-000000000099',
          fencing_epoch: '1',
        },
        contextDigest: 'sha256:' + 'c'.repeat(64),
        providerRef: 'deepseek:api',
        modelId: 'm',
      },
      broker: {
        baseUrl: 'http://control',
        authorization: 'Bearer t',
        activation: {
          kind: 'EXECUTE',
          projectId: 'p',
          activityId: 'a',
          lease: {
            activity_id: 'a',
            attempt_id: '00000000-0000-4000-8000-000000000099',
            fencing_epoch: '1',
          },
        },
      },
    });
    expect(out.marksGoalDone).toBe(false);
    expect(out.effectId).toBe('e1');
  });
});
