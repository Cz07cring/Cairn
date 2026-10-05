/**
 * AB01 × Effect Gateway：把 BrokerBackedHarnessTool 接到两轮缝的 executeTool。
 *
 * Runner 只 prepare + 观察；不本地 dispatch（Broker 为唯一真实派发方）。
 * ≠ 官方 AgentLoop；≠ Goal DONE。
 */
import {
  createBrokerBackedHarnessTool,
  type BrokerBackedHarnessTool,
  type BrokerBackedHarnessToolConfig,
  type HarnessToolCall,
  type HarnessToolResult,
} from './brokerBackedHarnessTool.js';
import type {Ab01ExecuteTool} from './ab01TwoTurnLoop.js';

export type Ab01BrokerGateway = {
  /** 交给 runAb01ReadFileTwoTurn 的 executeTool */
  executeTool: Ab01ExecuteTool;
  tool: BrokerBackedHarnessTool;
};

/**
 * 从 BrokerBackedHarnessTool 配置构造 AB01 工具执行口。
 * 调用方负责提供真实或协议形 fetchImpl / lease。
 */
export function createAb01BrokerExecuteTool(
  config: BrokerBackedHarnessToolConfig,
): Ab01BrokerGateway {
  const tool = createBrokerBackedHarnessTool(config);
  return {
    tool,
    executeTool: (call: HarnessToolCall): Promise<HarnessToolResult> => tool.execute(call),
  };
}
