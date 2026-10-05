/**
 * 第三百三十九批：Cordis ForceStop 落零工具总结 Artifact；≠ DONE。
 */
import {describe, expect, test, vi} from 'vitest';
import {createHeartbeatLinkedAdmissionGate} from './brokerToolHttpPorts.js';
import {
  canonicalizeToolArgumentsPayload,
  CordisHardForceStopError,
  forwardExecuteToolCallsFromChunks,
} from './cordisExecuteBridge.js';
import type {ExecuteToolHost} from './executeToolHost.js';
import {
  argsDigestOfCanonicalPayload,
  createNoProgressGuard,
  type NoProgressGuard,
} from './noProgressGuard.js';

const claimed = {
  activity: {
    id: '22222222-2222-2222-2222-222222222222',
    kind: 'EXECUTE' as const,
    project_id: '11111111-1111-1111-1111-111111111111',
    state_revision: 1,
    binding: {},
  },
  lease: {
    activity_id: '22222222-2222-2222-2222-222222222222',
    attempt_id: '33333333-3333-3333-3333-333333333333',
    fencing_epoch: '7',
  },
};

function mockHost(
  gate: ReturnType<typeof createHeartbeatLinkedAdmissionGate>,
  puts: string[],
): ExecuteToolHost {
  return {
    gate,
    artifacts: {
      putCollectorContent: vi.fn(async (input: {body: string | Uint8Array}) => {
        puts.push(typeof input.body === 'string' ? input.body : '');
        return {artifactId: `art-fs-${puts.length}`, digest: 'sha256:x'};
      }),
      getArtifactContent: vi.fn(),
    },
    ports: {
      createStep: vi.fn(async () => ({
        stepId: 's1',
        logicalStepId: 's1',
        intentRevision: 1,
      })),
      prepareEffect: vi.fn(async () => ({
        effectId: 'e-post',
        status: 'PREPARED',
        stateRevision: 1,
      })),
      dispatchEffect: vi.fn(async () => ({status: 'DISPATCHED'})),
    },
    observe: {},
  } as unknown as ExecuteToolHost;
}

