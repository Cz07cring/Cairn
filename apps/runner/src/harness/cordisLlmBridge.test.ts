import {existsSync} from 'node:fs';
import {expect, test, vi} from 'vitest';
import {cordisEntryPath} from './cordisBootGate.js';
import {
  registerKernelLlmOnContext,
  runZeroToolPlanTurnViaCordis,
  runZeroToolPlanTurnViaCordisWithLease,
  type CordisPlanBridgePorts,
} from './cordisLlmBridge.js';
import {bootPinnedCordis} from './cordisBootGate.js';

const checkout = process.env.RING_HARNESS_CHECKOUT;
const cordisBuilt =
  Boolean(checkout) && existsSync(cordisEntryPath(checkout as string));

function basePorts(
  overrides: Partial<CordisPlanBridgePorts> = {},
): CordisPlanBridgePorts {
  const createInvocation = vi.fn(async () => ({invocationId: 'inv-1'}));
  const completeInvocation = vi.fn(async () => ({
    text: 'fixture plan narrative',
  }));
  return {
    claimPlan: async () => ({
      lease: {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
      activity: {
        id: 'a',
        kind: 'PLAN',
        state_revision: 1,
        binding: {goal_contract_revision: 1},
        goal_id: 'g',
        task_id: null,
        project_id: 'p',
      },
    }),
    compileContext: async () => ({
      id: 'bundle-1',
      content_digest: 'sha256:' + 'a'.repeat(64),
      content: {role: 'PLANNER'},
    }),
    bindContext: async () => ({context_digest: 'sha256:' + 'b'.repeat(64)}),
    modelInvocation: {createInvocation, completeInvocation},
    buildPlan: () => ({tasks: [{id: 'T1'}]}),
    submitPlanOutcome: vi.fn(async () => undefined),
    ...overrides,
  };
}

test.skipIf(!cordisBuilt)('挂 ring llm 后 ctx.llm 可读且 implementation 诚实', async () => {
  const boot = await bootPinnedCordis(checkout);
  const ports = {
    createInvocation: vi.fn(async () => ({invocationId: 'x'})),
    completeInvocation: vi.fn(async () => ({text: 'ok'})),
  };
  const dispose = registerKernelLlmOnContext(
    boot.ctx,
    ports,
    {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
    'sha256:' + 'c'.repeat(64),
  );
  try {
    const llm = boot.ctx.llm ?? (boot.ctx.get('llm') as {implementation: string});
    expect(llm?.implementation).toBe('ring-kernel-bridge');
  } finally {
    dispose();
  }
});

test.skipIf(!cordisBuilt)('tools 非空 → ROLE_TOOL_FORBIDDEN 且不调 ports', async () => {
  const boot = await bootPinnedCordis(checkout);
  const createInvocation = vi.fn(async () => ({invocationId: 'x'}));
  const dispose = registerKernelLlmOnContext(
    boot.ctx,
    {
      createInvocation,
      completeInvocation: async () => ({text: ''}),
    },
    {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
    'sha256:' + 'd'.repeat(64),
  );
  try {
    const llm = boot.ctx.get('llm') as {
      stream: (o: unknown) => AsyncIterable<unknown>;
    };
    await expect(async () => {
      for await (const _ of llm.stream({
        provider: 'ring-kernel',
        model: 'm',
        messages: [{role: 'user', content: 'x'}],
        tools: [{name: 'read_file'}],
      })) {
        // 不应进入
      }
    }).rejects.toThrow(/ROLE_TOOL_FORBIDDEN/);
    expect(createInvocation).not.toHaveBeenCalled();
  } finally {
    dispose();
  }
});

test.skipIf(!cordisBuilt)('回执含 tool-call chunk → ROLE_TOOL_FORBIDDEN', async () => {
  const boot = await bootPinnedCordis(checkout);
  const dispose = registerKernelLlmOnContext(
    boot.ctx,
    {
      createInvocation: async () => ({invocationId: 'x'}),
      completeInvocation: async () => ({
        text: '',
        chunks: [{type: 'tool-call', name: 'read_file'}],
      }),
    },
    {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
    'sha256:' + 'e'.repeat(64),
  );
  try {
    const llm = boot.ctx.get('llm') as {
      stream: (o: unknown) => AsyncIterable<unknown>;
    };
    await expect(async () => {
      for await (const _ of llm.stream({
        provider: 'ring-kernel',
        model: 'm',
        messages: [{role: 'user', content: 'x'}],
        tools: [],
      })) {
        //
      }
    }).rejects.toThrow(/ROLE_TOOL_FORBIDDEN/);
  } finally {
    dispose();
  }
});

test.skipIf(!cordisBuilt)('零工具 turn → ModelInvocation + fixture PlanCreate 通路', async () => {
  const ports = basePorts();
  const result = await runZeroToolPlanTurnViaCordis(ports, checkout);
  expect(result).toBe('succeeded');
  expect(ports.modelInvocation.createInvocation).toHaveBeenCalledOnce();
  const createArg = (ports.modelInvocation.createInvocation as ReturnType<typeof vi.fn>).mock
    .calls[0][0];
  expect(createArg.toolsExposedToModel).toEqual([]);
  expect(createArg.providerRef).toBe('ring-kernel');
  expect(ports.modelInvocation.completeInvocation).toHaveBeenCalledOnce();
  expect(ports.submitPlanOutcome).toHaveBeenCalledOnce();
});

test('无 READY 时 idle', async () => {
  // 不依赖 Cordis：claim 空则在 boot 前返回
  const ports = basePorts({claimPlan: async () => null});
  const result = await runZeroToolPlanTurnViaCordis(ports, checkout);
  expect(result).toBe('idle');
});

test.skipIf(!cordisBuilt)('WithLease 跳过 claimPlan', async () => {
  const claimPlan = vi.fn(async () => null);
  const ports = basePorts({claimPlan});
  const claimed = {
    lease: {activity_id: 'a', attempt_id: 't', fencing_epoch: '1'},
    activity: {
      id: 'a',
      kind: 'PLAN' as const,
      state_revision: 1,
      binding: {goal_contract_revision: 1},
      goal_id: 'g',
      task_id: null,
      project_id: 'p',
    },
  };
  const {claimPlan: _ignored, ...rest} = ports;
  await expect(
    runZeroToolPlanTurnViaCordisWithLease(rest, claimed, checkout),
  ).resolves.toBe('succeeded');
  expect(claimPlan).not.toHaveBeenCalled();
  expect(ports.submitPlanOutcome).toHaveBeenCalledOnce();
});
