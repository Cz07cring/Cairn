/**
 * run_tests 证据 → 模型 / cover 可读文本。
 * Broker 存 JSON（含 "exit_code":N）；cover 与续跑须见 exit_code=N 行。≠DONE。
 */

/** 将 Broker run_tests JSON 证据规范成含 exit_code= 行的 ToolResult 文本。 */
export function formatRunTestsToolResultText(raw: string): string {
  const trimmed = (raw || '').trim();
  if (!trimmed) return raw;
  try {
    const parsed = JSON.parse(trimmed) as {
      exit_code?: unknown;
      suite?: unknown;
      timed_out?: unknown;
      stdout_preview?: unknown;
      stderr_preview?: unknown;
    };
    if (typeof parsed.exit_code !== 'number') {
      return raw;
    }
    const lines: string[] = [
      `exit_code=${parsed.exit_code}`,
      `suite=${String(parsed.suite ?? '')}`,
    ];
    if (parsed.timed_out === true) {
      lines.push('timed_out=true');
    }
    const out = String(parsed.stdout_preview ?? '').trim();
    const err = String(parsed.stderr_preview ?? '').trim();
    if (out) {
      lines.push('--- stdout ---', out);
    }
    if (err) {
      lines.push('--- stderr ---', err);
    }
    return lines.join('\n');
  } catch {
    return raw;
  }
}
