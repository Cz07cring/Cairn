/**
 * 验证观察：经 Kernel Effect Gateway 派发 Broker `run_tests(suite=auditor)`。
 * 供 AUDIT×CANDIDATE 与 FINALIZE（GLOBAL）共用；禁止本地 stub PASS。≠ Goal DONE 自报。
 */
import {createHash} from 'node:crypto';
import {RUN_TESTS_SCHEMA_DIGEST} from './cordisExecuteBridge.js';
import {
  createExecuteToolHost,
  observeDispatchedEffect,
  type ExecuteToolHost,
} from './executeToolHost.js';
import {registerStepAndForwardToBroker} from './brokerToolBridge.js';
import type {ActivityLeaseView} from './fakePlanHost.js';
import type {CandidateObservation} from './candidateAuditor.js';
import {toolResultLooksGreen} from './sealGreenGate.js';

export type BrokerCandidateObserveConfig = {
  baseUrl: string;
  authorization: string;
  fetchImpl?: typeof fetch;
  /** 测试注入宿主；缺省 Control HTTP 装配 */
  host?: ExecuteToolHost;
  poll?: {maxAttempts?: number; delayMs?: number};
};

const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'UNKNOWN', 'CANCELLED']);
const VERIFY_KINDS = new Set(['AUDIT', 'FINALIZE']);

/** AUDIT/FINALIZE 专用：仅 suite=auditor（EXECUTE 仍只能 public）。 */
export function canonicalizeAuditorRunTestsPayload(): string {
  return JSON.stringify({
    tool_ref: 'run_tests',
    tool_schema_digest: RUN_TESTS_SCHEMA_DIGEST,
    parameters: {suite: 'auditor'},
  });
}

function digestOf(label: string): string {
  return 'sha256:' + createHash('sha256').update(label).digest('hex');
}

function parseExitCode(evidenceText: string): number | null {
  const jsonMatch = evidenceText.match(/"exit_code"\s*:\s*(-?\d+)/);
  if (jsonMatch) {
    return Number(jsonMatch[1]);
  }
  const lineMatch = evidenceText.match(/(?:^|\n)exit_code=(-?\d+)/);
  if (lineMatch) {
    return Number(lineMatch[1]);
  }
  return null;
}

/**
 * 从**验收分配**取校验器身份，不调外部 API。
 *
 * 为什么：Runner 只持 worker 身份，公共 `/api/v1/verification-profiles` 要求
 * viewer/operator（实测 403 VERIFIER_DIGEST_FETCH_FAILED）；而 Kernel 建审计/FINALIZE
 * 时本已读到该 profile 行，把 `config.verifier_digest` 随分配一并冻结，
 * 既满足最小权限，也让「用哪个校验器」成为不可漂移的契约一部分。
 * 缺字段时失败关闭（不猜一个 digest 冒充）。
 */
function verifierDigestFromAssignment(
  assignment: Record<string, unknown>,
): string {
  const digest = assignment.verifier_digest;
  if (typeof digest !== 'string' || !digest.startsWith('sha256:')) {
    throw new Error('VERIFIER_DIGEST_MISSING');
  }
  return digest;
}

/**
 * 已 claim 的 AUDIT/FINALIZE×CANDIDATE：prepare/dispatch auditor → 读证据 → 观察。
 */
