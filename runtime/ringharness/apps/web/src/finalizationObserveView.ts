/**
 * 最终屏障 / Release 投影：展示 Kernel 事实；recovery 提交不计算 DONE。
 */
import type {BarrierResource, GoalResource, ReleaseView} from '@ring/api-client';

export type FinalizationObserveView = {
  goalId: string;
  goalStatus: string;
  blockReason: string | null;
  stateRevision: number;
  planRevision: number | null;
  writeEpoch: string;
  barrier: {
    id: string;
    status: BarrierResource['status'];
    writeEpoch: string;
    candidateManifestId: string | null;
    inFlightEngineering: number;
    unknownEffects: number;
  } | null;
  release: {
    manifestId: string;
    validityStatus: string;
    writeEpoch: string;
  } | null;
};

export type FinalizationRecoveryAction = 'REVERIFY' | 'REWORK';

export type FinalizationRecoveryEligibility = {
  eligible: boolean;
  actions: FinalizationRecoveryAction[];
  denyReason: string | null;
  requestBase: {
    expected_state_revision: number;
    expected_plan_revision: number;
    barrier_id: string;
    expected_write_epoch: string;
  } | null;
};

export function finalizationViewFromGoal(
  goal: GoalResource,
  release: ReleaseView | null,
): FinalizationObserveView {
  const barrier = goal.barrier
    ? {
        id: goal.barrier.id,
        status: goal.barrier.status,
        writeEpoch: goal.barrier.write_epoch,
        candidateManifestId: goal.barrier.candidate_manifest_id ?? null,
        inFlightEngineering: goal.barrier.in_flight_engineering,
        unknownEffects: goal.barrier.unknown_effects,
      }
    : null;

  const releaseView =
    release?.manifest != null
      ? {
          manifestId: release.manifest.id,
          validityStatus: release.validity?.status ?? 'UNKNOWN',
          writeEpoch: release.manifest.write_epoch,
        }
      : goal.release_manifest_id
        ? {
            // Goal 上有指针但 GET release 失败/空：只展示指针，不宣称 VALID
            manifestId: goal.release_manifest_id,
            validityStatus: 'UNFETCHED',
            writeEpoch: barrier?.writeEpoch ?? '—',
          }
        : null;

  return {
    goalId: goal.id,
    goalStatus: goal.status,
    blockReason: goal.block_reason ?? null,
    stateRevision: goal.state_revision,
    planRevision: goal.plan_revision ?? null,
    writeEpoch: goal.write_epoch,
    barrier,
    release: releaseView,
  };
}

/** 诚实文案：屏障态 ≠ PASS ≠ DONE（除非 Goal 已是 DONE 且由 Kernel 裁决）。 */
export function finalizationCaption(view: FinalizationObserveView): string {
  if (view.goalStatus === 'DONE') {
    return 'Goal 状态为 DONE：须为 Kernel 在固定 VerificationProfile + 最终屏障上的裁决；本面板只读，不重算 DONE。';
  }
  if (view.goalStatus === 'BLOCKED') {
    const reason = view.blockReason ?? '（无 block_reason）';
    return `Goal BLOCKED（${reason}）：仅可经 finalization-recovery；不能强制 DONE。`;
  }
  if (view.barrier == null) {
    return '尚无最终屏障：INTEGRATE 成功前不会出现 barrier；空 ≠ 验收 PASS，≠ Goal DONE。';
  }
  switch (view.barrier.status) {
    case 'DRAINING':
      return '屏障 DRAINING：等待在途 ENGINEERING 排空；≠ SEALED，≠ DONE。';
    case 'SEALED':
      return '屏障 SEALED：等待 FINALIZE GLOBAL 验收材料；SEALED ≠ Goal DONE。';
    case 'RELEASED':
      return '屏障 RELEASED：通常伴随 Goal DONE；若 Goal 非 DONE 须核对 Kernel 事实，禁止前端自报完成。';
    case 'ABORTED':
      return '屏障 ABORTED：需经 finalization-recovery（REVERIFY/REWORK）；不能强制 DONE。';
    default:
      return `屏障状态 ${view.barrier.status}：只读观察，不贡献 PASS。`;
  }
}

