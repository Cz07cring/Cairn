/**
 * EXECUTE tool-call → Broker；arguments 必填并进入内容寻址工件。
 */
import {existsSync} from 'node:fs';
import {expect, test, vi} from 'vitest';
import {cordisEntryPath} from './cordisBootGate.js';
import {
  defaultResolveExecuteInputArtifact,
  forwardExecuteToolCallsFromChunks,
} from './cordisExecuteBridge.js';
import {createHeartbeatLinkedAdmissionGate} from './brokerToolHttpPorts.js';
import type {ExecuteToolHost} from './executeToolHost.js';
import {contentDigestSha256 as digestOf} from './artifactPutHttpPorts.js';

const checkout = process.env.RING_HARNESS_CHECKOUT;
const cordisBuilt =
  Boolean(checkout) && existsSync(cordisEntryPath(checkout as string));

const lease = {
  activity_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  attempt_id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
  fencing_epoch: '1',
};

function executeClaimed() {
  return {
    lease,
    activity: {
      id: lease.activity_id,
      kind: 'EXECUTE' as const,
      state_revision: 1,
      binding: {},
      goal_id: 'g',
      task_id: null,
      project_id: 'p',
    },
  };
}

function mockHost(overrides: Partial<ExecuteToolHost> = {}): ExecuteToolHost {
  const gate = createHeartbeatLinkedAdmissionGate();
  return {
    gate,
    ports: {
      createStep: vi.fn(async () => ({
        stepId: 's1',
        logicalStepId: 's1',
        intentRevision: 1,
      })),
      prepareEffect: vi.fn(async () => ({
        effectId: 'e1',
        status: 'PREPARED',
        stateRevision: 1,
      })),
      dispatchEffect: vi.fn(async () => ({status: 'DISPATCHED'})),
    },
    observe: {
      getEffect: vi.fn(async () => ({
        id: 'e1',
        status: 'DISPATCHED',
        stateRevision: 1,
        evidenceIds: [],
      })),
      postTrustedReceipt: vi.fn(async () => ({disposition: 'APPLIED'})),
    },
    artifacts: {
      putCollectorContent: vi.fn(async () => ({
        artifactId: 'art-put',
        digest: 'sha256:' + 'a'.repeat(64),
      })),
      getArtifactContent: vi.fn(async () => ''),
    },
    ...overrides,
  };
}

test('forwardExecuteToolCallsFromChunks：tool-call + arguments → Broker', async () => {
  const host = mockHost();
  const resolve = vi.fn(async () => ({artifactId: 'art-1', purpose: '读入口'}));
  const out = await forwardExecuteToolCallsFromChunks(
    host,
    executeClaimed(),
    [
      {type: 'tool-call', name: 'read_file', arguments: '{"path":"a.ts"}'},
      {type: 'finish', reason: 'stop'},
    ],
    resolve,
  );
  expect(out).toEqual([
    {
      tool: 'read_file',
      effectId: 'e1',
      // 诚实回报「已准备」：Runner 只提交意图，派发与执行归 Broker
      dispatchStatus: 'PREPARED',
      logicalStepId: 's1',
    },
  ]);
  expect(resolve).toHaveBeenCalledWith('read_file', '{"path":"a.ts"}');
  expect(host.ports.createStep).toHaveBeenCalledOnce();
});

test('缺 arguments 失败关闭', async () => {
  await expect(
    forwardExecuteToolCallsFromChunks(
      mockHost(),
      executeClaimed(),
      [{type: 'tool-call', name: 'read_file'}],
      async () => ({artifactId: 'art', purpose: 'x'}),
    ),
  ).rejects.toThrow(/EXECUTE_TOOL_ARGUMENTS_REQUIRED/);
});

