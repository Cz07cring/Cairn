/**
 * 控制面鉴权头归一化（全仓单一事实来源）。
 *
 * **为什么需要**：本仓调用方（`serve_joint_stack` / `RunActivation` / 各 port 工厂）
 * 一律传**裸 token**（如 `planEnv.workerJwt`），而 `Authorization: Bearer <token>`
 * 的 `Bearer ` 前缀必须由**发请求的一方**补。此前各 port 工厂各写各的：
 * `controlHttpPorts.authHeaders` 补了前缀，而 `artifactPutHttpPorts` /
 * `brokerToolHttpPorts` / `effectObserveHttpPorts` 等**原样透出** ⇒ 控制面判
 * `401 UNAUTHENTICATED 请先登录`。
 *
 * 实测代价（2026-09-15）：审计回合按「上传采集载荷 → 登记 step → prepare/dispatch」
 * 逐个撞墙，每修一处就暴露下一处同类缺陷，而失败**不传导到活动状态** ——
 * 活动永停 RUNNING，外部只看到安静的卡住。故此处统一，避免继续逐点打补丁。
 *
 * 幂等：已带前缀者原样返回，不会叠加成 `Bearer Bearer …`。
 */
export function bearerAuthHeader(authorization: string): string {
  const raw = (authorization ?? '').trim();
  if (!raw) {
    throw new Error('AUTH_HEADER_EMPTY: 调用方未提供 worker 凭据');
  }
  return raw.startsWith('Bearer ') ? raw : `Bearer ${raw}`;
}

/** 便捷构造：返回可直接展开进 fetch headers 的对象。 */
export function bearerAuthHeaders(
  authorization: string,
  extra: Record<string, string> = {},
): Record<string, string> {
  return {Authorization: bearerAuthHeader(authorization), ...extra};
}
