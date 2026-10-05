import {expect, test} from 'vitest';
import {contentDigestSha256} from './artifactPutHttpPorts.js';
import {createHeartbeatLinkedAdmissionGate} from './brokerToolHttpPorts.js';
import {
  argsDigestOfCanonicalPayload,
  createNoProgressGuard,
} from './noProgressGuard.js';

const argsA = argsDigestOfCanonicalPayload('{"path":"a.ts"}');
const argsB = argsDigestOfCanonicalPayload('{"path":"b.ts"}');

test('正常批处理不同资源：不误停', () => {
  const guard = createNoProgressGuard();
  for (const [path, digest] of [
    ['a.ts', argsA],
    ['b.ts', argsB],
  ] as const) {
    const v = guard.observe({
      callId: path,
      tool: 'read_file',
      argsDigest: digest,
      outcome: 'SUCCEEDED',
      signals: [{kind: 'artifact_digest', value: contentDigestSha256(path)}],
    });
    expect(v.action).toBe('continue');
  }
  expect(guard.metrics().nudgeCount).toBe(0);
  expect(guard.metrics().forceStopped).toBe(false);
});

test('相同成功调用无新 digest：先 Nudge，再软 ForceStop（禁同参、不关全闸）', () => {
  const gate = createHeartbeatLinkedAdmissionGate();
  const guard = createNoProgressGuard({gate, repeatSuccessBeforeNudge: 2});
  const sameDigest = contentDigestSha256('same-body');

  const first = guard.observe({
    callId: '1',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'SUCCEEDED',
    signals: [{kind: 'artifact_digest', value: sameDigest}],
  });
  expect(first.action).toBe('continue');

  // 第二次相同签名且 digest 已见 → 无进展
  const nudge = guard.observe({
    callId: '2',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'SUCCEEDED',
    signals: [{kind: 'artifact_digest', value: sameDigest}],
  });
  expect(nudge).toMatchObject({
    action: 'nudge',
    code: 'NO_PROGRESS_NUDGE',
    nudgeCount: 1,
    marksGoalDone: false,
  });
  expect(gate.allowed()).toBe(true);

  const stop = guard.observe({
    callId: '3',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'SUCCEEDED',
    signals: [{kind: 'artifact_digest', value: sameDigest}],
  });
  expect(stop).toMatchObject({
    action: 'force_stop',
    code: 'NO_PROGRESS_FORCE_STOP',
    closeToolAdmission: false,
    allowZeroToolSummary: true,
    marksGoalDone: false,
  });
  // 软停：全闸仍开，换工具可写
  expect(gate.allowed()).toBe(true);
  expect(guard.metrics().forceStopped).toBe(false);
  expect(guard.metrics().nudgeBudgetConsumed).toBe(1);
  expect(guard.metrics().nudgeBudgetMax).toBe(1);

  const banned = guard.beforeAdmit({tool: 'read_file', argsDigest: argsA});
  expect(banned).toMatchObject({
    action: 'force_stop',
    closeToolAdmission: false,
  });
  const other = guard.beforeAdmit({tool: 'write_file', argsDigest: argsB});
  expect(other.action).toBe('continue');
});

test('AB06：Nudge 计入预算；耗尽后 ForceStop 且 ≠DONE', () => {
  const gate = createHeartbeatLinkedAdmissionGate();
  const guard = createNoProgressGuard({
    gate,
    repeatSuccessBeforeNudge: 2,
    forceStopAfterNudge: false,
    maxNudgeBudget: 1,
  });
  const sameDigest = contentDigestSha256('budget-body');
  guard.observe({
    callId: '1',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'SUCCEEDED',
    signals: [{kind: 'artifact_digest', value: sameDigest}],
  });
  const nudge = guard.observe({
    callId: '2',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'SUCCEEDED',
    signals: [{kind: 'artifact_digest', value: sameDigest}],
  });
  expect(nudge.action).toBe('nudge');
  expect(guard.metrics().nudgeBudgetConsumed).toBe(1);

  const stop = guard.observe({
    callId: '3',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'SUCCEEDED',
    signals: [{kind: 'artifact_digest', value: sameDigest}],
  });
  expect(stop).toMatchObject({
    action: 'force_stop',
    marksGoalDone: false,
  });
  expect(String((stop as {reason?: string}).reason || '')).toMatch(
    /nudge_budget_exhausted/,
  );
  expect(gate.allowed()).toBe(false);
});

