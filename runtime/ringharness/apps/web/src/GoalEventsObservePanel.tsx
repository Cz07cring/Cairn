import {useQuery, useQueryClient} from '@tanstack/react-query';
import {useEffect, useMemo, useRef, useState} from 'react';
import {
  consumeGoalEventsOnce,
  getAuthSession,
  getGoalSnapshot,
  type BrowserClientAuth,
  type GoalSseEvent,
} from '@ring/api-client';
import {readStoredObserveGoalId} from './createGoalDraft.js';
import {
  goalEventsCaption,
  shouldInvalidateFromEvent,
  toGoalEventRow,
} from './goalEventsView.js';

/**
 * Goal 事件 SSE 观察：INVALIDATE → 重取 snapshot；事件 ≠ DONE。
 */
export function GoalEventsObservePanel() {
  const queryClient = useQueryClient();
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

  const observeGoalQuery = useQuery({
    queryKey: ['observe-goal-id'],
    queryFn: () => readStoredObserveGoalId(),
    staleTime: 0,
  });
  const goalId = (observeGoalQuery.data || readStoredObserveGoalId()).trim();

  const [events, setEvents] = useState<GoalSseEvent[]>([]);
  const [cursorSeq, setCursorSeq] = useState('0');
  const [cursorExpired, setCursorExpired] = useState(false);
  const [stale, setStale] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const afterSeqRef = useRef('0');
  const alignedGoalRef = useRef('');
  const loopGen = useRef(0);

  const snapshotQuery = useQuery({
    queryKey: ['goal-snapshot', goalId, auth?.kind],
    enabled: auth != null && Boolean(goalId),
    queryFn: ({signal}) => getGoalSnapshot({auth: auth!, goalId, signal}),
  });

  // 换 Goal 或首次 snapshot：用 latest_seq 对齐游标，避免重放全历史
  useEffect(() => {
    if (!goalId) {
      return;
    }
    if (alignedGoalRef.current !== goalId) {
      alignedGoalRef.current = goalId;
      afterSeqRef.current = '0';
      setCursorSeq('0');
      setEvents([]);
      setCursorExpired(false);
    }
    const latest = snapshotQuery.data?.latest_seq;
    if (latest && afterSeqRef.current === '0') {
      afterSeqRef.current = latest;
      setCursorSeq(latest);
    }
  }, [goalId, snapshotQuery.data?.latest_seq]);

  useEffect(() => {
    if (auth == null || !goalId) {
      return;
    }
    const gen = ++loopGen.current;
    const ac = new AbortController();
    let stopped = false;

    const sleep = (ms: number) =>
      new Promise<void>((resolve) => {
        const t = setTimeout(resolve, ms);
        ac.signal.addEventListener('abort', () => {
          clearTimeout(t);
          resolve();
        });
      });

    const tick = async () => {
      while (!stopped && loopGen.current === gen) {
        try {
          const result = await consumeGoalEventsOnce({
            auth,
            goalId,
            afterSeq: afterSeqRef.current,
            signal: ac.signal,
            maxEvents: 20,
          });
          if (stopped || loopGen.current !== gen) {
            return;
          }
          setStreamError(null);
          if (result.cursorExpired) {
            setCursorExpired(true);
            setStale(false);
            afterSeqRef.current = '0';
            setCursorSeq('0');
            await queryClient.invalidateQueries({
              queryKey: ['goal-snapshot', goalId],
            });
            await sleep(1500);
            continue;
          }
          if (result.events.length > 0) {
            setStale(false);
            setCursorExpired(false);
            setEvents((prev) => [...result.events, ...prev].slice(0, 40));
            afterSeqRef.current = result.lastSeq;
            setCursorSeq(result.lastSeq);
            if (result.events.some(shouldInvalidateFromEvent)) {
              await queryClient.invalidateQueries({
                queryKey: ['goal-snapshot', goalId],
              });
              await queryClient.invalidateQueries({
                queryKey: ['goal-finalization'],
              });
              await queryClient.invalidateQueries({
                queryKey: ['goal-audits'],
              });
              await queryClient.invalidateQueries({
                queryKey: ['commands-list'],
              });
              await queryClient.invalidateQueries({
                queryKey: ['commands-goal'],
              });
            }
          } else {
            setStale(true);
          }
          await sleep(2000);
        } catch (err) {
          if (ac.signal.aborted || loopGen.current !== gen) {
            return;
          }
          setStreamError(err instanceof Error ? err.message : '事件流异常');
          await sleep(4000);
        }
      }
    };

    void tick();
    return () => {
      stopped = true;
      ac.abort();
    };
  }, [auth, goalId, queryClient]);

  if (auth == null && !sessionQuery.isPending) {
    return (
      <section className="empty" id="goal-events">
        <div className="symbol">⌁</div>
        <h2>目标的最新动态</h2>
        <p>请先登录，登录后即可查看和操作。</p>
      </section>
    );
  }

  if (!goalId) {
    return (
      <section className="observe" id="goal-events">
        <h2>目标的最新动态</h2>
        <p className="muted">请先选择要查看的目标。</p>
      </section>
    );
  }

  const rows = events.map(toGoalEventRow);
  const latest =
    events[0]?.seq ?? snapshotQuery.data?.latest_seq ?? cursorSeq;

  return (
    <section className="create-goal observe" id="goal-events">
      <h2>目标的最新动态</h2>
      <p className="lead-sm">按时间查看目标刚刚发生了什么；断线后会重新读取权威状态。</p>
      <details className="wb-tech"><summary>查看技术详情</summary><p>GET <code>/api/v1/goals/&#123;id&#125;/events</code>：INVALIDATE
        仅触发 snapshot/列表重读，不从 payload 合并对象。事件 ≠ DONE。</p></details>

      <dl className="fin-facts">
        <div>
          <dt>观察 Goal</dt>
          <dd className="digest">{goalId}</dd>
        </div>
        <div>
          <dt>snapshot status</dt>
          <dd>{snapshotQuery.data?.goal.status ?? '—'}</dd>
        </div>
        <div>
          <dt>latest_seq</dt>
          <dd>{snapshotQuery.data?.latest_seq ?? '—'}</dd>
        </div>
        <div>
          <dt>订阅游标</dt>
          <dd>{cursorSeq}</dd>
        </div>
      </dl>

      <p className="gate-caption">
        {goalEventsCaption({
          latestSeq: latest,
          cursorExpired,
          stale,
          eventCount: events.length,
        })}
      </p>

      {streamError ? <p className="observe-error">{streamError}</p> : null}
      {snapshotQuery.isError ? (
        <p className="observe-error">
          {snapshotQuery.error instanceof Error
            ? snapshotQuery.error.message
            : 'snapshot 不可读'}
        </p>
      ) : null}

      {rows.length > 0 ? (
        <ul className="review-list">
          {rows.map((r) => (
            <li key={`${r.seq}-${r.type}`}>
              <div className="review-head">
                <span>
                  seq={r.seq} · {r.type}
                </span>
                <span className="muted">
                  {r.resourceType}/{r.change}
                </span>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">等待 INVALIDATE 或状态变更事件…</p>
      )}
    </section>
  );
}
