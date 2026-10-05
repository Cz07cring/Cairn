import {useEffect, useState} from 'react';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {getAuthSession, getLiveness} from '@ring/api-client';
import {AuthSessionBar} from './AuthSessionBar.js';
import {CreateGoalDraftPanel} from './CreateGoalDraftPanel.js';
import {StartGoalDraftPanel} from './StartGoalDraftPanel.js';
import {ObserveGoalPickerPanel} from './ObserveGoalPickerPanel.js';
import {QuarantineInboxPanel} from './QuarantineInboxPanel.js';
import {ApprovalsObservePanel} from './ApprovalsObservePanel.js';
import {EffectsObservePanel} from './EffectsObservePanel.js';
import {ActivitiesObservePanel} from './ActivitiesObservePanel.js';
import {TasksObservePanel} from './TasksObservePanel.js';
import {PlansObservePanel} from './PlansObservePanel.js';
import {MemoriesObservePanel} from './MemoriesObservePanel.js';
import {ModelInvocationsObservePanel} from './ModelInvocationsObservePanel.js';
import {TaskEvidenceObservePanel} from './TaskEvidenceObservePanel.js';
import {CommandsObservePanel} from './CommandsObservePanel.js';
import {GoalReviewObservePanel} from './GoalReviewObservePanel.js';
import {FinalizationObservePanel} from './FinalizationObservePanel.js';
import {ExecutionRecordsPage} from './ExecutionRecordsPage.js';
import {TaskGraphPanel} from './TaskGraphPanel.js';
import {CommandPalette} from './CommandPalette.js';
import {WorkbenchIcon, type WorkbenchIconName} from './WorkbenchIcon.js';
import {CurrentGoalSummary} from './CurrentGoalSummary.js';
import {RunLiveTimeline} from './RunLiveTimeline.js';
import {AgentChatPanel} from './AgentChatPanel.js';
import {isRunPage, parseWorkbenchHash, workbenchHash, type WorkbenchPage} from './workbenchNavigation.js';
import './workbench.css';
import './comfort.css';

type NavItem = {page: WorkbenchPage; label: string; icon: WorkbenchIconName; badge?: string};
const NAV_GROUPS: Array<{label: string; items: NavItem[]}> = [
  {label: '工作', items: [{page: 'overview', label: '今日总览', icon: 'home'}, {page: 'goals', label: '目标', icon: 'goal'}, {page: 'attention', label: '需要我处理', icon: 'alert', badge: '检查'}]},
  {label: '观察', items: [{page: 'runs', label: '执行记录', icon: 'runs'}, {page: 'run-evidence', label: '验收与交付', icon: 'check'}, {page: 'health', label: '系统健康', icon: 'health'}]},
];
const RUN_TABS: Array<{page: WorkbenchPage; label: string}> = [
  {page: 'run-overview', label: '概览'},
  {page: 'run-live', label: '执行交流'},
  {page: 'run-trace', label: '执行链路'},
  {page: 'run-logs', label: '日志'},
  {page: 'run-evidence', label: '证据与验收'},
];

function useWorkbenchPage(): WorkbenchPage {
  const [page, setPage] = useState(() => parseWorkbenchHash(location.hash));
  useEffect(() => {
    const onHashChange = () => setPage(parseWorkbenchHash(location.hash));
    addEventListener('hashchange', onHashChange);
    return () => removeEventListener('hashchange', onHashChange);
  }, []);
  return page;
}

