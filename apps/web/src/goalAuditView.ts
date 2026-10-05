/**
 * Goal 联合审计 → 只读展示模型。
 * GOAL_REVIEW 是周期诊断：≠ criterion PASS；≠ Goal DONE。
 */
import type {GoalAuditItem} from '@ring/api-client';

export type GoalReviewFindingView = {
  code: string;
  severity: string;
  recommendation: string;
};

export type GoalReviewRow = {
  id: string;
  createdAt: string | null;
  findings: GoalReviewFindingView[];
  hasBlocker: boolean;
  snapshotDigest: string;
};

export function goalReviewRowsFromItems(items: GoalAuditItem[]): GoalReviewRow[] {
  const rows: GoalReviewRow[] = [];
  for (const item of items) {
    if (item.record_type !== 'GOAL_REVIEW') {
      continue;
    }
    const review = item.review;
    const findings = (review.findings ?? []).map((f) => ({
      code: String(f.code ?? ''),
      severity: String(f.severity ?? ''),
      recommendation: String(f.recommendation ?? ''),
    }));
    rows.push({
      id: review.id,
      createdAt: review.created_at ?? null,
      findings,
      hasBlocker: findings.some((f) => f.severity === 'BLOCKER'),
      snapshotDigest: String(review.review_snapshot_digest ?? ''),
    });
  }
  return rows;
}

/** 列表按 created_at ASC；工程门控只看最新一条。 */
export function latestReviewHasBlocker(rows: GoalReviewRow[]): boolean {
  if (rows.length === 0) {
    return false;
  }
  return rows[rows.length - 1]?.hasBlocker === true;
}

export function reviewGateCaption(rows: GoalReviewRow[]): string {
  if (rows.length === 0) {
    return '尚无周期诊断；不表示验收通过，也不表示 Goal DONE。';
  }
  if (latestReviewHasBlocker(rows)) {
    return '最新 GoalReview 含 BLOCKER：禁止新 ENGINEERING / 最终屏障 SEAL（≠ Goal BLOCKED）。';
  }
  return '最新 GoalReview 无 BLOCKER：诊断不贡献 PASS，Goal 仍须经 Kernel+VerificationProfile 裁决 DONE。';
}

export type ReviewBudgetView = {
  max_reviews: number;
  reviews_used: number;
  reviews_remaining: number;
  min_interval_seconds: number;
  stagnation_seconds: number;
  eligible_now: boolean;
  blocking_reason_code?: string | null;
  marks_goal_done?: boolean;
};

/** 复盘预算文案；eligible/marks 异常时明确失败关闭提示。 */
export function reviewBudgetCaption(snap: ReviewBudgetView | null): string {
  if (snap == null) {
    return '复盘预算未加载。';
  }
  if (snap.marks_goal_done === true) {
    return '异常：goal-review-budget 快照 marks_goal_done=true（协议应恒 false）；禁止当作 Goal DONE。';
  }
  const base = `复盘预算 ${snap.reviews_used}/${snap.max_reviews}（剩余 ${snap.reviews_remaining}）；间隔 ≥${snap.min_interval_seconds}s；停滞 ≥${snap.stagnation_seconds}s。`;
  if (snap.eligible_now) {
    return `${base} 当前可通过预算门（≠ 已 claim，≠ DONE）。`;
  }
  const code = snap.blocking_reason_code ?? 'UNKNOWN';
  return `${base} 当前不可新建（${code}）；≠ Goal DONE。`;
}
