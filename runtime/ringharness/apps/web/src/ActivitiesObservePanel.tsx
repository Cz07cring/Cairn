import {useQuery} from '@tanstack/react-query';
import {useMemo, useState} from 'react';
import {
  getAuthSession,
  listGoalActivities,
  type ActivityKind,
  type ActivityStatus,
  type BrowserClientAuth,
} from '@ring/api-client';
import {readStoredObserveGoalId} from './createGoalDraft.js';
import {
  activitiesCaption,
  activityRowsFromList,
  activityStatusCaption,
  countOrphanPlanActivities,
  taskBindingCaption,
} from './activitiesObserveView.js';

const KIND_OPTIONS: Array<'ALL' | ActivityKind> = [
  'ALL',
  'PLAN',
  'EXECUTE',
  'AUDIT',
  'INTEGRATE',
  'RECONCILE',
  'FINALIZE',
  'PROBE_MODEL',
  'VALIDATE_SKILL',
  'INDEX_MEMORY',
  'EXPORT_EVIDENCE',
];

const STATUS_OPTIONS: Array<'ALL' | ActivityStatus> = [
  'ALL',
  'PENDING',
  'READY',
  'RUNNING',
  'WAITING',
  'RECOVERING',
  'SUCCEEDED',
  'FAILED',
  'CANCELLED',
];

/**
 * Goal 活动时间线（含无 Task 的 PLAN）。
 * Activity SUCCEEDED ≠ Goal/Task DONE。
 */
export function ActivitiesObservePanel() {
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

  const [kindFilter, setKindFilter] = useState<'ALL' | ActivityKind>('ALL');
  const [statusFilter, setStatusFilter] = useState<'ALL' | ActivityStatus>(
    'ALL',
  );

  const storedGoalId = readStoredObserveGoalId();
  const observeGoalQuery = useQuery({
    queryKey: ['observe-goal-id'],
    queryFn: () => readStoredObserveGoalId(),
    staleTime: 0,
  });
  const goalId = (observeGoalQuery.data || storedGoalId).trim();

  const listQuery = useQuery({
    queryKey: [
      'goal-activities',
      goalId,
      kindFilter,
      statusFilter,
      auth?.kind,
    ],
    enabled: auth != null && Boolean(goalId),
    queryFn: ({signal}) =>
      listGoalActivities({
        auth: auth!,
        goalId,
        kind: kindFilter === 'ALL' ? undefined : kindFilter,
        status: statusFilter === 'ALL' ? undefined : statusFilter,
        signal,
      }),
    refetchInterval: 10_000,
  });

  const rows = useMemo(
    () => activityRowsFromList(listQuery.data ?? []),
    [listQuery.data],
  );
  const orphanCount = countOrphanPlanActivities(rows);

  if (auth == null && !sessionQuery.isPending) {
    return (
      <section className="empty" id="activities">
        <div className="symbol">⚠</div>
        <h2>每一步怎么执行</h2>
        <p>请先登录，登录后即可查看每一步的执行情况。</p>
      </section>
    );
  }

  return (
    <section className="create-goal observe" id="activities">
      <h2>每一步怎么执行</h2>
      <p className="lead-sm">查看系统每一次计划、执行和验收尝试，以及它现在进行到哪里。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>GET <code>{'/api/v1/goals/{goal_id}/activities'}</code>
        ；含无 Task 的首次 PLAN。Activity SUCCEEDED ≠ Goal/Task DONE。须先选择观察
        Goal。</p></details>

      {!goalId ? (
        <p className="observe-error">
          尚未选择要查看的目标（见上方观察选择器）。
        </p>
      ) : null}

      <div className="create-goal-grid">
        <label>
          执行类型
          <select
            value={kindFilter}
            onChange={(e) =>
              setKindFilter(e.target.value as 'ALL' | ActivityKind)
            }
            disabled={!goalId}
          >
            {KIND_OPTIONS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        <label>
          当前状态
          <select
            value={statusFilter}
            onChange={(e) =>
              setStatusFilter(e.target.value as 'ALL' | ActivityStatus)
            }
            disabled={!goalId}
          >
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
      </div>

      <p className="gate-caption" role="status">
        {activitiesCaption({
          rowCount: rows.length,
          orphanCount,
          kindFilter: kindFilter === 'ALL' ? null : kindFilter,
          statusFilter: statusFilter === 'ALL' ? null : statusFilter,
        })}
      </p>

      {!goalId ? null : listQuery.isPending ? (
        <p role="status">正在读取 activities…</p>
      ) : listQuery.isError ? (
        <p role="alert" className="observe-error">
          {listQuery.error instanceof Error
            ? listQuery.error.message
            : '读取失败'}
        </p>
      ) : rows.length === 0 ? (
        <p className="muted">
          当前筛选下无活动。空列表 ≠ Goal DONE。
        </p>
      ) : (
        <ul className="review-list">
          {rows.map((row) => (
            <li key={row.id}>
              <div className="review-head">
                <span
                  className={
                    row.status === 'FAILED'
                      ? 'sev-blocker'
                      : row.status === 'SUCCEEDED'
                        ? 'sev-ok'
                        : undefined
                  }
                >
                  {row.status}
                </span>
                <span>{row.kind}</span>
                <span className="muted">{taskBindingCaption(row.taskId)}</span>
              </div>
              <p className="muted">{activityStatusCaption(row.status)}</p>
              <p className="digest">
                activity {row.id} · retry {row.retryCount} · rev{' '}
                {row.stateRevision}
                {row.updatedAt ? ` · updated ${row.updatedAt}` : ''}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
