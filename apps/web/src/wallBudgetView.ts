/**
 * 墙钟预算只读投影：展示 Kernel 快照；耗尽 ≠ DONE。
 */
import type {GoalWallBudgetSnapshot} from '@ring/api-client';

export function wallBudgetCaption(snap: GoalWallBudgetSnapshot): string {
  if (snap.marks_goal_done) {
    return '异常：wall-budget 快照 marks_goal_done=true（协议应恒 false）；禁止当作 Goal DONE。';
  }
  if (snap.budget_usage_unknown) {
    return '墙钟用量 UNKNOWN：失败关闭视为耗尽风险；≠ Goal DONE，须核对 budget_usage。';
  }
  if (snap.budget_exhausted) {
    return `墙钟预算已耗尽（elapsed=${snap.elapsed_wall_seconds ?? '?'} / limit=${snap.wall_clock_limit_seconds ?? '?'}）；耗尽 ≠ Goal DONE。`;
  }
  return `墙钟剩余 ${snap.budget_remaining_wall_seconds ?? '?'}s（已用 ${snap.elapsed_wall_seconds ?? '?'} / 上限 ${snap.wall_clock_limit_seconds ?? '?'}）；只读投影 ≠ DONE。`;
}
