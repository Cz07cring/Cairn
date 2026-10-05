/**
 * Goal / Release / finalization-recovery：Bearer 或 OIDC session。
 * 不把缺屏障伪装成 DONE；recovery 202 ≠ DONE。
 */
import type {components} from './generated';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type GoalResource = components['schemas']['GoalResource'];
export type BarrierResource = components['schemas']['BarrierResource'];
export type ReleaseView = components['schemas']['ReleaseView'];
export type FinalizationRecoveryRequest =
  components['schemas']['FinalizationRecoveryRequest'];
export type CommandOperation = components['schemas']['CommandOperation'];

async function readEnvelopeData<T>(
  response: Response,
  emptyMessage: string,
): Promise<T> {
  if (response.status === 401 || response.status === 403) {
    throw new Error('无权读取该 Goal');
  }
  if (response.status === 404) {
    throw new Error(emptyMessage);
  }
  if (!response.ok) {
    throw new Error(`读取失败（HTTP ${response.status}）`);
  }
  const body: unknown = await response.json();
  if (
    typeof body !== 'object' ||
    body === null ||
    !('data' in body) ||
    (body as {data: unknown}).data == null
  ) {
    throw new Error('响应无法识别');
  }
  return (body as {data: T}).data;
}

/** GET /api/v1/goals/{goal_id} */
export async function getGoal(input: {
  goalId: string;
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
}): Promise<GoalResource> {
  const goalId = input.goalId.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch(`/api/v1/goals/${encodeURIComponent(goalId)}`, {
    signal: input.signal,
    cache: 'no-store',
    ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
  });
  return readEnvelopeData<GoalResource>(response, 'Goal 不存在或不可见');
}

/**
 * GET /api/v1/goals/{goal_id}/release
 * 无 Release 时接口可能 404 —— 返回 null（诚实：未交付 ≠ 失败）。
 */
export async function getGoalRelease(input: {
  goalId: string;
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
}): Promise<ReleaseView | null> {
  const goalId = input.goalId.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/release`,
    {
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
    },
  );
  if (response.status === 404) {
    return null;
  }
  return readEnvelopeData<ReleaseView>(response, 'Release 不可见');
}

export type GoalWallBudgetSnapshot =
  components['schemas']['GoalWallBudgetSnapshot'];

/** GET /api/v1/goals/{goal_id}/wall-budget；marks_goal_done 恒应 false。 */
export async function getGoalWallBudget(input: {
  goalId: string;
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
}): Promise<GoalWallBudgetSnapshot> {
  const goalId = input.goalId.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/wall-budget`,
    {
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
    },
  );
  return readEnvelopeData<GoalWallBudgetSnapshot>(
    response,
    '墙钟预算不可见',
  );
}

export type GoalReviewBudgetSnapshot =
  components['schemas']['GoalReviewBudgetSnapshot'];

/** GET /api/v1/goals/{goal_id}/goal-review-budget；marks_goal_done 恒应 false。 */
export async function getGoalReviewBudget(input: {
  goalId: string;
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
}): Promise<GoalReviewBudgetSnapshot> {
  const goalId = input.goalId.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/goal-review-budget`,
    {
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
    },
  );
  return readEnvelopeData<GoalReviewBudgetSnapshot>(
    response,
    '复盘预算不可见',
  );
}

/**
 * POST /api/v1/goals/{goal_id}/finalization-recovery
 * 须 operator；session 模式须 CSRF；202 只表示命令已受理，≠ Goal DONE。
 */
export async function postFinalizationRecovery(input: {
  goalId: string;
  authorization?: string;
  auth?: BrowserClientAuth;
  idempotencyKey: string;
  body: FinalizationRecoveryRequest;
  signal?: AbortSignal;
}): Promise<CommandOperation> {
  const goalId = input.goalId.trim();
  const key = input.idempotencyKey.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  if (!key) {
    throw new Error('缺少 Idempotency-Key');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/finalization-recovery`,
    {
      method: 'POST',
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(
        auth,
        {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          'Idempotency-Key': key,
        },
        {mutating: true},
      ),
      body: JSON.stringify(input.body),
    },
  );
  if (response.status === 401 || response.status === 403) {
    throw new Error('无权提交 finalization-recovery（须 operator）');
  }
  if (response.status === 409) {
    throw new Error('恢复被拒绝：状态/版本冲突或当前不可恢复');
  }
  if (response.status === 422) {
    throw new Error('恢复请求无效（校验失败）');
  }
  if (response.status !== 202 && !response.ok) {
    throw new Error(`恢复提交失败（HTTP ${response.status}）`);
  }
  return readEnvelopeData<CommandOperation>(response, '恢复命令不可见');
}

export type EvidenceExportRequest = components['schemas']['EvidenceExportRequest'];

/**
 * POST /api/v1/goals/{goal_id}/evidence-exports
 * 须 operator；202 仅排队 EXPORT_EVIDENCE，不捏造 artifact；≠ Goal DONE。
 */
export async function postEvidenceExport(input: {
  goalId: string;
  authorization?: string;
  auth?: BrowserClientAuth;
  idempotencyKey: string;
  body: EvidenceExportRequest;
  signal?: AbortSignal;
}): Promise<CommandOperation> {
  const goalId = input.goalId.trim();
  const key = input.idempotencyKey.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  if (!key) {
    throw new Error('缺少 Idempotency-Key');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/evidence-exports`,
    {
      method: 'POST',
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(
        auth,
        {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          'Idempotency-Key': key,
        },
        {mutating: true},
      ),
      body: JSON.stringify(input.body),
    },
  );
  if (response.status === 401 || response.status === 403) {
    throw new Error('无权提交证据导出（须 operator）');
  }
  if (response.status === 503) {
    throw new Error(
      '签名/信任根未配置，无法 OFFLINE_VERIFIABLE（失败关闭，不降级）',
    );
  }
  if (response.status === 409) {
    throw new Error('导出被拒绝：信任封锁或幂等冲突');
  }
  if (response.status === 422) {
    throw new Error('导出请求无效（状态/清单/失效门禁）');
  }
  if (response.status !== 202 && !response.ok) {
    throw new Error(`导出提交失败（HTTP ${response.status}）`);
  }
  return readEnvelopeData<CommandOperation>(response, '导出命令不可见');
}