test('maxNudgeBudget < 1 失败关闭', () => {
  expect(() => createNoProgressGuard({maxNudgeBudget: 0})).toThrow(
    /NO_PROGRESS_NUDGE_BUDGET_INVALID/,
  );
});

test('相同校验失败：更宽容，达阈值后 Nudge', () => {
  const guard = createNoProgressGuard({repeatValidationBeforeNudge: 3});
  let last = guard.observe({
    callId: 'v1',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'VALIDATION_REJECTED',
    signals: [{kind: 'validation_rejected', value: 'empty_path'}],
  });
  expect(last.action).toBe('continue');
  last = guard.observe({
    callId: 'v2',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'VALIDATION_REJECTED',
    signals: [{kind: 'validation_rejected', value: 'empty_path'}],
  });
  expect(last.action).toBe('continue');
  last = guard.observe({
    callId: 'v3',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'VALIDATION_REJECTED',
    signals: [{kind: 'validation_rejected', value: 'empty_path'}],
  });
  expect(last.action).toBe('nudge');
});

test('validation_loop：Nudge 后再拒 → 软停禁同参、全闸仍开（可 seal）；≠DONE', () => {
  const gate = createHeartbeatLinkedAdmissionGate();
  const guard = createNoProgressGuard({
    gate,
    repeatValidationBeforeNudge: 2,
  });
  guard.observe({
    callId: 'v1',
    tool: 'write_file',
    argsDigest: argsA,
    outcome: 'VALIDATION_REJECTED',
    signals: [{kind: 'validation_rejected', value: 'empty_path'}],
  });
  const nudge = guard.observe({
    callId: 'v2',
    tool: 'write_file',
    argsDigest: argsA,
    outcome: 'VALIDATION_REJECTED',
    signals: [{kind: 'validation_rejected', value: 'empty_path'}],
  });
  expect(nudge.action).toBe('nudge');
  const stop = guard.observe({
    callId: 'v3',
    tool: 'write_file',
    argsDigest: argsA,
    outcome: 'VALIDATION_REJECTED',
    signals: [{kind: 'validation_rejected', value: 'empty_path'}],
  });
  expect(stop).toMatchObject({
    action: 'force_stop',
    closeToolAdmission: false,
    marksGoalDone: false,
  });
  expect(String((stop as {reason?: string}).reason || '')).toMatch(
    /^validation_loop:/,
  );
  expect(gate.allowed()).toBe(true);
  expect(guard.metrics().forceStopped).toBe(false);
  expect(
    guard.beforeAdmit({tool: 'write_file', argsDigest: argsA}).action,
  ).toBe('force_stop');
  expect(
    guard.beforeAdmit({tool: 'seal_candidate', argsDigest: argsB}).action,
  ).toBe('continue');
});

test('交替同参成功但无新 digest：仍计为重复签名升级', () => {
  const guard = createNoProgressGuard({repeatSuccessBeforeNudge: 2});
  const dig = contentDigestSha256('body');
  guard.observe({
    callId: '1',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'SUCCEEDED',
    signals: [{kind: 'artifact_digest', value: dig}],
  });
  const v = guard.observe({
    callId: '2',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'SUCCEEDED',
    signals: [{kind: 'artifact_digest', value: dig}],
  });
  expect(v.action).toBe('nudge');
});

