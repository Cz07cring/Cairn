/**
 * AB05 命名缝 × 官方 AgentLoop OpenAI adapter：
 * awaiting_llm Hard 关准入；与工具阶段钟共享且异主不互救；≠ DONE。
 */
import {describe, expect, test, vi} from 'vitest';
import {
  awaitWithWatchdog,
  createActivationWatchdog,
  WatchdogHardIdleError,
} from './activationWatchdog.js';
import {createHeartbeatLinkedAdmissionGate} from './brokerToolHttpPorts.js';
import {createOpenAiCompatibleOfficialAdapter} from './officialAgentLoopOpenAiAdapter.js';

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

class FakeLlmAdapterBase {}

describe('AB05 × 官方 OpenAI adapter', () => {
  test('chatCompletion 挂起 → awaiting_llm Hard，关准入、≠DONE', async () => {
    const nowRef = {now: 0};
    const gate = createHeartbeatLinkedAdmissionGate();
    const wd = createActivationWatchdog({
      now: () => nowRef.now,
      gate,
      budgets: {awaiting_llm: {softIdleMs: 20, hardIdleMs: 80}},
    });
    const clock = clockDrivenInterval(25, nowRef);

    let hangResolve: ((v: Response) => void) | undefined;
    const hangFetch = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          hangResolve = resolve;
        }),
    );

    const {adapter} = createOpenAiCompatibleOfficialAdapter(
      FakeLlmAdapterBase,
      (raw) => raw,
      {
        baseUrl: 'http://ab05-official.test',
        apiKey: 'k',
        modelId: 'hang',
        fetchImpl: hangFetch as unknown as typeof fetch,
      },
      {
        watchdog: wd,
        tickEveryMs: 10,
        setIntervalFn: clock.setIntervalFn,
        clearIntervalFn: clock.clearIntervalFn,
      },
    );

    const stream = (
      adapter as {
        stream: (o: {
          provider: string;
          model: string;
          messages: Array<{role: 'user'; content: Array<{type: string; text: string}>}>;
        }) => AsyncGenerator<unknown>;
      }
    ).stream({
      provider: 'openai-compatible',
      model: 'hang',
      messages: [
        {role: 'user', content: [{type: 'text', text: 'hi'}]},
      ],
    });

    await expect(
      (async () => {
        for await (const _ of stream) {
          /* drain */
        }
      })(),
    ).rejects.toBeInstanceOf(WatchdogHardIdleError);

    expect(gate.allowed()).toBe(false);
    expect(wd.hardOutcome()?.marksGoalDone).toBe(false);
    expect(wd.hardOutcome()?.phase).toBe('awaiting_llm');
    hangResolve?.(
      new Response(JSON.stringify({choices: [{message: {content: 'late'}}]}), {
        status: 200,
        headers: {'Content-Type': 'application/json'},
      }),
    );
    clock.stopAll();
  });

  test('流间隙逼近 Hard 后切入 executing_tool，工具心跳保住准入', async () => {
    const nowRef = {now: 0};
    const gate = createHeartbeatLinkedAdmissionGate();
    const wd = createActivationWatchdog({
      now: () => nowRef.now,
      gate,
      budgets: {
        streaming_llm: {softIdleMs: 30, hardIdleMs: 100},
        executing_tool: {softIdleMs: 200, hardIdleMs: 1_000},
      },
    });

    wd.enterPhase('streaming_llm', 'llm_stream');
    nowRef.now = 90; // 已过 Soft、距流 Hard 仅 10ms
    const nearHard = wd.tick();
    expect(nearHard?.kind).not.toBe('hard_idle');
    expect(gate.allowed()).toBe(true);

    // 切入工具阶段：共享钟换主，流预算不得误杀
    wd.enterPhase('executing_tool', 'tool_runtime');
    for (let i = 0; i < 5; i += 1) {
      nowRef.now += 150;
      wd.reportProgress('tool_runtime', {toolCallId: 't1'});
      expect(wd.tick()).toBeNull();
    }
    expect(gate.allowed()).toBe(true);
    expect(wd.hardOutcome()).toBeNull();
    expect(wd.heartbeatDetails().phase).toBe('executing_tool');
  });

  test('executing_tool 期间 llm_stream 进度不能重置工具钟', async () => {
    const nowRef = {now: 0};
    const gate = createHeartbeatLinkedAdmissionGate();
    const wd = createActivationWatchdog({
      now: () => nowRef.now,
      gate,
      budgets: {
        executing_tool: {softIdleMs: 40, hardIdleMs: 120},
      },
    });
    wd.enterPhase('executing_tool', 'tool_runtime');
    wd.reportProgress('tool_runtime');
    nowRef.now = 50;
    // 异主进度不得救工具钟
    wd.reportProgress('llm_stream');
    nowRef.now = 130;
    const tick = wd.tick();
    expect(tick?.kind).toBe('hard_idle');
    expect(gate.allowed()).toBe(false);
    expect(tick && 'marksGoalDone' in tick ? tick.marksGoalDone : true).toBe(
      false,
    );
  });

  test('共享钟：adapter awaitWithWatchdog 与工具阶段可顺序衔接', async () => {
    const nowRef = {now: 0};
    const gate = createHeartbeatLinkedAdmissionGate();
    const wd = createActivationWatchdog({
      now: () => nowRef.now,
      gate,
      budgets: {
        awaiting_llm: {softIdleMs: 30, hardIdleMs: 100},
        executing_tool: {softIdleMs: 200, hardIdleMs: 500},
      },
    });
    const clock = clockDrivenInterval(40, nowRef);

    wd.enterPhase('awaiting_llm', 'llm_provider');
    const ok = await awaitWithWatchdog(
      wd,
      Promise.resolve('token'),
      {
        tickEveryMs: 10,
        setIntervalFn: clock.setIntervalFn,
        clearIntervalFn: clock.clearIntervalFn,
      },
    );
    expect(ok).toBe('token');
    wd.reportProgress('llm_provider');
    wd.enterPhase('executing_tool', 'tool_runtime');
    wd.reportProgress('tool_runtime');
    nowRef.now += 50;
    expect(wd.tick()).toBeNull();
    expect(gate.allowed()).toBe(true);
    clock.stopAll();
  });
});
