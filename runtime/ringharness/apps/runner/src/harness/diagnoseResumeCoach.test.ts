import {expect, test} from 'vitest';
import {
  buildDiagnoseResumeCoachText,
  countSuccessfulWritesSinceLastRunTests,
  diagnosePhaseRejectReason,
  diagnoseResumeMaxRounds,
  lastFailedWriteSummary,
} from './diagnoseResumeCoach.js';

test('run_tests/seal coach 禁止 read_file；≠DONE 措辞保留', () => {
  const forRun = buildDiagnoseResumeCoachText({
    next: 'run_tests',
    hasSealStep: true,
    acceptanceBlock: 'Acceptance: tests green.',
  });
  expect(forRun).toMatch(/Call ONLY tool "run_tests"/);
  expect(forRun).toMatch(/Forbidden now: read_file, git_diff/);
  expect(forRun).toMatch(/marksGoalDone=false/);
  expect(forRun).toMatch(/Acceptance: tests green/);

  const forSeal = buildDiagnoseResumeCoachText({
    next: 'seal_candidate',
    hasSealStep: true,
  });
  expect(forSeal).toMatch(/Call ONLY tool "seal_candidate"/);
  expect(forSeal).toMatch(/Forbidden now: read_file/);
});

test('read_file 缺口不禁读，但仍要求立即调工具', () => {
  const text = buildDiagnoseResumeCoachText({
    next: 'read_file',
    hasSealStep: false,
  });
  expect(text).not.toMatch(/Forbidden now: read_file/);
  expect(text).toMatch(/Immediately call "read_file"/);
});

test('diagnoseResumeMaxRounds 高于旧 6+4 上限', () => {
  expect(diagnoseResumeMaxRounds(5)).toBeGreaterThanOrEqual(20);
  expect(diagnoseResumeMaxRounds(10)).toBe(40);
});

test('写后未绿测：拒绝 seal 与 read；放行 run_tests', () => {
  expect(
    diagnosePhaseRejectReason({
      toolName: 'seal_candidate',
      hasWrite: true,
      greenAfterWrite: false,
      hasSeal: false,
      hasSealStep: true,
    }),
  ).toMatch(/before seal_candidate/);
  expect(
    diagnosePhaseRejectReason({
      toolName: 'read_file',
      hasWrite: true,
      greenAfterWrite: false,
      hasSeal: false,
      hasSealStep: true,
    }),
  ).toMatch(/DIAGNOSE_PHASE_FORBIDDEN/);
  expect(
    diagnosePhaseRejectReason({
      toolName: 'run_tests',
      hasWrite: true,
      greenAfterWrite: false,
      hasSeal: false,
      hasSealStep: true,
    }),
  ).toBeNull();
});

test('写后已绿测：只允许 seal_candidate', () => {
  expect(
    diagnosePhaseRejectReason({
      toolName: 'read_file',
      hasWrite: true,
      greenAfterWrite: true,
      hasSeal: false,
      hasSealStep: true,
    }),
  ).toMatch(/ONLY seal_candidate/);
  expect(
    diagnosePhaseRejectReason({
      toolName: 'seal_candidate',
      hasWrite: true,
      greenAfterWrite: true,
      hasSeal: false,
      hasSealStep: true,
    }),
  ).toBeNull();
});

test('无成功 write 超读预算：拒继续 read，逼 write_file', () => {
  expect(
    diagnosePhaseRejectReason({
      toolName: 'read_file',
      hasWrite: false,
      greenAfterWrite: true,
      hasSeal: false,
      hasSealStep: true,
      preWriteExploreCount: 12,
    }),
  ).toMatch(/budget exhausted before write_file/);
  expect(
    diagnosePhaseRejectReason({
      toolName: 'write_file',
      hasWrite: false,
      greenAfterWrite: true,
      hasSeal: false,
      hasSealStep: true,
      preWriteExploreCount: 12,
    }),
  ).toBeNull();
  expect(
    diagnosePhaseRejectReason({
      toolName: 'seal_candidate',
      hasWrite: false,
      greenAfterWrite: true,
      hasSeal: false,
      hasSealStep: true,
    }),
  ).toMatch(/before seal_candidate/);
});

