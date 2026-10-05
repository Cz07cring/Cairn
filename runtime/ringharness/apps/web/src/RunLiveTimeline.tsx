import {useQuery, useQueryClient} from '@tanstack/react-query';
import {useEffect, useMemo, useRef, useState} from 'react';
import {
  consumeGoalEventsOnce,
  getAuthSession,
  getGoalSnapshot,
  listCommands,
  listEffects,
  listGoalActivities,
  listModelInvocations,
  type BrowserClientAuth,
} from '@ring/api-client';
import {readStoredObserveGoalId} from './createGoalDraft.js';
import {assessRunProgress, buildRunLiveTimeline, type RunLiveRow} from './runLiveTimelineView.js';

const sourceLabels: Record<RunLiveRow['source'], string> = {
  activity: '工作步骤', model: '模型工作', effect: '工具与命令', command: '运行控制',
};

function formatTime(value: string | null): string {
  if (!value) return '时间未记录';
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(date);
}

/** 汇总真实控制面数据；不展示模型私有推理链。 */
export function RunLiveTimeline({standalone = false}: {standalone?: boolean}) {
  const queryClient = useQueryClient();
  const [showAll, setShowAll] = useState(false);
  const [search, setSearch] = useState('');
  const [autoFollow, setAutoFollow] = useState(true);
  const [clearedIds, setClearedIds] = useState<Set<string>>(() => new Set());
  const listRef = useRef<HTMLOListElement>(null);
  const [streamState, setStreamState] = useState<'connecting' | 'live' | 'reconnecting'>('connecting');
  const streamSeq = useRef('0');
  const streamGoal = useRef('');
  const sessionQuery = useQuery({
    queryKey: ['auth-session'], queryFn: ({signal}) => getAuthSession({signal}), retry: false,
  });
  const auth: BrowserClientAuth | null = useMemo(() => {
    if (sessionQuery.data) return {kind: 'session', csrfToken: sessionQuery.data.csrf_token};
    const bearer = String(import.meta.env.VITE_RING_DEV_BEARER ?? '').trim();
    return bearer ? {kind: 'bearer', authorization: bearer} : null;
  }, [sessionQuery.data]);

  const observeGoalQuery = useQuery({
    queryKey: ['observe-goal-id'], queryFn: () => readStoredObserveGoalId(), staleTime: 0,
  });
  const goalId = (
    observeGoalQuery.data || readStoredObserveGoalId() ||
    String(import.meta.env.VITE_RING_GOAL_ID ?? '')
  ).trim();
  const goalQuery = useQuery({
    queryKey: ['run-live-goal', goalId, auth?.kind],
    enabled: auth != null && Boolean(goalId),
    queryFn: ({signal}) => getGoalSnapshot({auth: auth!, goalId, signal}),
    refetchInterval: 5_000,
  });
  const projectId = goalQuery.data?.goal.project_id ?? String(import.meta.env.VITE_RING_PROJECT_ID ?? '').trim();
  const activitiesQuery = useQuery({
    queryKey: ['run-live-activities', goalId, auth?.kind], enabled: auth != null && Boolean(goalId),
    queryFn: ({signal}) => listGoalActivities({auth: auth!, goalId, signal, limit: 50}), refetchInterval: 5_000,
  });
  const invocationsQuery = useQuery({
    queryKey: ['run-live-models', projectId, goalId, auth?.kind],
    enabled: auth != null && Boolean(projectId) && Boolean(goalId),
    queryFn: ({signal}) => listModelInvocations({auth: auth!, projectId, goalId, signal, limit: 50}),
    refetchInterval: 5_000,
  });
  const effectsQuery = useQuery({
    queryKey: ['run-live-effects', projectId, goalId, auth?.kind],
    enabled: auth != null && Boolean(projectId) && Boolean(goalId),
    queryFn: ({signal}) => listEffects({auth: auth!, projectId, goalId, signal, limit: 50}),
    refetchInterval: 5_000,
  });
  const commandsQuery = useQuery({
    queryKey: ['run-live-commands', projectId, goalId, auth?.kind],
    enabled: auth != null && Boolean(goalId),
    queryFn: ({signal}) => listCommands({auth: auth!, projectId: projectId || undefined, goalId, signal}),
    refetchInterval: 5_000,
  });

  useEffect(() => {
    if (!auth || !goalId || !goalQuery.data?.latest_seq) return;
    if (streamGoal.current !== goalId) {
      streamGoal.current = goalId;
      streamSeq.current = goalQuery.data.latest_seq;
    }
    const controller = new AbortController();
    let stopped = false;
    const wait = (ms: number) => new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, ms);
      controller.signal.addEventListener('abort', () => {clearTimeout(timer); resolve();}, {once: true});
    });
    const listen = async () => {
      while (!stopped) {
        try {
          setStreamState(streamSeq.current === '0' ? 'connecting' : 'live');
          const result = await consumeGoalEventsOnce({
            auth, goalId, afterSeq: streamSeq.current, signal: controller.signal, maxEvents: 20,
          });
          if (stopped) return;
          if (result.cursorExpired) {
            streamSeq.current = '0';
            setStreamState('reconnecting');
            await queryClient.invalidateQueries({queryKey: ['run-live-goal', goalId]});
          } else if (result.events.length > 0) {
            streamSeq.current = result.lastSeq;
            await Promise.all([
              queryClient.invalidateQueries({queryKey: ['run-live-goal', goalId]}),
              queryClient.invalidateQueries({queryKey: ['run-live-activities', goalId]}),
              queryClient.invalidateQueries({queryKey: ['run-live-models']}),
              queryClient.invalidateQueries({queryKey: ['run-live-effects']}),
              queryClient.invalidateQueries({queryKey: ['run-live-commands']}),
            ]);
          }
          await wait(500);
        } catch {
          if (stopped || controller.signal.aborted) return;
          setStreamState('reconnecting');
          await wait(2_000);
        }
      }
    };
    void listen();
    return () => {stopped = true; controller.abort();};
  }, [auth, goalId, goalQuery.data?.latest_seq, queryClient]);

  const rows = useMemo(() => buildRunLiveTimeline({
    activities: activitiesQuery.data ?? [], invocations: invocationsQuery.data ?? [],
    effects: effectsQuery.data ?? [], commands: commandsQuery.data ?? [],
  }), [activitiesQuery.data, invocationsQuery.data, effectsQuery.data, commandsQuery.data]);
  const runProgress = assessRunProgress(
    goalQuery.data?.goal.status,
    activitiesQuery.data ?? [],
  );
  const filteredRows = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    return rows.filter((row) => !clearedIds.has(row.id) && (!needle ||
      `${row.title} ${row.summary} ${row.details}`.toLocaleLowerCase().includes(needle)));
  }, [rows, search, clearedIds]);
  const visibleRows = showAll || standalone ? filteredRows : filteredRows.slice(0, 10);
  const errors = [activitiesQuery, invocationsQuery, effectsQuery, commandsQuery]
    .filter((query) => query.isError).length;
  const pending = [goalQuery, activitiesQuery, invocationsQuery, effectsQuery, commandsQuery]
    .some((query) => query.isPending && query.fetchStatus === 'fetching');

  useEffect(() => {
    if (standalone && autoFollow && listRef.current) listRef.current.scrollTop = 0;
  }, [standalone, autoFollow, visibleRows[0]?.id]);

  return <section className={`wb-surface wb-live${standalone ? ' is-standalone' : ''}`} id="run-live" aria-labelledby="run-live-title">
    <header className="wb-live-header"><div><small>实时事件 + 自动补偿刷新</small><h2 id="run-live-title">现在正在发生什么</h2>
      <p>{standalone ? '独立窗口会持续更新模型轮次、工具请求、命令结果和异常。' : '把模型工作、执行步骤、命令和结果放到同一条时间线上。'}</p></div>
      <span className={streamState === 'live' && !pending ? 'wb-live-pulse' : 'wb-live-pulse is-loading'}><i/>{streamState === 'live' ? (pending ? '收到变化，正在更新' : '数据连接正常') : streamState === 'reconnecting' ? '正在重新连接数据' : '正在建立数据连接'}</span>
    </header>
    <div className={`wb-run-progress is-${runProgress.state}`} role="status"><i/><div><b>{runProgress.label}</b><span>{runProgress.message}</span></div><small>数据连接：{streamState === 'live' ? '正常' : '重连中'}</small></div>
    <details className="wb-live-guide"><summary>这里会展示哪些信息？</summary><div><p><b>模型工作：</b>展示模型主动写出的工作摘要和工具请求，不展示或编造私有推理。</p><p><b>命令行：</b>展示命令名称、状态和已封存证据；安全的逐行输出接口尚未接通，因此不会直接渲染未经脱敏的原始文件。</p></div></details>
    {standalone ? <div className="wb-live-toolbar"><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="筛选已加载的运行信息…" aria-label="筛选运行信息"/><button type="button" className={autoFollow ? 'active' : ''} onClick={() => setAutoFollow((value) => !value)}>{autoFollow ? '自动跟随：开' : '自动跟随：关'}</button><button type="button" onClick={() => setClearedIds(new Set(rows.map((row) => row.id)))}>清空当前窗口</button></div> : null}
    {!goalId ? <p className="observe-error">请先在上方选择一个目标。</p>
      : auth == null && !sessionQuery.isPending ? <p className="observe-error">登录后才能读取运行实况。</p>
      : errors === 4 ? <p className="observe-error">运行实况暂时无法读取，请检查控制服务和登录状态。</p>
      : rows.length === 0 && pending ? <div className="wb-live-empty"><b>正在读取运行记录…</b><span>第一次加载通常只需几秒。</span></div>
      : rows.length === 0 ? <div className="wb-live-empty"><b>这个目标还没有运行记录</b><span>启动后，规划、模型工作和命令结果会依次出现在这里。</span></div>
      : filteredRows.length === 0 ? <div className="wb-live-empty"><b>当前窗口没有匹配记录</b><span>清除筛选后可查看已加载内容；新记录仍会继续进入。</span></div>
      : <ol className="wb-live-list" ref={listRef}>{visibleRows.map((row) => <li key={row.id} className={`is-${row.tone}`}>
          <i className="wb-live-marker"/><div className="wb-live-card"><div className="wb-live-meta"><span>{sourceLabels[row.source]}</span><time>{formatTime(row.occurredAt)}</time></div>
            <h3>{row.title}</h3><p>{row.summary}</p><details><summary>查看技术详情</summary><p>{row.details}</p></details></div>
        </li>)}</ol>}
    {errors > 0 && errors < 4 ? <p className="wb-live-partial">有 {errors} 类明细暂时没有读到；已显示其余真实记录。</p> : null}
    {!standalone && filteredRows.length > 10 ? <button className="wb-live-more" type="button" onClick={() => setShowAll((value) => !value)}>{showAll ? '收起较早记录' : `再看 ${filteredRows.length - 10} 条较早记录`}</button> : null}
    <footer className="wb-live-boundary">单步成功只表示这一步完成；整个目标仍需独立验收和最终交付检查。</footer>
  </section>;
}
