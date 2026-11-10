/**
 * 聊天状态机：会话、消息、SSE 流、停止、失败重试。
 *
 * 原则：
 * - 浏览器不存权威历史；刷新/切换会话一律从 `/v1/sessions/{id}/messages` 重建。
 * - 增量文本只做展示；终态以服务端 `turn.completed/stopped/failed` 或消息接口为准。
 * - 停止后不再追加文本（即使还有迟到的 delta）。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError, serverNowMsFromResponse } from './api';
import { explainError, type ExplainedError } from './errors';
import { newClientTurnId } from './format';
import { navigate, routePath } from './route';
import { consumeSse, StreamAbortError, StreamTimeoutError } from './sse';
import {
  mergeRecallProposals,
  proposalFromServer,
  proposalsFromFinal,
  recallExpired,
  type RecallProposalView,
} from './recall';
import type {
  ChatMessage,
  DeltaData,
  SessionRecord,
  SseEvent,
  TurnDeferredData,
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
  /** 发言人显示名（群名片优先、其次昵称）。她自己的消息与网页自己发的话都是 null。 */
  senderName: string | null;
  /** 发言人的平台号码（QQ 号）。界面上和名字分两行，不挤成 `@号码 名字`。 */
  senderId: string | null;
  /**
   * 这条消息已被撤回的时刻（`null` = 没撤）。
   *
   * 两个来源合并成一个字段：她自己收回了（`recalled_at`）优先，其次是对方
   * 撤回（`retracted_at`）。界面只需要"这条已经不在平台上了"这一个事实；
   * 谁撤的在措辞上区分（她撤的才可能出现在「她想撤回」的上下文里）。
   */
  recalledAt: string | null;
  /** 是她自己撤回的（区别于"对方撤回"）。 */
  recalledByActor: boolean;
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

/**
 * 服务端把"没有"给成 null / undefined / 空串都算没有。
 * 空串会渲染出一行空白署名，比缺字段更难看，所以统一折成 null。
 */
function optionalText(value: string | null | undefined): string | null {
  const text = (value ?? '').trim();
  return text || null;
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
    senderName: optionalText(message.sender_name),
    senderId: optionalText(message.sender_id),
    // 她自己撤回优先：`recalled_at` 非空说明这条是她的发言被收回，
    // 此时 `retracted_at` 不该覆盖它（那是"对方撤回"，语义不同）。
    recalledAt: optionalText(message.recalled_at) ?? optionalText(message.retracted_at),
    recalledByActor: Boolean(optionalText(message.recalled_at)),
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
    senderName: null,
    senderId: null,
    recalledAt: null,
    recalledByActor: false,
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
  /**
   * 置顶 / 取消置顶。
   *
   * 成功后**替换整条记录**（后端返回的 `SessionRecord` 带新的 `pinned`），
   * 顺序由 `SessionSidebar` 按 `pinned` 分组——两处共用同一个字段，
   * 不在前端另存一份"置顶 id 集合"（两份状态必然有一天不一致）。
   */
  pinSession: (sessionId: string, pinned: boolean) => Promise<void>;
  selectSession: (sessionId: string) => void;
  dismissNotice: () => void;
  /** 她提议撤回、还没处理完的（挂在对应消息下面）。 */
  recallProposals: RecallProposalView[];
  /** 撤回提议列表读不到（只影响卡片区，聊天照常）。 */
  recallProposalsUnavailable: boolean;
  /** 手动重拉撤回提议列表。 */
  reloadRecallProposals: () => void;
  /** 点「撤回」：真的执行，成功/失败都收敛到卡片状态上。 */
  confirmRecall: (proposal: RecallProposalView) => void;
  /** 点「算了」：只把提议标成 declined。 */
  declineRecall: (proposal: RecallProposalView) => void;
}

/**
 * 把 `turn.deferred` 载荷变成一句给用户看的话。
 *
 * **钳过必须说出来**：她要求 999 秒、代码钳到 300 秒时，只说"等 5 分钟"
 * 等于把系统的上限伪装成她的选择。这是本项目反复强调的一条底线（与"不许把
 * 槽位忙包装成人格动作"同源），所以这里的文案分两种。
 *
 * 导出它是为了让单测直接断言文案，而不是去点 DOM 猜。
 */
export function describeDeferral(info: TurnDeferredData): string {
  const applied = Math.round(Number(info.applied_seconds ?? 0));
  const requested = Math.round(Number(info.requested_seconds ?? applied));
  const human = formatDuration(applied);
  if (info.clamped && requested > applied) {
    return `她说等会儿再回（本来想等 ${formatDuration(requested)}，最长只能等 ${human}）`;
  }
  return `她说等会儿再回（大约 ${human} 后）`;
}

