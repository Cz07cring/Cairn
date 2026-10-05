import {expect, test} from 'vitest';
import {
  createActivationWatchdog,
  type HardIdleOutcome,
} from './activationWatchdog.js';
import {createHeartbeatLinkedAdmissionGate} from './brokerToolHttpPorts.js';

function fakeClock(start = 0) {
  let t = start;
  return {
    now: () => t,
    advance: (ms: number) => {
      t += ms;
    },
    set: (ms: number) => {
      t = ms;
    },
  };
}

test('LLM 无首 token：Soft 一次可见，Hard 关准入且 marksGoalDone=false', () => {
  const clock = fakeClock();
  const gate = createHeartbeatLinkedAdmissionGate();
  const wd = createActivationWatchdog({
    now: clock.now,
    gate,
    budgets: {
      awaiting_llm: {softIdleMs: 100, hardIdleMs: 500},
    },
  });
  wd.enterPhase('awaiting_llm', 'llm_provider');

  clock.advance(100);
  const soft = wd.tick();
  expect(soft?.kind).toBe('soft_idle');
  expect(soft).toMatchObject({closesAdmission: false, marksGoalDone: false});
  expect(gate.allowed()).toBe(true);

  // 同 epoch Soft 不重复
  clock.advance(50);
  expect(wd.tick()?.kind).not.toBe('soft_idle');

  clock.advance(400);
  const hard = wd.tick() as HardIdleOutcome;
  expect(hard.kind).toBe('hard_idle');
  expect(hard.code).toBe('WATCHDOG_HARD_IDLE');
  expect(hard.marksGoalDone).toBe(false);
  expect(hard.closesAdmission).toBe(true);
  expect(gate.allowed()).toBe(false);
  expect(gate.closedReason()).toMatch(/WATCHDOG_HARD_IDLE:awaiting_llm/);
});

test('半流卡死：仅 llm_stream 进度重置；tool_runtime 不能救流时钟', () => {
  const clock = fakeClock();
  const wd = createActivationWatchdog({
    now: clock.now,
    budgets: {
      streaming_llm: {softIdleMs: 50, hardIdleMs: 200},
    },
  });
  wd.enterPhase('streaming_llm', 'llm_stream');
  clock.advance(40);
  wd.reportProgress('llm_stream');
  clock.advance(40);
  // 工具心跳不得重置流间隙时钟
  wd.reportProgress('tool_runtime');
  clock.advance(170);
  const hard = wd.tick();
  expect(hard?.kind).toBe('hard_idle');
  expect(hard).toMatchObject({phase: 'streaming_llm', owner: 'llm_stream'});
});

test('合法长工具持续心跳：effect_observer 进度阻止 Hard', () => {
  const clock = fakeClock();
  const gate = createHeartbeatLinkedAdmissionGate();
  const wd = createActivationWatchdog({
    now: clock.now,
    gate,
    budgets: {
      awaiting_effect_observation: {softIdleMs: 50, hardIdleMs: 200},
    },
  });
  wd.enterPhase('awaiting_effect_observation', 'effect_observer');
  for (let i = 0; i < 10; i += 1) {
    clock.advance(80);
    wd.reportProgress('effect_observer', {effectId: 'eff-1'});
    expect(wd.tick()).toBeNull();
  }
  expect(gate.allowed()).toBe(true);
  expect(wd.hardOutcome()).toBeNull();
  expect(wd.heartbeatDetails()).toMatchObject({
    phase: 'awaiting_effect_observation',
    effect_id: 'eff-1',
    hard_idle_fired: false,
  });
});

/**
 * AB05：LLM 流间隙已逼近 Hard，但切入工具阶段后只认 tool/effect 预算；
 * 不得用流间隙预算误杀正常进展中的工具（≠ Goal DONE）。
 */
test('AB05：流间隙逼近 Hard 后切入 executing_tool，工具心跳保住准入', () => {
  const clock = fakeClock();
  const gate = createHeartbeatLinkedAdmissionGate();
  const wd = createActivationWatchdog({
    now: clock.now,
    gate,
    budgets: {
      streaming_llm: {softIdleMs: 30, hardIdleMs: 100},
      executing_tool: {softIdleMs: 200, hardIdleMs: 1_000},
    },
  });
  wd.enterPhase('streaming_llm', 'llm_stream');
  clock.advance(90); // 已过 Soft、距流 Hard 仅 10ms
  const nearHard = wd.tick();
  expect(nearHard?.kind).not.toBe('hard_idle');
  expect(gate.allowed()).toBe(true);

  // 模型已产出 tool_call：切入工具阶段，重置阶段钟
  wd.enterPhase('executing_tool', 'tool_runtime');
  for (let i = 0; i < 5; i += 1) {
    clock.advance(150); // 远超流 hardIdleMs，仍低于工具 hard
    wd.reportProgress('tool_runtime', {toolCallId: 'tc-1'});
    expect(wd.tick()).toBeNull();
  }
  expect(gate.allowed()).toBe(true);
  expect(wd.hardOutcome()).toBeNull();
  expect(wd.heartbeatDetails()).toMatchObject({
    phase: 'executing_tool',
    owner: 'tool_runtime',
    tool_call_id: 'tc-1',
    hard_idle_fired: false,
  });
});

