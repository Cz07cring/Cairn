/**
 * 官方 AgentLoop chat-driven 多工具（注入 fetch，非 ScriptedAdapter）。
 * scriptedOrder=false；ToolResult 回灌后才发下一工具；≠ Goal DONE。
 */
import {existsSync} from 'node:fs';
import {describe, expect, test} from 'vitest';
import type {HarnessToolResult} from './brokerBackedHarnessTool.js';
import {cordisEntryPath} from './cordisBootGate.js';
import {runOfficialAgentLoopChatDrivenWriteThenRunTestsTurn} from './officialAgentLoopHost.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';
import {DEEPSEEK_HARNESS_COMMIT} from './pin.js';

const checkout = process.env.RING_HARNESS_CHECKOUT;
const ready =
  Boolean(checkout) &&
  existsSync(cordisEntryPath(checkout as string)) &&
  isOfficialAgentLoopBuilt(checkout);

const WRITE_MARKER = 'WRITE_OK_MARKER';
const TESTS_MARKER = 'TESTS_OK_exit0';

describe.skipIf(!ready)(
  'official AgentLoop chat-driven multistep (injected chat)',
  () => {
    test(
      'chat 观察 tool 消息后 write→run_tests；scriptedOrder=false；≠DONE',
      async () => {
        const evidence =
          await runOfficialAgentLoopChatDrivenWriteThenRunTestsTurn({
            checkoutDir: checkout,
            writePath: 'order_service/store.py',
            writeContent: 'fixed\n',
            userPrompt: '请先 write_file 修好订单，再 run_tests public',
            idleTimeoutMs: 40_000,
            chat: {
              baseUrl: 'http://injected.invalid',
              apiKey: 'test-key',
              modelId: 'injected-chat-driven',
              // 故意不传 fetchImpl：宿主默认按消息状态决策
            },
            executeTool: async (call) => {
              if (call.name === 'write_file') {
                expect(call.callId).toBe('call_cd_write');
                const args = JSON.parse(call.arguments) as {
                  path?: string;
                  content?: string;
                };
                expect(args.path).toBe('order_service/store.py');
                const result: HarnessToolResult = {
                  content: [
                    {
                      type: 'text',
                      text: `${WRITE_MARKER}\nafter_digest=sha256:cd`,
                    },
                  ],
                  isError: false,
                  meta: {
                    effectId: 'effect-cd-write',
                    status: 'SUCCEEDED',
                    evidenceIds: [],
                    requiresReconciliation: false,
                  },
                };
                return result;
              }
              if (call.name === 'run_tests') {
                expect(call.callId).toBe('call_cd_tests');
                const result: HarnessToolResult = {
                  content: [
                    {
                      type: 'text',
                      text: `${TESTS_MARKER}\nexit_code=0\nsuite=public`,
                    },
                  ],
                  isError: false,
                  meta: {
                    effectId: 'effect-cd-tests',
                    status: 'SUCCEEDED',
                    evidenceIds: [],
                    requiresReconciliation: false,
                  },
                };
                return result;
              }
              throw new Error(`unexpected tool ${call.name}`);
            },
          });

        expect(evidence.scriptedOrder).toBe(false);
        expect(evidence.chatDrivenOrder).toBe(true);
        expect(evidence.marksGoalDone).toBe(false);
        expect(evidence.driver).toBe(
          'official-dsh-agent-loop+openai-compatible',
        );
        expect(evidence.pin).toBe(DEEPSEEK_HARNESS_COMMIT);
        expect(evidence.trail.map((t) => t.toolName)).toEqual([
          'write_file',
          'run_tests',
        ]);
        expect(evidence.modelRounds).toBeGreaterThanOrEqual(3);
        expect(evidence.laterRoundsCitePriorToolResults).toBe(true);
        expect(evidence.assistantTexts.join('\n')).toContain(WRITE_MARKER);
      },
      60_000,
    );
  },
);
