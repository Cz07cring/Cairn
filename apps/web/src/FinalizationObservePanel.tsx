import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {
  getAuthSession,
  getGoal,
  getGoalRelease,
  getGoalWallBudget,
  postEvidenceExport,
  postFinalizationRecovery,
} from '@ring/api-client';
import {
  evidenceExportEligibility,
  evidenceExportSuccessCaption,
  finalizationCaption,
  finalizationRecoveryEligibility,
  finalizationViewFromGoal,
  type EvidenceExportTrustMode,
  type FinalizationObserveView,
  type FinalizationRecoveryAction,
} from './finalizationObserveView.js';
import {
  observeConfigEmptyHint,
  resolveObserveConfig,
} from './observeConfig.js';
import {wallBudgetCaption} from './wallBudgetView.js';
import {canOperatorCreate, readStoredObserveGoalId} from './createGoalDraft.js';

function BarrierBody({view}: {view: FinalizationObserveView}) {
  return (
    <dl className="fin-facts">
      <div>
        <dt>Goal 状态</dt>
        <dd>{view.goalStatus}</dd>
      </div>
      {view.blockReason ? (
        <div>
          <dt>block_reason</dt>
          <dd>{view.blockReason}</dd>
        </div>
      ) : null}
      {view.barrier ? (
        <>
          <div>
            <dt>屏障状态</dt>
            <dd className={`barrier-${view.barrier.status.toLowerCase()}`}>
              {view.barrier.status}
            </dd>
          </div>
          <div>
            <dt>write_epoch</dt>
            <dd>{view.barrier.writeEpoch}</dd>
          </div>
          <div>
            <dt>候选</dt>
            <dd className="digest">{view.barrier.candidateManifestId ?? '—'}</dd>
          </div>
          <div>
            <dt>在途工程 / UNKNOWN</dt>
            <dd>
              {view.barrier.inFlightEngineering} / {view.barrier.unknownEffects}
            </dd>
          </div>
        </>
      ) : (
        <div>
          <dt>屏障</dt>
          <dd>无</dd>
        </div>
      )}
      {view.release ? (
        <>
          <div>
            <dt>Release</dt>
            <dd className="digest">{view.release.manifestId}</dd>
          </div>
          <div>
            <dt>validity</dt>
            <dd>{view.release.validityStatus}</dd>
          </div>
        </>
      ) : (
        <div>
          <dt>Release</dt>
          <dd>尚未签发</dd>
        </div>
      )}
    </dl>
  );
}

function RecoveryActions({
  view,
  goalId,
  auth,
}: {
  view: FinalizationObserveView;
  goalId: string;
  auth: import('@ring/api-client').BrowserClientAuth;
}) {
  const queryClient = useQueryClient();
  const eligibility = finalizationRecoveryEligibility(view);
  const mutation = useMutation({
    mutationFn: async (action: FinalizationRecoveryAction) => {
      if (!eligibility.requestBase) {
        throw new Error('当前不可提交 recovery');
      }
      return postFinalizationRecovery({
        goalId,
        auth,
        idempotencyKey: crypto.randomUUID(),
        body: {
          ...eligibility.requestBase,
          action,
          reason:
            action === 'REVERIFY'
              ? 'web:补证据重审（REVERIFY）；≠ DONE'
              : 'web:修复后重验（REWORK）；≠ DONE',
        },
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: ['goal-finalization', goalId],
      });
    },
  });

  if (view.goalStatus !== 'BLOCKED') {
    return null;
  }

  return (
    <div className="fin-recovery">
      <h3>处理未通过的交付检查</h3>
      <p className="muted">
        唯一恢复入口。202 仅表示命令已受理；不把按钮成功当成 Goal DONE。须
        operator 身份（session 或 Bearer）。
      </p>
      {!eligibility.eligible ? (
        <p className="observe-error">{eligibility.denyReason}</p>
      ) : (
        <div className="fin-recovery-actions">
          {eligibility.actions.map((action) => (
            <button
              key={action}
              type="button"
              className="fin-recovery-btn"
              disabled={mutation.isPending}
              onClick={() => mutation.mutate(action)}
            >
              {action === 'REVERIFY' ? '补充材料后重新验收' : '修改结果后重新验收'}
            </button>
          ))}
        </div>
      )}
      {mutation.isSuccess ? (
        <p className="sev-ok">
          命令已受理（{mutation.data.kind} / {mutation.data.status}）；请刷新核对
          Goal，勿自称 DONE。
        </p>
      ) : null}
      {mutation.isError ? (
        <p className="observe-error">
          {mutation.error instanceof Error
            ? mutation.error.message
            : '提交失败'}
        </p>
      ) : null}
    </div>
  );
}