test('AB05：executing_tool 期间 llm_stream 进度不能重置工具钟；停心跳仍 Hard', () => {
  const clock = fakeClock();
  const gate = createHeartbeatLinkedAdmissionGate();
  const wd = createActivationWatchdog({
    now: clock.now,
    gate,
    budgets: {
      executing_tool: {softIdleMs: 40, hardIdleMs: 120},
    },
  });
  wd.enterPhase('executing_tool', 'tool_runtime');
  clock.advance(30);
  wd.reportProgress('tool_runtime');
  clock.advance(50);
  // 流 chunk 不得救工具阶段
  wd.reportProgress('llm_stream');
  clock.advance(80);
  const hard = wd.tick() as HardIdleOutcome;
  expect(hard.kind).toBe('hard_idle');
  expect(hard).toMatchObject({
    phase: 'executing_tool',
    owner: 'tool_runtime',
    marksGoalDone: false,
    requiresReconciliation: true,
  });
  expect(gate.allowed()).toBe(false);
});

test('AB05：流 Hard 已触发后不得再切入工具阶段复活准入', () => {
  const clock = fakeClock();
  const gate = createHeartbeatLinkedAdmissionGate();
  const wd = createActivationWatchdog({
    now: clock.now,
    gate,
    budgets: {
      streaming_llm: {softIdleMs: 20, hardIdleMs: 60},
      executing_tool: {softIdleMs: 1_000, hardIdleMs: 5_000},
    },
  });
  wd.enterPhase('streaming_llm', 'llm_stream');
  clock.advance(60);
  expect(wd.tick()?.kind).toBe('hard_idle');
  expect(gate.allowed()).toBe(false);

  wd.enterPhase('executing_tool', 'tool_runtime');
  wd.reportProgress('tool_runtime');
  expect(wd.heartbeatDetails().phase).toBe('streaming_llm');
  expect(wd.hardOutcome()?.marksGoalDone).toBe(false);
  expect(gate.allowed()).toBe(false);
});

test('审批等待：短墙钟不 Hard（长 hard 预算）', () => {
  const clock = fakeClock();
  const wd = createActivationWatchdog({
    now: clock.now,
    budgets: {
      awaiting_approval: {softIdleMs: 100, hardIdleMs: 10_000},
    },
  });
  wd.enterPhase('awaiting_approval', 'approval');
  clock.advance(500);
  const soft = wd.tick();
  expect(soft?.kind).toBe('soft_idle');
  clock.advance(1_000);
  expect(wd.tick()?.kind).not.toBe('hard_idle');
  expect(wd.hardOutcome()).toBeNull();
});

test('阶段 epoch 重入：Soft 可再发一次，旧 Soft 不取消', () => {
  const clock = fakeClock();
  const gate = createHeartbeatLinkedAdmissionGate();
  const wd = createActivationWatchdog({
    now: clock.now,
    gate,
    budgets: {
      awaiting_llm: {softIdleMs: 50, hardIdleMs: 10_000},
    },
  });
  wd.enterPhase('awaiting_llm', 'llm_provider');
  clock.advance(50);
  expect(wd.tick()?.kind).toBe('soft_idle');
  wd.enterPhase('awaiting_llm', 'llm_provider');
  expect(wd.heartbeatDetails().phase_epoch).toBe(2);
  clock.advance(50);
  expect(wd.tick()?.kind).toBe('soft_idle');
  expect(wd.softEvents()).toHaveLength(2);
  expect(gate.allowed()).toBe(true);
});

test('Hard 后 tick 幂等返回同一 outcome，且不因异主 progress 复活', () => {
  const clock = fakeClock();
  const gate = createHeartbeatLinkedAdmissionGate();
  const wd = createActivationWatchdog({
    now: clock.now,
    gate,
    budgets: {setup: {softIdleMs: 10, hardIdleMs: 50}},
  });
  wd.enterPhase('setup', 'runner_setup');
  clock.advance(50);
  const first = wd.tick();
  const second = wd.tick();
  expect(first).toEqual(second);
  wd.reportProgress('runner_setup');
  wd.enterPhase('finalizing', 'finalizer');
  expect(wd.hardOutcome()?.kind).toBe('hard_idle');
  expect(gate.allowed()).toBe(false);
});
