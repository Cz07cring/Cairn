/**
 * 第八十七批：Stop HTTP 端口接 observeStop。
 */
import {expect, test, vi} from 'vitest';
import {observeStop} from './stopSeam.js';
import {createHttpStopSeamPorts} from './stopHttpPorts.js';

test('createHttpStopSeamPorts：GET stop + POST receipts 字段对齐', async () => {
  const calls: {url: string; method?: string; body: unknown}[] = [];
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({url: href, method, body});
    if (method === 'GET' && href.includes('/stops/')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          data: {
            id: 'ssssssss-ssss-ssss-ssss-ssssssssssss',
            status: 'REQUESTED',
            state_revision: 1,
            receipt_ids: [],
          },
        }),
      } as Response;
    }
    if (method === 'POST' && href.endsWith('/receipts')) {
      return {
        ok: true,
        status: 201,
        json: async () => ({data: {disposition: 'APPLIED'}}),
      } as Response;
    }
    throw new Error(href);
  });

  const ports = createHttpStopSeamPorts({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    fetchImpl: fetchImpl as unknown as typeof fetch,
  });

  const result = await observeStop(ports, {
    stopId: 'ssssssss-ssss-ssss-ssss-ssssssssssss',
    adapter: 'harness',
    activationId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    attemptId: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
    receiptId: 'rrrrrrrr-rrrr-rrrr-rrrr-rrrrrrrrrrrr',
    nowIso: '2026-09-12T00:00:00.000Z',
  });

  expect(result.observation).toBe('RUNNING');
  expect(result.disposition).toBe('APPLIED');
  expect(calls[0]?.method).toBe('GET');
  expect(calls[0]?.url).toBe(
    'http://control.test/internal/v1/stops/ssssssss-ssss-ssss-ssss-ssssssssssss',
  );
  expect(calls[1]?.body).toMatchObject({
    observation: 'RUNNING',
    compute_released: false,
    write_capability_revoked: false,
    stop_id: 'ssssssss-ssss-ssss-ssss-ssssssssssss',
  });
});

test('HTTP GET stop 非 2xx 失败关闭', async () => {
  const ports = createHttpStopSeamPorts({
    baseUrl: 'http://control.test',
    authorization: 'Bearer w',
    fetchImpl: (async () =>
      ({ok: false, status: 404, json: async () => ({})}) as Response) as typeof fetch,
  });
  await expect(ports.loadStop('missing')).rejects.toThrow(/STOP_GET_FAILED: HTTP 404/);
});
