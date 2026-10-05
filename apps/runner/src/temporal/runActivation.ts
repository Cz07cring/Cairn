/**
 * Temporal Activity：RunActivation。
 *
 * PLAN：环境就绪时默认装配 Control HTTP ports（claim=GET activity，无扫 claim），
 * 经零工具 Cordis 桥提交 PlanCreate outcome；也可 DI runPlan / createCordisPorts。
 * EXECUTE：env 门后默认装配 Control HTTP + Broker 宿主（kind-aware tools）；
 * `RING_HARNESS_EXECUTE_RUNTIME=deepseek-official-agent-loop` 时改走官方 AgentLoop
 * × Broker Gateway（Runner 不 dispatch）；`RING_HARNESS_EXECUTE_OFFICIAL_MODE=
 * read_file|diagnose`（缺省 read_file；diagnose 可 FSM=1）；可 DI runExecute；
 * 空 effect_ids → FAILED NO_TOOL_PROPOSAL。≠ Goal DONE。
 * AUDIT×GOAL_REVIEW：确定性 Critic（无 Cordis）→ Kernel GOAL_REVIEW outcome；≠ DONE。
 * AUDIT×CANDIDATE / FINALIZE：默认经 Broker `run_tests(suite=auditor)` 观察；≠ Runner 自报 DONE。
 * INTEGRATE：从 Goal PASS 审计解析候选 → outcome；≠ DONE。
 */

import {existsSync} from 'node:fs';
import {
  createDefaultActivationGuards,
  createDefaultActivationGuardsHydrated,
} from '../harness/activationGuards.js';
import {cordisEntryPath} from '../harness/cordisBootGate.js';
import {
  runExecuteToolTurnViaCordisWithLease,
  isCordisHardForceStopError,
  type CordisExecuteTurnPorts,
} from '../harness/cordisExecuteBridge.js';
import {
  runZeroToolPlanTurnViaCordisWithLease,
  type CordisPlanBridgePorts,
} from '../harness/cordisLlmBridge.js';
import {
  formatWatchdogHardIdleFailReason,
  isWatchdogHardIdleError,
  persistWatchdogHardIdlePartial,
} from '../harness/watchdogPartialArtifact.js';
import {handleCordisHardForceStopCloseout} from '../harness/forceStopZeroToolCloseout.js';
import {reportActivationTermination} from '../harness/reportActivationTermination.js';
import {readFileSync} from 'node:fs';
import {createHttpArtifactPutPorts} from '../harness/artifactPutHttpPorts.js';
import {
  createHttpExecuteOutcomeDeps,
  submitExecuteOutcome,
  type ExecuteOutcomeSubmitResult,
} from '../harness/executeOutcomeSubmit.js';
import {
  createControlHttpCordisPortsWithGoalPlan,
  createControlHttpGoalReviewPorts,
  createControlHttpCandidateAuditPorts,
  createControlHttpFinalizePorts,
  createControlHttpIntegratePorts,
} from '../harness/controlHttpPorts.js';
import {
  createExecuteToolHost,
  type ExecuteToolHost,
} from '../harness/executeToolHost.js';
import {
  assessOfficialExecuteRuntime,
  resolveExecuteRuntime,
  runOfficialExecuteTurn,
} from '../harness/executeOfficialRuntime.js';

import {resolveToolResultObserveFromEnv} from '../harness/executeToolResultRound.js';
import type {ActivityLeaseView, LeaseIdentity} from '../harness/fakePlanHost.js';
import {
  type CandidateAuditResult,
  runCandidateAuditTurnWithLease,
} from '../harness/candidateAuditor.js';
import {observeCandidateViaBrokerAuditorSuite} from '../harness/brokerCandidateObserve.js';
import {
  runFinalizeTurnWithLease,
  type FinalizeAuditorResult,
} from '../harness/finalizeAuditor.js';
import {
  runIntegrateTurnWithLease,
  type IntegrateResult,
} from '../harness/integrateHost.js';
import {
  runGoalReviewCriticTurnWithLease,
  type GoalReviewCriticResult,
} from '../harness/goalReviewCritic.js';

/** 与 Python `execute_activity("RunActivation", …)` 对齐的具名 Activity。 */
export const RUN_ACTIVATION_ACTIVITY_NAME = 'RunActivation' as const;

/**
 * 解析 worker 凭据：**优先文件**（`RING_RUNNER_WORKER_JWT_FILE`），其次环境变量。
 *
 * 为什么必须支持文件：控制面对 JWT 的 `exp - iat` 有 **1 小时硬上限**
 * （`apps/control` identity 显式拒绝），长跑的 Runner 无法持一张永久凭据。
 * 环境变量在进程启动时固化 ⇒ 到期后每次激活都以 401 失败，且**无人能刷新它**
 * （实测：broker 侧同因形成永不恢复的 401 空转）。改为每激活读一次文件，
 * 由外部续铸者（`serve_joint_stack.py --refresh-bearer`）刷新该文件即可自愈。
 * 单次激活 < 5 分钟，远短于令牌寿命，故「按激活读取」足够。
 * 读不到文件时回退环境变量，保持既有部署形态不变。
 */
function resolveWorkerJwt(env: Record<string, string | undefined>): string {
  const file = (env.RING_RUNNER_WORKER_JWT_FILE || '').trim();
  if (file) {
    try {
      const fromFile = readFileSync(file, 'utf8').trim();
      if (fromFile) return fromFile;
    } catch {
      // 文件缺失/不可读：回退环境变量，不因续铸者故障而中断
    }
  }
  return (env.RING_RUNNER_WORKER_JWT || env.RING_WORKER_JWT || '').trim();
}

export type ActivationKind =
  | 'PLAN'
  | 'EXECUTE'
  | 'AUDIT'
  | 'FINALIZE'
  | 'INTEGRATE'
  | 'VERIFY'
  | 'CRITIQUE'
  | 'REPAIR'
  | string;

export type RunActivationInput = {
  activity_id: string;
  attempt_id?: string;
  goal_id: string;
  kind: ActivationKind;
  fencing_epoch?: string;
  owner_epoch: string;
};

/** 注入的 PLAN 回合结果（由 Cordis/Harness/fake 端口产出）。 */
export type PlanActivationOutcome = {
  status: 'ACTIVATION_SUBMITTED' | 'IDLE' | 'FAILED';
  pending_harness: boolean;
  kind: 'PLAN';
  reason?: string;
  activity_id: string;
  attempt_id?: string;
  fencing_epoch?: string;
};

