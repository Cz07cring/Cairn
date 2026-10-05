/**
 * EXECUTE 完成后的**活动回执提交**（Kernel 那一侧的唯一推进入口）。
 *
 * 背景（2026-09-15 实测，见 doc/engineering/跨活动推进缺口-调研结论-2026-09-15.md）：
 * Kernel 的活动行**只有**收到 `POST /internal/v1/activities/{id}/outcomes` 才会离开
 * `RUNNING`。Runner 侧此前没有任何执行回执的提交方 —— PLAN / AUDIT / FINALIZE /
 * INTEGRATE 都有，唯独 EXECUTE 没有。于是执行腿把活干完后：
 *   Temporal 侧 `RunActivation` 已 COMPLETED（6 个 effect 全成功），
 *   Kernel 侧同一活动行永停 `RUNNING`，
 *   编排工作流只能一轮轮读到同一个常量读数，直到观察窗口烧完。
 * 佐证：控制面全部 15 次 outcomes 请求都落在 PLAN 活动上，EXECUTE 为 0 次。
 *
 * 为什么要反查候选：Kernel 的 `submit_execute_outcome` 要求
 * `candidate_manifests.id = outcome.candidate_manifest_id 且 activity_id = 本活动`。
 * 而封印工具在 Broker 侧创建 candidate 后，会把它的 id 写进**证据工件**的内容
 * （`candidate_manifest_id` 字段），故可沿
 *   「封印 effect → 证据工件 → candidate_manifest_id」反查得到。
 *
 * 边界：本模块只提交回执，**不**判定 Goal/Task DONE；DONE 仍只归 Kernel。
 */
import {bearerAuthHeaders} from './authHeader.js';

/** 一次 EXECUTE 回执提交所需的全部 IO 端口（可注入，便于单测）。 */
export type ExecuteOutcomeDeps = {
  readActivity: (activityId: string) => Promise<{stateRevision: number; status: string}>;
  readEffect: (
    effectId: string,
  ) => Promise<{toolRef: string; status: string; evidenceIds: string[]}>;
  readArtifactContent: (artifactId: string) => Promise<string>;
  postOutcome: (input: {
    activityId: string;
    lease: {activity_id: string; attempt_id: string; fencing_epoch: string};
    expectedStateRevision: number;
    candidateManifestId: string;
    evidenceIds: string[];
  }) => Promise<void>;
};

export type ExecuteOutcomeSubmitResult =
  | {submitted: true; candidateManifestId: string; evidenceIds: string[]}
  | {submitted: false; reason: string};

type Envelope<T> = {data: T};

export type ExecuteOutcomeHttpConfig = {
  baseUrl: string;
  authorization: string;
  fetchImpl?: typeof fetch;
};

/** 控制面 HTTP 端口：读活动 / 读 effect / 读证据工件 / 提交回执。 */
export function createHttpExecuteOutcomeDeps(
  config: ExecuteOutcomeHttpConfig,
): ExecuteOutcomeDeps {
  const fetchFn = config.fetchImpl ?? fetch;
  const base = config.baseUrl.replace(/\/$/, '');
  // 形态对齐 `controlHttpPorts`：调用方传**裸 token**，统一经 authHeader 补 `Bearer `。
  const headers = bearerAuthHeaders(config.authorization, {
    'Content-Type': 'application/json',
  });

  return {
    async readActivity(activityId) {
      const res = await fetchFn(
        `${base}/api/v1/activities/${encodeURIComponent(activityId)}`,
        {method: 'GET', headers},
      );
      if (!res.ok) {
        throw new Error(`EXECUTE_OUTCOME_ACTIVITY_GET_FAILED: HTTP ${res.status}`);
      }
      const body = (await res.json()) as Envelope<{
        state_revision: number;
        status: string;
      }>;
      return {
        stateRevision: Number(body.data.state_revision),
        status: String(body.data.status ?? ''),
      };
    },

    async readEffect(effectId) {
      const res = await fetchFn(
        `${base}/api/v1/effects/${encodeURIComponent(effectId)}`,
        {method: 'GET', headers},
      );
      if (!res.ok) {
        throw new Error(`EXECUTE_OUTCOME_EFFECT_GET_FAILED: HTTP ${res.status}`);
      }
      const body = (await res.json()) as Envelope<{
        tool_ref: string;
        status: string;
        evidence_ids?: string[];
      }>;
      return {
        toolRef: String(body.data.tool_ref ?? ''),
        status: String(body.data.status ?? ''),
        evidenceIds: Array.isArray(body.data.evidence_ids)
          ? body.data.evidence_ids.map(String)
          : [],
      };
    },

    /**
     * 读证据工件内容。**不借用** `artifactPutHttpPorts`：该端口把 Authorization
     * 原样透出（不补 `Bearer `），用它取数会 401 且被静默跳过 ——
     * 表现为「候选反查失败」，排查绕远（实测踩过，故此处自建并复用同一鉴权头）。
     */
    async readArtifactContent(artifactId) {
      const res = await fetchFn(
        `${base}/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`,
        {method: 'GET', headers},
      );
      if (!res.ok) {
        throw new Error(`EXECUTE_OUTCOME_ARTIFACT_GET_FAILED: HTTP ${res.status}`);
      }
      return await res.text();
    },

    async postOutcome(input) {
      const res = await fetchFn(
        `${base}/internal/v1/activities/${encodeURIComponent(input.activityId)}/outcomes`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            lease: input.lease,
            expected_state_revision: input.expectedStateRevision,
            outcome: {
              candidate_manifest_id: input.candidateManifestId,
              evidence_ids: input.evidenceIds,
            },
          }),
        },
      );
      if (!res.ok) {
        const text = await res.text().catch(() => '');
        throw new Error(`EXECUTE_OUTCOME_POST_FAILED: HTTP ${res.status} ${text.slice(0, 300)}`);
      }
    },
  };
}