test('已跑过 run_tests 未 write：读预算收紧为 3', () => {
  expect(
    diagnosePhaseRejectReason({
      toolName: 'read_file',
      hasWrite: false,
      greenAfterWrite: true,
      hasSeal: false,
      hasSealStep: true,
      preWriteExploreCount: 3,
      sawSuccessfulRunTests: true,
    }),
  ).toMatch(/budget exhausted before write_file/);
  expect(
    diagnosePhaseRejectReason({
      toolName: 'read_file',
      hasWrite: false,
      greenAfterWrite: true,
      hasSeal: false,
      hasSealStep: true,
      preWriteExploreCount: 2,
      sawSuccessfulRunTests: true,
    }),
  ).toBeNull();
});

test('写后未绿测且连续写过多：拒 write、逼 run_tests', () => {
  expect(
    diagnosePhaseRejectReason({
      toolName: 'write_file',
      hasWrite: true,
      greenAfterWrite: false,
      hasSeal: false,
      hasSealStep: true,
      writesSinceLastRunTests: 4,
    }),
  ).toMatch(/too many write_file/);
  expect(
    diagnosePhaseRejectReason({
      toolName: 'run_tests',
      hasWrite: true,
      greenAfterWrite: false,
      hasSeal: false,
      hasSealStep: true,
      writesSinceLastRunTests: 4,
    }),
  ).toBeNull();
});

test('write_file coach 带 path/content 与失败摘要', () => {
  const text = buildDiagnoseResumeCoachText({
    next: 'write_file',
    hasSealStep: true,
    writePath: 'order_service/store.py',
    writeContentPreview: 'fixed_idempotent\n',
    lastWriteFailure: 'VALIDATION_REJECTED: content empty',
  });
  expect(text).toMatch(/Call ONLY tool "write_file"/);
  expect(text).toMatch(/preferred path="order_service\/store\.py"/);
  expect(text).toMatch(/fixed_idempotent/);
  expect(text).toMatch(/Last write_file failed/);
  expect(text).toMatch(/VALIDATION_REJECTED/);
});

test('lastFailedWriteSummary 取末次 write 若失败', () => {
  expect(
    lastFailedWriteSummary([
      {
        toolName: 'write_file',
        effectStatus: 'VALIDATION_REJECTED',
        toolIsError: true,
        toolResultText: 'content empty',
      },
      {toolName: 'run_tests', effectStatus: 'SUCCEEDED'},
    ]),
  ).toMatch(/VALIDATION_REJECTED: content empty/);
  expect(
    lastFailedWriteSummary([
      {toolName: 'run_tests', effectStatus: 'SUCCEEDED'},
      {
        toolName: 'write_file',
        effectStatus: 'FAILED',
        toolIsError: true,
        toolResultText: 'path denied',
      },
    ]),
  ).toMatch(/FAILED: path denied/);
  expect(
    lastFailedWriteSummary([
      {
        toolName: 'write_file',
        effectStatus: 'FAILED',
        toolIsError: true,
        toolResultText: 'old',
      },
      {
        toolName: 'write_file',
        effectStatus: 'SUCCEEDED',
        toolIsError: false,
        toolResultText: 'ok',
      },
    ]),
  ).toBeUndefined();
});

test('countSuccessfulWritesSinceLastRunTests 从末往前计', () => {
  expect(
    countSuccessfulWritesSinceLastRunTests([
      {toolName: 'run_tests', effectStatus: 'SUCCEEDED'},
      {toolName: 'write_file', effectStatus: 'SUCCEEDED'},
      {toolName: 'write_file', effectStatus: 'SUCCEEDED'},
    ]),
  ).toBe(2);
  expect(
    countSuccessfulWritesSinceLastRunTests([
      {toolName: 'write_file', effectStatus: 'SUCCEEDED'},
      {toolName: 'run_tests', effectStatus: 'FAILED', toolIsError: true},
      {toolName: 'write_file', effectStatus: 'SUCCEEDED'},
    ]),
  ).toBe(1);
});