function SideNavigation({page, collapsed, onToggle}: {page: WorkbenchPage; collapsed: boolean; onToggle: () => void}) {
  return <aside className="wb-side-nav">
    <a className="wb-brand" href={workbenchHash('overview')} aria-label="Ringharness 总览"><span className="wb-brand-mark">R</span><span><b>Ringharness</b><small>长程任务控制台</small></span></a>
    <div className="wb-workspace"><span className="wb-dot"/><span><b>个人工作区</b><small>开发环境 · v0.6</small></span><span>⌄</span></div>
    <nav aria-label="主导航">{NAV_GROUPS.map((group) => <div className="wb-nav-group" key={group.label}><p>{group.label}</p>{group.items.map((item) => {
      const active = item.page === page || (item.page === 'runs' && isRunPage(page));
      return <a key={item.page} href={workbenchHash(item.page)} title={collapsed ? item.label : undefined} aria-label={item.label} aria-current={active ? 'page' : undefined}><span className="wb-nav-icon"><WorkbenchIcon name={item.icon}/></span><span>{item.label}</span>{item.badge ? <small>{item.badge}</small> : null}</a>;
    })}</div>)}</nav>
    <div className="wb-authority"><span className="wb-dot"/><div><b>最终完成由系统裁决</b><small>执行成功不等于目标完成</small></div></div>
    <button className="wb-collapse" type="button" onClick={onToggle} aria-label={collapsed ? '展开侧栏' : '收起侧栏'}><WorkbenchIcon name="collapse"/>{collapsed ? null : <span>收起侧栏</span>}</button>
  </aside>;
}

function PageHeader({title, description, health, dark, onTheme, onRefresh, onCommand, refreshing}: {title: string; description: string; health: string; dark: boolean; onTheme: () => void; onRefresh: () => void; onCommand: () => void; refreshing: boolean}) {
  const label = health === 'online' ? '控制服务在线' : health === 'loading' ? '正在连接' : health === 'offline' ? '控制服务不可用' : '状态未知';
  return <header className="wb-page-header"><div><p>个人工作区 <span>/</span> {title}</p><h1>{title}</h1><span>{description}</span></div><div className="wb-header-actions"><button className="wb-command-trigger" type="button" onClick={onCommand}><WorkbenchIcon name="search"/><span>快速前往</span><kbd>⌘ K</kbd></button><button type="button" onClick={onRefresh} aria-label="刷新当前数据"><WorkbenchIcon name="refresh"/>{refreshing ? '刷新中…' : <span className="wb-action-label">刷新</span>}</button><button type="button" onClick={onTheme} aria-label={dark ? '切换浅色主题' : '切换深色主题'}><WorkbenchIcon name={dark ? 'sun' : 'moon'}/></button><div className={`wb-health wb-health-${health}`} role="status"><i/>{label}</div></div></header>;
}

function OverviewPage() {
  return <>
    <section className="wb-welcome"><div><small>无人值守任务工作台</small><h2>先看结论，再进入执行细节。</h2><p>这里集中显示正在做什么、是否走偏、哪里需要你，以及什么时候可以交付。</p></div><a href="#goals">创建或选择目标</a></section>
    <section className="wb-status-strip"><div><i className="running"/><b>执行事实</b><strong>持续观察</strong><small>模型执行 / 外部操作 / 持久编排</small></div><div><i className="audit"/><b>验收三态</b><strong>PASS · INSUFFICIENT · FAIL</strong><small>独立验收方判断</small></div><div><i className="done"/><b>目标完成</b><strong>仅最终裁决器可判定</strong><small>固定配置 + 最终屏障</small></div></section>
    <section className="wb-overview-grid"><CurrentGoalSummary/>
      <article className="wb-surface wb-attention"><small>需要关注</small><h2>只呈现需要判断的事项</h2><div><b>!</b><span><strong>隔离义务与审批</strong><p>UNKNOWN、停止未确认、证据不足会在这里汇总。</p></span></div><div className="calm"><b>✓</b><span><strong>系统不会自行放宽门禁</strong><p>缺少可信证据时保持阻塞，也不会翻译成完成。</p></span></div><a href="#attention">查看需要我处理 →</a></article></section>
    <section className="wb-surface wb-quick"><small>快速开始</small><h2>从目标到交付</h2><div><a href="#goals"><b>01</b><strong>定义目标</strong><span>写清成功标准、约束和预算</span></a><a href="#run-overview"><b>02</b><strong>观察执行</strong><span>定位任务、步骤和当前进展</span></a><a href="#run-evidence"><b>03</b><strong>核对交付</strong><span>查看审计三态和最终屏障</span></a></div></section>
  </>;
}