/** 从证据工件内容里取 candidate_manifest_id；不是 JSON 或缺字段则返回 null。 */
export function candidateManifestIdFromEvidence(evidenceText: string): string | null {
  try {
    const parsed = JSON.parse(evidenceText) as unknown;
    if (parsed && typeof parsed === 'object') {
      const value = (parsed as Record<string, unknown>).candidate_manifest_id;
      if (typeof value === 'string' && value.trim()) return value.trim();
    }
  } catch {
    // 证据工件不保证是 JSON（如 run_tests 的 stdout）；此处只需跳过
  }
  return null;
}

/**
 * 提交 EXECUTE 回执。**非致命**：任何一步不成立都返回 `{submitted:false, reason}`，
 * 由调用方把 reason 透出到活动结果（可见即可排查），不抛出、不冒充成功。
 */
export async function submitExecuteOutcome(input: {
  deps: ExecuteOutcomeDeps;
  activityId: string;
  attemptId: string;
  fencingEpoch: string;
  effectIds: readonly string[];
}): Promise<ExecuteOutcomeSubmitResult> {
  const {deps} = input;
  if (input.effectIds.length === 0) {
    return {submitted: false, reason: 'NO_EFFECTS'};
  }

  // 1) 当前活动状态与版本：Kernel 以 expected_state_revision 做乐观并发校验，
  //    必须现读，不能沿用 claim 时的旧值。
  let activity: {stateRevision: number; status: string};
  try {
    activity = await deps.readActivity(input.activityId);
  } catch (err) {
    return {
      submitted: false,
      reason: `ACTIVITY_READ_FAILED:${err instanceof Error ? err.message : String(err)}`,
    };
  }
  if (activity.status !== 'RUNNING') {
    // 已被他人推进或终止：不重复提交（Kernel 也会拒），如实报因
    return {submitted: false, reason: `ACTIVITY_NOT_RUNNING:${activity.status}`};
  }

  // 2) 找封印 effect（取最后一个成功的：若模型重试过封印，以最后一次为准）
  let sealEvidenceIds: string[] = [];
  for (const effectId of input.effectIds) {
    let effect: {toolRef: string; status: string; evidenceIds: string[]};
    try {
      effect = await deps.readEffect(effectId);
    } catch {
      continue; // 单个 effect 读失败不影响其它候选
    }
    if (effect.toolRef === 'seal_candidate' && effect.status === 'SUCCEEDED') {
      sealEvidenceIds = effect.evidenceIds;
    }
  }
  if (sealEvidenceIds.length === 0) {
    return {submitted: false, reason: 'NO_SUCCESSFUL_SEAL_EFFECT'};
  }

  // 3) 沿证据工件反查 candidate_manifest_id
  let candidateManifestId: string | null = null;
  for (const artifactId of sealEvidenceIds) {
    try {
      const content = await deps.readArtifactContent(artifactId);
      candidateManifestId = candidateManifestIdFromEvidence(content);
    } catch {
      continue;
    }
    if (candidateManifestId) break;
  }
  if (!candidateManifestId) {
    return {submitted: false, reason: 'CANDIDATE_NOT_FOUND_IN_SEAL_EVIDENCE'};
  }

  // 4) 提交回执
  try {
    await deps.postOutcome({
      activityId: input.activityId,
      lease: {
        activity_id: input.activityId,
        attempt_id: input.attemptId,
        fencing_epoch: input.fencingEpoch,
      },
      expectedStateRevision: activity.stateRevision,
      candidateManifestId,
      evidenceIds: sealEvidenceIds,
    });
  } catch (err) {
    return {
      submitted: false,
      reason: `OUTCOME_POST_FAILED:${err instanceof Error ? err.message : String(err)}`,
    };
  }
  return {submitted: true, candidateManifestId, evidenceIds: sealEvidenceIds};
}
