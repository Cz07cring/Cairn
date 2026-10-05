/**
 * ToolResult 第二轮：观察 effect → Harness chat 回灌；≠ 官方 AgentLoop / Goal DONE。
 */
import {describe, expect, test, vi} from 'vitest';
import {createHeartbeatLinkedAdmissionGate} from './brokerToolHttpPorts.js';
import {
  defaultFormatToolResult,
  resolveToolResultObserveFromEnv,
  runToolResultSecondTurn,
} from './executeToolResultRound.js';
import type {ExecuteToolHost} from './executeToolHost.js';
import type {KernelLlmChunk} from './cordisBootGate.js';

const MARKER = 'status=SUCCEEDED';
const FILE_BODY = 'export const x = 1;';

function mockHost(
  status = 'SUCCEEDED',
  evidenceIds: string[] = [],
): ExecuteToolHost {
  const gate = createHeartbeatLinkedAdmissionGate();
  return {
    gate,
    ports: {
      createStep: vi.fn(),
      prepareEffect: vi.fn(),
      dispatchEffect: vi.fn(),
    },
    observe: {
      getEffect: vi.fn(async (id: string) => ({
        id,
        status,
        stateRevision: 2,
        evidenceIds,
      })),
      postTrustedReceipt: vi.fn(),
    },
    artifacts: {
      putCollectorContent: vi.fn(),
      getArtifactContent: vi.fn(async (artifactId: string) => {
        if (artifactId === 'art-body') {
          return FILE_BODY;
        }
        throw new Error(`ARTIFACT_GET_FAILED: unknown ${artifactId}`);
      }),
    },
  };
}

describe('runToolResultSecondTurn', () => {
  test('观察 SUCCEEDED → 第二轮引用 tool 回执；marksGoalDone=false', async () => {
    const chunks: KernelLlmChunk[] = [
      {
        type: 'tool-call',
        name: 'read_file',
        arguments: '{"path":"src/a.ts"}',
      },
      {type: 'finish', reason: 'stop'},
    ];
    const host = mockHost('SUCCEEDED');
    const fetchImpl = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        choices: [
          {
            message: {
              content: `已读到回执 ${MARKER} effect_id=eff-1`,
            },
            finish_reason: 'stop',
          },
        ],
      }),
    }));

    const evidence = await runToolResultSecondTurn({
      host,
      chunks,
      effects: [
        {tool: 'read_file', effectId: 'eff-1', dispatchStatus: 'DISPATCHED'},
      ],
      userPrompt: '请读取 src/a.ts',
      opts: {
        chat: {
          baseUrl: 'http://chat.test',
          apiKey: 'k',
          modelId: 'm',
          fetchImpl: fetchImpl as unknown as typeof fetch,
        },
        observe: {maxAttempts: 1, delayMs: 0},
        formatToolResult: async () =>
          `tool=read_file\neffect_id=eff-1\n${MARKER}`,
      },
    });

    expect(evidence.marksGoalDone).toBe(false);
    expect(evidence.requiresReconciliation).toBe(false);
    expect(evidence.observed[0]?.status).toBe('SUCCEEDED');
    expect(evidence.round2CitesToolResult).toBe(true);
    expect(evidence.round2AssistantText).toContain(MARKER);
    expect(host.observe.getEffect).toHaveBeenCalledWith('eff-1');
    expect(fetchImpl).toHaveBeenCalledOnce();
    const callArgs = fetchImpl.mock.calls[0] as unknown as [string, RequestInit?];
    const body = JSON.parse(String(callArgs[1]?.body ?? '{}')) as {
      messages: Array<{
        role: string;
        tool_call_id?: string;
        tool_calls?: Array<{id: string}>;
      }>;
    };
    const toolMsg = body.messages.find((m) => m.role === 'tool');
    expect(toolMsg?.tool_call_id).toBe('call_1');
    const assistant = body.messages.find((m) => m.role === 'assistant');
    expect(assistant?.tool_calls?.map((c) => c.id)).toEqual(['call_1']);
  });

  test('SUCCEEDED + evidence_ids → 默认 format 拉工件正文回灌', async () => {
    const chunks: KernelLlmChunk[] = [
      {type: 'tool-call', name: 'read_file', arguments: '{"path":"a.ts"}'},
      {type: 'finish', reason: 'stop'},
    ];
    const host = mockHost('SUCCEEDED', ['art-body']);
    const fetchImpl = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        choices: [
          {
            message: {content: `文件内容含 ${FILE_BODY}`, finish_reason: 'stop'},
          },
        ],
      }),
    }));

    const evidence = await runToolResultSecondTurn({
      host,
      chunks,
      effects: [
        {tool: 'read_file', effectId: 'eff-e', dispatchStatus: 'DISPATCHED'},
      ],
      userPrompt: 'read',
      opts: {
        chat: {
          baseUrl: 'http://chat.test',
          apiKey: 'k',
          modelId: 'm',
          fetchImpl: fetchImpl as unknown as typeof fetch,
        },
        observe: {maxAttempts: 1, delayMs: 0},
      },
    });

    expect(host.artifacts.getArtifactContent).toHaveBeenCalledWith('art-body');
    expect(evidence.observed[0]?.toolResultText).toContain(FILE_BODY);
    expect(evidence.observed[0]?.evidenceIds).toEqual(['art-body']);
    expect(evidence.round2CitesToolResult).toBe(true);
    expect(evidence.marksGoalDone).toBe(false);
  });

  test('多工具：assistant.tool_calls 含全部 call，再按序 tool 消息', async () => {
    const chunks: KernelLlmChunk[] = [
      {type: 'tool-call', name: 'read_file', arguments: '{"path":"a"}'},
      {type: 'tool-call', name: 'read_file', arguments: '{"path":"b"}'},
      {type: 'finish', reason: 'stop'},
    ];
    const host = mockHost('SUCCEEDED');
    host.observe.getEffect = vi.fn(async (id: string) => ({
      id,
      status: 'SUCCEEDED',
      stateRevision: 2,
      evidenceIds: [],
    }));
    const fetchImpl = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        choices: [
          {
            message: {content: 'status=SUCCEEDED 两文件均已读', finish_reason: 'stop'},
          },
        ],
      }),
    }));

    await runToolResultSecondTurn({
      host,
      chunks,
      effects: [
        {tool: 'read_file', effectId: 'eff-a', dispatchStatus: 'DISPATCHED'},
        {tool: 'read_file', effectId: 'eff-b', dispatchStatus: 'DISPATCHED'},
      ],
      userPrompt: 'read both',
      opts: {
        chat: {
          baseUrl: 'http://chat.test',
          apiKey: 'k',
          modelId: 'm',
          fetchImpl: fetchImpl as unknown as typeof fetch,
        },
        observe: {maxAttempts: 1, delayMs: 0},
        formatToolResult: async ({effectId}) =>
          `tool=read_file\neffect_id=${effectId}\nstatus=SUCCEEDED`,
      },
    });

    const callArgs = fetchImpl.mock.calls[0] as unknown as [string, RequestInit?];
    const body = JSON.parse(String(callArgs[1]?.body ?? '{}')) as {
      messages: Array<{
        role: string;
        tool_call_id?: string;
        tool_calls?: Array<{id: string; function: {name: string}}>;
      }>;
    };
    const assistant = body.messages.find((m) => m.role === 'assistant');
    expect(assistant?.tool_calls?.map((c) => c.id)).toEqual(['call_1', 'call_2']);
    const toolMsgs = body.messages.filter((m) => m.role === 'tool');
    expect(toolMsgs.map((m) => m.tool_call_id)).toEqual(['call_1', 'call_2']);
  });

  test('UNKNOWN 标记 requiresReconciliation，仍不写 DONE', async () => {
    const chunks: KernelLlmChunk[] = [
      {type: 'tool-call', name: 'read_file', arguments: '{"path":"x"}'},
      {type: 'finish', reason: 'stop'},
    ];
    const host = mockHost('UNKNOWN');
    const fetchImpl = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        choices: [{message: {content: 'status=UNKNOWN 待对账'}, finish_reason: 'stop'}],
      }),
    }));

    const evidence = await runToolResultSecondTurn({
      host,
      chunks,
      effects: [
        {tool: 'read_file', effectId: 'eff-u', dispatchStatus: 'DISPATCHED'},
      ],
      userPrompt: 'read',
      opts: {
        chat: {
          baseUrl: 'http://chat.test',
          apiKey: 'k',
          modelId: 'm',
          fetchImpl: fetchImpl as unknown as typeof fetch,
        },
        observe: {maxAttempts: 1, delayMs: 0},
        formatToolResult: async () => 'tool=read_file\nstatus=UNKNOWN',
      },
    });

    expect(evidence.requiresReconciliation).toBe(true);
    expect(evidence.marksGoalDone).toBe(false);
  });
});

