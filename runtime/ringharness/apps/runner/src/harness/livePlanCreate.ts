/**
 * live Qwen → PlanCreate：从模型正文解析 JSON，校验/装配后提交。
 * 禁止在 live 路径静默回退到合同 fixture（M2：禁 fixture 冒充模型结果）。
 *
 * 两种合法模型输出：
 * 1) 完整 PlanCreate（含 tasks/coverage）
 * 2) 语义槽位 `{reason,objective,acceptance_description}` —— 结构由宿主按合同装配，
 *    文案必须来自模型；缺任一字段或非 JSON → 诚实失败。
 */
import {randomUUID} from 'node:crypto';

export type LivePlanGoal = {
  plan_revision?: number | null;
  contract: {
    budget: Record<string, unknown>;
    success_criteria: Array<{
      id: string;
      verification_profile_id: string;
    }>;
  };
};

export class LivePlanParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'LivePlanParseError';
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function asNonEmptyString(v: unknown, label: string): string {
  if (typeof v !== 'string' || !v.trim()) {
    throw new LivePlanParseError(`缺少或非法字段: ${label}`);
  }
  return v.trim();
}

/** 尝试解析一段候选文本中的 JSON 对象。 */
function tryParseObject(candidate: string): Record<string, unknown> | null {
  const start = candidate.indexOf('{');
  const end = candidate.lastIndexOf('}');
  if (start < 0 || end <= start) {
    return null;
  }
  try {
    const v = JSON.parse(candidate.slice(start, end + 1));
    return isRecord(v) ? v : null;
  } catch {
    return null;
  }
}

/**
 * 从模型正文抽取 JSON 对象（允许 markdown fence / 前置思考链）。
 * 优先「像 Plan」的对象（reason / tasks / 语义槽位），避免命中嵌套小 JSON。
 */
export function extractJsonObject(text: string): unknown {
  const raw = (text || '').trim();
  if (!raw) {
    throw new LivePlanParseError('模型正文为空');
  }

  const candidates: Record<string, unknown>[] = [];

  const pushCandidate = (candidate: string) => {
    // 收集文中所有平衡尝试：从每个 { 扫到匹配的 }
    for (let i = 0; i < candidate.length; i += 1) {
      if (candidate[i] !== '{') continue;
      let depth = 0;
      for (let j = i; j < candidate.length; j += 1) {
        const ch = candidate[j];
        if (ch === '{') depth += 1;
        else if (ch === '}') {
          depth -= 1;
          if (depth === 0) {
            const got = tryParseObject(candidate.slice(i, j + 1));
            if (got) candidates.push(got);
            break;
          }
        }
      }
    }
  };

  const fenced = [...raw.matchAll(/```(?:json)?\s*([\s\S]*?)```/gi)];
  for (const m of fenced) {
    if (m[1]) pushCandidate(m[1].trim());
  }
  pushCandidate(raw);

  if (candidates.length === 0) {
    throw new LivePlanParseError('模型正文中未找到可解析 JSON 对象');
  }

  const score = (o: Record<string, unknown>): number => {
    let s = 0;
    if (typeof o.reason === 'string') s += 3;
    if (Array.isArray(o.tasks)) s += 5;
    if (Array.isArray(o.coverage)) s += 3;
    if (typeof o.objective === 'string') s += 2;
    if (typeof o.acceptance_description === 'string') s += 2;
    // 通用 JSON（单测 / 无 Plan 字段时）仍可选最大对象
    s += Math.min(2, Object.keys(o).length);
    return s;
  };

  candidates.sort((a, b) => score(b) - score(a));
  const best = candidates[0];
  if (!best) {
    throw new LivePlanParseError('模型正文中未找到可解析 JSON 对象');
  }
  return best;
}

function assembleFromSlots(
  goal: LivePlanGoal,
  slots: {
    reason: string;
    objective: string;
    acceptance_description: string;
  },
): Record<string, unknown> {
  const criterion = goal.contract.success_criteria[0];
  if (!criterion?.id || !criterion.verification_profile_id) {
    throw new LivePlanParseError('Goal 合同缺少 success_criteria');
  }
  const taskId = randomUUID();
  const profileId = criterion.verification_profile_id;
  return {
    expected_plan_revision: goal.plan_revision ?? null,
    reason: `live-qwen-plan-host: ${slots.reason}`.slice(0, 10000),
    tasks: [
      {
        id: taskId,
        contract: {
          objective: slots.objective,
          depends_on: [],
          input_artifact_ids: [],
          deliverables: [{kind: 'patch', required: true}],
          acceptance: [
            {
              id: 'A1',
              description: slots.acceptance_description,
              required: true,
              verification_profile_id: profileId,
            },
          ],
          covers_goal_criterion_ids: [criterion.id],
          allowed_paths: ['src/**'],
          protected_paths: [],
          required_capabilities: [],
          budget: goal.contract.budget,
          retry_policy: {
            max_execution_rounds: 2,
            max_audit_attempts_per_candidate: 2,
            max_activity_retries: 1,
          },
          resources: {
            cpu_millicores: 100,
            memory_bytes: 268435456,
            disk_bytes: 67108864,
            model_slots: 0,
            browser_slots: 0,
            exclusive_labels: [],
          },
          risk: 'low',
        },
        replaces_task_id: null,
      },
    ],
    coverage: [
      {
        goal_criterion_id: criterion.id,
        task_id: taskId,
        task_acceptance_id: 'A1',
        verification_profile_id: profileId,
      },
    ],
  };
}