/** 秒 → 人话时长。60 秒以上说"X 分钟"，否则说"X 秒"。 */
function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '一会儿';
  if (seconds >= 60) {
    const minutes = seconds / 60;
    const rounded = Math.round(minutes * 10) / 10;
    return `${Number.isInteger(rounded) ? rounded : rounded.toFixed(1)} 分钟`;
  }
  return `${Math.round(seconds)} 秒`;
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
  const [recallProposals, setRecallProposals] = useState<RecallProposalView[]>([]);
  const [recallProposalsUnavailable, setRecallProposalsUnavailable] = useState(false);

  const currentIdRef = useRef<string | null>(null);
  const liveRef = useRef<LiveTurn | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const messagesRef = useRef<UiMessage[]>([]);
  const activeTurnRef = useRef<TurnView | null>(null);
  const recallProposalsRef = useRef<RecallProposalView[]>([]);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  useEffect(() => {
    activeTurnRef.current = activeTurn;
  }, [activeTurn]);

  useEffect(() => {
    recallProposalsRef.current = recallProposals;
  }, [recallProposals]);

  /**
   * 合并一批撤回提议（**按 `proposal_id` 去重**）。
   *
   * 为什么要走 ref 再写 state：同一轮 final 可能重复到达（幂等重放、断线
   * 重连、SSE 重放），而 `setState` 的 updater 里读 `prev` 是安全的——但
   * `applyFinal` 是普通函数、同一 tick 里可能连调两次，用 ref 保证第二次
   * 看到的是第一次的结果。终态（已撤回/已放弃/过期）不会被翻回待确认。
   */
  const addRecallProposals = useCallback((incoming: RecallProposalView[]) => {
    if (incoming.length === 0) return;
    setRecallProposals((prev) => {
      const next = mergeRecallProposals(prev, incoming);
      recallProposalsRef.current = next;
      return next;
    });
  }, []);

  /** 把某条提议推进到新状态（不可变更新，保持顺序）。 */
  const patchRecallProposal = useCallback(
    (proposalId: string, patch: Partial<RecallProposalView>) => {
      setRecallProposals((prev) => {
        const next = prev.map((item) =>
          item.proposalId === proposalId ? { ...item, ...patch } : item,
        );
        recallProposalsRef.current = next;
        return next;
      });
    },
    [],
  );

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

  /**
   * 拉一次「她想撤回」的待确认列表。
   *
   * 为什么要有这条：流式事件只在一轮结束时给一次，关掉标签页就再也看不到了，
   * 而提议本身还挂在库里等人确认。所以**切换会话时与刷新消息时都要拉一次**。
   *
   * 失败**只影响卡片区**：`recallProposalsUnavailable` 单独一个标志，
   * 聊天主流程（收发消息）一点都不该被它拖住——撤回提议读不到和"她不能说话"
   * 是两件事。
   */
  const loadRecallProposals = useCallback(async (sessionId: string) => {
    if (!sessionId) return;
    try {
      const { items, serverNowMs } = await api.recallProposals(sessionId);
      if (currentIdRef.current !== sessionId) return;
      const nowMs = Date.now();
      addRecallProposals(items.map((item) => proposalFromServer(item, serverNowMs, nowMs)));
      setRecallProposalsUnavailable(false);
    } catch (error) {
      if (currentIdRef.current !== sessionId) return;
      const explained = explainError(error);
      if (explained.code === 'unauthorized') return;
      setRecallProposalsUnavailable(true);
    }
  }, [addRecallProposals]);

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
    // 撤回提议是**按会话**的：切会话必须先清空，否则上一个会话的卡片会挂到
    // 新会话的消息下面（挂错消息比不显示更糟——用户会以为她要撤的是新那条）。
    setRecallProposals([]);
    recallProposalsRef.current = [];
    setRecallProposalsUnavailable(false);
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
        await Promise.all([loadMessages(target), loadRecallProposals(target)]);
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
  }, [routeSessionId, loadMessages, loadRecallProposals, commitLive]);

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
      /**
       * 这条 SSE 响应的服务端时刻（HTTP `Date` 头）。
       *
       * 流里下发的 `recall_proposals` **没有 `created_at`**（那是落库字段），
       * 而"还能撤几秒"必须用服务端时钟算。拿这一轮响应的 `Date` 当头锚，
       * 误差只有请求往返的一瞬；拿不到时退回本机时钟并在卡片上注明。
       */
      let streamServerNowMs: number | null = null;

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

      /*
       * 这一轮收到的 `turn.deferred` 载荷（如果有）。
       *
       * 为什么放在 `applyFinal` 外面：`turn.deferred` 与 `turn.completed` 是
       * **两个**事件，而"她延后了"这个事实要影响终态的处理（不画空气泡、
       * 把说明挂到用户消息上）。用一个闭包变量把两件事连起来，比在
       * `TurnFinalData` 上加一个"这一轮延后过"的布尔更诚实——后者会让
       * `turn.completed` 看起来自带延后语义，而它其实没有。
       */
      let deferredInfo: TurnDeferredData | null = null;

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
        } else if (deferredInfo && !serverMessage && !accumulated) {
          /*
           * 她**选择**过一会儿再回：这一轮确实没有正文，但**不是**空回复。
           *
           * 处置与 `silence` 相同（不留空气泡、把说明挂到用户那条消息上），
           * 区别只在文案：延后是"她说了会回"，沉默是"她这次不打算说"。
           * 混成同一句会让用户以为她不想理人。
           *
           * 钳过时必须让用户看得出来（`clamped` + `requested_seconds`）：
           * 只显示实际等 5 分钟，等于把系统的上限说成她的选择——用户会以为
           * 她只想等 5 分钟，而她想等 16 分钟。
           */
          removeActor();
          if (userKey) {
            updateUser({
              state: 'completed',
              local: false,
              silenceReason: describeDeferral(deferredInfo),
            });
          }
          setActiveTurn(null);
          return;
        } else if (data.decision === 'silence' && !serverMessage && !accumulated) {
          // 她选择不说话。**不能**留一个空气泡——那会渲染成「（无内容）」，
          // 看起来像生成失败，和"她这次不想说"完全是两回事。
          // 理由挂到用户那条消息上，界面上显示「她没回：<原因>」。
          removeActor();
          if (userKey) {
            updateUser({
              state: 'completed',
              local: false,
              silenceReason: data.decision_reason || '她这次没有开口',
            });
          }
          setActiveTurn(null);
          return;
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
        /*
         * 她这一轮提议撤回的待确认项：**立刻挂卡片**，然后补一次列表。
         *
         * 流里只给 `{proposal_id, message_id, note}`，没有 `created_at`，
         * 也不含状态（别处可能已经处理过）。所以这里的职责只有两条：
         * 1. 让卡片**当场出现**（不必等一次额外请求——等一次就等于让用户
         *    在一两秒里看不到她刚提的撤回）；
         * 2. 紧接着拉一次列表，用权威的 `created_at` 覆盖掉用回复时刻顶上的
         *    那个代理值，顺便把别处已处理的状态收进来。
         */
        if (owned()) {
          const nowMs = Date.now();
          // 代理时刻：她这条回复的落库时间（提议正是在这一轮写下来的）。
          const fallbackCreatedAt = serverMessage?.created_at ?? new Date(nowMs).toISOString();
          const incoming = proposalsFromFinal(
            data.recall_proposals,
            fallbackCreatedAt,
            streamServerNowMs,
            nowMs,
          );
          if (incoming.length > 0) {
            addRecallProposals(incoming);
            void loadRecallProposals(sessionId);
          }
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
                senderName: optionalText(data.user_message.sender_name),
                senderId: optionalText(data.user_message.sender_id),
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
          case 'turn.deferred': {
            /*
             * 她**选择**过一会儿再回。服务端在原轮 `_finalize` 之前发这个事件，
             * 所以它一定先于 `turn.completed`（有服务端用例钉这个顺序）。
             *
             * 这里的职责只有两条：
             * 1. 把"她在等"挂到用户那条消息上（和"她没回"同一个位置，
             *    因为对用户来说都是"暂时没有回复"）；
             * 2. **不画空气泡**——延后不是"她回了一条空的"。
             */
            const data = event.data as unknown as TurnDeferredData;
            if (typeof data.turn_id === 'string') turnId = data.turn_id;
            deferredInfo = data;
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
        streamServerNowMs = serverNowMsFromResponse(response);
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
    [addRecallProposals, commitLive, loadMessages, loadRecallProposals, onTestMode],
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
          senderName: null,
          senderId: null,
          recalledAt: null,
          recalledByActor: false,
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

  const pinSession = useCallback(async (sessionId: string, pinned: boolean) => {
    try {
      const updated = await api.pinSession(sessionId, pinned);
      setSessions((prev) =>
        prev.map((session) => (session.session_id === sessionId ? updated : session)),
      );
    } catch (error) {
      const explained = explainError(error);
      // 失败要说清"没置顶成功"，不能静默——用户会以为点上了，
      // 下次刷新发现顺序没变，只会以为界面在乱跳。
      setNotice({ kind: 'error', text: `${pinned ? '置顶' : '取消置顶'}失败：${explained.reason}` });
    }
  }, []);

  const selectSession = useCallback((sessionId: string) => {
    if (sessionId === currentIdRef.current) return;
    navigate(routePath({ name: 'chat', sessionId }));
  }, []);

  const reloadRecallProposals = useCallback(() => {
    const sessionId = currentIdRef.current;
    if (sessionId) void loadRecallProposals(sessionId);
  }, [loadRecallProposals]);

  /**
   * 点「撤回」：先看时间窗，再真的执行。
   *
   * 为什么**前端也要判一次**（后端本来就会判）：让用户点一个注定失败的按钮，
   * 比一开始就写"来不及"更糟。这里的判据与卡片显示用的是同一个纯函数
   * （`recallExpired`），不会出现"按钮能点但点了必失败"的错位。
   *
   * 请求在飞时状态是 `pending`：按钮禁用，防止双击变成两次不可逆请求。
   */
  const confirmRecall = useCallback(
    (proposal: RecallProposalView) => {
      const sessionId = currentIdRef.current;
      if (!sessionId) return;
      if (proposal.status === 'pending') return;
      // 窗口外直接收敛到 expired，**不发请求**。
      if (recallExpired(proposal, Date.now())) {
        patchRecallProposal(proposal.proposalId, { status: 'expired' });
        return;
      }
      patchRecallProposal(proposal.proposalId, { status: 'pending', failureNote: null });
      void api
        .confirmRecall(sessionId, proposal.proposalId)
        .then((result) => {
          if (currentIdRef.current !== sessionId) return;
          if (result.state === 'recalled') {
            patchRecallProposal(proposal.proposalId, { status: 'confirmed', failureNote: null });
            /*
             * 被撤那条在气泡上要能看出来。
             *
             * **不改 `state`**：撤回不是"生成失败/被停止"，把它改成 stopped
             * 会让界面显示「已停止，未生成内容」——而那条内容是真实生成过的，
             * 只是现在平台上看不到了。单独一个 `recalledAt` 才不会篡改事实。
             */
            const targetId = result.message_id || proposal.messageId;
            const recalledAt = result.recalled_at ?? new Date().toISOString();
            setMessages((prev) =>
              prev.map((message) =>
                message.messageId === targetId
                  ? { ...message, recalledAt, recalledByActor: true }
                  : message,
              ),
            );
            return;
          }
          // 其余四种状态各说各的：expired 是"来不及"，unsupported 是"这个渠道
          // 不支持"，uncertain 是"没拿到回执，不敢说成功"。
          if (result.state === 'expired') {
            patchRecallProposal(proposal.proposalId, {
              status: 'expired',
              failureNote: result.note || null,
            });
            return;
          }
          patchRecallProposal(proposal.proposalId, {
            status: 'failed',
            failureNote: result.note || null,
          });
        })
        .catch((error: unknown) => {
          if (currentIdRef.current !== sessionId) return;
          const explained = explainError(error);
          if (explained.code === 'not_found') {
            // 提议已经不在了（别处处理过 / 被清理）：卡片留着但不可点。
            patchRecallProposal(proposal.proposalId, { status: 'gone' });
            return;
          }
          if (explained.code === 'conflict') {
            // 409 = 已经处理过（并发双击、另一个标签页先点了）。
            patchRecallProposal(proposal.proposalId, {
              status: 'gone',
              failureNote: '这条已经处理过了',
            });
            return;
          }
          patchRecallProposal(proposal.proposalId, {
            status: 'failed',
            failureNote: explained.reason,
          });
        });
    },
    [patchRecallProposal],
  );

  const declineRecall = useCallback(
    (proposal: RecallProposalView) => {
      const sessionId = currentIdRef.current;
      if (!sessionId) return;
      if (proposal.status === 'pending') return;
      patchRecallProposal(proposal.proposalId, { status: 'pending', failureNote: null });
      void api
        .declineRecall(sessionId, proposal.proposalId)
        .then(() => {
          if (currentIdRef.current !== sessionId) return;
          patchRecallProposal(proposal.proposalId, { status: 'declined', failureNote: null });
        })
        .catch((error: unknown) => {
          if (currentIdRef.current !== sessionId) return;
          const explained = explainError(error);
          // 拒绝失败时退回"待确认"：不能因为一次网络抖动就把她的提议吞掉。
          patchRecallProposal(proposal.proposalId, {
            status: explained.code === 'not_found' ? 'gone' : 'proposed',
            failureNote: explained.code === 'not_found' ? '这条已经处理过了' : null,
          });
        });
    },
    [patchRecallProposal],
  );

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
    pinSession,
    selectSession,
    dismissNotice,
    recallProposals,
    recallProposalsUnavailable,
    reloadRecallProposals,
    confirmRecall,
    declineRecall,
  };
}
