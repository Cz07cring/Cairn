/**
 * compose 联调用：长驻 ring-runner，注册真实 RunActivation。
 * 由 Python 测试以子进程拉起；缺 RING_TEMPORAL_TARGET 则退出 2。
 *
 * 用法：
 *   RING_TEMPORAL_TARGET=127.0.0.1:7233 pnpm --filter @ring/runner exec tsx src/temporal/composeRunnerWorker.ts
 */
import {NativeConnection, Worker} from '@temporalio/worker';
import {
  RUN_ACTIVATION_ACTIVITY_NAME,
  runActivationActivities,
} from './runActivation.js';
import {loadSettings, RUNNER_TASK_QUEUE, WORKER_BUILD_ID} from './worker.js';

async function main(): Promise<void> {
  const settings = loadSettings(process.env);
  if (!settings.temporalTarget) {
    console.error('composeRunnerWorker: 需要 RING_TEMPORAL_TARGET');
    process.exit(2);
  }
  const connection = await NativeConnection.connect({
    address: settings.temporalTarget,
  });
  const worker = await Worker.create({
    connection,
    namespace: settings.namespace,
    taskQueue: settings.taskQueue || RUNNER_TASK_QUEUE,
    activities: runActivationActivities,
    buildId: WORKER_BUILD_ID,
  });
  console.log(
    `compose-runner-worker-ready target=${settings.temporalTarget} queue=${settings.taskQueue} activity=${RUN_ACTIVATION_ACTIVITY_NAME}`,
  );
  await worker.run();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