test('同名工具不同 arguments → 不同 ToolPayload 工件（Broker 形状）', async () => {
  const bodies: string[] = [];
  const digests: string[] = [];
  let n = 0;
  const host = mockHost({
    artifacts: {
      putCollectorContent: vi.fn(async (input) => {
        const body =
          typeof input.body === 'string'
            ? input.body
            : new TextDecoder().decode(input.body);
        bodies.push(body);
        const digest = digestOf(body);
        digests.push(digest);
        n += 1;
        return {artifactId: `art-${n}`, digest};
      }),
      getArtifactContent: vi.fn(async () => ''),
    },
    ports: {
      createStep: vi.fn(async ({purpose}) => ({
        stepId: purpose,
        logicalStepId: purpose,
        intentRevision: 1,
      })),
      prepareEffect: vi.fn(async ({inputArtifactId}) => ({
        effectId: `eff-${inputArtifactId}`,
        status: 'PREPARED',
        stateRevision: 1,
      })),
      dispatchEffect: vi.fn(async () => ({status: 'DISPATCHED'})),
    },
  });
  const claimed = executeClaimed();
  const resolve = defaultResolveExecuteInputArtifact(host, claimed);
  const out = await forwardExecuteToolCallsFromChunks(
    host,
    claimed,
    [
      {type: 'tool-call', name: 'read_file', arguments: '{"path":"a.ts"}'},
      {type: 'tool-call', name: 'read_file', arguments: '{"path":"b.ts"}'},
      {type: 'finish', reason: 'stop'},
    ],
    resolve,
  );
  expect(out).toHaveLength(2);
  expect(digests[0]).not.toEqual(digests[1]);
  const {READ_FILE_SCHEMA_DIGEST} = await import('./cordisExecuteBridge.js');
  expect(JSON.parse(bodies[0]!)).toEqual({
    tool_ref: 'read_file',
    tool_schema_digest: READ_FILE_SCHEMA_DIGEST,
    parameters: {path: 'a.ts'},
  });
  expect(JSON.parse(bodies[1]!)).toEqual({
    tool_ref: 'read_file',
    tool_schema_digest: READ_FILE_SCHEMA_DIGEST,
    parameters: {path: 'b.ts'},
  });
  expect(out[0]?.effectId).toBe('eff-art-1');
  expect(out[1]?.effectId).toBe('eff-art-2');
});

test('非法 tool arguments 不得 createStep/prepare/dispatch', async () => {
  const host = mockHost();
  const claimed = executeClaimed();
  const resolve = defaultResolveExecuteInputArtifact(host, claimed);
  const cases: Array<{args: string; re: RegExp}> = [
    {args: 'not-json', re: /INVALID_JSON/},
    {args: '[]', re: /NOT_OBJECT/},
    {args: 'null', re: /NOT_OBJECT/},
    {args: '{}', re: /MISSING_PATH/},
    {args: '{"path":""}', re: /EMPTY_PATH/},
    {args: '{"path":"  "}', re: /EMPTY_PATH/},
    {args: '{"path":"a.ts","extra":1}', re: /UNKNOWN_FIELDS/},
    {args: 'x'.repeat(10_001), re: /TOO_LONG/},
  ];
  for (const c of cases) {
    await expect(
      forwardExecuteToolCallsFromChunks(
        host,
        claimed,
        [{type: 'tool-call', name: 'read_file', arguments: c.args}],
        resolve,
      ),
    ).rejects.toThrow(c.re);
  }
  expect(host.ports.createStep).not.toHaveBeenCalled();
  expect(host.ports.prepareEffect).not.toHaveBeenCalled();
  expect(host.artifacts.putCollectorContent).not.toHaveBeenCalled();
});

test('canonicalizeReadFileToolPayload 输出 ToolPayload 且 Broker 可抽 path', async () => {
  const {canonicalizeReadFileToolPayload, READ_FILE_SCHEMA_DIGEST} = await import(
    './cordisExecuteBridge.js'
  );
  const body = canonicalizeReadFileToolPayload('{"path":"src/main.py"}');
  expect(JSON.parse(body)).toEqual({
    tool_ref: 'read_file',
    tool_schema_digest: READ_FILE_SCHEMA_DIGEST,
    parameters: {path: 'src/main.py'},
  });
  const {execFileSync} = await import('node:child_process');
  const out = execFileSync(
    'uv',
    [
      'run',
      'python',
      '-c',
      'from execution_broker.read_file import parse_read_file_input; import sys; print(parse_read_file_input(sys.stdin.buffer.read()))',
    ],
    {
      input: body,
      encoding: 'utf-8',
      cwd: new URL('../../../../', import.meta.url).pathname,
    },
  );
  expect(out.trim()).toBe('src/main.py');
});

test('canonicalizeReadFileToolPayload 拒绝 schema 漂移的 ToolPayload', async () => {
  const {canonicalizeReadFileToolPayload} = await import('./cordisExecuteBridge.js');
  expect(() =>
    canonicalizeReadFileToolPayload(
      JSON.stringify({
        tool_ref: 'read_file',
        tool_schema_digest: 'sha256:' + '0'.repeat(64),
        parameters: {path: 'a.ts'},
      }),
    ),
  ).toThrow(/EXECUTE_TOOL_ARGUMENTS_SCHEMA_DRIFT/);
});

