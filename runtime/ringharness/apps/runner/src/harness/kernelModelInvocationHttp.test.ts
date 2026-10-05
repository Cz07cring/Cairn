import {expect, test, vi} from 'vitest';
import {createHttpModelInvocationPorts} from './kernelModelInvocationHttp.js';

test('createInvocation POST /internal/v1/model-invocations', async () => {
  const fetchImpl = vi.fn(async () => ({
    ok: true,
    status: 201,
    json: async () => ({data: {id: 'inv-http-1'}}),
  })) as unknown as typeof fetch;
  const ports = createHttpModelInvocationPorts({
    baseUrl: 'http://127.0.0.1:58101',
    authorization: 'Bearer t',
    fetchImpl,
  });
  const created = await ports.createInvocation({
    lease: {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
    contextDigest: 'sha256:' + 'a'.repeat(64),
    providerRef: 'ring-kernel',
    modelId: 'fixture',
    inputDigest: 'sha256:' + 'b'.repeat(64),
    toolsExposedToModel: [],
  });
  expect(created.invocationId).toBe('inv-http-1');
  expect(fetchImpl).toHaveBeenCalledOnce();
  const [url, init] = (fetchImpl as ReturnType<typeof vi.fn>).mock.calls[0];
  expect(url).toBe('http://127.0.0.1:58101/internal/v1/model-invocations');
  expect(init.method).toBe('POST');
  const body = JSON.parse(init.body);
  expect(body.provider_ref).toBe('ring-kernel');
  expect(body.model_id).toBe('fixture');
});

test('无 liveDispatch 时 complete 不打 receipts（对齐 PLAN fixture）', async () => {
  const fetchImpl = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({}),
  })) as unknown as typeof fetch;
  const ports = createHttpModelInvocationPorts({
    baseUrl: 'http://127.0.0.1:58101',
    authorization: 'Bearer t',
    fetchImpl,
    fixtureText: 'host-only',
  });
  const done = await ports.completeInvocation({
    invocationId: 'inv-1',
    lease: {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
  });
  expect(done.text).toBe('host-only');
  expect(fetchImpl).not.toHaveBeenCalled();
});

test('liveDispatch 走 dispatch 端点；失败关闭', async () => {
  const fetchImpl = vi.fn(async () => ({
    ok: false,
    status: 503,
    json: async () => ({}),
  })) as unknown as typeof fetch;
  const ports = createHttpModelInvocationPorts({
    baseUrl: 'http://127.0.0.1:58101',
    authorization: 'Bearer t',
    fetchImpl,
    liveDispatch: true,
  });
  await expect(
    ports.completeInvocation({
      invocationId: 'inv-2',
      lease: {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
    }),
  ).rejects.toThrow(/MODEL_INVOCATION_DISPATCH_FAILED/);
  const [url] = (fetchImpl as ReturnType<typeof vi.fn>).mock.calls[0];
  expect(String(url)).toContain('/dispatch');
});

test('liveDispatch 成功必须带回 assistant_text；禁止 fixture 冒充', async () => {
  const fetchImpl = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      data: {
        id: 'inv-3',
        model_id: 'Qwen3.8-Flash-Next-Uncensored-Mixed-omlx',
        status: 'SUCCEEDED',
        assistant_text: 'context received',
      },
    }),
  })) as unknown as typeof fetch;
  const ports = createHttpModelInvocationPorts({
    baseUrl: 'http://127.0.0.1:58101',
    authorization: 'Bearer t',
    fetchImpl,
    liveDispatch: true,
    fixtureText: 'should-not-use',
  });
  const done = await ports.completeInvocation({
    invocationId: 'inv-3',
    lease: {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
  });
  expect(done.text).toBe('context received');
});

test('liveDispatch 空 assistant_text 失败关闭', async () => {
  const fetchImpl = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({data: {id: 'inv-4', assistant_text: ''}}),
  })) as unknown as typeof fetch;
  const ports = createHttpModelInvocationPorts({
    baseUrl: 'http://127.0.0.1:58101',
    authorization: 'Bearer t',
    fetchImpl,
    liveDispatch: true,
  });
  await expect(
    ports.completeInvocation({
      invocationId: 'inv-4',
      lease: {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
    }),
  ).rejects.toThrow(/MODEL_INVOCATION_LIVE_EMPTY/);
});
