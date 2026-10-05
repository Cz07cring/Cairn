/**
 * TS Runner Temporal Worker：仅注册 RunActivation Activity。
 *
 * - 任务队列 `ring-runner`（与控制面 `ring-control` 分离）
 * - 不注册 TS Workflow；业务状态机仍在 Python GoalWorkflow
 * - 缺 RING_TEMPORAL_TARGET 时诚实空闲（镜像 Python workflow-worker）
 */

import {NativeConnection, Worker} from '@temporalio/worker';
import {
  RUN_ACTIVATION_ACTIVITY_NAME,
  runActivationActivities,
} from './runActivation.js';

/** 与 deploy/temporal/VERSIONS.md、contracts 矩阵对齐。 */
export const RUNNER_TASK_QUEUE = 'ring-runner';
export const WORKER_BUILD_ID = 'm0-ts-1.23.0-dev';

export type RunnerWorkerSettings = {
  temporalTarget: string | null;
  namespace: string;
  taskQueue: string;
};

export function loadSettings(
  env: NodeJS.ProcessEnv = process.env,
): RunnerWorkerSettings {
  const target = (env.RING_TEMPORAL_TARGET || '').trim() || null;
  const namespace = (env.RING_TEMPORAL_NAMESPACE || 'default').trim() || 'default';
  const taskQueue =
    (env.RING_TEMPORAL_RUNNER_TASK_QUEUE || RUNNER_TASK_QUEUE).trim() ||
    RUNNER_TASK_QUEUE;
  return {temporalTarget: target, namespace, taskQueue};
}

function logIdle(): void {
  // 与 Python ``workflow-worker-idle`` 同形，便于本机日志检索
  console.log('runner-temporal-idle');
}

/**
 * 缺 target：打 idle 并以 0 退出（once / 长驻均如此，避免假称已接入）。
 * 有 target：NativeConnection + 仅注册 RunActivation。
 */
export async function runWorkerLoop(options: {
  once?: boolean;
  env?: NodeJS.ProcessEnv;
}): Promise<number> {
  const settings = loadSettings(options.env ?? process.env);
  if (settings.temporalTarget === null) {
    logIdle();
    if (options.once) {
      return 0;
    }
    console.log('runner-temporal-idle exiting: RING_TEMPORAL_TARGET 未设置');
    return 0;
  }

  const connection = await NativeConnection.connect({
    address: settings.temporalTarget,
  });
  try {
    const worker = await Worker.create({
      connection,
      namespace: settings.namespace,
      taskQueue: settings.taskQueue,
      activities: runActivationActivities,
      buildId: WORKER_BUILD_ID,
    });
    if (options.once) {
      // 只验证可注册，不长驻（单测 / 冒烟）
      console.log(
        `runner-temporal-once connected target=${settings.temporalTarget} queue=${settings.taskQueue} activity=${RUN_ACTIVATION_ACTIVITY_NAME} build_id=${WORKER_BUILD_ID}`,
      );
      return 0;
    }
    console.log(
      `runner-temporal-running target=${settings.temporalTarget} queue=${settings.taskQueue} activity=${RUN_ACTIVATION_ACTIVITY_NAME} build_id=${WORKER_BUILD_ID}`,
    );
    await worker.run();
    return 0;
  } finally {
    await connection.close();
  }
}
