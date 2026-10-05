/**
 * Runner → Control collector：物化 EXECUTE 工具输入工件。
 * digest 必须与正文一致；须持有效租约 fencing（失败关闭）。
 */
import {createHash} from 'node:crypto';
import {bearerAuthHeader} from './authHeader.js';

export type ControlHttpArtifactConfig = {
  baseUrl: string;
  authorization: string;
  fetchImpl?: typeof fetch;
};

export type PutCollectorInput = {
  projectId: string;
  lease: {activity_id: string; attempt_id: string; fencing_epoch: string};
  body: Uint8Array | string;
  mime?: string;
};

export type PutCollectorResult = {
  artifactId: string;
  digest: string;
};

type Envelope<T> = {data: T};

function toBytes(body: Uint8Array | string): Uint8Array {
  if (typeof body === 'string') {
    return new TextEncoder().encode(body);
  }
  return body;
}

export function contentDigestSha256(body: Uint8Array | string): string {
  const bytes = toBytes(body);
  return 'sha256:' + createHash('sha256').update(bytes).digest('hex');
}

export type ArtifactPutPorts = {
  putCollectorContent: (input: PutCollectorInput) => Promise<PutCollectorResult>;
  /** 只读拉回 evidence 工件正文（ToolResult 回灌）；缺省实现由 HTTP GET */
  getArtifactContent: (artifactId: string) => Promise<string>;
};

export function createHttpArtifactPutPorts(
  config: ControlHttpArtifactConfig,
): ArtifactPutPorts {
  const fetchFn = config.fetchImpl ?? fetch;
  const base = config.baseUrl.replace(/\/$/, '');
  // 形态对齐 `controlHttpPorts.authHeaders`：调用方按全仓约定传**裸 token**，此处补
  // `Bearer `。原样设头会被控制面判 UNAUTHENTICATED 401 —— 实测该缺陷使审计的采集
  // 载荷上传失败（`ARTIFACT_PUT_FAILED: HTTP 401 请先登录`），审计活动永停 RUNNING，
  // 外部只看到安静的卡住（同一缺陷类见 executeOutcomeSubmit.ts 的同类注释）。
  const authValue = bearerAuthHeader(config.authorization);

  return {
    async putCollectorContent(input) {
      const bytes = toBytes(input.body);
      const digest = contentDigestSha256(bytes);
      const res = await fetchFn(`${base}/internal/v1/artifacts/${digest}/content`, {
        method: 'PUT',
        headers: {
          Authorization: authValue,
          'Content-Type': input.mime || 'application/octet-stream',
          'X-Ring-Project-Id': input.projectId,
          'X-Ring-Activity-Id': input.lease.activity_id,
          'X-Ring-Attempt-Id': input.lease.attempt_id,
          'X-Ring-Fencing-Epoch': input.lease.fencing_epoch,
        },
        body: Buffer.from(bytes),
      });
      if (!res.ok) {
        let detail = '';
        try {
          detail =
            typeof res.text === 'function'
              ? (await res.text()).slice(0, 400)
              : '';
        } catch {
          detail = '';
        }
        throw new Error(
          `ARTIFACT_PUT_FAILED: HTTP ${res.status}${detail ? ` ${detail}` : ''}`,
        );
      }
      const env = (await res.json()) as Envelope<{id: string; digest?: string}>;
      return {artifactId: env.data.id, digest: env.data.digest ?? digest};
    },
    async getArtifactContent(artifactId) {
      const res = await fetchFn(`${base}/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`, {
        method: 'GET',
        headers: {
          Authorization: authValue,
        },
      });
      if (!res.ok) {
        throw new Error(`ARTIFACT_GET_FAILED: HTTP ${res.status}`);
      }
      return await res.text();
    },
  };
}
