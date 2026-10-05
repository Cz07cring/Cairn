/**
 * Runner activation seam（doc/05）：fake 与 Harness adapter 共用。
 * RuntimeEvent 只表示运行输入，不能直接认定业务成功。
 */

export type ActivationRef = {
  activation_id: string;
  activity_id: string;
  attempt_id: string;
  fencing_epoch: string;
  adapter: 'fake' | 'harness';
};

export type RuntimeEventType =
  | 'STARTED'
  | 'MODEL_OUTPUT'
  | 'TOOL_PROPOSAL'
  | 'CHECKPOINT_PROPOSED'
  | 'STOP_OBSERVED'
  | 'FINISHED';

export type RuntimeEvent = {
  activation_id: string;
  seq: number;
  type: RuntimeEventType;
  payload_ref: string | null;
};

export type HarnessAdapterPorts = {
  /** 钉扎 checkout；未配置时 adapter 拒绝启动（不做 format-only 冒充）。 */
  requirePinnedCheckout: () => 'checkout-matched';
  claimPlan: () => Promise<import('./fakePlanHost.js').ActivityLeaseView | null>;
  /** Kernel ContextCompiler；禁止手写空 bindings 冒充。 */
  compileContext: (input: {
    activityId: string;
    lease: import('./fakePlanHost.js').LeaseIdentity;
    maxInputTokens?: number;
  }) => Promise<{id: string; content_digest: string; content: Record<string, unknown>}>;
  bindContext: (input: {
    activityId: string;
    lease: import('./fakePlanHost.js').LeaseIdentity;
    bindingDigest: string;
    contextBundleId: string;
  }) => Promise<{context_digest: string}>;
  completeModelTurn: (input: {
    lease: import('./fakePlanHost.js').LeaseIdentity;
    contextDigest: string;
    toolsExposedToModel: readonly string[];
  }) => Promise<void>;
  buildPlan: (
    lease: import('./fakePlanHost.js').ActivityLeaseView,
  ) => Record<string, unknown>;
  submitPlanOutcome: (input: {
    activityId: string;
    lease: import('./fakePlanHost.js').LeaseIdentity;
    expectedStateRevision: number;
    plan: Record<string, unknown>;
  }) => Promise<void>;
};
