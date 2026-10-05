/**
 * 官方 AgentLoop × Broker Gateway：注入 chat + 协议形 Control HTTP。
 * PREPARED→SUCCEEDED+evidence；Runner 无 /dispatch；≠ Goal DONE。
 */
import {createHash} from 'node:crypto';
import {existsSync} from 'node:fs';
import {describe, expect, test, vi} from 'vitest';
import {cordisEntryPath} from './cordisBootGate.js';
import {runOfficialAb01BrokerReadFileTurn} from './officialAb01BrokerSeam.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';

const checkout = process.env.RING_HARNESS_CHECKOUT;
const ready =
  Boolean(checkout) &&
  existsSync(cordisEntryPath(checkout as string)) &&
  isOfficialAgentLoopBuilt(checkout);

const MARKER = 'RING_OFFICIAL_AB01_BROKER_MARKER_a91f';
const FILE_BODY = `workspace file\n${MARKER}\nend\n`;

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

describe.skipIf(!ready)(
  'officialAb01BrokerSeam',
  () => {
    test(
      '官方 Loop + Gateway：prepare→SUCCEEDED+evidence，无 Runner dispatch，≠DONE',
      async () => {
        const callId =
          'call_' +
          createHash('sha256').update('official-ab01-broker').digest('hex').slice(0, 8);
        const controlRequests: Array<{url: string; method: string}> = [];
        let effectReads = 0;
        let chatRound = 0;

        const controlFetch = vi.fn(
          async (url: string | URL, init?: RequestInit) => {
            const href = String(url);
            const method = init?.method ?? 'GET';
            controlRequests.push({url: href, method});

            if (method === 'PUT' && href.includes('/internal/v1/artifacts/')) {
              return Response.json({data: {id: 'input-artifact'}});
            }
            if (method === 'POST' && href.endsWith('/steps')) {
              return Response.json(
                {
                  data: {
                    id: 'step-1',
                    logical_step_id: 'step-1',
                    intent_revision: 1,
                  },
                },
                {status: 201},
              );
            }
            if (method === 'POST' && href.endsWith('/effects/prepare')) {
              return Response.json(
                {
                  data: {
                    id: 'effect-official-ab01',
                    status: 'PREPARED',
                    state_revision: 1,
                  },
                },
                {status: 201},
              );
            }
            if (
              method === 'GET' &&
              href.endsWith('/api/v1/effects/effect-official-ab01')
            ) {
              effectReads += 1;
              return Response.json({
                data:
                  effectReads === 1
                    ? {
                        id: 'effect-official-ab01',
                        status: 'DISPATCHED',
                        evidence_ids: [],
                      }
                    : {
                        id: 'effect-official-ab01',
                        status: 'SUCCEEDED',
                        evidence_ids: ['result-artifact'],
                      },
              });
            }
            if (
              method === 'GET' &&
              href.endsWith('/api/v1/artifacts/result-artifact/content')
            ) {
              return new Response(FILE_BODY, {status: 200});
            }
            throw new Error(`unexpected ${method} ${href}`);
          },
        );

        const evidence = await runOfficialAb01BrokerReadFileTurn({
          checkoutDir: checkout,
          expectedPath: 'notes/ab01.txt',
          userPrompt: '调用 read_file 读取 notes/ab01.txt',
          idleTimeoutMs: 40_000,
          chat: {
            baseUrl: 'https://chat.test',
            apiKey: 'k',
            modelId: 'm',
            fetchImpl: async (_url, init) => {
              chatRound += 1;
              const body = JSON.parse(String(init?.body ?? '{}')) as {
                messages?: Array<Record<string, unknown>>;
                tools?: unknown[];
              };
              if (chatRound === 1) {
                expect(body.tools?.length).toBeGreaterThan(0);
                return new Response(
                  JSON.stringify({
                    choices: [
                      {
                        finish_reason: 'tool_calls',
                        message: {
                          content: null,
                          tool_calls: [
                            {
                              id: callId,
                              type: 'function',
                              function: {
                                name: 'read_file',
                                arguments: JSON.stringify({
                                  path: 'notes/ab01.txt',
                                }),
                              },
                            },
                          ],
                        },
                      },
                    ],
                  }),
                  {
                    status: 200,
                    headers: {'Content-Type': 'application/json'},
                  },
                );
              }
              const messages = body.messages ?? [];
              expect(JSON.stringify(messages)).toContain(MARKER);
              return new Response(
                JSON.stringify({
                  choices: [
                    {
                      finish_reason: 'stop',
                      message: {content: `工具返回标记 ${MARKER}`},
                    },
                  ],
                }),
                {
                  status: 200,
                  headers: {'Content-Type': 'application/json'},
                },
              );
            },
          },
          broker: {
            baseUrl: 'http://control.test',
            authorization: 'Bearer worker',
            activation,
            fetchImpl: controlFetch as unknown as typeof fetch,
            poll: {maxAttempts: 3, delayMs: 0},
          },
        });

        expect(evidence.driver).toBe(
          'official-dsh-agent-loop+openai-compatible',
        );
        expect(evidence.marksGoalDone).toBe(false);
        expect(evidence.toolName).toBe('read_file');
        expect(evidence.effectId).toBe('effect-official-ab01');
        expect(evidence.effectStatus).toBe('SUCCEEDED');
        expect(evidence.toolResultText).toContain(MARKER);
        expect(evidence.modelRounds).toBeGreaterThanOrEqual(2);
        expect(evidence.round2CitesToolResult).toBe(true);
        expect(evidence.assistantTexts.join('\n')).toContain(MARKER);

        expect(
          controlRequests.some(
            (r) => r.method === 'POST' && r.url.endsWith('/effects/prepare'),
          ),
        ).toBe(true);
        expect(
          controlRequests.some(
            (r) => r.method === 'POST' && r.url.endsWith('/dispatch'),
          ),
        ).toBe(false);
        expect(effectReads).toBeGreaterThanOrEqual(2);
        expect(chatRound).toBeGreaterThanOrEqual(2);
      },
      60_000,
    );
  },
);
