import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {useEffect, useMemo, useState} from 'react';
import {
  createGoal,
  getAuthSession,
  listModelProfiles,
  listPolicies,
  listProjects,
  listSkillSets,
  listVerificationProfiles,
  type BrowserClientAuth,
} from '@ring/api-client';
import {
  buildGoalCreateBody,
  canOperatorCreate,
  createGoalDraftCaption,
  validateCreateGoalDraftForm,
  writeStoredObserveGoalId,
  type CreateGoalDraftForm,
} from './createGoalDraft.js';

function configLabel(id: string, name: string | undefined): string {
  return name ? `${name} (${id.slice(0, 8)}…)` : id;
}

/**
 * 创建 Goal DRAFT：从项目配置装配合同并 POST /goals。
 * 成功仅 DRAFT；不自动 START；可写入观察 Goal id。
 */
export function CreateGoalDraftPanel() {
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
  // Bearer 开发令牌无法从 session 读角色；允许尝试（服务端再鉴权）
  const allowAttempt =
    operatorOk || (auth?.kind === 'bearer' && !sessionQuery.data);

  const [form, setForm] = useState<CreateGoalDraftForm>({
    projectId: '',
    objective: '',
    criterionId: 'C1',
    criterionDescription: 'Goal GLOBAL 验收通过',
    verificationProfileId: '',
    policyId: '',
    modelProfileId: '',
    skillSetId: '',
    baseCommit: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    constraint: '仅修改隔离工作区',
  });
  const [defaultsAppliedFor, setDefaultsAppliedFor] = useState<string>('');

  const projectsQuery = useQuery({
    queryKey: ['projects', auth?.kind],
    enabled: auth != null && !sessionQuery.isPending,
    queryFn: ({signal}) => listProjects({auth: auth!, signal}),
  });

  const projectId =
    form.projectId ||
    projectsQuery.data?.[0]?.id ||
    String(import.meta.env.VITE_RING_PROJECT_ID ?? '').trim();

  const configsQuery = useQuery({
    queryKey: ['goal-create-configs', projectId, auth?.kind],
    enabled: Boolean(projectId) && auth != null,
    queryFn: async ({signal}) => {
      const [policies, models, skillSets, profiles] = await Promise.all([
        listPolicies({auth: auth!, projectId, signal}),
        listModelProfiles({auth: auth!, projectId, signal}),
        listSkillSets({auth: auth!, projectId, signal}),
        listVerificationProfiles({auth: auth!, projectId, signal}),
      ]);
      const goalProfiles = profiles.filter(
        (p) => p.config.target_scope === 'GOAL',
      );
      return {policies, models, skillSets, goalProfiles};
    },
  });

  useEffect(() => {
    if (!configsQuery.data || !projectId) {
      return;
    }
    if (defaultsAppliedFor === projectId) {
      return;
    }
    setForm((prev) => ({
      ...prev,
      projectId: prev.projectId || projectId,
      policyId: prev.policyId || configsQuery.data.policies[0]?.id || '',
      modelProfileId:
        prev.modelProfileId || configsQuery.data.models[0]?.id || '',
      skillSetId: prev.skillSetId || configsQuery.data.skillSets[0]?.id || '',
      verificationProfileId:
        prev.verificationProfileId ||
        configsQuery.data.goalProfiles[0]?.id ||
        '',
    }));
    setDefaultsAppliedFor(projectId);
  }, [configsQuery.data, projectId, defaultsAppliedFor]);

  const validation = validateCreateGoalDraftForm({
    ...form,
    projectId: form.projectId || projectId,
  });

  const mutation = useMutation({
    mutationFn: async () => {
      if (!auth) {
        throw new Error('请先登录或配置 DEV_BEARER');
      }
      const body = buildGoalCreateBody({
        ...form,
        projectId: form.projectId || projectId,
      });
      return createGoal({
        auth,
        body,
        idempotencyKey: crypto.randomUUID(),
      });
    },
    onSuccess: async (goal) => {
      writeStoredObserveGoalId(goal.id);
      await queryClient.invalidateQueries({queryKey: ['observe-goal-id']});
      await queryClient.invalidateQueries({queryKey: ['goal-audits']});
      await queryClient.invalidateQueries({queryKey: ['goal-finalization']});
      await queryClient.invalidateQueries({queryKey: ['goals-observe']});
      await queryClient.invalidateQueries({queryKey: ['goals-draft']});
      await queryClient.invalidateQueries({queryKey: ['commands-list']});
    },
  });

  if (auth == null && !sessionQuery.isPending) {
    return (
      <section className="empty" id="create-goal">
        <div className="symbol">＋</div>
        <h2>新建一个目标</h2>
        <p>请先登录，登录后即可填写目标、规则和验收标准。</p>
      </section>
    );
  }

  return (
    <section className="create-goal observe" id="create-goal">
      <h2>新建一个目标</h2>
      <p className="lead-sm">先保存目标、规则和验收标准；保存后还不会自动开始执行。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>{createGoalDraftCaption(null)}</p></details>
      {!allowAttempt ? (
        <p className="observe-error">当前账号没有创建目标的权限。</p>
      ) : null}
      {projectsQuery.isError ? (
        <p className="observe-error">
          {projectsQuery.error instanceof Error
            ? projectsQuery.error.message
            : '读取项目失败'}
        </p>
      ) : null}
      {configsQuery.isError ? (
        <p className="observe-error">
          {configsQuery.error instanceof Error
            ? configsQuery.error.message
            : '读取配置失败'}
        </p>
      ) : null}
      {configsQuery.data && configsQuery.data.goalProfiles.length === 0 ? (
        <p className="observe-error">
          项目缺少 target_scope=GOAL 的 VerificationProfile，无法创建诚实合同。
        </p>
      ) : null}

      <div className="create-goal-grid">
        <label>
          项目
          <select
            value={form.projectId || projectId}
            onChange={(e) => {
              setDefaultsAppliedFor('');
              setForm((f) => ({
                ...f,
                projectId: e.target.value,
                policyId: '',
                modelProfileId: '',
                skillSetId: '',
                verificationProfileId: '',
              }));
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
          想要完成什么
          <input
            value={form.objective}
            onChange={(e) => setForm((f) => ({...f, objective: e.target.value}))}
            placeholder="一句话描述要完成的目标"
          />
        </label>
        <label>
          执行规则
          <select
            value={form.policyId}
            onChange={(e) => setForm((f) => ({...f, policyId: e.target.value}))}
          >
            {(configsQuery.data?.policies ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {configLabel(p.id, p.config.name)}
              </option>
            ))}
          </select>
        </label>
        <label>
          使用哪个模型
          <select
            value={form.modelProfileId}
            onChange={(e) =>
              setForm((f) => ({...f, modelProfileId: e.target.value}))
            }
          >
            {(configsQuery.data?.models ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {configLabel(p.id, p.config.name)}
              </option>
            ))}
          </select>
        </label>
        <label>
          可用能力
          <select
            value={form.skillSetId}
            onChange={(e) => setForm((f) => ({...f, skillSetId: e.target.value}))}
          >
            {(configsQuery.data?.skillSets ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {configLabel(p.id, p.config.name)}
              </option>
            ))}
          </select>
        </label>
        <label>
          如何验收目标
          <select
            value={form.verificationProfileId}
            onChange={(e) =>
              setForm((f) => ({...f, verificationProfileId: e.target.value}))
            }
          >
            {(configsQuery.data?.goalProfiles ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {configLabel(p.id, p.config.name)}
              </option>
            ))}
          </select>
        </label>
        <label>
          从哪个代码版本开始
          <input
            value={form.baseCommit}
            onChange={(e) => setForm((f) => ({...f, baseCommit: e.target.value}))}
          />
        </label>
        <label className="span-2">
          验收标准
          <input
            value={form.criterionDescription}
            onChange={(e) =>
              setForm((f) => ({...f, criterionDescription: e.target.value}))
            }
          />
        </label>
      </div>

      {!validation.ok ? (
        <p className="muted">待补齐：{validation.errors.join('；')}</p>
      ) : null}

      <button
        type="button"
        className="fin-recovery-btn"
        disabled={
          !allowAttempt ||
          !validation.ok ||
          mutation.isPending ||
          configsQuery.isPending ||
          (configsQuery.data?.goalProfiles.length ?? 0) === 0
        }
        onClick={() => mutation.mutate()}
      >
        {mutation.isPending ? '正在创建…' : '保存目标草稿'}
      </button>

      {mutation.isSuccess ? (
        <p className="sev-ok">
          {createGoalDraftCaption(mutation.data.status)} id=
          <code>{mutation.data.id}</code>
          （已写入观察 Goal，可刷新下方面板）
        </p>
      ) : null}
      {mutation.isError ? (
        <p className="observe-error">
          {mutation.error instanceof Error
            ? mutation.error.message
            : '创建失败'}
        </p>
      ) : null}
    </section>
  );
}
