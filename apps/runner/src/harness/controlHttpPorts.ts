/**
 * Control HTTP 端口：把 Cordis/Harness PLAN 桥接到已实现的 Kernel 路由。
 *
 * TEMPORAL RunActivation：活动已 admit，lease 来自 Activity 输入；
 * 禁止再 POST /internal/v1/claims 扫描 claim（避免抢其它 READY）。
 * fixture 模型回合默认不 liveDispatch；≠ Goal DONE；≠ 真 Qwen 除非显式打开。
 */
import {randomUUID} from 'node:crypto';
import {bearerAuthHeaders} from './authHeader.js';
import type {
  CordisPlanBridgePorts,
  KernelModelInvocationPorts,
} from './cordisLlmBridge.js';
import type {ActivityLeaseView, LeaseIdentity} from './fakePlanHost.js';
import {
  createHttpModelInvocationPorts,
  type ControlHttpConfig,
} from './kernelModelInvocationHttp.js';
import {
  livePlanUserPrompt,
  parseLivePlanCreate,
  type LivePlanGoal,
} from './livePlanCreate.js';
import {createHttpStopSeamPorts} from './stopHttpPorts.js';
import {selectIntegrateCandidateIdFromAudits} from './integrateHost.js';

export type ControlHttpPortsConfig = ControlHttpConfig & {
  /** 已 admit 的租约身份（来自 RunActivation 输入）。 */
  admittedLease: LeaseIdentity;
  /**
   * 覆盖默认 PlanCreate。未提供时由 createControlHttpCordisPortsWithGoalPlan
   * 预取 Goal 合同后注入（对齐 test_plans._plan_body）。
   */
  buildPlan: (
    lease: ActivityLeaseView,
    opts?: {modelNarrative?: string},
  ) => Record<string, unknown>;
  livePlanPrompt?: (contextDigest: string) => string;
  /** 心跳失败时关闭 Broker 工具准入（v0.6 §6）。 */
  toolAdmissionGate?: CordisPlanBridgePorts['toolAdmissionGate'];
};

type Envelope<T> = {data: T};

type ActivityApiRow = {
  id: string;
  kind: string;
  state_revision: number;
  binding: Record<string, unknown>;
  goal_id: string | null;
  task_id: string | null;
  project_id: string;
  target?: {type: string; id?: string | null};
  verification_assignments?: Array<Record<string, unknown>>;
};

type GoalApiRow = {
  id: string;
  contract_revision?: number;
  plan_revision?: number | null;
  contract: {
    budget: Record<string, unknown>;
    success_criteria: Array<{
      id: string;
      verification_profile_id: string;
    }>;
  };
  barrier?: {
    id: string;
    status?: string;
    candidate_manifest_id?: string;
  } | null;
};

function authHeaders(authorization: string): Record<string, string> {
  return bearerAuthHeaders(authorization, {'Content-Type': 'application/json'});
}

function trimBase(url: string): string {
  return url.replace(/\/+$/, '');
}

function activityViewFromApi(activity: ActivityApiRow): ActivityLeaseView['activity'] {
  return {
    id: activity.id,
    kind: activity.kind,
    state_revision: activity.state_revision,
    binding: activity.binding ?? {},
    goal_id: activity.goal_id,
    task_id: activity.task_id ?? null,
    project_id: activity.project_id,
    ...(activity.target
      ? {target: {type: activity.target.type, id: activity.target.id ?? null}}
      : {}),
    ...(Array.isArray(activity.verification_assignments)
      ? {verification_assignments: activity.verification_assignments}
      : {}),
  };
}

async function readEnvelope<T>(res: Response, label: string): Promise<T> {
  if (!res.ok) {
    let detail = '';
    try {
      detail = (await res.text()).slice(0, 500);
    } catch {
      detail = '';
    }
    throw new Error(
      detail ? `${label}: HTTP ${res.status} ${detail}` : `${label}: HTTP ${res.status}`,
    );
  }
  const body = (await res.json()) as Envelope<T>;
  if (body.data == null) {
    throw new Error(`${label}: 响应缺少 data`);
  }
  return body.data;
}

