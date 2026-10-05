import {describe, expect, it, vi} from 'vitest';
import {
  activationRefFromLease,
  requireHarnessCheckout,
  runHarnessPlanActivation,
} from './harnessPlanAdapter.js';
import {forwardToolProposalToBroker} from './brokerToolBridge.js';
import {authorizeToolProposal} from '../roles.js';

describe('harnessPlanAdapter', () => {
  it('无 checkout 时拒绝启动', () => {
    const prev = process.env.RING_HARNESS_CHECKOUT;
    delete process.env.RING_HARNESS_CHECKOUT;
    try {
      expect(() => requireHarnessCheckout(undefined)).toThrow(/HARNESS_CHECKOUT_REQUIRED/);
    } finally {
      if (prev !== undefined) {
        process.env.RING_HARNESS_CHECKOUT = prev;
      }
    }
  });

  it('钉扎匹配后走 compileContext 且零工具', async () => {
    const compileContext = vi.fn(async () => ({
      id: 'bundle-1',
      content_digest: 'sha256:' + 'a'.repeat(64),
      content: {role: 'PLANNER', input_bindings: [{classification: 'CONTRACT'}]},
    }));
    const completeModelTurn = vi.fn(async (input: {toolsExposedToModel: readonly string[]}) => {
      expect(input.toolsExposedToModel).toEqual([]);
    });
    const result = await runHarnessPlanActivation({
      requirePinnedCheckout: () => 'checkout-matched',
      claimPlan: async () => ({
        lease: {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
        activity: {
          id: 'a',
          kind: 'PLAN',
          state_revision: 1,
          binding: {goal_contract_revision: 1},
          goal_id: 'g',
          task_id: null,
          project_id: 'p',
        },
      }),
      compileContext,
      bindContext: async () => ({context_digest: 'sha256:' + 'b'.repeat(64)}),
      completeModelTurn,
      buildPlan: () => ({tasks: []}),
      submitPlanOutcome: async () => undefined,
    });
    expect(result).toBe('succeeded');
    expect(compileContext).toHaveBeenCalledOnce();
    expect(completeModelTurn).toHaveBeenCalledOnce();
    const ref = activationRefFromLease(
      {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
      'harness',
    );
    expect(ref.adapter).toBe('harness');
  });
});

describe('brokerToolBridge', () => {
  it('EXECUTE 工具必须经 Broker prepare；派发唯一归 Broker（Runner 不派发）', async () => {
    const prepareEffect = vi.fn(async () => ({
      effectId: 'e1',
      status: 'PREPARED',
      stateRevision: 1,
    }));
    const dispatchEffect = vi.fn(async () => ({status: 'DISPATCHED'}));
    const out = await forwardToolProposalToBroker(
      {prepareEffect, dispatchEffect},
      {
        kind: 'EXECUTE',
        tool: 'read_file',
        logicalStepId: 's1',
        inputArtifactId: 'art1',
        lease: {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
      },
      new Set(['read_file']),
    );
    expect(out.effectId).toBe('e1');
    expect(prepareEffect).toHaveBeenCalledOnce();
    // Runner 不得派发：一旦派发，Broker 取件队列（只认 PREPARED/AUTHORIZED）
    // 就再也取不到该 effect，副作用永无人执行。
    expect(dispatchEffect).not.toHaveBeenCalled();
    expect(out.dispatchStatus).toBe('PREPARED');
  });

  it('PLAN 工具提案本地拒绝且不调用 Broker', () => {
    expect(() => authorizeToolProposal('PLAN', 'read_file', new Set(['read_file']))).toThrow(
      /ROLE_TOOL_FORBIDDEN/,
    );
  });
});
