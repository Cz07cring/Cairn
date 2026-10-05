import {expect, test, vi} from 'vitest';
import {
  createExecuteToolHost,
  dispatchExecuteToolProposal,
  observeDispatchedEffect,
} from './executeToolHost.js';

const lease = {
  activity_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  attempt_id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
  fencing_epoch: '1',
};

test('dispatchExecuteToolProposal：step→prepare→dispatch', async () => {
  const fetchImpl = vi.fn(async (url: string | URL) => {
    const href = String(url);
    if (href.includes('/steps')) {
      return {
        ok: true,
        status: 201,
        json: async () => ({
          data: {
            id: 'step-1',
            logical_step_id: 'step-1',
            intent_revision: 1,
          },
        }),
      } as Response;
    }
    if (href.endsWith('/prepare')) {
      return {
        ok: true,
        status: 201,
        json: async () => ({
          data: {id: 'eff-1', status: 'PREPARED', state_revision: 2},
        }),
      } as Response;
    }
    if (href.includes('/dispatch')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({data: {id: 'eff-1', status: 'DISPATCHED'}}),
      } as Response;
    }
    throw new Error(href);
  });

  const host = createExecuteToolHost({
    baseUrl: 'http://control.test',
    authorization: 'Bearer w',
    fetchImpl: fetchImpl as unknown as typeof fetch,
  });
  const out = await dispatchExecuteToolProposal(host, {
    kind: 'EXECUTE',
    tool: 'read_file',
    purpose: '读文件',
    inputArtifactId: 'art-1',
    activityId: lease.activity_id,
    lease,
  });
  expect(out).toEqual({
    effectId: 'eff-1',
    // 诚实回报「已准备」：Runner 不派发（派发唯一归 Broker）
    dispatchStatus: 'PREPARED',
    logicalStepId: 'step-1',
  });
});

test('dispatch 后 observeDispatchedEffect 不写假 SUCCEEDED 回执', async () => {
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';
    if (href.includes('/steps')) {
      return {
        ok: true,
        status: 201,
        json: async () => ({
          data: {id: 'step-1', logical_step_id: 'step-1', intent_revision: 1},
        }),
      } as Response;
    }
    if (href.endsWith('/prepare')) {
      return {
        ok: true,
        status: 201,
        json: async () => ({
          data: {id: 'eff-1', status: 'PREPARED', state_revision: 1},
        }),
      } as Response;
    }
    if (href.includes('/dispatch')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({data: {id: 'eff-1', status: 'DISPATCHED'}}),
      } as Response;
    }
    if (method === 'GET' && href.includes('/effects/')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          data: {id: 'eff-1', status: 'DISPATCHED', state_revision: 1},
        }),
      } as Response;
    }
    if (href.includes('/receipts')) {
      throw new Error('must not post receipt');
    }
    throw new Error(href);
  });

  const host = createExecuteToolHost({
    baseUrl: 'http://control.test',
    authorization: 'Bearer w',
    fetchImpl: fetchImpl as unknown as typeof fetch,
  });
  const dispatched = await dispatchExecuteToolProposal(host, {
    kind: 'EXECUTE',
    tool: 'read_file',
    purpose: '读文件',
    inputArtifactId: 'art-1',
    activityId: lease.activity_id,
    lease,
  });
  const seen = await observeDispatchedEffect(host, dispatched.effectId, {
    maxAttempts: 2,
    delayMs: 0,
  });
  expect(seen.status).toBe('DISPATCHED');
});

test('准入门关闭后 dispatchExecuteToolProposal 拒绝', async () => {
  const host = createExecuteToolHost({
    baseUrl: 'http://control.test',
    authorization: 'Bearer w',
    fetchImpl: (async () => {
      throw new Error('should not fetch');
    }) as typeof fetch,
  });
  host.gate.onHeartbeatFailure(new Error('LEASE_EXPIRED'));
  await expect(
    dispatchExecuteToolProposal(host, {
      kind: 'EXECUTE',
      tool: 'read_file',
      purpose: 'x',
      inputArtifactId: 'a',
      activityId: lease.activity_id,
      lease,
    }),
  ).rejects.toThrow(/TOOL_ADMISSION_CLOSED:LEASE_EXPIRED/);
});
