/**
 * E2E-1 进程缝入口：官方 AgentLoop（scripted）× Broker Gateway × 真实 Control。
 *
 * 由 pytest 拉起 uvicorn + 独立 broker_app 后调用；Runner 不 /dispatch。
 * 输出一行 JSON 到 stdout（marksGoalDone 恒 false）。
 */
import {createAb01BrokerExecuteTool} from './ab01BrokerGateway.js';
import type {HarnessToolActivation} from './brokerBackedHarnessTool.js';
import {runOfficialAgentLoopReadFileTurn} from './officialAgentLoopHost.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';
import {resolveHarnessPin} from './pin.js';

export type E2e1OfficialBrokerTurnEnv = {
  checkout: string;
  controlUrl: string;
  authorization: string;
  activation: HarnessToolActivation;
  expectedPath: string;
  userPrompt: string;
  pollDelayMs?: number;
  pollMaxAttempts?: number;
  idleTimeoutMs?: number;
  /** 测试可注入；默认 global fetch（真实 Control）。 */
  fetchImpl?: typeof fetch;
};

export type E2e1OfficialBrokerTurnResult = {
  driver: string;
  harnessPin: string;
  modelRounds: number;
  round2CitesToolResult: boolean;
  effectId: string;
  effectStatus: string;
  toolCallId: string;
  toolResultPreview: string;
  runnerCalledDispatch: false;
  marksGoalDone: false;
};

export function parseE2e1Env(
  env: NodeJS.ProcessEnv = process.env,
): E2e1OfficialBrokerTurnEnv {
  const need = (k: string) => {
    const v = (env[k] || '').trim();
    if (!v) throw new Error(`E2E1_ENV_MISSING:${k}`);
    return v;
  };
  const lease = JSON.parse(need('RING_E2E1_LEASE_JSON')) as {
    activity_id: string;
    attempt_id: string;
    fencing_epoch: string;
  };
  return {
    checkout: need('RING_HARNESS_CHECKOUT'),
    controlUrl: need('RING_CONTROL_URL'),
    authorization: `Bearer ${need('RING_WORKER_JWT')}`,
    activation: {
      kind: 'EXECUTE',
      projectId: need('RING_E2E1_PROJECT_ID'),
      activityId: lease.activity_id,
      lease: {
        activity_id: lease.activity_id,
        attempt_id: lease.attempt_id,
        fencing_epoch: String(lease.fencing_epoch),
      },
    },
    expectedPath: need('RING_E2E1_READ_PATH'),
    userPrompt:
      (env.RING_E2E1_USER_PROMPT || '').trim() ||
      `请调用 read_file，path 为 ${need('RING_E2E1_READ_PATH')}`,
    pollDelayMs: Number(env.RING_E2E1_POLL_DELAY_MS || '200'),
    pollMaxAttempts: Number(env.RING_E2E1_POLL_MAX_ATTEMPTS || '150'),
    idleTimeoutMs: Number(env.RING_E2E1_IDLE_TIMEOUT_MS || '60000'),
  };
}

/**
 * 官方 scripted AgentLoop + 真实 BrokerBackedHarnessTool（仅 prepare/观察）。
 */
export async function runE2e1OfficialBrokerProcessTurn(
  input: E2e1OfficialBrokerTurnEnv,
): Promise<E2e1OfficialBrokerTurnResult> {
  if (!isOfficialAgentLoopBuilt(input.checkout)) {
    throw new Error('HARNESS_AGENT_LOOP_NOT_BUILT');
  }
  const gateway = createAb01BrokerExecuteTool({
    baseUrl: input.controlUrl,
    authorization: input.authorization,
    activation: input.activation,
    fetchImpl: input.fetchImpl,
    poll: {
      delayMs: input.pollDelayMs ?? 200,
      maxAttempts: input.pollMaxAttempts ?? 150,
    },
  });
  const evidence = await runOfficialAgentLoopReadFileTurn({
    checkoutDir: input.checkout,
    executeTool: gateway.executeTool,
    userPrompt: input.userPrompt,
    expectedPath: input.expectedPath,
    idleTimeoutMs: input.idleTimeoutMs ?? 60_000,
  });
  return {
    driver: evidence.driver,
    harnessPin: resolveHarnessPin(),
    modelRounds: evidence.modelRounds,
    round2CitesToolResult: evidence.round2CitesToolResult,
    effectId: evidence.effectId,
    effectStatus: evidence.effectStatus,
    toolCallId: evidence.toolCallId,
    toolResultPreview: evidence.toolResultText.slice(0, 200),
    runnerCalledDispatch: false,
    marksGoalDone: false,
  };
}

async function main(): Promise<void> {
  const input = parseE2e1Env();
  const result = await runE2e1OfficialBrokerProcessTurn(input);
  process.stdout.write(JSON.stringify(result) + '\n');
  if (
    result.marksGoalDone ||
    result.modelRounds < 2 ||
    !result.round2CitesToolResult ||
    result.effectStatus !== 'SUCCEEDED'
  ) {
    process.exitCode = 2;
  }
}

const isDirect =
  typeof process.argv[1] === 'string' &&
  (process.argv[1].endsWith('e2e1OfficialBrokerProcessTurn.ts') ||
    process.argv[1].endsWith('e2e1OfficialBrokerProcessTurn.js'));

if (isDirect) {
  main().catch((err) => {
    process.stderr.write(String(err?.stack || err) + '\n');
    process.exitCode = 1;
  });
}
