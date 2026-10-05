/**
 * E2E 进程缝：经 Temporal Activity 入口 `runActivation` 跑官方 diagnose[+seal]。
 *
 * 环境：`RING_HARNESS_EXECUTE_RUNTIME=deepseek-official-agent-loop`、
 * `RING_HARNESS_EXECUTE_OFFICIAL_MODE=diagnose`；测可 `FSM=1`。
 * Runner 不 /dispatch；≠ Goal DONE（marks_goal_done 恒 false）。
 */
import {readFileSync} from 'node:fs';
import {
  EXECUTE_RUNTIME_OFFICIAL,
  resolveOfficialExecuteMode,
} from './executeOfficialRuntime.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';
import {resolveHarnessPin} from './pin.js';
import {
  runActivation,
  type RunActivationResult,
} from '../temporal/runActivation.js';

export type E2eRunActivationDiagnoseEvidence = {
  via: 'runActivation';
  driver: string;
  harnessPin: string;
  status: string;
  effect_ids: string[];
  tool_names: string[];
  scripted_order: boolean;
  chat_driven_order: boolean;
  marks_goal_done: false;
  sealEffectId?: string;
  readEffectId?: string;
  redTestsEffectId?: string;
  writeEffectId?: string;
  greenTestsEffectId?: string;
  runnerCalledDispatch: false;
};

function need(env: NodeJS.ProcessEnv, k: string): string {
  const v = (env[k] || '').trim();
  if (!v) throw new Error(`E2E_RA_ENV_MISSING:${k}`);
  return v;
}

/** 从 E2E 环境组装 process.env 覆盖（写 content 文件 → EXECUTE_WRITE_CONTENT）。 */
export function prepareRunActivationDiagnoseEnv(
  env: NodeJS.ProcessEnv = process.env,
): NodeJS.ProcessEnv {
  const checkout = need(env, 'RING_HARNESS_CHECKOUT');
  if (!isOfficialAgentLoopBuilt(checkout)) {
    throw new Error('HARNESS_AGENT_LOOP_NOT_BUILT');
  }
  need(env, 'RING_CONTROL_URL');
  need(env, 'RING_WORKER_JWT');
  need(env, 'RING_E2E_RA_GOAL_ID');
  const lease = JSON.parse(need(env, 'RING_E2E_RA_LEASE_JSON')) as {
    activity_id: string;
    attempt_id: string;
    fencing_epoch: string;
  };
  if (!lease.activity_id || !lease.attempt_id || !lease.fencing_epoch) {
    throw new Error('E2E_RA_LEASE_INCOMPLETE');
  }

  const useFsm = (env.RING_HARNESS_EXECUTE_OFFICIAL_FSM || '').trim() === '1';
  const contentFile = (env.RING_HARNESS_EXECUTE_WRITE_CONTENT_FILE || '').trim();
  const writeContent = contentFile
    ? readFileSync(contentFile, 'utf8')
    : (env.RING_HARNESS_EXECUTE_WRITE_CONTENT || '').trim();
  // FSM 必须有预定写正文；live 自主写码时可缺省（空串，不进 prompt）
  if (useFsm && !writeContent) {
    throw new Error('E2E_RA_ENV_MISSING:RING_HARNESS_EXECUTE_WRITE_CONTENT(_FILE)');
  }

  const mode = resolveOfficialExecuteMode({
    ...env,
    RING_HARNESS_EXECUTE_OFFICIAL_MODE:
      env.RING_HARNESS_EXECUTE_OFFICIAL_MODE || 'diagnose',
  });
  if (mode !== 'diagnose') {
    throw new Error(`E2E_RA_MODE_NOT_DIAGNOSE:${mode}`);
  }

  return {
    ...env,
    RING_HARNESS_EXECUTE_RUNTIME: EXECUTE_RUNTIME_OFFICIAL,
    RING_HARNESS_EXECUTE_OFFICIAL_MODE: 'diagnose',
    RING_HARNESS_EXECUTE_WRITE_CONTENT: writeContent,
    // assessOfficialExecuteRuntime 需要 chat 形；FSM 时不真正外呼
    RING_LOCAL_QWEN_BASE: (env.RING_LOCAL_QWEN_BASE || '').trim() || 'http://127.0.0.1:9',
    RING_LOCAL_QWEN_API_KEY: (env.RING_LOCAL_QWEN_API_KEY || '').trim() || 'e2e-ra-fsm',
    RING_LOCAL_QWEN_MODEL: (env.RING_LOCAL_QWEN_MODEL || '').trim() || 'e2e-ra-fsm',
  };
}

