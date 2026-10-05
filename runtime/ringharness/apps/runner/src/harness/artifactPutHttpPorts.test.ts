/**
 * 第九十二批：EXECUTE 输入工件 PUT collector。
 */
import {expect, test, vi} from 'vitest';
import {
  contentDigestSha256,
  createHttpArtifactPutPorts,
} from './artifactPutHttpPorts.js';

const lease = {
  activity_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  attempt_id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
  fencing_epoch: '1',
};

test('contentDigestSha256 稳定', () => {
  expect(contentDigestSha256('{"path":"a"}')).toMatch(/^sha256:[0-9a-f]{64}$/);
  expect(contentDigestSha256('{"path":"a"}')).toBe(contentDigestSha256('{"path":"a"}'));
});

test('putCollectorContent：PUT digest + fencing 头对齐 Control', async () => {
  const body = '{"path":"src/main.py"}';
  const digest = contentDigestSha256(body);
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    expect(String(url)).toBe(
      `http://control.test/internal/v1/artifacts/${digest}/content`,
    );
    expect(init?.method).toBe('PUT');
    const headers = init?.headers as Record<string, string>;
    expect(headers['X-Ring-Project-Id']).toBe('pppppppp-pppp-pppp-pppp-pppppppppppp');
    expect(headers['X-Ring-Activity-Id']).toBe(lease.activity_id);
    expect(headers['X-Ring-Attempt-Id']).toBe(lease.attempt_id);
    expect(headers['X-Ring-Fencing-Epoch']).toBe('1');
    expect(headers['Content-Type']).toBe('application/json');
    return {
      ok: true,
      status: 201,
      json: async () => ({
        data: {id: 'art-1', digest},
      }),
    } as Response;
  });

  const ports = createHttpArtifactPutPorts({
    baseUrl: 'http://control.test',
    authorization: 'Bearer w',
    fetchImpl: fetchImpl as unknown as typeof fetch,
  });
  const out = await ports.putCollectorContent({
    projectId: 'pppppppp-pppp-pppp-pppp-pppppppppppp',
    lease,
    body,
    mime: 'application/json',
  });
  expect(out).toEqual({artifactId: 'art-1', digest});
});

test('PUT 非 2xx 失败关闭', async () => {
  const ports = createHttpArtifactPutPorts({
    baseUrl: 'http://control.test',
    authorization: 'Bearer w',
    fetchImpl: (async () =>
      ({ok: false, status: 403, json: async () => ({})}) as Response) as typeof fetch,
  });
  await expect(
    ports.putCollectorContent({
      projectId: 'p',
      lease,
      body: '{}',
    }),
  ).rejects.toThrow(/ARTIFACT_PUT_FAILED: HTTP 403/);
});

test('getArtifactContent：GET /api/v1/artifacts/{id}/content', async () => {
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    expect(String(url)).toBe('http://control.test/api/v1/artifacts/art-9/content');
    expect(init?.method).toBe('GET');
    const headers = init?.headers as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer w');
    return {
      ok: true,
      status: 200,
      text: async () => 'file body here',
    } as Response;
  });
  const ports = createHttpArtifactPutPorts({
    baseUrl: 'http://control.test',
    authorization: 'Bearer w',
    fetchImpl: fetchImpl as unknown as typeof fetch,
  });
  await expect(ports.getArtifactContent('art-9')).resolves.toBe('file body here');
});

test('GET 非 2xx 失败关闭', async () => {
  const ports = createHttpArtifactPutPorts({
    baseUrl: 'http://control.test',
    authorization: 'Bearer w',
    fetchImpl: (async () =>
      ({ok: false, status: 404, text: async () => ''}) as Response) as typeof fetch,
  });
  await expect(ports.getArtifactContent('missing')).rejects.toThrow(
    /ARTIFACT_GET_FAILED: HTTP 404/,
  );
});