export type RunActivationResult =
  | {
      status: 'NOT_IMPLEMENTED';
      reason: string;
      kind: ActivationKind;
      pending_harness: true;
    }
  | {
      status: 'PENDING_ENV';
      reason: string;
      kind: ActivationKind;
      pending_harness: true;
    }
  | {
      status: 'ACTIVATION_SUBMITTED';
      pending_harness: false;
      kind: 'PLAN';
      activity_id: string;
      attempt_id?: string;
      fencing_epoch?: string;
    }
  | {
      status: 'ACTIVATION_SUBMITTED';
      pending_harness: false;
      kind: 'EXECUTE';
      activity_id: string;
      attempt_id?: string;
      fencing_epoch?: string;
      /** 已 prepare/dispatch 的 effect id；≠ SUCCEEDED ≠ Goal DONE */
      effect_ids: string[];
      /** 官方路径诚实证据；缺省表示未走 official 或未透出 */
      tool_names?: string[];
      driver?: string;
      scripted_order?: boolean;
      chat_driven_order?: boolean;
      marks_goal_done?: false;
      /** 活动回执提交结果；提交不了也带原因（见 executeOutcomeSubmit.ts） */
      execute_outcome_submit?: ExecuteOutcomeSubmitResult;
    }
  | {
      status: 'ACTIVATION_SUBMITTED';
      pending_harness: false;
      kind: 'AUDIT';
      activity_id: string;
      attempt_id?: string;
      fencing_epoch?: string;
      finding_codes?: string[];
      verdict?: 'PASS' | 'FAIL' | 'INSUFFICIENT';
      verifier_run_id?: string;
      /** 恒 false：findings/audit ≠ Goal DONE */
      marks_goal_done: false;
    }
  | {
      status: 'ACTIVATION_SUBMITTED';
      pending_harness: false;
      kind: 'FINALIZE';
      activity_id: string;
      attempt_id?: string;
      fencing_epoch?: string;
      verdict?: 'PASS' | 'FAIL' | 'INSUFFICIENT';
      verifier_run_ids?: string[];
      /** 恒 false：Runner 不自报 DONE；Kernel 可能随后裁决 DONE */
      marks_goal_done: false;
    }
  | {
      status: 'ACTIVATION_SUBMITTED';
      pending_harness: false;
      kind: 'INTEGRATE';
      activity_id: string;
      attempt_id?: string;
      fencing_epoch?: string;
      candidate_manifest_id?: string;
      /** 恒 false：INTEGRATE ≠ Goal DONE */
      marks_goal_done: false;
    }
  | {
      status: 'FAILED';
      pending_harness: false;
      kind: ActivationKind;
      reason: string;
      activity_id: string;
      attempt_id?: string;
      fencing_epoch?: string;
      /** AB05：Hard Idle 部分文本已落盘时 */
      partial_artifact_id?: string;
      /** AB06：硬 ForceStop 零工具收口 Artifact id */
      force_stop_closeout_artifact_id?: string;
      force_stop_summary_artifact_id?: string;
      /** AB06：post-dispatch 硬停前已 dispatch 的 effect（对账用；≠ SUCCEEDED ≠ DONE） */
      effect_ids?: string[];
      /** M3.5：Kernel activation_terminations 行 id */
      activation_termination_id?: string;
      marks_goal_done?: false;
    };

export type PlanHarnessEnv = {
  checkout: string;
  controlUrl: string;
  workerJwt: string;
  /** RING_RUNNER_LIVE_DISPATCH=1 时经 Control 打本地 Qwen。 */
  liveDispatch: boolean;
  /** live 时必须为真实 model id，禁止 plan-fixture。 */
  modelId: string;
};

export type PlanHarnessReadiness =
  | ({ok: true} & PlanHarnessEnv)
  | {ok: false; reason: string};

/** 注入的 EXECUTE 回合结果（tool-call→Broker；≠ effect 终态）。 */
export type ExecuteActivationOutcome = {
  status: 'ACTIVATION_SUBMITTED' | 'FAILED';
  pending_harness: boolean;
  kind: 'EXECUTE';
  reason?: string;
  activity_id: string;
  attempt_id?: string;
  fencing_epoch?: string;
  effect_ids?: string[];
  /** ToolResult 第二轮证据；缺省表示未跑第二轮 */
  tool_result_round?: {
    round2_cites_tool_result: boolean;
    requires_reconciliation: boolean;
    marks_goal_done: false;
  };
  tool_names?: string[];
  driver?: string;
  scripted_order?: boolean;
  chat_driven_order?: boolean;
  marks_goal_done?: false;
  /** AB05：Hard Idle 部分文本 Artifact id（FAILED 时） */
  partial_artifact_id?: string;
  /** AB06：硬 ForceStop 零工具收口（FAILED 时）；≠ DONE */
  force_stop_closeout?: {
    assistant_text: string;
    summary_artifact_id: string | null;
    closeout_artifact_id?: string;
    used_fallback: boolean;
    marks_goal_done: false;
  };
  /** M3.5：Kernel activation_terminations 行 id */
  activation_termination_id?: string;
  /**
   * 把成功执行作为活动回执提交给 Kernel 的结果（提交不了也带原因）。
   *
   * 为什么要有：Kernel 的活动行只有收到 outcomes 才离开 `RUNNING`；此前 Runner 侧
   * 五类活动都有回执提交方、唯独 EXECUTE 没有，导致 Temporal 已 COMPLETED 而
   * Kernel 永停 RUNNING，编排观察循环永远等不到变化（2026-09-15 实测）。
   * `submitted:false` 时 `reason` 说明是哪一步不成立，直接可排查。
   */
  execute_outcome_submit?: ExecuteOutcomeSubmitResult;
};

export type RunActivationDeps = {
  /** 测试或宿主注入：完整 PLAN 回合（优先于 createCordisPorts）。 */
  runPlan?: (input: RunActivationInput) => Promise<PlanActivationOutcome>;
  /**
   * 测试或宿主注入：完整 EXECUTE 回合（优先于默认 Cordis+Broker 装配）。
   */
  runExecute?: (input: RunActivationInput) => Promise<ExecuteActivationOutcome>;
  /**
   * 测试或宿主注入：完整 AUDIT×GOAL_REVIEW Critic 回合。
   */
  runAudit?: (input: RunActivationInput) => Promise<GoalReviewCriticResult>;
  /**
   * 测试或宿主注入：完整 AUDIT×CANDIDATE 回合（须含真实观察，禁止 stub PASS）。
   */
  runCandidateAudit?: (
    input: RunActivationInput,
  ) => Promise<CandidateAuditResult>;
  /**
   * 测试或宿主注入：完整 FINALIZE 回合（须含真实观察，禁止 stub PASS）。
   */
  runFinalize?: (input: RunActivationInput) => Promise<FinalizeAuditorResult>;
  /**
   * 测试或宿主注入：完整 INTEGRATE 回合（须解析真实 PASS 候选）。
   */
  runIntegrate?: (input: RunActivationInput) => Promise<IntegrateResult>;
  /**
   * 构造桥端口。未提供且环境齐全时默认装配 Control HTTP ports。
   * PLAN：含 Goal PlanCreate；EXECUTE：仅 claim/compile/bind/model（无 PlanCreate）。
   */
  createCordisPorts?: (
    input: RunActivationInput,
    env: PlanHarnessEnv,
  ) => CordisPlanBridgePorts | Promise<CordisPlanBridgePorts>;
  /** 测试或宿主注入：EXECUTE Broker 宿主（与 createCordisPorts 成对显式 DI）。 */
  createExecuteHost?: (
    input: RunActivationInput,
    env: PlanHarnessEnv,
  ) => ExecuteToolHost | Promise<ExecuteToolHost>;
  /**
   * 测试注入：覆盖默认 fetch（仅默认 HTTP 装配路径使用）。
   */
  fetchImpl?: typeof fetch;
  env?: NodeJS.ProcessEnv;
};