function RunHeader({page}: {page: WorkbenchPage}) {
  return <><section className="wb-run-hero"><div><small>三权分立 · 管理 / 执行腿 / 治理</small><h2>当前观察目标</h2><p>执行交流用本仓 Agent 聊天（类 Codex）；工具仍经 Broker→Kernel；DONE 只经 VerificationProfile。</p></div><div><span>● 等待实时状态</span><a className="wb-popout" href={workbenchHash('run-live')} target="_blank" rel="noreferrer">单独打开执行交流 ↗</a></div></section><nav className="wb-run-tabs" aria-label="执行详情">{RUN_TABS.map((tab) => <a key={tab.page} href={workbenchHash(tab.page)} aria-current={page === tab.page ? 'page' : undefined}>{tab.label}</a>)}</nav><ObserveGoalPickerPanel/></>;
}

function Context({kind = 'normal', title, children}: {kind?: 'normal'|'warning'|'evidence'; title: string; children: string}) {
  return <section className={`wb-context wb-context-${kind}`}><b>{title}</b><p>{children}</p></section>;
}

function RunPage({page}: {page: WorkbenchPage}) {
  return <><RunHeader page={page}/>
    {page === 'run-live' ? <>
      <AgentChatPanel/>
      <Context title="技术时间线（同事实，另一视图）">上方面板是 Agent 聊天投影；下方仍是 Kernel 权威时间线。不编造私有推理；聊完 ≠ Goal DONE。</Context>
      <RunLiveTimeline standalone/>
    </> : <RunLiveTimeline standalone={false}/>}
    {page === 'run-overview' ? <><Context title="任务图与步骤详情">根据任务合同中的前置关系展示真实步骤；点击任一步可在右侧查看详情。</Context><TaskGraphPanel/><PlansObservePanel/><ActivitiesObservePanel/></> : null}
    {page === 'run-trace' ? <><Context title="执行链路">模型工作、执行步骤和外部操作按来源分开显示。执行链路只说明发生过什么。</Context><ModelInvocationsObservePanel/><ActivitiesObservePanel/><EffectsObservePanel/><MemoriesObservePanel/></> : null}
    {page === 'run-logs' ? <><Context title="日志与事件">实时窗口会接收目标变化并刷新命令结果；退出码 0 或工具成功不代表验收通过。</Context><CommandsObservePanel/></> : null}
    {page === 'run-evidence' ? <><Context kind="evidence" title="证据完整性、来源可信度、验收正确性分开判断。">Hash 一致只证明内容未变；三种验收结论和最终交付检查共同决定能否交付。</Context><TaskEvidenceObservePanel/><GoalReviewObservePanel/><FinalizationObservePanel/></> : null}
  </>;
}

function HealthPage({health}: {health: string}) {
  return <><section className="wb-health-grid"><article className="wb-surface"><small>控制服务</small><h2>{health === 'online' ? '在线' : '尚未确认在线'}</h2><p>当前只确认服务可以响应，还不代表所有功能都可用。</p></article><article className="wb-surface"><small>执行引擎</small><h2>待真实运行核对</h2><p>模型连通或页面可见不等于完整执行流程。</p></article><article className="wb-surface"><small>100 小时验收</small><h2>尚未完成</h2><p>长程稳定性必须由独立验收结果更新。</p></article></section><section className="wb-surface wb-delivery"><h2>当前交付边界</h2><p><b>已实现</b> 项目管理、登录、目标草稿、启动入口和只读进度查看。</p><p><b>部分</b> 长任务编排、模型执行、安全恢复和交付检查仍在持续完善。</p><p><b>待验收</b> 真实 E2E、默认 live CI、100 小时无人值守。</p></section></>;
}