function parseFullPlanCreate(
  parsed: Record<string, unknown>,
  goal: LivePlanGoal,
): Record<string, unknown> {
  const criterion = goal.contract.success_criteria[0]!;
  const reason = asNonEmptyString(parsed.reason, 'reason');
  const tasks = parsed.tasks;
  if (!Array.isArray(tasks) || tasks.length < 1) {
    throw new LivePlanParseError('tasks 须为非空数组');
  }
  const coverage = parsed.coverage;
  if (!Array.isArray(coverage) || coverage.length < 1) {
    throw new LivePlanParseError('coverage 须为非空数组');
  }

  const normalizedTasks: Record<string, unknown>[] = [];
  for (const rawTask of tasks) {
    if (!isRecord(rawTask)) {
      throw new LivePlanParseError('task 须为对象');
    }
    const contract = rawTask.contract;
    if (!isRecord(contract)) {
      throw new LivePlanParseError('task.contract 须为对象');
    }
    const objective = asNonEmptyString(contract.objective, 'task.contract.objective');
    const acceptance = contract.acceptance;
    if (!Array.isArray(acceptance) || acceptance.length < 1) {
      throw new LivePlanParseError('task.contract.acceptance 须为非空数组');
    }
    for (const acc of acceptance) {
      if (!isRecord(acc)) {
        throw new LivePlanParseError('acceptance 项须为对象');
      }
      asNonEmptyString(acc.id, 'acceptance.id');
      asNonEmptyString(acc.description, 'acceptance.description');
      const profile = asNonEmptyString(
        acc.verification_profile_id,
        'acceptance.verification_profile_id',
      );
      if (profile !== criterion.verification_profile_id) {
        throw new LivePlanParseError(
          'acceptance.verification_profile_id 须与 Goal 合同一致',
        );
      }
    }
    const covers = contract.covers_goal_criterion_ids;
    if (!Array.isArray(covers) || !covers.includes(criterion.id)) {
      throw new LivePlanParseError(
        `covers_goal_criterion_ids 须包含 Goal 标准 ${criterion.id}`,
      );
    }
    const taskId =
      typeof rawTask.id === 'string' && rawTask.id.trim()
        ? rawTask.id.trim()
        : randomUUID();
    normalizedTasks.push({
      ...rawTask,
      id: taskId,
      contract: {
        ...contract,
        objective,
        budget: goal.contract.budget,
      },
      replaces_task_id: rawTask.replaces_task_id ?? null,
    });
  }

  const taskIds = new Set(
    normalizedTasks.map((t) => String((t as {id: string}).id)),
  );
  for (const entry of coverage) {
    if (!isRecord(entry)) {
      throw new LivePlanParseError('coverage 项须为对象');
    }
    const goalCriterionId = asNonEmptyString(
      entry.goal_criterion_id,
      'coverage.goal_criterion_id',
    );
    if (goalCriterionId !== criterion.id) {
      throw new LivePlanParseError('coverage.goal_criterion_id 须与 Goal 合同一致');
    }
    const profile = asNonEmptyString(
      entry.verification_profile_id,
      'coverage.verification_profile_id',
    );
    if (profile !== criterion.verification_profile_id) {
      throw new LivePlanParseError(
        'coverage.verification_profile_id 须与 Goal 合同一致',
      );
    }
    const taskId = asNonEmptyString(entry.task_id, 'coverage.task_id');
    if (!taskIds.has(taskId)) {
      throw new LivePlanParseError('coverage.task_id 未出现在 tasks');
    }
  }

  return {
    // 并发版本以 Kernel 刚返回的 Goal 为准，不信任模型自行填写的值。
    expected_plan_revision: goal.plan_revision ?? null,
    reason: reason.startsWith('live-qwen-plan-host:')
      ? reason
      : `live-qwen-plan-host: ${reason}`.slice(0, 10000),
    tasks: normalizedTasks,
    coverage,
  };
}

/**
 * 解析并校验 live PlanCreate（完整或语义槽位）。
 */
export function parseLivePlanCreate(
  narrative: string,
  goal: LivePlanGoal,
): Record<string, unknown> {
  if (!goal.contract.success_criteria[0]?.id) {
    throw new LivePlanParseError('Goal 合同缺少 success_criteria');
  }

  const parsed = extractJsonObject(narrative);
  if (!isRecord(parsed)) {
    throw new LivePlanParseError('PlanCreate 根须为对象');
  }

  // 语义槽位：小模型更稳；结构由宿主装配，文案须来自模型
  if (
    typeof parsed.objective === 'string' &&
    typeof parsed.acceptance_description === 'string' &&
    !Array.isArray(parsed.tasks)
  ) {
    return assembleFromSlots(goal, {
      reason: asNonEmptyString(parsed.reason, 'reason'),
      objective: asNonEmptyString(parsed.objective, 'objective'),
      acceptance_description: asNonEmptyString(
        parsed.acceptance_description,
        'acceptance_description',
      ),
    });
  }

  if (Array.isArray(parsed.tasks)) {
    return parseFullPlanCreate(parsed, goal);
  }

  throw new LivePlanParseError(
    '须输出完整 PlanCreate（含 tasks/coverage）或语义槽位 {reason,objective,acceptance_description}',
  );
}

/** live 用户提示：优先要小 JSON 槽位，降低 CoT/截断导致非法 PlanCreate 的概率。 */
export function livePlanUserPrompt(
  goal: LivePlanGoal,
  contextDigest: string,
): string {
  const criterion = goal.contract.success_criteria[0];
  if (!criterion) {
    throw new LivePlanParseError('Goal 合同缺少 success_criteria');
  }
  return [
    `PLAN context_digest=${contextDigest}`,
    '零工具 Planner。禁止解释与 markdown。只输出一行 JSON。',
    '格式严格为：{"reason":"...","objective":"...","acceptance_description":"..."}',
    '三个字符串均非空，每个不超过40个字。',
    `参考 goal_criterion_id=${criterion.id}`,
  ].join('\n');
}
