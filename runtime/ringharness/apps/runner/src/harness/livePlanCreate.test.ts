import {expect, test} from 'vitest';
import {
  extractJsonObject,
  LivePlanParseError,
  parseLivePlanCreate,
} from './livePlanCreate.js';

const goal = {
  contract: {
    budget: {
      wall_clock_seconds: 3600,
      max_tokens: 10000,
      max_cost_usd: '1',
      max_tool_calls: 10,
      max_network_calls: 0,
      max_disk_bytes: 1048576,
      max_gpu_seconds: null,
    },
    success_criteria: [
      {
        id: 'C1',
        verification_profile_id: '55555555-5555-5555-5555-555555555555',
      },
    ],
  },
};

const goodPlan = {
  expected_plan_revision: null,
  reason: '覆盖 C1 并落地补丁',
  tasks: [
    {
      id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
      contract: {
        objective: '实现并验证补丁',
        depends_on: [],
        input_artifact_ids: [],
        deliverables: [{kind: 'patch', required: true}],
        acceptance: [
          {
            id: 'A1',
            description: '测试通过',
            required: true,
            verification_profile_id: '55555555-5555-5555-5555-555555555555',
          },
        ],
        covers_goal_criterion_ids: ['C1'],
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
      goal_criterion_id: 'C1',
      task_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
      task_acceptance_id: 'A1',
      verification_profile_id: '55555555-5555-5555-5555-555555555555',
    },
  ],
};

test('extractJsonObject 支持 markdown fence', () => {
  const got = extractJsonObject('```json\n{"a":1}\n```');
  expect(got).toEqual({a: 1});
});

test('extractJsonObject 跳过前置 CoT 取最后 JSON', () => {
  const got = extractJsonObject(
    'thinking...\n{"noise":true}\nfinal\n{"reason":"r","objective":"o","acceptance_description":"a"}',
  );
  expect(got).toEqual({
    reason: 'r',
    objective: 'o',
    acceptance_description: 'a',
  });
});

test('parseLivePlanCreate 接受合法完整 PlanCreate', () => {
  const plan = parseLivePlanCreate(JSON.stringify(goodPlan), goal);
  expect(String(plan.reason)).toContain('live-qwen-plan-host:');
  expect(plan.tasks).toHaveLength(1);
});

test('parseLivePlanCreate 接受语义槽位并装配结构', () => {
  const plan = parseLivePlanCreate(
    JSON.stringify({
      reason: '覆盖 C1',
      objective: '写补丁',
      acceptance_description: '测过',
    }),
    goal,
  );
  expect(String(plan.reason)).toContain('覆盖 C1');
  expect(plan.reason).not.toBe('runner-control-http fixture plan');
  const tasks = plan.tasks as Array<{contract: {objective: string}}>;
  expect(tasks[0]?.contract.objective).toBe('写补丁');
});

test('parseLivePlanCreate 拒绝错误 verification_profile_id', () => {
  const bad = structuredClone(goodPlan);
  bad.tasks[0].contract.acceptance[0].verification_profile_id =
    '99999999-9999-9999-9999-999999999999';
  expect(() => parseLivePlanCreate(JSON.stringify(bad), goal)).toThrow(
    LivePlanParseError,
  );
});

test('parseLivePlanCreate 拒绝非 JSON 散文', () => {
  expect(() => parseLivePlanCreate('我只写一段计划散文', goal)).toThrow(
    LivePlanParseError,
  );
});
