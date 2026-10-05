/**
 * E2E-2 进程缝：官方 AgentLoop scripted 多工具 × 真实 Control/Broker。
 *
 * 支持 write→run[→seal]，或诊断全周期 read→红测→write→绿测[→seal]。
 * Runner 不 /dispatch；scriptedOrder=true ≠ 模型自主决策；marksGoalDone 恒 false。
 */
import {readFileSync} from 'node:fs';
import type {HarnessToolActivation} from './brokerBackedHarnessTool.js';
import {runOfficialMultistepBrokerWriteRunTests} from './officialMultistepBrokerSeam.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';
import {resolveHarnessPin} from './pin.js';

export type E2e2OfficialMultistepEnv = {
  checkout: string;
  controlUrl: string;
  authorization: string;
  activation: HarnessToolActivation;
  writePath: string;
  writeContent: string;
  userPrompt: string;
  pollDelayMs?: number;
  pollMaxAttempts?: number;
  idleTimeoutMs?: number;
  sealVerificationProfileIds?: string[];
  diagnoseReadPath?: string;
};

export type E2e2OfficialMultistepResult = {
  driver: string;
  harnessPin: string;
  scriptedOrder: true;
  modelRounds: number;
  laterRoundsCitePriorToolResults: boolean;
  trail: Array<{
    toolName: string;
    effectId: string;
    effectStatus: string;
    toolIsError: boolean;
  }>;
  writeEffectId: string;
  testsEffectId: string;
  readEffectId?: string;
  redTestsEffectId?: string;
  greenTestsEffectId?: string;
  sealEffectId?: string;
  runnerCalledDispatch: false;
  marksGoalDone: false;
};

export function parseE2e2MultistepEnv(
  env: NodeJS.ProcessEnv = process.env,
): E2e2OfficialMultistepEnv {
  const need = (k: string) => {
    const v = (env[k] || '').trim();
    if (!v) throw new Error(`E2E2_MS_ENV_MISSING:${k}`);
    return v;
  };
  const lease = JSON.parse(need('RING_E2E2_LEASE_JSON')) as {
    activity_id: string;
    attempt_id: string;
    fencing_epoch: string;
  };
  const writePath = need('RING_E2E2_WRITE_PATH');
  const contentFile = (env.RING_E2E2_WRITE_CONTENT_FILE || '').trim();
  const writeContent = contentFile
    ? readFileSync(contentFile, 'utf8')
    : need('RING_E2E2_WRITE_CONTENT');
  const sealRaw = (env.RING_E2E2_SEAL_PROFILE_IDS || '').trim();
  let sealVerificationProfileIds: string[] | undefined;
  if (sealRaw) {
    sealVerificationProfileIds = sealRaw.startsWith('[')
      ? (JSON.parse(sealRaw) as string[])
      : sealRaw
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean);
  }
  const diagnoseReadPath = (env.RING_E2E2_DIAGNOSE_READ_PATH || '').trim() || undefined;
  const defaultPrompt = diagnoseReadPath
    ? `先 read_file ${diagnoseReadPath}，再 run_tests 看红，write_file ${writePath}，再 run_tests 绿` +
      (sealVerificationProfileIds?.length ? '，再 seal_candidate' : '')
    : `先 write_file ${writePath}，再 run_tests suite=public` +
      (sealVerificationProfileIds?.length ? '，再 seal_candidate' : '');
  return {
    checkout: need('RING_HARNESS_CHECKOUT'),
    controlUrl: need('RING_CONTROL_URL'),
    authorization: `Bearer ${need('RING_WORKER_JWT')}`,
    activation: {
      kind: 'EXECUTE',
      projectId: need('RING_E2E2_PROJECT_ID'),
      activityId: lease.activity_id,
      lease: {
        activity_id: lease.activity_id,
        attempt_id: lease.attempt_id,
        fencing_epoch: String(lease.fencing_epoch),
      },
    },
    writePath,
    writeContent,
    userPrompt: (env.RING_E2E2_USER_PROMPT || '').trim() || defaultPrompt,
    pollDelayMs: Number(env.RING_E2E2_POLL_DELAY_MS || '150'),
    pollMaxAttempts: Number(env.RING_E2E2_POLL_MAX_ATTEMPTS || '200'),
    idleTimeoutMs: Number(env.RING_E2E2_IDLE_TIMEOUT_MS || '120000'),
    sealVerificationProfileIds,
    diagnoseReadPath,
  };
}

