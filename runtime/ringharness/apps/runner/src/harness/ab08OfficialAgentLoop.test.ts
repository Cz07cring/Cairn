/**
 * AB08 命名缝 × 官方 AgentLoop diagnose：
 * 工具轮次结束后 seal；seal 前挂入本回合，seal 后进新 Turn 种子；不双跑；≠ DONE。
 */
import {existsSync} from 'node:fs';
import {describe, expect, test, vi} from 'vitest';
import {cordisEntryPath} from './cordisBootGate.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';
import {runOfficialAgentLoopChatDrivenDiagnoseCycleTurn} from './officialAgentLoopHost.js';
import {createTurnUserMessageGate} from './turnUserMessageGate.js';
import type {HarnessToolResult} from './brokerBackedHarnessTool.js';

const checkout = process.env.RING_HARNESS_CHECKOUT;
const ready =
  Boolean(checkout) &&
  existsSync(cordisEntryPath(checkout as string)) &&
  isOfficialAgentLoopBuilt(checkout);

function okTool(text: string): HarnessToolResult {
  return {
    content: [{type: 'text', text}],
    isError: false,
    meta: {
      effectId: `eff-${text.slice(0, 8)}`,
      status: 'SUCCEEDED',
      evidenceIds: [],
      requiresReconciliation: false,
    },
  };
}

describe.skipIf(!ready)('AB08 × 官方 AgentLoop diagnose', () => {
  test('工具后 seal 前消息挂入本回合证据，不双跑、≠DONE', async () => {
    const executeTool = vi.fn(async (call: {name: string}) => {
      if (call.name === 'run_tests') {
        // FSM：首测红、次测绿（与注入 FSM 对齐）
        const n = executeTool.mock.calls.filter(
          (c) => (c[0] as {name: string}).name === 'run_tests',
        ).length;
        return okTool(
          n <= 1
            ? 'FAILED tests=1 exit_code=1'
            : 'PASSED tests=1 exit_code=0',
        );
      }
      if (call.name === 'read_file') return okTool('old-src');
      if (call.name === 'write_file') return okTool('wrote');
      return okTool(`ok:${call.name}`);
    });

    const evidence = await runOfficialAgentLoopChatDrivenDiagnoseCycleTurn({
      checkoutDir: checkout as string,
      executeTool,
      userPrompt: '诊断修复',
      readPath: 'order_service/store.py',
      writePath: 'order_service/store.py',
      writeContent: 'fixed',
      useInjectedDecisionFsm: true,
      chat: {
        baseUrl: 'http://ab08-official.test',
        apiKey: 'unused',
        modelId: 'fsm',
      },
      ab08: {
        turnId: 'official-ab08-attach',
        onBetweenToolsAndSeal: async (gate) => {
          const d = await gate.offer({
            messageId: 'm-attach',
            text: 'late-before-seal',
          });
          expect(d.kind).toBe('attached');
          expect(d.marksGoalDone).toBe(false);
        },
      },
    });

    expect(evidence.marksGoalDone).toBe(false);
    expect(evidence.scriptedOrder).toBe(false);
    expect(evidence.turnId).toBe('official-ab08-attach');
    expect(evidence.attachedInjected).toEqual([
      {messageId: 'm-attach', text: 'late-before-seal'},
    ]);
    expect(evidence.deferredTurn).toBeNull();
    expect(evidence.trail.some((t) => t.toolName === 'write_file')).toBe(true);
  }, 120_000);

  test('seal 后迟到消息进新 Turn 种子，本回合不双跑、≠DONE', async () => {
    const executeTool = vi.fn(async (call: {name: string}) => {
      if (call.name === 'run_tests') {
        const n = executeTool.mock.calls.filter(
          (c) => (c[0] as {name: string}).name === 'run_tests',
        ).length;
        return okTool(
          n <= 1
            ? 'FAILED tests=1 exit_code=1'
            : 'PASSED tests=1 exit_code=0',
        );
      }
      if (call.name === 'read_file') return okTool('old-src');
      if (call.name === 'write_file') return okTool('wrote');
      return okTool(`ok:${call.name}`);
    });

    const evidence = await runOfficialAgentLoopChatDrivenDiagnoseCycleTurn({
      checkoutDir: checkout as string,
      executeTool,
      userPrompt: '诊断修复',
      readPath: 'order_service/store.py',
      writePath: 'order_service/store.py',
      writeContent: 'fixed',
      useInjectedDecisionFsm: true,
      chat: {
        baseUrl: 'http://ab08-official.test',
        apiKey: 'unused',
        modelId: 'fsm',
      },
      ab08: {
        messageGate: createTurnUserMessageGate({
          turnId: 'official-ab08-old',
          newTurnId: () => 'official-ab08-new',
        }),
        onAfterSeal: async (gate) => {
          const d = await gate.offer({
            messageId: 'm-def',
            text: 'after-seal',
          });
          expect(d.kind).toBe('deferred_new_turn');
          expect(d.marksGoalDone).toBe(false);
        },
      },
    });

    expect(evidence.marksGoalDone).toBe(false);
    expect(evidence.attachedInjected).toEqual([]);
    expect(evidence.deferredTurn).toEqual({
      turnId: 'official-ab08-new',
      messages: [{messageId: 'm-def', text: 'after-seal'}],
    });
  }, 120_000);
});
