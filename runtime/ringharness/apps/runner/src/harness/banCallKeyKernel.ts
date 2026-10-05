/**
 * Runner → Control：AB06 软禁同参键读写；≠ DONE。
 */
import type {NudgeBudgetKernelPorts} from './nudgeBudgetKernel.js';

export type GoalBanCallKeysSnapshot = {
  goalId: string;
  callKeys: string[];
  callKey?: string | null;
  inserted?: boolean | null;
  marksGoalDone: false;
};

function bearer(raw: string): string {
  return raw.startsWith('Bearer ') ? raw : `Bearer ${raw}`;
}

/** GET /internal/v1/goals/{id}/ban-call-keys */
export async function fetchGoalBanCallKeys(
  ports: NudgeBudgetKernelPorts,
  goalId: string,
): Promise<GoalBanCallKeysSnapshot> {
  const fetchFn = ports.fetchImpl ?? fetch;
  const base = ports.baseUrl.replace(/\/$/, '');
  const res = await fetchFn(
    `${base}/internal/v1/goals/${encodeURIComponent(goalId)}/ban-call-keys`,
    {
      headers: {
        Authorization: bearer(ports.authorization),
        Accept: 'application/json',
      },
    },
  );
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`BAN_CALL_KEYS_GET_HTTP_${res.status}:${text.slice(0, 200)}`);
  }
  const body = (await res.json()) as {
    data: {
      goal_id: string;
      call_keys: string[];
      marks_goal_done: boolean;
    };
  };
  if (body.data.marks_goal_done !== false) {
    throw new Error('BAN_CALL_KEYS_MARKED_GOAL_DONE');
  }
  return {
    goalId: body.data.goal_id,
    callKeys: body.data.call_keys ?? [],
    marksGoalDone: false,
  };
}

/** POST /internal/v1/goals/{id}/ban-call-keys */
export async function addGoalBanCallKey(
  ports: NudgeBudgetKernelPorts,
  goalId: string,
  callKey: string,
): Promise<GoalBanCallKeysSnapshot> {
  const fetchFn = ports.fetchImpl ?? fetch;
  const base = ports.baseUrl.replace(/\/$/, '');
  const res = await fetchFn(
    `${base}/internal/v1/goals/${encodeURIComponent(goalId)}/ban-call-keys`,
    {
      method: 'POST',
      headers: {
        Authorization: bearer(ports.authorization),
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({call_key: callKey}),
    },
  );
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`BAN_CALL_KEYS_ADD_HTTP_${res.status}:${text.slice(0, 200)}`);
  }
  const body = (await res.json()) as {
    data: {
      goal_id: string;
      call_keys: string[];
      call_key?: string;
      inserted?: boolean;
      marks_goal_done: boolean;
    };
  };
  if (body.data.marks_goal_done !== false) {
    throw new Error('BAN_CALL_KEYS_MARKED_GOAL_DONE');
  }
  return {
    goalId: body.data.goal_id,
    callKeys: body.data.call_keys ?? [],
    callKey: body.data.call_key,
    inserted: body.data.inserted,
    marksGoalDone: false,
  };
}