/** 从 Goal 合同构造最小合法 PlanCreate（仅非 live / 单测骨架；live 禁止静默使用）。 */
export function buildFixturePlanFromGoal(
  goal: GoalApiRow,
): Record<string, unknown> {
  const criterion = goal.contract.success_criteria[0];
  if (!criterion) {
    throw new Error('PLAN_FIXTURE_GOAL_MISSING_CRITERION');
  }
  const taskId = randomUUID();
  const profileId = criterion.verification_profile_id;
  return {
    expected_plan_revision: goal.plan_revision ?? null,
    reason: 'runner-control-http fixture plan',
    tasks: [
      {
        id: taskId,
        contract: {
          objective: '修复并验证',
          depends_on: [],
          input_artifact_ids: [],
          deliverables: [{kind: 'patch', required: true}],
          acceptance: [
            {
              id: 'A1',
              description: '机械验收',
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

/**
 * 装配 CordisPlanBridgePorts：claim 用 GET activity + admitted lease，不扫 claim。
 * 关键路由：
 * - GET  /api/v1/activities/{id}
 * - POST /internal/v1/activities/{id}/context-compile
 * - POST /internal/v1/activities/{id}/context
 * - POST /internal/v1/model-invocations
 * - POST /internal/v1/activities/{id}/outcomes
 * （默认 PlanCreate 另见 WithGoalPlan → GET /api/v1/goals/{id}）
 */
export function createControlHttpCordisPorts(
  config: ControlHttpPortsConfig,
): CordisPlanBridgePorts {
  const baseUrl = trimBase(config.baseUrl);
  const fetchFn = config.fetchImpl ?? fetch;
  const headers = authHeaders(config.authorization);
  const lease = config.admittedLease;

  const modelInvocation: KernelModelInvocationPorts = createHttpModelInvocationPorts({
    baseUrl,
    authorization: headers.Authorization,
    fetchImpl: fetchFn,
    liveDispatch: config.liveDispatch,
    fixtureText: config.fixtureText,
  });

  const stopSeam = createHttpStopSeamPorts({
    baseUrl,
    authorization: headers.Authorization,
    fetchImpl: fetchFn,
  });

  return {
    async claimPlan(): Promise<ActivityLeaseView | null> {
      // 已 admit：只读活动快照；禁止 POST /internal/v1/claims。
      const res = await fetchFn(`${baseUrl}/api/v1/activities/${lease.activity_id}`, {
        method: 'GET',
        headers,
      });
      if (res.status === 404) {
        return null;
      }
      const activity = await readEnvelope<ActivityApiRow>(res, 'ACTIVITY_GET_FAILED');
      if (activity.id !== lease.activity_id) {
        throw new Error('ACTIVITY_ID_MISMATCH');
      }
      return {
        lease: {
          activity_id: lease.activity_id,
          attempt_id: lease.attempt_id,
          fencing_epoch: lease.fencing_epoch,
        },
        activity: activityViewFromApi(activity),
      };
    },

    async compileContext(input) {
      const res = await fetchFn(
        `${baseUrl}/internal/v1/activities/${input.activityId}/context-compile`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            lease: input.lease,
            max_input_tokens: input.maxInputTokens ?? 8192,
          }),
        },
      );
      return readEnvelope<{
        id: string;
        content_digest: string;
        content: Record<string, unknown>;
      }>(res, 'CONTEXT_COMPILE_FAILED');
    },

    async bindContext(input) {
      const res = await fetchFn(
        `${baseUrl}/internal/v1/activities/${input.activityId}/context`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            lease: input.lease,
            binding_digest: input.bindingDigest,
            context_bundle_id: input.contextBundleId,
          }),
        },
      );
      return readEnvelope<{context_digest: string}>(res, 'CONTEXT_BIND_FAILED');
    },

    async heartbeat(input) {
      const res = await fetchFn(
        `${baseUrl}/internal/v1/activities/${input.activityId}/heartbeat`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            lease: input.lease,
            renewal_seq: input.renewalSeq,
          }),
        },
      );
      return readEnvelope<{
        renewal_seq: number;
        control?: string;
        pending_stop_ids?: string[];
      }>(res, 'HEARTBEAT_FAILED');
    },

    modelInvocation,

    buildPlan: config.buildPlan,

    livePlanPrompt: config.livePlanPrompt,

    toolAdmissionGate: config.toolAdmissionGate,

    stopSeam,

    async submitPlanOutcome(input) {
      const res = await fetchFn(
        `${baseUrl}/internal/v1/activities/${input.activityId}/outcomes`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            lease: input.lease,
            expected_state_revision: input.expectedStateRevision,
            outcome: {plan: input.plan},
          }),
        },
      );
      if (!res.ok) {
        throw new Error(`PLAN_OUTCOME_FAILED: HTTP ${res.status}`);
      }
    },
  };
}

