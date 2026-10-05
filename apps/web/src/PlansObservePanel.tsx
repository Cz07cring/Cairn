import {useQuery} from '@tanstack/react-query';
import {useMemo} from 'react';
import {
  getAuthSession,
  listGoalPlans,
  type BrowserClientAuth,
} from '@ring/api-client';
import {readStoredObserveGoalId} from './createGoalDraft.js';
import {
  countPublishedPlans,
  planRowsFromList,
  planStatusCaption,
  plansCaption,
} from './plansObserveView.js';

/**
 * Goal Plan 列表观察。
 * Plan PUBLISHED ≠ Goal DONE。
 */
export function PlansObservePanel() {
  const sessionQuery = useQuery({
    queryKey: ['auth-session'],
    queryFn: ({signal}) => getAuthSession({signal}),
    retry: false,
  });

  const auth: BrowserClientAuth | null = useMemo(() => {
    if (sessionQuery.data) {
      return {kind: 'session', csrfToken: sessionQuery.data.csrf_token};
    }
    const bearer = String(import.meta.env.VITE_RING_DEV_BEARER ?? '').trim();
    if (bearer) {
      return {kind: 'bearer', authorization: bearer};
    }
    return null;
  }, [sessionQuery.data]);

  const storedGoalId = readStoredObserveGoalId();
  const observeGoalQuery = useQuery({
    queryKey: ['observe-goal-id'],
    queryFn: () => readStoredObserveGoalId(),
    staleTime: 0,
  });
  const goalId = (observeGoalQuery.data || storedGoalId).trim();

  const listQuery = useQuery({
    queryKey: ['goal-plans', goalId, auth?.kind],
    enabled: auth != null && Boolean(goalId),
    queryFn: ({signal}) =>
      listGoalPlans({
        auth: auth!,
        goalId,
        signal,
      }),
    refetchInterval: 10_000,
  });

  const rows = useMemo(
    () => planRowsFromList(listQuery.data ?? []),
    [listQuery.data],
  );
  const publishedCount = countPublishedPlans(rows);

  if (auth == null && !sessionQuery.isPending) {
    return (
      <section className="empty" id="plans">
        <div className="symbol">⚠</div>
        <h2>系统准备怎么做</h2>
        <p>请先登录，登录后即可查看工作计划。</p>
      </section>
    );
  }

  return (
    <section className="create-goal observe" id="plans">
      <h2>系统准备怎么做</h2>
      <p className="lead-sm">查看系统准备按什么顺序完成目标，以及计划是否已正式采用。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>GET <code>{'/api/v1/goals/{goal_id}/plans'}</code>
        。Plan PUBLISHED ≠ Goal DONE。须先选择观察 Goal。</p></details>

      {!goalId ? (
        <p className="observe-error">
          尚未选择要查看的目标（见上方观察选择器）。
        </p>
      ) : null}

      <p className="gate-caption" role="status">
        {plansCaption({rowCount: rows.length, publishedCount})}
      </p>

      {!goalId ? null : listQuery.isPending ? (
        <p role="status">正在读取 plans…</p>
      ) : listQuery.isError ? (
        <p role="alert" className="observe-error">
          {listQuery.error instanceof Error
            ? listQuery.error.message
            : '读取失败'}
        </p>
      ) : rows.length === 0 ? (
        <p className="muted">暂无计划。空列表 ≠ Goal DONE。</p>
      ) : (
        <ul className="review-list">
          {rows.map((row) => (
            <li key={row.id}>
              <div className="review-head">
                <span
                  className={
                    row.status === 'REJECTED'
                      ? 'sev-blocker'
                      : row.status === 'PUBLISHED'
                        ? 'sev-ok'
                        : undefined
                  }
                >
                  {row.status}
                </span>
                <span>
                  rev {row.planRevision == null ? '—' : row.planRevision}
                </span>
                <span className="muted">{row.taskCount} tasks</span>
              </div>
              <p className="muted">{planStatusCaption(row.status)}</p>
              {row.reason ? <p className="muted">reason: {row.reason}</p> : null}
              <p className="digest">
                plan {row.id}
                {row.updatedAt ? ` · updated ${row.updatedAt}` : ''}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