test('E2E-2 工具 canonicalize 对齐 Kernel digest 且拒 command', async () => {
  const {
    WRITE_FILE_SCHEMA_DIGEST,
    RUN_TESTS_SCHEMA_DIGEST,
    GIT_DIFF_SCHEMA_DIGEST,
    SEAL_CANDIDATE_SCHEMA_DIGEST,
    canonicalizeWriteFileToolPayload,
    canonicalizeRunTestsToolPayload,
    canonicalizeGitDiffToolPayload,
    canonicalizeSealCandidateToolPayload,
    canonicalizeToolArgumentsPayload,
    DEFAULT_EXECUTE_TOOL_NAMES,
  } = await import('./cordisExecuteBridge.js');

  expect([...DEFAULT_EXECUTE_TOOL_NAMES].sort()).toEqual(
    ['git_diff', 'read_file', 'run_tests', 'seal_candidate', 'write_file'].sort(),
  );

  expect(JSON.parse(canonicalizeWriteFileToolPayload('{"path":"a.py","content":"x\\n"}'))).toEqual({
    tool_ref: 'write_file',
    tool_schema_digest: WRITE_FILE_SCHEMA_DIGEST,
    parameters: {path: 'a.py', content: 'x\n'},
  });
  expect(JSON.parse(canonicalizeRunTestsToolPayload('{"suite":"public"}'))).toEqual({
    tool_ref: 'run_tests',
    tool_schema_digest: RUN_TESTS_SCHEMA_DIGEST,
    parameters: {suite: 'public'},
  });
  expect(JSON.parse(canonicalizeGitDiffToolPayload('{}'))).toEqual({
    tool_ref: 'git_diff',
    tool_schema_digest: GIT_DIFF_SCHEMA_DIGEST,
    parameters: {},
  });
  const pid = '00000000-0000-4000-8000-000000000001';
  expect(
    JSON.parse(
      canonicalizeSealCandidateToolPayload(
        JSON.stringify({verification_profile_ids: [pid]}),
      ),
    ),
  ).toEqual({
    tool_ref: 'seal_candidate',
    tool_schema_digest: SEAL_CANDIDATE_SCHEMA_DIGEST,
    parameters: {verification_profile_ids: [pid]},
  });

  expect(() => canonicalizeRunTestsToolPayload('{"suite":"public","command":"x"}')).toThrow(
    /UNKNOWN_FIELDS/,
  );
  expect(() => canonicalizeGitDiffToolPayload('{"command":"rm"}')).toThrow(/UNKNOWN_FIELDS/);
  expect(() => canonicalizeToolArgumentsPayload('shell', '{}')).toThrow(
    /EXECUTE_TOOL_SCHEMA_UNKNOWN/,
  );

  const {execFileSync} = await import('node:child_process');
  const pyCode =
    'from control_kernel.domain.tool_capability_manifest import ' +
    'WRITE_FILE_SCHEMA_DIGEST,RUN_TESTS_SCHEMA_DIGEST,' +
    'GIT_DIFF_SCHEMA_DIGEST,SEAL_CANDIDATE_SCHEMA_DIGEST;' +
    'print(WRITE_FILE_SCHEMA_DIGEST);print(RUN_TESTS_SCHEMA_DIGEST);' +
    'print(GIT_DIFF_SCHEMA_DIGEST);print(SEAL_CANDIDATE_SCHEMA_DIGEST)';
  const py = execFileSync('uv', ['run', 'python', '-c', pyCode], {
    encoding: 'utf-8',
    cwd: new URL('../../../../', import.meta.url).pathname,
  })
    .trim()
    .split('\n');
  expect(py).toEqual([
    WRITE_FILE_SCHEMA_DIGEST,
    RUN_TESTS_SCHEMA_DIGEST,
    GIT_DIFF_SCHEMA_DIGEST,
    SEAL_CANDIDATE_SCHEMA_DIGEST,
  ]);
});

test('forwardExecuteToolCallsFromChunks：write_file / run_tests 可 prepare', async () => {
  const host = mockHost();
  const claimed = executeClaimed();
  const resolve = defaultResolveExecuteInputArtifact(host, claimed);
  const out = await forwardExecuteToolCallsFromChunks(
    host,
    claimed,
    [
      {
        type: 'tool-call',
        name: 'write_file',
        arguments: '{"path":"a.py","content":"ok\\n"}',
      },
      {type: 'tool-call', name: 'run_tests', arguments: '{"suite":"public"}'},
      {type: 'tool-call', name: 'git_diff', arguments: '{}'},
    ],
    resolve,
  );
  expect(out).toHaveLength(3);
  expect(host.ports.createStep).toHaveBeenCalledTimes(3);
  expect(host.ports.prepareEffect).toHaveBeenCalledTimes(3);
});

