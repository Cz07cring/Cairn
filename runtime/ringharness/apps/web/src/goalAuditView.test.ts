import {describe, expect, it} from 'vitest';
import type {GoalAuditItem} from '@ring/api-client';
import {
  goalReviewRowsFromItems,
  latestReviewHasBlocker,
  reviewBudgetCaption,
  reviewGateCaption,
} from './goalAuditView.js';

function reviewItem(
  id: string,
  findings: Array<{code: string; severity: 'INFO' | 'WARN' | 'BLOCKER'; recommendation: string}>,
): GoalAuditItem {
  return {
    record_type: 'GOAL_REVIEW',
    review: {
      id,
      created_at: '2026-09-13T00:00:00Z',
      project_id: 'p1',
      goal_id: 'g1',
      producer_activity_id: 'a1',
      producer_attempt_id: 't1',
      goal_contract_revision: 1,
      plan_revision: 1,
      review_snapshot_digest: 'sha256:' + 'ab'.repeat(32),
      findings: findings.map((f) => ({
        code: f.code,
        severity: f.severity,
        evidence_ids: [],
        recommendation: f.recommendation,
      })),
      content_digest: 'sha256:' + 'cd'.repeat(32),
    },
  };
}

describe('goalAuditView', () => {
  it('投影 GOAL_REVIEW 并识别 BLOCKER', () => {
    const rows = goalReviewRowsFromItems([
      reviewItem('r1', [
        {code: 'STUCK', severity: 'BLOCKER', recommendation: '停'},
      ]),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0]?.hasBlocker).toBe(true);
    expect(rows[0]?.findings[0]?.code).toBe('STUCK');
  });

  it('最新无 BLOCKER 时门控文案不宣称 DONE/PASS', () => {
    const rows = goalReviewRowsFromItems([
      reviewItem('r1', [
        {code: 'STUCK', severity: 'BLOCKER', recommendation: '停'},
      ]),
      reviewItem('r2', [
        {code: 'CLEARED', severity: 'INFO', recommendation: '继续'},
      ]),
    ]);
    expect(latestReviewHasBlocker(rows)).toBe(false);
    const caption = reviewGateCaption(rows);
    expect(caption).toContain('无 BLOCKER');
    expect(caption).toContain('不贡献 PASS');
    expect(caption).toContain('VerificationProfile');
    expect(caption).toMatch(/裁决 DONE/);
  });

  it('空列表文案诚实', () => {
    expect(reviewGateCaption([])).toContain('尚无周期诊断');
    expect(latestReviewHasBlocker([])).toBe(false);
  });

  it('复盘预算文案不把 eligible 当成 DONE', () => {
    const caption = reviewBudgetCaption({
      max_reviews: 2,
      reviews_used: 0,
      reviews_remaining: 2,
      min_interval_seconds: 5,
      stagnation_seconds: 10,
      eligible_now: false,
      blocking_reason_code: 'GOAL_REVIEW_NOT_STAGNANT',
      marks_goal_done: false,
    });
    expect(caption).toContain('0/2');
    expect(caption).toContain('GOAL_REVIEW_NOT_STAGNANT');
    expect(caption).toContain('≠ Goal DONE');
    expect(
      reviewBudgetCaption({
        max_reviews: 1,
        reviews_used: 0,
        reviews_remaining: 1,
        min_interval_seconds: 1,
        stagnation_seconds: 1,
        eligible_now: true,
        marks_goal_done: true,
      }),
    ).toContain('marks_goal_done=true');
  });
});
