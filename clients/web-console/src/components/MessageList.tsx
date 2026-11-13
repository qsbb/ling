import { useState } from 'react';
import { formatClock, formatModelLabel } from '../format';
import { retryLabel } from '../errors';
import { usePersonaName } from '../personaName';
import type { RecallProposalView } from '../recall';
import type { LiveTurn, UiMessage } from '../useChat';
import type { TurnView } from '../types';
import { AlertIcon, CheckIcon, RefreshIcon, StopIcon } from './Icons';
import { PROMPT_DRAWER_ID, PROMPT_ENTRY_LABEL } from './PromptDrawer';
import { RecallCard, RecallUnavailableCard } from './RecallCard';
import { VoicePlayer, type VoiceMessageView } from './VoicePlayer';
import { fetchOperatorSpeechText } from '../voiceMessages';

export function OperatorSpeechText({ messageId }: { messageId: string }) {
  const [text, setText] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  return (
    <div className="voice-message__operator">
      <button
        type="button"
        className="voice-message__transcript"
        disabled={loading}
        aria-expanded={text !== null}
        onClick={async () => {
          if (text !== null) {
            setText(null);
            return;
          }
          setLoading(true);
          setFailed(false);
          const transcript = await fetchOperatorSpeechText(messageId);
          setText(transcript);
          setFailed(transcript === null);
          setLoading(false);
        }}
      >
        {text !== null ? '隐藏正文' : loading ? '读取中…' : '后台查看正文'}
      </button>
      {text !== null ? (
        <p className="voice-message__text" data-testid="voice-operator-text">{text}</p>
      ) : failed ? (
        <p className="voice-message__error" role="alert">正文读取失败。</p>
      ) : null}
    </div>
  );
}

/**
 * 会话类型决定「谁在说话」怎么读。
 *
 * - `web`：浏览器前面这位问她，`role=user` 是"我发的"，靠右、不署名；
 * - `group`：同一个群窗口里所有人都在，`role=user` 是群里某个人，必须署名；
 * - `private`：对面那位，署名可以省（标题栏已经写着是谁），但没有名字时
 *   不能假装知道。
 */
export type DialogueVariant = 'web' | 'group' | 'private';

export interface SpeakerLabel {
  name: string;
  /** 号码单独一行小字显示；没有就不渲染这一行，绝不拼成 `@号码 名字`。 */
  id: string | null;
}

export interface DialogueRow {
  message: UiMessage;
  /**
   * **她自己说的话**（`role=actor`）：靠右、用她那一侧的泡泡。
   *
   * 这里是"谁在说话"，不是"谁发起的这次请求"——原来按
   * `variant === 'web' && role === 'user'` 算，于是渠道会话里她和群成员
   * 全挤在左边，群里根本看不出哪句是她的。
   */
  own: boolean;
  /** 别人说的话（`role=user`）：靠左；渠道会话里还要署名。 */
  inbound: boolean;
  speaker: SpeakerLabel | null;
  /** 同一个人连续多条只署一次；换人必须重新署名。 */
  showSpeaker: boolean;
  /** 同一段连续相同原因只在最后一条给一句说明。 */
  silence: { text: string; run: number } | null;
}

