/** 本地防御：PLAN 禁止任何工具；其余须再经 Kernel/Broker 鉴权。 */
export function authorizeToolProposal(
  kind: 'PLAN' | 'EXECUTE' | 'AUDIT' | 'INTEGRATE' | 'FINALIZE',
  tool: string,
  registeredTools: ReadonlySet<string>,
): {tool: string; requiresBrokerAuthorization: true} {
  if (kind === 'PLAN') {
    throw new Error('ROLE_TOOL_FORBIDDEN');
  }
  if (!registeredTools.has(tool)) {
    throw new Error('TOOL_NOT_ALLOWED');
  }
  return {tool, requiresBrokerAuthorization: true};
}