/**
 * 预取 Goal 合同并装配 buildPlan / livePlanPrompt。
 * live：解析模型 PlanCreate JSON；非 live：合同 fixture 骨架。
 * 额外路由：GET /api/v1/goals/{id}。
 */
export async function createControlHttpCordisPortsWithGoalPlan(
  config: Omit<ControlHttpPortsConfig, 'buildPlan' | 'livePlanPrompt'> & {
    buildPlan?: ControlHttpPortsConfig['buildPlan'];
    livePlanPrompt?: ControlHttpPortsConfig['livePlanPrompt'];
  },
): Promise<CordisPlanBridgePorts> {
  if (config.buildPlan) {
    return createControlHttpCordisPorts({
      ...config,
      buildPlan: config.buildPlan,
      livePlanPrompt: config.livePlanPrompt,
    });
  }

  const baseUrl = trimBase(config.baseUrl);
  const fetchFn = config.fetchImpl ?? fetch;
  const headers = authHeaders(config.authorization);
  const lease = config.admittedLease;

  const activityRes = await fetchFn(
    `${baseUrl}/api/v1/activities/${lease.activity_id}`,
    {method: 'GET', headers},
  );
  const activity = await readEnvelope<ActivityApiRow>(
    activityRes,
    'ACTIVITY_GET_FAILED',
  );
  if (!activity.goal_id) {
    throw new Error('ACTIVITY_MISSING_GOAL_ID');
  }
  const goalRes = await fetchFn(`${baseUrl}/api/v1/goals/${activity.goal_id}`, {
    method: 'GET',
    headers,
  });
  const goal = await readEnvelope<GoalApiRow>(goalRes, 'GOAL_GET_FAILED');
  const liveGoal = goal as LivePlanGoal;

  return createControlHttpCordisPorts({
    ...config,
    buildPlan: (_lease, opts) => {
      const narrative = opts?.modelNarrative?.trim();
      if (narrative) {
        return parseLivePlanCreate(narrative, liveGoal);
      }
      return buildFixturePlanFromGoal(goal);
    },
    livePlanPrompt: (contextDigest) => livePlanUserPrompt(liveGoal, contextDigest),
  });
}

export type GoalReviewControlHttpConfig = ControlHttpConfig & {
  admittedLease: LeaseIdentity;
};

/**
 * AUDIT×GOAL_REVIEW：claim=GET activity；compile/bind/artifact/outcome。
 * 不依赖 Cordis / PlanCreate；≠ criterion PASS；≠ Goal DONE。
 */
export function createControlHttpGoalReviewPorts(
  config: GoalReviewControlHttpConfig,
): import('./goalReviewCritic.js').GoalReviewCriticPorts {
  const baseUrl = trimBase(config.baseUrl);
  const fetchFn = config.fetchImpl ?? fetch;
  const headers = authHeaders(config.authorization);
  const lease = config.admittedLease;

  return {
    async claimAudit(): Promise<ActivityLeaseView | null> {
      const res = await fetchFn(`${baseUrl}/api/v1/activities/${lease.activity_id}`, {
        method: 'GET',
        headers,
      });
      if (res.status === 404) {
        return null;
      }
      const activity = await readEnvelope<ActivityApiRow>(res, 'ACTIVITY_GET_FAILED');
      if (activity.id !== lease.activity_id) {
        throw new Error('ACTIVITY_ID_MISMATCH');
      }
      return {
        lease: {
          activity_id: lease.activity_id,
          attempt_id: lease.attempt_id,
          fencing_epoch: lease.fencing_epoch,
        },
        activity: activityViewFromApi(activity),
      };
    },

    async compileContext(input) {
      const res = await fetchFn(
        `${baseUrl}/internal/v1/activities/${input.activityId}/context-compile`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            lease: input.lease,
            max_input_tokens: input.maxInputTokens ?? 8192,
          }),
        },
      );
      return readEnvelope<{
        id: string;
        content_digest: string;
        content: Record<string, unknown>;
      }>(res, 'CONTEXT_COMPILE_FAILED');
    },

    async bindContext(input) {
      const res = await fetchFn(
        `${baseUrl}/internal/v1/activities/${input.activityId}/context`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            lease: input.lease,
            binding_digest: input.bindingDigest,
            context_bundle_id: input.contextBundleId,
          }),
        },
      );
      return readEnvelope<{context_digest: string}>(res, 'CONTEXT_BIND_FAILED');
    },

    async readEvidenceArtifact(artifactId) {
      const res = await fetchFn(
        `${baseUrl}/api/v1/artifacts/${artifactId}/content`,
        {method: 'GET', headers},
      );
      if (!res.ok) {
        throw new Error(`ARTIFACT_CONTENT_FAILED: HTTP ${res.status}`);
      }
      return res.text();
    },

    async submitGoalReviewOutcome(input) {
      const res = await fetchFn(
        `${baseUrl}/internal/v1/activities/${input.activityId}/outcomes`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            lease: input.lease,
            expected_state_revision: input.expectedStateRevision,
            outcome: {
              target_type: 'GOAL_REVIEW',
              review: input.review,
            },
          }),
        },
      );
      if (!res.ok) {
        let detail = '';
        try {
          detail = (await res.text()).slice(0, 500);
        } catch {
          detail = '';
        }
        throw new Error(
          detail
            ? `GOAL_REVIEW_OUTCOME_FAILED: HTTP ${res.status} ${detail}`
            : `GOAL_REVIEW_OUTCOME_FAILED: HTTP ${res.status}`,
        );
      }
    },
  };
}

