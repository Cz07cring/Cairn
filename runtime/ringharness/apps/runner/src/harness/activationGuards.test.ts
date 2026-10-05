import {expect, test, vi} from 'vitest';
import {createDefaultActivationGuards} from './activationGuards.js';
import {createHeartbeatLinkedAdmissionGate} from './brokerToolHttpPorts.js';
import {forwardExecuteToolCallsFromChunks} from './cordisExecuteBridge.js';
import type {ExecuteToolHost} from './executeToolHost.js';

test('createDefaultActivationGuards：共享 gate，暴露 watchdog + progressGuard', () => {
  const gate = createHeartbeatLinkedAdmissionGate();
  const {watchdog, progressGuard} = createDefaultActivationGuards(gate);
  watchdog.enterPhase('awaiting_llm', 'llm_provider');
  expect(watchdog.heartbeatDetails().phase).toBe('awaiting_llm');
  expect(progressGuard.metrics().forceStopped).toBe(false);
  expect(gate.allowed()).toBe(true);
});

test('forwardExecuteToolCallsFromChunks：同参重复 DISPATCHED → Nudge → ForceStop', async () => {
  const gate = createHeartbeatLinkedAdmissionGate();
  const {progressGuard} = createDefaultActivationGuards(gate);
  let seq = 0;
  const toolHost = {
    gate,
    ports: {
      createStep: async () => {
        seq += 1;
        return {
          stepId: `step-${seq}`,
          logicalStepId: `step-${seq}`,
          intentRevision: 1,
        };
      },
      prepareEffect: async () => ({
        effectId: `eff-${seq}`,
        status: 'PREPARED',
        stateRevision: 1,
      }),
      dispatchEffect: async () => ({
        effectId: `eff-${seq}`,
        status: 'DISPATCHED',
        stateRevision: 2,
      }),
    },
    observe: {},
    artifacts: {},
  } as unknown as ExecuteToolHost;

  const claimed = {
    lease: {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
    activity: {
      id: 'a',
      kind: 'EXECUTE' as const,
      state_revision: 1,
      binding: {goal_contract_revision: 1},
      goal_id: 'g',
      task_id: null,
      project_id: 'p',
    },
  };

  const resolve = vi.fn(async () => ({
    artifactId: 'art-1',
    purpose: 'tool:read_file',
  }));
  const args = JSON.stringify({path: 'same.ts'});
  const chunk = {
    type: 'tool-call' as const,
    name: 'read_file',
    arguments: args,
  };

  const first = await forwardExecuteToolCallsFromChunks(
    toolHost,
    claimed,
    [chunk],
    resolve,
    new Set(['read_file']),
    progressGuard,
  );
  expect(first).toHaveLength(1);
  expect(progressGuard.lastVerdict().action).toBe('continue');

  const second = await forwardExecuteToolCallsFromChunks(
    toolHost,
    claimed,
    [chunk],
    resolve,
    new Set(['read_file']),
    progressGuard,
  );
  expect(second).toHaveLength(1);
  expect(progressGuard.lastVerdict().action).toBe('nudge');

  const third = await forwardExecuteToolCallsFromChunks(
    toolHost,
    claimed,
    [chunk],
    resolve,
    new Set(['read_file']),
    progressGuard,
  );
  // 第三百零七批把「同参重复」从**硬停**改为**软拒绝**（noProgressGuard.forceStop 的
  // `closeToolAdmission:false`，文档串写明「仅禁止被钉扎的同参调用，其它工具仍可准入
  // （避免 diagnose 读循环后无法 write）」）。
  //
  // 时序要点：force_stop 由**派发之后**的 observe 判定，故本次调用**已下发**（长度仍为 1）；
  // 变化的是**后续**准入：该同参被钉扎（bannedCallKeys），而**全闸不关** ——
  // 即 gate.allowed() 仍为 true、metrics().forceStopped 仍为 false。
  // 原用例断言的是硬停语义（全闸关闭 + forceStopped=true），未随该批更新
  // （同批只改了 noProgressGuard.test.ts / brokerBackedHarnessTool.test.ts），故 CI 红。
  expect(third).toHaveLength(1);
  const thirdVerdict = progressGuard.lastVerdict();
  expect(thirdVerdict.action).toBe('force_stop');
  if (thirdVerdict.action !== 'force_stop') {
    throw new Error('unreachable: 上一行已断言 force_stop');
  }
  expect(thirdVerdict.closeToolAdmission).toBe(false);
  expect(gate.allowed()).toBe(true);
  expect(progressGuard.metrics().forceStopped).toBe(false);

  // 软拒绝的设计意图：**换工具仍可写**（否则 diagnose 读循环会把 write 一起挡死）
  const switched = await forwardExecuteToolCallsFromChunks(
    toolHost,
    claimed,
    [
      {
        type: 'tool-call' as const,
        name: 'write_file',
        arguments: JSON.stringify({path: 'same.ts', content: 'fixed'}),
      },
    ],
    resolve,
    new Set(['read_file', 'write_file']),
    progressGuard,
  );
  expect(switched).toHaveLength(1);
});
