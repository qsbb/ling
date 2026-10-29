import { formatClock } from '../format';
import { retryLabel } from '../errors';
import type { LiveTurn, UiMessage } from '../useChat';
import type { TurnView } from '../types';
import { AlertIcon, CheckIcon, RefreshIcon, StopIcon } from './Icons';

interface MessageListProps {
  messages: UiMessage[];
  activeTurn: TurnView | null;
  live: LiveTurn | null;
  loading: boolean;
  onRetry: (turnId: string) => void;
  onResend: (message: UiMessage) => void;
  onStop: () => void;
  onOpenSettings: () => void;
}

const SETTINGS_CODES = new Set([
  'secret_missing',
  'config_invalid',
  'provider_unavailable',
  'provider_timeout',
  'provider_error',
]);

function statusText(message: UiMessage, liveTurnId: string | null): string {
  if (message.role === 'user') {
    switch (message.state) {
      case 'pending':
        return '发送中…';
      case 'failed':
        return '发送失败';
      case 'stopped':
        return '已停止';
      default:
        return '已送达';
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
}: {
  message: UiMessage;
  liveTurnId: string | null;
  onRetry: (turnId: string) => void;
}) {
  return (
    <div className="message__meta">
      <span className={`state state--${message.state}`}>
        {message.state === 'completed' ? <CheckIcon size={12} /> : null}
        {message.state === 'streaming' || message.state === 'pending' ? (
          <span className="state__pulse" aria-hidden="true" />
        ) : null}
        {statusText(message, liveTurnId)}
      </span>
      {message.modelLabel ? <span className="message__model">{message.modelLabel}</span> : null}
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

export function MessageList({
  messages,
  activeTurn,
  live,
  loading,
  onRetry,
  onResend,
  onStop,
  onOpenSettings,
}: MessageListProps) {
  const liveTurnId = live?.turnId ?? null;
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

      {messages.map((message) => {
        if (message.system) {
          return (
            <li key={message.key} className="message message--system">
              <p>{message.text}</p>
            </li>
          );
        }
        const isUser = message.role === 'user';
        const streaming = message.state === 'streaming' || message.state === 'pending';
        return (
          <li
            key={message.key}
            className={`message ${isUser ? 'message--user' : 'message--actor'}`}
            data-state={message.state}
            data-role={message.role}
          >
            {!isUser ? (
              <div className="message__avatar" aria-hidden="true">
                <span />
              </div>
            ) : null}
            <div className="message__body">
              {!isUser ? <div className="message__author">ling</div> : null}
              <div className={`message__bubble ${streaming ? 'message__bubble--streaming' : ''}`}>
                {message.text ? (
                  <span className="message__text">{message.text}</span>
                ) : streaming ? (
                  <span className="message__typing" aria-label="正在生成">
                    <i />
                    <i />
                    <i />
                  </span>
                ) : message.state === 'stopped' ? (
                  <span className="message__empty">已停止，未生成内容</span>
                ) : (
                  <span className="message__empty">（无内容）</span>
                )}
                {streaming && message.text ? <span className="caret" aria-hidden="true" /> : null}
              </div>
              {message.state === 'failed' ? (
                <ErrorCard
                  message={message}
                  onRetry={onRetry}
                  onResend={onResend}
                  onOpenSettings={onOpenSettings}
                />
              ) : null}
              {isUser ? (
                <>
                  <div className="message__meta message__meta--user">
                    {message.state === 'pending' ? (
                      <span className="state__pulse" aria-hidden="true" />
                    ) : null}
                    <span className={`state state--${message.state}`}>
                      {statusText(message, liveTurnId)}
                    </span>
                    {message.state === 'stopped' ? (
                      <span className="message__attempt">可重新发送</span>
                    ) : null}
                    <span className="message__time">{formatClock(message.createdAt)}</span>
                  </div>
                  {message.silenceReason ? (
                    // 她选择不回时把理由显示出来。没有这行的话，用户看到的
                    // 是一条没人理的消息，像故障——而沉默和失败都要能被解释。
                    // 用柔和样式而不是错误样式：这不是错误，是她的选择。
                    <p className="message__silence" data-testid="silence-reason">
                      她没回：{message.silenceReason}
                    </p>
                  ) : null}
                </>
              ) : (
                <ActorMeta message={message} liveTurnId={liveTurnId} onRetry={onRetry} />
              )}
            </div>
          </li>
        );
      })}

      {showGeneratingRow && activeTurn ? (
        <li className="message message--actor message--generating" data-role="actor">
          <div className="message__avatar" aria-hidden="true">
            <span className="message__avatar-pulse" />
          </div>
          <div className="message__body">
            <div className="message__author">ling</div>
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
