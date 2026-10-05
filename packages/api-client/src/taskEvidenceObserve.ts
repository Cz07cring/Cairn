/**
 * Task 证据列表只读观察。
 * EvidenceEnvelope 行 ≠ Audit PASS ≠ Goal DONE。
 */
import type {components} from './generated.js';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type EvidenceEnvelopeResource =
  components['schemas']['EvidenceEnvelopeResource'];

/** GET /api/v1/tasks/{task_id}/evidence */
export async function listTaskEvidence(input: {
  taskId: string;
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
  limit?: number;
  cursor?: string;
}): Promise<{items: EvidenceEnvelopeResource[]; nextCursor: string | null}> {
  const taskId = input.taskId.trim();
  if (!taskId) {
    throw new Error('缺少 task_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const params = new URLSearchParams({
    limit: String(input.limit ?? 50),
  });
  if (input.cursor?.trim()) {
    params.set('cursor', input.cursor.trim());
  }
  const response = await fetch(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/evidence?${params}`,
    {
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
    },
  );
  if (response.status === 401 || response.status === 403) {
    throw new Error('无权读取任务证据');
  }
  if (!response.ok) {
    throw new Error(`请求失败（HTTP ${response.status}）`);
  }
  const body: unknown = await response.json();
  if (
    typeof body !== 'object' ||
    body === null ||
    !('data' in body) ||
    !Array.isArray((body as {data: unknown}).data)
  ) {
    throw new Error('列表响应无法识别');
  }
  const meta =
    'meta' in body && typeof (body as {meta: unknown}).meta === 'object'
      ? ((body as {meta: {next_cursor?: string | null}}).meta ?? null)
      : null;
  return {
    items: (body as {data: EvidenceEnvelopeResource[]}).data,
    nextCursor:
      meta?.next_cursor != null && String(meta.next_cursor).trim()
        ? String(meta.next_cursor)
        : null,
  };
}
