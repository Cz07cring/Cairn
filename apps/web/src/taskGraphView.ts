import type {TaskResource} from '@ring/api-client';

/** 按真实 depends_on 关系分层；循环或缺失依赖的任务放在最后一层，避免界面卡死。 */
export function buildTaskColumns(tasks: TaskResource[]): TaskResource[][] {
  const byId = new Map(tasks.map((task) => [task.id, task]));
  const levels = new Map<string, number>();
  const visiting = new Set<string>();
  const levelOf = (task: TaskResource): number => {
    const cached = levels.get(task.id);
    if (cached != null) return cached;
    if (visiting.has(task.id)) return tasks.length;
    visiting.add(task.id);
    const dependencies = (task.contract.depends_on ?? []).map((id) => byId.get(id)).filter((item): item is TaskResource => item != null);
    const level = dependencies.length ? Math.max(...dependencies.map(levelOf)) + 1 : 0;
    visiting.delete(task.id);
    levels.set(task.id, level);
    return level;
  };
  tasks.forEach(levelOf);
  const columns: TaskResource[][] = [];
  tasks.forEach((task) => {
    const level = Math.min(levels.get(task.id) ?? 0, tasks.length);
    (columns[level] ??= []).push(task);
  });
  return columns.filter(Boolean);
}