describe('defaultFormatToolResult', () => {
  test('无 evidence 仅 status 摘要', async () => {
    const host = mockHost();
    const text = await defaultFormatToolResult({
      tool: 'read_file',
      effectId: 'e1',
      status: 'SUCCEEDED',
      evidenceIds: [],
      host,
    });
    expect(text).toBe('tool=read_file\neffect_id=e1\nstatus=SUCCEEDED');
    expect(host.artifacts.getArtifactContent).not.toHaveBeenCalled();
  });
});

describe('resolveToolResultObserveFromEnv', () => {
  test('缺省对齐 Broker poll 窗口', () => {
    expect(resolveToolResultObserveFromEnv({})).toEqual({
      maxAttempts: 120,
      delayMs: 500,
    });
  });

  test('环境覆盖合法整数', () => {
    expect(
      resolveToolResultObserveFromEnv({
        RING_TOOL_RESULT_OBSERVE_MAX_ATTEMPTS: '40',
        RING_TOOL_RESULT_OBSERVE_DELAY_MS: '100',
      }),
    ).toEqual({maxAttempts: 40, delayMs: 100});
  });

  test('非法值失败关闭', () => {
    expect(() =>
      resolveToolResultObserveFromEnv({
        RING_TOOL_RESULT_OBSERVE_MAX_ATTEMPTS: '0',
      }),
    ).toThrow(/TOOL_RESULT_OBSERVE_INVALID/);
    expect(() =>
      resolveToolResultObserveFromEnv({
        RING_TOOL_RESULT_OBSERVE_DELAY_MS: '-1',
      }),
    ).toThrow(/TOOL_RESULT_OBSERVE_INVALID/);
  });
});
