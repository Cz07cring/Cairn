import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {useMemo, useState} from 'react';
import {
  getAuthSession,
  getGoal,
  listEffects,
  listProjects,
  postEffectReconcile,
  type BrowserClientAuth,
} from '@ring/api-client';
import {readStoredObserveGoalId} from './createGoalDraft.js';
import {
  canApproverReconcile,
  effectRowsFromList,
  effectStatusCaption,
  effectsCaption,
  reconcileSuccessCaption,
  type EffectRow,
} from './effectsObserveView.js';

/**
 * Effect 观察 + UNKNOWN 对账提交。
 * UNKNOWN ≠ 可重试；对账 202 ≠ Effect 已改写 ≠ Goal DONE。
 */
export function EffectsObservePanel() {
  const queryClient = useQueryClient();
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

  const reconcileOk =
    canApproverReconcile(sessionQuery.data?.roles) ||
    (auth?.kind === 'bearer' && !sessionQuery.data);

  const [projectId, setProjectId] = useState('');
  const [statusFilter, setStatusFilter] = useState<'UNKNOWN' | 'ALL'>(
    'UNKNOWN',
  );
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [externalRef, setExternalRef] = useState('web-reconcile');
  const [reason, setReason] = useState('工作台人工核对既有效果');
  const [observed, setObserved] = useState<'SUCCEEDED' | 'FAILED'>('SUCCEEDED');
  const [evidenceId, setEvidenceId] = useState('');

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
    queryKey: ['effects-goal', goalId, auth?.kind],
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
      'effects',
      resolvedProjectId,
      goalId,
      statusFilter,
      auth?.kind,
    ],
    enabled:
      auth != null &&
      !sessionQuery.isPending &&
      Boolean(resolvedProjectId) &&
      Boolean(goalId),
    queryFn: ({signal}) =>
      listEffects({
        auth: auth!,
        projectId: resolvedProjectId,
        goalId,
        status: statusFilter === 'ALL' ? undefined : 'UNKNOWN',
        signal,
      }),
    refetchInterval: 15_000,
  });

  const rows: EffectRow[] =
    listQuery.data != null ? effectRowsFromList(listQuery.data) : [];
  const unknownCount = rows.filter((r) => r.status === 'UNKNOWN').length;

  const reconcileMutation = useMutation({
    mutationFn: async (row: EffectRow) => {
      if (!auth) {
        throw new Error('未登录');
      }
      const evidence = evidenceId.trim() || row.evidenceIds[0] || '';
      if (!evidence) {
        throw new Error('须提供 evidence_ids（粘贴 EvidenceEnvelope id）');
      }
      const ref = externalRef.trim();
      if (!ref) {
        throw new Error('缺少 external_ref');
      }
      return postEffectReconcile({
        auth,
        effectId: row.id,
        idempotencyKey: crypto.randomUUID(),
        body: {
          expected_state_revision: row.stateRevision,
          observed_result: observed,
          external_ref: ref,
          evidence_ids: [evidence],
          reason: reason.trim() || '工作台人工核对',
        },
      });
    },
    onSuccess: () => {
      setActionMsg(reconcileSuccessCaption());
      void queryClient.invalidateQueries({queryKey: ['effects']});
    },
    onError: (err) => {
      setActionMsg(err instanceof Error ? err.message : '对账失败');
    },
  });

  if (auth == null && !sessionQuery.isPending) {
    return (
      <section className="empty" id="effects">
        <div className="symbol">⚠</div>
        <h2>外部操作与结果</h2>
        <p>请先登录，登录后即可查看外部操作记录。</p>
      </section>
    );
  }

  return (
    <section className="create-goal observe" id="effects">
      <h2>外部操作与结果</h2>
      <p className="lead-sm">查看工具对外部世界做了什么，并核对结果不确定的操作。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>GET <code>/api/v1/effects</code>；UNKNOWN 只允许对账提交，禁止「再执行一次」。
        对账 202 ≠ Effect 已改写 ≠ Goal DONE。须先选择观察 Goal。</p></details>

      {!goalId ? (
        <p className="observe-error">
          尚未选择要查看的目标（见上方观察选择器）。
        </p>
      ) : null}

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
          状态
          <select
            value={statusFilter}
            onChange={(e) =>
              setStatusFilter(e.target.value === 'ALL' ? 'ALL' : 'UNKNOWN')
            }
          >
            <option value="UNKNOWN">仅 UNKNOWN</option>
            <option value="ALL">全部</option>
          </select>
        </label>
        <label>
          观察结果
          <select
            value={observed}
            onChange={(e) =>
              setObserved(e.target.value === 'FAILED' ? 'FAILED' : 'SUCCEEDED')
            }
          >
            <option value="SUCCEEDED">SUCCEEDED</option>
            <option value="FAILED">FAILED</option>
          </select>
        </label>
        <label>
          外部系统凭据
          <input
            value={externalRef}
            onChange={(e) => setExternalRef(e.target.value)}
            maxLength={2000}
          />
        </label>
        <label>
          关联证据
          <input
            value={evidenceId}
            onChange={(e) => setEvidenceId(e.target.value)}
            placeholder="EvidenceEnvelope UUID（可空则用 effect 已有）"
            maxLength={64}
          />
        </label>
        <label>
          为什么要核对
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={2000}
          />
        </label>
      </div>

      <p className="gate-caption" role="status">
        {effectsCaption({
          rowCount: rows.length,
          unknownCount,
          filterStatus: statusFilter === 'ALL' ? null : 'UNKNOWN',
        })}
      </p>
      {!reconcileOk ? (
        <p className="muted">当前身份无 approver/admin，仅可只读列表。</p>
      ) : null}
      {actionMsg ? (
        <p className="muted" role="status">
          {actionMsg}
        </p>
      ) : null}

      {!goalId ? null : listQuery.isPending ? (
        <p role="status">正在读取 effects…</p>
      ) : listQuery.isError ? (
        <p role="alert" className="observe-error">
          {listQuery.error instanceof Error
            ? listQuery.error.message
            : '读取失败'}
        </p>
      ) : rows.length === 0 ? (
        <p className="muted">
          当前筛选下无 effect。空列表 ≠ 无副作用风险 ≠ DONE。
        </p>
      ) : (
        <ul className="review-list">
          {rows.map((row) => (
            <li key={row.id}>
              <div className="review-head">
                <span
                  className={
                    row.status === 'UNKNOWN' ? 'sev-blocker' : 'sev-ok'
                  }
                >
                  {row.status}
                </span>
                <span>{row.toolRef}</span>
                <span className="muted">{row.scope}</span>
              </div>
              <p className="muted">{effectStatusCaption(row.status)}</p>
              <p className="digest">
                effect {row.id} · step {row.logicalStepId}
              </p>
              {reconcileOk && row.status === 'UNKNOWN' ? (
                <div className="create-goal-actions">
                  <button
                    type="button"
                    disabled={reconcileMutation.isPending}
                    onClick={() => reconcileMutation.mutate(row)}
                  >
                    核对真实结果
                  </button>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