/**
 * AUDIT×CANDIDATE：claim + VerificationRun + AuditCandidateOutcome。
 * 观察（Broker/verifier）由调用方 DI；本装配不做 stub PASS。
 */
export function createControlHttpCandidateAuditPorts(
  config: GoalReviewControlHttpConfig & {
    observeCandidate: import('./candidateAuditor.js').CandidateAuditPorts['observeCandidate'];
  },
): import('./candidateAuditor.js').CandidateAuditPorts {
  const baseUrl = trimBase(config.baseUrl);
  const fetchFn = config.fetchImpl ?? fetch;
  const headers = authHeaders(config.authorization);
  const lease = config.admittedLease;

  return {
    observeCandidate: config.observeCandidate,

    async claimAudit(): Promise<ActivityLeaseView | null> {
      const res = await fetchFn(`${baseUrl}/api/v1/activities/${lease.activity_id}`, {
        method: 'GET',
        headers,
      });
      if (res.status === 404) {
        return null;
      }
      const activity = await readEnvelope<ActivityApiRow>(res, 'ACTIVITY_GET_FAILED');
      if (activity.id !== lease.activity_id) {
        throw new Error('ACTIVITY_ID_MISMATCH');
      }
      return {
        lease: {
          activity_id: lease.activity_id,
          attempt_id: lease.attempt_id,
          fencing_epoch: lease.fencing_epoch,
        },
        activity: activityViewFromApi(activity),
      };
    },

    async createVerificationRun(input) {
      const obs = input.observation;
      const body = {
        lease: input.lease,
        run: {
          project_id: input.activity.project_id,
          producer_activity_id: input.activity.id,
          producer_attempt_id: input.lease.attempt_id,
          subject_type: 'CANDIDATE',
          subject_id: input.activity.target?.id ?? input.assignment.subject_id,
          subject_digest: obs.subjectDigest,
          verification_profile_id: input.assignment.verification_profile_id,
          verifier_digest: obs.verifierDigest,
          audit_round: input.assignment.audit_round,
          layer: input.assignment.layer,
          input_digest: obs.inputDigest,
          environment_digest: obs.environmentDigest,
          receipt_ids: [obs.receiptArtifactId],
          observations: [
            {
              criterion_id: obs.criterionId,
              metric: 'checks_passed',
              value: obs.checksPassed ? 'true' : 'false',
              status: 'OBSERVED',
              evidence_ids: [obs.evidenceArtifactId],
              reason_code: 'MEASURED',
            },
          ],
        },
      };
      const res = await fetchFn(`${baseUrl}/internal/v1/verification-runs`, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
      });
      const data = await readEnvelope<{id: string}>(res, 'VERIFICATION_RUN_FAILED');
      return {runId: data.id};
    },

    async submitCandidateAuditOutcome(input) {
      const res = await fetchFn(
        `${baseUrl}/internal/v1/activities/${input.activityId}/outcomes`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            lease: input.lease,
            expected_state_revision: input.expectedStateRevision,
            outcome: {
              target_type: 'CANDIDATE',
              audit: input.audit,
            },
          }),
        },
      );
      if (!res.ok) {
        let detail = '';
        try {
          detail = (await res.text()).slice(0, 500);
        } catch {
          detail = '';
        }
        throw new Error(
          detail
            ? `CANDIDATE_AUDIT_OUTCOME_FAILED: HTTP ${res.status} ${detail}`
            : `CANDIDATE_AUDIT_OUTCOME_FAILED: HTTP ${res.status}`,
        );
      }
    },
  };
}

