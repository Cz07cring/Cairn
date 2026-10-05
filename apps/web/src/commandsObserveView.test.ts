import {describe, expect, it} from 'vitest';
import {
  commandRowLabel,
  commandsCaption,
  controlSuccessCaption,
  goalControlEligibility,
} from './commandsObserveView.js';

describe('commandsObserveView', () => {
  it('pause/resume/cancel 门禁对齐 Kernel', () => {
    expect(goalControlEligibility('RUNNING', true).canPause).toBe(true);
    expect(goalControlEligibility('RUNNING', true).canCancel).toBe(true);
    expect(goalControlEligibility('PAUSED', true).canResume).toBe(true);
    expect(goalControlEligibility('PAUSED', true).canPause).toBe(false);
    expect(goalControlEligibility('DRAFT', true).canCancel).toBe(false);
    expect(goalControlEligibility('DONE', true).canCancel).toBe(false);
    expect(goalControlEligibility('RUNNING', false).canPause).toBe(false);
  });

  it('文案不把命令成功当成 DONE', () => {
    expect(
      commandsCaption({count: 2, anySucceeded: true}),
    ).toContain('≠ Goal DONE');
    expect(
      controlSuccessCaption({
        action: 'pause',
        commandStatus: 'SUCCEEDED',
        finalStatus: 'PAUSING',
      }),
    ).toContain('≠ DONE');
    expect(
      commandRowLabel({
        id: '11111111-1111-1111-1111-111111111111',
        kind: 'START',
        status: 'SUCCEEDED',
        result: {final_status: 'PLANNING'},
      }),
    ).toContain('PLANNING');
  });
});
