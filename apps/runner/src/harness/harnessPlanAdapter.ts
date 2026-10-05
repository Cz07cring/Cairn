/**
 * Harness PLAN adapter（ringharness 侧）：钉扎门禁 + ContextCompiler + 零工具。
 * 尚未加载 Cordis/官方 SDK；有 checkout 且 SHA 匹配才允许跑本路径。
 */
import {bindingDigestOf, rejectPlanTools} from './fakePlanHost.js';
import type {ActivationRef, HarnessAdapterPorts} from './activation.js';
import {runHarnessPinGate} from './pinGate.js';

export function requireHarnessCheckout(
  checkoutDir: string | undefined = process.env.RING_HARNESS_CHECKOUT,
): 'checkout-matched' {
  const result = runHarnessPinGate(checkoutDir);
  if (result !== 'checkout-matched') {
    throw new Error(
      'HARNESS_CHECKOUT_REQUIRED: Harness adapter 必须提供 RING_HARNESS_CHECKOUT 且 HEAD=钉扎 SHA',
    );
  }
  return result;
}

/**
 * 一轮 Harness PLAN activation。
 * 与 fake 共用 outcome 合同；差异在钉扎强制与 compileContext。
 */
export async function runHarnessPlanActivation(
  ports: HarnessAdapterPorts,
): Promise<'idle' | 'succeeded'> {
  ports.requirePinnedCheckout();
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
  // PLAN 工具集必须为空；模型侧意外 tool call 在此拒绝。
  rejectPlanTools([]);

  await ports.bindContext({
    activityId: claimed.activity.id,
    lease: claimed.lease,
    bindingDigest: bindingDigestOf(claimed.activity.binding),
    contextBundleId: compiled.id,
  });

  await ports.completeModelTurn({
    lease: claimed.lease,
    contextDigest: compiled.content_digest,
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

export function activationRefFromLease(
  lease: {activity_id: string; attempt_id: string; fencing_epoch: string},
  adapter: ActivationRef['adapter'],
): ActivationRef {
  return {
    activation_id: lease.attempt_id,
    activity_id: lease.activity_id,
    attempt_id: lease.attempt_id,
    fencing_epoch: lease.fencing_epoch,
    adapter,
  };
}
