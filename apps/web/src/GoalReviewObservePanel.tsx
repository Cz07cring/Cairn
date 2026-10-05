import {useQuery} from '@tanstack/react-query';
import {getAuthSession, getGoalReviewBudget, listGoalAuditItems} from '@ring/api-client';
import {readStoredObserveGoalId} from './createGoalDraft.js';
import {
  goalReviewRowsFromItems,
  reviewBudgetCaption,
  reviewGateCaption,
  type GoalReviewRow,
} from './goalAuditView.js';
import {
  observeConfigEmptyHint,
  resolveObserveConfig,
} from './observeConfig.js';

function ReviewList({rows}: {rows: GoalReviewRow[]}) {
  if (rows.length === 0) {
    return (
      <p className="muted">
        该 Goal 尚无 GOAL_REVIEW 记录。空列表 ≠ 验收 PASS，≠ Goal DONE。
      </p>
    );
  }
  return (
    <ul className="review-list">
      {rows.map((row) => (
        <li key={row.id}>
          <div className="review-head">
            <span className={row.hasBlocker ? 'sev-blocker' : 'sev-ok'}>
              {row.hasBlocker ? '含 BLOCKER' : '无 BLOCKER'}
            </span>
            <time dateTime={row.createdAt ?? undefined}>
              {row.createdAt ?? '时间未知'}
            </time>
          </div>
          <ul className="finding-list">
            {row.findings.map((f, i) => (
              <li key={`${row.id}:${f.code}:${i}`}>
                <strong>{f.code}</strong>
                <span className={`sev-${f.severity.toLowerCase()}`}>{f.severity}</span>
                <span>{f.recommendation}</span>
              </li>
            ))}
          </ul>
          <p className="digest">{row.snapshotDigest}</p>
        </li>
      ))}
    </ul>
  );
}

/**
 * 周期诊断面板：优先 OIDC session，其次 DEV_BEARER；须 VITE_RING_GOAL_ID。
 * findings ≠ PASS ≠ Goal DONE。
 */
export function GoalReviewObservePanel() {
  const sessionQuery = useQuery({
    queryKey: ['auth-session'],
    queryFn: ({signal}) => getAuthSession({signal}),
    retry: false,
  });
  const storedGoal = useQuery({
    queryKey: ['observe-goal-id'],
    queryFn: async () => readStoredObserveGoalId(),
  });
  const cfg = resolveObserveConfig({
    goalIdEnv: String(import.meta.env.VITE_RING_GOAL_ID ?? ''),
    bearerEnv: String(import.meta.env.VITE_RING_DEV_BEARER ?? ''),
    session: sessionQuery.data,
    storedGoalId: storedGoal.data,
  });
  const query = useQuery({
    queryKey: ['goal-audits', cfg?.goalId, cfg?.source],
    enabled: cfg != null && !sessionQuery.isPending,
    queryFn: ({signal}) =>
      listGoalAuditItems({
        goalId: cfg!.goalId,
        auth: cfg!.auth,
        signal,
        limit: 50,
      }),
    refetchInterval: cfg ? 30_000 : false,
  });
  const budgetQuery = useQuery({
    queryKey: ['goal-review-budget', cfg?.goalId, cfg?.source],
    enabled: cfg != null && !sessionQuery.isPending,
    queryFn: ({signal}) =>
      getGoalReviewBudget({
        goalId: cfg!.goalId,
        auth: cfg!.auth,
        signal,
      }),
    refetchInterval: cfg ? 30_000 : false,
  });

  const rows =
    query.data != null ? goalReviewRowsFromItems(query.data) : ([] as GoalReviewRow[]);

  return (
    <section id="goal-reviews" className="observe" aria-labelledby="goal-reviews-title">
      <h2 id="goal-reviews-title">系统有没有走偏</h2>
      <p className="lead-sm">系统会定期比较目标、计划和当前工作，记录是否偏离及建议。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>只读观察 Kernel 落库的 GOAL_REVIEW。findings 不是 criterion PASS，也不能把 Goal
        标成 DONE。</p></details>
      {cfg == null ? (
        <div className="observe-empty">
          <p>{observeConfigEmptyHint(null)}</p>
        </div>
      ) : sessionQuery.isPending || query.isPending ? (
        <p role="status">正在读取 Goal 审计…</p>
      ) : query.isError ? (
        <p role="alert" className="observe-error">
          {query.error instanceof Error ? query.error.message : '读取失败'}
        </p>
      ) : (
        <>
          <p className="muted">{observeConfigEmptyHint(cfg)}</p>
          <p className="gate-caption" role="status">
            {budgetQuery.isError
              ? '复盘预算读取失败（不影响审计列表）；≠ DONE。'
              : reviewBudgetCaption(budgetQuery.data ?? null)}
          </p>
          <p className="gate-caption" role="status">
            {reviewGateCaption(rows)}
          </p>
          <ReviewList rows={rows} />
        </>
      )}
    </section>
  );
}
