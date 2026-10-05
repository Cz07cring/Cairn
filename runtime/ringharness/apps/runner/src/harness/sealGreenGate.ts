/**
 * seal 绿测闸门：write_file 成功后须有 exit_code=0 的 run_tests，才允许视为可 seal。
 * Kernel prepare 亦有同语义权威闸；此处供官方 Loop cover / 续跑。≠ Goal DONE。
 */

export type SealGreenTrailEntry = {
  toolName: string;
  effectStatus: string;
  toolResultText?: string;
  toolIsError?: boolean;
};

/** 证据文本是否绿测：exit_code=0 或 Broker JSON "exit_code":0。 */
export function toolResultLooksGreen(text: string | undefined): boolean {
  if (!text) return false;
  if (/(?:^|\n|;|\s)exit_code=0(?:\s|$|\n|;)/.test(text)) return true;
  // Broker 原始 JSON：{"exit_code":0,...}（cover 在未格式化前也能认）
  if (/"exit_code"\s*:\s*0\b/.test(text)) return true;
  return false;
}

/** 工具是否算 cover/阶段门「成功」：须 SUCCEEDED 且非 isError。 */
export function trailEntrySucceeded(entry: SealGreenTrailEntry): boolean {
  return entry.effectStatus === 'SUCCEEDED' && !entry.toolIsError;
}

/** trail 上是否有成功调用过指定工具（拒答/校验失败不算）。 */
export function trailHasSuccessfulTool(
  trail: readonly SealGreenTrailEntry[],
  toolName: string,
): boolean {
  return trail.some((t) => t.toolName === toolName && trailEntrySucceeded(t));
}

/** 仅成功工具名序列——cover 不得把 VALIDATION_REJECTED 算进覆盖。 */
export function successfulToolNames(
  trail: readonly SealGreenTrailEntry[],
): string[] {
  return trail.filter(trailEntrySucceeded).map((t) => t.toolName);
}

/**
 * 无成功 write → 返回 true（闸门不适用）。
 * 有 write → 其后须有 SUCCEEDED 且证据含 exit_code=0 的 run_tests。
 */
export function trailHasGreenRunTestsAfterWrite(
  trail: readonly SealGreenTrailEntry[],
): boolean {
  let lastWrite = -1;
  for (let i = 0; i < trail.length; i += 1) {
    const t = trail[i]!;
    if (t.toolName === 'write_file' && trailEntrySucceeded(t)) {
      lastWrite = i;
    }
  }
  if (lastWrite < 0) return true;
  for (let i = lastWrite + 1; i < trail.length; i += 1) {
    const t = trail[i]!;
    if (t.toolName !== 'run_tests') continue;
    if (!trailEntrySucceeded(t)) continue;
    if (toolResultLooksGreen(t.toolResultText)) return true;
  }
  return false;
}
