/**
 * 项目记忆列表；只读观察。
 * Memory VERIFIED ≠ Goal DONE。
 */
import type {components} from './generated.js';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type MemoryResource = components['schemas']['MemoryResource'];
export type MemoryKind = MemoryResource['kind'];

async function readEnvelopeList<T>(
  response: Response,
  emptyMessage: string,
): Promise<T[]> {
  if (response.status === 401 || response.status === 403) {
    throw new Error(emptyMessage);
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
  return (body as {data: T[]}).data;
}

/** GET /api/v1/memories */
export async function listMemories(input: {
  projectId: string;
  kind?: MemoryKind;
  q?: string;
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
  limit?: number;
}): Promise<MemoryResource[]> {
  const projectId = input.projectId.trim();
  if (!projectId) {
    throw new Error('缺少 project_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const params = new URLSearchParams({
    project_id: projectId,
    limit: String(input.limit ?? 50),
  });
  if (input.kind) {
    params.set('kind', input.kind);
  }
  if (input.q?.trim()) {
    params.set('q', input.q.trim());
  }
  const response = await fetch(`/api/v1/memories?${params}`, {
    signal: input.signal,
    cache: 'no-store',
    ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
  });
  return readEnvelopeList<MemoryResource>(response, '无权读取记忆列表');
}
