/**
 * Runner → Control Step/Effect HTTP 端口。
 * 本地永不读盘/起进程；Kernel 心跳失败经准入门关闭后续工具。
 */
import {bearerAuthHeader} from './authHeader.js';
import type {BrokerToolPorts, ExecuteToolPorts} from './brokerToolBridge.js';

export type ControlHttpBrokerConfig = {
  baseUrl: string;
  authorization: string;
  fetchImpl?: typeof fetch;
};

type Envelope<T> = {data: T};

type EffectRow = {
  id: string;
  status: string;
  state_revision: number;
};

type StepRow = {
  id: string;
  logical_step_id: string;
  intent_revision: number;
};

export type ToolAdmissionGate = {
  allowed: () => boolean;
  closedReason: () => string | null;
  onHeartbeatFailure: (err: unknown) => void;
};

/** 默认打开；任意心跳失败后永久关闭本宿主回合内的工具准入。 */
export function createHeartbeatLinkedAdmissionGate(): ToolAdmissionGate {
  let open = true;
  let reason: string | null = null;
  return {
    allowed: () => open,
    closedReason: () => reason,
    onHeartbeatFailure(err) {
      open = false;
      reason = err instanceof Error ? err.message : 'heartbeat_failed';
    },
  };
}

function assertGateOpen(gate: ToolAdmissionGate): void {
  if (!gate.allowed()) {
    throw new Error(
      `TOOL_ADMISSION_CLOSED:${gate.closedReason() ?? 'admission_closed'}`,
    );
  }
}

/** 从 Control ApiError JSON 抽出 code/message，拼进失败关闭文案。 */
export async function readControlApiErrorDetail(
  res: Response,
): Promise<string> {
  try {
    const body = (await res.clone().json()) as {
      error?: {code?: string; message?: string};
    };
    const code = (body.error?.code || '').trim();
    const message = (body.error?.message || '').trim();
    if (code && message) return `${code}: ${message}`;
    if (code) return code;
    if (message) return message;
  } catch {
    /* 非 JSON 体忽略 */
  }
  return '';
}

async function throwHttpFailure(
  prefix: string,
  res: Response,
): Promise<never> {
  const detail = await readControlApiErrorDetail(res);
  throw new Error(
    `${prefix}: HTTP ${res.status}${detail ? ` ${detail}` : ''}`,
  );
}

/** 在 createStep/prepare/dispatch 前检查准入门；关闭后禁止再打 Broker。 */
export function withToolAdmissionGate(
  ports: ExecuteToolPorts,
  gate: ToolAdmissionGate,
): ExecuteToolPorts;
export function withToolAdmissionGate(
  ports: BrokerToolPorts,
  gate: ToolAdmissionGate,
): BrokerToolPorts;
export function withToolAdmissionGate(
  ports: BrokerToolPorts | ExecuteToolPorts,
  gate: ToolAdmissionGate,
): BrokerToolPorts | ExecuteToolPorts {
  const base: BrokerToolPorts = {
    async prepareEffect(input) {
      assertGateOpen(gate);
      return ports.prepareEffect(input);
    },
    async dispatchEffect(input) {
      assertGateOpen(gate);
      return ports.dispatchEffect(input);
    },
  };
  if (!('createStep' in ports) || typeof ports.createStep !== 'function') {
    return base;
  }
  return {
    ...base,
    async createStep(input) {
      assertGateOpen(gate);
      return ports.createStep(input);
    },
  };
}

export function createHttpBrokerToolPorts(
  config: ControlHttpBrokerConfig,
): BrokerToolPorts {
  const fetchFn = config.fetchImpl ?? fetch;
  const headers = {
    Authorization: bearerAuthHeader(config.authorization),
    'Content-Type': 'application/json',
  };
  const base = config.baseUrl.replace(/\/$/, '');

  return {
    async prepareEffect(input) {
      const res = await fetchFn(`${base}/internal/v1/effects/prepare`, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          lease: input.lease,
          logical_step_id: input.logicalStepId,
          intent_revision: input.intentRevision ?? 1,
          tool_ref: input.toolRef,
          input_artifact_id: input.inputArtifactId,
        }),
      });
      if (!res.ok) {
        await throwHttpFailure('EFFECT_PREPARE_FAILED', res);
      }
      const body = (await res.json()) as Envelope<EffectRow>;
      return {
        effectId: body.data.id,
        status: body.data.status,
        stateRevision: body.data.state_revision,
      };
    },

    async dispatchEffect(input) {
      const res = await fetchFn(
        `${base}/internal/v1/effects/${input.effectId}/dispatch`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            lease: input.lease,
            effect_state_revision: input.effectStateRevision,
          }),
        },
      );
      if (!res.ok) {
        throw new Error(`EFFECT_DISPATCH_FAILED: HTTP ${res.status}`);
      }
      const body = (await res.json()) as Envelope<EffectRow>;
      return {status: body.data.status};
    },
  };
}

/** Step 登记 + prepare/dispatch；供 EXECUTE 宿主一次性装配。 */
export function createHttpExecuteToolPorts(
  config: ControlHttpBrokerConfig,
): ExecuteToolPorts {
  const fetchFn = config.fetchImpl ?? fetch;
  const headers = {
    Authorization: bearerAuthHeader(config.authorization),
    'Content-Type': 'application/json',
  };
  const base = config.baseUrl.replace(/\/$/, '');
  const effects = createHttpBrokerToolPorts(config);

  return {
    ...effects,
    async createStep(input) {
      const res = await fetchFn(
        `${base}/internal/v1/activities/${input.activityId}/steps`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            lease: input.lease,
            predecessor_step_id: input.predecessorStepId ?? null,
            purpose: input.purpose,
            tool_ref: input.toolRef,
          }),
        },
      );
      if (!res.ok) {
        await throwHttpFailure('STEP_CREATE_FAILED', res);
      }
      const body = (await res.json()) as Envelope<StepRow>;
      return {
        stepId: body.data.id,
        logicalStepId: body.data.logical_step_id,
        intentRevision: body.data.intent_revision,
      };
    },
  };
}

/**
 * HTTP 工具端口 + 心跳联动准入门（v0.6 §6：续期失败立即撤销后续工具准入）。
 */
export function createGatedHttpExecuteToolHost(config: ControlHttpBrokerConfig): {
  ports: ExecuteToolPorts;
  gate: ToolAdmissionGate;
} {
  const gate = createHeartbeatLinkedAdmissionGate();
  const ports = withToolAdmissionGate(createHttpExecuteToolPorts(config), gate);
  return {ports, gate};
}