interface MessageListProps {
  messages: UiMessage[];
  activeTurn: TurnView | null;
  live: LiveTurn | null;
  loading: boolean;
  onRetry: (turnId: string) => void;
  onResend: (message: UiMessage) => void;
  onStop: () => void;
  onOpenSettings: () => void;
  variant?: DialogueVariant;
  /**
   * 「她这次看到了什么」入口是否可见：只有 owner 能看（后端对其他人 404）。
   * 默认关，既有页面与测试不受影响。
   */
  promptEnabled?: boolean;
  canViewVoiceTranscript?: boolean;
  /** 当前抽屉打开的那条消息（按 `key` 比，再点同一条 = 收起）。 */
  promptOpenKey?: string | null;
  /** 点击入口。第二个参数是入口元素本身，关闭抽屉后要把焦点还给它。 */
  onOpenPrompt?: (message: UiMessage, trigger: HTMLElement) => void;
  /**
   * 她提议撤回、还没处理完的（按 `message_id` 挂到对应消息下面）。
   *
   * 默认空数组：其它调用方（测试、别的页面）不受影响。
   */
  recallProposals?: RecallProposalView[];
  /** 撤回提议列表读不到：只在卡片区降级，聊天照常。 */
  recallUnavailable?: boolean;
  onReloadRecall?: () => void;
  onConfirmRecall?: (proposal: RecallProposalView) => void;
  onDeclineRecall?: (proposal: RecallProposalView) => void;
  /** 注入时钟（单测用固定时间；运行时是 `Date.now`）。 */
  recallClock?: () => number;
  /**
   * 语音消息（SP03）：`message_id → 可播放的语音`。
   *
   * 默认空 Map：没有语音的会话、以及既有测试，行为完全不变。语音信息来自
   * 独立端点 `GET /v1/sessions/{id}/voice-messages`——**不塞进消息契约**，
   * 那样 `types.ts`（SP00 独占）就必须为"有没有音频"改一次。
   */
  voices?: Map<string, VoiceMessageView>;
  /** 上报一次播放（后端按 `(message_id, subject)` 累加）。 */
  onVoicePlayed?: (messageId: string, info: { positionMs?: number; completed?: boolean }) => void;
  /** 签名 URL 过期后重取地址。不传则过期时直接报错。 */
  onRefreshVoiceUrl?: (messageId: string) => Promise<string | null>;
}

/**
 * 被撤那条的摘要：卡片上要让人确认"撤的是哪一句"。
 *
 * 取不到正文时返回 `null`（卡片会换成"不在这一屏里"的说明）——**绝不**用
 * "（无内容）"之类的占位符冒充正文，那会让人以为要撤的是一条空消息。
 */
export function recallExcerpt(messages: UiMessage[], messageId: string): string | null {
  const target = messages.find((message) => message.messageId === messageId);
  const text = (target?.text ?? '').trim();
  if (!text) return null;
  // 摘要只取首段前 80 字：卡片是"确认一下"的提示，不是全文阅读器。
  const firstLine = text.split('\n')[0] ?? text;
  const compact = firstLine.replace(/\s+/g, ' ');
  return compact.length > 80 ? `${compact.slice(0, 80)}…` : compact;
}

const SETTINGS_CODES = new Set([
  'secret_missing',
  'config_invalid',
  'provider_unavailable',
  'provider_timeout',
  'provider_error',
]);

function cleanText(value: string | null | undefined): string | null {
  const text = (value ?? '').trim();
  return text || null;
}

/**
 * 一条消息该挂谁的署名。
 *
 * 她自己的话（`role=actor`）不署名：她不是"群里某个人"，气泡和头像已经把她
 * 和成员分开了；每句都挂一遍名字，反而把群成员的名字淹掉。
 * 网页会话里 `role=user` 是浏览器前面这位，也不需要署名。
 */
export function speakerLabel(
  message: UiMessage,
  variant: DialogueVariant,
): SpeakerLabel | null {
  if (message.role !== 'user' || variant === 'web') return null;
  const name = cleanText(message.senderName);
  const id = cleanText(message.senderId);
  if (name) return { name, id };
  // 没有群名片就只报号码，绝不编名字；同一个号码也不再重复第二行。
  if (id) return { name: `成员 ${id}`, id: null };
  return null;
}

/** 同一条原因可能带多余空白/换行，比较前先折平，否则"同一段"会被拆开。 */
export function normalizeSilenceReason(value: string | null | undefined): string | null {
  const text = cleanText(value);
  return text ? text.replace(/\s+/g, ' ') : null;
}

