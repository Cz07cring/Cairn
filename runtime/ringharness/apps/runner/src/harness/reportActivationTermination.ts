/**
 * Runner → Control：登记 activation 确定性终止（M3.5）；≠ Goal DONE。
 */
export type ActivationTerminationLease = {
  activity_id: string;
  attempt_id: string;
  fencing_epoch: string;
};

export type ReportActivationTerminationInput = {
  baseUrl: string;
  authorization: string;
  activityId: string;
  lease: ActivationTerminationLease;
  reason:
    | 'NO_PROGRESS_STOP'
    | 'BUDGET_EXHAUSTED'
    | 'CANCELLED'
    | 'GOAL_REQUIRES_REVIEW';
  detail?: string;
  summaryArtifactId?: string | null;
  closeoutArtifactId?: string | null;
  fetchImpl?: typeof fetch;
};

export type ReportActivationTerminationResult = {
  id: string;
  reason: string;
  marksGoalDone: false;
  summaryArtifactId?: string | null;
  closeoutArtifactId?: string | null;
};

/**
 * POST /internal/v1/activities/{id}/activation-terminations。
 * 失败向上抛；调用方可吞并仍返回 FAILED activation（≠ DONE）。
 */
export async function reportActivationTermination(
  input: ReportActivationTerminationInput,
): Promise<ReportActivationTerminationResult> {
  const fetchFn = input.fetchImpl ?? fetch;
  const base = input.baseUrl.replace(/\/$/, '');
  const res = await fetchFn(
    `${base}/internal/v1/activities/${input.activityId}/activation-terminations`,
    {
      method: 'POST',
      headers: {
        Authorization: input.authorization.startsWith('Bearer ')
          ? input.authorization
          : `Bearer ${input.authorization}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        lease: {
          activity_id: input.lease.activity_id,
          attempt_id: input.lease.attempt_id,
          fencing_epoch: input.lease.fencing_epoch,
        },
        reason: input.reason,
        ...(input.detail ? {detail: input.detail} : {}),
        ...(input.summaryArtifactId
          ? {summary_artifact_id: input.summaryArtifactId}
          : {}),
        ...(input.closeoutArtifactId
          ? {closeout_artifact_id: input.closeoutArtifactId}
          : {}),
      }),
    },
  );
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(
      `ACTIVATION_TERMINATION_HTTP_${res.status}:${text.slice(0, 200)}`,
    );
  }
  const body = (await res.json()) as {
    data: {
      id: string;
      reason: string;
      marks_goal_done: boolean;
      summary_artifact_id?: string | null;
      closeout_artifact_id?: string | null;
    };
  };
  if (body.data.marks_goal_done !== false) {
    throw new Error('ACTIVATION_TERMINATION_MARKED_GOAL_DONE');
  }
  return {
    id: body.data.id,
    reason: body.data.reason,
    marksGoalDone: false,
    summaryArtifactId: body.data.summary_artifact_id,
    closeoutArtifactId: body.data.closeout_artifact_id,
  };
}
