/**
 * 第八十六批：Broker HTTP 端口 + Kernel 心跳失败关闭工具准入。
 */
import {expect, test, vi} from 'vitest';
import {
  forwardToolProposalToBroker,
  registerStepAndForwardToBroker,
} from './brokerToolBridge.js';
import {
  createGatedHttpExecuteToolHost,
  createHeartbeatLinkedAdmissionGate,
  createHttpBrokerToolPorts,
  createHttpExecuteToolPorts,
  withToolAdmissionGate,
} from './brokerToolHttpPorts.js';

const lease = {
  activity_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  attempt_id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
  fencing_epoch: '1',
};

test('createHttpBrokerToolPorts：prepare/dispatch 对齐 Control 内部路由与字段', async () => {
  const calls: {url: string; body: unknown}[] = [];
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({url: href, body});
    if (href.endsWith('/internal/v1/effects/prepare')) {
      return {
        ok: true,
        status: 201,
        json: async () => ({
          data: {
            id: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
            status: 'PREPARED',
            state_revision: 3,
          },
        }),
      } as Response;
    }
    if (href.includes('/dispatch')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          data: {id: 'cccccccc-cccc-cccc-cccc-cccccccccccc', status: 'DISPATCHED'},
        }),
      } as Response;
    }
    throw new Error(`unexpected ${href}`);
  });

  const ports = createHttpBrokerToolPorts({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    fetchImpl: fetchImpl as unknown as typeof fetch,
  });

  const prepared = await ports.prepareEffect({
    lease,
    logicalStepId: 'dddddddd-dddd-dddd-dddd-dddddddddddd',
    toolRef: 'read_file',
    inputArtifactId: 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee',
    intentRevision: 2,
  });
  expect(prepared).toEqual({
    effectId: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
    status: 'PREPARED',
    stateRevision: 3,
  });
  expect(calls[0]?.url).toBe('http://control.test/internal/v1/effects/prepare');
  expect(calls[0]?.body).toEqual({
    lease,
    logical_step_id: 'dddddddd-dddd-dddd-dddd-dddddddddddd',
    intent_revision: 2,
    tool_ref: 'read_file',
    input_artifact_id: 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee',
  });

  const dispatched = await ports.dispatchEffect({
    lease,
    effectId: prepared.effectId,
    effectStateRevision: prepared.stateRevision,
  });
  expect(dispatched.status).toBe('DISPATCHED');
  expect(calls[1]?.url).toBe(
    'http://control.test/internal/v1/effects/cccccccc-cccc-cccc-cccc-cccccccccccc/dispatch',
  );
  expect(calls[1]?.body).toEqual({
    lease,
    effect_state_revision: 3,
  });
});

test('createHttpExecuteToolPorts：createStep → prepare/dispatch', async () => {
  const calls: string[] = [];
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    calls.push(href);
    if (href.includes('/steps')) {
      return {
        ok: true,
        status: 201,
        json: async () => ({
          data: {
            id: 'ffffffff-ffff-ffff-ffff-ffffffffffff',
            logical_step_id: 'ffffffff-ffff-ffff-ffff-ffffffffffff',
            intent_revision: 1,
          },
        }),
      } as Response;
    }
    if (href.endsWith('/prepare')) {
      expect(JSON.parse(String(init?.body))).toMatchObject({
        logical_step_id: 'ffffffff-ffff-ffff-ffff-ffffffffffff',
        intent_revision: 1,
      });
      return {
        ok: true,
        status: 201,
        json: async () => ({
          data: {id: 'eff1', status: 'PREPARED', state_revision: 1},
        }),
      } as Response;
    }
    if (href.includes('/dispatch')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({data: {id: 'eff1', status: 'DISPATCHED'}}),
      } as Response;
    }
    throw new Error(href);
  });

  const ports = createHttpExecuteToolPorts({
    baseUrl: 'http://control.test',
    authorization: 'Bearer w',
    fetchImpl: fetchImpl as unknown as typeof fetch,
  });
  const out = await registerStepAndForwardToBroker(
    ports,
    {
      kind: 'EXECUTE',
      tool: 'read_file',
      purpose: '读取入口',
      inputArtifactId: 'art1',
      activityId: lease.activity_id,
      lease,
    },
    new Set(['read_file']),
  );
  expect(out.effectId).toBe('eff1');
  expect(out.logicalStepId).toBe('ffffffff-ffff-ffff-ffff-ffffffffffff');
  expect(calls[0]).toContain(`/activities/${lease.activity_id}/steps`);
});

