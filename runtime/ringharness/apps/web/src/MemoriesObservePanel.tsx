import {useQuery} from '@tanstack/react-query';
import {useMemo, useState} from 'react';
import {
  getAuthSession,
  getGoal,
  listMemories,
  listProjects,
  type BrowserClientAuth,
  type MemoryKind,
} from '@ring/api-client';
import {readStoredObserveGoalId} from './createGoalDraft.js';
import {
  countVerifiedMemories,
  memoriesCaption,
  memoryRowsFromList,
  memoryStatusCaption,
} from './memoriesObserveView.js';

const KIND_OPTIONS: Array<'ALL' | MemoryKind> = [
  'ALL',
  'fact',
  'decision',
  'failure',
  'hypothesis',
  'question',
];

/**
 * 项目记忆只读观察。
 * Memory VERIFIED ≠ Goal DONE。
 */
export function MemoriesObservePanel() {
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
  const [kindFilter, setKindFilter] = useState<'ALL' | MemoryKind>('ALL');
  const [q, setQ] = useState('');

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
    queryKey: ['memories-goal', goalId, auth?.kind],
    enabled: auth != null && Boolean(goalId),
    queryFn: ({signal}) => getGoal({auth: auth!, goalId, signal}),
  });

  const resolvedProjectId =
    projectId || goalQuery.data?.project_id || '';

  const listQuery = useQuery({
    queryKey: [
      'memories',
      resolvedProjectId,
      kindFilter,
      q,
      auth?.kind,
    ],
    enabled: auth != null && Boolean(resolvedProjectId),
    queryFn: ({signal}) =>
      listMemories({
        auth: auth!,
        projectId: resolvedProjectId,
        kind: kindFilter === 'ALL' ? undefined : kindFilter,
        q: q.trim() || undefined,
        signal,
      }),
    refetchInterval: 15_000,
  });

  const rows = useMemo(
    () => memoryRowsFromList(listQuery.data ?? []),
    [listQuery.data],
  );
  const verifiedCount = countVerifiedMemories(rows);

  if (auth == null && !sessionQuery.isPending) {
    return (
      <section className="empty" id="memories">
        <div className="symbol">⚠</div>
        <h2>执行过程中记住的信息</h2>
        <p>请先登录，登录后即可查看执行过程中记住的信息。</p>
      </section>
    );
  }

  return (
    <section className="create-goal observe" id="memories">
      <h2>执行过程中记住的信息</h2>
      <p className="lead-sm">查看系统为后续步骤保留的事实和上下文。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>GET <code>/api/v1/memories</code>
        。Memory VERIFIED ≠ Goal DONE。可按项目 / kind / 关键词筛选。</p></details>

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
          信息类型
          <select
            value={kindFilter}
            onChange={(e) =>
              setKindFilter(e.target.value as 'ALL' | MemoryKind)
            }
            disabled={!resolvedProjectId}
          >
            {KIND_OPTIONS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        <label>
          搜索关键词
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            disabled={!resolvedProjectId}
            maxLength={200}
            placeholder="可选"
          />
        </label>
      </div>

      <p className="gate-caption" role="status">
        {memoriesCaption({
          rowCount: rows.length,
          verifiedCount,
          kindFilter: kindFilter === 'ALL' ? null : kindFilter,
        })}
      </p>

      {!resolvedProjectId ? (
        <p className="observe-error">
          请选择项目，或先选择观察 Goal 以推断 project_id。
        </p>
      ) : listQuery.isPending ? (
        <p role="status">正在读取 memories…</p>
      ) : listQuery.isError ? (
        <p role="alert" className="observe-error">
          {listQuery.error instanceof Error
            ? listQuery.error.message
            : '读取失败'}
        </p>
      ) : rows.length === 0 ? (
        <p className="muted">当前筛选下无记忆。空列表 ≠ Goal DONE。</p>
      ) : (
        <ul className="review-list">
          {rows.map((row) => (
            <li key={row.id}>
              <div className="review-head">
                <span
                  className={
                    row.status === 'VERIFIED'
                      ? 'sev-ok'
                      : row.status === 'SUPERSEDED'
                        ? 'sev-blocker'
                        : undefined
                  }
                >
                  {row.status}
                </span>
                <span>{row.kind}</span>
                <span className="muted">
                  confidence {row.confidenceBp}bp · evidence{' '}
                  {row.sourceEvidenceCount}
                </span>
              </div>
              <p>{row.statement}</p>
              <p className="muted">{memoryStatusCaption(row.status)}</p>
              <p className="digest">
                memory {row.id}
                {row.updatedAt ? ` · updated ${row.updatedAt}` : ''}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