/**
 * 把消息列表编组成渲染行。
 *
 * 发言人：连续同一人只署一次（读起来像一段对话），换人一定重新署名——
 * 宁可多署一次，也不能把两个不同的人读成同一个人。
 *
 * 沉默原因：同一段连续相同的理由只在段末说一次，并标出条数。每条都刷一遍
 * 「她没回：…」时，用户读到的是噪音，反而分不清她"没看见"还是"不想理"。
 */
export function buildDialogueRows(
  messages: UiMessage[],
  variant: DialogueVariant,
): DialogueRow[] {
  const rows: DialogueRow[] = [];
  let previousSpeaker: string | null = null;

  for (const message of messages) {
    if (message.system) {
      rows.push({
        message,
        own: false,
        inbound: false,
        speaker: null,
        showSpeaker: false,
        silence: null,
      });
      previousSpeaker = null;
      continue;
    }
    /*
     * 左右统一成「她 → 右，别人 → 左」，两种会话都一视同仁。
     *
     * 判据只看 `role`：`actor` 永远是她；`user` 在网页会话里是浏览器前面
     * 这位、在渠道会话里是群里/对面的某个人——**都不会是"她自己"**。
     * 所以不再需要 `variant`，但保留参数签名（调用方与测试都在用）。
     */
    const own = message.role === 'actor';
    const speaker = speakerLabel(message, variant);
    const speakerKey = speaker ? `${speaker.name}|${speaker.id ?? ''}` : null;
    const showSpeaker = speaker !== null && speakerKey !== previousSpeaker;
    rows.push({ message, own, inbound: speaker !== null, speaker, showSpeaker, silence: null });
    previousSpeaker = speakerKey;
  }

  let index = 0;
  while (index < rows.length) {
    const reason = normalizeSilenceReason(rows[index]?.message.silenceReason);
    if (!reason) {
      index += 1;
      continue;
    }
    let end = index + 1;
    while (
      end < rows.length &&
      normalizeSilenceReason(rows[end]?.message.silenceReason) === reason
    ) {
      end += 1;
    }
    const last = rows[end - 1];
    if (last) last.silence = { text: reason, run: end - index };
    index = end;
  }

  return rows;
}

/** 稳定的小色号：同一个人永远同一个色，不同人尽量分开。 */
export function avatarTone(key: string | null | undefined): number {
  let sum = 0;
  for (const char of key ?? '') {
    sum = (sum + (char.codePointAt(0) ?? 0)) % 9973;
  }
  return sum % 4;
}

export function avatarInitial(name: string | null | undefined): string {
  const text = cleanText(name);
  if (!text) return '·';
  const first = Array.from(text)[0] ?? '·';
  return first.toUpperCase();
}

export function statusText(
  message: UiMessage,
  liveTurnId: string | null,
  variant: DialogueVariant = 'web',
): string {
  if (message.role === 'user') {
    switch (message.state) {
      case 'pending':
        return '发送中…';
      case 'failed':
        return '发送失败';
      case 'stopped':
        return '已停止';
      default:
        // 「已送达」是网页发出的消息才有的概念。群里的消息由 QQ 抵达，
        // 一条条标"已送达"只会把真正需要看的状态（失败/停止）淹掉。
        return variant === 'web' ? '已送达' : '';
    }
  }
  const live = liveTurnId !== null && message.turnId === liveTurnId;
  switch (message.state) {
    case 'pending':
      return live ? '正在连接…' : '生成中…';
    case 'streaming':
      return '正在回应…';
    case 'stopping':
      return '正在停止…';
    case 'completed':
      return '已完成';
    case 'stopped':
      return '已停止';
    case 'failed':
      return '失败';
    default:
      return '';
  }
}

