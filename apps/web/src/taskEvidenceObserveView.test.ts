import {describe, expect, it} from 'vitest';
import {
  evidenceCaption,
  evidenceExitCaption,
  evidenceRowsFromList,
} from './taskEvidenceObserveView.js';

describe('taskEvidenceObserveView', () => {
  it('投影列表字段', () => {
    const rows = evidenceRowsFromList([
      {
        id: 'ev-1',
        effect_id: 'ef-1',
        exit_code: 0,
        timed_out: false,
        candidate_manifest_id: 'cand-1',
        content_digest: 'sha256:abc',
        producer_identity: 'broker:exec',
        artifact_ids: ['a1', 'a2'],
        command_argv: ['pytest', '-q', 'tests'],
      },
    ]);
    expect(rows[0]).toMatchObject({
      id: 'ev-1',
      exitCode: 0,
      artifactCount: 2,
      commandPreview: 'pytest -q tests',
      candidateManifestId: 'cand-1',
    });
  });

  it('文案强调信封 ≠ PASS/DONE', () => {
    expect(evidenceCaption({rowCount: 1, timedOutCount: 0})).toContain(
      '≠ Goal DONE',
    );
    expect(evidenceExitCaption(0, false)).toContain('≠ Audit PASS');
    expect(evidenceExitCaption(1, false)).toContain('≠ Goal FAILED');
    expect(evidenceExitCaption(null, true)).toContain('timed_out');
  });
});
