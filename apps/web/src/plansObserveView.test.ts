import {describe, expect, it} from 'vitest';
import {
  countPublishedPlans,
  planRowsFromList,
  planStatusCaption,
  plansCaption,
} from './plansObserveView.js';

describe('plansObserveView', () => {
  it('投影列表并强调 PUBLISHED ≠ Goal DONE', () => {
    const rows = planRowsFromList([
      {
        id: 'p1',
        status: 'PUBLISHED',
        plan_revision: 2,
        tasks: [{}, {}],
        reason: 'v2',
      },
      {
        id: 'p2',
        status: 'CANDIDATE',
        plan_revision: null,
        tasks: [{}],
        reason: 'draft',
      },
    ]);
    expect(rows).toHaveLength(2);
    expect(rows[0]?.taskCount).toBe(2);
    expect(countPublishedPlans(rows)).toBe(1);
    const caption = plansCaption({rowCount: 2, publishedCount: 1});
    expect(caption).toContain('PUBLISHED 1');
    expect(caption).toContain('≠ Goal DONE');
  });

  it('各状态文案禁止冒充 DONE', () => {
    expect(planStatusCaption('PUBLISHED')).toContain('≠ Goal DONE');
    expect(planStatusCaption('CANDIDATE')).toContain('≠ DONE');
  });
});
