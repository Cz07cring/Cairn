import type {paths} from './generated';
export type Project = NonNullable<paths['/api/v1/projects']['get']['responses'][200]['content']['application/json']['data']>[number];
export {
  listGoalAuditItems,
  type GoalAuditItem,
  type GoalReviewResource,
} from './goalAudits';
export {
  getGoal,
  getGoalRelease,
  getGoalWallBudget,
  getGoalReviewBudget,
  postFinalizationRecovery,
  postEvidenceExport,
  type GoalResource,
  type BarrierResource,
  type ReleaseView,
  type GoalWallBudgetSnapshot,
  type GoalReviewBudgetSnapshot,
  type FinalizationRecoveryRequest,
  type EvidenceExportRequest,
  type CommandOperation,
} from './goalFinalization';
export {
  buildAuthLoginHref,
  getAuthSession,
  postAuthLogout,
  type AuthSession,
  type LogoutResult,
} from './authSession';
export {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth';
export {
  getGoalSnapshot,
  consumeGoalEventsOnce,
  parseSseBlock,
  type GoalSnapshot,
  type GoalSseEvent,
  type ConsumeGoalEventsResult,
} from './goalEvents';
export {
  listCommands,
  pauseGoal,
  resumeGoal,
  cancelGoal,
} from './goalCommands';
export {
  listEffects,
  postEffectReconcile,
  type EffectResource,
  type ReconciliationRequest,
} from './effectsObserve';
export {
  listGoalActivities,
  type ActivityResource,
  type ActivityKind,
  type ActivityStatus,
} from './activitiesObserve';
export {
  listGoalTasks,
  type TaskResource,
  type TaskStatus,
} from './tasksObserve';
export {
  listGoalPlans,
  type PlanResource,
} from './plansObserve';
export {
  listMemories,
  type MemoryResource,
  type MemoryKind,
} from './memoriesObserve';
export {
  listModelInvocations,
  type ModelInvocationResource,
  type ModelInvocationStatus,
} from './modelInvocationsObserve';
export {
  listTaskEvidence,
  type EvidenceEnvelopeResource,
} from './taskEvidenceObserve';
export {
  listApprovals,
  postApprovalDecision,
  postApprovalRevoke,
  type ApprovalResource,
  type ApprovalDecision,
} from './approvals';
export {
  listQuarantinedObligations,
  getSystemStatus,
  type VerificationObligationResource,
  type SystemStatus,
} from './quarantineInbox';
export {
  listOrchestrationAbandonments,
  releaseOrchestrationAbandonment,
  type OrchestrationAbandonmentResource,
} from './goalAbandonment';
export {
  listActivationTerminations,
  postNoProgressReviewAck,
  type ActivationTerminationResource,
} from './goalActivationTerminations';
export {
  listProjects,
  listGoals,
  listPolicies,
  listModelProfiles,
  listSkillSets,
  listVerificationProfiles,
  createGoal,
  startGoal,
  type ProjectResource,
  type GoalCreate,
  type ControlRequest,
  type PolicyResource,
  type ModelProfileResource,
  type SkillSetResource,
  type VerificationProfileResource,
} from './goalWorkspace';
/** This public health probe carries no credentials or project data. */
export async function getLiveness(signal?: AbortSignal): Promise<{alive:boolean}> {
 const response=await fetch('/health/live',{signal,cache:'no-store'});
 if(!response.ok)throw new Error('控制服务暂不可用');
 const result:unknown=await response.json();
 if(typeof result!=='object'||result===null||!('alive' in result)||typeof result.alive!=='boolean')throw new Error('服务响应无法识别');
 return {alive:result.alive};
}
