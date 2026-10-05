import {useEffect, useMemo, useRef, useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {getAuthSession, listGoalTasks, type BrowserClientAuth, type TaskResource} from '@ring/api-client';
import {readStoredObserveGoalId} from './createGoalDraft.js';
import {readWorkbenchParam, withWorkbenchParam} from './workbenchNavigation.js';
import {buildTaskColumns} from './taskGraphView.js';
import {WorkbenchIcon} from './WorkbenchIcon.js';

const STATUS: Record<TaskResource['status'], string> = {PENDING:'等待前置步骤',READY:'可以开始',RUNNING:'正在执行',VERIFYING:'正在验收',RETRYING:'正在重试',BLOCKED:'等待处理',STALE:'信息已过期',RECOVERING:'正在安全恢复',DONE:'本步骤已完成',FAILED:'执行失败',CANCELLED:'已停止'};
type InspectorTab = 'summary' | 'input' | 'acceptance' | 'output';

export function TaskGraphPanel() {
  const session = useQuery({queryKey:['auth-session'], queryFn:({signal})=>getAuthSession({signal}), retry:false});
  const auth: BrowserClientAuth | null = useMemo(() => {
    if (session.data) return {kind:'session', csrfToken:session.data.csrf_token};
    const bearer=String(import.meta.env.VITE_RING_DEV_BEARER ?? '').trim();
    return bearer ? {kind:'bearer', authorization:bearer} : null;
  },[session.data]);
  const goalId=readStoredObserveGoalId() || String(import.meta.env.VITE_RING_GOAL_ID ?? '').trim();
  const tasks=useQuery({queryKey:['goal-task-graph',goalId,auth?.kind],enabled:auth!=null&&Boolean(goalId),queryFn:({signal})=>listGoalTasks({auth:auth!,goalId,signal}),refetchInterval:5000});
  const [selectedId,setSelectedId]=useState(()=>readWorkbenchParam(location.hash,'task'));
  const [tab,setTab]=useState<InspectorTab>('summary');
  const [compact,setCompact]=useState(false);
  const closeButtonRef=useRef<HTMLButtonElement>(null);
  useEffect(()=>{const sync=()=>setSelectedId(readWorkbenchParam(location.hash,'task'));addEventListener('hashchange',sync);return()=>removeEventListener('hashchange',sync);},[]);
  useEffect(()=>{if(selectedId){setTab('summary');requestAnimationFrame(()=>closeButtonRef.current?.focus());}},[selectedId]);
  useEffect(()=>{const close=(event:KeyboardEvent)=>{if(event.key==='Escape'&&selectedId)location.hash=withWorkbenchParam('run-overview','task');};addEventListener('keydown',close);return()=>removeEventListener('keydown',close);},[selectedId]);
  const rows=tasks.data??[];
  const columns=buildTaskColumns(rows);
  const selected=rows.find((task)=>task.id===selectedId)??null;
  const choose=(id?:string)=>{location.hash=withWorkbenchParam('run-overview','task',id);};
  if(auth==null&&!session.isPending)return <section className="wb-graph-empty"><h2>登录后查看任务流程</h2><p>选中任一步即可在右侧查看输入、产物、验收标准和重试信息。</p></section>;
  if(!goalId)return <section className="wb-graph-empty"><h2>先选择一个目标</h2><p>选择目标后，这里会显示真实任务关系和每一步的当前状态。</p></section>;
  if(tasks.isPending)return <section className="wb-graph-empty"><span className="wb-spinner"/><h2>正在读取任务流程…</h2></section>;
  if(tasks.isError)return <section className="wb-graph-empty error"><h2>暂时无法读取任务流程</h2><p>{tasks.error instanceof Error?tasks.error.message:'请稍后刷新'}</p></section>;
  if(!rows.length)return <section className="wb-graph-empty"><h2>计划还没有拆出任务步骤</h2><p>空列表不代表目标已经完成。</p></section>;
  return <section className={`wb-graph-shell${selected?' has-inspector':''}${compact?' compact':''}`}>
    <div className="wb-graph-canvas">
      <header><div><small>任务关系图</small><h2>系统正在按这些步骤工作</h2><p>从左到右表示真实前置关系；点击步骤查看执行详情。</p></div><div><span>{rows.length} 个步骤</span><button onClick={()=>setCompact((value)=>!value)} aria-label={compact?'展开任务卡片':'缩小任务卡片'}><WorkbenchIcon name={compact?'expand':'collapse'}/>{compact?'展开':'缩略'}</button></div></header>
      <div className="wb-graph-columns">{columns.map((column,columnIndex)=><div className="wb-graph-column" key={columnIndex}><small>第 {columnIndex+1} 阶段</small>{column.map((task,index)=><button key={task.id} className={`wb-task-node status-${task.status.toLowerCase()}${selectedId===task.id?' selected':''}`} onClick={()=>choose(task.id)}><span className="wb-node-index">{String(index+1).padStart(2,'0')}</span><span><strong>{task.contract.objective}</strong><small>{STATUS[task.status]}</small></span><i/><footer>{(task.contract.depends_on?.length ?? 0)>0?<span>{task.contract.depends_on?.length} 个前置步骤</span>:<span>起始步骤</span>}<WorkbenchIcon name="chevron"/></footer></button>)}</div>)}</div>
    </div>
    {selected?<aside className="wb-inspector" aria-label="任务步骤详情"><header><div><small>步骤详情</small><h2>{selected.contract.objective}</h2><p>{selected.id.slice(0,8)}… · 第 {selected.execution_round} 轮</p></div><button ref={closeButtonRef} onClick={()=>choose()} aria-label="关闭步骤详情"><WorkbenchIcon name="close"/></button></header>
      <div className="wb-inspector-status"><i className={`status-${selected.status.toLowerCase()}`}/><span><small>当前状态</small><b>{STATUS[selected.status]}</b></span></div>
      <nav className="wb-inspector-tabs" aria-label="步骤信息">{([['summary','概况'],['input','输入'],['acceptance','验收'],['output','交付']] as Array<[InspectorTab,string]>).map(([value,label])=><button key={value} className={tab===value?'active':''} onClick={()=>setTab(value)}>{label}</button>)}</nav>
      {tab==='summary'?<><dl><div><dt>风险程度</dt><dd>{selected.contract.risk==='high'?'高':selected.contract.risk==='medium'?'中':'低'}</dd></div><div><dt>计划版本</dt><dd>第 {selected.plan_revision} 版</dd></div><div><dt>前置步骤</dt><dd>{selected.contract.depends_on?.length??0} 个</dd></div><div><dt>验收标准</dt><dd>{selected.contract.acceptance.length} 项</dd></div></dl>{selected.block_reason?<div className="wb-inspector-alert"><b>为什么停住</b><p>{selected.block_reason}</p></div>:<div className="wb-inspector-ok"><WorkbenchIcon name="check"/><span>当前没有记录阻断原因</span></div>}</>:null}
      {tab==='input'?<section><h3>执行需要什么</h3>{selected.contract.input_artifact_ids?.length?<ul>{selected.contract.input_artifact_ids.map((id)=><li key={id}>输入材料 {id.slice(0,8)}…</li>)}</ul>:<p>任务合同没有单独列出输入材料。</p>}<h3>允许使用的范围</h3>{selected.contract.allowed_paths.length?<ul>{selected.contract.allowed_paths.map((path)=><li key={path}>{path}</li>)}</ul>:<p>没有开放文件路径。</p>}</section>:null}
      {tab==='acceptance'?<section><h3>怎样才算通过</h3>{selected.contract.acceptance.length?<ol>{selected.contract.acceptance.map((item)=><li key={item.id}>{item.description}{item.required?'（必须）':'（可选）'}</li>)}</ol>:<p>任务合同没有验收标准，不能据此判定完成。</p>}</section>:null}
      {tab==='output'?<section><h3>要交付什么</h3>{selected.contract.deliverables.length?<ul>{selected.contract.deliverables.map((item,index)=><li key={`${item.kind}-${index}`}>{item.kind}{item.required?'（必须）':'（可选）'}</li>)}</ul>:<p>没有单独列出交付项。</p>}</section>:null}
      <details className="wb-tech"><summary>查看技术详情</summary><p>task {selected.id}<br/>contract rev {selected.contract_revision}<br/>state rev {selected.state_revision}<br/>lineage {selected.work_lineage_id}</p></details>
    </aside>:null}
  </section>;
}
