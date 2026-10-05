/**
 * AB01 + Broker Gateway：协议形 PREPARED→SUCCEEDED+evidence，再按 tool_call_id 回灌第二轮。
 * fetchImpl 模拟 Control/Broker；禁止 Runner 侧 /dispatch。
 */
import {createHash} from 'node:crypto';
import {describe, expect, test, vi} from 'vitest';
import {createAb01BrokerExecuteTool} from './ab01BrokerGateway.js';
import {runAb01ReadFileTwoTurn} from './ab01TwoTurnLoop.js';

const MARKER = 'RING_AB01_BROKER_MARKER_c4e1';
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

function chatRoundFetch(callId: string): typeof fetch {
  let round = 0;
  return async (_url, init) => {
    round += 1;
    const body = JSON.parse(String(init?.body ?? '{}')) as {
      messages?: Array<Record<string, unknown>>;
      tools?: unknown[];
    };
    if (round === 1) {
      expect(body.tools?.length).toBe(1);
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
                      arguments: JSON.stringify({path: 'notes/ab01.txt'}),
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
    const messages = body.messages as Array<Record<string, unknown>>;
    const toolMsg = messages.find((m) => m.role === 'tool');
    expect(toolMsg?.tool_call_id).toBe(callId);
    expect(String(toolMsg?.content)).toContain(MARKER);
    return new Response(
      JSON.stringify({
        choices: [
          {
            finish_reason: 'stop',
            message: {content: `工具返回标记 ${MARKER}`},
          },
        ],
      }),
      {status: 200, headers: {'Content-Type': 'application/json'}},
    );
  };
}

describe('ab01BrokerGateway', () => {
  test('两轮 chat + Broker 协议链：PREPARED→SUCCEEDED+evidence，无 Runner dispatch，≠DONE', async () => {
    const callId =
      'call_' + createHash('sha256').update('ab01-broker').digest('hex').slice(0, 8);
    const controlRequests: Array<{url: string; method: string}> = [];
    let effectReads = 0;

    const controlFetch = vi.fn(async (url: string | URL, init?: RequestInit) => {
      const href = String(url);
      const method = init?.method ?? 'GET';
      controlRequests.push({url: href, method});

      if (method === 'PUT' && href.includes('/internal/v1/artifacts/')) {
        return Response.json({data: {id: 'input-artifact'}});
      }
      if (method === 'POST' && href.endsWith('/steps')) {
        return Response.json(
          {data: {id: 'step-1', logical_step_id: 'step-1', intent_revision: 1}},
          {status: 201},
        );
      }
      if (method === 'POST' && href.endsWith('/effects/prepare')) {
        return Response.json(
          {data: {id: 'effect-ab01', status: 'PREPARED', state_revision: 1}},
          {status: 201},
        );
      }
      if (method === 'GET' && href.endsWith('/api/v1/effects/effect-ab01')) {
        effectReads += 1;
        return Response.json({
          data:
            effectReads === 1
              ? {id: 'effect-ab01', status: 'DISPATCHED', evidence_ids: []}
              : {
                  id: 'effect-ab01',
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
    });

    const {executeTool} = createAb01BrokerExecuteTool({
      baseUrl: 'http://control.test',
      authorization: 'Bearer worker',
      activation,
      fetchImpl: controlFetch as unknown as typeof fetch,
      poll: {maxAttempts: 3, delayMs: 0},
    });

    const evidence = await runAb01ReadFileTwoTurn({
      chat: {
        baseUrl: 'https://chat.test',
        apiKey: 'k',
        modelId: 'm',
        fetchImpl: chatRoundFetch(callId),
      },
      expectedPath: 'notes/ab01.txt',
      userPrompt: '调用 read_file 读取 notes/ab01.txt',
      executeTool,
    });

    expect(evidence.marksGoalDone).toBe(false);
    expect(evidence.toolCallId).toBe(callId);
    expect(evidence.effectId).toBe('effect-ab01');
    expect(evidence.effectStatus).toBe('SUCCEEDED');
    expect(evidence.toolResultText).toContain(MARKER);
    expect(evidence.round2CitesToolResult).toBe(true);

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
  });
});
