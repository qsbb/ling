import { useCallback, useEffect, useRef, useState } from 'react';
import { formatDuration } from '../format';
import type { UserInfo } from '../types';
import { useChat, type UiMessage } from '../useChat';
import { Composer } from './Composer';
import { WindowPolicy } from './WindowPolicy';
import { AlertIcon, PencilIcon } from './Icons';
import { MessageList } from './MessageList';
import {
  canOpenPromptSnapshot,
  PromptDrawer,
  promptEntryAction,
} from './PromptDrawer';
import { canRenameSession, describeSession, SessionSidebar } from './SessionSidebar';
import './chat-page.css';

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

/**
 * 会话设置面板。
 *
 * **单实例**是这里的硬约束（需求 1）：面板由一个 `open` 布尔驱动，而不是
 * "每条会话一个面板、各自打开"。重复点按钮是 toggle；切会话时同一份状态
 * 复位成关闭——所以既不会叠出两层，也不会把上一条会话的设置留在新会话上。
 *
 * 用 <dialog>-like 的浮层 + 遮罩，是因为它从标题栏按钮打开、覆盖消息区，
 * 比在标题栏下面插一行把正文整体推下去更稳（后者在窄屏会把正文挤到看不见）。
 */
function SessionSettingsPanel({
  open,
  sessionId,
  groupReplyLimit,
  accessTarget,
  onGroupReplyLimitSaved,
  onClose,
}: {
  open: boolean;
  sessionId: string;
  groupReplyLimit: number | null | undefined;
  accessTarget: { kind: 'group' | 'private'; id: string } | undefined;
  onGroupReplyLimitSaved: () => void;
  onClose: () => void;
}) {
  const panelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onClose();
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    panelRef.current?.focus();
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="session-settings" data-testid="session-settings-overlay">
      <button
        type="button"
        className="session-settings__scrim"
        aria-label="收起设置面板"
        onClick={onClose}
      />
      <div
        className="session-settings__panel"
        id="active-window-policy"
        role="dialog"
        aria-label="会话设置"
        data-testid="session-settings"
        tabIndex={-1}
        ref={panelRef}
      >
        <WindowPolicy
          key={sessionId}
          sessionId={sessionId}
          groupReplyLimit={groupReplyLimit}
          onGroupReplyLimitSaved={onGroupReplyLimitSaved}
          accessTarget={accessTarget}
          onClose={onClose}
        />
      </div>
    </div>
  );
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
  const [editingTitleId, setEditingTitleId] = useState<string | null>(null);
  const [savingTitle, setSavingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState('');
  /**
   * 会话设置只有一个开关。
   *
   * 绑定的是 `sessionId` 而不是"每条会话各存一份"——换会话时下面的 effect
   * 把它清零，所以重复点、切会话都不会叠层。
   */
  const [settingsOpen, setSettingsOpen] = useState(false);
  /** 标题栏的外部键（窗口键/会话 id）按需展开，默认不占阅读宽度。 */
  const [detailsOpen, setDetailsOpen] = useState(false);
  const listRef = useRef<HTMLDivElement | null>(null);
  const stickToBottom = useRef(true);
  /**
   * 往上分页时保住阅读位置。
   *
   * 新内容拼在**前面**，浏览器会把同一批 DOM 整体往下推 `delta`，于是读者
   * 正看的那一行瞬间跳出视口顶部。做法：拼接前后各记一次
   * （`scrollHeight`、`scrollTop`、`messages.length`），差出多少就补回多少。
   * 只对"条数变多且用户不在底部"生效——发送新消息导致的变高仍走贴底逻辑。
   */
  const scrollAnchor = useRef<{ height: number; top: number; count: number } | null>(null);
  const titleInputRef = useRef<HTMLInputElement | null>(null);
  const settingsButtonRef = useRef<HTMLButtonElement | null>(null);
  /**
   * 「她这次看到了什么」抽屉。
   *
   * 打开状态按**消息 key** 记（不是 messageId）：本地还没落库的消息没有
   * messageId，但入口照样可点，抽屉里给「这次没有记录」。两个字段分开存，
   * 是为了让"点哪一条"和"查哪一条"各自说清。
   */
  const [promptOpenKey, setPromptOpenKey] = useState<string | null>(null);
  const [promptMessageId, setPromptMessageId] = useState<string | null>(null);
  /** 入口元素：关闭后焦点回到它（否则焦点掉回 <body>，键盘用户得从头 Tab）。 */
  const promptOpenerRef = useRef<HTMLElement | null>(null);
  /** 开抽屉前的阅读位置：关闭后回到这里（需求 3）。 */
  const promptScrollRef = useRef<number | null>(null);
  /** 只有 owner 能看完整提示词；后端对其他人一律 404，入口也就不出现。 */
  const promptEnabled = canOpenPromptSnapshot(user.role);

  const session = chat.sessions.find((item) => item.session_id === chat.currentId) ?? null;
  const canEditPolicy = user.role === 'owner' && session !== null && !session.archived;
  // 标题给人看（群名/昵称），外部键留给按需展开；读不到名字时按号码回退。
  const heading = session ? describeSession(session) : null;
  // 拆不出群/私聊的旧渠道窗口按私聊排（署名、靠左），不硬猜。
  const variant =
    heading === null || heading.kind === 'web'
      ? 'web'
      : heading.kind === 'channel'
        ? 'private'
        : heading.kind;
  const renamable = session ? canRenameSession(session, user.role) : false;
  const editingTitle = renamable && editingTitleId !== null && editingTitleId === chat.currentId;
  const titleRequired = heading?.kind === 'web';
  const busy = chat.live !== null || chat.activeTurn !== null;
  const stopping = chat.live?.state === 'stopping' || chat.activeTurn?.state === 'stopping';
  const elapsed = useElapsed(chat.live?.startedAt ?? null);

  useEffect(() => {
    stickToBottom.current = true;
    setSettingsOpen(false);
    setDetailsOpen(false);
    setEditingTitleId(null);
  }, [chat.currentId]);

  useEffect(() => {
    const node = listRef.current;
    if (!node) return;
    const anchor = scrollAnchor.current;
    if (anchor && chat.messages.length > anchor.count) {
      node.scrollTop = anchor.top + (node.scrollHeight - anchor.height);
      scrollAnchor.current = null;
      // 补完位置后视口已经不在顶部，避免同一次拼接再触发一次分页。
      stickToBottom.current =
        node.scrollHeight - node.scrollTop - node.clientHeight < 96;
      return;
    }
    if (!stickToBottom.current) return;
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
    setEditingTitleId(chat.currentId);
  };

  const commitTitle = async () => {
    if (!editingTitle || !editingTitleId || savingTitle || (titleRequired && !titleDraft.trim())) return;
    setSavingTitle(true);
    try {
      if (await chat.renameSession(editingTitleId, titleDraft)) {
        setEditingTitleId((current) => (current === editingTitleId ? null : current));
      }
    } finally {
      setSavingTitle(false);
    }
  };

  const closeSettings = useCallback(() => {
    setSettingsOpen(false);
    settingsButtonRef.current?.focus();
  }, []);

  const closePrompt = useCallback(() => {
    const opener = promptOpenerRef.current;
    const scrolledTo = promptScrollRef.current;
    promptOpenerRef.current = null;
    promptScrollRef.current = null;
    setPromptOpenKey(null);
    setPromptMessageId(null);
    /*
     * 抽屉只盖住右侧，背后的对话区仍然可滚（组件刻意不做全屏禁用）。
     * 所以"回到原阅读位置"不能靠浏览器自动恢复：用户可能在抽屉开着时
     * 滚了背景。这里把开抽屉前记下的 scrollTop 还回去，再把焦点还给入口。
     * 两步都等这一帧卸载完再做，否则会被卸载时的清理覆盖。
     */
    window.setTimeout(() => {
      const list = listRef.current;
      if (list && scrolledTo !== null) list.scrollTop = scrolledTo;
      if (opener?.isConnected) opener.focus();
    }, 0);
  }, []);

  const togglePrompt = useCallback(
    (message: UiMessage, trigger: HTMLElement) => {
      if (promptEntryAction(promptOpenKey, message.key) === 'close') {
        closePrompt();
        return;
      }
      promptOpenerRef.current = trigger;
      promptScrollRef.current = listRef.current?.scrollTop ?? null;
      setPromptOpenKey(message.key);
      setPromptMessageId(message.messageId);
    },
    [closePrompt, promptOpenKey],
  );

  // 换会话就收起：抽屉里的内容是上一条会话的快照，留着会张冠李戴。
  useEffect(() => {
    setPromptOpenKey(null);
    setPromptMessageId(null);
    promptOpenerRef.current = null;
    promptScrollRef.current = null;
  }, [chat.currentId]);

  const meta = [
    heading ? heading.badge : null,
    session ? `${session.message_count} 条消息` : null,
  ].filter(Boolean);

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
        onPin={chat.pinSession}
        onMove={chat.moveSession}
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
                placeholder={titleRequired ? undefined : '使用渠道名称'}
                disabled={savingTitle}
                onChange={(event) => setTitleDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Escape') setEditingTitleId(null);
                }}
              />
              <button
                type="submit"
                className="button button--small button--primary"
                disabled={savingTitle || (titleRequired && !titleDraft.trim())}
              >
                保存
              </button>
              <button
                type="button"
                className="button button--small"
                onClick={() => setEditingTitleId(null)}
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
                {meta.map((item) => (
                  <span key={item} className="chat-titlebar__stat">
                    {item}
                  </span>
                ))}
                {busy ? (
                  <span className="chat-titlebar__stat chat-titlebar__stat--busy" data-testid="generating-badge">
                    {elapsed ? `生成中 · 已等待 ${elapsed}` : '生成中…'}
                  </span>
                ) : null}
                {heading ? (
                  <button
                    type="button"
                    className="chat-titlebar__meta-toggle"
                    aria-expanded={detailsOpen}
                    aria-controls="chat-titlebar-details"
                    onClick={() => setDetailsOpen((current) => !current)}
                  >
                    {detailsOpen ? '收起信息' : '会话信息'}
                  </button>
                ) : null}
              </p>
              {detailsOpen && heading ? (
                <dl className="chat-titlebar__details" id="chat-titlebar-details" data-testid="session-details">
                  <div>
                    <dt>会话键</dt>
                    <dd className="chat-titlebar__id">{heading.subtitle}</dd>
                  </div>
                  <div>
                    <dt>类型</dt>
                    <dd>{heading.badge}</dd>
                  </div>
                </dl>
              ) : null}
            </div>
          )}

          <div className="chat-titlebar__actions">
            {canEditPolicy ? (
              <button
                ref={settingsButtonRef}
                type="button"
                className="ghost-button"
                aria-expanded={settingsOpen}
                aria-controls="active-window-policy"
                data-testid="session-settings-toggle"
                onClick={() => setSettingsOpen((current) => !current)}
              >
                会话设置
              </button>
            ) : null}
            {!editingTitle && session && renamable ? (
              <button type="button" className="ghost-button" onClick={startTitleEdit}>
                <PencilIcon /> 改名
              </button>
            ) : null}
          </div>
        </header>

        {canEditPolicy && session ? (
          <SessionSettingsPanel
            open={settingsOpen}
            sessionId={session.session_id}
            groupReplyLimit={heading?.kind === 'group' ? session.group_reply_max_parts ?? null : undefined}
            accessTarget={(heading?.kind === 'group' || heading?.kind === 'private') && session.peer_id
              ? { kind: heading.kind, id: session.peer_id }
              : undefined}
            onGroupReplyLimitSaved={chat.reloadSessions}
            onClose={closeSettings}
          />
        ) : null}

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

        {/*
          抽屉打开时给对话区让出抽屉的宽度（`data-prompt-open` → CSS 里
          `padding-right: clamp(420px, 34vw, 560px)`）。她的消息贴右，入口按钮
          也跟着贴右；不让位的话入口会被从右侧滑出的面板整个压住——
          "再点入口 = 收起"就永远没有机会触发。这里不用 z-index /
          pointer-events 把入口抬到面板之上（那会浮在面板内容上）。≤560px
          时抽屉占满视口，让位没有意义，由媒体查询跳过。
          数据属性放在滚动容器上而不是 `.chat-main`：只让消息区让位，
          标题栏与输入框仍占满整宽。
        */}
        <div
          className="messages"
          data-prompt-open={promptOpenKey !== null ? 'true' : undefined}
          ref={listRef}
          data-testid="message-scroll"
          onScroll={(event) => {
            const node = event.currentTarget;
            stickToBottom.current =
              node.scrollHeight - node.scrollTop - node.clientHeight < 96;
            // 滚到顶（留 4px 余量）自动取更早一页。`loadOlder` 自己带并发锁，
            // 连续触发最多多一次空请求。
            if (node.scrollTop <= 4 && chat.hasMoreHistory && !chat.loadingOlder) {
              scrollAnchor.current = {
                height: node.scrollHeight,
                top: node.scrollTop,
                count: chat.messages.length,
              };
              chat.loadOlder();
            }
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
              loadingOlder={chat.loadingOlder}
              hasMoreHistory={chat.hasMoreHistory}
              onLoadOlder={() => {
                const node = listRef.current;
                if (node) {
                  scrollAnchor.current = {
                    height: node.scrollHeight,
                    top: node.scrollTop,
                    count: chat.messages.length,
                  };
                }
                chat.loadOlder();
              }}
              variant={variant}
              onRetry={chat.retry}
              onResend={chat.resend}
              onStop={() => void chat.stop()}
              onOpenSettings={onOpenSettings}
              promptEnabled={promptEnabled}
              canViewVoiceTranscript={user.role === 'owner' || user.role === 'admin'}
              promptOpenKey={promptOpenKey}
              onOpenPrompt={togglePrompt}
              recallProposals={chat.recallProposals}
              recallUnavailable={chat.recallProposalsUnavailable}
              onReloadRecall={chat.reloadRecallProposals}
              onConfirmRecall={chat.confirmRecall}
              onDeclineRecall={chat.declineRecall}
              voices={chat.voices}
              onVoicePlayed={chat.noteVoicePlayed}
              onRefreshVoiceUrl={chat.refreshVoiceUrl}
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
          onAttached={() => {
            chat.reloadMessages();
            chat.reloadSessions();
          }}
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

      {promptEnabled ? (
        <PromptDrawer
          open={promptOpenKey !== null}
          messageId={promptMessageId}
          onClose={closePrompt}
        />
      ) : null}
    </div>
  );
}
