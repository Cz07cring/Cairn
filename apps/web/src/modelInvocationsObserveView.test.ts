import {describe, expect, it} from 'vitest';
import {
  modelInvocationRowsFromList,
  modelInvocationStatusCaption,
  modelInvocationsCaption,
} from './modelInvocationsObserveView.js';

describe('modelInvocationsObserveView', () => {
  it('投影列表且不依赖 assistant 正文', () => {
    const rows = modelInvocationRowsFromList([
      {
        id: 'inv-1',
        status: 'SUCCEEDED',
        usage_status: 'CONFIRMED',
        model_id: 'deepseek-flash',
        provider_ref: 'deepseek:api',
        goal_id: 'g1',
        activity_id: 'a1',
        invocation_seq: 2,
        input_digest: 'sha256:in',
        payload_digest: 'sha256:pay',
        tool_calls: [{name: 'read_file'}],
      },
      {
        id: 'inv-2',
        status: 'UNKNOWN',
        usage_status: 'UNKNOWN',
        model_id: 'deepseek-flash',
        provider_ref: 'deepseek:api',
        goal_id: null,
        activity_id: 'a2',
        invocation_seq: 1,
        input_digest: 'sha256:in2',
        payload_digest: 'sha256:pay2',
        tool_calls: null,
      },
    ]);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({
      id: 'inv-1',
      status: 'SUCCEEDED',
      toolCallCount: 1,
      goalId: 'g1',
    });
    expect(rows[1]?.goalId).toBe('');
    expect(rows[1]?.toolCallCount).toBe(0);
  });

  it('文案强调 SUCCEEDED ≠ DONE', () => {
    expect(
      modelInvocationsCaption({
        rowCount: 2,
        succeededCount: 1,
        unknownCount: 1,
      }),
    ).toContain('≠ Goal DONE');
    expect(modelInvocationStatusCaption('SUCCEEDED')).toContain('≠ Goal');
    expect(modelInvocationStatusCaption('UNKNOWN')).toContain('对账');
  });
});