export function WorkbenchApp() {
  const page = useWorkbenchPage();
  const queryClient = useQueryClient();
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('ringharness.sidebar-collapsed') === 'true');
  const [dark, setDark] = useState(() => localStorage.getItem('ringharness.theme') === 'dark');
  const [refreshing, setRefreshing] = useState(false);
  const [commandOpen, setCommandOpen] = useState(false);
  const healthQuery = useQuery({queryKey: ['public-liveness'], queryFn: ({signal}) => getLiveness(signal), refetchInterval: 15_000});
  useQuery({queryKey: ['auth-session'], queryFn: ({signal}) => getAuthSession({signal}), retry: false, refetchInterval: 60_000});
  const health = healthQuery.isPending ? 'loading' : healthQuery.isError ? 'offline' : healthQuery.data?.alive ? 'online' : 'unknown';
  const meta: Record<WorkbenchPage, [string,string]> = {overview:['今日总览','先看进展、风险和下一步，再进入技术细节。'],goals:['目标','创建、启动并选择要查看的目标。'],attention:['需要我处理','集中处理阻断、隔离、审批和补证据事项。'],runs:['执行记录','像查看工作台一样筛选每次目标运行，并进入完整详情。'],'run-overview':['执行详情','在同一上下文查看任务、执行链路、日志和证据。'],'run-live':['执行交流','本仓 Agent 聊天跟流（类 Codex）；模型摘要与工具回执；聊完 ≠ Goal DONE。'],'run-trace':['执行详情','在同一上下文查看任务、执行链路、日志和证据。'],'run-logs':['执行详情','在同一上下文查看任务、执行链路、日志和证据。'],'run-evidence':['验收与交付','核对验收材料、三种验收结论和最终交付结果。'],health:['系统健康','区分进程存活、能力可用和业务验收。']};
  const toggleCollapsed = () => setCollapsed((value) => {localStorage.setItem('ringharness.sidebar-collapsed', String(!value)); return !value;});
  const toggleTheme = () => setDark((value) => {localStorage.setItem('ringharness.theme', value ? 'light' : 'dark'); return !value;});
  const refresh = async () => {setRefreshing(true); try {await queryClient.invalidateQueries();} finally {setTimeout(() => setRefreshing(false), 250);}};
  useEffect(() => {
    const openCommand = (event: KeyboardEvent) => {if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === 'k') {event.preventDefault(); setCommandOpen(true);}};
    addEventListener('keydown', openCommand);
    return () => removeEventListener('keydown', openCommand);
  }, []);
  return <div className={`wb-shell${collapsed ? ' wb-collapsed' : ''}`} data-theme={dark ? 'dark' : 'light'}><SideNavigation page={page} collapsed={collapsed} onToggle={toggleCollapsed}/><main className={`wb-main wb-page-${page}`}><PageHeader title={meta[page][0]} description={meta[page][1]} health={health} dark={dark} onTheme={toggleTheme} onRefresh={() => void refresh()} onCommand={() => setCommandOpen(true)} refreshing={refreshing}/><AuthSessionBar/>{page === 'overview' ? <OverviewPage/> : null}{page === 'goals' ? <><CreateGoalDraftPanel/><StartGoalDraftPanel/><ObserveGoalPickerPanel/><TasksObservePanel/><PlansObservePanel/></> : null}{page === 'attention' ? <><Context kind="warning" title="这里只汇总需要人工判断的事实。">解除阻塞或接受命令只表示请求被受理，必须等待权威状态回读。</Context><QuarantineInboxPanel/><ApprovalsObservePanel/><FinalizationObservePanel/></> : null}{page === 'runs' ? <ExecutionRecordsPage/> : null}{isRunPage(page) ? <RunPage page={page}/> : null}{page === 'health' ? <HealthPage health={health}/> : null}<footer>页面只展示系统确认过的事实；已受理、局部完成、已经送达，都不代表目标通过最终验收。</footer></main><CommandPalette open={commandOpen} onClose={() => setCommandOpen(false)}/></div>;
}
