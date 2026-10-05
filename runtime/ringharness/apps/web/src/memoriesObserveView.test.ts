import {describe, expect, it} from 'vitest';
import {
  countVerifiedMemories,
  memoriesCaption,
  memoryRowsFromList,
  memoryStatusCaption,
} from './memoriesObserveView.js';

describe('memoriesObserveView', () => {
  it('投影列表并强调 VERIFIED ≠ Goal DONE', () => {
    const rows = memoryRowsFromList([
      {
        id: 'm1',
        kind: 'fact',
        status: 'VERIFIED',
        statement: '端口固定为 58101',
        confidence_bp: 9000,
        source_evidence_ids: ['e1'],
      },
      {
        id: 'm2',
        kind: 'hypothesis',
        status: 'PROPOSED',
        statement: '可能是租约抖动',
        confidence_bp: 4000,
      },
    ]);
    expect(rows).toHaveLength(2);
    expect(countVerifiedMemories(rows)).toBe(1);
    const caption = memoriesCaption({
      rowCount: 2,
      verifiedCount: 1,
      kindFilter: null,
    });
    expect(caption).toContain('VERIFIED 1');
    expect(caption).toContain('≠ Goal DONE');
  });

  it('状态文案禁止冒充 DONE', () => {
    expect(memoryStatusCaption('VERIFIED')).toContain('≠ Goal DONE');
    expect(memoryStatusCaption('PROPOSED')).toContain('≠ DONE');
  });
});
