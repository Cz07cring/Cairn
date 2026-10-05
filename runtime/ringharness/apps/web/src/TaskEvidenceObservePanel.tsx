import {useQuery} from '@tanstack/react-query';
import {useMemo, useState} from 'react';
import {
  getAuthSession,
  listGoalTasks,
  listTaskEvidence,
  type BrowserClientAuth,
} from '@ring/api-client';
import {readStoredObserveGoalId} from './createGoalDraft.js';
import {
  evidenceCaption,
  evidenceExitCaption,
  evidenceRowsFromList,
} from './taskEvidenceObserveView.js';

/**
 * Task EvidenceEnvelope 只读观察。
 * 信封行 ≠ Audit PASS ≠ Goal DONE。
 */
export function TaskEvidenceObservePanel() {
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

  const [taskId, setTaskId] = useState('');

  const storedGoalId = readStoredObserveGoalId();
  const observeGoalQuery = useQuery({
    queryKey: ['observe-goal-id'],
    queryFn: () => readStoredObserveGoalId(),
    staleTime: 0,
  });
  const goalId = (observeGoalQuery.data || storedGoalId).trim();

  const tasksQuery = useQuery({
    queryKey: ['goal-tasks-for-evidence', goalId, auth?.kind],
    enabled: auth != null && Boolean(goalId),
    queryFn: ({signal}) =>
      listGoalTasks({auth: auth!, goalId, signal, limit: 50}),
  });

  const resolvedTaskId =
    taskId || tasksQuery.data?.[0]?.id || '';

  const listQuery = useQuery({
    queryKey: ['task-evidence', resolvedTaskId, auth?.kind],
    enabled: auth != null && Boolean(resolvedTaskId),
    queryFn: ({signal}) =>
      listTaskEvidence({
        auth: auth!,
        taskId: resolvedTaskId,
        signal,
      }),
    refetchInterval: 15_000,
  });

  const rows =
    listQuery.data != null
      ? evidenceRowsFromList(listQuery.data.items)
      : [];
  const timedOutCount = rows.filter((r) => r.timedOut).length;

  return (
    <section id="task-evidence" aria-labelledby="task-evidence-title">
      <h2 id="task-evidence-title">这一步有哪些验收材料</h2>
      <p className="lead-sm">逐项查看支撑验收结论的文件、日志和运行记录。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>GET <code>{'/api/v1/tasks/{task_id}/evidence'}</code>
        。EvidenceEnvelope 行 ≠ Audit PASS ≠ Goal DONE。</p></details>

      <div className="create-goal-grid">
        <label>
          选择任务步骤
          <select
            value={resolvedTaskId}
            onChange={(e) => setTaskId(e.target.value)}
            disabled={!goalId || tasksQuery.isPending}
          >
            {(tasksQuery.data ?? []).map((t) => (
              <option key={t.id} value={t.id}>
                {(t.contract?.objective || t.id).slice(0, 48)} · {t.status}
              </option>
            ))}
            {!tasksQuery.data?.length && resolvedTaskId ? (
              <option value={resolvedTaskId}>{resolvedTaskId}</option>
            ) : null}
          </select>
        </label>
      </div>

      <p className="gate-caption" role="status">
        {evidenceCaption({rowCount: rows.length, timedOutCount})}
      </p>

      {!goalId ? (
        <p className="observe-error">请先选择要查看的目标。</p>
      ) : !resolvedTaskId ? (
        <p className="muted">当前目标下没有任务步骤。空列表 ≠ Goal DONE。</p>
      ) : listQuery.isPending ? (
        <p role="status">正在读取 task evidence…</p>
      ) : listQuery.isError ? (
        <p role="alert" className="observe-error">
          {listQuery.error instanceof Error
            ? listQuery.error.message
            : '读取失败'}
        </p>
      ) : rows.length === 0 ? (
        <p className="muted">该任务步骤还没有验收材料。空列表 ≠ PASS。</p>
      ) : (
        <ul className="review-list">
          {rows.map((row) => (
            <li key={row.id}>
              <div className="review-head">
                <span
                  className={
                    row.timedOut || (row.exitCode != null && row.exitCode !== 0)
                      ? 'sev-blocker'
                      : row.exitCode === 0
                        ? 'sev-ok'
                        : undefined
                  }
                >
                  {row.timedOut
                    ? 'TIMED_OUT'
                    : row.exitCode == null
                      ? 'OBSERVED'
                      : `exit ${row.exitCode}`}
                </span>
                <span className="muted">
                  artifacts {row.artifactCount} · {row.producerIdentity}
                </span>
              </div>
              <p className="muted">
                {evidenceExitCaption(row.exitCode, row.timedOut)}
              </p>
              {row.commandPreview ? (
                <p>
                  <code>{row.commandPreview}</code>
                </p>
              ) : null}
              <p className="digest">
                evidence {row.id} · effect {row.effectId}
                {row.candidateManifestId
                  ? ` · candidate ${row.candidateManifestId}`
                  : ''}
              </p>
              <p className="digest">
                content {row.contentDigest.slice(0, 28)}…
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
