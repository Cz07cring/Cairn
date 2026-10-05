/**
 * 本仓 Agent 聊天面（类 Codex / Harness）：只读投影 Kernel 事实 + 跟流刷新。
 * Composer 在用户消息 API 未接通前失败关闭；聊完 ≠ Goal DONE。
 */
import {useEffect, useMemo, useRef, useState, type FormEvent} from 'react';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {
  consumeGoalEventsOnce,
  getAuthSession,
  getGoalSnapshot,
  listEffects,
  listGoalActivities,
  listModelInvocations,
  type BrowserClientAuth,
} from '@ring/api-client';
import {readStoredObserveGoalId} from './createGoalDraft.js';
import {
  agentChatComposeGate,
  buildAgentChatMessages,
  type AgentChatMessage,
} from './agentChatView.js';
import './agentChat.css';

const roleLabel: Record<AgentChatMessage['role'], string> = {
  system: '系统',
  assistant: '助手',
  tool: '工具',
  user: '你',
};

function formatTime(value: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(date);
}

/** 用户消息入 activation 的公开 API；未接通前恒 false。 */
const USER_MESSAGE_API_READY = false;

export function AgentChatPanel() {
  const queryClient = useQueryClient();
  const listRef = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState('');
  const [composeHint, setComposeHint] = useState<string | null>(null);
  const [autoFollow, setAutoFollow] = useState(true);
  const streamSeq = useRef('0');
  const streamGoal = useRef('');

  const sessionQuery = useQuery({
    queryKey: ['auth-session'],
    queryFn: ({signal}) => getAuthSession({signal}),
    retry: false,
  });
  const auth: BrowserClientAuth | null = useMemo(() => {
    if (sessionQuery.data) {
      return {kind: 'session', csrfToken: sessionQuery.data.csrf_token};
    }
    const bearer = String(import.meta.env.VITE_RING_DEV_BEARER ?? '').trim();
    return bearer ? {kind: 'bearer', authorization: bearer} : null;
  }, [sessionQuery.data]);

  const observeGoalQuery = useQuery({
    queryKey: ['observe-goal-id'],
    queryFn: () => readStoredObserveGoalId(),
    staleTime: 0,
  });
  const goalId = (
    observeGoalQuery.data ||
    readStoredObserveGoalId() ||
    String(import.meta.env.VITE_RING_GOAL_ID ?? '')
  ).trim();

  const goalQuery = useQuery({
    queryKey: ['agent-chat-goal', goalId, auth?.kind],
    enabled: auth != null && Boolean(goalId),
    queryFn: ({signal}) => getGoalSnapshot({auth: auth!, goalId, signal}),
    refetchInterval: 5_000,
  });
  const projectId =
    goalQuery.data?.goal.project_id ??
    String(import.meta.env.VITE_RING_PROJECT_ID ?? '').trim();

  const activitiesQuery = useQuery({
    queryKey: ['agent-chat-activities', goalId, auth?.kind],
    enabled: auth != null && Boolean(goalId),
    queryFn: ({signal}) => listGoalActivities({auth: auth!, goalId, signal, limit: 50}),
    refetchInterval: 5_000,
  });
  const invocationsQuery = useQuery({
    queryKey: ['agent-chat-models', projectId, goalId, auth?.kind],
    enabled: auth != null && Boolean(projectId) && Boolean(goalId),
    queryFn: ({signal}) =>
      listModelInvocations({auth: auth!, projectId, goalId, signal, limit: 50}),
    refetchInterval: 5_000,
  });
  const effectsQuery = useQuery({
    queryKey: ['agent-chat-effects', projectId, goalId, auth?.kind],
    enabled: auth != null && Boolean(projectId) && Boolean(goalId),
    queryFn: ({signal}) =>
      listEffects({auth: auth!, projectId, goalId, signal, limit: 50}),
    refetchInterval: 5_000,
  });

  useEffect(() => {
    if (!auth || !goalId || !goalQuery.data?.latest_seq) return;
    if (streamGoal.current !== goalId) {
      streamGoal.current = goalId;
      streamSeq.current = goalQuery.data.latest_seq;
    }
    const controller = new AbortController();
    let stopped = false;
    const wait = (ms: number) =>
      new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, ms);
        controller.signal.addEventListener(
          'abort',
          () => {
            clearTimeout(timer);
            resolve();
          },
          {once: true},
        );
      });
    const listen = async () => {
      while (!stopped) {
        try {
          const result = await consumeGoalEventsOnce({
            auth,
            goalId,
            afterSeq: streamSeq.current,
            signal: controller.signal,
            maxEvents: 20,
          });
          if (stopped) return;
          if (result.cursorExpired) {
            streamSeq.current = '0';
            await queryClient.invalidateQueries({queryKey: ['agent-chat-goal', goalId]});
          } else if (result.events.length > 0) {
            streamSeq.current = result.lastSeq;
            await Promise.all([
              queryClient.invalidateQueries({queryKey: ['agent-chat-goal', goalId]}),
              queryClient.invalidateQueries({queryKey: ['agent-chat-activities', goalId]}),
              queryClient.invalidateQueries({queryKey: ['agent-chat-models']}),
              queryClient.invalidateQueries({queryKey: ['agent-chat-effects']}),
            ]);
          }
          await wait(500);
        } catch {
          if (stopped || controller.signal.aborted) return;
          await wait(2_000);
        }
      }
    };
    void listen();
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [auth, goalId, goalQuery.data?.latest_seq, queryClient]);

  const messages = useMemo(
    () =>
      buildAgentChatMessages({
        activities: activitiesQuery.data ?? [],
        invocations: invocationsQuery.data ?? [],
        effects: effectsQuery.data ?? [],
      }),
    [activitiesQuery.data, invocationsQuery.data, effectsQuery.data],
  );

  useEffect(() => {
    if (!autoFollow || !listRef.current) return;
    listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [messages, autoFollow]);

  const composeGate = agentChatComposeGate({
    hasObserveGoal: Boolean(goalId),
    userMessageApiReady: USER_MESSAGE_API_READY,
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (!composeGate.ok) {
      setComposeHint(composeGate.reason);
      return;
    }
    setComposeHint(null);
    setDraft('');
  };

  return (
    <section className="wb-surface wb-agent-chat" aria-labelledby="agent-chat-title">
      <header className="wb-agent-chat-header">
        <div>
          <small>执行交流 · 本仓 Agent 聊天（类 Codex / Harness）</small>
          <h2 id="agent-chat-title">与执行腿跟流对话</h2>
          <p>
            展示模型工作摘要与工具回执；不展示私有推理。聊完或工具成功 ≠ Goal DONE。
          </p>
        </div>
        <button
          type="button"
          className={autoFollow ? 'active' : ''}
          onClick={() => setAutoFollow((v) => !v)}
        >
          {autoFollow ? '自动跟随：开' : '自动跟随：关'}
        </button>
      </header>

      {!goalId ? (
        <p className="observe-error">请先选择要观察的目标。</p>
      ) : auth == null && !sessionQuery.isPending ? (
        <p className="observe-error">登录后才能读取执行交流。</p>
      ) : (
        <div className="wb-agent-chat-list" ref={listRef} role="log" aria-live="polite">
          {messages.length === 0 ? (
            <div className="wb-agent-chat-empty">
              <b>还没有可展示的执行交流</b>
              <span>目标启动并产生模型轮次或工具回执后，会以对话气泡出现在这里。</span>
            </div>
          ) : (
            messages.map((msg) => (
              <article
                key={msg.id}
                className={`wb-agent-chat-bubble is-${msg.role} is-${msg.tone}`}
              >
                <div className="wb-agent-chat-meta">
                  <span>{roleLabel[msg.role]}</span>
                  <span>{msg.meta}</span>
                  <time>{formatTime(msg.occurredAt)}</time>
                </div>
                <p>{msg.body}</p>
                <details>
                  <summary>技术详情</summary>
                  <pre>{msg.details}</pre>
                </details>
              </article>
            ))
          )}
        </div>
      )}

      <form className="wb-agent-chat-composer" onSubmit={onSubmit}>
        <label className="sr-only" htmlFor="agent-chat-draft">
          发给执行腿的消息
        </label>
        <textarea
          id="agent-chat-draft"
          rows={2}
          value={draft}
          placeholder={
            composeGate.ok
              ? '输入要交给执行腿的下一条消息…'
              : '跟流只读；用户消息入口接通后可在此交流'
          }
          onChange={(e) => setDraft(e.target.value)}
          disabled={!composeGate.ok}
        />
        <div className="wb-agent-chat-composer-actions">
          <span className="wb-agent-chat-boundary">
            {composeHint ??
              (composeGate.ok
                ? '发送走受控入口，不会绕过 Broker。'
                : composeGate.reason)}
          </span>
          <button type="submit" disabled={!composeGate.ok || !draft.trim()}>
            发送
          </button>
        </div>
      </form>
    </section>
  );
}
