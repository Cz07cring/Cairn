/**
 * 官方 AgentLoop + OpenAI 兼容 chat（注入 fetch，不打真网）。
 * 证明循环归上游 AgentLoop，而非本仓自建两轮。
 */
import {existsSync} from 'node:fs';
import {describe, expect, test} from 'vitest';
import type {HarnessToolResult} from './brokerBackedHarnessTool.js';
import {cordisEntryPath} from './cordisBootGate.js';
import {runOfficialAgentLoopLiveReadFileTurn} from './officialAgentLoopHost.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';
import {DEEPSEEK_HARNESS_COMMIT} from './pin.js';

const checkout = process.env.RING_HARNESS_CHECKOUT;
const ready =
  Boolean(checkout) &&
  existsSync(cordisEntryPath(checkout as string)) &&
  isOfficialAgentLoopBuilt(checkout);

const MARKER = 'RING_OFFICIAL_OPENAI_ADAPTER_MARKER';

describe.skipIf(!ready)(
  'official AgentLoop + openai-compatible adapter (injected chat)',
  () => {
    test(
      '注入两轮 chat → AgentLoop 调 read_file → 第二轮引用标记；marksGoalDone=false',
      async () => {
        let chatRound = 0;
        const evidence = await runOfficialAgentLoopLiveReadFileTurn({
          checkoutDir: checkout,
          expectedPath: 'notes/official-openai.txt',
          userPrompt:
            '请调用工具 read_file，path 为 notes/official-openai.txt',
          idleTimeoutMs: 40_000,
          chat: {
            baseUrl: 'http://injected.invalid',
            apiKey: 'test-key',
            modelId: 'injected-model',
            fetchImpl: async (_url, init) => {
              chatRound += 1;
              const body = JSON.parse(String(init?.body ?? '{}')) as {
                messages?: unknown[];
                tools?: unknown[];
              };
              expect(Array.isArray(body.tools)).toBe(true);
              if (chatRound === 1) {
                return new Response(
                  JSON.stringify({
                    choices: [
                      {
                        finish_reason: 'tool_calls',
                        message: {
                          content: null,
                          tool_calls: [
                            {
                              id: 'call_inj_1',
                              type: 'function',
                              function: {
                                name: 'read_file',
                                arguments:
                                  '{"path":"notes/official-openai.txt"}',
                              },
                            },
                          ],
                        },
                      },
                    ],
                  }),
                  {status: 200, headers: {'Content-Type': 'application/json'}},
                );
              }
              const messages = body.messages ?? [];
              const hasTool = JSON.stringify(messages).includes(MARKER);
              expect(hasTool).toBe(true);
              return new Response(
                JSON.stringify({
                  choices: [
                    {
                      finish_reason: 'stop',
                      message: {
                        content: `文件内容含 ${MARKER}`,
                      },
                    },
                  ],
                }),
                {status: 200, headers: {'Content-Type': 'application/json'}},
              );
            },
          },
          executeTool: async (call) => {
            expect(call.name).toBe('read_file');
            const result: HarnessToolResult = {
              content: [{type: 'text', text: `file\n${MARKER}\nend`}],
              isError: false,
              meta: {
                effectId: 'effect-official-openai-1',
                status: 'SUCCEEDED',
                evidenceIds: [],
                requiresReconciliation: false,
              },
            };
            return result;
          },
        });

        expect(evidence.driver).toBe(
          'official-dsh-agent-loop+openai-compatible',
        );
        expect(evidence.marksGoalDone).toBe(false);
        expect(evidence.pin).toBe(DEEPSEEK_HARNESS_COMMIT);
        expect(evidence.toolName).toBe('read_file');
        // 上游 ToolCallId 须透传到 Broker execute（禁止自造 official-*）
        expect(evidence.toolCallId).toBe('call_inj_1');
        expect(evidence.modelRounds).toBeGreaterThanOrEqual(2);
        expect(evidence.round2CitesToolResult).toBe(true);
        expect(evidence.assistantTexts.join('\n')).toContain(MARKER);
        expect(chatRound).toBeGreaterThanOrEqual(2);
      },
      60_000,
    );
  },
);
