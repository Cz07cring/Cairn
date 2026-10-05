import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {useMemo, useState} from 'react';
import {
  getAuthSession,
  listGoals,
  listProjects,
  startGoal,
  type BrowserClientAuth,
} from '@ring/api-client';
import {canOperatorCreate} from './createGoalDraft.js';
import {
  goalOptionLabel,
  startGoalEligibility,
  startGoalSuccessCaption,
  toStartGoalCandidate,
} from './startGoalDraft.js';

/**
 * 对 DRAFT Goal 提交 START：202 命令受理 ≠ Harness 就绪 ≠ DONE。
 */
export function StartGoalDraftPanel() {
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
  const [goalId, setGoalId] = useState('');

  const projectsQuery = useQuery({
    queryKey: ['projects', auth?.kind],
    enabled: auth != null && !sessionQuery.isPending,
    queryFn: ({signal}) => listProjects({auth: auth!, signal}),
  });

  const resolvedProjectId =
    projectId ||
    projectsQuery.data?.[0]?.id ||
    String(import.meta.env.VITE_RING_PROJECT_ID ?? '').trim();

  const draftsQuery = useQuery({
    queryKey: ['goals-draft', resolvedProjectId, auth?.kind],
    enabled:
      auth != null &&
      !sessionQuery.isPending &&
      Boolean(resolvedProjectId),
    queryFn: ({signal}) =>
      listGoals({
        auth: auth!,
        projectId: resolvedProjectId,
        status: 'DRAFT',
        signal,
      }),
  });

  const candidates = (draftsQuery.data ?? []).map(toStartGoalCandidate);
  const selected =
    candidates.find((g) => g.id === goalId) ?? candidates[0] ?? null;

  const eligibility = startGoalEligibility(
    selected,
    allowAttempt && auth != null,
  );

  const mutation = useMutation({
    mutationFn: async () => {
      if (!auth || !selected || !eligibility.canStart) {
        throw new Error(eligibility.reason);
      }
      return startGoal({
        auth,
        goalId: selected.id,
        idempotencyKey: crypto.randomUUID(),
        body: {
          expected_state_revision: selected.stateRevision,
          reason: 'web-start-draft',
        },
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({queryKey: ['goals-draft']});
      await queryClient.invalidateQueries({queryKey: ['goals-observe']});
      await queryClient.invalidateQueries({queryKey: ['observe-goal-id']});
      await queryClient.invalidateQueries({queryKey: ['goal-audits']});
      await queryClient.invalidateQueries({queryKey: ['goal-finalization']});
      await queryClient.invalidateQueries({queryKey: ['commands-list']});
      setGoalId('');
    },
  });

  if (auth == null && !sessionQuery.isPending) {
    return (
      <section className="empty" id="start-goal">
        <div className="symbol">▶</div>
        <h2>启动一个目标</h2>
        <p>请先登录，登录后才能启动目标。</p>
      </section>
    );
  }

  return (
    <section className="create-goal observe" id="start-goal">
      <h2>启动一个目标</h2>
      <p className="lead-sm">确认后把目标草稿交给系统开始规划；受理成功不代表已经完成。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>POST <code>/api/v1/goals/&#123;id&#125;/start</code>：命令 202 / SUCCEEDED
        最多把 Goal 推到 PLANNING；不等于 Harness 闭环，更不等于 DONE。</p></details>

      {!allowAttempt && sessionQuery.data ? (
        <p className="observe-error">当前账号没有启动目标的权限。</p>
      ) : null}

      <div className="create-goal-grid">
        <label>
          项目
          <select
            value={resolvedProjectId}
            onChange={(e) => {
              setProjectId(e.target.value);
              setGoalId('');
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
          选择目标草稿
          <select
            value={selected?.id ?? ''}
            onChange={(e) => setGoalId(e.target.value)}
            disabled={candidates.length === 0}
          >
            {candidates.length === 0 ? (
              <option value="">暂无目标草稿（请先在上方创建）</option>
            ) : (
              candidates.map((g) => (
                <option key={g.id} value={g.id}>
                  {goalOptionLabel(g)}
                </option>
              ))
            )}
          </select>
        </label>
      </div>

      <p className="muted">{eligibility.reason}</p>

      {draftsQuery.isError ? (
        <p className="observe-error">
          {draftsQuery.error instanceof Error
            ? draftsQuery.error.message
            : '无法读取目标草稿'}
        </p>
      ) : null}

      <button
        type="button"
        className="fin-recovery-btn"
        disabled={!eligibility.canStart || mutation.isPending}
        onClick={() => mutation.mutate()}
      >
        {mutation.isPending ? '正在启动…' : '开始执行'}
      </button>

      {mutation.isSuccess ? (
        <p className="sev-ok">
          {startGoalSuccessCaption({
            commandStatus: mutation.data.status,
            finalStatus: mutation.data.result?.final_status,
          })}{' '}
          command=<code>{mutation.data.id}</code>
        </p>
      ) : null}
      {mutation.isError ? (
        <p className="observe-error">
          {mutation.error instanceof Error
            ? mutation.error.message
            : 'START 失败'}
        </p>
      ) : null}
    </section>
  );
}
