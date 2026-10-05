/**
 * Fake PLAN 宿主：零工具 Manager 闭环（claim → context-compile → 模型回合 → PlanCreate outcome）。
 * 不是官方 Cordis adapter；意外 tool call 必须 ROLE_TOOL_FORBIDDEN。
 */
import {createHash} from 'node:crypto';
import {authorizeToolProposal} from '../roles.js';

export type LeaseIdentity = {
  activity_id: string;
  attempt_id: string;
  fencing_epoch: string;
};

export type ActivityLeaseView = {
  lease: LeaseIdentity;
  activity: {
    id: string;
    kind: string;
    state_revision: number;
    binding: Record<string, unknown>;
    goal_id: string | null;
    /** EXECUTE 必有；PLAN 等可为 null */
    task_id: string | null;
    project_id: string;
    /** AUDIT 等目标类型；缺省表示调用方未透出 */
    target?: {type: string; id?: string | null};
    /** AUDIT(CANDIDATE) 验收分配；缺省表示未透出 */
    verification_assignments?: Array<Record<string, unknown>>;
  };
};

export type FakePlanHostPorts = {
  claimPlan: () => Promise<ActivityLeaseView | null>;
  /** 优先走 Kernel ContextCompiler；与 Harness adapter 对齐。 */
  compileContext: (input: {
    activityId: string;
    lease: LeaseIdentity;
    maxInputTokens?: number;
  }) => Promise<{id: string; content_digest: string; content: Record<string, unknown>}>;
  bindContext: (input: {
    activityId: string;
    lease: LeaseIdentity;
    bindingDigest: string;
    contextBundleId: string;
  }) => Promise<{context_digest: string}>;
  /** 登记一次模型调用并完成宿主侧回合（可 fixture，可不打真实模型）。 */
  completeModelTurn: (input: {
    lease: LeaseIdentity;
    contextDigest: string;
    toolsExposedToModel: readonly string[];
  }) => Promise<void>;
  buildPlan: (lease: ActivityLeaseView) => Record<string, unknown>;
  submitPlanOutcome: (input: {
    activityId: string;
    lease: LeaseIdentity;
    expectedStateRevision: number;
    plan: Record<string, unknown>;
  }) => Promise<void>;
};

export function bindingDigestOf(binding: Record<string, unknown>): string {
  // 与 Python json.dumps(..., sort_keys=True, separators=(",", ":")) 对齐
  const sorted = sortKeysDeep(binding);
  return (
    'sha256:' +
    createHash('sha256').update(JSON.stringify(sorted)).digest('hex')
  );
}

function sortKeysDeep(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sortKeysDeep);
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
        .map(([k, v]) => [k, sortKeysDeep(v)]),
    );
  }
  return value;
}

/** PLAN 工具集必须为空；任何提案都拒绝。 */
export function rejectPlanTools(toolsExposedToModel: readonly string[]): void {
  if (toolsExposedToModel.length === 0) {
    return;
  }
  authorizeToolProposal('PLAN', toolsExposedToModel[0]!, new Set(toolsExposedToModel));
}

/**
 * 跑一轮 Fake PLAN activation。
 * 返回 idle=无 READY；succeeded=已提交 PlanCreate（Goal 是否 RUNNING 由 Kernel 决定）。
 */
export async function runFakePlanActivation(
  ports: FakePlanHostPorts,
): Promise<'idle' | 'succeeded'> {
  const claimed = await ports.claimPlan();
  if (!claimed) {
    return 'idle';
  }
  if (claimed.activity.kind !== 'PLAN') {
    throw new Error('UNEXPECTED_ACTIVITY_KIND');
  }

  const compiled = await ports.compileContext({
    activityId: claimed.activity.id,
    lease: claimed.lease,
  });
  if (compiled.content.role !== 'PLANNER') {
    throw new Error('CONTEXT_ROLE_MISMATCH');
  }

  const bound = await ports.bindContext({
    activityId: claimed.activity.id,
    lease: claimed.lease,
    bindingDigest: bindingDigestOf(claimed.activity.binding),
    contextBundleId: compiled.id,
  });

  // 宿主回合：工具集恒空；completeModelTurn 内若暴露工具会再次校验。
  await ports.completeModelTurn({
    lease: claimed.lease,
    contextDigest: bound.context_digest,
    toolsExposedToModel: [],
  });

  const plan = ports.buildPlan(claimed);
  await ports.submitPlanOutcome({
    activityId: claimed.activity.id,
    lease: claimed.lease,
    expectedStateRevision: claimed.activity.state_revision,
    plan,
  });
  return 'succeeded';
}