test('HTTP prepare 非 2xx 失败关闭，不本地读盘', async () => {
  const ports = createHttpBrokerToolPorts({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    fetchImpl: (async () =>
      ({ok: false, status: 403, json: async () => ({})}) as Response) as typeof fetch,
  });
  await expect(
    ports.prepareEffect({
      lease,
      logicalStepId: 'dddddddd-dddd-dddd-dddd-dddddddddddd',
      toolRef: 'read_file',
      inputArtifactId: 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee',
    }),
  ).rejects.toThrow(/EFFECT_PREPARE_FAILED: HTTP 403/);
});

test('心跳失败后关闭准入：forward 不再 prepare', async () => {
  const prepareEffect = vi.fn(async () => ({
    effectId: 'e1',
    status: 'PREPARED',
    stateRevision: 1,
  }));
  const dispatchEffect = vi.fn(async () => ({status: 'DISPATCHED'}));
  const createStep = vi.fn(async () => ({
    stepId: 's',
    logicalStepId: 's',
    intentRevision: 1,
  }));
  const gate = createHeartbeatLinkedAdmissionGate();
  gate.onHeartbeatFailure(new Error('LEASE_EXPIRED'));
  const ports = withToolAdmissionGate(
    {prepareEffect, dispatchEffect, createStep},
    gate,
  );

  await expect(
    registerStepAndForwardToBroker(
      ports,
      {
        kind: 'EXECUTE',
        tool: 'read_file',
        purpose: 'x',
        inputArtifactId: 'art1',
        activityId: 'a',
        lease,
      },
      new Set(['read_file']),
    ),
  ).rejects.toThrow(/TOOL_ADMISSION_CLOSED:LEASE_EXPIRED/);
  expect(createStep).not.toHaveBeenCalled();
  expect(prepareEffect).not.toHaveBeenCalled();
});

test('准入门打开时 forward 只 prepare、**不** dispatch（派发唯一归 Broker）', async () => {
  const prepareEffect = vi.fn(async () => ({
    effectId: 'e1',
    status: 'PREPARED',
    stateRevision: 7,
  }));
  const dispatchEffect = vi.fn(async () => ({status: 'DISPATCHED'}));
  const gate = createHeartbeatLinkedAdmissionGate();
  const ports = withToolAdmissionGate({prepareEffect, dispatchEffect}, gate);

  const out = await forwardToolProposalToBroker(
    ports,
    {
      kind: 'EXECUTE',
      tool: 'read_file',
      logicalStepId: 's1',
      inputArtifactId: 'art1',
      lease,
    },
    new Set(['read_file']),
  );
  // 诚实回报「已准备」而非「已派发」：Runner 不声称派发
  expect(out).toEqual({effectId: 'e1', dispatchStatus: 'PREPARED'});
  // **关键不变量**：副作用咽喉只能有一个派发者。Runner 若也 dispatch，会把 effect
  // 推到 DISPATCHED，而 Broker 取件只认 PREPARED/AUTHORIZED ⇒ effect 被踢出队列、
  // 永无人执行（实测：验收 run_tests 永停 DISPATCHED、零回执，Goal 到不了 DONE）。
  expect(dispatchEffect).not.toHaveBeenCalled();
});

test('createGatedHttpExecuteToolHost 装配 ports+gate', async () => {
  const {ports, gate} = createGatedHttpExecuteToolHost({
    baseUrl: 'http://control.test',
    authorization: 'Bearer w',
    fetchImpl: (async () =>
      ({ok: false, status: 500, json: async () => ({})}) as Response) as typeof fetch,
  });
  expect(gate.allowed()).toBe(true);
  gate.onHeartbeatFailure(new Error('hb'));
  expect(gate.allowed()).toBe(false);
  await expect(
    ports.createStep({
      activityId: 'a',
      lease,
      purpose: 'p',
      toolRef: 'read_file',
    }),
  ).rejects.toThrow(/TOOL_ADMISSION_CLOSED/);
});
