import {describe, expect, it} from 'vitest';
import type {GoalWallBudgetSnapshot} from '@ring/api-client';
import {wallBudgetCaption} from './wallBudgetView.js';

describe('wallBudgetCaption', () => {
  it('正常剩余诚实且 ≠ DONE', () => {
    const snap: GoalWallBudgetSnapshot = {
      budget_usage_unknown: false,
      budget_exhausted: false,
      elapsed_wall_seconds: 7,
      active_seconds: 7,
      budget_remaining_wall_seconds: 3593,
      wall_clock_limit_seconds: 3600,
      marks_goal_done: false,
    };
    const text = wallBudgetCaption(snap);
    expect(text).toContain('剩余');
    expect(text).toContain('≠ DONE');
  });

  it('UNKNOWN / exhausted 失败关闭文案', () => {
    expect(
      wallBudgetCaption({
        budget_usage_unknown: true,
        budget_exhausted: true,
        marks_goal_done: false,
      }),
    ).toContain('UNKNOWN');
    expect(
      wallBudgetCaption({
        budget_usage_unknown: false,
        budget_exhausted: true,
        elapsed_wall_seconds: 3600,
        wall_clock_limit_seconds: 3600,
        marks_goal_done: false,
      }),
    ).toContain('已耗尽');
  });

  it('marks_goal_done=true 视为异常', () => {
    expect(
      wallBudgetCaption({
        budget_usage_unknown: false,
        budget_exhausted: false,
        marks_goal_done: true,
      }),
    ).toContain('异常');
  });
});