test('PLAN activity 拒绝', async () => {
  await expect(
    forwardExecuteToolCallsFromChunks(
      mockHost(),
      {
        lease,
        activity: {
          id: lease.activity_id,
          kind: 'PLAN' as 'EXECUTE',
          state_revision: 1,
          binding: {},
          goal_id: 'g',
          task_id: null,
          project_id: 'p',
        },
      },
      [{type: 'tool-call', name: 'read_file', arguments: '{}'}],
      async () => ({artifactId: 'art', purpose: 'x'}),
    ),
  ).rejects.toThrow(/UNEXPECTED_ACTIVITY_KIND/);
});

test('无 input artifact 失败关闭', async () => {
  await expect(
    forwardExecuteToolCallsFromChunks(
      mockHost(),
      executeClaimed(),
      [{type: 'tool-call', name: 'read_file', arguments: '{"path":"a.ts"}'}],
      async () => ({artifactId: '  ', purpose: 'x'}),
    ),
  ).rejects.toThrow(/EXECUTE_INPUT_ARTIFACT_REQUIRED/);
});

test('心跳关闸后拒绝 tool-call', async () => {
  const host = mockHost();
  host.gate.onHeartbeatFailure(new Error('hb'));
  await expect(
    forwardExecuteToolCallsFromChunks(
      host,
      executeClaimed(),
      [{type: 'tool-call', name: 'read_file', arguments: '{"path":"a.ts"}'}],
      async () => ({artifactId: 'art', purpose: 'x'}),
    ),
  ).rejects.toThrow(/TOOL_ADMISSION_CLOSED/);
});

test('无 tool-call chunk 返回空，不虚构 effect', async () => {
  const host = mockHost();
  const out = await forwardExecuteToolCallsFromChunks(
    host,
    executeClaimed(),
    [{type: 'finish', reason: 'stop'}],
    async () => ({artifactId: 'art', purpose: 'x'}),
  );
  expect(out).toEqual([]);
  expect(host.ports.createStep).not.toHaveBeenCalled();
});

test('runExecuteToolTurnFromChunks 包装 succeeded', async () => {
  const {runExecuteToolTurnFromChunks} = await import('./cordisExecuteBridge.js');
  const host = mockHost();
  const turn = await runExecuteToolTurnFromChunks(
    host,
    executeClaimed(),
    [
      {type: 'tool-call', name: 'read_file', arguments: '{"path":"x"}'},
      {type: 'finish', reason: 'stop'},
    ],
    async () => ({artifactId: 'art-1', purpose: '读'}),
  );
  expect(turn.status).toBe('succeeded');
  expect(turn.effects).toHaveLength(1);
  expect(turn.marksGoalDone).toBe(false);
  expect(turn.attachedInjected).toEqual([]);
  expect(turn.deferredTurn).toBeNull();
});

test('AB08×EXECUTE：工具后 seal 前消息挂入本回合证据，不双跑', async () => {
  const {runExecuteToolTurnFromChunks} = await import('./cordisExecuteBridge.js');
  const {createTurnUserMessageGate} = await import('./turnUserMessageGate.js');
  const host = mockHost();
  const turn = await runExecuteToolTurnFromChunks(
    host,
    executeClaimed(),
    [
      {type: 'tool-call', name: 'read_file', arguments: '{"path":"x"}'},
      {type: 'finish', reason: 'stop'},
    ],
    async () => ({artifactId: 'art-1', purpose: '读'}),
    undefined,
    undefined,
    {
      turnId: 'exec-ab08',
      onBetweenToolsAndSeal: async (gate) => {
        const d = await gate.offer({messageId: 'm-attach', text: 'late'});
        expect(d.kind).toBe('attached');
        expect(d.marksGoalDone).toBe(false);
      },
    },
  );
  expect(turn.attachedInjected).toEqual([{messageId: 'm-attach', text: 'late'}]);
  expect(turn.deferredTurn).toBeNull();
  expect(turn.marksGoalDone).toBe(false);
});

