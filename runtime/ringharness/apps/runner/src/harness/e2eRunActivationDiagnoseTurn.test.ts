/**
 * e2eRunActivationDiagnoseTurn：环境组装与结果投影（无 checkout 亦可测投影）。
 */
import {describe, expect, test} from 'vitest';
import {EXECUTE_RUNTIME_OFFICIAL} from './executeOfficialRuntime.js';
import {evidenceFromRunActivationResult} from './e2eRunActivationDiagnoseTurn.js';

describe('e2eRunActivationDiagnoseTurn', () => {
  test('投影：五工具含 seal；marks_goal_done=false', () => {
    const evidence = evidenceFromRunActivationResult({
      status: 'ACTIVATION_SUBMITTED',
      pending_harness: false,
      kind: 'EXECUTE',
      activity_id: 'act-1',
      attempt_id: 'att-1',
      fencing_epoch: '1',
      effect_ids: ['e1', 'e2', 'e3', 'e4', 'e5'],
      tool_names: [
        'read_file',
        'run_tests',
        'write_file',
        'run_tests',
        'seal_candidate',
      ],
      driver: EXECUTE_RUNTIME_OFFICIAL,
      scripted_order: false,
      chat_driven_order: true,
      marks_goal_done: false,
    });
    expect(evidence.via).toBe('runActivation');
    expect(evidence.marks_goal_done).toBe(false);
    expect(evidence.scripted_order).toBe(false);
    expect(evidence.chat_driven_order).toBe(true);
    expect(evidence.sealEffectId).toBe('e5');
    expect(evidence.readEffectId).toBe('e1');
    expect(evidence.runnerCalledDispatch).toBe(false);
  });

  test('投影：夹杂乱序时按覆盖取 effect', () => {
    const evidence = evidenceFromRunActivationResult({
      status: 'ACTIVATION_SUBMITTED',
      pending_harness: false,
      kind: 'EXECUTE',
      activity_id: 'act-1',
      effect_ids: ['a', 'b', 'c', 'd', 'e', 'f'],
      tool_names: [
        'run_tests',
        'read_file',
        'git_diff',
        'write_file',
        'run_tests',
        'seal_candidate',
      ],
      scripted_order: false,
      chat_driven_order: true,
      marks_goal_done: false,
    });
    expect(evidence.readEffectId).toBe('b');
    expect(evidence.redTestsEffectId).toBe('a');
    expect(evidence.writeEffectId).toBe('d');
    expect(evidence.greenTestsEffectId).toBe('e');
    expect(evidence.sealEffectId).toBe('f');
  });

  test('投影：缺必要工具失败关闭', () => {
    expect(() =>
      evidenceFromRunActivationResult({
        status: 'ACTIVATION_SUBMITTED',
        pending_harness: false,
        kind: 'EXECUTE',
        activity_id: 'act-1',
        effect_ids: ['e1', 'e2'],
        tool_names: ['write_file', 'run_tests'],
        scripted_order: false,
        marks_goal_done: false,
      }),
    ).toThrow(/E2E_RA_COVER/);
  });
});
