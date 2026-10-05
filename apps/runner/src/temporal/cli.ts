/**
 * Runner Temporal Worker 入口：`pnpm --filter @ring/runner temporal-worker [-- --once]`
 */

import {runWorkerLoop} from './worker.js';

async function main(): Promise<void> {
  const once = process.argv.includes('--once');
  const code = await runWorkerLoop({once});
  process.exitCode = code;
}

main().catch((err: unknown) => {
  console.error(err);
  process.exitCode = 1;
});
