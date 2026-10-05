/**
 * 第三百三十四批：真实 SSE chunk 间隙 Watchdog（AB05）。
 * 首字节前进 streaming_llm；间隙 Hard 带 partialAssistantText；≠ DONE。
 */
import {describe, expect, test, vi} from 'vitest';
import {
  createActivationWatchdog,
  WatchdogHardIdleError,
} from './activationWatchdog.js';
import {createHeartbeatLinkedAdmissionGate} from './brokerToolHttpPorts.js';
import {chatCompletionWithToolsStream} from './openaiCompatibleToolChat.js';
import {createOpenAiCompatibleOfficialAdapter} from './officialAgentLoopOpenAiAdapter.js';

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

/** 可控 SSE：先发若干 data 行，再可选挂起直至 abort。 */
function sseFetch(
  events: string[],
  opts?: {hangAfter?: number; onHang?: () => void},
): typeof fetch {
  const hangAfter = opts?.hangAfter;
  return vi.fn(async (_url: string | URL, init?: RequestInit) => {
    const encoder = new TextEncoder();
    let i = 0;
    let hanging: ((v: unknown) => void) | undefined;
    const stream = new ReadableStream<Uint8Array>({
      async pull(controller) {
        if (init?.signal?.aborted) {
          controller.error(new DOMException('Aborted', 'AbortError'));
          return;
        }
        if (hangAfter !== undefined && i >= hangAfter) {
          opts?.onHang?.();
          await new Promise((resolve) => {
            hanging = resolve;
            init?.signal?.addEventListener('abort', () => {
              hanging?.(null);
              try {
                controller.error(new DOMException('Aborted', 'AbortError'));
              } catch {
                /* already closed */
              }
            });
          });
          return;
        }
        if (i >= events.length) {
          controller.close();
          return;
        }
        const line = `data: ${events[i]}\n\n`;
        i += 1;
        controller.enqueue(encoder.encode(line));
      },
    });
    return new Response(stream, {
      status: 200,
      headers: {'Content-Type': 'text/event-stream'},
    });
  }) as unknown as typeof fetch;
}

class FakeLlmAdapterBase {}

describe('AB05 SSE chunk-gap Watchdog', () => {
  test('流间隙 Hard：保留 partialAssistantText、关准入、≠DONE', async () => {
    const nowRef = {now: 0};
    const gate = createHeartbeatLinkedAdmissionGate();
    const wd = createActivationWatchdog({
      now: () => nowRef.now,
      gate,
      budgets: {
        awaiting_llm: {softIdleMs: 20, hardIdleMs: 200},
        streaming_llm: {softIdleMs: 20, hardIdleMs: 80},
      },
    });
    const clock = clockDrivenInterval(25, nowRef);
    wd.enterPhase('awaiting_llm', 'llm_provider');

    const fetchImpl = sseFetch(
      [
        JSON.stringify({choices: [{delta: {content: 'hel'}}]}),
        JSON.stringify({choices: [{delta: {content: 'lo'}}]}),
        '[DONE]',
      ],
      {hangAfter: 1},
    );

    let caught: unknown;
    try {
      await chatCompletionWithToolsStream(
        {
          baseUrl: 'http://sse-gap.test',
          apiKey: 'k',
          modelId: 'm',
          fetchImpl,
        },
        [{role: 'user', content: 'hi'}],
        [],
        {
          watchdog: wd,
          tickEveryMs: 10,
          setIntervalFn: clock.setIntervalFn,
          clearIntervalFn: clock.clearIntervalFn,
        },
      );
    } catch (err) {
      caught = err;
    }
    clock.stopAll();

    expect(caught).toBeInstanceOf(WatchdogHardIdleError);
    const hard = caught as WatchdogHardIdleError;
    expect(hard.partialAssistantText).toBe('hel');
    expect(hard.outcome.phase).toBe('streaming_llm');
    expect(hard.outcome.marksGoalDone).toBe(false);
    expect(gate.allowed()).toBe(false);
  });

  test('完整 SSE 组装文本与 tool_calls', async () => {
    const fetchImpl = sseFetch([
      JSON.stringify({
        choices: [
          {
            delta: {
              tool_calls: [
                {
                  index: 0,
                  id: 'c1',
                  function: {name: 'read_file', arguments: '{"p'},
                },
              ],
            },
          },
        ],
      }),
      JSON.stringify({
        choices: [
          {
            delta: {
              tool_calls: [{index: 0, function: {arguments: 'ath":"a.ts"}'}}],
            },
          },
        ],
      }),
      JSON.stringify({choices: [{delta: {}, finish_reason: 'tool_calls'}]}),
      '[DONE]',
    ]);

    const round = await chatCompletionWithToolsStream(
      {
        baseUrl: 'http://sse-ok.test',
        apiKey: 'k',
        modelId: 'm',
        fetchImpl,
      },
      [{role: 'user', content: 'hi'}],
      [
        {
          type: 'function',
          function: {name: 'read_file', parameters: {type: 'object'}},
        },
      ],
    );
    expect(round.toolCalls).toEqual([
      {id: 'c1', name: 'read_file', arguments: '{"path":"a.ts"}'},
    ]);
    expect(round.finishReason).toBe('tool_calls');
  });

  test('官方 adapter×watchdog：无首字节 Hard（awaiting_llm）', async () => {
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
        baseUrl: 'http://ab05-sse.test',
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
          messages: Array<{role: string; content: Array<{type: string; text: string}>}>;
        }) => AsyncGenerator<unknown>;
      }
    ).stream({
      provider: 'openai-compatible',
      model: 'hang',
      messages: [{role: 'user', content: [{type: 'text', text: 'hi'}]}],
    });

    await expect(
      (async () => {
        for await (const _ of stream) {
          /* drain */
        }
      })(),
    ).rejects.toBeInstanceOf(WatchdogHardIdleError);

    expect(gate.allowed()).toBe(false);
    expect(wd.hardOutcome()?.phase).toBe('awaiting_llm');
    expect(wd.hardOutcome()?.marksGoalDone).toBe(false);
    hangResolve?.(
      new Response('late', {status: 200, headers: {'Content-Type': 'text/event-stream'}}),
    );
    clock.stopAll();
  });
});
