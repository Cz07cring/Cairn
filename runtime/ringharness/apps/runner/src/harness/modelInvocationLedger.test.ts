/**
 * 第三百五十五批：官方 Loop chat 经 ModelInvocation 账本；≠ DONE。
 */
import {describe, expect, test} from 'vitest';
import {
  createLedgeredChatRunner,
  resolveOfficialProviderRef,
} from './modelInvocationLedger.js';
import type {ChatRoundResult} from './openaiCompatibleToolChat.js';

describe('modelInvocationLedger', () => {
  test('resolveOfficialProviderRef：deepseek / 显式 / 默认 pm2', () => {
    expect(
      resolveOfficialProviderRef({
        RING_LOCAL_QWEN_MODEL: 'deepseek-flash',
      } as NodeJS.ProcessEnv),
    ).toBe('deepseek:api');
    expect(
      resolveOfficialProviderRef({
        RING_MODEL_PROVIDER_REF: 'custom:ref',
      } as NodeJS.ProcessEnv),
    ).toBe('custom:ref');
    expect(resolveOfficialProviderRef({} as NodeJS.ProcessEnv)).toBe(
      'pm2:omlx-flashnext',
    );
  });

  test('runRound：create→dispatch(runner_owned)→chat→receipt SUCCEEDED；≠DONE', async () => {
    const calls: Array<{url: string; body?: unknown}> = [];
    const fetchImpl: typeof fetch = async (input, init) => {
      const url = String(input);
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({url, body});
      if (url.endsWith('/model-invocations') && init?.method === 'POST') {
        return new Response(
          JSON.stringify({
            data: {
              id: 'inv-1',
              state_revision: 1,
              status: 'AUTHORIZED',
              marks_goal_done: false,
            },
          }),
          {status: 200, headers: {'Content-Type': 'application/json'}},
        );
      }
      if (url.includes('/dispatch')) {
        expect(body.runner_owned_completion).toBe(true);
        return new Response(
          JSON.stringify({
            data: {
              id: 'inv-1',
              state_revision: 2,
              status: 'DISPATCHED',
            },
          }),
          {status: 200, headers: {'Content-Type': 'application/json'}},
        );
      }
      if (url.includes('/receipts')) {
        expect(body.observed_result).toBe('SUCCEEDED');
        expect(body.invocation_id).toBe('inv-1');
        return new Response(
          JSON.stringify({data: {disposition: 'APPLIED'}}),
          {status: 201, headers: {'Content-Type': 'application/json'}},
        );
      }
      throw new Error(`unexpected url ${url}`);
    };

    const runner = createLedgeredChatRunner({
      baseUrl: 'http://control.test',
      authorization: 'Bearer t',
      fetchImpl,
      lease: {
        activity_id: 'a1',
        attempt_id: '00000000-0000-4000-8000-000000000001',
        fencing_epoch: '1',
      },
      contextDigest: 'sha256:' + 'a'.repeat(64),
      providerRef: 'deepseek:api',
      modelId: 'deepseek-flash',
    });

    const chatRound: ChatRoundResult = {
      assistantText: 'ok',
      toolCalls: [],
      finishReason: 'stop',
      raw: {usage: {prompt_tokens: 3, completion_tokens: 5}},
    };
    const out = await runner.runRound(
      async () => chatRound,
      [{role: 'user', content: 'hi'}],
      [],
    );
    expect(out.assistantText).toBe('ok');
    expect(calls.map((c) => c.url.replace('http://control.test', ''))).toEqual([
      '/internal/v1/model-invocations',
      '/internal/v1/model-invocations/inv-1/dispatch',
      '/internal/v1/model-invocations/inv-1/receipts',
    ]);
    const receipt = calls[2]!.body as {input_tokens: number; usage_status: string};
    expect(receipt.input_tokens).toBe(3);
    expect(receipt.usage_status).toBe('CONFIRMED');
  });

  test('chat 失败仍尽量 FAILED receipt', async () => {
    let receiptBody: {observed_result?: string} | undefined;
    const fetchImpl: typeof fetch = async (input, init) => {
      const url = String(input);
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      if (url.endsWith('/model-invocations') && init?.method === 'POST') {
        return new Response(
          JSON.stringify({
            data: {id: 'inv-2', state_revision: 1, status: 'AUTHORIZED'},
          }),
          {status: 200, headers: {'Content-Type': 'application/json'}},
        );
      }
      if (url.includes('/dispatch')) {
        return new Response(
          JSON.stringify({
            data: {id: 'inv-2', state_revision: 2, status: 'DISPATCHED'},
          }),
          {status: 200, headers: {'Content-Type': 'application/json'}},
        );
      }
      if (url.includes('/receipts')) {
        receiptBody = body;
        return new Response(
          JSON.stringify({data: {disposition: 'APPLIED'}}),
          {status: 201, headers: {'Content-Type': 'application/json'}},
        );
      }
      throw new Error(`unexpected ${url}`);
    };
    const runner = createLedgeredChatRunner({
      baseUrl: 'http://c',
      authorization: 't',
      fetchImpl,
      lease: {
        activity_id: 'a',
        attempt_id: '00000000-0000-4000-8000-000000000002',
        fencing_epoch: '1',
      },
      contextDigest: 'sha256:' + 'b'.repeat(64),
      providerRef: 'pm2:omlx-flashnext',
      modelId: 'qwen',
    });
    await expect(
      runner.runRound(
        async () => {
          throw new Error('CHAT_DOWN');
        },
        [{role: 'user', content: 'x'}],
        [],
      ),
    ).rejects.toThrow(/CHAT_DOWN/);
    expect(receiptBody?.observed_result).toBe('FAILED');
  });

  test('runRound：create HTTP 失败须抛 MODEL_INVOCATION_LEDGER_CREATE_*（Issue #70）', async () => {
    const fetchImpl: typeof fetch = async () =>
      new Response('provider_ref 不在冻结 Profile 允许集中', {
        status: 422,
        headers: {'Content-Type': 'text/plain'},
      });
    const runner = createLedgeredChatRunner({
      baseUrl: 'http://control.test',
      authorization: 'Bearer t',
      fetchImpl,
      lease: {
        activity_id: 'a',
        attempt_id: '00000000-0000-4000-8000-000000000002',
        fencing_epoch: '1',
      },
      contextDigest: 'sha256:' + 'c'.repeat(64),
      providerRef: 'deepseek:api',
      modelId: 'deepseek-flash',
    });
    let chatCalled = false;
    await expect(
      runner.runRound(
        async () => {
          chatCalled = true;
          throw new Error('CHAT_SHOULD_NOT_RUN');
        },
        [{role: 'user', content: 'x'}],
        [],
      ),
    ).rejects.toThrow(/MODEL_INVOCATION_LEDGER_CREATE_HTTP_422/);
    expect(chatCalled).toBe(false);
  });
});
