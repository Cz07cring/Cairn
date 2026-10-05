import {describe, expect, it} from 'vitest';
import {parseSseBlock} from '@ring/api-client';
import {
  goalEventsCaption,
  shouldInvalidateFromEvent,
  toGoalEventRow,
} from './goalEventsView.js';

describe('goalEventsView', () => {
  it('解析 SSE 帧并对齐 id/event', () => {
    const event = parseSseBlock(
      [
        'id: 1',
        'event: GOAL_STATE_CHANGED',
        'data: {"seq":"1","type":"GOAL_STATE_CHANGED","payload":{"resource_type":"GOAL","change":"INVALIDATE"}}',
      ].join('\n'),
    );
    expect(event?.seq).toBe('1');
    expect(shouldInvalidateFromEvent(event!)).toBe(true);
    expect(toGoalEventRow(event!).change).toBe('INVALIDATE');
  });

  it('文案不把事件当成 DONE', () => {
    expect(
      goalEventsCaption({
        latestSeq: '3',
        cursorExpired: false,
        stale: false,
        eventCount: 2,
      }),
    ).toContain('≠ DONE');
    expect(
      goalEventsCaption({
        latestSeq: '0',
        cursorExpired: true,
        stale: false,
        eventCount: 0,
      }),
    ).toContain('410');
  });

  it('忽略 heartbeat 注释块', () => {
    expect(parseSseBlock(': heartbeat')).toBeNull();
  });
});
