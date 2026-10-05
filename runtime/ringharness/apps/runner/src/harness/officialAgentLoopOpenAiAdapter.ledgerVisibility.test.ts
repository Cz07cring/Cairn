/**
 * Issue #70：账本失败不得被吞成「模型零轮且 loopError=none」。
 * adapter 须保留 lastStreamError，且 create 失败时不得调用 chat。
 */
import {describe, expect, test} from 'vitest';
import {createOpenAiCompatibleOfficialAdapter} from './officialAgentLoopOpenAiAdapter.js';
import type {ModelInvocationLedgerPorts} from './modelInvocationLedger.js';

class FakeLlmAdapter {
  // 上游基类占位
}

describe('officialAgentLoopOpenAiAdapter ledger visibility', () => {
  test('create 422 → stream 抛 MODEL_INVOCATION_LEDGER_* 且 lastStreamError 可取', async () => {
    const fetchImpl: typeof fetch = async () =>
      new Response('provider mismatch', {status: 422});
    const ports: ModelInvocationLedgerPorts = {
      baseUrl: 'http://control.test',
      authorization: 'Bearer t',
      fetchImpl,
      lease: {
        activity_id: 'a',
        attempt_id: '00000000-0000-4000-8000-000000000002',
        fencing_epoch: '1',
      },
      contextDigest: 'sha256:' + 'd'.repeat(64),
      providerRef: 'deepseek:api',
      modelId: 'deepseek-flash',
    };
    let chatHits = 0;
    const {adapter, rounds, lastStreamError} =
      createOpenAiCompatibleOfficialAdapter(
        FakeLlmAdapter,
        (raw) => raw,
        {
          baseUrl: 'http://chat.test',
          apiKey: 'k',
          modelId: 'deepseek-flash',
          fetchImpl: async () => {
            chatHits += 1;
            return new Response('{}', {status: 200});
          },
        },
        undefined,
        ports,
      );

    const stream = (
      adapter as {
        stream: (opts: unknown) => AsyncGenerator<unknown>;
      }
    ).stream({
      provider: 'openai-compatible',
      model: 'deepseek-flash',
      messages: [{role: 'user', content: [{type: 'text', text: 'hi'}]}],
    });

    await expect(async () => {
      for await (const _ of stream) {
        /* drain */
      }
    }).rejects.toThrow(/MODEL_INVOCATION_LEDGER_CREATE_HTTP_422/);

    expect(chatHits).toBe(0);
    expect(rounds).toHaveLength(0);
    const err = lastStreamError();
    expect(err).toBeInstanceOf(Error);
    expect((err as Error).message).toMatch(/MODEL_INVOCATION_LEDGER_CREATE_HTTP_422/);
  });
});
