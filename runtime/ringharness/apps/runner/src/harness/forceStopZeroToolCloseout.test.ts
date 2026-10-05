/**
 * 第三百四十一批：硬 ForceStop 后零工具 LLM 收口；≠ DONE。
 */
import {describe, expect, test, vi} from 'vitest';
import {chatCompletionMockResponse} from './openaiCompatibleToolChat.js';
import {
  isHardForceStopCloseoutSignal,
  maybeRunHardForceStopCloseout,
  parseSummaryArtifactIdFromForceStopText,
  runForceStopZeroToolCloseout,
} from './forceStopZeroToolCloseout.js';

describe('forceStopZeroToolCloseout', () => {
  test('解析 summary_artifact=；软禁文案不触发硬收口信号', () => {
    expect(
      parseSummaryArtifactIdFromForceStopText(
        'TOOL_ADMISSION_CLOSED:NO_PROGRESS_FORCE_STOP:x:summary_artifact=art-fs-9',
      ),
    ).toBe('art-fs-9');
    expect(
      isHardForceStopCloseoutSignal([
        'Error: repeat；禁止重复本调用，可换工具继续 summary_artifact=art-soft',
      ]),
    ).toBe(false);
    expect(
      isHardForceStopCloseoutSignal([
        'TOOL_ADMISSION_CLOSED:NO_PROGRESS_FORCE_STOP:budget',
      ]),
    ).toBe(true);
    expect(
      isHardForceStopCloseoutSignal(['Error: x；允许零工具总结，禁止再调工具']),
    ).toBe(true);
  });

  test('一次 chat tools=[]；引用总结；marksGoalDone=false', async () => {
    const bodies: unknown[] = [];
    const out = await runForceStopZeroToolCloseout({
      chat: {
        baseUrl: 'http://chat.test',
        apiKey: 'k',
        modelId: 'm',
        fetchImpl: async (_url, init) => {
          bodies.push(JSON.parse(String(init?.body ?? '{}')));
          return chatCompletionMockResponse(init, {
            choices: [
              {
                message: {
                  content:
                    '停工因重复失败；工具已关。这不是 Goal DONE。',
                },
              },
            ],
          });
        },
      },
      summaryArtifactId: 'art-fs-1',
      summaryBody: JSON.stringify({
        kind: 'NO_PROGRESS_FORCE_STOP_SUMMARY',
        marks_goal_done: false,
        reason: 'nudge_budget_exhausted',
      }),
    });

    expect(bodies).toHaveLength(1);
    const req = bodies[0] as {tools?: unknown[]; tool_choice?: string};
    expect(req.tools).toBeUndefined();
    expect(out.toolCalls).toEqual([]);
    expect(out.marksGoalDone).toBe(false);
    expect(out.usedFallback).toBe(false);
    expect(out.assistantText).toContain('不是 Goal DONE');
    expect(out.summaryArtifactId).toBe('art-fs-1');
  });

  test('chat 失败用兜底正文；仍 ≠DONE；可选 PUT closeout', async () => {
    let putBody = '';
    const put = vi.fn(async (body: string) => {
      putBody = body;
      return {artifactId: 'art-close-1'};
    });
    const out = await runForceStopZeroToolCloseout({
      chat: {
        baseUrl: 'http://chat.test',
        apiKey: 'k',
        modelId: 'm',
        fetchImpl: async () => {
          throw new Error('network');
        },
      },
      summaryArtifactId: 'art-fs-2',
      putCloseout: put,
    });
    expect(out.usedFallback).toBe(true);
    expect(out.marksGoalDone).toBe(false);
    expect(out.assistantText).toMatch(/≠ Goal DONE/);
    expect(out.closeoutArtifactId).toBe('art-close-1');
    expect(put).toHaveBeenCalledOnce();
    const doc = JSON.parse(putBody) as {
      kind: string;
      marks_goal_done: boolean;
    };
    expect(doc.kind).toBe('NO_PROGRESS_FORCE_STOP_CLOSEOUT');
    expect(doc.marks_goal_done).toBe(false);
  });

  test('getSummaryContent 拉取失败仍可收口', async () => {
    const out = await runForceStopZeroToolCloseout({
      chat: {
        baseUrl: 'http://chat.test',
        apiKey: 'k',
        modelId: 'm',
        fetchImpl: async (_url, init) =>
          chatCompletionMockResponse(init, {
            choices: [{message: {content: '收口：无进展，≠ Goal DONE'}}],
          }),
      },
      summaryArtifactId: 'art-missing',
      getSummaryContent: async () => {
        throw new Error('404');
      },
    });
    expect(out.marksGoalDone).toBe(false);
    expect(out.assistantText).toContain('≠ Goal DONE');
  });

  test('maybeRun：软禁信号跳过；硬关闸跑收口且 tools=[]', async () => {
    const soft = await maybeRunHardForceStopCloseout({
      signalTexts: ['Error: x；禁止重复本调用，可换工具继续'],
      chat: {
        baseUrl: 'http://chat.test',
        apiKey: 'k',
        modelId: 'm',
        fetchImpl: async () => {
          throw new Error('should_not_call');
        },
      },
    });
    expect(soft).toBeNull();

    let toolsField: unknown = 'unset';
    const hard = await maybeRunHardForceStopCloseout({
      signalTexts: [
        'TOOL_ADMISSION_CLOSED:NO_PROGRESS_FORCE_STOP:z:summary_artifact=art-z',
      ],
      chat: {
        baseUrl: 'http://chat.test',
        apiKey: 'k',
        modelId: 'm',
        fetchImpl: async (_url, init) => {
          const body = JSON.parse(String(init?.body ?? '{}')) as {
            tools?: unknown;
          };
          toolsField = body.tools;
          return chatCompletionMockResponse(init, {
            choices: [{message: {content: '硬停收口 ≠ Goal DONE'}}],
          });
        },
      },
    });
    expect(hard).not.toBeNull();
    expect(hard!.marksGoalDone).toBe(false);
    expect(hard!.summaryArtifactId).toBe('art-z');
    expect(toolsField).toBeUndefined();
  });

  test('第三百四十二批：handleCordisHardForceStopCloseout 硬停收口；软禁 null', async () => {
    const {handleCordisHardForceStopCloseout} = await import(
      './forceStopZeroToolCloseout.js'
    );
    const soft = await handleCordisHardForceStopCloseout({
      error: new Error('NO_PROGRESS_REPEAT_BLOCKED:soft'),
      chat: {
        baseUrl: 'http://chat.test',
        apiKey: 'k',
        modelId: 'm',
        fetchImpl: async () => {
          throw new Error('should_not_call');
        },
      },
    });
    expect(soft).toBeNull();

    let putCount = 0;
    const hard = await handleCordisHardForceStopCloseout({
      error: new Error(
        'TOOL_ADMISSION_CLOSED:NO_PROGRESS_FORCE_STOP:budget:summary_artifact=art-sum',
      ),
      chat: {
        baseUrl: 'http://chat.test',
        apiKey: 'k',
        modelId: 'm',
        fetchImpl: async (_url, init) =>
          chatCompletionMockResponse(init, {
            choices: [{message: {content: 'Cordis 收口 ≠ Goal DONE'}}],
          }),
      },
      putCloseout: async () => {
        putCount += 1;
        return {artifactId: 'art-close-cordis'};
      },
    });
    expect(hard).not.toBeNull();
    expect(hard!.marksGoalDone).toBe(false);
    expect(hard!.closeout.summaryArtifactId).toBe('art-sum');
    expect(hard!.closeout.closeoutArtifactId).toBe('art-close-cordis');
    expect(hard!.closeout.marksGoalDone).toBe(false);
    expect(putCount).toBe(1);
  });
});