/**
 * 对齐 Kernel recover_finalization 门禁的前端投影（不替代服务端校验）。
 * 合格时给出可提交 action 列表；永远不暗示提交后即 DONE。
 */
export function finalizationRecoveryEligibility(
  view: FinalizationObserveView,
): FinalizationRecoveryEligibility {
  if (view.goalStatus !== 'BLOCKED') {
    return {
      eligible: false,
      actions: [],
      denyReason: '仅 Goal BLOCKED 可提交 finalization-recovery',
      requestBase: null,
    };
  }
  if (
    view.blockReason !== 'FINALIZATION_FAIL' &&
    view.blockReason !== 'FINALIZATION_INSUFFICIENT'
  ) {
    return {
      eligible: false,
      actions: [],
      denyReason:
        'block_reason 须为 FINALIZATION_FAIL 或 FINALIZATION_INSUFFICIENT',
      requestBase: null,
    };
  }
  if (view.barrier == null || view.barrier.status !== 'SEALED') {
    return {
      eligible: false,
      actions: [],
      denyReason: '屏障须存在且为 SEALED',
      requestBase: null,
    };
  }
  if (view.barrier.unknownEffects > 0) {
    return {
      eligible: false,
      actions: [],
      denyReason: '存在 UNKNOWN/DISPATCHED 在途 effect，禁止恢复写入',
      requestBase: null,
    };
  }
  if (view.planRevision == null || view.planRevision < 1) {
    return {
      eligible: false,
      actions: [],
      denyReason: '缺少有效 plan_revision',
      requestBase: null,
    };
  }
  const actions: FinalizationRecoveryAction[] =
    view.blockReason === 'FINALIZATION_INSUFFICIENT'
      ? ['REVERIFY', 'REWORK']
      : ['REWORK'];
  return {
    eligible: true,
    actions,
    denyReason: null,
    requestBase: {
      expected_state_revision: view.stateRevision,
      expected_plan_revision: view.planRevision,
      barrier_id: view.barrier.id,
      expected_write_epoch: view.writeEpoch,
    },
  };
}

export type EvidenceExportTrustMode = 'INTERNAL_COPY' | 'OFFLINE_VERIFIABLE';

export type EvidenceExportEligibility = {
  eligible: boolean;
  denyReason: string | null;
  releaseManifestId: string | null;
  allowOffline: boolean;
  offlineDenyReason: string | null;
};

/**
 * 对齐 Kernel start_evidence_export 门禁投影；202 排队 ≠ 导出字节就绪 ≠ DONE。
 */
export function evidenceExportEligibility(
  view: FinalizationObserveView,
): EvidenceExportEligibility {
  if (!['DONE', 'CANCELLED', 'FAILED'].includes(view.goalStatus)) {
    return {
      eligible: false,
      denyReason: `Goal 状态为 ${view.goalStatus}，不可导出（须终态且已有 Release）`,
      releaseManifestId: null,
      allowOffline: false,
      offlineDenyReason: null,
    };
  }
  if (view.release == null) {
    return {
      eligible: false,
      denyReason: '尚无 ReleaseManifest，无法导出证据',
      releaseManifestId: null,
      allowOffline: false,
      offlineDenyReason: null,
    };
  }
  const invalidated = view.release.validityStatus === 'INVALIDATED';
  return {
    eligible: true,
    denyReason: null,
    releaseManifestId: view.release.manifestId,
    allowOffline: !invalidated,
    offlineDenyReason: invalidated
      ? 'ReleaseValidity INVALIDATED：拒绝 OFFLINE_VERIFIABLE（可试 INTERNAL_COPY）'
      : null,
  };
}

export function evidenceExportSuccessCaption(trustMode: EvidenceExportTrustMode): string {
  return `已受理 EXPORT_EVIDENCE（${trustMode}）：仅排队 READY Activity，不捏造 artifact；≠ Goal DONE。`;
}
