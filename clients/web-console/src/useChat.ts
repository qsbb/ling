/**
 * 聊天状态机：会话、消息、SSE 流、停止、失败重试。
 *
 * 原则：
 * - 浏览器不存权威历史；刷新/切换会话一律从 `/v1/sessions/{id}/messages` 重建。
 * - 增量文本只做展示；终态以服务端 `turn.completed/stopped/failed` 或消息接口为准。
 * - 停止后不再追加文本（即使还有迟到的 delta）。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from './api';
import { explainError, type ExplainedError } from './errors';
import { newClientTurnId } from './format';
import { navigate, routePath } from './route';
import { consumeSse, StreamAbortError, StreamTimeoutError } from './sse';
import type {
  ChatMessage,
  DeltaData,
  SessionRecord,
  SseEvent,
  TurnFinalData,
  TurnStartedData,
  TurnState,
  TurnView,
} from './types';

/** 无新数据多久算超时（服务端会先给 provider_timeout，这里只兜底网络静默）。 */
const STREAM_IDLE_MS = 45_000;
const POLL_MS = 2_000;
const STOP_FALLBACK_MS = 5_000;

export interface UiMessage {
  key: string;
  messageId: string | null;
  turnId: string | null;
  clientTurnId: string | null;
  role: 'user' | 'actor';
  system: boolean;
  text: string;
  state: TurnState;
  createdAt: string;
  modelLabel: string | null;
  errorCode: string | null;
  errorMessage: string | null;
  errorDetail: string | null;
  errorTraceId: string | null;
  retriable: boolean;
  local: boolean;
  attempt: number;
  /** 她选择不回时的理由。非空才显示。 */
  silenceReason: string | null;
}

export interface LiveTurn {
  kind: 'send' | 'retry';
  sessionId: string;
  turnId: string | null;
  actorKey: string;
  state: 'connecting' | 'streaming' | 'stopping';
  stopRequested: boolean;
  startedAt: number;
}

export interface Notice {
  kind: 'error' | 'info';
  text: string;
  detail?: string;
}

function buildServerMessage(message: ChatMessage): UiMessage {
  const failed = message.state === 'failed';
  const explained = failed
    ? explainError({ code: message.error_code ?? 'internal', message: '', retriable: true })
    : null;
  return {
    key: message.message_id,
    messageId: message.message_id,
    turnId: message.turn_id,
    clientTurnId: null,
    role: message.role === 'user' ? 'user' : 'actor',
    system: message.role === 'system',
    text: message.text,
    state: message.state,
    createdAt: message.created_at,
    modelLabel: message.model_label,
    errorCode: message.error_code,
    errorMessage: explained?.reason ?? null,
    errorDetail: explained?.hint ?? null,
    errorTraceId: null,
    retriable: message.state === 'failed' || message.state === 'stopped',
    local: false,
    attempt: 1,
    silenceReason:
      message.handled_as === 'ignored' || message.handled_as === 'missed'
        ? (message.handled_reason ?? '')
        : null,
  };
}

function mapServerMessages(messages: ChatMessage[]): UiMessage[] {
  return messages.map(buildServerMessage);
}

function emptyActor(key: string, turnId: string | null): UiMessage {
  return {
    key,
    messageId: null,
    turnId,
    clientTurnId: null,
    role: 'actor',
    system: false,
    text: '',
    state: 'pending',
    createdAt: new Date().toISOString(),
    modelLabel: null,
    errorCode: null,
    errorMessage: null,
    errorDetail: null,
    errorTraceId: null,
    retriable: false,
    local: true,
    attempt: 1,
    silenceReason: null,
  };
}

interface PumpOptions {
  kind: 'send' | 'retry';
  sessionId: string;
  clientTurnId: string | null;
  text: string | null;
  turnId: string | null;
  actorKey: string;
  userKey: string | null;
}

export interface ChatState {
  sessions: SessionRecord[];
  currentId: string | null;
  messages: UiMessage[];
  activeTurn: TurnView | null;
  live: LiveTurn | null;
  notice: Notice | null;
  bootstrapping: boolean;
  loadingMessages: boolean;
  loadError: ExplainedError | null;
  send: (text: string) => void;
  resend: (message: UiMessage) => void;
  retry: (turnId: string) => void;
  stop: () => void;
  reloadMessages: () => void;
  reloadSessions: () => void;
  createSession: () => void;
  renameSession: (sessionId: string, title: string) => Promise<void>;
  selectSession: (sessionId: string) => void;
  dismissNotice: () => void;
}

