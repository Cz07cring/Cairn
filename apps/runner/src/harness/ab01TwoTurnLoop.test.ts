/**
 * AB01 两轮缝单测：消息装配 + 伪造 chat/工具；不宣称官方 AgentLoop / Goal DONE。
 */
import {createHash} from 'node:crypto';
import {describe, expect, test} from 'vitest';
import {
  buildAb01Round2Messages,
  citationNeedleFromToolResult,
  runAb01ReadFileTwoTurn,
} from './ab01TwoTurnLoop.js';
import type {ChatMessage, ChatRoundResult} from './openaiCompatibleToolChat.js';
import {createTurnUserMessageGate} from './turnUserMessageGate.js';

const MARKER = 'RING_AB01_MARKER_9f3c';

function fakeRound(partial: Partial<ChatRoundResult> & {toolCalls?: ChatRoundResult['toolCalls']}): ChatRoundResult {
  return {
    assistantText: partial.assistantText ?? '',
    toolCalls: partial.toolCalls ?? [],
    finishReason: partial.finishReason ?? 'stop',
    raw: partial.raw ?? {},
  };
}

describe('ab01TwoTurnLoop', () => {
  test('citationNeedleFromToolResult 跳过 spill 行', () => {
    expect(
      citationNeedleFromToolResult(`[tool_result_spilled] x\n${MARKER}\ntrail`),
    ).toBe(MARKER);
  });

  test('buildAb01Round2Messages 按 tool_call_id 绑定', () => {
    const prior: ChatMessage[] = [{role: 'user', content: 'read it'}];
    const round1 = fakeRound({
      assistantText: '',
      toolCalls: [{id: 'call_1', name: 'read_file', arguments: '{"path":"a.txt"}'}],
    });
    const msgs = buildAb01Round2Messages({
      prior,
      round1,
      toolCallId: 'call_1',
      toolName: 'read_file',
      toolArguments: '{"path":"a.txt"}',
      toolResultText: MARKER,
      followUp: 'cite',
    });
    expect(msgs).toHaveLength(4);
    const assistant = msgs[1] as Extract<ChatMessage, {tool_calls: unknown}>;
    expect(assistant.tool_calls[0]?.id).toBe('call_1');
    const tool = msgs[2] as Extract<ChatMessage, {role: 'tool'}>;
    expect(tool.tool_call_id).toBe('call_1');
    expect(tool.content).toBe(MARKER);
  });

  test('runAb01ReadFileTwoTurn：伪造两轮 chat + 工具，断言引用与 marksGoalDone=false', async () => {
    const callId = 'call_' + createHash('sha256').update('ab01').digest('hex').slice(0, 8);
    let round = 0;
    const fetchImpl: typeof fetch = async (_url, init) => {
      round += 1;
      const body = JSON.parse(String(init?.body ?? '{}')) as {
        messages?: unknown[];
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
              message: {content: `标记是 ${MARKER}`},
            },
          ],
        }),
        {status: 200, headers: {'Content-Type': 'application/json'}},
      );
    };

    const evidence = await runAb01ReadFileTwoTurn({
      chat: {
        baseUrl: 'https://example.test',
        apiKey: 'test-key',
        modelId: 'test-model',
        fetchImpl,
      },
      expectedPath: 'notes/ab01.txt',
      userPrompt: `请调用 read_file 读取 notes/ab01.txt`,
      executeTool: async (call) => {
        expect(call.callId).toBe(callId);
        expect(call.name).toBe('read_file');
        return {
          content: [{type: 'text', text: MARKER}],
          isError: false,
          meta: {
            effectId: 'effect-ab01',
            status: 'SUCCEEDED',
            evidenceIds: ['art-1'],
            requiresReconciliation: false,
          },
        };
      },
    });

    expect(evidence.toolCallId).toBe(callId);
    expect(evidence.effectId).toBe('effect-ab01');
    expect(evidence.round2CitesToolResult).toBe(true);
    expect(evidence.marksGoalDone).toBe(false);
    expect(evidence.turnId).toBe('ab01-turn');
    expect(evidence.attachedInjected).toEqual([]);
    expect(evidence.deferredTurn).toBeNull();
  });

  test('AB08×AB01：工具后 seal 前消息挂入本轮并注入 round2，不进 deferred', async () => {
    const callId = 'call_ab08_attach';
    let round = 0;
    let sawLateInRound2 = false;
    const fetchImpl: typeof fetch = async (_url, init) => {
      round += 1;
      if (round === 1) {
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
      const body = JSON.parse(String(init?.body ?? '{}')) as {
        messages?: Array<Record<string, unknown>>;
      };
      const users = (body.messages ?? []).filter((m) => m.role === 'user');
      sawLateInRound2 = users.some((m) => String(m.content).includes('LATE_ATTACH'));
      return new Response(
        JSON.stringify({
          choices: [
            {finish_reason: 'stop', message: {content: `标记是 ${MARKER}`}},
          ],
        }),
        {status: 200, headers: {'Content-Type': 'application/json'}},
      );
    };

    const evidence = await runAb01ReadFileTwoTurn({
      chat: {
        baseUrl: 'https://example.test',
        apiKey: 'k',
        modelId: 'm',
        fetchImpl,
      },
      turnId: 'turn-ab08-a',
      expectedPath: 'notes/ab01.txt',
      userPrompt: '请调用 read_file 读取 notes/ab01.txt',
      onBetweenToolAndSeal: async (gate) => {
        const d = await gate.offer({messageId: 'late-attach', text: 'LATE_ATTACH please'});
        expect(d.kind).toBe('attached');
        expect(d.marksGoalDone).toBe(false);
      },
      executeTool: async () => ({
        content: [{type: 'text', text: MARKER}],
        isError: false,
        meta: {
          effectId: 'e1',
          status: 'SUCCEEDED',
          evidenceIds: [],
          requiresReconciliation: false,
        },
      }),
    });

    expect(sawLateInRound2).toBe(true);
    expect(evidence.attachedInjected).toEqual([
      {messageId: 'late-attach', text: 'LATE_ATTACH please'},
    ]);
    expect(evidence.deferredTurn).toBeNull();
    expect(evidence.marksGoalDone).toBe(false);
  });

  test('AB08×AB01：seal 后迟到消息进新 Turn 种子，本轮 round2 不注入、不双跑', async () => {
    const callId = 'call_ab08_defer';
    let round = 0;
    let sawDeferredInRound2 = false;
    const fetchImpl: typeof fetch = async (_url, init) => {
      round += 1;
      if (round === 1) {
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
      const body = JSON.parse(String(init?.body ?? '{}')) as {
        messages?: Array<Record<string, unknown>>;
      };
      sawDeferredInRound2 = (body.messages ?? []).some(
        (m) => m.role === 'user' && String(m.content).includes('DEFERRED_ONLY'),
      );
      return new Response(
        JSON.stringify({
          choices: [
            {finish_reason: 'stop', message: {content: `标记是 ${MARKER}`}},
          ],
        }),
        {status: 200, headers: {'Content-Type': 'application/json'}},
      );
    };

    const evidence = await runAb01ReadFileTwoTurn({
      chat: {
        baseUrl: 'https://example.test',
        apiKey: 'k',
        modelId: 'm',
        fetchImpl,
      },
      turnId: 'turn-ab08-old',
      messageGate: createTurnUserMessageGate({
        turnId: 'turn-ab08-old',
        newTurnId: () => 'turn-ab08-new',
      }),
      expectedPath: 'notes/ab01.txt',
      userPrompt: '请调用 read_file 读取 notes/ab01.txt',
      onAfterSeal: async (gate) => {
        const d = await gate.offer({
          messageId: 'late-defer',
          text: 'DEFERRED_ONLY should not run here',
        });
        expect(d.kind).toBe('deferred_new_turn');
        expect(d.marksGoalDone).toBe(false);
      },
      executeTool: async () => ({
        content: [{type: 'text', text: MARKER}],
        isError: false,
        meta: {
          effectId: 'e2',
          status: 'SUCCEEDED',
          evidenceIds: [],
          requiresReconciliation: false,
        },
      }),
    });

    expect(sawDeferredInRound2).toBe(false);
    expect(evidence.attachedInjected).toEqual([]);
    expect(evidence.deferredTurn).toEqual({
      turnId: 'turn-ab08-new',
      messages: [
        {messageId: 'late-defer', text: 'DEFERRED_ONLY should not run here'},
      ],
    });
    expect(evidence.marksGoalDone).toBe(false);
  });

  test('buildAb01Round2Messages 注入 attached 用户消息', () => {
    const prior: ChatMessage[] = [{role: 'user', content: 'read it'}];
    const round1 = fakeRound({
      assistantText: '',
      toolCalls: [{id: 'call_1', name: 'read_file', arguments: '{"path":"a.txt"}'}],
    });
    const msgs = buildAb01Round2Messages({
      prior,
      round1,
      toolCallId: 'call_1',
      toolName: 'read_file',
      toolArguments: '{"path":"a.txt"}',
      toolResultText: MARKER,
      followUp: 'cite',
      attachedUserMessages: [{messageId: 'm2', text: 'extra late'}],
    });
    expect(msgs.at(-1)).toEqual({role: 'user', content: 'extra late'});
  });

  test('无 read_file tool_call 失败关闭', async () => {
    const fetchImpl: typeof fetch = async () =>
      new Response(
        JSON.stringify({
          choices: [{finish_reason: 'stop', message: {content: '我直接猜内容'}}],
        }),
        {status: 200, headers: {'Content-Type': 'application/json'}},
      );
    await expect(
      runAb01ReadFileTwoTurn({
        chat: {
          baseUrl: 'https://example.test',
          apiKey: 'k',
          modelId: 'm',
          fetchImpl,
        },
        expectedPath: 'x.txt',
        userPrompt: 'read',
        executeTool: async () => {
          throw new Error('should_not_run');
        },
      }),
    ).rejects.toThrow(/AB01_NO_SINGLE_READ_FILE/);
  });
});