export async function runE2e2OfficialMultistepOrderTurn(
  input: E2e2OfficialMultistepEnv,
): Promise<E2e2OfficialMultistepResult> {
  if (!isOfficialAgentLoopBuilt(input.checkout)) {
    throw new Error('HARNESS_AGENT_LOOP_NOT_BUILT');
  }
  const diagnose = Boolean(input.diagnoseReadPath);
  const expectSeal = (input.sealVerificationProfileIds ?? []).length > 0;
  const evidence = await runOfficialMultistepBrokerWriteRunTests({
    checkoutDir: input.checkout,
    writePath: input.writePath,
    writeContent: input.writeContent,
    userPrompt: input.userPrompt,
    idleTimeoutMs: input.idleTimeoutMs ?? 120_000,
    sealVerificationProfileIds: input.sealVerificationProfileIds,
    diagnoseReadPath: input.diagnoseReadPath,
    broker: {
      baseUrl: input.controlUrl,
      authorization: input.authorization,
      activation: input.activation,
      poll: {
        delayMs: input.pollDelayMs ?? 150,
        maxAttempts: input.pollMaxAttempts ?? 240,
      },
    },
  });
  const expected = diagnose
    ? expectSeal
      ? ['read_file', 'run_tests', 'write_file', 'run_tests', 'seal_candidate']
      : ['read_file', 'run_tests', 'write_file', 'run_tests']
    : expectSeal
      ? ['write_file', 'run_tests', 'seal_candidate']
      : ['write_file', 'run_tests'];
  if (evidence.trail.length < expected.length) {
    throw new Error('E2E2_MS_TRAIL_SHORT');
  }
  const names = evidence.trail.slice(0, expected.length).map((t) => t.toolName);
  if (names.join(',') !== expected.join(',')) {
    throw new Error(`E2E2_MS_ORDER:${names.join(',')}`);
  }
  for (const step of evidence.trail.slice(0, expected.length)) {
    if (step.effectStatus !== 'SUCCEEDED' || step.toolIsError) {
      throw new Error(`E2E2_MS_EFFECT:${step.toolName}:${step.effectStatus}`);
    }
  }
  const byName = (name: string, index: number) => {
    const hit = evidence.trail[index];
    if (!hit || hit.toolName !== name) throw new Error(`E2E2_MS_IDX:${name}`);
    return hit;
  };
  let readEffectId: string | undefined;
  let redTestsEffectId: string | undefined;
  let write;
  let green;
  let sealEffectId: string | undefined;
  if (diagnose) {
    readEffectId = byName('read_file', 0).effectId;
    redTestsEffectId = byName('run_tests', 1).effectId;
    write = byName('write_file', 2);
    green = byName('run_tests', 3);
    if (expectSeal) sealEffectId = byName('seal_candidate', 4).effectId;
  } else {
    write = byName('write_file', 0);
    green = byName('run_tests', 1);
    if (expectSeal) sealEffectId = byName('seal_candidate', 2).effectId;
  }
  return {
    driver: evidence.driver,
    harnessPin: resolveHarnessPin(),
    scriptedOrder: true,
    modelRounds: evidence.modelRounds,
    laterRoundsCitePriorToolResults: evidence.laterRoundsCitePriorToolResults,
    trail: evidence.trail.map((t) => ({
      toolName: t.toolName,
      effectId: t.effectId,
      effectStatus: t.effectStatus,
      toolIsError: t.toolIsError,
    })),
    writeEffectId: write.effectId,
    testsEffectId: green.effectId,
    readEffectId,
    redTestsEffectId,
    greenTestsEffectId: green.effectId,
    sealEffectId,
    runnerCalledDispatch: false,
    marksGoalDone: false,
  };
}

async function main(): Promise<void> {
  const input = parseE2e2MultistepEnv();
  const result = await runE2e2OfficialMultistepOrderTurn(input);
  process.stdout.write(JSON.stringify(result) + '\n');
  const diagnose = Boolean(result.readEffectId);
  const expectSeal = Boolean(result.sealEffectId);
  const minTools = diagnose ? (expectSeal ? 5 : 4) : expectSeal ? 3 : 2;
  const minRounds = minTools + 1;
  if (
    result.marksGoalDone ||
    !result.scriptedOrder ||
    result.modelRounds < minRounds ||
    !result.laterRoundsCitePriorToolResults ||
    result.runnerCalledDispatch
  ) {
    process.exitCode = 2;
  }
}

const isDirect =
  typeof process.argv[1] === 'string' &&
  (process.argv[1].endsWith('e2e2OfficialMultistepOrderTurn.ts') ||
    process.argv[1].endsWith('e2e2OfficialMultistepOrderTurn.js'));

if (isDirect) {
  main().catch((err) => {
    process.stderr.write(String(err?.stack || err) + '\n');
    process.exitCode = 1;
  });
}
