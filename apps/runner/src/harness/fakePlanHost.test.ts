import {expect, test} from 'vitest';
import {
  bindingDigestOf,
  rejectPlanTools,
  runFakePlanActivation,
  type ActivityLeaseView,
  type FakePlanHostPorts,
} from './fakePlanHost.js';

function leaseFixture(): ActivityLeaseView {
  return {
    lease: {
      activity_id: '11111111-1111-1111-1111-111111111111',
      attempt_id: '22222222-2222-2222-2222-222222222222',
      fencing_epoch: '1',
    },
    activity: {
      id: '11111111-1111-1111-1111-111111111111',
      kind: 'PLAN',
      state_revision: 2,
      binding: {
        goal_contract_revision: 1,
        plan_revision: null,
        subject_digest: 'sha256:a',
        policy_digest: 'sha256:b',
      },
      goal_id: '33333333-3333-3333-3333-333333333333',
      task_id: null,
      project_id: '44444444-4444-4444-4444-444444444444',
    },
  };
}

test('PLAN 暴露任何工具即 ROLE_TOOL_FORBIDDEN', () => {
  expect(() => rejectPlanTools(['read_file'])).toThrow('ROLE_TOOL_FORBIDDEN');
  expect(() => rejectPlanTools([])).not.toThrow();
});

test('Fake PLAN 宿主零工具闭环：claim→compile→bind→model→outcome', async () => {
  const claimed = leaseFixture();
  const calls: string[] = [];
  const ports: FakePlanHostPorts = {
    claimPlan: async () => {
      calls.push('claim');
      return claimed;
    },
    compileContext: async () => {
      calls.push('compile');
      return {
        id: 'bundle-1',
        content_digest: 'sha256:ctx',
        content: {role: 'PLANNER'},
      };
    },
    bindContext: async () => {
      calls.push('bind');
      return {context_digest: 'sha256:ctx'};
    },
    completeModelTurn: async ({toolsExposedToModel}) => {
      rejectPlanTools(toolsExposedToModel);
      calls.push('model');
    },
    buildPlan: () => {
      calls.push('build');
      return {expected_plan_revision: null, reason: 'fixture', tasks: [], coverage: []};
    },
    submitPlanOutcome: async () => {
      calls.push('outcome');
    },
  };
  await expect(runFakePlanActivation(ports)).resolves.toBe('succeeded');
  expect(calls).toEqual(['claim', 'compile', 'bind', 'model', 'build', 'outcome']);
});

test('无 READY 活动时返回 idle', async () => {
  const ports: FakePlanHostPorts = {
    claimPlan: async () => null,
    compileContext: async () => ({
      id: 'x',
      content_digest: 'sha256:x',
      content: {role: 'PLANNER'},
    }),
    bindContext: async () => ({context_digest: 'sha256:x'}),
    completeModelTurn: async () => undefined,
    buildPlan: () => ({}),
    submitPlanOutcome: async () => undefined,
  };
  await expect(runFakePlanActivation(ports)).resolves.toBe('idle');
});

test('bindingDigest 稳定', () => {
  const d1 = bindingDigestOf({a: 1, b: 2});
  const d2 = bindingDigestOf({b: 2, a: 1});
  expect(d1).toBe(d2);
  expect(d1.startsWith('sha256:')).toBe(true);
});
