import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {useMemo, useState} from 'react';
import {
  getAuthSession,
  listApprovals,
  listProjects,
  postApprovalDecision,
  postApprovalRevoke,
  type BrowserClientAuth,
} from '@ring/api-client';
import {
  approvalDecisionSuccessCaption,
  approvalRevokeSuccessCaption,
  approvalRowsFromList,
  approvalsCaption,
  canApproverAct,
  type ApprovalRow,
} from './approvalsView.js';

/**
 * 审批 inbox：列表 + APPROVE/DENY/撤销。
 * 裁决成功 ≠ Effect SUCCEEDED ≠ Goal DONE。
 */
export function ApprovalsObservePanel() {
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

  const approverOk = canApproverAct(sessionQuery.data?.roles);
  const allowAct =
    approverOk || (auth?.kind === 'bearer' && !sessionQuery.data);

  const [projectId, setProjectId] = useState('');
  const [statusFilter, setStatusFilter] = useState<'PENDING' | 'ALL'>('PENDING');
  const [reason, setReason] = useState('工作台人工裁决');
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  const projectsQuery = useQuery({
    queryKey: ['projects', auth?.kind],
    enabled: auth != null && !sessionQuery.isPending,
    queryFn: ({signal}) => listProjects({auth: auth!, signal}),
  });

  const resolvedProjectId =
    projectId ||
    projectsQuery.data?.[0]?.id ||
    String(import.meta.env.VITE_RING_PROJECT_ID ?? '').trim();

  const listQuery = useQuery({
    queryKey: [
      'approvals',
      resolvedProjectId,
      statusFilter,
      auth?.kind,
    ],
    enabled:
      auth != null && !sessionQuery.isPending && Boolean(resolvedProjectId),
    queryFn: ({signal}) =>
      listApprovals({
        auth: auth!,
        projectId: resolvedProjectId,
        status: statusFilter === 'ALL' ? undefined : 'PENDING',
        signal,
      }),
    refetchInterval: 15_000,
  });

  const rows: ApprovalRow[] =
    listQuery.data != null ? approvalRowsFromList(listQuery.data) : [];
  const pendingCount = rows.filter((r) => r.status === 'PENDING').length;

  const decideMutation = useMutation({
    mutationFn: async (input: {
      row: ApprovalRow;
      decision: 'APPROVE' | 'DENY';
    }) => {
      if (!auth) {
        throw new Error('未登录');
      }
      return postApprovalDecision({
        auth,
        approvalId: input.row.id,
        body: {
          expected_state_revision: input.row.stateRevision,
          decision: input.decision,
          subject: {
            type: input.row.subjectType as 'EFFECT' | 'MODEL_INVOCATION',
            id: input.row.subjectId,
          },
          payload_digest: input.row.payloadDigest,
          reason: reason.trim() || '工作台人工裁决',
        },
      });
    },
    onSuccess: (_data, vars) => {
      setActionMsg(approvalDecisionSuccessCaption(vars.decision));
      void queryClient.invalidateQueries({queryKey: ['approvals']});
    },
    onError: (err) => {
      setActionMsg(err instanceof Error ? err.message : '裁决失败');
    },
  });

  const revokeMutation = useMutation({
    mutationFn: async (row: ApprovalRow) => {
      if (!auth) {
        throw new Error('未登录');
      }
      return postApprovalRevoke({
        auth,
        approvalId: row.id,
        expectedStateRevision: row.stateRevision,
        reason: reason.trim() || '工作台人工撤销',
      });
    },
    onSuccess: () => {
      setActionMsg(approvalRevokeSuccessCaption());
      void queryClient.invalidateQueries({queryKey: ['approvals']});
    },
    onError: (err) => {
      setActionMsg(err instanceof Error ? err.message : '撤销失败');
    },
  });

  if (auth == null && !sessionQuery.isPending) {
    return (
      <section className="empty" id="approvals">
        <div className="symbol">⚠</div>
        <h2>待你批准的事项</h2>
        <p>请先登录，登录后即可处理待你批准的事项。</p>
      </section>
    );
  }

  return (
    <section className="create-goal observe" id="approvals">
      <h2>待你批准的事项</h2>
      <p className="lead-sm">集中处理需要你同意、拒绝或撤回决定的事项。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>GET <code>/api/v1/approvals</code>；APPROVE/DENY/撤销只改审批行。≠ Effect
        成功，≠ Goal DONE。</p></details>

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
          查看范围
          <select
            value={statusFilter}
            onChange={(e) =>
              setStatusFilter(e.target.value === 'ALL' ? 'ALL' : 'PENDING')
            }
          >
            <option value="PENDING">仅 PENDING</option>
            <option value="ALL">全部</option>
          </select>
        </label>
        <label>
          处理原因
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={2000}
          />
        </label>
      </div>

      <p className="gate-caption" role="status">
        {approvalsCaption({
          rowCount: rows.length,
          pendingCount,
          filterStatus: statusFilter === 'ALL' ? null : 'PENDING',
        })}
      </p>
      {!allowAct ? (
        <p className="muted">当前身份无 approver/admin，仅可只读列表。</p>
      ) : null}
      {actionMsg ? (
        <p className="muted" role="status">
          {actionMsg}
        </p>
      ) : null}

      {listQuery.isPending ? (
        <p role="status">正在读取审批…</p>
      ) : listQuery.isError ? (
        <p role="alert" className="observe-error">
          {listQuery.error instanceof Error
            ? listQuery.error.message
            : '读取失败'}
        </p>
      ) : rows.length === 0 ? (
        <p className="muted">当前筛选下无审批记录。空列表 ≠ 无需审批 ≠ DONE。</p>
      ) : (
        <ul className="review-list">
          {rows.map((row) => (
            <li key={row.id}>
              <div className="review-head">
                <span className={row.status === 'PENDING' ? 'sev-blocker' : 'sev-ok'}>
                  {row.status}
                </span>
                <span>
                  {row.subjectType}:{row.subjectId.slice(0, 8)}…
                </span>
                {row.goalId ? (
                  <span className="muted">goal {row.goalId.slice(0, 8)}…</span>
                ) : null}
              </div>
              <p className="digest">{row.payloadDigest}</p>
              {row.decisionReason ? (
                <p className="muted">{row.decisionReason}</p>
              ) : null}
              {allowAct && row.status === 'PENDING' ? (
                <div className="create-goal-actions">
                  <button
                    type="button"
                    disabled={decideMutation.isPending || revokeMutation.isPending}
                    onClick={() =>
                      decideMutation.mutate({row, decision: 'APPROVE'})
                    }
                  >
                    同意并继续
                  </button>
                  <button
                    type="button"
                    disabled={decideMutation.isPending || revokeMutation.isPending}
                    onClick={() =>
                      decideMutation.mutate({row, decision: 'DENY'})
                    }
                  >
                    拒绝并停止
                  </button>
                </div>
              ) : null}
              {allowAct &&
              (row.status === 'APPROVED' || row.status === 'DENIED') ? (
                <div className="create-goal-actions">
                  <button
                    type="button"
                    disabled={decideMutation.isPending || revokeMutation.isPending}
                    onClick={() => revokeMutation.mutate(row)}
                  >
                    撤回决定
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