function ErrorCard({ message, onRetry, onResend, onOpenSettings }: {
  message: UiMessage;
  onRetry: (turnId: string) => void;
  onResend: (message: UiMessage) => void;
  onOpenSettings: () => void;
}) {
  const isUser = message.role === 'user';
  const canRetry = !isUser && message.turnId !== null && message.retriable;
  const canResend = isUser && message.local && message.clientTurnId !== null;
  return (
    <div className="message-error" role="alert">
      <div className="message-error__head">
        <AlertIcon />
        <span>{message.errorMessage ?? (isUser ? '发送失败' : '生成失败')}</span>
      </div>
      {message.errorDetail ? <p className="message-error__hint">{message.errorDetail}</p> : null}
      <div className="message-error__actions">
        {canResend ? (
          <button type="button" className="button button--small" onClick={() => onResend(message)}>
            <RefreshIcon /> 重新发送
          </button>
        ) : null}
        {canRetry && message.turnId ? (
          <button
            type="button"
            className="button button--small button--primary"
            onClick={() => onRetry(message.turnId as string)}
          >
            <RefreshIcon /> {retryLabel(message.state)}
          </button>
        ) : null}
        {message.errorCode && SETTINGS_CODES.has(message.errorCode) ? (
          <button type="button" className="button button--small" onClick={onOpenSettings}>
            打开设置
          </button>
        ) : null}
      </div>
      {message.errorTraceId ? (
        <p className="message-error__trace">trace {message.errorTraceId}</p>
      ) : null}
    </div>
  );
}

function ActorMeta({
  message,
  liveTurnId,
  onRetry,
  variant,
  align = 'start',
}: {
  message: UiMessage;
  liveTurnId: string | null;
  onRetry: (turnId: string) => void;
  variant: DialogueVariant;
  /** 她的话在右：状态与时间也贴右。 */
  align?: 'start' | 'end';
}) {
  const stateText =
    message.role === 'actor' ? statusText(message, liveTurnId, variant) : '';
  return (
    <div
      className={`message__meta${align === 'end' ? ' message__meta--own' : ''}`}
    >
      {stateText ? (
        <span className={`state state--${message.state}`}>
          {message.state === 'completed' ? <CheckIcon size={12} /> : null}
          {message.state === 'streaming' || message.state === 'pending' ? (
            <span className="state__pulse" aria-hidden="true" />
          ) : null}
          {stateText}
        </span>
      ) : null}
      {message.modelLabel ? (
        <span className="message__model" title={message.modelLabel}>
          {formatModelLabel(message.modelLabel)}
        </span>
      ) : null}
      {message.attempt > 1 ? <span className="message__attempt">第 {message.attempt} 次尝试</span> : null}
      {message.state === 'stopped' && message.turnId ? (
        <button
          type="button"
          className="button button--small"
          onClick={() => onRetry(message.turnId as string)}
        >
          <RefreshIcon /> 继续这一轮
        </button>
      ) : null}
      <span className="message__time">{formatClock(message.createdAt)}</span>
    </div>
  );
}

/**
 * 她没回的原因。
 *
 * 单独一行、小字、中性色：这不是错误，是她的选择，所以不能用错误样式；
 * 也不能和「已送达 12:23」挤在一行——一个是传输状态，一个是她的判断。
 */
function SilenceNote({ note }: { note: { text: string; run: number } }) {
  return (
    <p
      className={`message__silence${note.run > 1 ? ' message__silence--run' : ''}`}
      data-testid="silence-reason"
      data-run={note.run}
    >
      <span className="message__silence-tag">她没回</span>
      <span className="message__silence-text">{note.text}</span>
      {note.run > 1 ? (
        // 合并后的说明更淡：它解释的是一整段，不是某一条。
        <span className="message__silence-run">连续 {note.run} 条</span>
      ) : null}
    </p>
  );
}

