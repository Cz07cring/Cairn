/**
 * Kernel ModelInvocation HTTP 端口：Cordis 薄 llm 经控制面登记调用。
 * PLAN fixture 与 test_plan_host 对齐：create → AUTHORIZED，不伪造 DISPATCHED。
 * liveDispatch 才走 /dispatch（本地 Qwen）；缺模型时由控制面 503，失败关闭。
 * EXECUTE：create 携带 exposed_tools；live 回传 tool_calls → Cordis chunks。
 */
import {bearerAuthHeader} from './authHeader.js';
import type {KernelLlmChunk} from './cordisBootGate.js';
import type {KernelModelInvocationPorts} from './cordisLlmBridge.js';
import type {LeaseIdentity} from './fakePlanHost.js';

export type ControlHttpConfig = {
  baseUrl: string;
  authorization: string;
  fetchImpl?: typeof fetch;
  /** 为 true 时 completeInvocation 走 dispatch（需本地模型）。 */
  liveDispatch?: boolean;
  fixtureText?: string;
  /** 非 live：可注入 tool-call chunks（EXECUTE fixture）。 */
  fixtureChunks?: KernelLlmChunk[];
};

type Envelope<T> = {data: T};

type DispatchResource = {
  id: string;
  assistant_text?: string | null;
  tool_calls?: Array<{id: string; name: string; arguments: string}> | null;
};

export function createHttpModelInvocationPorts(
  config: ControlHttpConfig,
): KernelModelInvocationPorts {
  const fetchFn = config.fetchImpl ?? fetch;
  const headers = {
    Authorization: bearerAuthHeader(config.authorization),
    'Content-Type': 'application/json',
  };
  let seq = 0;

  return {
    async createInvocation(input) {
      seq += 1;
      const res = await fetchFn(`${config.baseUrl}/internal/v1/model-invocations`, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          lease: input.lease,
          invocation_seq: seq,
          context_digest: input.contextDigest,
          input_digest: input.inputDigest,
          provider_ref: input.providerRef,
          model_id: input.modelId,
          max_output_tokens: 512,
          max_cost_usd: '0',
          data_categories: [],
          exposed_tools: [...input.toolsExposedToModel],
        }),
      });
      if (!res.ok) {
        throw new Error(`MODEL_INVOCATION_CREATE_FAILED: HTTP ${res.status}`);
      }
      const body = (await res.json()) as Envelope<{id: string}>;
      return {invocationId: body.data.id};
    },

    async completeInvocation(input: {invocationId: string; lease: LeaseIdentity}) {
      if (!config.liveDispatch) {
        // 诚实：仅登记后返回宿主侧文本/chunks；不 POST 假 receipts（需先 DISPATCHED）。
        if (config.fixtureChunks && config.fixtureChunks.length > 0) {
          return {text: config.fixtureText ?? '', chunks: config.fixtureChunks};
        }
        return {text: config.fixtureText ?? 'fixture-plan-host'};
      }
      const dispatched = await fetchFn(
        `${config.baseUrl}/internal/v1/model-invocations/${input.invocationId}/dispatch`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            lease: input.lease,
            expected_state_revision: 1,
          }),
        },
      );
      if (!dispatched.ok) {
        throw new Error(`MODEL_INVOCATION_DISPATCH_FAILED: HTTP ${dispatched.status}`);
      }
      const body = (await dispatched.json()) as Envelope<DispatchResource>;
      const assistant = (body.data?.assistant_text || '').trim();
      const toolCalls = body.data?.tool_calls ?? [];
      const chunks: KernelLlmChunk[] = [];
      if (assistant) {
        chunks.push({type: 'text-delta', text: assistant});
      }
      for (const call of toolCalls) {
        chunks.push({
          type: 'tool-call',
          name: call.name,
          arguments: call.arguments,
        });
      }
      chunks.push({type: 'finish', reason: 'stop'});
      if (!assistant && toolCalls.length === 0) {
        // live 路径禁止用 fixture 文本冒充模型结果
        throw new Error(
          'MODEL_INVOCATION_LIVE_EMPTY: liveDispatch 未返回 assistant_text 或 tool_calls',
        );
      }
      return {text: assistant, chunks};
    },
  };
}
