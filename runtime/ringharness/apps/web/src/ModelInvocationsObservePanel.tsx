import {useQuery} from '@tanstack/react-query';
import {useMemo, useState} from 'react';
import {
  getAuthSession,
  getGoal,
  listModelInvocations,
  listProjects,
  type BrowserClientAuth,
} from '@ring/api-client';
import {readStoredObserveGoalId} from './createGoalDraft.js';
import {
  modelInvocationRowsFromList,
  modelInvocationStatusCaption,
  modelInvocationsCaption,
} from './modelInvocationsObserveView.js';

/**
 * 模型调用只读观察。
 * SUCCEEDED ≠ Goal DONE；不展示原始输入。
 */
export function ModelInvocationsObservePanel() {
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

  const [projectId, setProjectId] = useState('');
  const [scopeFilter, setScopeFilter] = useState<'GOAL' | 'PROJECT'>('GOAL');

  const storedGoalId = readStoredObserveGoalId();
  const observeGoalQuery = useQuery({
    queryKey: ['observe-goal-id'],
    queryFn: () => readStoredObserveGoalId(),
    staleTime: 0,
  });
  const goalId = (observeGoalQuery.data || storedGoalId).trim();

  const projectsQuery = useQuery({
    queryKey: ['projects', auth?.kind],
    enabled: auth != null && !sessionQuery.isPending,
    queryFn: ({signal}) => listProjects({auth: auth!, signal}),
  });

  const goalQuery = useQuery({
    queryKey: ['model-invocations-goal', goalId, auth?.kind],
    enabled: auth != null && Boolean(goalId),
    queryFn: ({signal}) => getGoal({auth: auth!, goalId, signal}),
  });

  const resolvedProjectId =
    projectId ||
    goalQuery.data?.project_id ||
    projectsQuery.data?.[0]?.id ||
    String(import.meta.env.VITE_RING_PROJECT_ID ?? '').trim();

  const listQuery = useQuery({
    queryKey: [
      'model-invocations',
      resolvedProjectId,
      scopeFilter === 'GOAL' ? goalId : '',
      auth?.kind,
    ],
    enabled:
      auth != null &&
      !sessionQuery.isPending &&
      Boolean(resolvedProjectId) &&
      (scopeFilter === 'PROJECT' || Boolean(goalId)),
    queryFn: ({signal}) =>
      listModelInvocations({
        auth: auth!,
        projectId: resolvedProjectId,
        goalId: scopeFilter === 'GOAL' ? goalId : undefined,
        signal,
      }),
    refetchInterval: 15_000,
  });

  const rows =
    listQuery.data != null ? modelInvocationRowsFromList(listQuery.data) : [];
  const succeededCount = rows.filter((r) => r.status === 'SUCCEEDED').length;
  const unknownCount = rows.filter((r) => r.status === 'UNKNOWN').length;

  return (
    <section id="model-invocations" aria-labelledby="model-invocations-title">
      <h2 id="model-invocations-title">模型做了哪些工作</h2>
      <p className="lead-sm">查看模型调用是否返回、耗时与用量；这里不展示敏感原始输入。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>GET <code>/api/v1/model-invocations</code>
        。SUCCEEDED / usage CONFIRMED ≠ Goal DONE。不展示原始输入。</p></details>

      <div className="create-goal-grid">
        <label>
          项目
          <select
            value={resolvedProjectId}
            onChange={(e) => setProjectId(e.target.value)}
            disabled={projectsQuery.isPending}
          >
            {(projectsQuery.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name || p.id}
              </option>
            ))}
            {!projectsQuery.data?.length && resolvedProjectId ? (
              <option value={resolvedProjectId}>{resolvedProjectId}</option>
            ) : null}
          </select>
        </label>
        <label>
          范围
          <select
            value={scopeFilter}
            onChange={(e) =>
              setScopeFilter(e.target.value as 'GOAL' | 'PROJECT')
            }
            disabled={!resolvedProjectId}
          >
            <option value="GOAL">当前观察 Goal</option>
            <option value="PROJECT">整个项目</option>
          </select>
        </label>
      </div>

      <p className="gate-caption" role="status">
        {modelInvocationsCaption({
          rowCount: rows.length,
          succeededCount,
          unknownCount,
        })}
      </p>

      {scopeFilter === 'GOAL' && !goalId ? (
        <p className="observe-error">请先选择要查看的目标，或改为「整个项目」。</p>
      ) : !resolvedProjectId ? (
        <p className="observe-error">请选择项目。</p>
      ) : listQuery.isPending ? (
        <p role="status">正在读取 model-invocations…</p>
      ) : listQuery.isError ? (
        <p role="alert" className="observe-error">
          {listQuery.error instanceof Error
            ? listQuery.error.message
            : '读取失败'}
        </p>
      ) : rows.length === 0 ? (
        <p className="muted">当前筛选下无模型调用。空列表 ≠ Goal DONE。</p>
      ) : (
        <ul className="review-list">
          {rows.map((row) => (
            <li key={row.id}>
              <div className="review-head">
                <span
                  className={
                    row.status === 'SUCCEEDED'
                      ? 'sev-ok'
                      : row.status === 'UNKNOWN' || row.status === 'FAILED'
                        ? 'sev-blocker'
                        : undefined
                  }
                >
                  {row.status}
                </span>
                <span>
                  {row.modelId} · {row.providerRef}
                </span>
                <span className="muted">
                  seq {row.invocationSeq} · tools {row.toolCallCount} · usage{' '}
                  {row.usageStatus}
                </span>
              </div>
              <p className="muted">
                {modelInvocationStatusCaption(row.status)}
              </p>
              <p className="digest">
                invocation {row.id}
                {row.goalId ? ` · goal ${row.goalId}` : ''}
                {' · '}
                activity {row.activityId}
              </p>
              <p className="digest">
                input {row.inputDigest.slice(0, 24)}…
                {' · '}
                payload {row.payloadDigest.slice(0, 24)}…
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
