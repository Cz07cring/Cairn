/**
 * 官方 diagnose 续跑 coach 文案与写后阶段硬拒绝（可单测）。
 *
 * 实测（rc364/rc365）：模型在缺 seal / 缺写后绿测时仍狂刷 read_file；
 * coach Forbidden 不够时，在 execute 入口硬拒绝。≠ Goal DONE。
 *
 * rc-ds10-fea1f52：rawGot 含失败 write_file 时 cover 仍 missing=write_file；
 * 续跑须带明确 path/content 与上次失败摘要，避免空转口述。
 */
export function buildDiagnoseResumeCoachText(input: {
  next: string;
  hasSealStep: boolean;
  acceptanceBlock?: string;
  /** 建议写入路径（live/e2e 常见 RING_HARNESS_EXECUTE_WRITE_PATH） */
  writePath?: string;
  /** 建议正文预览（截断）；有则写入 coach，逼非空 content */
  writeContentPreview?: string;
  /** 最近一次失败 write_file 的工具结果摘要 */
  lastWriteFailure?: string;
}): string {
  const next = input.next.trim();
  const critical =
    next === 'run_tests' || next === 'write_file' || next === 'seal_candidate';
  const forbid = critical
    ? `Forbidden now: read_file, git_diff, and reopening any prior path. Call ONLY tool "${next}" via tool_calls.`
    : `Do not reopen the same file path; if a prior result said no_progress_nudge or banned repeat, switch tool. Immediately call "${next}" via tool_calls.`;
  const cover =
    `Cover required tools (read_file, run_tests, write_file, run_tests after write` +
    (input.hasSealStep ? ', seal_candidate after green run_tests' : '') +
    `; order may vary; git_diff only when not blocked).`;
  const acceptance =
    input.acceptanceBlock &&
    (next === 'run_tests' || next === 'write_file' || next === 'seal_candidate')
      ? `${input.acceptanceBlock} Public tests must pass (exit_code=0) before seal_candidate. `
      : '';
  let writeHint = '';
  if (next === 'write_file') {
    const path = (input.writePath ?? '').trim();
    const preview = (input.writeContentPreview ?? '').trim();
    const fail = (input.lastWriteFailure ?? '').trim();
    writeHint =
      ` write_file MUST use tool_calls with non-empty path and content` +
      (path ? ` (preferred path="${path}")` : '') +
      (preview
        ? ` (content may start with: ${JSON.stringify(preview.slice(0, 120))})`
        : '') +
      '. Empty content or wrong path is rejected and does NOT count as cover. ';
    if (fail) {
      writeHint += `Last write_file failed: ${fail.slice(0, 220)}. Fix args and retry write_file. `;
    }
  }
  return (
    `Do not narrate. ${forbid} ${cover} ${acceptance}${writeHint}` +
    `marksGoalDone=false in prior results does NOT mean stop.`
  );
}

/** chat-driven diagnose 续跑次数：须高于「读循环」常见轮次，否则未到 seal 就耗尽。 */
export function diagnoseResumeMaxRounds(expectedToolCount: number): number {
  return Math.max(20, expectedToolCount * 4);
}

/**
 * 写后阶段硬拒绝 / 写前读预算 / 写后未绿测写预算：
 * - 无成功 write 前禁止 seal
 * - 无 write 时 read/git_diff 有上限，逼模型进入 write
 * - 已 write 未绿测：禁 read/git_diff；连续写超限则逼 run_tests
 * - 已绿测缺 seal：只允许 seal_candidate
 * 返回拒绝原因；null 表示放行。≠ Goal DONE。
 */