test('Issue#68：交替两路径读且 digest 已见 → 非连续同签名也 Nudge；≠DONE', () => {
  // 盲区：countTrailingSame 在 A→B→A 下 trailing 恒为 1，26 轮读转不触发
  const guard = createNoProgressGuard({repeatSuccessBeforeNudge: 2});
  const digA = contentDigestSha256('body-a');
  const digB = contentDigestSha256('body-b');

  expect(
    guard.observe({
      callId: 'a1',
      tool: 'read_file',
      argsDigest: argsA,
      outcome: 'SUCCEEDED',
      signals: [{kind: 'artifact_digest', value: digA}],
    }).action,
  ).toBe('continue');
  expect(
    guard.observe({
      callId: 'b1',
      tool: 'read_file',
      argsDigest: argsB,
      outcome: 'SUCCEEDED',
      signals: [{kind: 'artifact_digest', value: digB}],
    }).action,
  ).toBe('continue');

  const nudge = guard.observe({
    callId: 'a2',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'SUCCEEDED',
    signals: [{kind: 'artifact_digest', value: digA}],
  });
  expect(nudge).toMatchObject({
    action: 'nudge',
    code: 'NO_PROGRESS_NUDGE',
    marksGoalDone: false,
  });
});

test('Issue#68：中间插入 run_tests 新 digest 不得洗掉同参再读计数；≠DONE', () => {
  const guard = createNoProgressGuard({repeatSuccessBeforeNudge: 2});
  const digA = contentDigestSha256('file-a');
  const digTests = contentDigestSha256('tests-run-1');

  expect(
    guard.observe({
      callId: 'r1',
      tool: 'read_file',
      argsDigest: argsA,
      outcome: 'SUCCEEDED',
      signals: [{kind: 'artifact_digest', value: digA}],
    }).action,
  ).toBe('continue');
  // 测试输出常带时间戳 → 每次 fresh；不得因此让「再读同一文件」永远 trailing=1
  expect(
    guard.observe({
      callId: 't1',
      tool: 'run_tests',
      argsDigest: argsDigestOfCanonicalPayload('{"suite":"unit"}'),
      outcome: 'SUCCEEDED',
      signals: [{kind: 'artifact_digest', value: digTests}],
    }).action,
  ).toBe('continue');

  const nudge = guard.observe({
    callId: 'r2',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'SUCCEEDED',
    signals: [{kind: 'artifact_digest', value: digA}],
  });
  expect(nudge).toMatchObject({
    action: 'nudge',
    marksGoalDone: false,
  });
});

test('UNKNOWN 立即 ForceStop，禁止继续准入', () => {
  const gate = createHeartbeatLinkedAdmissionGate();
  const guard = createNoProgressGuard({gate});
  const v = guard.observe({
    callId: 'u',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'UNKNOWN',
    signals: [{kind: 'effect_terminal', value: 'UNKNOWN'}],
  });
  expect(v.action).toBe('force_stop');
  expect(v).toMatchObject({marksGoalDone: false});
  expect(gate.allowed()).toBe(false);
});

test('新 Artifact digest 视为进展并清除 Nudge 钉扎', () => {
  const guard = createNoProgressGuard({repeatSuccessBeforeNudge: 2});
  const d1 = contentDigestSha256('v1');
  const d2 = contentDigestSha256('v2');
  guard.observe({
    callId: '1',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'SUCCEEDED',
    signals: [{kind: 'artifact_digest', value: d1}],
  });
  const nudge = guard.observe({
    callId: '2',
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'SUCCEEDED',
    signals: [{kind: 'artifact_digest', value: d1}],
  });
  expect(nudge.action).toBe('nudge');
  const cont = guard.observe({
    callId: '3',
    tool: 'read_file',
    argsDigest: argsB,
    outcome: 'SUCCEEDED',
    signals: [{kind: 'artifact_digest', value: d2}],
  });
  expect(cont.action).toBe('continue');
  expect(guard.metrics().forceStopped).toBe(false);
});
