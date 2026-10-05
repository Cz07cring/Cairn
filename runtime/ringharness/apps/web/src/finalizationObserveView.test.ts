import {describe, expect, it} from 'vitest';
import type {GoalResource, ReleaseView} from '@ring/api-client';
import {
  evidenceExportEligibility,
  evidenceExportSuccessCaption,
  finalizationCaption,
  finalizationRecoveryEligibility,
  finalizationViewFromGoal,
} from './finalizationObserveView.js';

function goalStub(
  overrides: Partial<GoalResource> & {status: GoalResource['status']},
): GoalResource {
  return {
    id: 'g1',
    project_id: 'p1',
    created_at: '2026-09-13T00:00:00Z',
    updated_at: '2026-09-13T00:00:00Z',
    state_revision: 1,
    contract_revision: 1,
    plan_revision: null,
    contract_digest: 'sha256:' + '11'.repeat(32),
    previous_status: null,
    integration_commit: null,
    block_reason: null,
    write_epoch: '1',
    release_manifest_id: null,
    barrier: null,
    orchestration_backend: 'TEMPORAL',
    owner_epoch: '1',
    criterion_summary: {verified: 0, total: 1},
    budget_usage: {
      wall_clock_seconds: 0,
      tokens: 0,
      cost_usd: '0',
      tool_calls: 0,
      network_calls: 0,
      disk_bytes: 0,
      gpu_seconds: 0,
    },
    contract: {
      project_id: 'p1',
      title: 't',
      objective: 'o',
      success_criteria: [],
      policy_id: 'pol',
      model_profile_id: 'mod',
      skill_set_id: 'sk',
      budget: {
        wall_clock_seconds: 3600,
        max_tokens: 1,
        max_cost_usd: '1',
        max_tool_calls: 1,
        max_network_calls: 0,
        max_disk_bytes: 1,
        max_gpu_seconds: null,
      },
    },
    ...overrides,
  } as GoalResource;
}

const sealedBarrier = {
  id: 'b1',
  created_at: null,
  updated_at: null,
  goal_id: 'g1',
  status: 'SEALED' as const,
  write_epoch: '2',
  contract_revision: 1,
  plan_revision: 1,
  candidate_manifest_id: 'c1',
  in_flight_engineering: 0,
  unknown_effects: 0,
};

