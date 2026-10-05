/**
 * 官方 AgentLoop：Runner 自持 chat 时，仍把每轮登记进 Kernel ModelInvocation。
 * 路径：create → dispatch(runner_owned_completion) → chat → receipt。
 * ≠ Goal DONE；不替代 Control 对 Cordis 路径的代调 dispatch。
 */
import {createHash, randomUUID} from 'node:crypto';
import type {LeaseIdentity} from './fakePlanHost.js';
import type {
  ChatMessage,
  ChatRoundResult,
  ChatToolDef,
} from './openaiCompatibleToolChat.js';

export type ModelInvocationLedgerPorts = {
  baseUrl: string;
  authorization: string;
  fetchImpl?: typeof fetch;
  lease: LeaseIdentity;
  contextDigest: string;
  providerRef: string;
  modelId: string;
  maxOutputTokens?: number;
};

type Envelope<T> = {data: T};

function bearer(raw: string): string {
  return raw.startsWith('Bearer ') ? raw : `Bearer ${raw}`;
}

function inputDigestOfRound(
  messages: readonly ChatMessage[],
  tools: readonly ChatToolDef[],
): string {
  const payload = JSON.stringify({messages, tools});
  return (
    'sha256:' + createHash('sha256').update(payload, 'utf8').digest('hex')
  );
}

function usageFromRound(round: ChatRoundResult): {
  inputTokens: number | null;
  outputTokens: number | null;
  usageStatus: 'CONFIRMED' | 'UNKNOWN';
} {
  const raw = round.raw as {
    usage?: {prompt_tokens?: number; completion_tokens?: number};
  } | null;
  const input = raw?.usage?.prompt_tokens;
  const output = raw?.usage?.completion_tokens;
  if (
    typeof input === 'number' &&
    typeof output === 'number' &&
    Number.isFinite(input) &&
    Number.isFinite(output)
  ) {
    return {
      inputTokens: Math.max(0, Math.trunc(input)),
      outputTokens: Math.max(0, Math.trunc(output)),
      usageStatus: 'CONFIRMED',
    };
  }
  return {inputTokens: null, outputTokens: null, usageStatus: 'UNKNOWN'};
}

/**
 * 包一层 chat：成功/失败都尽量落账本；登记失败向上抛（失败关闭）。
 * chat 成功但 receipt 失败 → 抛错（避免静默无账）。
 */