/**
 * 检查 PLAN Harness 桥所需环境（钉扎 checkout + Control HTTP + worker JWT）。
 * 与 Broker 的 RING_BROKER_* 同形；Runner 优先 RING_RUNNER_* / RING_CONTROL_URL。
 * live：RING_RUNNER_LIVE_DISPATCH=1 + RING_LOCAL_QWEN_MODEL（或默认真模型 id）。
 */
export function assessPlanHarnessEnv(
  env: NodeJS.ProcessEnv = process.env,
): PlanHarnessReadiness {
  const checkout = (env.RING_HARNESS_CHECKOUT || '').trim();
  const controlUrl = (
    env.RING_RUNNER_CONTROL_URL ||
    env.RING_CONTROL_URL ||
    ''
  ).trim();
  const workerJwt = resolveWorkerJwt(env);

  if (!checkout) {
    return {ok: false, reason: '缺少 RING_HARNESS_CHECKOUT'};
  }
  if (!controlUrl) {
    return {
      ok: false,
      reason: '缺少 RING_RUNNER_CONTROL_URL 或 RING_CONTROL_URL',
    };
  }
  if (!workerJwt) {
    return {
      ok: false,
      reason: '缺少 RING_RUNNER_WORKER_JWT 或 RING_WORKER_JWT',
    };
  }

  const liveRaw = (env.RING_RUNNER_LIVE_DISPATCH || '').trim().toLowerCase();
  const liveDispatch = liveRaw === '1' || liveRaw === 'true' || liveRaw === 'yes';
  const modelId = (
    env.RING_LOCAL_QWEN_MODEL ||
    env.RING_RUNNER_MODEL_ID ||
    (liveDispatch
      ? 'Qwen3.8-Flash-Next-Uncensored-Mixed-omlx'
      : 'plan-fixture')
  ).trim();
  if (liveDispatch && (modelId === 'plan-fixture' || modelId === 'fixture')) {
    return {
      ok: false,
      reason:
        'liveDispatch 禁止 model_id=plan-fixture；请设置 RING_LOCAL_QWEN_MODEL',
    };
  }

  return {
    ok: true,
    checkout,
    controlUrl,
    workerJwt,
    liveDispatch,
    modelId,
  };
}

export type GoalReviewControlEnv = {
  controlUrl: string;
  workerJwt: string;
};

export type GoalReviewControlReadiness =
  | ({ok: true} & GoalReviewControlEnv)
  | {ok: false; reason: string};

/**
 * AUDIT×GOAL_REVIEW 确定性 Critic：只需 Control HTTP + worker JWT（无 Cordis checkout）。
 */
export function assessGoalReviewControlEnv(
  env: NodeJS.ProcessEnv = process.env,
): GoalReviewControlReadiness {
  const controlUrl = (
    env.RING_RUNNER_CONTROL_URL ||
    env.RING_CONTROL_URL ||
    ''
  ).trim();
  const workerJwt = resolveWorkerJwt(env);
  if (!controlUrl) {
    return {
      ok: false,
      reason: '缺少 RING_RUNNER_CONTROL_URL 或 RING_CONTROL_URL',
    };
  }
  if (!workerJwt) {
    return {
      ok: false,
      reason: '缺少 RING_RUNNER_WORKER_JWT 或 RING_WORKER_JWT',
    };
  }
  return {ok: true, controlUrl, workerJwt};
}

function cordisBuiltAt(checkout: string): boolean {
  return existsSync(cordisEntryPath(checkout));
}

function idsFrom(input: RunActivationInput): {
  activity_id: string;
  attempt_id?: string;
  fencing_epoch?: string;
} {
  return {
    activity_id: input.activity_id,
    attempt_id: input.attempt_id,
    fencing_epoch: input.fencing_epoch,
  };
}

/** TEMPORAL admit 要求完整 lease；缺则无法跳过扫 claim。 */
export function admittedLeaseFromInput(
  input: RunActivationInput,
): LeaseIdentity | null {
  const attempt = (input.attempt_id || '').trim();
  const epoch = (input.fencing_epoch || '').trim();
  if (!attempt || !epoch) {
    return null;
  }
  return {
    activity_id: input.activity_id,
    attempt_id: attempt,
    fencing_epoch: epoch,
  };
}

async function defaultCreateCordisPorts(
  input: RunActivationInput,
  env: PlanHarnessEnv,
  fetchImpl?: typeof fetch,
): Promise<CordisPlanBridgePorts> {
  const lease = admittedLeaseFromInput(input);
  if (!lease) {
    throw new Error(
      'TEMPORAL admit 需要 attempt_id 与 fencing_epoch；拒绝扫 claim 回退',
    );
  }
  if (input.kind === 'EXECUTE') {
    // EXECUTE：无 PlanCreate；modelInvocation 携带 exposed_tools
    const {createControlHttpCordisPorts} = await import(
      '../harness/controlHttpPorts.js'
    );
    const {createHeartbeatLinkedAdmissionGate} = await import(
      '../harness/brokerToolHttpPorts.js'
    );
    return createControlHttpCordisPorts({
      baseUrl: env.controlUrl,
      authorization: env.workerJwt,
      admittedLease: lease,
      fetchImpl,
      liveDispatch: env.liveDispatch,
      toolAdmissionGate: createHeartbeatLinkedAdmissionGate(),
      buildPlan: () => {
        throw new Error('EXECUTE_PATH_NO_PLAN');
      },
    });
  }
  return createControlHttpCordisPortsWithGoalPlan({
    baseUrl: env.controlUrl,
    authorization: env.workerJwt,
    admittedLease: lease,
    fetchImpl,
    liveDispatch: env.liveDispatch,
  });
}

async function defaultCreateExecuteHost(
  env: PlanHarnessEnv,
  fetchImpl?: typeof fetch,
): Promise<ExecuteToolHost> {
  const {createExecuteToolHost} = await import('../harness/executeToolHost.js');
  return createExecuteToolHost({
    baseUrl: env.controlUrl,
    authorization: env.workerJwt,
    fetchImpl,
  });
}

