/**
 * 钉扎 DeepSeek Harness checkout 内官方 AgentLoop 依赖的 lib 入口。
 * 未 build:lib:host 时路径不存在 → 调用方 skip / PENDING_ENV。
 */
import {existsSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {cordisEntryPath} from './cordisBootGate.js';
import {requireHarnessCheckout} from './harnessPlanAdapter.js';

/** checkout 内相对路径（相对 RING_HARNESS_CHECKOUT）。 */
export const OFFICIAL_AGENT_LOOP_LIBS = {
  cordis: join('vendor', 'cordis', 'lib', 'index.js'),
  agentLoop: join('packages', 'core', 'agent-loop', 'lib', 'index.js'),
  llm: join('packages', 'llm', 'llm', 'lib', 'index.js'),
  session: join('packages', 'core', 'session', 'lib', 'index.js'),
  sessionProjection: join(
    'packages',
    'session',
    'session-projection',
    'lib',
    'index.js',
  ),
  systemPrompt: join('packages', 'core', 'system-prompt', 'lib', 'index.js'),
  tools: join('packages', 'core', 'tools', 'lib', 'index.js'),
  agent: join('packages', 'core', 'agent', 'lib', 'index.js'),
} as const;

export type OfficialAgentLoopLibKey = keyof typeof OFFICIAL_AGENT_LOOP_LIBS;

export function officialAgentLoopLibPath(
  checkoutDir: string,
  key: OfficialAgentLoopLibKey,
): string {
  return join(checkoutDir, OFFICIAL_AGENT_LOOP_LIBS[key]);
}

/** Cordis + AgentLoop peers 均已产出 lib 时为 true。 */
export function isOfficialAgentLoopBuilt(
  checkoutDir: string | undefined = process.env.RING_HARNESS_CHECKOUT,
): boolean {
  if (!checkoutDir) return false;
  try {
    requireHarnessCheckout(checkoutDir);
  } catch {
    return false;
  }
  if (!existsSync(cordisEntryPath(checkoutDir))) return false;
  return (Object.keys(OFFICIAL_AGENT_LOOP_LIBS) as OfficialAgentLoopLibKey[]).every(
    (key) => existsSync(officialAgentLoopLibPath(checkoutDir, key)),
  );
}

export function officialAgentLoopImportUrl(
  checkoutDir: string,
  key: OfficialAgentLoopLibKey,
): string {
  const path = officialAgentLoopLibPath(checkoutDir, key);
  if (!existsSync(path)) {
    throw new Error(
      `HARNESS_AGENT_LOOP_NOT_BUILT: 缺少 ${OFFICIAL_AGENT_LOOP_LIBS[key]}；` +
        '请在钉扎 checkout 内执行 pnpm run build:lib:host',
    );
  }
  return pathToFileURL(path).href;
}
