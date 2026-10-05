import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {useMemo, useState} from 'react';
import {
  cancelGoal,
  getAuthSession,
  getGoal,
  listCommands,
  listProjects,
  pauseGoal,
  resumeGoal,
  type BrowserClientAuth,
} from '@ring/api-client';
import {
  canOperatorCreate,
  readStoredObserveGoalId,
} from './createGoalDraft.js';
import {
  commandRowLabel,
  commandsCaption,
  controlSuccessCaption,
  goalControlEligibility,
  type GoalControlAction,
} from './commandsObserveView.js';

/**
 * 命令进度观察 + pause/resume/cancel。
 * 命令 202/SUCCEEDED / PAUSING ≠ 终态 ≠ DONE。
 */
export function CommandsObservePanel() {
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

  const resolvedProjectId =
    projectId ||
    projectsQuery.data?.[0]?.id ||
    String(import.meta.env.VITE_RING_PROJECT_ID ?? '').trim();

  const goalQuery = useQuery({
    queryKey: ['commands-goal', goalId, auth?.kind],
    enabled: auth != null && Boolean(goalId),
    queryFn: ({signal}) => getGoal({auth: auth!, goalId, signal}),
    refetchInterval: 15_000,
  });

  const commandsQuery = useQuery({
    queryKey: [
      'commands-list',
      resolvedProjectId,
      goalId,
      auth?.kind,
    ],
    enabled:
      auth != null &&
      !sessionQuery.isPending &&
      (Boolean(goalId) || Boolean(resolvedProjectId)),
    queryFn: ({signal}) =>
      listCommands({
        auth: auth!,
        projectId: resolvedProjectId || undefined,
        goalId: goalId || undefined,
        signal,
      }),
    refetchInterval: 10_000,
  });

  const status = goalQuery.data?.status ?? null;
  const stateRevision = goalQuery.data?.state_revision;
  const eligibility = goalControlEligibility(
    status,
    allowAttempt && auth != null,
  );

  const controlMutation = useMutation({
    mutationFn: async (action: GoalControlAction) => {
      if (!auth || !goalId || stateRevision == null) {
        throw new Error('缺少 Goal 或 state_revision');
      }
      const body = {
        expected_state_revision: stateRevision,
        reason: `web-${action}`,
      };
      const key = crypto.randomUUID();
      if (action === 'pause') {
        if (!eligibility.canPause) {
          throw new Error(eligibility.reason);
        }
        return {action, op: await pauseGoal({auth, goalId, body, idempotencyKey: key})};
      }
      if (action === 'resume') {
        if (!eligibility.canResume) {
          throw new Error(eligibility.reason);
        }
        return {action, op: await resumeGoal({auth, goalId, body, idempotencyKey: key})};
      }
      if (!eligibility.canCancel) {
        throw new Error(eligibility.reason);
      }
      return {action, op: await cancelGoal({auth, goalId, body, idempotencyKey: key})};
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({queryKey: ['commands-list']});
      await queryClient.invalidateQueries({queryKey: ['commands-goal']});
      await queryClient.invalidateQueries({queryKey: ['goals-observe']});
      await queryClient.invalidateQueries({queryKey: ['goal-finalization']});
      await queryClient.invalidateQueries({queryKey: ['observe-goal-detail']});
    },
  });

  const commands = commandsQuery.data ?? [];

  if (auth == null && !sessionQuery.isPending) {
    return (
      <section className="empty" id="commands">
        <div className="symbol">⌘</div>
        <h2>运行控制与处理进度</h2>
        <p>请先登录，登录后即可查看和操作。</p>
      </section>
    );
  }

  return (
    <section className="create-goal observe" id="commands">
      <h2>运行控制与处理进度</h2>
      <p className="lead-sm">安全暂停、继续或请求停止目标，并查看系统是否真正处理完成。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>列出 <code>/api/v1/commands</code>；可对观察 Goal 提交 pause / resume /
        cancel。命令受理 ≠ 终态确认 ≠ DONE。</p></details>

      <div className="create-goal-grid">
        <label>
          选择项目
          <select
            value={resolvedProjectId}
            onChange={(e) => setProjectId(e.target.value)}
          >
            {(projectsQuery.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label className="span-2">
          当前目标
          <input
            readOnly
            value={
              goalId
                ? `${status ?? '?'} · ${goalId}`
                : '（请先在上方选择/创建 Goal）'
            }
          />
        </label>
      </div>

      <p className="gate-caption">
        {commandsCaption({
          count: commands.length,
          anySucceeded: commands.some((c) => c.status === 'SUCCEEDED'),
        })}
      </p>
      <p className="muted">{eligibility.reason}</p>

      <div className="fin-recovery-actions">
        <button
          type="button"
          className="fin-recovery-btn"
          disabled={!eligibility.canPause || controlMutation.isPending}
          onClick={() => controlMutation.mutate('pause')}
        >
          安全暂停
        </button>
        <button
          type="button"
          className="fin-recovery-btn"
          disabled={!eligibility.canResume || controlMutation.isPending}
          onClick={() => controlMutation.mutate('resume')}
        >
          继续运行
        </button>
        <button
          type="button"
          className="fin-recovery-btn"
          disabled={!eligibility.canCancel || controlMutation.isPending}
          onClick={() => controlMutation.mutate('cancel')}
        >
          请求停止
        </button>
      </div>

      {controlMutation.isSuccess ? (
        <p className="sev-ok">
          {controlSuccessCaption({
            action: controlMutation.data.action,
            commandStatus: controlMutation.data.op.status,
            finalStatus: controlMutation.data.op.result?.final_status,
          })}{' '}
          <code>{controlMutation.data.op.id}</code>
        </p>
      ) : null}
      {controlMutation.isError ? (
        <p className="observe-error">
          {controlMutation.error instanceof Error
            ? controlMutation.error.message
            : '控制命令失败'}
        </p>
      ) : null}

      {commandsQuery.isError ? (
        <p className="observe-error">
          {commandsQuery.error instanceof Error
            ? commandsQuery.error.message
            : '命令列表不可读'}
        </p>
      ) : null}

      {commands.length > 0 ? (
        <ul className="review-list">
          {commands.map((c) => (
            <li key={c.id}>
              <div className="review-head">
                <span>{commandRowLabel(c)}</span>
                <span className="muted">{c.updated_at ?? c.created_at ?? ''}</span>
              </div>
              <p className="digest">
                {c.goal_id ? (
                  <>
                    goal=<code>{c.goal_id}</code> ·{' '}
                  </>
                ) : null}
                digest=<code>{c.request_digest.slice(0, 18)}…</code>
                {c.error?.message ? ` · err=${c.error.message}` : ''}
              </p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">暂无命令行。</p>
      )}
    </section>
  );
}