export function createLedgeredChatRunner(ports: ModelInvocationLedgerPorts) {
  const fetchFn = ports.fetchImpl ?? fetch;
  const base = ports.baseUrl.replace(/\/$/, '');
  const headers = {
    Authorization: bearer(ports.authorization),
    'Content-Type': 'application/json',
    Accept: 'application/json',
  };
  let seq = 0;

  async function createAndDispatch(
    messages: readonly ChatMessage[],
    tools: readonly ChatToolDef[],
  ): Promise<{invocationId: string; stateRevision: number}> {
    seq += 1;
    const createRes = await fetchFn(`${base}/internal/v1/model-invocations`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        lease: ports.lease,
        invocation_seq: seq,
        context_digest: ports.contextDigest,
        input_digest: inputDigestOfRound(messages, tools),
        provider_ref: ports.providerRef,
        model_id: ports.modelId,
        max_output_tokens: ports.maxOutputTokens ?? 512,
        max_cost_usd: '0',
        data_categories: [],
        exposed_tools: tools.map((t) => t.function.name).filter(Boolean),
      }),
    });
    if (!createRes.ok) {
      const text = await createRes.text().catch(() => '');
      throw new Error(
        `MODEL_INVOCATION_LEDGER_CREATE_HTTP_${createRes.status}:${text.slice(0, 200)}`,
      );
    }
    const created = (await createRes.json()) as Envelope<{
      id: string;
      state_revision: number;
      status: string;
    }>;
    if (created.data.status !== 'AUTHORIZED' && created.data.status !== 'DISPATCHED') {
      // 幂等重放可能已是终态；仍尝试 dispatch 仅当 AUTHORIZED
      if (created.data.status === 'SUCCEEDED' || created.data.status === 'FAILED') {
        return {
          invocationId: created.data.id,
          stateRevision: created.data.state_revision,
        };
      }
    }

    const dispatchRes = await fetchFn(
      `${base}/internal/v1/model-invocations/${created.data.id}/dispatch`,
      {
        method: 'POST',
        headers,
        body: JSON.stringify({
          lease: ports.lease,
          expected_state_revision: created.data.state_revision,
          runner_owned_completion: true,
        }),
      },
    );
    if (!dispatchRes.ok) {
      const text = await dispatchRes.text().catch(() => '');
      throw new Error(
        `MODEL_INVOCATION_LEDGER_DISPATCH_HTTP_${dispatchRes.status}:${text.slice(0, 200)}`,
      );
    }
    const dispatched = (await dispatchRes.json()) as Envelope<{
      id: string;
      state_revision: number;
      status: string;
    }>;
    if (dispatched.data.status !== 'DISPATCHED') {
      throw new Error(
        `MODEL_INVOCATION_LEDGER_NOT_DISPATCHED:${dispatched.data.status}`,
      );
    }
    return {
      invocationId: dispatched.data.id,
      stateRevision: dispatched.data.state_revision,
    };
  }

  async function postReceipt(input: {
    invocationId: string;
    observed: 'SUCCEEDED' | 'FAILED' | 'UNKNOWN';
    usageStatus: 'CONFIRMED' | 'UNKNOWN';
    inputTokens: number | null;
    outputTokens: number | null;
  }): Promise<void> {
    const res = await fetchFn(
      `${base}/internal/v1/model-invocations/${input.invocationId}/receipts`,
      {
        method: 'POST',
        headers,
        body: JSON.stringify({
          receipt_id: randomUUID(),
          invocation_id: input.invocationId,
          producer_attempt_id: ports.lease.attempt_id,
          observed_result: input.observed,
          usage_status: input.usageStatus,
          input_tokens: input.inputTokens,
          output_tokens: input.outputTokens,
          cost_usd: null,
          result_artifact_id: null,
          observed_at: new Date().toISOString().replace(/\.\d{3}Z$/, '.000Z'),
        }),
      },
    );
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(
        `MODEL_INVOCATION_LEDGER_RECEIPT_HTTP_${res.status}:${text.slice(0, 200)}`,
      );
    }
  }

  return {
    async runRound(
      chatFn: () => Promise<ChatRoundResult>,
      messages: readonly ChatMessage[],
      tools: readonly ChatToolDef[],
    ): Promise<ChatRoundResult> {
      const {invocationId} = await createAndDispatch(messages, tools);
      try {
        const round = await chatFn();
        const usage = usageFromRound(round);
        await postReceipt({
          invocationId,
          observed: 'SUCCEEDED',
          usageStatus: usage.usageStatus,
          inputTokens: usage.inputTokens,
          outputTokens: usage.outputTokens,
        });
        return round;
      } catch (err) {
        try {
          await postReceipt({
            invocationId,
            observed: 'FAILED',
            usageStatus: 'UNKNOWN',
            inputTokens: null,
            outputTokens: null,
          });
        } catch {
          /* 保留原始 chat 错误 */
        }
        throw err;
      }
    },
  };
}

/** 从 chat base / 环境推断 provider_ref（须能通过 ModelProfile 冻结校验）。 */
export function resolveOfficialProviderRef(env: NodeJS.ProcessEnv = process.env): string {
  const explicit = (env.RING_MODEL_PROVIDER_REF || '').trim();
  if (explicit) return explicit;
  const base = (env.RING_LOCAL_QWEN_BASE || '').toLowerCase();
  const model = (env.RING_LOCAL_QWEN_MODEL || '').toLowerCase();
  if (base.includes('deepseek') || model.includes('deepseek')) {
    return 'deepseek:api';
  }
  return 'pm2:omlx-flashnext';
}
