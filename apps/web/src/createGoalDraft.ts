/**
 * 创建 Goal DRAFT 表单投影与载荷装配；不自动 START；≠ DONE。
 */
import type {GoalCreate} from '@ring/api-client';

export const OBSERVE_GOAL_STORAGE_KEY = 'ring.observeGoalId';

export function readStoredObserveGoalId(): string {
  try {
    return String(localStorage.getItem(OBSERVE_GOAL_STORAGE_KEY) ?? '').trim();
  } catch {
    return '';
  }
}

export function writeStoredObserveGoalId(goalId: string): void {
  const id = goalId.trim();
  try {
    if (!id) {
      localStorage.removeItem(OBSERVE_GOAL_STORAGE_KEY);
      return;
    }
    localStorage.setItem(OBSERVE_GOAL_STORAGE_KEY, id);
  } catch {
    // 隐私模式等：忽略，观察仍可用 env
  }
}

export type CreateGoalDraftForm = {
  projectId: string;
  objective: string;
  criterionId: string;
  criterionDescription: string;
  verificationProfileId: string;
  policyId: string;
  modelProfileId: string;
  skillSetId: string;
  baseCommit: string;
  constraint: string;
};

export type CreateGoalDraftValidation = {
  ok: boolean;
  errors: string[];
};

const DEFAULT_BUDGET: GoalCreate['budget'] = {
  wall_clock_seconds: 3600,
  max_tokens: 100000,
  max_cost_usd: '0',
  max_tool_calls: 100,
  max_network_calls: 10,
  max_disk_bytes: 1048576,
  max_gpu_seconds: null,
};

const DEFAULT_RETRY: GoalCreate['retry_policy'] = {
  max_execution_rounds: 4,
  max_audit_attempts_per_candidate: 3,
  max_activity_retries: 3,
  max_plan_revisions: 10,
};

export function validateCreateGoalDraftForm(
  form: CreateGoalDraftForm,
): CreateGoalDraftValidation {
  const errors: string[] = [];
  if (!form.projectId.trim()) {
    errors.push('缺少 project_id');
  }
  if (!form.objective.trim()) {
    errors.push('缺少 objective');
  }
  if (!form.criterionId.trim()) {
    errors.push('缺少 criterion id');
  }
  if (!form.criterionDescription.trim()) {
    errors.push('缺少验收标准描述');
  }
  if (!form.verificationProfileId.trim()) {
    errors.push('缺少 VerificationProfile（须 GOAL 范围）');
  }
  if (!form.policyId.trim()) {
    errors.push('缺少 policy_id');
  }
  if (!form.modelProfileId.trim()) {
    errors.push('缺少 model_profile_id');
  }
  if (!form.skillSetId.trim()) {
    errors.push('缺少 skill_set_id');
  }
  const commit = form.baseCommit.trim();
  if (!/^[0-9a-f]{40}$/i.test(commit)) {
    errors.push('base_commit 须为 40 位 hex');
  }
  return {ok: errors.length === 0, errors};
}

export function buildGoalCreateBody(form: CreateGoalDraftForm): GoalCreate {
  const check = validateCreateGoalDraftForm(form);
  if (!check.ok) {
    throw new Error(check.errors.join('；'));
  }
  const constraint = form.constraint.trim() || '仅修改隔离工作区';
  return {
    project_id: form.projectId.trim(),
    objective: form.objective.trim(),
    success_criteria: [
      {
        id: form.criterionId.trim(),
        description: form.criterionDescription.trim(),
        required: true,
        verification_profile_id: form.verificationProfileId.trim(),
      },
    ],
    constraints: [constraint],
    budget: DEFAULT_BUDGET,
    retry_policy: DEFAULT_RETRY,
    policy_id: form.policyId.trim(),
    model_profile_id: form.modelProfileId.trim(),
    skill_set_id: form.skillSetId.trim(),
    base_commit: form.baseCommit.trim().toLowerCase(),
  };
}

export function createGoalDraftCaption(status: string | null): string {
  if (status === 'DRAFT') {
    return '已创建 Goal DRAFT：仅持久化合同，未 START；DRAFT ≠ RUNNING ≠ DONE。';
  }
  return '创建目标只写 DRAFT。START / 调度 / 验收仍走 Kernel；禁止把创建成功当成完成。';
}

export function canOperatorCreate(roles: string[] | null | undefined): boolean {
  return Array.isArray(roles) && roles.includes('operator');
}
