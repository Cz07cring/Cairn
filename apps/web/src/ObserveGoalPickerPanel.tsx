import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {useEffect, useMemo, useState} from 'react';
import {
  getAuthSession,
  getGoal,
  listGoals,
  listOrchestrationAbandonments,
  listProjects,
  releaseOrchestrationAbandonment,
  type BrowserClientAuth,
} from '@ring/api-client';
import {
  canOperatorCreate,
  readStoredObserveGoalId,
  writeStoredObserveGoalId,
} from './createGoalDraft.js';
import {
  abandonmentCaption,
  abandonmentReleaseEligibility,
  observeGoalOptionLabel,
  preferredObserveGoalId,
  toObserveGoalOption,
} from './observeGoalPicker.js';

/**
 * 观察 Goal 选择器 + 编排放弃只读/解除（Issue #24）。
 * 选择写入 localStorage；解除封锁 ≠ DONE。
 */
export function ObserveGoalPickerPanel() {
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

  const operatorOk = canOperatorCreate(sessionQuery.data?.roles);
  const allowAttempt =
    operatorOk || (auth?.kind === 'bearer' && !sessionQuery.data);

  const [projectId, setProjectId] = useState('');
  const [selectedId, setSelectedId] = useState(() => readStoredObserveGoalId());

  const projectsQuery = useQuery({
    queryKey: ['projects', auth?.kind],
    enabled: auth != null && !sessionQuery.isPending,
    queryFn: ({signal}) => listProjects({auth: auth!, signal}),
  });

  const resolvedProjectId =
    projectId ||
    projectsQuery.data?.[0]?.id ||
    String(import.meta.env.VITE_RING_PROJECT_ID ?? '').trim();

  const goalsQuery = useQuery({
    queryKey: ['goals-observe', resolvedProjectId, auth?.kind],
    enabled:
      auth != null && !sessionQuery.isPending && Boolean(resolvedProjectId),
    queryFn: ({signal}) =>
      listGoals({
        auth: auth!,
        projectId: resolvedProjectId,
        signal,
      }),
  });

  const options = (goalsQuery.data ?? []).map(toObserveGoalOption);
  const preferredId = preferredObserveGoalId(options, selectedId || readStoredObserveGoalId());
  const selected =
    options.find((g) => g.id === preferredId) ??
    null;

  useEffect(() => {
    if (!preferredId || selectedId === preferredId) {
      return;
    }
    setSelectedId(preferredId);
    writeStoredObserveGoalId(preferredId);
    void queryClient.invalidateQueries({queryKey: ['observe-goal-id']});
  }, [preferredId, queryClient, selectedId]);

  const detailQuery = useQuery({
    queryKey: ['observe-goal-detail', selected?.id, auth?.kind],
    enabled: auth != null && Boolean(selected?.id),
    queryFn: ({signal}) =>
      getGoal({auth: auth!, goalId: selected!.id, signal}),
  });

  const detailOption = detailQuery.data
    ? toObserveGoalOption(detailQuery.data)
    : selected;

  const abandonQuery = useQuery({
    queryKey: ['orchestration-abandonments', selected?.id, auth?.kind],
    enabled: auth != null && Boolean(selected?.id),
    queryFn: ({signal}) =>
      listOrchestrationAbandonments({
        auth: auth!,
        goalId: selected!.id,
        signal,
      }),
  });

  const abandonments = abandonQuery.data ?? [];
  const eligibility = abandonmentReleaseEligibility(
    detailOption,
    allowAttempt && auth != null,
    abandonments.length,
  );

  const selectGoal = (id: string) => {
    setSelectedId(id);
    writeStoredObserveGoalId(id);
    void queryClient.invalidateQueries({queryKey: ['observe-goal-id']});
    void queryClient.invalidateQueries({queryKey: ['goal-audits']});
    void queryClient.invalidateQueries({queryKey: ['goal-finalization']});
  };

  const releaseMutation = useMutation({
    mutationFn: async () => {
      if (!auth || !detailOption || !eligibility.canRelease) {
        throw new Error(eligibility.reason);
      }
      return releaseOrchestrationAbandonment({
        auth,
        goalId: detailOption.id,
        body: {
          expected_state_revision: detailOption.stateRevision,
          reason: 'web-release-orchestration-abandonment',
        },
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: ['goals-observe'],
      });
      await queryClient.invalidateQueries({
        queryKey: ['observe-goal-detail'],
      });
      await queryClient.invalidateQueries({
        queryKey: ['orchestration-abandonments'],
      });
      await queryClient.invalidateQueries({queryKey: ['goal-finalization']});
    },
  });

  if (auth == null && !sessionQuery.isPending) {
    return (
      <section className="empty" id="observe-goal-picker">
        <div className="symbol">◎</div>
        <h2>选择要查看的目标</h2>
        <p>请先登录，登录后即可选择要查看的目标。</p>
      </section>
    );
  }

  return (
    <section className="create-goal observe" id="observe-goal-picker">
      <h2>选择要查看的目标</h2>
      <p className="lead-sm">先选择一个目标，后续页面都会围绕它显示信息。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>选择项目内 Goal 写入观察 id；列出编排放弃事实并可尝试人工解除封锁。
        选择/解除 ≠ PASS ≠ DONE。</p></details>

      <div className="create-goal-grid">
        <label>
          项目
          <select
            value={resolvedProjectId}
            onChange={(e) => {
              setProjectId(e.target.value);
              setSelectedId('');
            }}
          >
            {(projectsQuery.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label className="span-2">
          要查看的目标
          <select
            value={selected?.id ?? ''}
            onChange={(e) => selectGoal(e.target.value)}
            disabled={options.length === 0}
          >
            {options.length === 0 ? (
              <option value="">暂无 Goal</option>
            ) : (
              options.map((g) => (
                <option key={g.id} value={g.id}>
                  {observeGoalOptionLabel(g)}
                </option>
              ))
            )}
          </select>
        </label>
      </div>

      {detailOption ? (
        <p className="muted">
          当前观察：<code>{detailOption.id}</code> · {detailOption.status}
          {detailOption.blockReason
            ? ` · ${detailOption.blockReason}`
            : ''}
        </p>
      ) : (
        <p className="muted">尚未选择要查看的目标（创建 DRAFT 后会出现在列表）。</p>
      )}

      <p className="gate-caption">
        {abandonmentCaption({
          goalStatus: detailOption?.status ?? '—',
          count: abandonments.length,
          marksGoalDoneAny: abandonments.some((a) => a.marks_goal_done),
        })}
      </p>

      {abandonments.length > 0 ? (
        <ul className="review-list">
          {abandonments.map((a) => (
            <li key={a.id}>
              <div className="review-head">
                <span>
                  gen={a.generation} · {a.reason}
                </span>
                <span className="muted">{a.created_at ?? ''}</span>
              </div>
              <p className="digest">
                abandonment=<code>{a.id}</code>
                {a.prior_run_id ? ` · prior_run=${a.prior_run_id}` : ''}
                {a.marks_goal_done ? ' · marks_goal_done=true（异常）' : ''}
              </p>
            </li>
          ))}
        </ul>
      ) : null}

      <p className="muted">{eligibility.reason}</p>

      <button
        type="button"
        className="fin-recovery-btn"
        disabled={!eligibility.canRelease || releaseMutation.isPending}
        onClick={() => releaseMutation.mutate()}
      >
        {releaseMutation.isPending ? '正在恢复…' : '允许系统重新接手'}
      </button>

      {releaseMutation.isSuccess ? (
        <p className="sev-ok">
          已解除封锁 → {releaseMutation.data.status}（previous 恢复路径）；≠
          DONE。id=<code>{releaseMutation.data.id}</code>
        </p>
      ) : null}
      {releaseMutation.isError ? (
        <p className="observe-error">
          {releaseMutation.error instanceof Error
            ? releaseMutation.error.message
            : '解除失败'}
        </p>
      ) : null}
    </section>
  );
}
