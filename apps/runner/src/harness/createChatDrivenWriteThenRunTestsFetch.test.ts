/**
 * createChatDrivenWriteThenRunTestsFetch 状态机单测（不启 AgentLoop）。
 */
import {describe, expect, test} from 'vitest';
import {createChatDrivenWriteThenRunTestsFetch} from './officialAgentLoopHost.js';

async function postMessages(
  fetchImpl: typeof fetch,
  messages: unknown[],
): Promise<{
  finish_reason?: string;
  message?: {
    content?: string | null;
    tool_calls?: Array<{function?: {name?: string}}>;
  };
}> {
  const res = await fetchImpl('http://injected.invalid/v1/chat/completions', {
    method: 'POST',
    body: JSON.stringify({messages, tools: [{type: 'function'}]}),
  });
  const body = (await res.json()) as {
    choices?: Array<{
      finish_reason?: string;
      message?: {
        content?: string | null;
        tool_calls?: Array<{function?: {name?: string}}>;
      };
    }>;
  };
  return body.choices?.[0] ?? {};
}

describe('createChatDrivenWriteThenRunTestsFetch', () => {
  const fetchImpl = createChatDrivenWriteThenRunTestsFetch({
    writePath: 'order_service/store.py',
    writeContent: 'fixed\n',
    writeMarker: 'WRITE_OK_MARKER',
  });

  test('首轮无 tool_calls 历史 → write_file', async () => {
    const choice = await postMessages(fetchImpl, [
      {role: 'user', content: 'fix order'},
    ]);
    expect(choice.finish_reason).toBe('tool_calls');
    expect(choice.message?.tool_calls?.[0]?.function?.name).toBe('write_file');
  });

  test('仅有 assistant write tool_calls、无 role=tool → 抛 PENDING', async () => {
    await expect(
      postMessages(fetchImpl, [
        {role: 'user', content: 'fix'},
        {
          role: 'assistant',
          content: null,
          tool_calls: [
            {
              id: 'call_cd_write',
              type: 'function',
              function: {name: 'write_file', arguments: '{}'},
            },
          ],
        },
      ]),
    ).rejects.toThrow(/CHAT_DRIVEN_PENDING_TOOL_RESULT/);
  });

  test('有 write + role=tool 回灌 → run_tests', async () => {
    const choice = await postMessages(fetchImpl, [
      {role: 'user', content: 'fix'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'call_cd_write',
            type: 'function',
            function: {name: 'write_file', arguments: '{}'},
          },
        ],
      },
      {
        role: 'tool',
        tool_call_id: 'call_cd_write',
        content: 'WRITE_OK_MARKER\nafter_digest=sha256:x',
      },
    ]);
    expect(choice.finish_reason).toBe('tool_calls');
    expect(choice.message?.tool_calls?.[0]?.function?.name).toBe('run_tests');
  });

  test('双工具完成后终轮只引用 tool 内容片段', async () => {
    const choice = await postMessages(fetchImpl, [
      {role: 'user', content: 'fix'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'call_cd_write',
            type: 'function',
            function: {name: 'write_file', arguments: '{}'},
          },
        ],
      },
      {
        role: 'tool',
        tool_call_id: 'call_cd_write',
        content: 'WRITE_OK_MARKER\npath=order_service/store.py',
      },
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'call_cd_tests',
            type: 'function',
            function: {name: 'run_tests', arguments: '{}'},
          },
        ],
      },
      {
        role: 'tool',
        tool_call_id: 'call_cd_tests',
        content: 'TESTS_OK_exit0\nexit_code=0\nsuite=public',
      },
    ]);
    expect(choice.finish_reason).toBe('stop');
    expect(choice.message?.content).toContain('WRITE_OK_MARKER');
    expect(choice.message?.content).toContain('suite=public');
    expect(choice.message?.content).not.toMatch(
      /path=order_service\/store\.py WRITE_OK_MARKER suite=public$/,
    );
  });
});
