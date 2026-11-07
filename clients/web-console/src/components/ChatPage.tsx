import { useEffect, useRef, useState } from 'react';
import { formatDuration } from '../format';
import type { UserInfo } from '../types';
import { useChat } from '../useChat';
import { Composer } from './Composer';
import { AlertIcon, PencilIcon } from './Icons';
import { MessageList } from './MessageList';
import { canRenameSession, describeSession, SessionSidebar } from './SessionSidebar';

interface ChatPageProps {
  sessionId: string | null;
  user: UserInfo;
  sidebarOpen: boolean;
  onCloseSidebar: () => void;
  onTestMode: (value: boolean) => void;
  onOpenSettings: () => void;
  onOpenPersona: () => void;
  onLogout: () => void;
}

function useElapsed(startedAt: number | null): string {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (startedAt === null) return;
    const timer = window.setInterval(() => setTick((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);
  if (startedAt === null) return '';
  return formatDuration((Date.now() - startedAt) / 1000);
}

export function ChatPage({
  sessionId,
  user,
  sidebarOpen,
  onCloseSidebar,
  onTestMode,
  onOpenSettings,
  onOpenPersona,
  onLogout,
}: ChatPageProps) {
  const chat = useChat(sessionId, onTestMode);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState('');
  const listRef = useRef<HTMLDivElement | null>(null);
  const stickToBottom = useRef(true);
  const titleInputRef = useRef<HTMLInputElement | null>(null);

  const session = chat.sessions.find((item) => item.session_id === chat.currentId) ?? null;
  // 标题给人看（群名/昵称），副标题留给机器键；读不到名字时按号码回退。
  const heading = session ? describeSession(session) : null;
  // 拆不出群/私聊的旧渠道窗口按私聊排（署名、靠左），不硬猜。
  const variant =
    heading === null || heading.kind === 'web'
      ? 'web'
      : heading.kind === 'channel'
        ? 'private'
        : heading.kind;
  const renamable = session ? canRenameSession(session) : false;
  const busy = chat.live !== null || chat.activeTurn !== null;
  const stopping = chat.live?.state === 'stopping' || chat.activeTurn?.state === 'stopping';
  const elapsed = useElapsed(chat.live?.startedAt ?? null);

  useEffect(() => {
    stickToBottom.current = true;
  }, [chat.currentId]);

  useEffect(() => {
    const node = listRef.current;
    if (!node || !stickToBottom.current) return;
    node.scrollTop = node.scrollHeight;
  }, [chat.messages, chat.activeTurn, chat.live]);

  useEffect(() => {
    if (!editingTitle) return;
    titleInputRef.current?.focus();
    titleInputRef.current?.select();
  }, [editingTitle]);

  useEffect(() => {
    if (!chat.notice) return;
    const timer = window.setTimeout(() => chat.dismissNotice(), 9000);
    return () => window.clearTimeout(timer);
  }, [chat.notice, chat.dismissNotice]);

  const startTitleEdit = () => {
    if (!chat.currentId) return;
    setTitleDraft(heading?.title ?? '');
    setEditingTitle(true);
  };

  const commitTitle = async () => {
    if (!chat.currentId) return;
    await chat.renameSession(chat.currentId, titleDraft);
    setEditingTitle(false);
  };

  return (
    <div className="chat-layout">
      <SessionSidebar
        sessions={chat.sessions}
        currentId={chat.currentId}
        open={sidebarOpen}
        user={user}
        onClose={onCloseSidebar}
        onSelect={chat.selectSession}
        onCreate={() => {
          onCloseSidebar();
          chat.createSession();
        }}
        onRename={chat.renameSession}
        onNavigateSettings={onOpenSettings}
        onNavigatePersona={onOpenPersona}
        onLogout={onLogout}
      />

      <section className="chat-main" aria-label="对话">
        <header className="chat-titlebar">
          {editingTitle ? (
            <form
              className="chat-titlebar__edit"
              onSubmit={(event) => {
                event.preventDefault();
                void commitTitle();
              }}
            >
              <input
                ref={titleInputRef}
                value={titleDraft}
                maxLength={60}
                aria-label="会话名称"
                onChange={(event) => setTitleDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Escape') setEditingTitle(false);
                }}
              />
              <button type="submit" className="button button--small button--primary">
                保存
              </button>
              <button
                type="button"
                className="button button--small"
                onClick={() => setEditingTitle(false)}
              >
                取消
              </button>
            </form>
          ) : (
            <div className="chat-titlebar__titles">
              <h1 className="chat-titlebar__title" data-testid="session-title" title={heading?.title}>
                {heading?.title ?? '对话'}
              </h1>
              <p className="chat-titlebar__sub">
                {heading ? (
                  <span className="chat-titlebar__channel" data-kind={heading.kind}>
                    {heading.badge}
                  </span>
                ) : null}
                {heading ? (
                  // 副标题是完整会话键：能定位到"这条会话连的是哪个群"。
                  <span className="chat-titlebar__id" title={heading.subtitle}>
                    {heading.subtitle}
                  </span>
                ) : null}
                <span className="chat-titlebar__stat">
                  · {session?.message_count ?? 0} 条消息
                </span>
                {busy ? (
                  <span className="chat-titlebar__stat chat-titlebar__stat--busy">
                    · {elapsed ? `生成中 · 已等待 ${elapsed}` : '生成中…'}
                  </span>
                ) : null}
              </p>
            </div>
          )}

          <div className="chat-titlebar__actions">
            {busy ? (
              <span className="badge badge--live" data-testid="generating-badge">
                <span className="badge__dot" aria-hidden="true" />
                生成中
              </span>
            ) : null}
            {!editingTitle && session && renamable ? (
              <button type="button" className="ghost-button" onClick={startTitleEdit}>
                <PencilIcon /> 改名
              </button>
            ) : null}
          </div>
        </header>

        {chat.loadError ? (
          <div className="load-error" role="alert">
            <div className="load-error__head">
              <AlertIcon />
              <strong>{chat.loadError.reason}</strong>
            </div>
            {chat.loadError.hint ? <p>{chat.loadError.hint}</p> : null}
            {chat.loadError.detail ? <p className="load-error__detail">{chat.loadError.detail}</p> : null}
            <button
              type="button"
              className="button button--small"
              onClick={() => {
                chat.reloadSessions();
                chat.reloadMessages();
              }}
            >
              重新读取
            </button>
          </div>
        ) : null}

        <div
          className="messages"
          ref={listRef}
          data-testid="message-scroll"
          onScroll={(event) => {
            const node = event.currentTarget;
            stickToBottom.current =
              node.scrollHeight - node.scrollTop - node.clientHeight < 96;
          }}
        >
          {chat.bootstrapping ? (
            <div className="messages__placeholder">
              <span className="spinner" aria-hidden="true" />
              正在读取会话…
            </div>
          ) : (
            <MessageList
              messages={chat.messages}
              activeTurn={chat.activeTurn}
              live={chat.live}
              loading={chat.loadingMessages}
              variant={variant}
              onRetry={chat.retry}
              onResend={chat.resend}
              onStop={() => void chat.stop()}
              onOpenSettings={onOpenSettings}
            />
          )}
        </div>

        <Composer
          busy={busy}
          stopping={stopping}
          disabled={!chat.currentId || chat.bootstrapping}
          sessionId={chat.currentId}
          onSend={chat.send}
          onStop={() => void chat.stop()}
        />

        {chat.notice ? (
          <div className={`toast toast--${chat.notice.kind}`} role="status">
            <div className="toast__body">
              <strong>{chat.notice.text}</strong>
              {chat.notice.detail ? <span>{chat.notice.detail}</span> : null}
            </div>
            <button
              type="button"
              className="toast__close"
              onClick={chat.dismissNotice}
              aria-label="关闭提示"
            >
              ×
            </button>
          </div>
        ) : null}
      </section>
    </div>
  );
}
