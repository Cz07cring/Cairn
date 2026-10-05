/**
 * sealGreenGate：write 后绿测判定。
 */
import {describe, expect, test} from 'vitest';
import {
  successfulToolNames,
  toolResultLooksGreen,
  trailHasGreenRunTestsAfterWrite,
  trailHasSuccessfulTool,
} from './sealGreenGate.js';

describe('toolResultLooksGreen', () => {
  test('识别 exit_code=0', () => {
    expect(toolResultLooksGreen('exit_code=0\nsuite=public')).toBe(true);
    expect(toolResultLooksGreen('exit_code=1\nFAIL')).toBe(false);
    expect(toolResultLooksGreen(undefined)).toBe(false);
  });

  test('识别 Broker JSON "exit_code":0', () => {
    expect(
      toolResultLooksGreen(
        '{"suite":"public","exit_code":0,"stdout_preview":"ok"}',
      ),
    ).toBe(true);
    expect(
      toolResultLooksGreen(
        '{"suite":"public","exit_code":1,"stdout_preview":"FAIL"}',
      ),
    ).toBe(false);
  });
});

describe('trailHasGreenRunTestsAfterWrite', () => {
  test('无 write → 闸门不适用', () => {
    expect(
      trailHasGreenRunTestsAfterWrite([
        {
          toolName: 'run_tests',
          effectStatus: 'SUCCEEDED',
          toolResultText: 'exit_code=1',
        },
      ]),
    ).toBe(true);
  });

  test('write 后仅红测 → false', () => {
    expect(
      trailHasGreenRunTestsAfterWrite([
        {toolName: 'write_file', effectStatus: 'SUCCEEDED', toolResultText: 'ok'},
        {
          toolName: 'run_tests',
          effectStatus: 'SUCCEEDED',
          toolResultText: 'exit_code=1\nFAIL',
        },
      ]),
    ).toBe(false);
  });

  test('write 后绿测 → true', () => {
    expect(
      trailHasGreenRunTestsAfterWrite([
        {toolName: 'write_file', effectStatus: 'SUCCEEDED', toolResultText: 'ok'},
        {
          toolName: 'run_tests',
          effectStatus: 'SUCCEEDED',
          toolResultText: 'exit_code=0\nPASS',
        },
      ]),
    ).toBe(true);
  });

  test('write 后 Broker JSON 绿测 → true', () => {
    expect(
      trailHasGreenRunTestsAfterWrite([
        {toolName: 'write_file', effectStatus: 'SUCCEEDED', toolResultText: 'ok'},
        {
          toolName: 'run_tests',
          effectStatus: 'SUCCEEDED',
          toolResultText: '{"suite":"public","exit_code":0}',
        },
      ]),
    ).toBe(true);
  });

  test('绿测在 write 前不算', () => {
    expect(
      trailHasGreenRunTestsAfterWrite([
        {
          toolName: 'run_tests',
          effectStatus: 'SUCCEEDED',
          toolResultText: 'exit_code=0',
        },
        {toolName: 'write_file', effectStatus: 'SUCCEEDED', toolResultText: 'ok'},
      ]),
    ).toBe(false);
  });
});

describe('successfulToolNames / trailHasSuccessfulTool', () => {
  test('VALIDATION_REJECTED 不算成功覆盖', () => {
    const trail = [
      {
        toolName: 'write_file',
        effectStatus: 'VALIDATION_REJECTED',
        toolIsError: true,
        toolResultText: 'bad args',
      },
      {
        toolName: 'run_tests',
        effectStatus: 'SUCCEEDED',
        toolResultText: 'exit_code=1',
      },
    ];
    expect(successfulToolNames(trail)).toEqual(['run_tests']);
    expect(trailHasSuccessfulTool(trail, 'write_file')).toBe(false);
  });
});
