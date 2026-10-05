import {useQuery} from '@tanstack/react-query';
import {useMemo, useState} from 'react';
import {
  getAuthSession,
  listGoalTasks,
  type BrowserClientAuth,
  type TaskStatus,
} from '@ring/api-client';
import {readStoredObserveGoalId} from './createGoalDraft.js';
import {
  countTaskDone,
  taskRowsFromList,
  taskStatusCaption,
  tasksCaption,
} from './tasksObserveView.js';

const STATUS_OPTIONS: Array<'ALL' | TaskStatus> = [
  'ALL',
  'PENDING',
  'READY',
  'RUNNING',
  'VERIFYING',
  'RETRYING',
  'BLOCKED',
  'STALE',
  'RECOVERING',
  'DONE',
  'FAILED',
  'CANCELLED',
];

/**
 * Goal Task 列表观察。
 * Task.status=DONE ≠ Goal DONE。
 */
export function TasksObservePanel() {
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

  const [statusFilter, setStatusFilter] = useState<'ALL' | TaskStatus>('ALL');

  const storedGoalId = readStoredObserveGoalId();
  const observeGoalQuery = useQuery({
    queryKey: ['observe-goal-id'],
    queryFn: () => readStoredObserveGoalId(),
    staleTime: 0,
  });
  const goalId = (observeGoalQuery.data || storedGoalId).trim();

  const listQuery = useQuery({
    queryKey: ['goal-tasks', goalId, statusFilter, auth?.kind],
    enabled: auth != null && Boolean(goalId),
    queryFn: ({signal}) =>
      listGoalTasks({
        auth: auth!,
        goalId,
        status: statusFilter === 'ALL' ? undefined : statusFilter,
        signal,
      }),
    refetchInterval: 10_000,
  });

  const rows = useMemo(
    () => taskRowsFromList(listQuery.data ?? []),
    [listQuery.data],
  );
  const doneCount = countTaskDone(rows);

  if (auth == null && !sessionQuery.isPending) {
    return (
      <section className="empty" id="tasks">
        <div className="symbol">⚠</div>
        <h2>任务步骤</h2>
        <p>请先登录，登录后即可查看任务步骤。</p>
      </section>
    );
  }

  return (
    <section className="create-goal observe" id="tasks">
      <h2>任务步骤</h2>
      <p className="lead-sm">查看目标被拆成哪些步骤、当前状态和阻塞原因。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>GET <code>{'/api/v1/goals/{goal_id}/tasks'}</code>
        。Task.status=DONE ≠ Goal DONE。支持 status 查询。须先选择观察 Goal。</p></details>

      {!goalId ? (
        <p className="observe-error">
          尚未选择要查看的目标（见上方观察选择器）。
        </p>
      ) : null}

      <div className="create-goal-grid">
        <label>
          查看状态
          <select
            value={statusFilter}
            onChange={(e) =>
              setStatusFilter(e.target.value as 'ALL' | TaskStatus)
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
        {tasksCaption({
          rowCount: rows.length,
          doneCount,
          statusFilter: statusFilter === 'ALL' ? null : statusFilter,
        })}
      </p>

      {!goalId ? null : listQuery.isPending ? (
        <p role="status">正在读取 tasks…</p>
      ) : listQuery.isError ? (
        <p role="alert" className="observe-error">
          {listQuery.error instanceof Error
            ? listQuery.error.message
            : '读取失败'}
        </p>
      ) : rows.length === 0 ? (
        <p className="muted">当前筛选下无任务。空列表 ≠ Goal DONE。</p>
      ) : (
        <ul className="review-list">
          {rows.map((row) => (
            <li key={row.id}>
              <div className="review-head">
                <span
                  className={
                    row.status === 'FAILED' || row.status === 'BLOCKED'
                      ? 'sev-blocker'
                      : row.status === 'DONE'
                        ? 'sev-ok'
                        : undefined
                  }
                >
                  {row.status}
                </span>
                <span>{row.objective}</span>
              </div>
              <p className="muted">{taskStatusCaption(row.status)}</p>
              {row.blockReason ? (
                <p className="muted">block_reason: {row.blockReason}</p>
              ) : null}
              <p className="digest">
                task {row.id} · plan_rev {row.planRevision} · contract_rev{' '}
                {row.contractRevision} · state_rev {row.stateRevision} · round{' '}
                {row.executionRound}
                {row.updatedAt ? ` · updated ${row.updatedAt}` : ''}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