async function runPlanViaCordisPorts(
  input: RunActivationInput,
  ports: CordisPlanBridgePorts,
  checkout: string,
  modelId: string,
  planEnv: PlanHarnessEnv,
  fetchImpl?: typeof fetch,
): Promise<PlanActivationOutcome> {
  // 已 admit：经 ports.claimPlan（HTTP=GET activity）取快照，再 WithLease 跳过二次 claim。
  const claimed: ActivityLeaseView | null = await ports.claimPlan();
  if (!claimed) {
    return {
      status: 'IDLE',
      pending_harness: false,
      kind: 'PLAN',
      reason: 'PLAN activity 不可读（GET 404 / idle）',
      ...idsFrom(input),
    };
  }
  // PLAN 零工具：默认挂 Watchdog（LLM 阶段钟）；无工具面故不挂 progressGuard
  const planPorts =
    ports.watchdog || !ports.toolAdmissionGate
      ? ports
      : {
          ...ports,
          watchdog: createDefaultActivationGuards({
            allowed: () => true,
            closedReason: () => null,
            onHeartbeatFailure: (err) =>
              ports.toolAdmissionGate?.onHeartbeatFailure(err),
          }).watchdog,
        };
  try {
    await runZeroToolPlanTurnViaCordisWithLease(
      planPorts,
      claimed,
      checkout,
      modelId,
    );
  } catch (err) {
    if (!isWatchdogHardIdleError(err)) {
      throw err;
    }
    let partialId: string | undefined;
    try {
      const artifacts = createHttpArtifactPutPorts({
        baseUrl: planEnv.controlUrl,
        authorization: planEnv.workerJwt,
        fetchImpl,
      });
      const persisted = await persistWatchdogHardIdlePartial({
        artifacts,
        projectId: claimed.activity.project_id,
        lease: claimed.lease,
        error: err,
      });
      partialId = persisted?.artifactId;
    } catch {
      /* 落盘失败仍 FAILED ≠ DONE */
    }
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'PLAN',
      reason: formatWatchdogHardIdleFailReason(err, partialId),
      ...idsFrom(input),
    };
  }
  return {
    status: 'ACTIVATION_SUBMITTED',
    pending_harness: false,
    kind: 'PLAN',
    ...idsFrom(input),
  };
}

async function runExecuteViaCordisPorts(
  input: RunActivationInput,
  ports: CordisPlanBridgePorts,
  host: ExecuteToolHost,
  checkout: string,
  modelId: string,
  planEnv: PlanHarnessEnv,
  processEnv: NodeJS.ProcessEnv = process.env,
  fetchImpl?: typeof fetch,
): Promise<ExecuteActivationOutcome> {
  const claimed: ActivityLeaseView | null = await ports.claimPlan();
  if (!claimed) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'EXECUTE',
      reason: 'EXECUTE activity 不可读（GET 404 / idle）',
      ...idsFrom(input),
    };
  }
  if (claimed.activity.kind !== 'EXECUTE') {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'EXECUTE',
      reason: `UNEXPECTED_ACTIVITY_KIND:${claimed.activity.kind}`,
      ...idsFrom(input),
    };
  }
  const guards = await createDefaultActivationGuardsHydrated(host.gate, {
    goalId: input.goal_id,
    kernel: {
      baseUrl: planEnv.controlUrl,
      authorization: planEnv.workerJwt,
      fetchImpl,
    },
  });
  const turnPorts: CordisExecuteTurnPorts = {
    compileContext: ports.compileContext.bind(ports),
    bindContext: ports.bindContext.bind(ports),
    heartbeat: ports.heartbeat?.bind(ports),
    modelInvocation: ports.modelInvocation,
    toolAdmissionGate: ports.toolAdmissionGate ?? host.gate,
    stopSeam: ports.stopSeam,
    watchdog: guards.watchdog,
    progressGuard: guards.progressGuard,
    liveExecutePrompt: (digest) => `EXECUTE context_digest=${digest}`,
  };

  // live + chat 凭据齐备时：观察 effect 终态并用 Harness chat 做 ToolResult 第二轮
  let toolResultRound:
    | {
        chat: {baseUrl: string; apiKey: string; modelId: string};
        userPrompt: string;
        observe: {maxAttempts: number; delayMs: number};
      }
    | undefined;
  if (planEnv.liveDispatch) {
    const chatBase = (processEnv.RING_LOCAL_QWEN_BASE || '').trim();
    const chatKey = (processEnv.RING_LOCAL_QWEN_API_KEY || '').trim();
    if (chatBase && chatKey) {
      toolResultRound = {
        chat: {baseUrl: chatBase, apiKey: chatKey, modelId},
        userPrompt: `EXECUTE goal=${input.goal_id}`,
        observe: resolveToolResultObserveFromEnv(processEnv),
      };
    }
  }

  try {
    const turn = await runExecuteToolTurnViaCordisWithLease(
      turnPorts,
      host,
      claimed,
      checkout,
      modelId,
      toolResultRound ? {toolResultRound} : undefined,
    );
    if (turn.effects.length === 0) {
      // 无 tool-call ≠ 假推进；禁止 ACTIVATION_SUBMITTED + 空 effect_ids
      return {
        status: 'FAILED',
        pending_harness: false,
        kind: 'EXECUTE',
        reason: 'NO_TOOL_PROPOSAL',
        ...idsFrom(input),
      };
    }
    return {
      status: 'ACTIVATION_SUBMITTED',
      pending_harness: false,
      kind: 'EXECUTE',
      effect_ids: turn.effects.map((e) => e.effectId),
      tool_result_round: turn.toolResultRound
        ? {
            round2_cites_tool_result: turn.toolResultRound.round2CitesToolResult,
            requires_reconciliation: turn.toolResultRound.requiresReconciliation,
            marks_goal_done: false,
          }
        : undefined,
      ...idsFrom(input),
    };
  } catch (err) {
    // AB06：硬 ForceStop → 零工具收口（有 chat 凭据则真调；否则兜底正文）；≠ DONE
    const closeoutChat: {
      baseUrl: string;
      apiKey: string;
      modelId: string;
      fetchImpl?: typeof fetch;
    } =
      toolResultRound?.chat ??
      {
        baseUrl: 'http://127.0.0.1:9',
        apiKey: 'unavailable',
        modelId: 'force-stop-closeout-fallback',
        fetchImpl: (async () => {
          throw new Error('FORCE_STOP_CLOSEOUT_CHAT_UNAVAILABLE');
        }) as typeof fetch,
      };
    try {
      const handled = await handleCordisHardForceStopCloseout({
        error: err,
        chat: closeoutChat,
        getSummaryContent: (id) => host.artifacts.getArtifactContent(id),
        putCloseout: async (body) => {
          const put = await host.artifacts.putCollectorContent({
            projectId: claimed.activity.project_id,
            lease: claimed.lease,
            body,
            mime: 'application/json',
          });
          return {artifactId: put.artifactId};
        },
      });
      if (handled) {
        const dispatchedIds = isCordisHardForceStopError(err)
          ? err.effects.map((e) => e.effectId)
          : [];
        // M3.5：把 NO_PROGRESS_STOP 写入 Kernel（持有者身份）；失败不吞 ForceStop FAILED
        let terminationId: string | undefined;
        try {
          const reported = await reportActivationTermination({
            baseUrl: planEnv.controlUrl,
            authorization: planEnv.workerJwt,
            activityId: claimed.activity.id,
            lease: {
              activity_id: claimed.lease.activity_id,
              attempt_id: claimed.lease.attempt_id,
              fencing_epoch: claimed.lease.fencing_epoch,
            },
            reason: 'NO_PROGRESS_STOP',
            detail: handled.reason.slice(0, 2000),
            summaryArtifactId: handled.closeout.summaryArtifactId,
            closeoutArtifactId: handled.closeout.closeoutArtifactId,
          });
          terminationId = reported.id;
        } catch {
          /* 登记失败仍 FAILED ≠ DONE */
        }
        return {
          status: 'FAILED',
          pending_harness: false,
          kind: 'EXECUTE',
          reason: handled.reason,
          marks_goal_done: false,
          ...(dispatchedIds.length > 0 ? {effect_ids: dispatchedIds} : {}),
          force_stop_closeout: {
            assistant_text: handled.closeout.assistantText,
            summary_artifact_id: handled.closeout.summaryArtifactId,
            ...(handled.closeout.closeoutArtifactId
              ? {closeout_artifact_id: handled.closeout.closeoutArtifactId}
              : {}),
            used_fallback: handled.closeout.usedFallback,
            marks_goal_done: false,
          },
          ...(terminationId
            ? {activation_termination_id: terminationId}
            : {}),
          ...idsFrom(input),
        };
      }
    } catch {
      /* 收口失败不吞原错；落入 Hard Idle / rethrow */
    }
    if (!isWatchdogHardIdleError(err)) {
      throw err;
    }
    let partialId: string | undefined;
    try {
      const persisted = await persistWatchdogHardIdlePartial({
        artifacts: host.artifacts,
        projectId: claimed.activity.project_id,
        lease: claimed.lease,
        error: err,
      });
      partialId = persisted?.artifactId;
    } catch {
      /* 落盘失败仍 FAILED ≠ DONE */
    }
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'EXECUTE',
      reason: formatWatchdogHardIdleFailReason(err, partialId),
      marks_goal_done: false,
      ...(partialId ? {partial_artifact_id: partialId} : {}),
      ...idsFrom(input),
    };
  }
}

