import {describe, expect, it} from 'vitest';
import {
  obligationRowLabel,
  quarantineComponentFromStatus,
  quarantineInboxCaption,
} from './quarantineInboxView.js';

describe('quarantineInboxView', () => {
  it('提取 quarantine 组件', () => {
    const c = quarantineComponentFromStatus({
      components: [
        {
          name: 'verification_obligation_quarantine',
          state: 'DEGRADED',
          reason_code: 'QUARANTINED_OBLIGATIONS:1',
        },
      ],
    });
    expect(c?.state).toBe('DEGRADED');
    expect(c?.reasonCode).toContain('QUARANTINED');
  });

  it('文案不把隔离当成 DONE', () => {
    const text = quarantineInboxCaption({
      component: {
        name: 'verification_obligation_quarantine',
        state: 'DEGRADED',
        reasonCode: 'QUARANTINED_OBLIGATIONS:2',
      },
      rowCount: 2,
    });
    expect(text).toContain('DEGRADED');
    expect(text).toContain('≠ DONE');
  });

  it('行标签', () => {
    expect(
      obligationRowLabel({
        id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        status: 'QUARANTINED',
        layer: 'MECHANICAL',
        audit_round: 1,
      }),
    ).toContain('QUARANTINED');
  });
});
