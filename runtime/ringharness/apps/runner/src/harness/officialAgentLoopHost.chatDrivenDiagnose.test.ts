/**
 * chat-driven 诊断全周期（注入 FSM）：read→红测→write→绿测；≠ DONE。
 */
import {existsSync} from 'node:fs';
import {describe, expect, test} from 'vitest';
import type {HarnessToolResult} from './brokerBackedHarnessTool.js';
import {cordisEntryPath} from './cordisBootGate.js';
import {runOfficialAgentLoopChatDrivenDiagnoseCycleTurn} from './officialAgentLoopHost.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';

const checkout = process.env.RING_HARNESS_CHECKOUT;
const ready =
  Boolean(checkout) &&
  existsSync(cordisEntryPath(checkout as string)) &&
  isOfficialAgentLoopBuilt(checkout);

describe.skipIf(!ready)(
  'official AgentLoop chat-driven diagnose cycle (injected FSM)',
  () => {
    test(
      'read→run_tests→write→run_tests；scriptedOrder=false；≠DONE',
      async () => {
        let runN = 0;
        const evidence =
          await runOfficialAgentLoopChatDrivenDiagnoseCycleTurn({
            checkoutDir: checkout,
            readPath: 'order_service/store.py',
            writePath: 'order_service/store.py',
            writeContent: 'fixed\n',
            userPrompt: '诊断并修复订单幂等后跑 public 测试',
            idleTimeoutMs: 60_000,
            chat: {
              baseUrl: 'http://injected.invalid',
              apiKey: 'test-key',
              modelId: 'injected-diagnose',
            },
            executeTool: async (call) => {
              if (call.name === 'read_file') {
                const r: HarnessToolResult = {
                  content: [
                    {
                      type: 'text',
                      text: 'READ_BUG_MARKER\ndouble_charge',
                    },
                  ],
                  isError: false,
                  meta: {
                    effectId: 'diag-read',
                    status: 'SUCCEEDED',
                    evidenceIds: [],
                    requiresReconciliation: false,
                  },
                };
                return r;
              }
              if (call.name === 'run_tests') {
                runN += 1;
                const r: HarnessToolResult = {
                  content: [
                    {
                      type: 'text',
                      text:
                        runN === 1
                          ? 'exit_code=1\nsuite=public\nFAIL'
                          : 'exit_code=0\nsuite=public\nPASS',
                    },
                  ],
                  isError: false,
                  meta: {
                    effectId: `diag-tests-${runN}`,
                    status: 'SUCCEEDED',
                    evidenceIds: [],
                    requiresReconciliation: false,
                  },
                };
                return r;
              }
              if (call.name === 'write_file') {
                const r: HarnessToolResult = {
                  content: [
                    {type: 'text', text: 'WRITE_OK_MARKER\nafter=sha256:x'},
                  ],
                  isError: false,
                  meta: {
                    effectId: 'diag-write',
                    status: 'SUCCEEDED',
                    evidenceIds: [],
                    requiresReconciliation: false,
                  },
                };
                return r;
              }
              throw new Error(`unexpected ${call.name}`);
            },
          });

        expect(evidence.scriptedOrder).toBe(false);
        expect(evidence.chatDrivenOrder).toBe(true);
        expect(evidence.marksGoalDone).toBe(false);
        expect(evidence.trail.map((t) => t.toolName)).toEqual([
          'read_file',
          'run_tests',
          'write_file',
          'run_tests',
        ]);
        expect(evidence.modelRounds).toBeGreaterThanOrEqual(5);
        expect(evidence.laterRoundsCitePriorToolResults).toBe(true);
      },
      90_000,
    );

    test(
      'read→红→write→绿→seal；scriptedOrder=false；≠DONE',
      async () => {
        const profileId = '11111111-1111-1111-1111-111111111111';
        let runN = 0;
        const evidence =
          await runOfficialAgentLoopChatDrivenDiagnoseCycleTurn({
            checkoutDir: checkout,
            readPath: 'order_service/store.py',
            writePath: 'order_service/store.py',
            writeContent: 'fixed\n',
            sealVerificationProfileIds: [profileId],
            userPrompt: '诊断修复后 seal_candidate',
            idleTimeoutMs: 60_000,
            chat: {
              baseUrl: 'http://injected.invalid',
              apiKey: 'test-key',
              modelId: 'injected-diagnose-seal',
            },
            executeTool: async (call) => {
              if (call.name === 'read_file') {
                return {
                  content: [{type: 'text', text: 'READ_BUG_MARKER'}],
                  isError: false,
                  meta: {
                    effectId: 'diag-read',
                    status: 'SUCCEEDED',
                    evidenceIds: [],
                    requiresReconciliation: false,
                  },
                };
              }
              if (call.name === 'run_tests') {
                runN += 1;
                return {
                  content: [
                    {
                      type: 'text',
                      text:
                        runN === 1
                          ? 'exit_code=1\nsuite=public'
                          : 'exit_code=0\nsuite=public',
                    },
                  ],
                  isError: false,
                  meta: {
                    effectId: `diag-tests-${runN}`,
                    status: 'SUCCEEDED',
                    evidenceIds: [],
                    requiresReconciliation: false,
                  },
                };
              }
              if (call.name === 'write_file') {
                return {
                  content: [{type: 'text', text: 'WRITE_OK_MARKER'}],
                  isError: false,
                  meta: {
                    effectId: 'diag-write',
                    status: 'SUCCEEDED',
                    evidenceIds: [],
                    requiresReconciliation: false,
                  },
                };
              }
              if (call.name === 'seal_candidate') {
                const args = JSON.parse(call.arguments) as {
                  verification_profile_ids?: string[];
                };
                expect(args.verification_profile_ids).toEqual([profileId]);
                return {
                  content: [{type: 'text', text: 'SEAL_OK_MARKER\nok'}],
                  isError: false,
                  meta: {
                    effectId: 'diag-seal',
                    status: 'SUCCEEDED',
                    evidenceIds: [],
                    requiresReconciliation: false,
                  },
                };
              }
              throw new Error(`unexpected ${call.name}`);
            },
          });

        expect(evidence.scriptedOrder).toBe(false);
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
      90_000,
    );
  },
);