function executeOutcomeToResult(
  outcome: ExecuteActivationOutcome,
): RunActivationResult {
  if (outcome.status === 'ACTIVATION_SUBMITTED' && !outcome.pending_harness) {
    const effectIds = outcome.effect_ids ?? [];
    if (effectIds.length === 0) {
      return {
        status: 'FAILED',
        pending_harness: false,
        kind: 'EXECUTE',
        reason: 'NO_TOOL_PROPOSAL',
        activity_id: outcome.activity_id,
        attempt_id: outcome.attempt_id,
        fencing_epoch: outcome.fencing_epoch,
      };
    }
    return {
      status: 'ACTIVATION_SUBMITTED',
      pending_harness: false,
      kind: 'EXECUTE',
      activity_id: outcome.activity_id,
      attempt_id: outcome.attempt_id,
      fencing_epoch: outcome.fencing_epoch,
      effect_ids: effectIds,
      ...(outcome.tool_names ? {tool_names: outcome.tool_names} : {}),
      ...(outcome.driver ? {driver: outcome.driver} : {}),
      ...(typeof outcome.scripted_order === 'boolean'
        ? {scripted_order: outcome.scripted_order}
        : {}),
      ...(typeof outcome.chat_driven_order === 'boolean'
        ? {chat_driven_order: outcome.chat_driven_order}
        : {}),
      marks_goal_done: false,
    };
  }
  return {
    status: 'FAILED',
    pending_harness: false,
    kind: 'EXECUTE',
    reason: outcome.reason ?? 'EXECUTE activation failed',
    activity_id: outcome.activity_id,
    attempt_id: outcome.attempt_id,
    fencing_epoch: outcome.fencing_epoch,
    marks_goal_done: false,
    ...(outcome.partial_artifact_id
      ? {partial_artifact_id: outcome.partial_artifact_id}
      : {}),
    ...(outcome.force_stop_closeout?.closeout_artifact_id
      ? {
          force_stop_closeout_artifact_id:
            outcome.force_stop_closeout.closeout_artifact_id,
        }
      : {}),
    ...(outcome.force_stop_closeout?.summary_artifact_id
      ? {
          force_stop_summary_artifact_id:
            outcome.force_stop_closeout.summary_artifact_id,
        }
      : {}),
    ...(outcome.effect_ids && outcome.effect_ids.length > 0
      ? {effect_ids: outcome.effect_ids}
      : {}),
    ...(outcome.activation_termination_id
      ? {activation_termination_id: outcome.activation_termination_id}
      : {}),
  };
}

/**
 * 把执行腿的**成功**结果作为活动回执提交给 Kernel，并把它并进返回值。
 *
 * 为什么必须有这一步：Kernel 的活动行只有收到 outcomes 才离开 `RUNNING`；缺了它，
 * Temporal 侧已 COMPLETED、Kernel 侧永停 RUNNING，编排工作流的观察循环永远等不到变化。
 * 详见 `harness/executeOutcomeSubmit.ts` 头部。
 *
 * **非致命**：提交不成立时只把原因并进结果（`execute_outcome_submit`），
 * 不改 ACTIVATION_SUBMITTED 本身 —— 执行确实完成了，不因回执失败而谎报失败；
 * 同时也不掩盖，便于从活动结果直接看出未提交的原因。
 * 恒不写 `marks_goal_done`（DONE 只归 Kernel）。
 */
