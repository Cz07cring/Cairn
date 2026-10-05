/**
 * createChatDrivenDiagnoseCycleFetch 状态机单测。
 */
import {describe, expect, test} from 'vitest';
import {createChatDrivenDiagnoseCycleFetch} from './officialAgentLoopHost.js';

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

describe('createChatDrivenDiagnoseCycleFetch', () => {
  const fetchImpl = createChatDrivenDiagnoseCycleFetch({
    readPath: 'order_service/store.py',
    writePath: 'order_service/store.py',
    writeContent: 'fixed\n',
  });

  test('空历史 → read_file', async () => {
    const c = await postMessages(fetchImpl, [{role: 'user', content: 'fix'}]);
    expect(c.message?.tool_calls?.[0]?.function?.name).toBe('read_file');
  });

  test('read 无 tool 回灌 → PENDING', async () => {
    await expect(
      postMessages(fetchImpl, [
        {role: 'user', content: 'fix'},
        {
          role: 'assistant',
          content: null,
          tool_calls: [
            {
              id: 'r1',
              type: 'function',
              function: {name: 'read_file', arguments: '{}'},
            },
          ],
        },
      ]),
    ).rejects.toThrow(/CHAT_DRIVEN_DIAG_PENDING/);
  });

  test('read+tool → run_tests；再 write；再 run_tests；终轮 cite', async () => {
    const afterRead = await postMessages(fetchImpl, [
      {role: 'user', content: 'fix'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'r1',
            type: 'function',
            function: {name: 'read_file', arguments: '{}'},
          },
        ],
      },
      {
        role: 'tool',
        tool_call_id: 'r1',
        content: 'READ_BUG_MARKER\nbuggy',
      },
    ]);
    expect(afterRead.message?.tool_calls?.[0]?.function?.name).toBe(
      'run_tests',
    );

    const afterRed = await postMessages(fetchImpl, [
      {role: 'user', content: 'fix'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'r1',
            type: 'function',
            function: {name: 'read_file', arguments: '{}'},
          },
        ],
      },
      {role: 'tool', tool_call_id: 'r1', content: 'READ_BUG_MARKER'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 't1',
            type: 'function',
            function: {name: 'run_tests', arguments: '{}'},
          },
        ],
      },
      {
        role: 'tool',
        tool_call_id: 't1',
        content: 'exit_code=1\nsuite=public',
      },
    ]);
    expect(afterRed.message?.tool_calls?.[0]?.function?.name).toBe(
      'write_file',
    );

    const afterWrite = await postMessages(fetchImpl, [
      {role: 'user', content: 'fix'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'r1',
            type: 'function',
            function: {name: 'read_file', arguments: '{}'},
          },
        ],
      },
      {role: 'tool', tool_call_id: 'r1', content: 'READ_BUG_MARKER'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 't1',
            type: 'function',
            function: {name: 'run_tests', arguments: '{}'},
          },
        ],
      },
      {role: 'tool', tool_call_id: 't1', content: 'exit_code=1'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'w1',
            type: 'function',
            function: {name: 'write_file', arguments: '{}'},
          },
        ],
      },
      {
        role: 'tool',
        tool_call_id: 'w1',
        content: 'WRITE_OK_MARKER',
      },
    ]);
    expect(afterWrite.message?.tool_calls?.[0]?.function?.name).toBe(
      'run_tests',
    );

    const stop = await postMessages(fetchImpl, [
      {role: 'user', content: 'fix'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'r1',
            type: 'function',
            function: {name: 'read_file', arguments: '{}'},
          },
        ],
      },
      {role: 'tool', tool_call_id: 'r1', content: 'READ_BUG_MARKER'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 't1',
            type: 'function',
            function: {name: 'run_tests', arguments: '{}'},
          },
        ],
      },
      {role: 'tool', tool_call_id: 't1', content: 'exit_code=1\nsuite=public'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'w1',
            type: 'function',
            function: {name: 'write_file', arguments: '{}'},
          },
        ],
      },
      {role: 'tool', tool_call_id: 'w1', content: 'WRITE_OK_MARKER'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 't2',
            type: 'function',
            function: {name: 'run_tests', arguments: '{}'},
          },
        ],
      },
      {
        role: 'tool',
        tool_call_id: 't2',
        content: 'exit_code=0\nsuite=public',
      },
    ]);
    expect(stop.finish_reason).toBe('stop');
    expect(stop.message?.content).toContain('READ_BUG_MARKER');
    expect(stop.message?.content).toContain('WRITE_OK_MARKER');
  });

  test('绿测后可追加 seal_candidate', async () => {
    const withSeal = createChatDrivenDiagnoseCycleFetch({
      readPath: 'order_service/store.py',
      writePath: 'order_service/store.py',
      writeContent: 'fixed\n',
      sealVerificationProfileIds: ['11111111-1111-1111-1111-111111111111'],
    });
    const baseMsgs = [
      {role: 'user', content: 'fix'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'r1',
            type: 'function',
            function: {name: 'read_file', arguments: '{}'},
          },
        ],
      },
      {role: 'tool', tool_call_id: 'r1', content: 'READ_BUG_MARKER'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 't1',
            type: 'function',
            function: {name: 'run_tests', arguments: '{}'},
          },
        ],
      },
      {role: 'tool', tool_call_id: 't1', content: 'exit_code=1'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'w1',
            type: 'function',
            function: {name: 'write_file', arguments: '{}'},
          },
        ],
      },
      {role: 'tool', tool_call_id: 'w1', content: 'WRITE_OK_MARKER'},
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 't2',
            type: 'function',
            function: {name: 'run_tests', arguments: '{}'},
          },
        ],
      },
      {
        role: 'tool',
        tool_call_id: 't2',
        content: 'exit_code=0\nsuite=public',
      },
    ];
    const sealStep = await postMessages(withSeal, baseMsgs);
    expect(sealStep.message?.tool_calls?.[0]?.function?.name).toBe(
      'seal_candidate',
    );
    const stop = await postMessages(withSeal, [
      ...baseMsgs,
      {
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 's1',
            type: 'function',
            function: {name: 'seal_candidate', arguments: '{}'},
          },
        ],
      },
      {
        role: 'tool',
        tool_call_id: 's1',
        content: 'SEAL_OK_MARKER\ncandidate=ok',
      },
    ]);
    expect(stop.finish_reason).toBe('stop');
    expect(stop.message?.content).toContain('SEAL_OK_MARKER');
  });
});