export function MessageList({
  messages,
  activeTurn,
  live,
  loading,
  onRetry,
  onResend,
  onStop,
  onOpenSettings,
  variant = 'web',
  promptEnabled = false,
  canViewVoiceTranscript = false,
  promptOpenKey = null,
  onOpenPrompt,
  recallProposals = [],
  recallUnavailable = false,
  onReloadRecall,
  onConfirmRecall,
  onDeclineRecall,
  recallClock,
  voices,
  onVoicePlayed,
  onRefreshVoiceUrl,
}: MessageListProps) {
  const liveTurnId = live?.turnId ?? null;
  const actorName = usePersonaName();
  const rows = buildDialogueRows(messages, variant);
  /*
   * 提议按 `message_id` 分桶：卡片挂在"她想撤的那条"下面。
   * 找不到对应消息的提议**不能丢**——那条消息可能已经滚出这一页（后端只给
   * 最近 200 条），而提议仍然有效。它们统一挂到列表末尾一个"另外的提议"块里。
   */
  const byMessage = new Map<string, RecallProposalView[]>();
  const orphans: RecallProposalView[] = [];
  const knownIds = new Set(
    messages.map((message) => message.messageId).filter((id): id is string => Boolean(id)),
  );
  for (const proposal of recallProposals) {
    if (!knownIds.has(proposal.messageId)) {
      orphans.push(proposal);
      continue;
    }
    const list = byMessage.get(proposal.messageId) ?? [];
    list.push(proposal);
    byMessage.set(proposal.messageId, list);
  }
  // 语音按 message_id 取：`voices` 缺省时返回 undefined，既有路径零影响。
  const voiceFor = (messageId: string | null | undefined) =>
    messageId && voices ? voices.get(messageId) : undefined;

  const renderProposals = (messageId: string | null) => {
    const list = messageId ? byMessage.get(messageId) : undefined;
    if (!list || list.length === 0) return null;
    return (
      <div className="message__recall">
        {list.map((proposal) => (
          <RecallCard
            key={proposal.proposalId}
            proposal={proposal}
            excerpt={recallExcerpt(messages, proposal.messageId)}
            onConfirm={onConfirmRecall ?? (() => undefined)}
            onDecline={onDeclineRecall ?? (() => undefined)}
            {...(recallClock ? { clock: recallClock } : {})}
          />
        ))}
      </div>
    );
  };
  const showGeneratingRow =
    activeTurn !== null &&
    (live === null || live.turnId !== activeTurn.turn_id) &&
    !messages.some(
      (message) =>
        message.role === 'actor' &&
        message.turnId === activeTurn.turn_id &&
        (message.state === 'streaming' || message.state === 'pending'),
    );

  if (loading && messages.length === 0) {
    return (
      <div className="messages__placeholder">
        <span className="spinner" aria-hidden="true" />
        正在读取服务端历史…
      </div>
    );
  }

  return (
    <ol className="messages__list" aria-live="polite">
      {messages.length === 0 ? (
        <li className="messages__empty">
          <p className="messages__empty-title">开始一段对话</p>
          <p className="messages__empty-text">发送第一句话，回复会逐段出现在这里。</p>
        </li>
      ) : null}

      {rows.map((row) => {
        const { message } = row;
        if (message.system) {
          return (
            <li key={message.key} className="message message--system">
              <p>{message.text}</p>
            </li>
          );
        }
        const streaming = message.state === 'streaming' || message.state === 'pending';
        const clock = formatClock(message.createdAt);
        const inboundState = row.inbound ? statusText(message, liveTurnId, variant) : '';
        /*
         * 行的左右只由"是谁说的"决定（与 `role` 一一对应）：
         * - 她：`message--actor`（右对齐）+ `message--own`（她的气泡色）；
         * - 别人：`message--user`（左对齐）+ `message--inbound`（对方的泡泡）。
         *
         * `message--actor` / `message--user` 这两个类以前就存在（选择器和测试
         * 都在用），只是含义从"谁发起的请求"变成"谁说的话"——`data-role`
         * 仍是权威且不变。
         */
        const rowClass = [
          'message',
          row.own ? 'message--actor message--own' : 'message--user message--inbound',
          row.speaker && !row.showSpeaker ? 'message--continued' : '',
        ]
          .filter(Boolean)
          .join(' ');
        return (
          <li
            key={message.key}
            className={rowClass}
            data-state={message.state}
            data-role={message.role}
          >
            {/*
              头像与气泡**同侧**：她的话语在右，头像也在右（CSS 的
              `row-reverse` 把它甩到气泡右边）；别人的头像在左。
              DOM 顺序始终"头像 → 正文"，视觉左右只由 CSS 决定——
              这样键盘/读屏的阅读顺序不因左右对调而颠倒。
            */}
            {row.own ? (
              <div className="message__avatar" role="img" aria-label={actorName} title={actorName}>
                <span />
              </div>
            ) : row.inbound ? (
              // 群成员头像用名字首字 + 稳定色号：不同人一眼能分开，
              // 但不用颜色表示身份（同名不同人靠下面的号码区分）。
              <span
                className="message__avatar message__avatar--member"
                data-tone={avatarTone(row.speaker?.name ?? message.key)}
                aria-hidden="true"
              >
                {avatarInitial(row.speaker?.name)}
              </span>
            ) : (
              // 渠道私聊里对方没有群名片：给一个中性头像，别让整行只剩气泡。
              <span className="message__avatar message__avatar--member" data-tone={avatarTone(message.key)} aria-hidden="true">
                ·
              </span>
            )}
            <div className="message__body">
              {row.speaker && row.showSpeaker ? (
                <div className="message__sender">
                  <span className="message__sender-name" title={row.speaker.name}>
                    {row.speaker.name}
                  </span>
                  {row.speaker.id ? (
                    <span className="message__sender-id" title={row.speaker.id}>
                      {row.speaker.id}
                    </span>
                  ) : null}
                </div>
              ) : null}
              <div
                className={`message__bubble ${streaming ? 'message__bubble--streaming' : ''}${
                  message.recalledAt ? ' message__bubble--recalled' : ''
                }`}
              >
                {/*
                  撤回后的气泡：正文仍然显示（用户可能正需要确认撤掉的是哪一句），
                  但整体降权 + 一个「已撤回」徽标。**不能**把正文换成"已撤回"三个字
                  ——那就等于替用户决定了"内容不重要"，而撤回恰恰是因为内容重要。
                */}
                {message.recalledAt ? (
                  <span className="message__recalled" data-testid="message-recalled">
                    {message.recalledByActor ? '她撤回了这条' : '对方撤回了一条消息'}
                  </span>
                ) : null}
                {message.text ? (
                  <span className="message__text">{message.text}</span>
                ) : streaming ? (
                  <span className="message__typing" aria-label="正在生成">
                    <i />
                    <i />
                    <i />
                  </span>
                ) : message.outputModality === 'voice' ? (
                  <span className="message__empty">
                    语音 · {message.speechState === 'ready' ? '已生成' :
                      message.speechState === 'failed' ? '生成失败' :
                      message.speechState === 'cancelled' ? '已取消' : '处理中'}
                  </span>
                ) : message.state === 'stopped' ? (
                  <span className="message__empty">已停止，未生成内容</span>
                ) : (
                  <span className="message__empty">（无内容）</span>
                )}
                {streaming && message.text ? <span className="caret" aria-hidden="true" /> : null}
              </div>
              {/*
                语音消息（SP03）：`<audio>` 放不进气泡文本里——它是块级控件，
                塞进 `message__text` 会让正文与播放器互相挤。所以挂在气泡**下面**，
                与撤回卡片同一位置约定（"紧贴这条消息"）。

                只画她自己说的（`row.own`）：用户的语音属于入站媒体，另一套授权。
              */}
              {row.own && voiceFor(message.messageId) ? (
                <VoicePlayer
                  voice={voiceFor(message.messageId)!}
                  onPlayed={onVoicePlayed}
                  refreshUrl={onRefreshVoiceUrl}
                />
              ) : null}
              {row.own && canViewVoiceTranscript && message.outputModality === 'voice' ? (
                <OperatorSpeechText messageId={message.messageId ?? message.key} />
              ) : null}
              {message.state === 'failed' ? (
                <ErrorCard
                  message={message}
                  onRetry={onRetry}
                  onResend={onResend}
                  onOpenSettings={onOpenSettings}
                />
              ) : null}
              {/* 她想撤回这条：卡片紧贴在这条消息下面，不另找地方。 */}
              {renderProposals(message.messageId)}
              {/*
                meta 行按"谁说的话"分，不再按"谁发起的请求"：
                - 她：完整状态（模型、第几次尝试、停止后继续）——左右对调之后
                  她自己出现在右边，这套原来只属于"我发的"渲染要跟着搬过来；
                - 别人（`role=user`）：只留状态与时间，渠道会话里不逐条刷「已送达」。
              */}
              {row.own ? (
                <ActorMeta
                  message={message}
                  liveTurnId={liveTurnId}
                  onRetry={onRetry}
                  variant={variant}
                  align="end"
                />
              ) : message.role === 'user' ? (
                // 渠道会话的入站消息：只留时间，状态只在失败/待发时出现。
                inboundState || clock ? (
                  <div className="message__meta">
                    {inboundState ? (
                      <span className={`state state--${message.state}`}>{inboundState}</span>
                    ) : null}
                    {clock ? <span className="message__time">{clock}</span> : null}
                  </div>
                ) : null
              ) : (
                <ActorMeta
                  message={message}
                  liveTurnId={liveTurnId}
                  onRetry={onRetry}
                  variant={variant}
                />
              )}
              {/*
                她的消息下面那行小字：她这一轮到底看到了什么。
                没有 turn_id / 已经被归档的消息**也要能点**——服务端 404 时
                抽屉里给「这次没有记录」，不在入口上提前猜。
              */}
              {message.role === 'actor' && promptEnabled && onOpenPrompt ? (
                <button
                  type="button"
                  className="message__prompt"
                  data-testid="prompt-entry"
                  aria-haspopup="dialog"
                  aria-expanded={promptOpenKey === message.key}
                  aria-controls={PROMPT_DRAWER_ID}
                  onClick={(event) => onOpenPrompt(message, event.currentTarget)}
                >
                  {PROMPT_ENTRY_LABEL}
                </button>
              ) : null}
              {row.silence ? <SilenceNote note={row.silence} /> : null}
            </div>
          </li>
        );
      })}

      {orphans.length > 0 ? (
        <li className="messages__recall-orphans" data-testid="recall-orphans">
          <p className="messages__recall-orphans-title">其它还没处理的撤回提议</p>
          {orphans.map((proposal) => (
            <RecallCard
              key={proposal.proposalId}
              proposal={proposal}
              excerpt={null}
              onConfirm={onConfirmRecall ?? (() => undefined)}
              onDecline={onDeclineRecall ?? (() => undefined)}
              {...(recallClock ? { clock: recallClock } : {})}
            />
          ))}
        </li>
      ) : null}

      {recallUnavailable ? (
        <li className="messages__recall-unavailable">
          <RecallUnavailableCard {...(onReloadRecall ? { onRetry: onReloadRecall } : {})} />
        </li>
      ) : null}

      {showGeneratingRow && activeTurn ? (
        <li className="message message--actor message--generating" data-role="actor">
          <div className="message__avatar" role="img" aria-label={actorName} title={actorName}>
            <span className="message__avatar-pulse" />
          </div>
          <div className="message__body">
            <div className="message__bubble message__bubble--streaming">
              <span className="message__typing" aria-label="服务端仍在生成">
                <i />
                <i />
                <i />
              </span>
            </div>
            <div className="message__meta">
              <span className="state state--streaming">
                <span className="state__pulse" aria-hidden="true" />
                服务端仍在生成
              </span>
              <span className="message__attempt">刷新不会中断生成</span>
              <button type="button" className="button button--small" onClick={onStop}>
                <StopIcon size={12} /> 停止
              </button>
            </div>
          </div>
        </li>
      ) : null}
    </ol>
  );
}
