/**
 * Runner → Control：AB06 Goal Nudge 预算读写；≠ DONE。
 */
export type GoalNudgeBudgetSnapshot = {
  goalId: string;
  consumed: number;
  maxBudget: number;
  accepted?: boolean | null;
  marksGoalDone: false;
};

export type NudgeBudgetKernelPorts = {
  baseUrl: string;
  authorization: string;
  fetchImpl?: typeof fetch;
};

function bearer(raw: string): string {
  return raw.startsWith('Bearer ') ? raw : `Bearer ${raw}`;
}

/** GET /internal/v1/goals/{id}/nudge-budget */
export async function fetchGoalNudgeBudget(
  ports: NudgeBudgetKernelPorts,
  goalId: string,
  maxBudget: number,
): Promise<GoalNudgeBudgetSnapshot> {
  const fetchFn = ports.fetchImpl ?? fetch;
  const base = ports.baseUrl.replace(/\/$/, '');
  const q = new URLSearchParams({max_budget: String(maxBudget)});
  const res = await fetchFn(
    `${base}/internal/v1/goals/${encodeURIComponent(goalId)}/nudge-budget?${q}`,
    {
      headers: {
        Authorization: bearer(ports.authorization),
        Accept: 'application/json',
      },
    },
  );
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`NUDGE_BUDGET_GET_HTTP_${res.status}:${text.slice(0, 200)}`);
  }
  const body = (await res.json()) as {
    data: {
      goal_id: string;
      consumed: number;
      max_budget: number;
      marks_goal_done: boolean;
    };
  };
  if (body.data.marks_goal_done !== false) {
    throw new Error('NUDGE_BUDGET_MARKED_GOAL_DONE');
  }
  return {
    goalId: body.data.goal_id,
    consumed: body.data.consumed,
    maxBudget: body.data.max_budget,
    marksGoalDone: false,
  };
}

/** POST /internal/v1/goals/{id}/nudge-budget/consume */
export async function consumeGoalNudgeBudget(
  ports: NudgeBudgetKernelPorts,
  goalId: string,
  maxBudget: number,
): Promise<GoalNudgeBudgetSnapshot & {accepted: boolean}> {
  const fetchFn = ports.fetchImpl ?? fetch;
  const base = ports.baseUrl.replace(/\/$/, '');
  const res = await fetchFn(
    `${base}/internal/v1/goals/${encodeURIComponent(goalId)}/nudge-budget/consume`,
    {
      method: 'POST',
      headers: {
        Authorization: bearer(ports.authorization),
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({max_budget: maxBudget}),
    },
  );
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(
      `NUDGE_BUDGET_CONSUME_HTTP_${res.status}:${text.slice(0, 200)}`,
    );
  }
  const body = (await res.json()) as {
    data: {
      goal_id: string;
      consumed: number;
      max_budget: number;
      accepted: boolean;
      marks_goal_done: boolean;
    };
  };
  if (body.data.marks_goal_done !== false) {
    throw new Error('NUDGE_BUDGET_MARKED_GOAL_DONE');
  }
  return {
    goalId: body.data.goal_id,
    consumed: body.data.consumed,
    maxBudget: body.data.max_budget,
    accepted: Boolean(body.data.accepted),
    marksGoalDone: false,
  };
}