export function useChat(
  routeSessionId: string | null,
  onTestMode: (value: boolean) => void,
): ChatState {
  const [sessions, setSessions] = useState<SessionRecord[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [activeTurn, setActiveTurn] = useState<TurnView | null>(null);
  const [live, setLive] = useState<LiveTurn | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [bootstrapping, setBootstrapping] = useState(true);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [loadError, setLoadError] = useState<ExplainedError | null>(null);

  const currentIdRef = useRef<string | null>(null);
  const liveRef = useRef<LiveTurn | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const messagesRef = useRef<UiMessage[]>([]);
  const activeTurnRef = useRef<TurnView | null>(null);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  useEffect(() => {
    activeTurnRef.current = activeTurn;
  }, [activeTurn]);

  const commitLive = useCallback((next: LiveTurn | null) => {
    liveRef.current = next;
    setLive(next);
  }, []);

  const reloadSessions = useCallback(() => {
    void api
      .listSessions()
      .then((list) => setSessions(list))
      .catch(() => undefined);
  }, []);

  const loadMessages = useCallback(async (sessionId: string) => {
    if (!sessionId) return;
    setLoadingMessages(true);
    setLoadError(null);
    try {
      const data = await api.messages(sessionId);
      if (currentIdRef.current !== sessionId) return;
      setMessages(mapServerMessages(data.messages));
      setActiveTurn(data.active_turn);
    } catch (error) {
      if (currentIdRef.current !== sessionId) return;
      const explained = explainError(error);
      if (explained.code !== 'unauthorized') setLoadError(explained);
    } finally {
      if (currentIdRef.current === sessionId) setLoadingMessages(false);
    }
  }, []);

  const reloadMessages = useCallback(() => {
    const sessionId = currentIdRef.current;
    if (sessionId) void loadMessages(sessionId);
  }, [loadMessages]);

  /* ------------------------------------------------------------ 首次/切换会话 */
  useEffect(() => {
    let cancelled = false;
    abortRef.current?.abort();
    abortRef.current = null;
    commitLive(null);
    setActiveTurn(null);
    setMessages([]);
    setCurrentId(null);
    currentIdRef.current = null;
    setBootstrapping(true);

    (async () => {
      try {
        const list = await api.listSessions();
        if (cancelled) return;
        setSessions(list);
        let target = routeSessionId;
        if (target && !list.some((session) => session.session_id === target)) {
          target = null;
        }
        if (!target) {
          if (list.length > 0) {
            target = list[0]!.session_id;
          } else {
            const created = await api.createSession();
            if (cancelled) return;
            setSessions([created]);
            target = created.session_id;
          }
          navigate(routePath({ name: 'chat', sessionId: target }), { replace: true });
          return;
        }
        currentIdRef.current = target;
        setCurrentId(target);
        setBootstrapping(false);
        await loadMessages(target);
      } catch (error) {
        if (cancelled) return;
        const explained = explainError(error);
        if (explained.code !== 'unauthorized') setLoadError(explained);
        setBootstrapping(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [routeSessionId, loadMessages, commitLive]);

  useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );

  /* ------------------------------------------------------------ 服务端仍在生成时轮询 */
  const activeTurnId = activeTurn?.turn_id ?? null;
  const liveTurnId = live?.turnId ?? null;
  useEffect(() => {
    if (!activeTurnId || !currentId) return;
    if (liveTurnId === activeTurnId) return;
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      try {
        const data = await api.messages(currentId);
        if (cancelled) return;
        setMessages(mapServerMessages(data.messages));
        setActiveTurn(data.active_turn);
      } catch {
        /* 网络抖动时保留当前视图，下个周期再试 */
      }
    };
    const timer = window.setInterval(() => {
      void tick();
    }, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeTurnId, liveTurnId, currentId]);

  /* ------------------------------------------------------------ 流处理 */
  const pump = useCallback(
    async (options: PumpOptions) => {
      const { kind, sessionId, actorKey, userKey } = options;
      const controller = new AbortController();
      abortRef.current = controller;
      let turnId = options.turnId;
      let accumulated = '';
      let terminal = false;

      commitLive({
        kind,
        sessionId,
        turnId,
        actorKey,
        state: 'connecting',
        stopRequested: false,
        startedAt: Date.now(),
      });

      const owned = () => currentIdRef.current === sessionId;
      const patchLive = (patch: Partial<LiveTurn>) => {
        const current = liveRef.current;
        if (!current || current.sessionId !== sessionId || current.actorKey !== actorKey) return;
        commitLive({ ...current, ...patch });
      };
      const updateActor = (patch: Partial<UiMessage>) => {
        if (!owned()) return;
        setMessages((prev) =>
          prev.map((message) => (message.key === actorKey ? { ...message, ...patch } : message)),
        );
      };
      const updateUser = (patch: Partial<UiMessage>) => {
        if (!owned() || !userKey) return;
        setMessages((prev) =>
          prev.map((message) => (message.key === userKey ? { ...message, ...patch } : message)),
        );
      };
      const ensureActor = () => {
        if (!owned()) return;
        setMessages((prev) =>
          prev.some((message) => message.key === actorKey)
            ? prev
            : [...prev, emptyActor(actorKey, turnId)],
        );
      };
      const removeActor = () => {
        if (!owned()) return;
        setMessages((prev) => prev.filter((message) => message.key !== actorKey));
      };

      const applyFinal = (eventName: string, data: TurnFinalData) => {
        terminal = true;
        const serverMessage = (data.message ?? null) as ChatMessage | null;
        const fallbackState: TurnState =
          eventName === 'turn.completed'
            ? 'completed'
            : eventName === 'turn.stopped'
              ? 'stopped'
              : 'failed';
        const finalState = (data.state ?? fallbackState) as TurnState;

        if (eventName === 'turn.failed') {
          const explained = explainError(
            data.error ?? { code: 'internal', message: '生成失败', retriable: true },
          );
          updateActor({
            messageId: serverMessage?.message_id ?? null,
            text: typeof data.text === 'string' ? data.text : accumulated,
            state: 'failed',
            modelLabel: (data.model_label ?? serverMessage?.model_label ?? null) as string | null,
            errorCode: explained.code,
            errorMessage: explained.reason,
            errorDetail: [explained.hint, explained.detail].filter(Boolean).join(' · ') || null,
            errorTraceId: explained.traceId,
            retriable: explained.retriable || finalState === 'failed',
            local: false,
          });
        } else if (serverMessage) {
          updateActor({
            messageId: serverMessage.message_id,
            turnId: serverMessage.turn_id,
            text: serverMessage.text,
            state: serverMessage.state,
            createdAt: serverMessage.created_at,
            modelLabel: serverMessage.model_label,
            errorCode: serverMessage.error_code,
            errorMessage: null,
            errorDetail: null,
            errorTraceId: null,
            retriable: serverMessage.state === 'stopped',
            local: false,
          });
        } else {
          updateActor({
            text: accumulated,
            state: finalState,
            errorMessage: null,
            errorDetail: null,
            errorTraceId: null,
            retriable: finalState === 'stopped',
            local: false,
          });
        }
        setActiveTurn(null);
        if (userKey) updateUser({ state: 'completed', local: false });
      };

      const handleEvent = (event: SseEvent) => {
        if (!owned()) return;
        switch (event.event) {
          case 'turn.started': {
            const data = event.data as unknown as TurnStartedData;
            if (typeof data.turn_id === 'string') turnId = data.turn_id;
            if (typeof data.test_mode === 'boolean') onTestMode(data.test_mode);
            if (data.user_message) {
              updateUser({
                messageId: data.user_message.message_id,
                turnId: data.user_message.turn_id,
                state: data.user_message.state,
                createdAt: data.user_message.created_at,
                local: false,
                errorMessage: null,
                errorDetail: null,
                errorCode: null,
              });
            } else if (userKey) {
              updateUser({ state: 'completed', local: false });
            }
            ensureActor();
            updateActor({ turnId, state: 'streaming' });
            patchLive({ turnId, state: 'streaming' });
            break;
          }
          case 'delta': {
            if (liveRef.current?.stopRequested) break;
            const data = event.data as unknown as DeltaData;
            const chunk = typeof data.text === 'string' ? data.text : '';
            if (!chunk) break;
            accumulated += chunk;
            ensureActor();
            updateActor({ text: accumulated, state: 'streaming' });
            break;
          }
          case 'turn.completed':
          case 'turn.stopped':
          case 'turn.failed': {
            const data = event.data as unknown as TurnFinalData;
            if (typeof data.turn_id === 'string') turnId = data.turn_id;
            applyFinal(event.event, data);
            break;
          }
          case 'ping':
          case 'heartbeat':
            break;
          default:
            break;
        }
      };

      try {
        const response =
          kind === 'send'
            ? await api.sendTurn(sessionId, options.clientTurnId ?? '', options.text ?? '', controller.signal)
            : await api.retryTurn(turnId ?? '', controller.signal);
        await consumeSse(response, handleEvent, {
          signal: controller.signal,
          idleMs: STREAM_IDLE_MS,
        });
        if (!terminal && owned()) {
          // 流被网关提前关闭/幂等重放未给终态：以服务端消息为准
          await loadMessages(sessionId);
        }
      } catch (error) {
        if (error instanceof StreamAbortError) return;
        if (!owned()) return;
        const explained: ExplainedError =
          error instanceof StreamTimeoutError
            ? {
                reason: '实时连接超时',
                hint: `${error.message}；可以停止本轮或重试。`,
                retriable: true,
                code: 'timeout',
                detail: null,
                traceId: null,
              }
            : explainError(error);
        if (!turnId) {
          removeActor();
          updateUser({
            state: 'failed',
            errorCode: explained.code,
            errorMessage: explained.reason,
            errorDetail: explained.hint || null,
            errorTraceId: explained.traceId,
            retriable: explained.retriable,
          });
          setNotice({
            kind: 'error',
            text: `发送失败：${explained.reason}`,
            detail: explained.hint || undefined,
          });
        } else {
          ensureActor();
          updateActor({
            state: 'failed',
            text: accumulated,
            errorCode: explained.code,
            errorMessage: explained.reason,
            errorDetail: [explained.hint, explained.detail].filter(Boolean).join(' · ') || null,
            errorTraceId: explained.traceId,
            retriable: explained.retriable,
          });
          if (error instanceof StreamTimeoutError) {
            setActiveTurn({
              turn_id: turnId,
              state: 'streaming',
              attempt: 1,
              model_label: null,
              error_code: null,
              retriable: false,
            });
          } else if (error instanceof ApiError) {
            setNotice({ kind: 'error', text: explained.reason, detail: explained.hint || undefined });
            await loadMessages(sessionId);
          }
        }
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
        const current = liveRef.current;
        if (current && current.actorKey === actorKey) commitLive(null);
        void api
          .listSessions()
          .then((list) => setSessions(list))
          .catch(() => undefined);
      }
    },
    [commitLive, loadMessages, onTestMode],
  );

  /* ------------------------------------------------------------ 用户动作 */
  const send = useCallback(
    (rawText: string) => {
      const text = rawText.trim();
      const sessionId = currentIdRef.current;
      if (!text || !sessionId || liveRef.current) return;
      const clientTurnId = newClientTurnId();
      const userKey = `local-user-${clientTurnId}`;
      const actorKey = `local-actor-${clientTurnId}`;
      setMessages((prev) => [
        ...prev,
        {
          key: userKey,
          messageId: null,
          turnId: null,
          clientTurnId,
          role: 'user',
          system: false,
          text,
          state: 'pending',
          createdAt: new Date().toISOString(),
          modelLabel: null,
          errorCode: null,
          errorMessage: null,
          errorDetail: null,
          errorTraceId: null,
          retriable: false,
          local: true,
          attempt: 1,
          silenceReason: null,
        },
      ]);
      void pump({
        kind: 'send',
        sessionId,
        clientTurnId,
        text,
        turnId: null,
        actorKey,
        userKey,
      });
    },
    [pump],
  );

  const resend = useCallback(
    (message: UiMessage) => {
      const sessionId = currentIdRef.current;
      if (!sessionId || !message.clientTurnId || liveRef.current) return;
      setMessages((prev) =>
        prev.map((item) =>
          item.key === message.key
            ? {
                ...item,
                state: 'pending',
                errorCode: null,
                errorMessage: null,
                errorDetail: null,
                errorTraceId: null,
                retriable: false,
              }
            : item,
        ),
      );
      void pump({
        kind: 'send',
        sessionId,
        clientTurnId: message.clientTurnId,
        text: message.text,
        turnId: null,
        actorKey: `local-actor-${message.clientTurnId}`,
        userKey: message.key,
      });
    },
    [pump],
  );

  const retry = useCallback(
    (turnId: string) => {
      const sessionId = currentIdRef.current;
      if (!sessionId || !turnId || liveRef.current) return;
      const existing = messagesRef.current.find(
        (message) => message.role === 'actor' && message.turnId === turnId,
      );
      const actorKey = existing?.key ?? `retry-actor-${turnId}`;
      setMessages((prev) => {
        const next = prev.map((message) =>
          message.key === actorKey
            ? {
                ...message,
                text: '',
                state: 'pending' as TurnState,
                errorCode: null,
                errorMessage: null,
                errorDetail: null,
                errorTraceId: null,
                retriable: false,
                attempt: message.attempt + 1,
                local: true,
                modelLabel: null,
                silenceReason: null,
              }
            : message,
        );
        return next.some((message) => message.key === actorKey)
          ? next
          : [...next, emptyActor(actorKey, turnId)];
      });
      setActiveTurn(null);
      void pump({
        kind: 'retry',
        sessionId,
        clientTurnId: null,
        text: null,
        turnId,
        actorKey,
        userKey: null,
      });
    },
    [pump],
  );

  const stop = useCallback(async () => {
    const current = liveRef.current;
    const turnId = current?.turnId ?? activeTurnRef.current?.turn_id ?? null;
    if (!turnId) return;
    if (current) {
      commitLive({ ...current, state: 'stopping', stopRequested: true });
      const stoppingKey = current.actorKey;
      setMessages((prev) =>
        prev.map((message) =>
          message.key === stoppingKey && (message.state === 'streaming' || message.state === 'pending')
            ? { ...message, state: 'stopping' }
            : message,
        ),
      );
      // 兜底：服务端若没有在 5 秒内给出终态，本地按停止收敛并以消息接口校准
      window.setTimeout(() => {
        const still = liveRef.current;
        if (still && still.actorKey === stoppingKey) {
          setMessages((prev) =>
            prev.map((message) =>
              message.key === stoppingKey
                ? { ...message, state: 'stopped', retriable: true, errorMessage: null }
                : message,
            ),
          );
          commitLive(null);
          abortRef.current?.abort();
          abortRef.current = null;
          const sessionId = currentIdRef.current;
          if (sessionId) void loadMessages(sessionId);
        }
      }, STOP_FALLBACK_MS);
    }
    try {
      const result = await api.stopTurn(turnId);
      if (result.state !== 'stopping') {
        setActiveTurn((prev) => (prev && prev.turn_id === turnId ? null : prev));
        if (!liveRef.current) {
          window.setTimeout(() => {
            const sessionId = currentIdRef.current;
            if (sessionId) void loadMessages(sessionId);
          }, 250);
        }
      } else {
        setActiveTurn((prev) =>
          prev && prev.turn_id === turnId ? { ...prev, state: 'stopping' } : prev,
        );
      }
    } catch (error) {
      const explained = explainError(error);
      setNotice({ kind: 'error', text: `停止失败：${explained.reason}`, detail: explained.hint || undefined });
    }
  }, [commitLive, loadMessages]);

  const createSession = useCallback(() => {
    if (liveRef.current) return;
    void api
      .createSession()
      .then((created) => {
        setSessions((prev) => [created, ...prev]);
        navigate(routePath({ name: 'chat', sessionId: created.session_id }));
      })
      .catch((error: unknown) => {
        const explained = explainError(error);
        setNotice({ kind: 'error', text: `新建会话失败：${explained.reason}` });
      });
  }, []);

  const renameSession = useCallback(async (sessionId: string, title: string) => {
    const trimmed = title.trim();
    if (!trimmed) return;
    try {
      const updated = await api.renameSession(sessionId, trimmed);
      setSessions((prev) =>
        prev.map((session) => (session.session_id === sessionId ? updated : session)),
      );
    } catch (error) {
      const explained = explainError(error);
      setNotice({ kind: 'error', text: `改名失败：${explained.reason}` });
    }
  }, []);

  const selectSession = useCallback((sessionId: string) => {
    if (sessionId === currentIdRef.current) return;
    navigate(routePath({ name: 'chat', sessionId }));
  }, []);

  const dismissNotice = useCallback(() => setNotice(null), []);

  return {
    sessions,
    currentId,
    messages,
    activeTurn,
    live,
    notice,
    bootstrapping,
    loadingMessages,
    loadError,
    send,
    resend,
    retry,
    stop,
    reloadMessages,
    reloadSessions,
    createSession,
    renameSession,
    selectSession,
    dismissNotice,
  };
}