describe('cordis ForceStop summary Artifact', () => {
  test('beforeAdmit 硬 ForceStop：PUT 总结且错误含 summary_artifact；≠DONE', async () => {
    const puts: string[] = [];
    const gate = createHeartbeatLinkedAdmissionGate();
    const guard = {
      beforeAdmit: () => ({
        action: 'force_stop' as const,
        code: 'NO_PROGRESS_FORCE_STOP' as const,
        reason: 'test_hard_stop',
        closeToolAdmission: true,
        allowZeroToolSummary: true as const,
        marksGoalDone: false as const,
      }),
      observe: () => ({action: 'continue' as const, marksGoalDone: false as const}),
      metrics: () => ({
        nudgeCount: 0,
        forceStopped: false,
        windowSize: 8,
        observations: 0,
        nudgeBudgetConsumed: 0,
        nudgeBudgetMax: 1,
        bannedCallKeyCount: 0,
      }),
      lastVerdict: () => ({action: 'continue' as const, marksGoalDone: false as const}),
    } satisfies NoProgressGuard;

    await expect(
      forwardExecuteToolCallsFromChunks(
        mockHost(gate, puts),
        claimed as never,
        [
          {
            type: 'tool-call',
            name: 'read_file',
            arguments: JSON.stringify({path: 'a.ts'}),
          },
        ],
        async () => ({artifactId: 'art-in', purpose: 'tool:read_file'}),
        new Set(['read_file']),
        guard,
      ),
    ).rejects.toThrow(/TOOL_ADMISSION_CLOSED:.*summary_artifact=art-fs-1/);

    expect(puts).toHaveLength(1);
    const doc = JSON.parse(puts[0]!) as {
      kind: string;
      marks_goal_done: boolean;
    };
    expect(doc.kind).toBe('NO_PROGRESS_FORCE_STOP_SUMMARY');
    expect(doc.marks_goal_done).toBe(false);
  });

  test('软禁同参：落总结后跳过该 call，不抛；≠DONE', async () => {
    const puts: string[] = [];
    const gate = createHeartbeatLinkedAdmissionGate();
    const argsJson = JSON.stringify({path: 'fail.ts'});
    const canonical = canonicalizeToolArgumentsPayload('read_file', argsJson);
    const digest = argsDigestOfCanonicalPayload(canonical);
    const guard = createNoProgressGuard({
      gate,
      maxNudgeBudget: 1,
      repeatSuccessBeforeNudge: 2,
    });
    for (let i = 0; i < 3; i += 1) {
      guard.observe({
        callId: `c${i}`,
        tool: 'read_file',
        argsDigest: digest,
        outcome: 'FAILED',
        signals: [{kind: 'effect_terminal', value: 'FAILED'}],
      });
    }
    expect(gate.allowed()).toBe(true);

    const out = await forwardExecuteToolCallsFromChunks(
      mockHost(gate, puts),
      claimed as never,
      [
        {
          type: 'tool-call',
          name: 'read_file',
          arguments: argsJson,
        },
      ],
      async () => ({artifactId: 'art-in', purpose: 'tool:read_file'}),
      new Set(['read_file']),
      guard,
    );
    expect(out).toEqual([]);
    expect(puts).toHaveLength(1);
    expect(JSON.parse(puts[0]!).marks_goal_done).toBe(false);
  });

  test('gate 已因 NO_PROGRESS 关闭：入口补落总结再抛；≠DONE', async () => {
    const puts: string[] = [];
    const gate = createHeartbeatLinkedAdmissionGate();
    gate.onHeartbeatFailure(new Error('NO_PROGRESS_FORCE_STOP:crash_before_put'));
    const guard = {
      beforeAdmit: () => ({action: 'continue' as const, marksGoalDone: false as const}),
      observe: () => ({action: 'continue' as const, marksGoalDone: false as const}),
      metrics: () => ({
        nudgeCount: 0,
        forceStopped: true,
        windowSize: 8,
        observations: 0,
        nudgeBudgetConsumed: 0,
        nudgeBudgetMax: 1,
        bannedCallKeyCount: 0,
      }),
      lastVerdict: () => ({
        action: 'force_stop' as const,
        code: 'NO_PROGRESS_FORCE_STOP' as const,
        reason: 'crash_before_put',
        closeToolAdmission: true,
        allowZeroToolSummary: true as const,
        marksGoalDone: false as const,
      }),
    } satisfies NoProgressGuard;

    await expect(
      forwardExecuteToolCallsFromChunks(
        mockHost(gate, puts),
        claimed as never,
        [
          {
            type: 'tool-call',
            name: 'read_file',
            arguments: JSON.stringify({path: 'a.ts'}),
          },
        ],
        async () => ({artifactId: 'art-in', purpose: 'tool:read_file'}),
        new Set(['read_file']),
        guard,
      ),
    ).rejects.toThrow(/TOOL_ADMISSION_CLOSED:.*summary_artifact=art-fs-1/);

    expect(puts).toHaveLength(1);
    expect(JSON.parse(puts[0]!).marks_goal_done).toBe(false);
  });

  test('gate 因 EFFECT_UNKNOWN 关闭：不补落 ForceStop 总结', async () => {
    const puts: string[] = [];
    const gate = createHeartbeatLinkedAdmissionGate();
    gate.onHeartbeatFailure(new Error('EFFECT_UNKNOWN'));

    await expect(
      forwardExecuteToolCallsFromChunks(
        mockHost(gate, puts),
        claimed as never,
        [
          {
            type: 'tool-call',
            name: 'read_file',
            arguments: JSON.stringify({path: 'a.ts'}),
          },
        ],
        async () => ({artifactId: 'art-in', purpose: 'tool:read_file'}),
        new Set(['read_file']),
      ),
    ).rejects.toThrow(/TOOL_ADMISSION_CLOSED:EFFECT_UNKNOWN/);
    expect(puts).toHaveLength(0);
  });

  test('第三百四十三批：post-dispatch 硬 ForceStop 抛 CordisHardForceStopError 带 effects；≠DONE', async () => {
    const puts: string[] = [];
    const gate = createHeartbeatLinkedAdmissionGate();
    const guard = {
      beforeAdmit: () => ({
        action: 'continue' as const,
        marksGoalDone: false as const,
      }),
      observe: () => {
        gate.onHeartbeatFailure(
          new Error('NO_PROGRESS_FORCE_STOP:post_dispatch_hard'),
        );
        return {
          action: 'force_stop' as const,
          code: 'NO_PROGRESS_FORCE_STOP' as const,
          reason: 'post_dispatch_hard',
          closeToolAdmission: true,
          allowZeroToolSummary: true as const,
          marksGoalDone: false as const,
        };
      },
      metrics: () => ({
        nudgeCount: 0,
        forceStopped: true,
        windowSize: 8,
        observations: 1,
        nudgeBudgetConsumed: 0,
        nudgeBudgetMax: 1,
        bannedCallKeyCount: 0,
      }),
      lastVerdict: () => ({
        action: 'force_stop' as const,
        code: 'NO_PROGRESS_FORCE_STOP' as const,
        reason: 'post_dispatch_hard',
        closeToolAdmission: true,
        allowZeroToolSummary: true as const,
        marksGoalDone: false as const,
      }),
    } satisfies NoProgressGuard;

    let caught: unknown;
    try {
      await forwardExecuteToolCallsFromChunks(
        mockHost(gate, puts),
        claimed as never,
        [
          {
            type: 'tool-call',
            name: 'read_file',
            arguments: JSON.stringify({path: 'a.ts'}),
          },
        ],
        async () => ({artifactId: 'art-in', purpose: 'tool:read_file'}),
        new Set(['read_file']),
        guard,
      );
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(CordisHardForceStopError);
    const hard = caught as CordisHardForceStopError;
    expect(hard.marksGoalDone).toBe(false);
    expect(hard.effects).toHaveLength(1);
    expect(hard.effects[0]?.effectId).toBe('e-post');
    expect(hard.message).toMatch(/TOOL_ADMISSION_CLOSED:.*summary_artifact=/);
    expect(puts.length).toBeGreaterThanOrEqual(1);
    expect(JSON.parse(puts[puts.length - 1]!).marks_goal_done).toBe(false);
  });
});