test('AB08×EXECUTE：seal 后迟到消息进新 Turn 种子', async () => {
  const {runExecuteToolTurnFromChunks} = await import('./cordisExecuteBridge.js');
  const {createTurnUserMessageGate} = await import('./turnUserMessageGate.js');
  const host = mockHost();
  const turn = await runExecuteToolTurnFromChunks(
    host,
    executeClaimed(),
    [{type: 'finish', reason: 'stop'}],
    async () => ({artifactId: 'art', purpose: 'x'}),
    undefined,
    undefined,
    {
      messageGate: createTurnUserMessageGate({
        turnId: 'exec-old',
        newTurnId: () => 'exec-new',
      }),
      onAfterSeal: async (gate) => {
        const d = await gate.offer({messageId: 'm-def', text: 'deferred'});
        expect(d.kind).toBe('deferred_new_turn');
      },
    },
  );
  expect(turn.attachedInjected).toEqual([]);
  expect(turn.deferredTurn).toEqual({
    turnId: 'exec-new',
    messages: [{messageId: 'm-def', text: 'deferred'}],
  });
  expect(turn.marksGoalDone).toBe(false);
});

test.skipIf(!cordisBuilt)(
  'runExecuteToolTurnViaCordisWithLease：无 tool-call → 空 effects',
  async () => {
    const {
      runExecuteToolTurnViaCordisWithLease,
    } = await import('./cordisExecuteBridge.js');
    const host = mockHost();
    const turn = await runExecuteToolTurnViaCordisWithLease(
      {
        compileContext: async () => ({
          id: 'b',
          content_digest: 'sha256:' + 'a'.repeat(64),
          content: {role: 'EXECUTOR'},
        }),
        bindContext: async () => ({context_digest: 'sha256:' + 'b'.repeat(64)}),
        modelInvocation: {
          createInvocation: async () => ({invocationId: 'i'}),
          completeInvocation: async () => ({text: 'no-tools'}),
        },
        toolAdmissionGate: host.gate,
      },
      host,
      executeClaimed(),
      checkout,
      'plan-fixture',
    );
    expect(turn.status).toBe('succeeded');
    expect(turn.effects).toEqual([]);
    expect(host.ports.createStep).not.toHaveBeenCalled();
  },
);

test.skipIf(!cordisBuilt)(
  'runExecuteToolTurnViaCordisWithLease：PLAN activity 拒绝',
  async () => {
    const {
      runExecuteToolTurnViaCordisWithLease,
    } = await import('./cordisExecuteBridge.js');
    const host = mockHost();
    const claimed = executeClaimed();
    claimed.activity.kind = 'PLAN' as 'EXECUTE';
    await expect(
      runExecuteToolTurnViaCordisWithLease(
        {
          compileContext: async () => ({
            id: 'b',
            content_digest: 'd',
            content: {role: 'EXECUTOR'},
          }),
          bindContext: async () => ({context_digest: 'd'}),
          modelInvocation: {
            createInvocation: async () => ({invocationId: 'i'}),
            completeInvocation: async () => ({text: 'x'}),
          },
        },
        host,
        claimed,
        checkout,
      ),
    ).rejects.toThrow(/UNEXPECTED_ACTIVITY_KIND/);
  },
);

test.skipIf(!cordisBuilt)('registerKernelLlmOnContextForExecute 允许 tool-call', async () => {
  const {bootPinnedCordis} = await import('./cordisBootGate.js');
  const {registerKernelLlmOnContextForExecute} = await import('./cordisExecuteBridge.js');
  const boot = await bootPinnedCordis(checkout);
  const dispose = registerKernelLlmOnContextForExecute(
    boot.ctx,
    {
      createInvocation: async (input) => {
        expect(input.toolsExposedToModel).toEqual(['read_file']);
        return {invocationId: 'inv-e'};
      },
      completeInvocation: async () => ({
        text: '',
        chunks: [
          {type: 'tool-call', name: 'read_file', arguments: '{"path":"z"}'},
          {type: 'finish', reason: 'stop'},
        ],
      }),
    },
    lease,
    'sha256:' + 'c'.repeat(64),
  );
  try {
    const llm = boot.ctx.get('llm') as {
      stream: (o: unknown) => AsyncIterable<{type: string; name?: string}>;
    };
    const names: string[] = [];
    for await (const chunk of llm.stream({
      provider: 'ring-kernel',
      model: 'm',
      messages: [{role: 'user', content: 'x'}],
      tools: [{name: 'read_file'}],
    })) {
      if (chunk.type === 'tool-call' && chunk.name) {
        names.push(chunk.name);
      }
    }
    expect(names).toEqual(['read_file']);
  } finally {
    dispose();
  }
});