/**
 * FINALIZE：claim + barrier 上下文 + VerificationRun + FinalizeSuccessOutcome。
 * 观察由调用方 DI；本装配不做 stub PASS / 不自报 Goal DONE。
 */
export function createControlHttpFinalizePorts(
  config: GoalReviewControlHttpConfig & {
    observeFinalize: import('./finalizeAuditor.js').FinalizeAuditorPorts['observeFinalize'];
  },
): import('./finalizeAuditor.js').FinalizeAuditorPorts {
  const baseUrl = trimBase(config.baseUrl);
  const fetchFn = config.fetchImpl ?? fetch;
  const headers = authHeaders(config.authorization);
  const lease = config.admittedLease;

  return {
    observeFinalize: config.observeFinalize,

    async claimFinalize() {
      const res = await fetchFn(`${baseUrl}/api/v1/activities/${lease.activity_id}`, {
        method: 'GET',
        headers,
      });
      if (res.status === 404) {
        return null;
      }
      const activity = await readEnvelope<ActivityApiRow>(res, 'ACTIVITY_GET_FAILED');
      if (activity.id !== lease.activity_id) {
        throw new Error('ACTIVITY_ID_MISMATCH');
      }
      return {
        lease: {
          activity_id: lease.activity_id,
          attempt_id: lease.attempt_id,
          fencing_epoch: lease.fencing_epoch,
        },
        activity: activityViewFromApi(activity),
      };
    },

    async loadBarrierContext(claimed) {
      const goalId = claimed.activity.goal_id;
      if (!goalId) {
        throw new Error('FINALIZE_MISSING_GOAL_ID');
      }
      const res = await fetchFn(`${baseUrl}/api/v1/goals/${goalId}`, {
        method: 'GET',
        headers,
      });
      const goal = await readEnvelope<GoalApiRow>(res, 'GOAL_GET_FAILED');
      const barrier = goal.barrier;
      if (!barrier?.id || !barrier.candidate_manifest_id) {
        throw new Error('GOAL_BARRIER_MISSING');
      }
      const rev = Number(goal.contract_revision);
      if (!Number.isFinite(rev) || rev < 1) {
        throw new Error('INVALID_GOAL_CONTRACT_REVISION');
      }
      const criterionByProfile: Record<string, string> = {};
      for (const c of goal.contract.success_criteria ?? []) {
        if (c?.id && c.verification_profile_id) {
          criterionByProfile[c.verification_profile_id] = c.id;
        }
      }
      return {
        barrierId: barrier.id,
        candidateManifestId: barrier.candidate_manifest_id,
        goalContractRevision: rev,
        criterionByProfile,
      };
    },

    async createVerificationRun(input) {
      const obs = input.observation;
      const body = {
        lease: input.lease,
        run: {
          project_id: input.activity.project_id,
          producer_activity_id: input.activity.id,
          producer_attempt_id: input.lease.attempt_id,
          subject_type: 'CANDIDATE',
          subject_id: input.activity.target?.id ?? input.assignment.subject_id,
          subject_digest: obs.subjectDigest,
          verification_profile_id: input.assignment.verification_profile_id,
          verifier_digest: obs.verifierDigest,
          audit_round: input.assignment.audit_round,
          layer: input.assignment.layer,
          input_digest: obs.inputDigest,
          environment_digest: obs.environmentDigest,
          receipt_ids: [obs.receiptArtifactId],
          observations: [
            {
              criterion_id: input.criterionId,
              metric: 'checks_passed',
              value: obs.checksPassed ? 'true' : 'false',
              status: 'OBSERVED',
              evidence_ids: [obs.evidenceArtifactId],
              reason_code: 'MEASURED',
            },
          ],
        },
      };
      const res = await fetchFn(`${baseUrl}/internal/v1/verification-runs`, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
      });
      const data = await readEnvelope<{id: string}>(res, 'VERIFICATION_RUN_FAILED');
      return {runId: data.id};
    },

    async submitFinalizeOutcome(input) {
      const res = await fetchFn(
        `${baseUrl}/internal/v1/activities/${input.activityId}/outcomes`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            lease: input.lease,
            expected_state_revision: input.expectedStateRevision,
            outcome: {
              barrier_id: input.barrierId,
              candidate_manifest_id: input.candidateManifestId,
              global_audits: input.globalAudits,
              evidence_ids: input.evidenceIds,
            },
          }),
        },
      );
      if (!res.ok) {
        let detail = '';
        try {
          detail = (await res.text()).slice(0, 500);
        } catch {
          detail = '';
        }
        throw new Error(
          detail
            ? `FINALIZE_OUTCOME_FAILED: HTTP ${res.status} ${detail}`
            : `FINALIZE_OUTCOME_FAILED: HTTP ${res.status}`,
        );
      }
    },
  };
}