export function evidenceFromRunActivationResult(
  result: RunActivationResult,
): E2eRunActivationDiagnoseEvidence {
  if (result.status !== 'ACTIVATION_SUBMITTED' || result.kind !== 'EXECUTE') {
    throw new Error(
      `E2E_RA_BAD_STATUS:${result.status}:${'kind' in result ? result.kind : ''}` +
        `:${'reason' in result ? result.reason : ''}`,
    );
  }
  const names = result.tool_names ?? [];
  const ids = result.effect_ids;
  if (names.length !== ids.length) {
    throw new Error(`E2E_RA_TRAIL_LEN:${names.length}:${ids.length}`);
  }
  const expectSeal = names.includes('seal_candidate');
  const required = expectSeal
    ? ['read_file', 'run_tests', 'write_file', 'seal_candidate']
    : ['read_file', 'run_tests', 'write_file'];

  // FSM 常见严格五工具；live 自主：覆盖 + write 后有 run_tests + write 在 seal 前
  const exact = [
    'read_file',
    'run_tests',
    'write_file',
    'run_tests',
    ...(expectSeal ? (['seal_candidate'] as const) : []),
  ];
  const isExact = names.join(',') === exact.join(',');

  const pick = (name: string, from = 0) => {
    const i = names.indexOf(name, from);
    if (i < 0) throw new Error(`E2E_RA_COVER: missing=${name} got=${names.join(',')}`);
    return i;
  };

  let readI: number;
  let redI: number;
  let writeI: number;
  let greenI: number;
  let sealI: number | undefined;

  if (isExact) {
    readI = 0;
    redI = 1;
    writeI = 2;
    greenI = 3;
    sealI = expectSeal ? 4 : undefined;
  } else {
    for (const n of required) {
      if (!names.includes(n)) {
        throw new Error(`E2E_RA_COVER: missing=${n} got=${names.join(',')}`);
      }
    }
    readI = pick('read_file');
    writeI = pick('write_file');
    // 红测：write 前的某次 run_tests；若无则用最早 run_tests
    const earlyRuns = names
      .map((n, i) => (n === 'run_tests' && i < writeI ? i : -1))
      .filter((i) => i >= 0);
    redI = earlyRuns.length > 0 ? earlyRuns[0]! : pick('run_tests');
    const lateRuns = names
      .map((n, i) => (n === 'run_tests' && i > writeI ? i : -1))
      .filter((i) => i >= 0);
    if (lateRuns.length === 0) {
      throw new Error(`E2E_RA_COVER: no run_tests after write got=${names.join(',')}`);
    }
    greenI = lateRuns[0]!;
    if (expectSeal) {
      sealI = pick('seal_candidate');
      if (writeI > sealI) {
        throw new Error(`E2E_RA_COVER: write after seal got=${names.join(',')}`);
      }
    }
  }

  return {
    via: 'runActivation',
    driver: result.driver ?? EXECUTE_RUNTIME_OFFICIAL,
    harnessPin: resolveHarnessPin(),
    status: result.status,
    effect_ids: ids,
    tool_names: names,
    scripted_order: result.scripted_order ?? true,
    chat_driven_order: result.chat_driven_order ?? false,
    marks_goal_done: false,
    readEffectId: ids[readI]!,
    redTestsEffectId: ids[redI]!,
    writeEffectId: ids[writeI]!,
    greenTestsEffectId: ids[greenI]!,
    sealEffectId: sealI !== undefined ? ids[sealI]! : undefined,
    runnerCalledDispatch: false,
  };
}

export async function runE2eRunActivationDiagnoseTurn(
  env: NodeJS.ProcessEnv = process.env,
): Promise<E2eRunActivationDiagnoseEvidence> {
  const prepared = prepareRunActivationDiagnoseEnv(env);
  const lease = JSON.parse(need(prepared, 'RING_E2E_RA_LEASE_JSON')) as {
    activity_id: string;
    attempt_id: string;
    fencing_epoch: string;
  };
  const result = await runActivation(
    {
      activity_id: lease.activity_id,
      attempt_id: lease.attempt_id,
      fencing_epoch: String(lease.fencing_epoch),
      goal_id: need(prepared, 'RING_E2E_RA_GOAL_ID'),
      kind: 'EXECUTE',
      owner_epoch: (prepared.RING_E2E_RA_OWNER_EPOCH || '').trim() || 'e2e-ra',
    },
    {env: prepared},
  );
  return evidenceFromRunActivationResult(result);
}

async function main(): Promise<void> {
  const evidence = await runE2eRunActivationDiagnoseTurn(process.env);
  process.stdout.write(`${JSON.stringify(evidence)}\n`);
}

const isMain =
  typeof process.argv[1] === 'string' &&
  /e2eRunActivationDiagnoseTurn\.(ts|js|mjs|cjs)$/.test(process.argv[1]);

if (isMain) {
  main().catch((err) => {
    console.error(err instanceof Error ? err.stack ?? err.message : err);
    process.exit(1);
  });
}
