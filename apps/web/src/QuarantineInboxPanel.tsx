import {useQuery} from '@tanstack/react-query';
import {useMemo, useState} from 'react';
import {
  getAuthSession,
  getSystemStatus,
  listProjects,
  listQuarantinedObligations,
  type BrowserClientAuth,
} from '@ring/api-client';
import {
  obligationRowLabel,
  quarantineComponentFromStatus,
  quarantineInboxCaption,
} from './quarantineInboxView.js';

/**
 * Quarantine inbox 只读面板：公开 list + SystemStatus 信号。
 * 不结算义务；≠ DONE。
 */
export function QuarantineInboxPanel() {
  const sessionQuery = useQuery({
    queryKey: ['auth-session'],
    queryFn: ({signal}) => getAuthSession({signal}),
    retry: false,
  });

  const auth: BrowserClientAuth | null = useMemo(() => {
    if (sessionQuery.data) {
      return {kind: 'session', csrfToken: sessionQuery.data.csrf_token};
    }
    const bearer = String(import.meta.env.VITE_RING_DEV_BEARER ?? '').trim();
    if (bearer) {
      return {kind: 'bearer', authorization: bearer};
    }
    return null;
  }, [sessionQuery.data]);

  const [projectId, setProjectId] = useState('');

  const projectsQuery = useQuery({
    queryKey: ['projects', auth?.kind],
    enabled: auth != null && !sessionQuery.isPending,
    queryFn: ({signal}) => listProjects({auth: auth!, signal}),
  });

  const resolvedProjectId =
    projectId ||
    projectsQuery.data?.[0]?.id ||
    String(import.meta.env.VITE_RING_PROJECT_ID ?? '').trim();

  const inboxQuery = useQuery({
    queryKey: ['quarantine-inbox', resolvedProjectId, auth?.kind],
    enabled:
      auth != null && !sessionQuery.isPending && Boolean(resolvedProjectId),
    queryFn: ({signal}) =>
      listQuarantinedObligations({
        auth: auth!,
        projectId: resolvedProjectId,
        signal,
      }),
    refetchInterval: 30_000,
  });

  const statusQuery = useQuery({
    queryKey: ['system-status', resolvedProjectId, auth?.kind],
    enabled:
      auth != null && !sessionQuery.isPending && Boolean(resolvedProjectId),
    queryFn: ({signal}) =>
      getSystemStatus({
        auth: auth!,
        projectId: resolvedProjectId,
        signal,
      }),
    refetchInterval: 30_000,
  });

  const component = statusQuery.data
    ? quarantineComponentFromStatus(statusQuery.data)
    : null;
  const rows = inboxQuery.data ?? [];

  if (auth == null && !sessionQuery.isPending) {
    return (
      <section className="empty" id="quarantine-inbox">
        <div className="symbol">⚠</div>
        <h2>材料不足，暂不能验收</h2>
        <p>请先登录，登录后即可查看哪些验收材料仍然不足。</p>
      </section>
    );
  }

  return (
    <section className="create-goal observe" id="quarantine-inbox">
      <h2>材料不足，暂不能验收</h2>
      <p className="lead-sm">这些验收项缺少可靠材料，补齐前不会进入最终交付。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>GET <code>/api/v1/verification-obligations?status=QUARANTINED</code> +
        SystemStatus 组件信号。只读；不自动 ASSESSED；≠ Goal DONE。</p></details>

      <div className="create-goal-grid">
        <label>
          项目
          <select
            value={resolvedProjectId}
            onChange={(e) => setProjectId(e.target.value)}
          >
            {(projectsQuery.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      <p className="gate-caption">
        {quarantineInboxCaption({
          component,
          rowCount: rows.length,
        })}
      </p>

      {statusQuery.data ? (
        <dl className="fin-facts">
          <div>
            <dt>隔离资源</dt>
            <dd>{statusQuery.data.resources.quarantined_resources}</dd>
          </div>
          <div>
            <dt>信任态</dt>
            <dd>{statusQuery.data.trust.status}</dd>
          </div>
          <div>
            <dt>stale</dt>
            <dd>{statusQuery.data.stale ? 'true' : 'false'}</dd>
          </div>
        </dl>
      ) : null}

      {inboxQuery.isError ? (
        <p className="observe-error">
          {inboxQuery.error instanceof Error
            ? inboxQuery.error.message
            : 'inbox 不可读'}
        </p>
      ) : null}

      {rows.length > 0 ? (
        <ul className="review-list">
          {rows.map((r) => (
            <li key={r.id}>
              <div className="review-head">
                <span>{obligationRowLabel(r)}</span>
                <span className="muted">{r.updated_at}</span>
              </div>
              <p className="digest">
                subject=<code>{r.subject_id}</code> · effects=
                {r.effect_ids.length}
                {r.superseded_by_obligation_id
                  ? ` · superseded_by=${r.superseded_by_obligation_id.slice(0, 8)}…`
                  : ''}
              </p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">当前无 QUARANTINED 义务行。</p>
      )}
    </section>
  );
}
