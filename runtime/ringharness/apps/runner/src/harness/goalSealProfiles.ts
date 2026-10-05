/**
 * 从 Goal + Task 合同解析 seal 用 VerificationProfile（≠ 测试粘贴 UUID）。
 * EXECUTE：优先 Task acceptance（MECHANICAL）；Goal 只提供 objective 文案。
 * acceptance 描述进入提示，帮助模型对齐验收，禁止粘贴修复正文。
 */
type Envelope<T> = {data: T};

export type GoalSealActivation = {
  goalId: string;
  taskId: string;
  objective: string;
  /**
   * Task acceptance 上的 verification_profile_id（去重保序）。
   * 故意不用 Goal success_criteria（常为 GLOBAL）。
   */
  sealVerificationProfileIds: string[];
  /** Task acceptance.description（去空、保序；不含 UUID） */
  acceptanceDescriptions: string[];
  /** 仅用合同客观描述拼装的用户提示；不含 profile UUID / 工具顺序 / 修复正文 */
  userPromptFromGoal: string;
};

function authHeaders(authorization: string): {Authorization: string} {
  const t = authorization.trim();
  return {
    Authorization: t.startsWith('Bearer ') ? t : `Bearer ${t}`,
  };
}

function collectProfileIds(
  rows: Array<{verification_profile_id?: string}> | undefined,
): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const c of rows ?? []) {
    const id = (c.verification_profile_id || '').trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out.push(id);
  }
  return out;
}

function collectAcceptanceDescriptions(
  rows: Array<{description?: string}> | undefined,
): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const c of rows ?? []) {
    const d = (c.description || '').trim();
    if (!d || seen.has(d)) continue;
    seen.add(d);
    out.push(d);
  }
  return out;
}

/** 把验收要点拼进续跑/用户文案；不含工具顺序与 profile UUID。 */
export function formatAcceptanceCriteriaBlock(
  descriptions: readonly string[],
): string {
  const lines = descriptions.map((d) => d.trim()).filter(Boolean);
  if (lines.length === 0) return '';
  return `验收要点：\n${lines.map((d) => `- ${d}`).join('\n')}`;
}

/**
 * Worker 可读 Goal（objective）+ Task（acceptance profiles/描述）。
 * 无 ACTIVE 租约时 Control 返回 404。
 */
export async function fetchGoalSealActivation(input: {
  baseUrl: string;
  authorization: string;
  goalId: string;
  taskId: string;
  fetchImpl?: typeof fetch;
}): Promise<GoalSealActivation> {
  const taskId = input.taskId.trim();
  if (!taskId) {
    throw new Error('TASK_ID_REQUIRED_FOR_SEAL');
  }
  const base = input.baseUrl.replace(/\/$/, '');
  const fetchFn = input.fetchImpl ?? fetch;
  const headers = authHeaders(input.authorization);

  const [goalRes, taskRes] = await Promise.all([
    fetchFn(`${base}/api/v1/goals/${input.goalId}`, {
      method: 'GET',
      headers,
    }),
    fetchFn(`${base}/api/v1/tasks/${taskId}`, {
      method: 'GET',
      headers,
    }),
  ]);
  if (!goalRes.ok) {
    throw new Error(`GOAL_GET_FAILED: HTTP ${goalRes.status}`);
  }
  if (!taskRes.ok) {
    throw new Error(`TASK_GET_FAILED: HTTP ${taskRes.status}`);
  }

  const goalBody = (await goalRes.json()) as Envelope<{
    id: string;
    objective?: string;
    contract?: {
      objective?: string;
      success_criteria?: Array<{verification_profile_id?: string}>;
    };
  }>;
  const taskBody = (await taskRes.json()) as Envelope<{
    id: string;
    contract?: {
      acceptance?: Array<{
        verification_profile_id?: string;
        description?: string;
      }>;
    };
  }>;

  const goal = goalBody.data;
  const objective = (
    goal.objective ||
    goal.contract?.objective ||
    ''
  ).trim();
  if (!objective) {
    throw new Error('GOAL_OBJECTIVE_EMPTY');
  }

  const acceptance = taskBody.data.contract?.acceptance ?? [];
  const sealVerificationProfileIds = collectProfileIds(acceptance);
  if (sealVerificationProfileIds.length === 0) {
    throw new Error('TASK_SEAL_PROFILES_EMPTY');
  }
  const acceptanceDescriptions = collectAcceptanceDescriptions(acceptance);
  const criteriaBlock = formatAcceptanceCriteriaBlock(acceptanceDescriptions);

  return {
    goalId: goal.id || input.goalId,
    taskId: taskBody.data.id || taskId,
    objective,
    sealVerificationProfileIds,
    acceptanceDescriptions,
    userPromptFromGoal:
      `目标：${objective}\n` +
      (criteriaBlock ? `${criteriaBlock}\n` : '') +
      `请自行使用可用工具阅读代码、跑公开测试、修复缺陷、复测通过后封存候选。` +
      `同一文件只读一次即可；确认缺陷后立即写入修复并复测，不要反复打开同一路径。` +
      `不要宣称 Goal DONE。`,
  };
}