export async function observeCandidateViaBrokerAuditorSuite(
  config: BrokerCandidateObserveConfig,
  claimed: ActivityLeaseView,
): Promise<CandidateObservation> {
  if (!VERIFY_KINDS.has(claimed.activity.kind)) {
    throw new Error(`UNEXPECTED_ACTIVITY_KIND:${claimed.activity.kind}`);
  }
  if (claimed.activity.target?.type !== 'CANDIDATE') {
    throw new Error(
      `UNSUPPORTED_VERIFY_TARGET:${claimed.activity.target?.type ?? 'missing'}`,
    );
  }

  const assignments = claimed.activity.verification_assignments;
  const assignment = Array.isArray(assignments) ? assignments[0] : null;
  if (!assignment || typeof assignment.verification_profile_id !== 'string') {
    throw new Error('MISSING_VERIFICATION_ASSIGNMENT');
  }
  const subjectDigest =
    typeof assignment.subject_digest === 'string'
      ? assignment.subject_digest
      : null;
  if (!subjectDigest?.startsWith('sha256:')) {
    throw new Error('MISSING_SUBJECT_DIGEST');
  }

  const host = config.host ?? createExecuteToolHost(config);
  if (!host.gate.allowed()) {
    throw new Error(
      `TOOL_ADMISSION_CLOSED:${host.gate.closedReason() ?? 'admission_closed'}`,
    );
  }

  const toolKind =
    claimed.activity.kind === 'FINALIZE' ? 'FINALIZE' : 'AUDIT';
  const payload = canonicalizeAuditorRunTestsPayload();
  const put = await host.artifacts.putCollectorContent({
    projectId: claimed.activity.project_id,
    lease: claimed.lease,
    body: payload,
    mime: 'application/json',
  });

  const forwarded = await registerStepAndForwardToBroker(
    host.ports,
    {
      kind: toolKind,
      tool: 'run_tests',
      purpose: `${toolKind.toLowerCase()}:run_tests:auditor`,
      inputArtifactId: put.artifactId,
      lease: claimed.lease,
      activityId: claimed.activity.id,
      predecessorStepId: null,
    },
    new Set(['run_tests']),
  );

  const effect = await observeDispatchedEffect(host, forwarded.effectId, {
    maxAttempts: config.poll?.maxAttempts ?? 60,
    delayMs: config.poll?.delayMs ?? 500,
  });

  if (!TERMINAL.has(effect.status)) {
    host.gate.onHeartbeatFailure(new Error('EFFECT_UNSETTLED'));
    throw new Error(
      `EFFECT_UNSETTLED:${effect.id}:${effect.status}（须对账，禁止 stub PASS）`,
    );
  }
  if (effect.status === 'UNKNOWN' || effect.status === 'CANCELLED') {
    host.gate.onHeartbeatFailure(new Error(`EFFECT_${effect.status}`));
    throw new Error(`EFFECT_${effect.status}:${effect.id}`);
  }

  const evidenceIds = effect.evidenceIds ?? [];
  if (evidenceIds.length === 0) {
    throw new Error(`EFFECT_RESULT_MISSING:${effect.id}`);
  }

  const evidenceTexts: string[] = [];
  for (const id of evidenceIds) {
    evidenceTexts.push(await host.artifacts.getArtifactContent(id));
  }
  const joined = evidenceTexts.join('\n');
  const exitCode = parseExitCode(joined);
  if (exitCode == null && effect.status === 'SUCCEEDED') {
    throw new Error(`EXIT_CODE_MISSING:${effect.id}`);
  }

  const checksPassed =
    effect.status === 'SUCCEEDED' &&
    (exitCode === 0 || toolResultLooksGreen(joined));

  // 校验器身份取自**分配**（Kernel 在创建活动时冻结），不再调公共
  // `/api/v1/verification-profiles`：该路由要求 viewer/operator，Runner 只持 worker
  // 身份，实测 403 VERIFIER_DIGEST_FETCH_FAILED。放宽公共读权限会扩大 worker 面；
  // 分配随活动下发才是最小权限写法（与 subject_digest 同源）。
  const verifierDigest = verifierDigestFromAssignment(assignment);

  const receiptArtifactId = evidenceIds[0]!;
  const evidenceArtifactId = evidenceIds[evidenceIds.length - 1]!;

  return {
    checksPassed,
    // AUDIT 任务准则常用 A1；FINALIZE GLOBAL 常用 Goal success_criteria（C1）
    criterionId: toolKind === 'FINALIZE' ? 'C1' : 'A1',
    reason: checksPassed
      ? `Broker auditor suite 绿（exit_code=${exitCode ?? 0}）`
      : `Broker auditor suite 红（status=${effect.status}, exit_code=${exitCode ?? 'n/a'}）`,
    evidenceArtifactId,
    receiptArtifactId,
    verifierDigest,
    subjectDigest,
    inputDigest: digestOf(`auditor-input:${put.digest}`),
    environmentDigest: digestOf(`auditor-env:${claimed.activity.id}`),
  };
}
