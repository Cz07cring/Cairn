import {useEffect, useMemo, useState} from 'react';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {getAuthSession, listGoals, listProjects, type BrowserClientAuth, type GoalResource} from '@ring/api-client';
import {writeStoredObserveGoalId} from './createGoalDraft.js';
import {countExecutionStatuses, filterExecutionRecords, goalStatusLabel, paginateExecutionRecords, type GoalStatusFilter} from './executionRecordsView.js';
import {WorkbenchIcon} from './WorkbenchIcon.js';

const FILTERS: Array<{value: GoalStatusFilter; label: string}> = [
  {value: 'ALL', label: '全部'}, {value: 'ACTIVE', label: '执行中'}, {value: 'VERIFYING', label: '正在验收'},
  {value: 'ATTENTION', label: '需要处理'}, {value: 'FINISHED', label: '已结束'},
];
type ColumnName = 'stage' | 'updated';

function shortTime(value: string | null): string {
  if (!value) return '暂无记录';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'});
}

export function ExecutionRecordsPage() {
  const queryClient = useQueryClient();
  const [projectId, setProjectId] = useState('');
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<GoalStatusFilter>('ALL');
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [columnMenu, setColumnMenu] = useState(false);
  const [columns, setColumns] = useState<Record<ColumnName, boolean>>({stage: true, updated: true});
  const session = useQuery({queryKey: ['auth-session'], queryFn: ({signal}) => getAuthSession({signal}), retry: false});
  const auth: BrowserClientAuth | null = useMemo(() => {
    if (session.data) return {kind: 'session', csrfToken: session.data.csrf_token};
    const bearer = String(import.meta.env.VITE_RING_DEV_BEARER ?? '').trim();
    return bearer ? {kind: 'bearer', authorization: bearer} : null;
  }, [session.data]);
  const projects = useQuery({queryKey: ['projects', auth?.kind], enabled: auth != null, queryFn: ({signal}) => listProjects({auth: auth!, signal})});
  const resolvedProject = projectId || projects.data?.[0]?.id || String(import.meta.env.VITE_RING_PROJECT_ID ?? '').trim();
  const goals = useQuery({queryKey: ['execution-records', resolvedProject, auth?.kind], enabled: auth != null && Boolean(resolvedProject), queryFn: ({signal}) => listGoals({auth: auth!, projectId: resolvedProject, signal}), refetchInterval: 10_000});
  const allRows = goals.data ?? [];
  const rows = filterExecutionRecords(allRows, search, status);
  const pageSize = 10;
  const totalPages = Math.max(1, Math.ceil(rows.length / pageSize));
  const visibleRows = paginateExecutionRecords(rows, page, pageSize);
  const counts = countExecutionStatuses(allRows);
  useEffect(() => setPage(1), [search, status, resolvedProject]);
  useEffect(() => setPage((value) => Math.min(value, totalPages)), [totalPages]);
  const open = (row: GoalResource) => {writeStoredObserveGoalId(row.id); void queryClient.invalidateQueries({queryKey: ['observe-goal-id']}); location.hash = 'run-overview';};
  const toggleSelected = (id: string) => setSelected((current) => {const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next;});
  const allVisibleSelected = visibleRows.length > 0 && visibleRows.every((row) => selected.has(row.id));
  const toggleAllVisible = () => setSelected((current) => {const next = new Set(current); if (allVisibleSelected) visibleRows.forEach((row) => next.delete(row.id)); else visibleRows.forEach((row) => next.add(row.id)); return next;});

  if (auth == null && !session.isPending) return <section className="wb-records-empty"><span><WorkbenchIcon name="runs"/></span><h2>登录后查看执行记录</h2><p>每个目标的状态、更新时间和需要处理的问题会集中显示在这里。</p></section>;
  return <section className="wb-records">
    <div className="wb-counts">
      <button onClick={() => setStatus('ALL')} className={status === 'ALL' ? 'active' : ''}><span>全部记录</span><b>{allRows.length}</b></button>
      <button onClick={() => setStatus('ACTIVE')} className={status === 'ACTIVE' ? 'active' : ''}><span>执行中</span><b>{counts.running}</b></button>
      <button onClick={() => setStatus('VERIFYING')} className={status === 'VERIFYING' ? 'active' : ''}><span>正在验收</span><b>{counts.verifying}</b></button>
      <button onClick={() => setStatus('ATTENTION')} className={status === 'ATTENTION' ? 'active' : ''}><span>需要处理</span><b>{counts.attention}</b></button>
      <button onClick={() => setStatus('FINISHED')} className={status === 'FINISHED' ? 'active' : ''}><span>已交付</span><b>{counts.delivered}</b></button>
    </div>
    <div className="wb-record-toolbar">
      <div className="wb-search"><WorkbenchIcon name="search"/><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索目标名称或编号" aria-label="搜索执行记录"/></div>
      <select value={resolvedProject} onChange={(event) => setProjectId(event.target.value)} aria-label="选择项目">{(projects.data ?? []).map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select>
      <div className="wb-filter-chips" aria-label="按状态筛选">{FILTERS.map((filter) => <button key={filter.value} className={status === filter.value ? 'active' : ''} onClick={() => setStatus(filter.value)}>{filter.label}</button>)}</div>
      <div className="wb-column-control"><button type="button" aria-expanded={columnMenu} onClick={() => setColumnMenu((value) => !value)}><WorkbenchIcon name="columns"/><span>显示内容</span></button>{columnMenu ? <div className="wb-column-menu"><b>表格显示内容</b>{([['stage', '当前阶段'], ['updated', '最后更新']] as Array<[ColumnName,string]>).map(([name, label]) => <label key={name}><input type="checkbox" checked={columns[name]} onChange={() => setColumns((current) => ({...current, [name]: !current[name]}))}/>{label}</label>)}</div> : null}</div>
    </div>
    {selected.size ? <div className="wb-selection-bar"><b>已选择 {selected.size} 条记录</b><span>批量操作需后端命令与权限检查，当前保持只读。</span><button onClick={() => setSelected(new Set())}>清除选择</button></div> : null}
    {goals.isPending ? <div className="wb-records-empty"><span className="wb-spinner"/><h2>正在读取执行记录</h2></div> : goals.isError ? <div className="wb-records-empty error"><span><WorkbenchIcon name="alert"/></span><h2>暂时无法读取执行记录</h2><p>{goals.error instanceof Error ? goals.error.message : '请稍后刷新'}</p></div> : rows.length === 0 ? <div className="wb-records-empty"><span><WorkbenchIcon name="search"/></span><h2>{allRows.length ? '没有符合筛选条件的记录' : '还没有执行记录'}</h2><p>{allRows.length ? '调整搜索词或状态筛选后再试。' : '创建并启动目标后，记录会出现在这里。'}</p></div> : <div className={`wb-record-table${!columns.stage ? ' hide-stage' : ''}${!columns.updated ? ' hide-updated' : ''}`} role="table" aria-label="执行记录">
      <div className="wb-record-head" role="row"><span><input type="checkbox" checked={allVisibleSelected} onChange={toggleAllVisible} aria-label="选择当前页全部记录"/></span><span>目标</span><span>状态</span><span className="column-stage">当前阶段</span><span className="column-updated">最后更新</span><span/></div>
      {visibleRows.map((row) => <div className="wb-record-row" role="row" key={row.id}><span><input type="checkbox" checked={selected.has(row.id)} onChange={() => toggleSelected(row.id)} aria-label={`选择 ${row.contract.objective}`}/></span><button onClick={() => open(row)}><span><strong>{row.contract.objective || '未命名目标'}</strong><small>{row.id.slice(0, 8)}…</small></span><span><i className={`status-${row.status.toLowerCase()}`}/>{goalStatusLabel(row.status)}</span><span className="column-stage">{row.block_reason ? '等待解决问题' : row.plan_revision ? `计划第 ${row.plan_revision} 版` : '等待形成计划'}</span><span className="column-updated">{shortTime(row.updated_at)}</span><span><WorkbenchIcon name="chevron"/></span></button></div>)}
    </div>}
    {rows.length ? <footer className="wb-pagination"><span>显示 {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, rows.length)}，共 {rows.length} 条</span><div><button disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>上一页</button><b>{page} / {totalPages}</b><button disabled={page >= totalPages} onClick={() => setPage((value) => value + 1)}>下一页</button></div></footer> : null}
  </section>;
}
