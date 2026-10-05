import {useMemo} from 'react';
import {useQuery} from '@tanstack/react-query';
import {getAuthSession, getGoal, listGoalActivities, listGoalTasks, type BrowserClientAuth} from '@ring/api-client';
import {readStoredObserveGoalId} from './createGoalDraft.js';
import {goalStatusLabel, taskProgress} from './goalProgressSummary.js';
import {assessRunProgress} from './runLiveTimelineView.js';

export function CurrentGoalSummary() {
  const session = useQuery({queryKey: ['auth-session'], queryFn: ({signal}) => getAuthSession({signal}), retry: false});
  const auth: BrowserClientAuth | null = useMemo(() => {
    if (session.data) return {kind: 'session', csrfToken: session.data.csrf_token};
    const bearer = String(import.meta.env.VITE_RING_DEV_BEARER ?? '').trim();
    return bearer ? {kind: 'bearer', authorization: bearer} : null;
  }, [session.data]);
  const configuredGoalId = String(import.meta.env.VITE_RING_GOAL_ID ?? '').trim();
  const goalId = configuredGoalId || readStoredObserveGoalId();
  const goal = useQuery({queryKey: ['overview-goal', goalId, auth?.kind], enabled: auth != null && Boolean(goalId), queryFn: ({signal}) => getGoal({auth: auth!, goalId, signal}), refetchInterval: 5_000});
  const tasks = useQuery({queryKey: ['overview-goal-tasks', goalId, auth?.kind], enabled: auth != null && Boolean(goalId), queryFn: ({signal}) => listGoalTasks({auth: auth!, goalId, signal}), refetchInterval: 5_000});
  const activities = useQuery({queryKey: ['overview-goal-activities', goalId, auth?.kind], enabled: auth != null && Boolean(goalId), queryFn: ({signal}) => listGoalActivities({auth: auth!, goalId, signal}), refetchInterval: 5_000});

  if (!auth || !goalId) return <article className="wb-surface wb-now"><header><div><small>当前工作</small><h2>选择一个目标开始观察</h2></div><span>等待选择</span></header><p>选择目标后，这里会显示任务推进、阻断和验收状态。</p><a href="#goals">创建或选择目标</a></article>;
  if (goal.isPending || tasks.isPending || activities.isPending) return <article className="wb-surface wb-now"><header><div><small>当前工作</small><h2>正在读取真实进度…</h2></div><span>读取中</span></header><p>正在从控制服务核对 Goal、Task 和执行步骤的权威状态。</p></article>;
  if (goal.isError || tasks.isError || activities.isError || !goal.data) return <article className="wb-surface wb-now"><header><div><small>当前工作</small><h2>暂时无法读取目标</h2></div><span>需要检查</span></header><p>身份、目标范围或控制服务不可用；页面不会用演示数据代替。</p><a href="#goals">重新选择目标</a></article>;
  const progress = taskProgress(tasks.data ?? []);
  const runProgress = assessRunProgress(goal.data.status, activities.data ?? []);
  return <article className="wb-surface wb-now"><header><div><small>当前工作 · {goal.data.id.slice(0, 8)}…</small><h2>{goal.data.contract.objective}</h2></div><span>{runProgress.state === 'stalled' ? runProgress.label : goalStatusLabel(goal.data.status)}</span></header><p>真实任务：共 {progress.total} 个，状态表中 {progress.active} 个标记处理中，{progress.done} 个已完成，{progress.attention} 个需要处理。</p>{runProgress.state === 'stalled' ? <p className="wb-now-alert">{runProgress.message}</p> : null}<div className="wb-journey"><div className={goal.data.status === 'PLANNING' ? 'active' : ''}><b>1</b><span>规划</span></div><div className={goal.data.status === 'RUNNING' ? 'active' : ''}><b>2</b><span>执行</span></div><div className={goal.data.status === 'VERIFYING' ? 'active' : ''}><b>3</b><span>验收</span></div><div className={goal.data.status === 'DONE' ? 'active' : ''}><b>4</b><span>交付</span></div></div><a href="#run-overview">查看任务流程与实时进度</a></article>;
}
