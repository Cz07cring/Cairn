/**
 * ModelInvocation 列表投影；SUCCEEDED ≠ Goal DONE；不展示原始模型输入。
 */

export type ModelInvocationRow = {
  id: string;
  status: string;
  usageStatus: string;
  modelId: string;
  providerRef: string;
  goalId: string;
  activityId: string;
  invocationSeq: number;
  inputDigest: string;
  payloadDigest: string;
  toolCallCount: number;
};

export type ModelInvocationListItem = {
  id: string;
  status: string;
  usage_status: string;
  model_id: string;
  provider_ref: string;
  goal_id?: string | null;
  activity_id: string;
  invocation_seq: number;
  input_digest: string;
  payload_digest: string;
  tool_calls?: unknown[] | null;
};

export function modelInvocationRowsFromList(
  items: ModelInvocationListItem[],
): ModelInvocationRow[] {
  return items.map((item) => ({
    id: item.id,
    status: String(item.status ?? ''),
    usageStatus: String(item.usage_status ?? ''),
    modelId: String(item.model_id ?? ''),
    providerRef: String(item.provider_ref ?? ''),
    goalId: item.goal_id == null ? '' : String(item.goal_id),
    activityId: String(item.activity_id ?? ''),
    invocationSeq: Number(item.invocation_seq),
    inputDigest: String(item.input_digest ?? ''),
    payloadDigest: String(item.payload_digest ?? ''),
    toolCallCount: Array.isArray(item.tool_calls) ? item.tool_calls.length : 0,
  }));
}

export function modelInvocationsCaption(input: {
  rowCount: number;
  succeededCount: number;
  unknownCount: number;
}): string {
  return (
    `共 ${input.rowCount} 条调用；SUCCEEDED ${input.succeededCount}，UNKNOWN ${input.unknownCount}。` +
    `模型成功或 usage CONFIRMED ≠ Goal DONE。列表不展示原始输入。`
  );
}

export function modelInvocationStatusCaption(status: string): string {
  if (status === 'SUCCEEDED') {
    return 'SUCCEEDED：模型调用成功；≠ Goal/Task DONE。';
  }
  if (status === 'UNKNOWN') {
    return 'UNKNOWN：须对账；禁止盲重放；≠ DONE。';
  }
  if (status === 'DISPATCHED') {
    return 'DISPATCHED：已外呼、结果未确认；≠ DONE。';
  }
  if (status === 'FAILED') {
    return 'FAILED：调用失败；≠ Goal FAILED 裁决。';
  }
  return `${status}：只读观察。`;
}
