/**
 * 官方 AgentLoop + Broker-backed read_file 验收缝。
 * 缺 build:lib:host → skip；≠ Goal DONE；不写入 RunActivation 默认路径。
 */
import {existsSync} from 'node:fs';
import {describe, expect, test} from 'vitest';
import type {HarnessToolResult} from './brokerBackedHarnessTool.js';
import {cordisEntryPath} from './cordisBootGate.js';
import {
  isOfficialAgentLoopBuilt,
  runOfficialAgentLoopReadFileTurn,
  runOfficialAgentLoopWriteThenRunTestsTurn,
} from './officialAgentLoopHost.js';
import {resolveHarnessPin} from './pin.js';

const checkout = process.env.RING_HARNESS_CHECKOUT;
const agentLoopBuilt =
  Boolean(checkout) &&
  existsSync(cordisEntryPath(checkout as string)) &&
  isOfficialAgentLoopBuilt(checkout);

describe('officialAgentLoopHost', () => {
  test('未 build 时 isOfficialAgentLoopBuilt 为 false 或环境缺省', () => {
    expect(typeof isOfficialAgentLoopBuilt()).toBe('boolean');
  });

  test.skipIf(!agentLoopBuilt)(
    '官方 AgentLoop 驱动 read_file → ToolResult → 第二轮；marksGoalDone=false',
    async () => {
      const expectedPath = 'src/official_loop.ts';
      const fileBody = 'OFFICIAL_LOOP_BODY_42';
      const evidence = await runOfficialAgentLoopReadFileTurn({
        checkoutDir: checkout,
        userPrompt: `请调用 read_file 读取 ${expectedPath}`,
        expectedPath,
        idleTimeoutMs: 25_000,
        executeTool: async (call) => {
          expect(call.name).toBe('read_file');
          const args = JSON.parse(call.arguments) as {path: string};
          expect(args.path).toBe(expectedPath);
          const result: HarnessToolResult = {
            content: [{type: 'text', text: fileBody}],
            isError: false,
            meta: {
              effectId: 'effect-official-1',
              status: 'SUCCEEDED',
              evidenceIds: ['ev-1'],
              requiresReconciliation: false,
            },
          };
          return result;
        },
      });

      expect(evidence.driver).toBe('official-dsh-agent-loop');
      expect(evidence.pin).toBe(resolveHarnessPin());
      expect(evidence.marksGoalDone).toBe(false);
      expect(evidence.toolName).toBe('read_file');
      expect(evidence.toolResultText).toBe(fileBody);
      expect(evidence.effectId).toBe('effect-official-1');
      expect(evidence.effectStatus).toBe('SUCCEEDED');
      expect(evidence.modelRounds).toBeGreaterThanOrEqual(2);
      expect(evidence.round2CitesToolResult).toBe(true);
      expect(evidence.toolIsError).toBe(false);
    },
    30_000,
  );

  test.skipIf(!agentLoopBuilt)(
    '官方 AgentLoop 多工具：write_file→run_tests→终轮；scriptedOrder；≠DONE',
    async () => {
      const writePath = 'order_service/store.py';
      const writeContent = 'FIXED_STORE_BODY\n';
      let seq = 0;
      const evidence = await runOfficialAgentLoopWriteThenRunTestsTurn({
        checkoutDir: checkout,
        userPrompt: `先 write_file ${writePath}，再 run_tests suite=public`,
        writePath,
        writeContent,
        idleTimeoutMs: 30_000,
        executeTool: async (call) => {
          seq += 1;
          if (seq === 1) {
            expect(call.name).toBe('write_file');
            const args = JSON.parse(call.arguments) as {
              path: string;
              content: string;
            };
            expect(args.path).toBe(writePath);
            expect(args.content).toBe(writeContent);
            return {
              content: [{type: 'text', text: 'WRITE_OK_MARKER after_digest=abc'}],
              isError: false,
              meta: {
                effectId: 'effect-write-1',
                status: 'SUCCEEDED',
                evidenceIds: ['ev-w'],
                requiresReconciliation: false,
              },
            } satisfies HarnessToolResult;
          }
          expect(call.name).toBe('run_tests');
          const args = JSON.parse(call.arguments) as {suite: string};
          expect(args.suite).toBe('public');
          return {
            content: [{type: 'text', text: 'exit_code=0 suite=public'}],
            isError: false,
            meta: {
              effectId: 'effect-tests-1',
              status: 'SUCCEEDED',
              evidenceIds: ['ev-t'],
              requiresReconciliation: false,
            },
          } satisfies HarnessToolResult;
        },
      });

      expect(evidence.scriptedOrder).toBe(true);
      expect(evidence.marksGoalDone).toBe(false);
      expect(evidence.driver).toBe('official-dsh-agent-loop');
      expect(evidence.pin).toBe(resolveHarnessPin());
      expect(evidence.trail.map((t) => t.toolName)).toEqual([
        'write_file',
        'run_tests',
      ]);
      expect(evidence.trail[0]?.effectId).toBe('effect-write-1');
      expect(evidence.trail[1]?.effectId).toBe('effect-tests-1');
      expect(evidence.modelRounds).toBeGreaterThanOrEqual(3);
      expect(evidence.laterRoundsCitePriorToolResults).toBe(true);
    },
    40_000,
  );

  test.skipIf(!agentLoopBuilt)(
    '官方 AgentLoop：write→run_tests→seal_candidate；scriptedOrder；≠DONE',
    async () => {
      const writePath = 'order_service/store.py';
      const writeContent = 'FIXED_STORE_BODY\n';
      const profileId = '44444444-4444-4444-4444-444444444444';
      let seq = 0;
      const evidence = await runOfficialAgentLoopWriteThenRunTestsTurn({
        checkoutDir: checkout,
        userPrompt: `write→run_tests→seal`,
        writePath,
        writeContent,
        sealVerificationProfileIds: [profileId],
        idleTimeoutMs: 30_000,
        executeTool: async (call) => {
          seq += 1;
          if (seq === 1) {
            expect(call.name).toBe('write_file');
            return {
              content: [{type: 'text', text: 'WRITE_OK_MARKER'}],
              isError: false,
              meta: {
                effectId: 'effect-write-1',
                status: 'SUCCEEDED',
                evidenceIds: ['ev-w'],
                requiresReconciliation: false,
              },
            } satisfies HarnessToolResult;
          }
          if (seq === 2) {
            expect(call.name).toBe('run_tests');
            return {
              content: [{type: 'text', text: 'exit_code=0 suite=public'}],
              isError: false,
              meta: {
                effectId: 'effect-tests-1',
                status: 'SUCCEEDED',
                evidenceIds: ['ev-t'],
                requiresReconciliation: false,
              },
            } satisfies HarnessToolResult;
          }
          expect(call.name).toBe('seal_candidate');
          const args = JSON.parse(call.arguments) as {
            verification_profile_ids: string[];
          };
          expect(args.verification_profile_ids).toEqual([profileId]);
          return {
            content: [{type: 'text', text: 'SEAL_OK_MARKER candidate=c1'}],
            isError: false,
            meta: {
              effectId: 'effect-seal-1',
              status: 'SUCCEEDED',
              evidenceIds: ['ev-s'],
              requiresReconciliation: false,
            },
          } satisfies HarnessToolResult;
        },
      });

      expect(evidence.scriptedOrder).toBe(true);
      expect(evidence.marksGoalDone).toBe(false);
      expect(evidence.trail.map((t) => t.toolName)).toEqual([
        'write_file',
        'run_tests',
        'seal_candidate',
      ]);
      expect(evidence.trail[2]?.effectId).toBe('effect-seal-1');
      expect(evidence.modelRounds).toBeGreaterThanOrEqual(4);
      expect(evidence.laterRoundsCitePriorToolResults).toBe(true);
    },
    40_000,
  );

  test.skipIf(!agentLoopBuilt)(
    '官方 AgentLoop 诊断全周期：read→红测→write→绿测→seal；scriptedOrder；≠DONE',
    async () => {
      const readPath = 'order_service/store.py';
      const writePath = 'order_service/store.py';
      const writeContent = 'FIXED_STORE_BODY\n';
      const profileId = '44444444-4444-4444-4444-444444444444';
      let seq = 0;
      const evidence = await runOfficialAgentLoopWriteThenRunTestsTurn({
        checkoutDir: checkout,
        userPrompt: '诊断修复全周期',
        writePath,
        writeContent,
        diagnoseReadPath: readPath,
        sealVerificationProfileIds: [profileId],
        idleTimeoutMs: 40_000,
        executeTool: async (call) => {
          seq += 1;
          const id = `effect-${seq}`;
          return {
            content: [{type: 'text', text: `${call.name}-ok-${seq}`}],
            isError: false,
            meta: {
              effectId: id,
              status: 'SUCCEEDED',
              evidenceIds: [`ev-${seq}`],
              requiresReconciliation: false,
            },
          } satisfies HarnessToolResult;
        },
      });

      expect(evidence.scriptedOrder).toBe(true);
      expect(evidence.marksGoalDone).toBe(false);
      expect(evidence.trail.map((t) => t.toolName)).toEqual([
        'read_file',
        'run_tests',
        'write_file',
        'run_tests',
        'seal_candidate',
      ]);
      expect(evidence.modelRounds).toBeGreaterThanOrEqual(6);
      expect(evidence.laterRoundsCitePriorToolResults).toBe(true);
    },
    50_000,
  );
});
