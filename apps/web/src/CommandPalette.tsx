import {useEffect, useRef, useState} from 'react';
import {WorkbenchIcon} from './WorkbenchIcon.js';
import {workbenchHash, type WorkbenchPage} from './workbenchNavigation.js';

const ACTIONS: Array<{page: WorkbenchPage; label: string; hint: string}> = [
  {page: 'overview', label: '回到今日总览', hint: '查看进展和风险'},
  {page: 'goals', label: '创建或选择目标', hint: '定义要完成什么'},
  {page: 'attention', label: '查看需要我处理', hint: '解决阻断和补证据'},
  {page: 'runs', label: '搜索执行记录', hint: '定位历史和正在运行的目标'},
  {page: 'run-overview', label: '打开任务流程', hint: '查看任务依赖和当前步骤'},
  {page: 'run-live', label: '打开执行交流', hint: 'Agent 聊天跟流 · 模型与工具'},
  {page: 'run-trace', label: '打开执行链路', hint: '查看模型、工具和外部操作'},
  {page: 'run-logs', label: '打开日志', hint: '查看命令输出和事件'},
  {page: 'run-evidence', label: '打开验收与交付', hint: '查看三态和最终屏障'},
  {page: 'health', label: '检查系统健康', hint: '区分在线、可用和已验收'},
];

export function CommandPalette({open, onClose}: {open: boolean; onClose: () => void}) {
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!open) return;
    setQuery('');
    setActiveIndex(0);
    requestAnimationFrame(() => inputRef.current?.focus());
    const close = (event: KeyboardEvent) => {if (event.key === 'Escape') onClose();};
    addEventListener('keydown', close);
    return () => removeEventListener('keydown', close);
  }, [open, onClose]);
  if (!open) return null;
  const needle = query.trim().toLocaleLowerCase();
  const rows = ACTIONS.filter((item) => `${item.label} ${item.hint}`.toLocaleLowerCase().includes(needle));
  const go = (page: WorkbenchPage) => {location.hash = workbenchHash(page); onClose();};
  return <div className="wb-command-backdrop" role="presentation" onMouseDown={(event) => {if (event.target === event.currentTarget) onClose();}}>
    <section className="wb-command-dialog" role="dialog" aria-modal="true" aria-label="快速前往">
      <header><WorkbenchIcon name="search"/><input ref={inputRef} value={query} onChange={(event) => {setQuery(event.target.value);setActiveIndex(0);}} onKeyDown={(event) => {if(event.key==='ArrowDown'){event.preventDefault();setActiveIndex((value)=>Math.min(value+1,rows.length-1));}if(event.key==='ArrowUp'){event.preventDefault();setActiveIndex((value)=>Math.max(value-1,0));}if(event.key==='Enter'&&rows[activeIndex])go(rows[activeIndex].page);}} placeholder="搜索页面或要做的事…" aria-label="搜索页面或操作"/><kbd>ESC</kbd></header>
      <div className="wb-command-results"><small>快速前往</small>{rows.length ? rows.map((item, index) => <button key={item.page} className={index===activeIndex?'active':''} onMouseEnter={()=>setActiveIndex(index)} onClick={() => go(item.page)}><span><WorkbenchIcon name={item.page === 'goals' ? 'goal' : item.page === 'attention' ? 'alert' : item.page === 'health' ? 'health' : item.page === 'overview' ? 'home' : 'runs'}/></span><b>{item.label}<small>{item.hint}</small></b><kbd>{index + 1}</kbd></button>) : <p>没有找到匹配入口</p>}</div>
      <footer><span><kbd>↑</kbd><kbd>↓</kbd> 浏览</span><span><kbd>↵</kbd> 打开</span><span>不会绕过任何权限或验收门禁</span></footer>
    </section>
  </div>;
}