export function diagnosePhaseRejectReason(input: {
  toolName: string;
  hasWrite: boolean;
  greenAfterWrite: boolean;
  hasSeal: boolean;
  hasSealStep: boolean;
  /** 成功 write 之前的 read_file+git_diff 次数 */
  preWriteExploreCount?: number;
  maxPreWriteExplores?: number;
  /**
   * 已见成功 run_tests 但尚未成功 write：收紧读预算（默认 3），
   * 避免红测后继续读循环却从未写入（biz-369）。
   */
  sawSuccessfulRunTests?: boolean;
  maxPreWriteExploresAfterTests?: number;
  /** 最近一次 run_tests 之后的成功 write_file 次数（未绿测时） */
  writesSinceLastRunTests?: number;
  maxWritesBeforeRetest?: number;
}): string | null {
  if (!input.hasSealStep) {
    return null;
  }
  const name = input.toolName;
  const explores = input.preWriteExploreCount ?? 0;
  const maxExplores = input.sawSuccessfulRunTests
    ? (input.maxPreWriteExploresAfterTests ?? 3)
    : (input.maxPreWriteExplores ?? 12);
  const writesSinceRun = input.writesSinceLastRunTests ?? 0;
  const maxWrites = input.maxWritesBeforeRetest ?? 4;

  // 无成功 write 前禁止 seal（避免 cover missing=write_file）
  if (!input.hasWrite && name === 'seal_candidate') {
    return (
      'DIAGNOSE_PHASE_FORBIDDEN: call write_file (then green run_tests) ' +
      'before seal_candidate'
    );
  }
  if (!input.hasWrite) {
    if (
      (name === 'read_file' || name === 'git_diff') &&
      explores >= maxExplores
    ) {
      return (
        'DIAGNOSE_PHASE_FORBIDDEN: read/git_diff budget exhausted before write_file; ' +
        'call write_file with a non-empty path and content now'
      );
    }
    return null;
  }
  if (!input.greenAfterWrite) {
    if (name === 'seal_candidate') {
      return (
        'DIAGNOSE_PHASE_FORBIDDEN: run_tests must pass (exit_code=0) after ' +
        'write_file before seal_candidate'
      );
    }
    if (name === 'read_file' || name === 'git_diff') {
      return (
        'DIAGNOSE_PHASE_FORBIDDEN: after write_file call run_tests with ' +
        '{"suite":"public"} until exit_code=0; read_file/git_diff banned in this phase'
      );
    }
    if (name === 'write_file' && writesSinceRun >= maxWrites) {
      return (
        'DIAGNOSE_PHASE_FORBIDDEN: too many write_file without green run_tests; ' +
        'call run_tests with {"suite":"public"} now (exit_code=0 required before seal)'
      );
    }
    return null;
  }
  if (!input.hasSeal && name !== 'seal_candidate') {
    return (
      'DIAGNOSE_PHASE_FORBIDDEN: green run_tests already observed; ' +
      'call ONLY seal_candidate now (read_file/write_file/run_tests/git_diff banned)'
    );
  }
  return null;
}

/** 自 trail 末往前：最近一次失败的 write_file 结果摘要（供 coach / COVER 诊断）。 */
export function lastFailedWriteSummary(
  trail: ReadonlyArray<{
    toolName: string;
    effectStatus: string;
    toolIsError?: boolean;
    toolResultText?: string;
  }>,
): string | undefined {
  for (let i = trail.length - 1; i >= 0; i -= 1) {
    const t = trail[i]!;
    if (t.toolName !== 'write_file') continue;
    const failed =
      Boolean(t.toolIsError) || t.effectStatus !== 'SUCCEEDED';
    if (!failed) return undefined;
    const text = (t.toolResultText || '').replace(/\s+/g, ' ').trim();
    return text
      ? `${t.effectStatus}: ${text.slice(0, 240)}`
      : t.effectStatus;
  }
  return undefined;
}

/** 自 trail 末往前：最近一次 run_tests 之后的成功 write 次数。 */
export function countSuccessfulWritesSinceLastRunTests(
  trail: ReadonlyArray<{
    toolName: string;
    effectStatus: string;
    toolIsError?: boolean;
  }>,
): number {
  let writes = 0;
  for (let i = trail.length - 1; i >= 0; i -= 1) {
    const t = trail[i]!;
    if (t.toolName === 'run_tests') {
      break;
    }
    if (
      t.toolName === 'write_file' &&
      t.effectStatus === 'SUCCEEDED' &&
      !t.toolIsError
    ) {
      writes += 1;
    }
  }
  return writes;
}