function EvidenceExportActions({
  view,
  goalId,
  auth,
  sessionRoles,
}: {
  view: FinalizationObserveView;
  goalId: string;
  auth: import('@ring/api-client').BrowserClientAuth;
  sessionRoles: string[] | null | undefined;
}) {
  const eligibility = evidenceExportEligibility(view);
  const operatorOk = canOperatorCreate(sessionRoles);
  const allowAct =
    operatorOk || (auth.kind === 'bearer' && sessionRoles == null);
  const mutation = useMutation({
    mutationFn: async (trustMode: EvidenceExportTrustMode) => {
      if (!eligibility.releaseManifestId) {
        throw new Error('缺少 release_manifest_id');
      }
      if (trustMode === 'OFFLINE_VERIFIABLE' && !eligibility.allowOffline) {
        throw new Error(eligibility.offlineDenyReason ?? '不可离线导出');
      }
      return postEvidenceExport({
        goalId,
        auth,
        idempotencyKey: crypto.randomUUID(),
        body: {
          release_manifest_id: eligibility.releaseManifestId,
          trust_mode: trustMode,
        },
      });
    },
  });

  return (
    <div className="fin-recovery">
      <h3>证据导出</h3>
      <p className="muted">
        POST <code>/api/v1/goals/…/evidence-exports</code>：仅排队
        EXPORT_EVIDENCE；202 ≠ 导出字节就绪 ≠ Goal DONE。须 operator。
      </p>
      {!eligibility.eligible ? (
        <p className="observe-error">{eligibility.denyReason}</p>
      ) : !allowAct ? (
        <p className="muted">当前账号只能查看交付信息，不能发起导出。</p>
      ) : (
        <div className="fin-recovery-actions">
          <button
            type="button"
            className="fin-recovery-btn"
            disabled={mutation.isPending}
            onClick={() => mutation.mutate('INTERNAL_COPY')}
          >
            下载内部副本
          </button>
          <button
            type="button"
            className="fin-recovery-btn"
            disabled={mutation.isPending || !eligibility.allowOffline}
            title={eligibility.offlineDenyReason ?? undefined}
            onClick={() => mutation.mutate('OFFLINE_VERIFIABLE')}
          >
            下载可离线核验包
          </button>
        </div>
      )}
      {eligibility.eligible && eligibility.offlineDenyReason ? (
        <p className="muted">{eligibility.offlineDenyReason}</p>
      ) : null}
      {mutation.isSuccess ? (
        <p className="sev-ok">
          {evidenceExportSuccessCaption(
            (mutation.variables as EvidenceExportTrustMode) ?? 'INTERNAL_COPY',
          )}{' '}
          命令 {mutation.data.kind}/{mutation.data.status}
          {mutation.data.result &&
          typeof mutation.data.result === 'object' &&
          mutation.data.result !== null &&
          'activity_id' in mutation.data.result
            ? `；activity=${String((mutation.data.result as {activity_id: string}).activity_id).slice(0, 8)}…`
            : ''}
        </p>
      ) : null}
      {mutation.isError ? (
        <p className="observe-error">
          {mutation.error instanceof Error
            ? mutation.error.message
            : '导出提交失败'}
        </p>
      ) : null}
    </div>
  );
}

/**
 * 最终屏障面板：优先 OIDC session，其次 DEV_BEARER；须 VITE_RING_GOAL_ID。
 * BLOCKED+SEALED 可提交 REVERIFY/REWORK；≠ DONE。
 */
export function FinalizationObservePanel() {
  const sessionQuery = useQuery({
    queryKey: ['auth-session'],
    queryFn: ({signal}) => getAuthSession({signal}),
    retry: false,
  });
  const storedGoal = useQuery({
    queryKey: ['observe-goal-id'],
    queryFn: async () => readStoredObserveGoalId(),
  });
  const cfg = resolveObserveConfig({
    goalIdEnv: String(import.meta.env.VITE_RING_GOAL_ID ?? ''),
    bearerEnv: String(import.meta.env.VITE_RING_DEV_BEARER ?? ''),
    session: sessionQuery.data,
    storedGoalId: storedGoal.data,
  });
  const query = useQuery({
    queryKey: ['goal-finalization', cfg?.goalId, cfg?.source],
    enabled: cfg != null && !sessionQuery.isPending,
    queryFn: async ({signal}) => {
      const goal = await getGoal({
        goalId: cfg!.goalId,
        auth: cfg!.auth,
        signal,
      });
      const release = await getGoalRelease({
        goalId: cfg!.goalId,
        auth: cfg!.auth,
        signal,
      });
      let wallBudget = null;
      try {
        wallBudget = await getGoalWallBudget({
          goalId: cfg!.goalId,
          auth: cfg!.auth,
          signal,
        });
      } catch {
        // 墙钟端点失败不阻断屏障观察；单独提示
        wallBudget = null;
      }
      return {
        view: finalizationViewFromGoal(goal, release),
        wallBudget,
      };
    },
    refetchInterval: cfg ? 30_000 : false,
  });

  return (
    <section
      id="finalization"
      className="observe"
      aria-labelledby="finalization-title"
    >
      <h2 id="finalization-title">是否可以交付</h2>
      <p className="lead-sm">集中核对验收材料、未决操作和交付条件；只有全部满足后才可交付。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>观察 Kernel 屏障与交付包；符合门禁时可提交 finalization-recovery。
        DRAINING/SEALED/RELEASED 都不是前端可宣称的 Goal DONE；DONE 仅经
        VerificationProfile + 最终屏障裁决。</p></details>
      {cfg == null ? (
        <div className="observe-empty">
          <p>{observeConfigEmptyHint(null)}</p>
        </div>
      ) : sessionQuery.isPending || query.isPending ? (
        <p className="muted">读取 Goal / Release…</p>
      ) : query.isError ? (
        <p className="observe-error">
          {query.error instanceof Error ? query.error.message : '读取失败'}
        </p>
      ) : query.data != null ? (
        <>
          <p className="muted">{observeConfigEmptyHint(cfg)}</p>
          <p className="gate-caption">{finalizationCaption(query.data.view)}</p>
          {query.data.wallBudget != null ? (
            <p className="gate-caption" role="status">
              {wallBudgetCaption(query.data.wallBudget)}
            </p>
          ) : (
            <p className="muted">墙钟预算快照暂不可读（不阻断屏障观察）。</p>
          )}
          <BarrierBody view={query.data.view} />
          <RecoveryActions
            view={query.data.view}
            goalId={cfg.goalId}
            auth={cfg.auth}
          />
          <EvidenceExportActions
            view={query.data.view}
            goalId={cfg.goalId}
            auth={cfg.auth}
            sessionRoles={sessionQuery.data?.roles}
          />
        </>
      ) : null}
    </section>
  );
}
