/**
 * AB08：运行中用户消息门禁。
 *
 * 在 Turn 仍接受输入时：消息原子挂到当前 Turn（accepted → 可 drain）。
 * Turn 已 seal 后：消息进入新 Turn 种子，不在旧 Turn 双跑。
 * 同 messageId 幂等为 duplicate。绝不以接管/封尾冒充 Goal DONE。
 */

export type TurnUserMessage = {
  messageId: string;
  text: string;
};

export type TurnMessageDisposition =
  | {
      kind: 'attached';
      turnId: string;
      messageId: string;
      marksGoalDone: false;
    }
  | {
      kind: 'deferred_new_turn';
      turnId: string;
      priorTurnId: string;
      messageId: string;
      marksGoalDone: false;
    }
  | {
      kind: 'duplicate';
      turnId: string;
      messageId: string;
      marksGoalDone: false;
    };

export type TurnSealResult = {
  turnId: string;
  /** seal 前已挂上、待注入下一模型边界的消息 */
  attached: readonly TurnUserMessage[];
  marksGoalDone: false;
};

export type TurnUserMessageGate = {
  turnId: string;
  /** 尾部竞态：与 seal 互斥串行，保证不丢不双跑 */
  offer: (message: TurnUserMessage) => Promise<TurnMessageDisposition>;
  /** 关闭当前 Turn 的输入窗；之后 offer 只能开新 Turn */
  seal: () => Promise<TurnSealResult>;
  /** 只读快照：已挂未 drain 的消息（测/观测用） */
  peekAttached: () => readonly TurnUserMessage[];
  /** 是否仍接受挂到本 Turn */
  isAccepting: () => boolean;
  /** seal 后若有迟到消息，取出新 Turn 种子（至多一次） */
  takeDeferredTurn: () => {
    turnId: string;
    messages: readonly TurnUserMessage[];
  } | null;
};

export type TurnUserMessageGateConfig = {
  turnId: string;
  /** 生成新 Turn id；默认 uuid 风格 */
  newTurnId?: () => string;
};

function defaultNewTurnId(): string {
  return `turn-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function createTurnUserMessageGate(
  config: TurnUserMessageGateConfig,
): TurnUserMessageGate {
  if (!config.turnId || !config.turnId.trim()) {
    throw new Error('TURN_ID_REQUIRED');
  }
  const newTurnIdFn = config.newTurnId ?? defaultNewTurnId;
  const turnId = config.turnId;
  const seen = new Set<string>();
  const attached: TurnUserMessage[] = [];
  let accepting = true;
  let deferred: {turnId: string; messages: TurnUserMessage[]} | null = null;
  // 串行化 offer/seal，消除尾部竞态下的双写
  let chain: Promise<void> = Promise.resolve();

  function enqueue<T>(fn: () => T): Promise<T> {
    const run = chain.then(() => fn());
    chain = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }

  return {
    turnId,

    offer(message) {
      return enqueue(() => {
        if (!message.messageId || !message.messageId.trim()) {
          throw new Error('MESSAGE_ID_REQUIRED');
        }
        if (seen.has(message.messageId)) {
          return {
            kind: 'duplicate' as const,
            turnId,
            messageId: message.messageId,
            marksGoalDone: false as const,
          };
        }
        seen.add(message.messageId);
        const copy: TurnUserMessage = {
          messageId: message.messageId,
          text: message.text,
        };
        if (accepting) {
          attached.push(copy);
          return {
            kind: 'attached' as const,
            turnId,
            messageId: copy.messageId,
            marksGoalDone: false as const,
          };
        }
        // 已 seal：开新 Turn，消息进新种子（不回灌旧 Turn）
        if (deferred === null) {
          deferred = {turnId: newTurnIdFn(), messages: [copy]};
        } else {
          deferred.messages.push(copy);
        }
        return {
          kind: 'deferred_new_turn' as const,
          turnId: deferred.turnId,
          priorTurnId: turnId,
          messageId: copy.messageId,
          marksGoalDone: false as const,
        };
      });
    },

    seal() {
      return enqueue(() => {
        accepting = false;
        return {
          turnId,
          attached: attached.map((m) => ({...m})),
          marksGoalDone: false as const,
        };
      });
    },

    peekAttached() {
      return attached.map((m) => ({...m}));
    },

    isAccepting() {
      return accepting;
    },

    takeDeferredTurn() {
      if (deferred === null) {
        return null;
      }
      const out = {
        turnId: deferred.turnId,
        messages: deferred.messages.map((m) => ({...m})),
      };
      deferred = null;
      return out;
    },
  };
}
