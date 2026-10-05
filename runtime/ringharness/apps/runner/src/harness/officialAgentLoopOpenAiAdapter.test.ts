/**
 * OpenAI 兼容适配器：消息翻译 + 合成 StreamChunk（无官方 checkout 亦可单测）。
 */
import {describe, expect, test} from 'vitest';
import {
  chatRoundToOfficialLlmChunks,
  harnessMessagesToOpenAiChat,
  harnessToolsToOpenAi,
} from './officialAgentLoopOpenAiAdapter.js';

describe('officialAgentLoopOpenAiAdapter', () => {
  test('tool-result 译为 role:tool；assistant tool-call 保留 id', () => {
    const messages = harnessMessagesToOpenAiChat(
      [
        {
          role: 'system',
          content: [{type: 'text', text: 'sys'}],
        },
        {
          role: 'user',
          content: [{type: 'text', text: '读 notes/a.txt'}],
        },
        {
          role: 'assistant',
          content: [
            {
              type: 'tool-call',
              id: 'call-1',
              name: 'read_file',
              arguments: '{"path":"notes/a.txt"}',
            },
          ],
        },
        {
          role: 'user',
          content: [
            {
              type: 'tool-result',
              toolCallId: 'call-1',
              content: [{type: 'text', text: 'FILE_BODY'}],
            },
          ],
        },
      ],
      undefined,
    );
    expect(messages[0]).toEqual({role: 'system', content: 'sys'});
    expect(messages[1]).toEqual({role: 'user', content: '读 notes/a.txt'});
    expect(messages[2]).toMatchObject({
      role: 'assistant',
      tool_calls: [
        {
          id: 'call-1',
          function: {name: 'read_file', arguments: '{"path":"notes/a.txt"}'},
        },
      ],
    });
    expect(messages[3]).toEqual({
      role: 'tool',
      tool_call_id: 'call-1',
      content: 'FILE_BODY',
    });
  });

  test('tools → OpenAI function schema；chat 轮次合成 tool-calls chunks', () => {
    const tools = harnessToolsToOpenAi([
      {
        name: 'read_file',
        description: 'read',
        parameters: {
          type: 'object',
          properties: {path: {type: 'string'}},
        },
      },
    ]);
    expect(tools[0]?.function.name).toBe('read_file');
    const chunks = chatRoundToOfficialLlmChunks(
      {
        assistantText: '',
        toolCalls: [
          {
            id: 'c1',
            name: 'read_file',
            arguments: '{"path":"x"}',
          },
        ],
        finishReason: 'tool_calls',
        raw: {},
      },
      (raw) => `brand:${raw}`,
    );
    expect(chunks.some((c) => c.type === 'tool-call-delta')).toBe(true);
    expect(chunks.some((c) => c.type === 'finish')).toBe(true);
  });
});
