/**
 * Runner → Control Stop 观察 HTTP 端口（M3：暂停/取消不停在本地假 EXITED）。
 */
import {bearerAuthHeader} from './authHeader.js';
import type {StopReceiptView, StopResourceView, StopSeamPorts} from './stopSeam.js';

export type ControlHttpStopConfig = {
  baseUrl: string;
  authorization: string;
  fetchImpl?: typeof fetch;
};

type Envelope<T> = {data: T};

type StopApiRow = {
  id: string;
  status: StopResourceView['status'];
  state_revision: number;
  receipt_ids?: string[];
};

/**
 * 实现 StopSeamPorts：GET stop + POST receipts。
 * 不发起 /activations/.../stop（那是 Kernel/控制命令侧）。
 */
export function createHttpStopSeamPorts(
  config: ControlHttpStopConfig,
): StopSeamPorts {
  const fetchFn = config.fetchImpl ?? fetch;
  const headers = {
    Authorization: bearerAuthHeader(config.authorization),
    'Content-Type': 'application/json',
  };
  const base = config.baseUrl.replace(/\/$/, '');

  return {
    async loadStop(stopId) {
      const res = await fetchFn(`${base}/internal/v1/stops/${stopId}`, {
        method: 'GET',
        headers,
      });
      if (!res.ok) {
        throw new Error(`STOP_GET_FAILED: HTTP ${res.status}`);
      }
      const body = (await res.json()) as Envelope<StopApiRow>;
      return {
        id: body.data.id,
        status: body.data.status,
        state_revision: body.data.state_revision,
        receipt_ids: body.data.receipt_ids ?? [],
      };
    },

    async postReceipt(body: StopReceiptView) {
      const res = await fetchFn(
        `${base}/internal/v1/stops/${body.stop_id}/receipts`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            receipt_id: body.receipt_id,
            stop_id: body.stop_id,
            activation_id: body.activation_id,
            attempt_id: body.attempt_id,
            resource_instance_id: body.resource_instance_id,
            observed_at: body.observed_at,
            observation: body.observation,
            compute_released: body.compute_released,
            write_capability_revoked: body.write_capability_revoked,
            proof_artifact_ids: body.proof_artifact_ids,
          }),
        },
      );
      if (!res.ok) {
        throw new Error(`STOP_RECEIPT_FAILED: HTTP ${res.status}`);
      }
      const env = (await res.json()) as Envelope<{disposition: string}>;
      return {disposition: env.data.disposition};
    },
  };
}
