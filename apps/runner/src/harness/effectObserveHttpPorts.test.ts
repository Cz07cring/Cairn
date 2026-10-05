/**
 * 第八十九批：Effect 观察 HTTP；dispatch 后不假 SUCCEEDED。
 */
import {expect, test, vi} from 'vitest';
import {
  createHttpEffectObservePorts,
  pollEffectStatus,
} from './effectObserveHttpPorts.js';

test('getEffect / postTrustedReceipt 路由与字段对齐', async () => {
  const calls: {url: string; method?: string; body: unknown}[] = [];
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({url: href, method, body});
    if (method === 'GET') {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          data: {
            id: 'eff-1',
            status: 'DISPATCHED',
            state_revision: 2,
            evidence_ids: ['art-1', 'art-2'],
          },
        }),
      } as Response;
    }
    return {
      ok: true,
      status: 201,
      json: async () => ({data: {disposition: 'APPLIED'}}),
    } as Response;
  });

  const ports = createHttpEffectObservePorts({
    baseUrl: 'http://control.test',
    authorization: 'Bearer w',
    fetchImpl: fetchImpl as unknown as typeof fetch,
  });

  const got = await ports.getEffect('eff-1');
  expect(got).toEqual({
    id: 'eff-1',
    status: 'DISPATCHED',
    stateRevision: 2,
    evidenceIds: ['art-1', 'art-2'],
  });
  expect(calls[0]?.url).toBe('http://control.test/api/v1/effects/eff-1');

  const accepted = await ports.postTrustedReceipt({
    receiptId: 'r1',
    effectId: 'eff-1',
    producerActivityId: 'a1',
    producerAttemptId: 't1',
    fencingEpoch: '1',
    startedAt: '2026-09-12T00:00:00.000Z',
    finishedAt: '2026-09-12T00:00:01.000Z',
    observedOutcome: 'UNKNOWN',
    timedOut: true,
  });
  expect(accepted.disposition).toBe('APPLIED');
  expect(calls[1]?.body).toMatchObject({
    observed_outcome: 'UNKNOWN',
    timed_out: true,
    effect_id: 'eff-1',
  });
});

test('pollEffectStatus 非终态超时仍返回 DISPATCHED，不写回执', async () => {
  const postTrustedReceipt = vi.fn();
  const getEffect = vi.fn(async () => ({
    id: 'eff-1',
    status: 'DISPATCHED',
    stateRevision: 1,
    evidenceIds: [],
  }));
  const last = await pollEffectStatus(
    {getEffect, postTrustedReceipt},
    'eff-1',
    {maxAttempts: 3, delayMs: 0},
  );
  expect(last.status).toBe('DISPATCHED');
  expect(getEffect).toHaveBeenCalledTimes(3);
  expect(postTrustedReceipt).not.toHaveBeenCalled();
});

test('pollEffectStatus 见到 SUCCEEDED 即停', async () => {
  const getEffect = vi
    .fn()
    .mockResolvedValueOnce({
      id: 'e',
      status: 'DISPATCHED',
      stateRevision: 1,
      evidenceIds: [],
    })
    .mockResolvedValueOnce({
      id: 'e',
      status: 'SUCCEEDED',
      stateRevision: 2,
      evidenceIds: ['art-x'],
    });
  const last = await pollEffectStatus(
    {getEffect, postTrustedReceipt: async () => ({disposition: 'APPLIED'})},
    'e',
    {maxAttempts: 5, delayMs: 0},
  );
  expect(last.status).toBe('SUCCEEDED');
  expect(getEffect).toHaveBeenCalledTimes(2);
});