/**
 * INTEGRATE：claim + 从 Goal audits 解析 PASS 候选 + IntegrateSuccessOutcome。
 * 禁止硬编码假 candidate id；≠ Goal DONE。
 */
export function createControlHttpIntegratePorts(
  config: GoalReviewControlHttpConfig,
): import('./integrateHost.js').IntegratePorts {
  const baseUrl = trimBase(config.baseUrl);
  const fetchFn = config.fetchImpl ?? fetch;
  const headers = authHeaders(config.authorization);
  const lease = config.admittedLease;

  return {
    async claimIntegrate() {
      const res = await fetchFn(`${baseUrl}/api/v1/activities/${lease.activity_id}`, {
        method: 'GET',
        headers,
      });
      if (res.status === 404) {
        return null;
      }
      const activity = await readEnvelope<ActivityApiRow>(res, 'ACTIVITY_GET_FAILED');
      if (activity.id !== lease.activity_id) {
        throw new Error('ACTIVITY_ID_MISMATCH');
      }
      return {
        lease: {
          activity_id: lease.activity_id,
          attempt_id: lease.attempt_id,
          fencing_epoch: lease.fencing_epoch,
        },
        activity: activityViewFromApi(activity),
      };
    },

    async resolveIntegrateCandidate(claimed) {
      const goalId = claimed.activity.goal_id;
      if (!goalId) {
        throw new Error('INTEGRATE_MISSING_GOAL_ID');
      }
      const auditsRes = await fetchFn(
        `${baseUrl}/api/v1/goals/${goalId}/audits?limit=100`,
        {method: 'GET', headers},
      );
      const audits = await readEnvelope<
        Array<{
          record_type?: string;
          audit?: {
            verdict?: string;
            layer?: string;
            subject_candidate_manifest_id?: string;
          };
          aggregation?: {
            verdict?: string;
            subject_candidate_manifest_id?: string;
            missing_items?: unknown[];
            accepted_audit_ids?: unknown[];
          };
        }>
      >(auditsRes, 'GOAL_AUDITS_FAILED');

      // 选型权威=Kernel aggregation（required_layers 槽齐全），非 Runner 自计 PASS 层数
      const candidateId = selectIntegrateCandidateIdFromAudits(audits);

      const candRes = await fetchFn(
        `${baseUrl}/api/v1/candidates/${candidateId}`,
        {method: 'GET', headers},
      );
      const cand = await readEnvelope<{
        id: string;
        git_commit?: string | null;
      }>(candRes, 'CANDIDATE_GET_FAILED');
      if (cand.id !== candidateId) {
        throw new Error('CANDIDATE_ID_MISMATCH');
      }
      return {
        candidateManifestId: cand.id,
        integrationCommit:
          typeof cand.git_commit === 'string' && cand.git_commit
            ? cand.git_commit
            : null,
        evidenceIds: [],
      };
    },

    async submitIntegrateOutcome(input) {
      const res = await fetchFn(
        `${baseUrl}/internal/v1/activities/${input.activityId}/outcomes`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            lease: input.lease,
            expected_state_revision: input.expectedStateRevision,
            outcome: {
              candidate_manifest_id: input.candidateManifestId,
              integration_commit: input.integrationCommit,
              evidence_ids: input.evidenceIds,
            },
          }),
        },
      );
      if (!res.ok) {
        let detail = '';
        try {
          detail = (await res.text()).slice(0, 500);
        } catch {
          detail = '';
        }
        throw new Error(
          detail
            ? `INTEGRATE_OUTCOME_FAILED: HTTP ${res.status} ${detail}`
            : `INTEGRATE_OUTCOME_FAILED: HTTP ${res.status}`,
        );
      }
    },
  };
}
