import {expect, test} from 'vitest';
import {
  assertSerialOnly,
  classifyToolCallConcurrency,
} from './toolConcurrencyClass.js';

test('AB07：READ_ONLY 标签仍判定 SERIAL，不授 PARALLEL', () => {
  const d = classifyToolCallConcurrency({
    toolRef: 'read_file',
    argumentsJson: JSON.stringify({path: 'src/shared.ts'}),
    replayClass: 'READ_ONLY',
  });
  expect(d).toEqual({
    class: 'SERIAL',
    resourceKeys: ['path:src/shared.ts'],
    reason: 'READ_ONLY_LABEL_DOES_NOT_GRANT_PARALLEL',
    marksGoalDone: false,
  });
  expect(() => assertSerialOnly(d)).not.toThrow();
});

test('AB07：两读同 path 的 resourceKeys 相同，分类仍各自 SERIAL', () => {
  const a = classifyToolCallConcurrency({
    toolRef: 'read_file',
    argumentsJson: '{"path":"var/x"}',
    replayClass: 'READ_ONLY',
  });
  const b = classifyToolCallConcurrency({
    toolRef: 'read_file',
    argumentsJson: '{"path":"var/x"}',
    replayClass: 'READ_ONLY',
  });
  expect(a.resourceKeys).toEqual(b.resourceKeys);
  expect(a.class).toBe('SERIAL');
  expect(b.class).toBe('SERIAL');
});

test('AB07：ToolPayload 规范化后仍提取 path 资源键', () => {
  const d = classifyToolCallConcurrency({
    toolRef: 'read_file',
    argumentsJson: JSON.stringify({
      tool_ref: 'read_file',
      tool_schema_digest: 'sha256:fe84c056deadbeef',
      parameters: {path: 'src/mutable.ts'},
    }),
    replayClass: 'READ_ONLY',
  });
  expect(d.resourceKeys).toEqual(['path:src/mutable.ts']);
  expect(d.class).toBe('SERIAL');
});

test('未就绪分类：缺 replay 亦 SERIAL 失败关闭并行', () => {
  const d = classifyToolCallConcurrency({
    toolRef: 'read_file',
    argumentsJson: '{"path":"a.ts"}',
  });
  expect(d.class).toBe('SERIAL');
  expect(d.reason).toBe('CONCURRENCY_CLASSIFICATION_NOT_READY');
  expect(d.marksGoalDone).toBe(false);
});

test('assertSerialOnly：非 SERIAL 失败关闭', () => {
  expect(() =>
    assertSerialOnly({
      class: 'PARALLEL_SAFE',
      resourceKeys: [],
      reason: 'CONCURRENCY_CLASSIFICATION_NOT_READY',
      marksGoalDone: false,
    }),
  ).toThrow(/TOOL_CONCURRENCY_REJECTED:PARALLEL_SAFE/);
});
