/**
 * Effect 观察 HTTP：dispatch 后只读状态 / 提交可信回执。
 * 禁止在无观察证据时本地冒充 SUCCEEDED。
 */
import {bearerAuthHeader} from './authHeader.js';

export type ControlHttpEffectConfig = {
  baseUrl: string;
  authorization: string;
  fetchImpl?: typeof fetch;
};

type Envelope<T> = {data: T};

export type EffectStatusView = {
  id: string;
  status: string;
  stateRevision: number;
  /** Kernel EffectResource.evidence_ids；缺省空 */
  evidenceIds: string[];
};

export type TrustedReceiptInput = {
  receiptId: string;
  effectId: string;
  producerActivityId: string;
  producerAttemptId: string;
  fencingEpoch: string;
  startedAt: string;
  finishedAt: string;
  observedOutcome: 'SUCCEEDED' | 'FAILED' | 'UNKNOWN';
  exitCode?: number | null;
  signal?: string | null;
  timedOut?: boolean;
  stdoutArtifactId?: string | null;
  stderrArtifactId?: string | null;
  resultArtifactIds?: string[];
  externalRef?: string | null;
};

export type EffectObservePorts = {
  getEffect: (effectId: string) => Promise<EffectStatusView>;
  postTrustedReceipt: (
    input: TrustedReceiptInput,
  ) => Promise<{disposition: string}>;
};

export function createHttpEffectObservePorts(
  config: ControlHttpEffectConfig,
): EffectObservePorts {
  const fetchFn = config.fetchImpl ?? fetch;
  const headers = {
    Authorization: bearerAuthHeader(config.authorization),
    'Content-Type': 'application/json',
  };
  const base = config.baseUrl.replace(/\/$/, '');

  return {
    async getEffect(effectId) {
      const res = await fetchFn(`${base}/api/v1/effects/${effectId}`, {
        method: 'GET',
        headers,
      });
      if (!res.ok) {
        throw new Error(`EFFECT_GET_FAILED: HTTP ${res.status}`);
      }
      const body = (await res.json()) as Envelope<{
        id: string;
        status: string;
        state_revision: number;
        evidence_ids?: string[];
      }>;
      return {
        id: body.data.id,
        status: body.data.status,
        stateRevision: body.data.state_revision,
        evidenceIds: Array.isArray(body.data.evidence_ids)
          ? body.data.evidence_ids.map(String)
          : [],
      };
    },

    async postTrustedReceipt(input) {
      const res = await fetchFn(
        `${base}/internal/v1/effects/${input.effectId}/receipts`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            receipt_id: input.receiptId,
            effect_id: input.effectId,
            producer_activity_id: input.producerActivityId,
            producer_attempt_id: input.producerAttemptId,
            fencing_epoch: input.fencingEpoch,
            started_at: input.startedAt,
            finished_at: input.finishedAt,
            exit_code: input.exitCode ?? null,
            signal: input.signal ?? null,
            timed_out: input.timedOut ?? false,
            stdout_artifact_id: input.stdoutArtifactId ?? null,
            stderr_artifact_id: input.stderrArtifactId ?? null,
            result_artifact_ids: input.resultArtifactIds ?? [],
            external_ref: input.externalRef ?? null,
            observed_outcome: input.observedOutcome,
          }),
        },
      );
      if (!res.ok) {
        throw new Error(`EFFECT_RECEIPT_FAILED: HTTP ${res.status}`);
      }
      const env = (await res.json()) as Envelope<{disposition: string}>;
      return {disposition: env.data.disposition};
    },
  };
}

const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'UNKNOWN', 'CANCELLED']);

/**
 * 轮询 GET effect；超时返回当前状态，**不**自动写 SUCCEEDED 回执。
 */
export async function pollEffectStatus(
  ports: EffectObservePorts,
  effectId: string,
  opts: {maxAttempts?: number; delayMs?: number; sleep?: (ms: number) => Promise<void>} = {},
): Promise<EffectStatusView> {
  const maxAttempts = opts.maxAttempts ?? 5;
  const delayMs = opts.delayMs ?? 0;
  const sleep = opts.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
  let last: EffectStatusView | undefined;
  for (let i = 0; i < maxAttempts; i += 1) {
    last = await ports.getEffect(effectId);
    if (TERMINAL.has(last.status)) {
      return last;
    }
    if (i + 1 < maxAttempts && delayMs > 0) {
      await sleep(delayMs);
    }
  }
  if (!last) {
    throw new Error('EFFECT_POLL_EMPTY');
  }
  return last;
}
