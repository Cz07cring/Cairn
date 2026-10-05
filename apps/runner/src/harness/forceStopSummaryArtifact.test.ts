/**
 * 第三百三十七批：ForceStop 零工具总结 Artifact；≠ DONE。
 */
import {describe, expect, test, vi} from 'vitest';
import {contentDigestSha256} from './artifactPutHttpPorts.js';
import {
  buildForceStopSummaryDocument,
  forceStopVerdictFromClosedGate,
  persistForceStopSummary,
} from './forceStopSummaryArtifact.js';

describe('forceStopSummaryArtifact', () => {
  test('文档恒 marks_goal_done=false 且 allow_zero_tool_summary', () => {
    const hard = {
      action: 'force_stop' as const,
      code: 'NO_PROGRESS_FORCE_STOP' as const,
      reason: 'effect_unknown:c2',
      closeToolAdmission: true,
      allowZeroToolSummary: true as const,
      marksGoalDone: false as const,
    };
    const soft = {
      action: 'force_stop' as const,
      code: 'NO_PROGRESS_FORCE_STOP' as const,
      reason: 'repeat_after_nudge:sig',
      closeToolAdmission: false,
      allowZeroToolSummary: true as const,
      marksGoalDone: false as const,
    };
    const hardDoc = buildForceStopSummaryDocument(hard, {
      tool: 'read_file',
      callId: 'c2',
    });
    const softDoc = buildForceStopSummaryDocument(soft, {tool: 'read_file'});
    expect(hardDoc.kind).toBe('NO_PROGRESS_FORCE_STOP_SUMMARY');
    expect(hardDoc.marks_goal_done).toBe(false);
    expect(hardDoc.allow_zero_tool_summary).toBe(true);
    expect(hardDoc.close_tool_admission).toBe(true);
    expect(softDoc.close_tool_admission).toBe(false);
    expect(softDoc.marks_goal_done).toBe(false);
  });

  test('persistForceStopSummary PUT JSON 且 marksGoalDone=false', async () => {
    const verdict = {
      action: 'force_stop' as const,
      code: 'NO_PROGRESS_FORCE_STOP' as const,
      reason: 'nudge_budget_exhausted:sig',
      closeToolAdmission: true,
      allowZeroToolSummary: true as const,
      marksGoalDone: false as const,
    };
    const bodyRef: {raw?: string} = {};
    const put = vi.fn(async (input: {body: string | Uint8Array; mime?: string}) => {
      bodyRef.raw = typeof input.body === 'string' ? input.body : '';
      expect(input.mime).toBe('application/json');
      return {
        artifactId: 'art-fs-1',
        digest: contentDigestSha256(input.body),
      };
    });
    const out = await persistForceStopSummary({
      artifacts: {putCollectorContent: put},
      projectId: '11111111-1111-1111-1111-111111111111',
      lease: {
        activity_id: '22222222-2222-2222-2222-222222222222',
        attempt_id: '33333333-3333-3333-3333-333333333333',
        fencing_epoch: '7',
      },
      verdict,
      context: {tool: 'read_file', callId: 'c9'},
    });
    expect(out.marksGoalDone).toBe(false);
    expect(out.artifactId).toBe('art-fs-1');
    expect(put).toHaveBeenCalledOnce();
    const doc = JSON.parse(bodyRef.raw!) as {kind: string; marks_goal_done: boolean};
    expect(doc.kind).toBe('NO_PROGRESS_FORCE_STOP_SUMMARY');
    expect(doc.marks_goal_done).toBe(false);
  });

  test('第三百四十批：仅 NO_PROGRESS 关闸原因可合成补落 verdict', () => {
    expect(forceStopVerdictFromClosedGate('EFFECT_UNKNOWN')).toBeNull();
    expect(forceStopVerdictFromClosedGate('WATCHDOG_HARD_IDLE')).toBeNull();
    expect(
      forceStopVerdictFromClosedGate('EFFECT_UNKNOWN', {
        action: 'force_stop',
        code: 'NO_PROGRESS_FORCE_STOP',
        reason: 'effect_unknown:c1',
        closeToolAdmission: true,
        allowZeroToolSummary: true,
        marksGoalDone: false,
      }),
    ).toBeNull();

    const syn = forceStopVerdictFromClosedGate(
      'NO_PROGRESS_FORCE_STOP:nudge_budget_exhausted:sig',
    );
    expect(syn).toMatchObject({
      action: 'force_stop',
      code: 'NO_PROGRESS_FORCE_STOP',
      reason: 'nudge_budget_exhausted:sig',
      closeToolAdmission: true,
      marksGoalDone: false,
    });

    const last = {
      action: 'force_stop' as const,
      code: 'NO_PROGRESS_FORCE_STOP' as const,
      reason: 'from_guard',
      closeToolAdmission: true,
      allowZeroToolSummary: true as const,
      marksGoalDone: false as const,
    };
    expect(
      forceStopVerdictFromClosedGate('NO_PROGRESS_FORCE_STOP:x', last),
    ).toBe(last);
  });
});
