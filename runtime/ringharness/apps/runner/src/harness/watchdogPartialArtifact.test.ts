/**
 * 第三百三十五批：Hard Idle 部分文本 → collector Artifact；≠ DONE。
 */
import {describe, expect, test, vi} from 'vitest';
import {
  createActivationWatchdog,
  type HardIdleOutcome,
  WatchdogHardIdleError,
} from './activationWatchdog.js';
import {contentDigestSha256} from './artifactPutHttpPorts.js';
import {
  buildWatchdogHardIdlePartialDocument,
  formatWatchdogHardIdleFailReason,
  persistWatchdogHardIdlePartial,
} from './watchdogPartialArtifact.js';

function forceHard(
  phase: 'awaiting_llm' | 'streaming_llm',
  owner: 'llm_provider' | 'llm_stream',
): HardIdleOutcome {
  const nowRef = {now: 0};
  const wd = createActivationWatchdog({
    budgets: {
      [phase]: {softIdleMs: 5, hardIdleMs: 10},
    },
    now: () => nowRef.now,
  });
  wd.enterPhase(phase, owner);
  nowRef.now = 20;
  const hard = wd.tick();
  if (hard?.kind !== 'hard_idle') {
    throw new Error('expected hard_idle');
  }
  return hard;
}

describe('watchdogPartialArtifact', () => {
  test('无 partial 正文 → 不 PUT、返回 null', async () => {
    const err = new WatchdogHardIdleError(forceHard('awaiting_llm', 'llm_provider'));
    const put = vi.fn();
    const out = await persistWatchdogHardIdlePartial({
      artifacts: {putCollectorContent: put},
      projectId: 'p',
      lease: {
        activity_id: 'a',
        attempt_id: 't',
        fencing_epoch: '1',
      },
      error: err,
    });
    expect(out).toBeNull();
    expect(put).not.toHaveBeenCalled();
    expect(buildWatchdogHardIdlePartialDocument(err)).toBeNull();
  });

  test('有 partial → PUT JSON 工件且 marksGoalDone=false', async () => {
    const err = new WatchdogHardIdleError(
      forceHard('streaming_llm', 'llm_stream'),
      'hel',
    );
    const bodyRef: {raw?: string} = {};
    const put = vi.fn(async (input: {body: string | Uint8Array; mime?: string}) => {
      bodyRef.raw = typeof input.body === 'string' ? input.body : '';
      expect(input.mime).toBe('application/json');
      return {
        artifactId: 'art-partial-1',
        digest: contentDigestSha256(input.body),
      };
    });

    const out = await persistWatchdogHardIdlePartial({
      artifacts: {putCollectorContent: put},
      projectId: '11111111-1111-1111-1111-111111111111',
      lease: {
        activity_id: '22222222-2222-2222-2222-222222222222',
        attempt_id: '33333333-3333-3333-3333-333333333333',
        fencing_epoch: '7',
      },
      error: err,
    });

    expect(out).toEqual({
      artifactId: 'art-partial-1',
      digest: expect.stringMatching(/^sha256:/),
      marksGoalDone: false,
    });
    expect(put).toHaveBeenCalledOnce();
    const doc = JSON.parse(bodyRef.raw!) as {
      kind: string;
      marks_goal_done: boolean;
      partial_assistant_text: string;
      phase: string;
    };
    expect(doc.kind).toBe('WATCHDOG_HARD_IDLE_PARTIAL');
    expect(doc.marks_goal_done).toBe(false);
    expect(doc.partial_assistant_text).toBe('hel');
    expect(doc.phase).toBe('streaming_llm');
    expect(formatWatchdogHardIdleFailReason(err, out!.artifactId)).toContain(
      'partial_artifact=art-partial-1',
    );
    expect(formatWatchdogHardIdleFailReason(err, out!.artifactId)).toMatch(
      /^WATCHDOG_HARD_IDLE:/,
    );
  });
});
