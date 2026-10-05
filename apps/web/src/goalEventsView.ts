/**
 * Goal 事件 SSE 投影：INVALIDATE 仅提示重读；事件到达 ≠ 工具再执行 ≠ DONE。
 */
import type {GoalSseEvent} from '@ring/api-client';

export type GoalEventRow = {
  seq: string;
  type: string;
  change: string;
  resourceType: string;
};

export function toGoalEventRow(event: GoalSseEvent): GoalEventRow {
  return {
    seq: event.seq,
    type: event.type,
    change: String(event.payload?.change ?? '—'),
    resourceType: String(event.payload?.resource_type ?? '—'),
  };
}

export function goalEventsCaption(input: {
  latestSeq: string | null;
  cursorExpired: boolean;
  stale: boolean;
  eventCount: number;
}): string {
  if (input.cursorExpired) {
    return '事件游标失效（410）：须重取 snapshot 再订阅；勿用旧 seq 猜状态。≠ DONE。';
  }
  if (input.stale) {
    return '事件流短暂无更新（stale）：可继续重连；stale ≠ 失败 ≠ DONE。';
  }
  if (input.eventCount === 0) {
    return `已对齐 latest_seq=${input.latestSeq ?? '—'}；暂无新 INVALIDATE。订阅 ≠ DONE。`;
  }
  return `已收 ${input.eventCount} 条事件（latest 观察至 ${input.latestSeq ?? '—'}）。INVALIDATE 只触发重读，≠ 工具再执行 ≠ DONE。`;
}

/** 是否应按 doc/04 触发 snapshot 重读（仅 INVALIDATE）。 */
export function shouldInvalidateFromEvent(event: GoalSseEvent): boolean {
  return event.payload?.change === 'INVALIDATE';
}