async function submitExecuteOutcomeAndMerge(
  outcome: ExecuteActivationOutcome,
  lease: {activityId: string; attemptId?: string; fencingEpoch?: string},
  planEnv: PlanHarnessEnv,
  fetchImpl: typeof fetch | undefined,
): Promise<RunActivationResult> {
  const result = executeOutcomeToResult(outcome);
  // 只对「EXECUTE 且已提交」的结果补回执；其余（FAILED / PENDING_ENV / 非 EXECUTE）原样返回。
  // 显式收窄：RunActivationResult 是含 PLAN/AUDIT 变体的联合类型，只有 EXECUTE 变体有 effect_ids。
  if (result.kind !== 'EXECUTE' || result.status !== 'ACTIVATION_SUBMITTED') {
    return result;
  }
  const effectIds = result.effect_ids ?? [];
  if (effectIds.length === 0) return result;
  // Kernel 回执须带 attempt 持有者身份；缺身份则不提交（如实报因，不冒充成功）
  if (!lease.attemptId || !lease.fencingEpoch) {
    return {
      ...result,
      execute_outcome_submit: {submitted: false, reason: 'NO_LEASE_IDENTITY'},
    };
  }

  // 端口自带鉴权与工件读取：**不借用** artifactPutHttpPorts（其 Authorization 不补
  // `Bearer `，取数会 401 并被静默跳过 → 表现为「候选反查失败」，实测踩过）
  const deps = createHttpExecuteOutcomeDeps({
    baseUrl: planEnv.controlUrl,
    authorization: planEnv.workerJwt,
    fetchImpl,
  });
  const submitted: ExecuteOutcomeSubmitResult = await submitExecuteOutcome({
    deps,
    activityId: lease.activityId,
    attemptId: lease.attemptId,
    fencingEpoch: lease.fencingEpoch,
    effectIds,
  });
  return {...result, execute_outcome_submit: submitted};
}

function outcomeToResult(
  outcome: PlanActivationOutcome,
): RunActivationResult {
  if (outcome.status === 'ACTIVATION_SUBMITTED') {
    return {
      status: 'ACTIVATION_SUBMITTED',
      pending_harness: false,
      kind: 'PLAN',
      activity_id: outcome.activity_id,
      attempt_id: outcome.attempt_id,
      fencing_epoch: outcome.fencing_epoch,
    };
  }
  if (outcome.status === 'IDLE') {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'PLAN',
      reason: outcome.reason ?? 'PLAN claim idle',
      activity_id: outcome.activity_id,
      attempt_id: outcome.attempt_id,
      fencing_epoch: outcome.fencing_epoch,
    };
  }
  return {
    status: 'FAILED',
    pending_harness: false,
    kind: 'PLAN',
    reason: outcome.reason ?? 'PLAN activation failed',
    activity_id: outcome.activity_id,
    attempt_id: outcome.attempt_id,
    fencing_epoch: outcome.fencing_epoch,
  };
}

function auditOutcomeToResult(outcome: GoalReviewCriticResult): RunActivationResult {
  if (outcome.status === 'ACTIVATION_SUBMITTED') {
    return {
      status: 'ACTIVATION_SUBMITTED',
      pending_harness: false,
      kind: 'AUDIT',
      activity_id: outcome.activity_id,
      attempt_id: outcome.attempt_id,
      fencing_epoch: outcome.fencing_epoch,
      finding_codes: outcome.finding_codes,
      marks_goal_done: false,
    };
  }
  return {
    status: 'FAILED',
    pending_harness: false,
    kind: 'AUDIT',
    reason: outcome.reason ?? 'AUDIT activation failed',
    activity_id: outcome.activity_id || '',
    attempt_id: outcome.attempt_id,
    fencing_epoch: outcome.fencing_epoch,
  };
}

function candidateAuditOutcomeToResult(
  outcome: CandidateAuditResult,
): RunActivationResult {
  if (outcome.status === 'ACTIVATION_SUBMITTED') {
    return {
      status: 'ACTIVATION_SUBMITTED',
      pending_harness: false,
      kind: 'AUDIT',
      activity_id: outcome.activity_id,
      attempt_id: outcome.attempt_id,
      fencing_epoch: outcome.fencing_epoch,
      verdict: outcome.verdict,
      verifier_run_id: outcome.verifier_run_id,
      marks_goal_done: false,
    };
  }
  return {
    status: 'FAILED',
    pending_harness: false,
    kind: 'AUDIT',
    reason: outcome.reason ?? 'CANDIDATE AUDIT activation failed',
    activity_id: outcome.activity_id || '',
    attempt_id: outcome.attempt_id,
    fencing_epoch: outcome.fencing_epoch,
  };
}

function finalizeOutcomeToResult(
  outcome: FinalizeAuditorResult,
): RunActivationResult {
  if (outcome.status === 'ACTIVATION_SUBMITTED') {
    return {
      status: 'ACTIVATION_SUBMITTED',
      pending_harness: false,
      kind: 'FINALIZE',
      activity_id: outcome.activity_id,
      attempt_id: outcome.attempt_id,
      fencing_epoch: outcome.fencing_epoch,
      verdict: outcome.verdict,
      verifier_run_ids: outcome.verifier_run_ids,
      marks_goal_done: false,
    };
  }
  return {
    status: 'FAILED',
    pending_harness: false,
    kind: 'FINALIZE',
    reason: outcome.reason ?? 'FINALIZE activation failed',
    activity_id: outcome.activity_id || '',
    attempt_id: outcome.attempt_id,
    fencing_epoch: outcome.fencing_epoch,
  };
}

function integrateOutcomeToResult(outcome: IntegrateResult): RunActivationResult {
  if (outcome.status === 'ACTIVATION_SUBMITTED') {
    return {
      status: 'ACTIVATION_SUBMITTED',
      pending_harness: false,
      kind: 'INTEGRATE',
      activity_id: outcome.activity_id,
      attempt_id: outcome.attempt_id,
      fencing_epoch: outcome.fencing_epoch,
      candidate_manifest_id: outcome.candidate_manifest_id,
      marks_goal_done: false,
    };
  }
  return {
    status: 'FAILED',
    pending_harness: false,
    kind: 'INTEGRATE',
    reason: outcome.reason ?? 'INTEGRATE activation failed',
    activity_id: outcome.activity_id || '',
    attempt_id: outcome.attempt_id,
    fencing_epoch: outcome.fencing_epoch,
  };
}

/**
 * PLAN：零工具；经 Kernel 端口提交 outcome，不伪造 PlanCreate。
 * EXECUTE：环境齐备时默认装配 Control HTTP ports + Broker 宿主（kind-aware tools 传输）；
 * 可 DI runExecute / createCordisPorts+createExecuteHost。
 * 空 effect_ids → FAILED NO_TOOL_PROPOSAL。
 * AUDIT×GOAL_REVIEW：确定性 Critic → Kernel outcome；≠ DONE。
 * AUDIT×CANDIDATE：默认 Broker auditor observe → VerificationRun → outcome；≠ DONE。
 * 其他 kind / 未知 AUDIT target：诚实 NOT_IMPLEMENTED。
 */
