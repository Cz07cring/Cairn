/**
 * run_tests ToolResult 规范化：Broker JSON → exit_code= 行。
 */
import {describe, expect, test} from 'vitest';
import {formatRunTestsToolResultText} from './runTestsToolResult.js';

describe('formatRunTestsToolResultText', () => {
  test('Broker JSON "exit_code":0 → 模型行 exit_code=0', () => {
    const raw = JSON.stringify({
      suite: 'public',
      exit_code: 0,
      stdout_preview: '1 passed',
      stderr_preview: '',
    });
    const text = formatRunTestsToolResultText(raw);
    expect(text).toMatch(/^exit_code=0\n/);
    expect(text).toContain('suite=public');
    expect(text).toContain('1 passed');
  });

  test('红测保留失败预览', () => {
    const raw = JSON.stringify({
      suite: 'public',
      exit_code: 1,
      stdout_preview: 'FAILED test_same_idempotency_key_twice',
      stderr_preview: '',
    });
    const text = formatRunTestsToolResultText(raw);
    expect(text).toMatch(/^exit_code=1\n/);
    expect(text).toContain('test_same_idempotency_key_twice');
  });

  test('非 JSON 原样返回', () => {
    expect(formatRunTestsToolResultText('exit_code=0\nok')).toBe('exit_code=0\nok');
  });
});