describe('finalizationObserveView', () => {
  it('无屏障时文案诚实', () => {
    const view = finalizationViewFromGoal(goalStub({status: 'RUNNING'}), null);
    expect(view.barrier).toBeNull();
    expect(finalizationCaption(view)).toContain('尚无最终屏障');
    expect(finalizationCaption(view)).toContain('≠ Goal DONE');
  });

  it('SEALED ≠ DONE', () => {
    const view = finalizationViewFromGoal(
      goalStub({
        status: 'VERIFYING',
        barrier: sealedBarrier,
      }),
      null,
    );
    expect(view.barrier?.status).toBe('SEALED');
    const caption = finalizationCaption(view);
    expect(caption).toContain('SEALED');
    expect(caption).toContain('≠ Goal DONE');
  });

  it('DONE 文案强调 Kernel 裁决', () => {
    const release: ReleaseView = {
      manifest: {
        id: 'rm1',
        created_at: '2026-09-13T01:00:00Z',
        project_id: 'p1',
        goal_id: 'g1',
        barrier_id: 'b1',
        write_epoch: '2',
        goal_contract_digest: 'sha256:' + '22'.repeat(32),
        plan_digest: 'sha256:' + '33'.repeat(32),
        candidate_manifest_id: 'c1',
        verification_profile_ids: [],
        audit_ids: [],
        evidence_ids: [],
        content_digest: 'sha256:' + '44'.repeat(32),
      },
      validity: {status: 'VALID', decision_ids: []},
    };
    const view = finalizationViewFromGoal(
      goalStub({
        status: 'DONE',
        release_manifest_id: 'rm1',
        barrier: {
          ...sealedBarrier,
          status: 'RELEASED',
        },
      }),
      release,
    );
    expect(view.release?.validityStatus).toBe('VALID');
    expect(finalizationCaption(view)).toContain('VerificationProfile');
    expect(finalizationCaption(view)).toContain('只读');
  });

  it('FINALIZATION_INSUFFICIENT + SEALED → REVERIFY/REWORK 可提交', () => {
    const view = finalizationViewFromGoal(
      goalStub({
        status: 'BLOCKED',
        block_reason: 'FINALIZATION_INSUFFICIENT',
        plan_revision: 2,
        state_revision: 5,
        write_epoch: '2',
        barrier: sealedBarrier,
      }),
      null,
    );
    const elig = finalizationRecoveryEligibility(view);
    expect(elig.eligible).toBe(true);
    expect(elig.actions).toEqual(['REVERIFY', 'REWORK']);
    expect(elig.requestBase?.barrier_id).toBe('b1');
    expect(finalizationCaption(view)).toContain('finalization-recovery');
  });

  it('FINALIZATION_FAIL 仅 REWORK；UNKNOWN effect 失败关闭', () => {
    const failView = finalizationViewFromGoal(
      goalStub({
        status: 'BLOCKED',
        block_reason: 'FINALIZATION_FAIL',
        plan_revision: 1,
        write_epoch: '2',
        barrier: sealedBarrier,
      }),
      null,
    );
    expect(finalizationRecoveryEligibility(failView).actions).toEqual(['REWORK']);

    const unknownView = finalizationViewFromGoal(
      goalStub({
        status: 'BLOCKED',
        block_reason: 'FINALIZATION_FAIL',
        plan_revision: 1,
        write_epoch: '2',
        barrier: {...sealedBarrier, unknown_effects: 1},
      }),
      null,
    );
    const elig = finalizationRecoveryEligibility(unknownView);
    expect(elig.eligible).toBe(false);
    expect(elig.denyReason).toContain('UNKNOWN');
  });

  it('DONE+VALID Release 可 INTERNAL_COPY；INVALIDATED 拒 OFFLINE', () => {
    const release: ReleaseView = {
      manifest: {
        id: 'rm1',
        created_at: '2026-09-13T01:00:00Z',
        project_id: 'p1',
        goal_id: 'g1',
        barrier_id: 'b1',
        write_epoch: '2',
        goal_contract_digest: 'sha256:' + '22'.repeat(32),
        plan_digest: 'sha256:' + '33'.repeat(32),
        candidate_manifest_id: 'c1',
        verification_profile_ids: [],
        audit_ids: [],
        evidence_ids: [],
        content_digest: 'sha256:' + '44'.repeat(32),
      },
      validity: {status: 'VALID', decision_ids: []},
    };
    const view = finalizationViewFromGoal(
      goalStub({
        status: 'DONE',
        release_manifest_id: 'rm1',
        barrier: {...sealedBarrier, status: 'RELEASED'},
      }),
      release,
    );
    const elig = evidenceExportEligibility(view);
    expect(elig.eligible).toBe(true);
    expect(elig.releaseManifestId).toBe('rm1');
    expect(elig.allowOffline).toBe(true);
    expect(evidenceExportSuccessCaption('INTERNAL_COPY')).toContain('≠ Goal DONE');

    const bad = finalizationViewFromGoal(
      goalStub({
        status: 'DONE',
        release_manifest_id: 'rm1',
        barrier: {...sealedBarrier, status: 'RELEASED'},
      }),
      {
        ...release,
        validity: {status: 'INVALIDATED', decision_ids: ['d1']},
      },
    );
    const offline = evidenceExportEligibility(bad);
    expect(offline.eligible).toBe(true);
    expect(offline.allowOffline).toBe(false);
    expect(offline.offlineDenyReason).toContain('INVALIDATED');
  });

  it('非终态不可导出', () => {
    const elig = evidenceExportEligibility(
      finalizationViewFromGoal(goalStub({status: 'RUNNING'}), null),
    );
    expect(elig.eligible).toBe(false);
    expect(elig.denyReason).toContain('RUNNING');
  });
});
