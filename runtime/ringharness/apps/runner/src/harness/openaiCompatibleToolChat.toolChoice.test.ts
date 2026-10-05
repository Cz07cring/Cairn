/**
 * tool_choice / resolveToolChoice 透传单测（不打真网）。
 */
import {describe, expect, test, vi} from 'vitest';
import {chatCompletionWithTools} from './openaiCompatibleToolChat.js';

describe('openaiCompatibleToolChat tool_choice', () => {
  test('缺省 auto；resolveToolChoice 优先于 toolChoice', async () => {
    const fetchImpl = vi.fn(async (_url: string | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body ?? '{}')) as {
        tool_choice?: string;
      };
      expect(body.tool_choice).toBe('none');
      return new Response(
        JSON.stringify({
          choices: [
            {
              finish_reason: 'stop',
              message: {content: 'ok'},
            },
          ],
        }),
        {status: 200, headers: {'Content-Type': 'application/json'}},
      );
    });

    await chatCompletionWithTools(
      {
        baseUrl: 'http://injected.invalid',
        apiKey: 'k',
        modelId: 'm',
        toolChoice: 'required',
        resolveToolChoice: () => 'none',
        fetchImpl: fetchImpl as unknown as typeof fetch,
      },
      [{role: 'user', content: 'hi'}],
      [
        {
          type: 'function',
          function: {name: 'read_file', parameters: {type: 'object'}},
        },
      ],
    );
    expect(fetchImpl).toHaveBeenCalledOnce();
  });
});