export async function runActivation(
  input: RunActivationInput,
  deps: RunActivationDeps = {},
): Promise<RunActivationResult> {
  if (!input?.activity_id || !input?.goal_id || !input?.kind || !input?.owner_epoch) {
    throw new Error('RunActivation 需要 activity_id、goal_id、kind、owner_epoch');
  }

  if (input.kind === 'AUDIT') {
    if (deps.runAudit) {
      return auditOutcomeToResult(await deps.runAudit(input));
    }
    if (deps.runCandidateAudit) {
      return candidateAuditOutcomeToResult(await deps.runCandidateAudit(input));
    }

    const env = deps.env ?? process.env;
    const readiness = assessGoalReviewControlEnv(env);
    if (!readiness.ok) {
      return {
        status: 'PENDING_ENV',
        reason: readiness.reason,
        kind: 'AUDIT',
        pending_harness: true,
      };
    }
    if (!admittedLeaseFromInput(input)) {
      return {
        status: 'PENDING_ENV',
        reason:
          'TEMPORAL admit 需要 attempt_id 与 fencing_epoch；拒绝无租约扫 claim / LEGACY 回退',
        kind: 'AUDIT',
        pending_harness: true,
      };
    }

    const lease = admittedLeaseFromInput(input)!;
    const ports = createControlHttpGoalReviewPorts({
      baseUrl: readiness.controlUrl,
      authorization: readiness.workerJwt,
      admittedLease: lease,
      fetchImpl: deps.fetchImpl,
    });
    const claimed = await ports.claimAudit();
    if (!claimed) {
      return {
        status: 'FAILED',
        pending_harness: false,
        kind: 'AUDIT',
        reason: 'AUDIT activity 不可读（GET 404 / idle）',
        activity_id: input.activity_id,
        attempt_id: input.attempt_id,
        fencing_epoch: input.fencing_epoch,
      };
    }
    if (claimed.activity.target?.type === 'GOAL_REVIEW') {
      return auditOutcomeToResult(
        await runGoalReviewCriticTurnWithLease(ports, claimed),
      );
    }
    if (claimed.activity.target?.type === 'CANDIDATE') {
      // 默认装配 Broker auditor 观察；禁止无观察 stub PASS。
      const host = createExecuteToolHost({
        baseUrl: readiness.controlUrl,
        authorization: readiness.workerJwt,
        fetchImpl: deps.fetchImpl,
      });
      const ports = createControlHttpCandidateAuditPorts({
        baseUrl: readiness.controlUrl,
        authorization: readiness.workerJwt,
        admittedLease: lease,
        fetchImpl: deps.fetchImpl,
          observeCandidate: (view) =>
          observeCandidateViaBrokerAuditorSuite(
            {
              baseUrl: readiness.controlUrl,
              authorization: readiness.workerJwt,
              fetchImpl: deps.fetchImpl,
              host,
            },
            view,
          ),
      });
      try {
        return candidateAuditOutcomeToResult(
          await runCandidateAuditTurnWithLease(ports, claimed),
        );
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return {
          status: 'FAILED',
          pending_harness: false,
          kind: 'AUDIT',
          reason: `CANDIDATE_AUDIT_BROKER_FAILED:${msg}`,
          activity_id: input.activity_id,
          attempt_id: input.attempt_id,
          fencing_epoch: input.fencing_epoch,
        };
      }
    }
    return {
      status: 'NOT_IMPLEMENTED',
      reason: `Harness RunActivation AUDIT target=${claimed.activity.target?.type ?? 'missing'} 未接线`,
      kind: 'AUDIT',
      pending_harness: true,
    };
  }

  if (input.kind === 'EXECUTE') {
    if (deps.runExecute) {
      return executeOutcomeToResult(await deps.runExecute(input));
    }

    const env = deps.env ?? process.env;
    const readiness = assessPlanHarnessEnv(env);
    if (!readiness.ok) {
      return {
        status: 'PENDING_ENV',
        reason: readiness.reason,
        kind: 'EXECUTE',
        pending_harness: true,
      };
    }
    if (!cordisBuiltAt(readiness.checkout)) {
      return {
        status: 'PENDING_ENV',
        reason:
          'RING_HARNESS_CHECKOUT 已配置但 Cordis 未构建（缺 vendor/cordis/lib）；保持 pending_harness',
        kind: 'EXECUTE',
        pending_harness: true,
      };
    }
    if (!admittedLeaseFromInput(input)) {
      return {
        status: 'PENDING_ENV',
        reason:
          'TEMPORAL admit 需要 attempt_id 与 fencing_epoch；拒绝无租约扫 claim / LEGACY 回退',
        kind: 'EXECUTE',
        pending_harness: true,
      };
    }

    let executeRuntime;
    try {
      executeRuntime = resolveExecuteRuntime(env);
    } catch (err) {
      return {
        status: 'PENDING_ENV',
        reason: err instanceof Error ? err.message : String(err),
        kind: 'EXECUTE',
        pending_harness: true,
      };
    }

    const planEnv: PlanHarnessEnv = {
      checkout: readiness.checkout,
      controlUrl: readiness.controlUrl,
      workerJwt: readiness.workerJwt,
      liveDispatch: readiness.liveDispatch,
      modelId: readiness.modelId,
    };

    if (executeRuntime !== 'default') {
      const official = assessOfficialExecuteRuntime(readiness.checkout, env);
      if (!official.ok) {
        return {
          status: 'PENDING_ENV',
          reason: official.reason,
          kind: 'EXECUTE',
          pending_harness: true,
        };
      }
      const ports = deps.createCordisPorts
        ? await deps.createCordisPorts(input, planEnv)
        : await defaultCreateCordisPorts(input, planEnv, deps.fetchImpl);
      const claimed = await ports.claimPlan();
      if (!claimed) {
        return {
          status: 'FAILED',
          pending_harness: false,
          kind: 'EXECUTE',
          reason: 'EXECUTE activity 不可读（GET 404 / idle）',
          activity_id: input.activity_id,
          attempt_id: input.attempt_id,
          fencing_epoch: input.fencing_epoch,
        };
      }
      const outcome = await runOfficialExecuteTurn({
        claimed,
        checkout: official.checkout,
        chat: {
          ...official.chat,
          // 与 Control 共用注入 fetch（测按 URL 分流）；真网可不设
          fetchImpl: deps.fetchImpl ?? official.chat.fetchImpl,
        },
        controlUrl: planEnv.controlUrl,
        workerJwt: planEnv.workerJwt,
        fetchImpl: deps.fetchImpl,
        env,
        // 不预填；diagnose 优先 env USER_PROMPT，否则从 Goal 合同装配
      });
      // 提交活动回执：缺此步 Kernel 侧永停 RUNNING（见 executeOutcomeSubmit.ts 头部）
      return submitExecuteOutcomeAndMerge(
        outcome,
        {
          activityId: claimed.activity.id,
          attemptId: claimed.lease.attempt_id,
          fencingEpoch: claimed.lease.fencing_epoch,
        },
        planEnv,
        deps.fetchImpl,
      );
    }

    const host = deps.createExecuteHost
      ? await deps.createExecuteHost(input, planEnv)
      : await defaultCreateExecuteHost(planEnv, deps.fetchImpl);
    const ports = deps.createCordisPorts
      ? await deps.createCordisPorts(input, planEnv)
      : await defaultCreateCordisPorts(input, planEnv, deps.fetchImpl);
    const outcome = await runExecuteViaCordisPorts(
      input,
      ports,
      host,
      readiness.checkout,
      readiness.modelId,
      planEnv,
      env,
      deps.fetchImpl,
    );
    // Cordis 通路同样必须提交回执；租约取自执行结果本身（无 claimed 对象）
    return submitExecuteOutcomeAndMerge(
      outcome,
      {
        activityId: outcome.activity_id,
        attemptId: outcome.attempt_id ?? input.attempt_id,
        fencingEpoch: outcome.fencing_epoch ?? input.fencing_epoch,
      },
      planEnv,
      deps.fetchImpl,
    );
  }

  if (input.kind === 'FINALIZE') {
    if (deps.runFinalize) {
      return finalizeOutcomeToResult(await deps.runFinalize(input));
    }

    const env = deps.env ?? process.env;
    const readiness = assessGoalReviewControlEnv(env);
    if (!readiness.ok) {
      return {
        status: 'PENDING_ENV',
        reason: readiness.reason,
        kind: 'FINALIZE',
        pending_harness: true,
      };
    }
    if (!admittedLeaseFromInput(input)) {
      return {
        status: 'PENDING_ENV',
        reason:
          'TEMPORAL admit 需要 attempt_id 与 fencing_epoch；拒绝无租约扫 claim / LEGACY 回退',
        kind: 'FINALIZE',
        pending_harness: true,
      };
    }

    const lease = admittedLeaseFromInput(input)!;
    const host = createExecuteToolHost({
      baseUrl: readiness.controlUrl,
      authorization: readiness.workerJwt,
      fetchImpl: deps.fetchImpl,
    });
    const ports = createControlHttpFinalizePorts({
      baseUrl: readiness.controlUrl,
      authorization: readiness.workerJwt,
      admittedLease: lease,
      fetchImpl: deps.fetchImpl,
      observeFinalize: (view) =>
        observeCandidateViaBrokerAuditorSuite(
          {
            baseUrl: readiness.controlUrl,
            authorization: readiness.workerJwt,
            fetchImpl: deps.fetchImpl,
            host,
          },
          view,
        ),
    });
    const claimed = await ports.claimFinalize();
    if (!claimed) {
      return {
        status: 'FAILED',
        pending_harness: false,
        kind: 'FINALIZE',
        reason: 'FINALIZE activity 不可读（GET 404 / idle）',
        activity_id: input.activity_id,
        attempt_id: input.attempt_id,
        fencing_epoch: input.fencing_epoch,
      };
    }
    try {
      return finalizeOutcomeToResult(
        await runFinalizeTurnWithLease(ports, claimed),
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      return {
        status: 'FAILED',
        pending_harness: false,
        kind: 'FINALIZE',
        reason: `FINALIZE_BROKER_FAILED:${msg}`,
        activity_id: input.activity_id,
        attempt_id: input.attempt_id,
        fencing_epoch: input.fencing_epoch,
      };
    }
  }

  if (input.kind === 'INTEGRATE') {
    if (deps.runIntegrate) {
      return integrateOutcomeToResult(await deps.runIntegrate(input));
    }

    const env = deps.env ?? process.env;
    const readiness = assessGoalReviewControlEnv(env);
    if (!readiness.ok) {
      return {
        status: 'PENDING_ENV',
        reason: readiness.reason,
        kind: 'INTEGRATE',
        pending_harness: true,
      };
    }
    if (!admittedLeaseFromInput(input)) {
      return {
        status: 'PENDING_ENV',
        reason:
          'TEMPORAL admit 需要 attempt_id 与 fencing_epoch；拒绝无租约扫 claim / LEGACY 回退',
        kind: 'INTEGRATE',
        pending_harness: true,
      };
    }

    const lease = admittedLeaseFromInput(input)!;
    const ports = createControlHttpIntegratePorts({
      baseUrl: readiness.controlUrl,
      authorization: readiness.workerJwt,
      admittedLease: lease,
      fetchImpl: deps.fetchImpl,
    });
    const claimed = await ports.claimIntegrate();
    if (!claimed) {
      return {
        status: 'FAILED',
        pending_harness: false,
        kind: 'INTEGRATE',
        reason: 'INTEGRATE activity 不可读（GET 404 / idle）',
        activity_id: input.activity_id,
        attempt_id: input.attempt_id,
        fencing_epoch: input.fencing_epoch,
      };
    }
    try {
      return integrateOutcomeToResult(
        await runIntegrateTurnWithLease(ports, claimed),
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      return {
        status: 'FAILED',
        pending_harness: false,
        kind: 'INTEGRATE',
        reason: `INTEGRATE_FAILED:${msg}`,
        activity_id: input.activity_id,
        attempt_id: input.attempt_id,
        fencing_epoch: input.fencing_epoch,
      };
    }
  }

  if (input.kind !== 'PLAN') {
    return {
      status: 'NOT_IMPLEMENTED',
      reason: `Harness RunActivation kind=${input.kind} 未接线`,
      kind: input.kind,
      pending_harness: true,
    };
  }

  if (deps.runPlan) {
    const outcome = await deps.runPlan(input);
    return outcomeToResult(outcome);
  }

  const env = deps.env ?? process.env;
  const readiness = assessPlanHarnessEnv(env);
  if (!readiness.ok) {
    return {
      status: 'PENDING_ENV',
      reason: readiness.reason,
      kind: 'PLAN',
      pending_harness: true,
    };
  }

  if (!cordisBuiltAt(readiness.checkout)) {
    return {
      status: 'PENDING_ENV',
      reason:
        'RING_HARNESS_CHECKOUT 已配置但 Cordis 未构建（缺 vendor/cordis/lib）；保持 pending_harness',
      kind: 'PLAN',
      pending_harness: true,
    };
  }

  if (!admittedLeaseFromInput(input)) {
    return {
      status: 'PENDING_ENV',
      reason:
        'TEMPORAL admit 需要 attempt_id 与 fencing_epoch；拒绝无租约扫 claim / LEGACY 回退',
      kind: 'PLAN',
      pending_harness: true,
    };
  }

  const planEnv: PlanHarnessEnv = {
    checkout: readiness.checkout,
    controlUrl: readiness.controlUrl,
    workerJwt: readiness.workerJwt,
    liveDispatch: readiness.liveDispatch,
    modelId: readiness.modelId,
  };
  const ports = deps.createCordisPorts
    ? await deps.createCordisPorts(input, planEnv)
    : await defaultCreateCordisPorts(input, planEnv, deps.fetchImpl);
  const outcome = await runPlanViaCordisPorts(
    input,
    ports,
    readiness.checkout,
    readiness.modelId,
    planEnv,
    deps.fetchImpl,
  );
  return outcomeToResult(outcome);
}

/** Worker 注册面：键名即 Temporal Activity 名。 */
export const runActivationActivities = {
  [RUN_ACTIVATION_ACTIVITY_NAME]: runActivation,
} as const;
