import {expect, test, vi} from 'vitest';
import {
  awaitWithWatchdog,
  createActivationWatchdog,
  WatchdogHardIdleError,
} from './activationWatchdog.js';
import {createHeartbeatLinkedAdmissionGate} from './brokerToolHttpPorts.js';
import {invokeKernelLlmStreamWithWatchdog} from './cordisLlmBridge.js';

/** 假时钟 + 推进型 setInterval：每次回调先前进 now 再 tick。 */
function clockDrivenInterval(advanceMs: number, nowRef: {now: number}) {
  const handles = new Set<ReturnType<typeof setInterval>>();
  const setIntervalFn = ((fn: () => void, _ms?: number) => {
    const id = setInterval(() => {
      nowRef.now += advanceMs;
      fn();
    }, 5);
    handles.add(id);
    return id;
  }) as typeof setInterval;
  const clearIntervalFn = ((id: ReturnType<typeof setInterval>) => {
    clearInterval(id);
    handles.delete(id);
  }) as typeof clearInterval;
  const stopAll = () => {
    for (const id of handles) {
      clearInterval(id);
    }
    handles.clear();
  };
  return {setIntervalFn, clearIntervalFn, stopAll};
}

test('awaitWithWatchdog：无首 token 时 Hard 拒绝且关准入', async () => {
  const nowRef = {now: 0};
  const gate = createHeartbeatLinkedAdmissionGate();
  const wd = createActivationWatchdog({
    now: () => nowRef.now,
    gate,
    budgets: {awaiting_llm: {softIdleMs: 20, hardIdleMs: 80}},
  });
  wd.enterPhase('awaiting_llm', 'llm_provider');

  let hangResolve: ((v: string) => void) | undefined;
  const hang = new Promise<string>((resolve) => {
    hangResolve = resolve;
  });
  const clock = clockDrivenInterval(25, nowRef);

  await expect(
    awaitWithWatchdog(wd, hang, {
      tickEveryMs: 10,
      setIntervalFn: clock.setIntervalFn,
      clearIntervalFn: clock.clearIntervalFn,
    }),
  ).rejects.toBeInstanceOf(WatchdogHardIdleError);

  expect(gate.allowed()).toBe(false);
  expect(wd.hardOutcome()?.marksGoalDone).toBe(false);
  hangResolve?.('too-late');
  clock.stopAll();
});

test('invokeKernelLlmStreamWithWatchdog：complete 挂起 → Hard，不产出 chunk', async () => {
  const nowRef = {now: 0};
  const gate = createHeartbeatLinkedAdmissionGate();
  const wd = createActivationWatchdog({
    now: () => nowRef.now,
    gate,
    budgets: {awaiting_llm: {softIdleMs: 15, hardIdleMs: 60}},
  });
  const clock = clockDrivenInterval(20, nowRef);

  const createInvocation = vi.fn(async () => ({invocationId: 'inv-hang'}));
  let completeResolve: ((v: {text: string}) => void) | undefined;
  const completeInvocation = vi.fn(
    () =>
      new Promise<{text: string}>((resolve) => {
        completeResolve = resolve;
      }),
  );

  const chunks: unknown[] = [];
  await expect(
    (async () => {
      for await (const c of invokeKernelLlmStreamWithWatchdog(
        {createInvocation, completeInvocation},
        {
          lease: {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
          contextDigest: 'sha256:' + 'a'.repeat(64),
          providerRef: 'ring-kernel',
          modelId: 'm',
          messages: [{role: 'user', content: 'hi'}],
          toolsExposedToModel: [],
        },
        {
          watchdog: wd,
          tickEveryMs: 10,
          setIntervalFn: clock.setIntervalFn,
          clearIntervalFn: clock.clearIntervalFn,
        },
      )) {
        chunks.push(c);
      }
    })(),
  ).rejects.toThrow(/WATCHDOG_HARD_IDLE/);

  expect(chunks).toEqual([]);
  expect(createInvocation).toHaveBeenCalledOnce();
  expect(completeInvocation).toHaveBeenCalledOnce();
  expect(gate.allowed()).toBe(false);
  completeResolve?.({text: 'late'});
  clock.stopAll();
});

test('invokeKernelLlmStreamWithWatchdog：正常完成进入 streaming 并产出 finish', async () => {
  const wd = createActivationWatchdog({
    budgets: {awaiting_llm: {softIdleMs: 60_000, hardIdleMs: 120_000}},
  });
  const out: Array<{type: string}> = [];
  for await (const c of invokeKernelLlmStreamWithWatchdog(
    {
      createInvocation: async () => ({invocationId: 'inv-1'}),
      completeInvocation: async () => ({text: 'hello'}),
    },
    {
      lease: {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
      contextDigest: 'sha256:' + 'b'.repeat(64),
      providerRef: 'ring-kernel',
      modelId: 'm',
      messages: [{role: 'user', content: 'hi'}],
      toolsExposedToModel: [],
    },
    {watchdog: wd},
  )) {
    out.push(c);
  }
  expect(out.some((c) => c.type === 'finish')).toBe(true);
  expect(wd.heartbeatDetails().phase).toBe('streaming_llm');
  expect(wd.hardOutcome()).toBeNull();
});
