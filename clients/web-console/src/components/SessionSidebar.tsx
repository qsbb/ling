import { useEffect, useRef, useState } from 'react';
import { formatSessionTime } from '../format';
import type { SessionRecord, UserInfo } from '../types';
import {
  CloseIcon,
  LogoutIcon,
  PencilIcon,
  PersonaIcon,
  PlusIcon,
  SettingsIcon,
} from './Icons';

/**
 * 会话该按哪套版式渲染。
 *
 * `group` 和 `private` 的窗口里，`role=user` 是**群里/对面的某个人**，不是浏览器
 * 前面这位：所以消息要署名、不能一律靠右。`web` 才是"我问她答"的老版式。
 * `channel` 是"知道是渠道窗口、但拆不出群还是私聊"（旧数据里标题被人改过）：
 * 照样署名，只是不硬猜是群还是私聊。
 */
export type DialogueKind = 'group' | 'private' | 'channel' | 'web';

/** 会话列表与标题栏只需要这几项；`display_name`/`channel`/`peer_id` 旧服务端没有。 */
export type SessionLike = Pick<SessionRecord, 'session_id' | 'title' | 'window_id' | 'subject_id'> &
  Partial<Pick<SessionRecord, 'display_name' | 'channel' | 'peer_id'>>;

export interface ChannelRef {
  /** 原始键，展示时作为副标题（完整、不截断，交给 CSS 省略）。 */
  raw: string;
  instance: string;
  kind: 'group' | 'private';
  peerId: string;
}

/** 渠道窗口键：`onebot-default:group:100200400` / `…:private:100200301`。 */
const CHANNEL_REF = /^([^:\s]+):(group|private):([^:\s]+)(?::[^:\s]+)*$/;

/**
 * 拆开渠道窗口键。
 *
 * 这个键由 bridge 用 `window_ref` 直接写成窗口标题，也是它找回同一窗口的依据
 * （`bridge._ensure_window` 按 `Window.title == window_ref` 匹配）。所以它永远
 * 存在于数据里，但不适合给人看。
 */
export function parseChannelRef(value: string | null | undefined): ChannelRef | null {
  const raw = (value ?? '').trim();
  const matched = CHANNEL_REF.exec(raw);
  if (!matched) return null;
  const [, instance = '', kind = '', peerId = ''] = matched;
  if (kind !== 'group' && kind !== 'private') return null;
  return { raw, instance, kind, peerId };
}

export interface SessionHeading {
  /** 给人看的标题：群名/对方昵称；读不到时按号码推导，绝不用裸机器键。 */
  title: string;
  /** 机器键（原始窗口键或会话 id）。截断由 CSS 做，`title` 属性给全量。 */
  subtitle: string;
  kind: DialogueKind;
  badge: string;
}

/**
 * 把一条会话翻成「标题 + 副标题」。
 *
 * 为什么必须推导而不能直接显示 `title`：渠道窗口的 title 是
 * `onebot-default:group:100200400`，在侧栏里被截成 `onebot-default:group:1002004…`
 * 之后，用户既看不出是哪个群，也读不到群号。群名读不到时宁可显示「群 100200400」
 * ——那是用户自己能在 QQ 里对上的东西。
 */
export function describeSession(session: SessionLike): SessionHeading {
  const rawTitle = (session.title ?? '').trim();
  const ref =
    parseChannelRef(rawTitle) ??
    parseChannelRef(session.window_id) ??
    parseChannelRef(session.subject_id) ??
    null;
  const channel = (session.channel ?? '').trim().toLowerCase();
  const hasPeer = Boolean((session.peer_id ?? '').trim());
  // 旧服务端没有 channel 字段：有没有对端号码就是"是不是渠道窗口"的证据。
  const kind: DialogueKind =
    ref?.kind ??
    (channel === 'group' || channel === 'private'
      ? channel
      : channel === 'web' || (channel === '' && !hasPeer)
        ? 'web'
        : 'channel');

  const rawDisplayName = (session.display_name ?? '').trim();
  // 服务端如果一时把窗口键当名字塞进来（历史上 title 就是这么用的），
  // 也不能让它变回标题：名字必须是能读的，否则走号码回退。
  const displayName = parseChannelRef(rawDisplayName) === null ? rawDisplayName : '';
  const peerId = (session.peer_id ?? '').trim() || ref?.peerId || null;
  const titleIsMachineKey = ref !== null;

  let title: string;
  if (displayName) {
    title = displayName;
  } else if (titleIsMachineKey || !rawTitle) {
    if (kind === 'group') title = peerId ? `群 ${peerId}` : '群聊';
    else if (kind === 'private') title = peerId ? `私聊 ${peerId}` : '私聊';
    else if (kind === 'channel') title = peerId ? `渠道 ${peerId}` : '渠道会话';
    else title = rawTitle || '对话';
  } else {
    // 网页会话的标题是用户自己起的名字，原样保留。
    title = rawTitle;
  }

  const badge =
    kind === 'group' ? '群' : kind === 'private' ? '私聊' : kind === 'channel' ? '渠道' : '网页';
  return {
    title,
    subtitle: ref?.raw ?? (session.session_id || session.window_id),
    kind,
    badge,
  };
}

/**
 * 只有网页会话可以改名。
 *
 * 渠道窗口的标题就是窗口键：改名会让 `bridge._ensure_window` 找不到旧窗口，
 * 下一条群消息会再建一个同名窗口，群聊被劈成两段。名字请由 `display_name` 给。
 */
export function canRenameSession(session: SessionLike): boolean {
  return describeSession(session).kind === 'web';
}

interface SessionSidebarProps {
  sessions: SessionRecord[];
  currentId: string | null;
  open: boolean;
  user: UserInfo;
  onClose: () => void;
  onSelect: (sessionId: string) => void;
  onCreate: () => void;
  onRename: (sessionId: string, title: string) => Promise<void>;
  onNavigateSettings: () => void;
  onNavigatePersona: () => void;
  onLogout: () => void;
}

export function SessionSidebar({
  sessions,
  currentId,
  open,
  user,
  onClose,
  onSelect,
  onCreate,
  onRename,
  onNavigateSettings,
  onNavigatePersona,
  onLogout,
}: SessionSidebarProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (editingId) inputRef.current?.focus();
  }, [editingId]);

  const startEdit = (session: SessionRecord) => {
    setEditingId(session.session_id);
    // 改的是给人看的名字，不是机器键。
    setDraft(describeSession(session).title);
  };

  const commit = async () => {
    if (!editingId) return;
    const id = editingId;
    await onRename(id, draft);
    setEditingId(null);
  };

  // 空气泡是新建后没说话的会话。留着它们只会把真对话挤出视野，
  // 所以除当前这条正在看以外，不占列表位置（消息还被服务端留着）。
  const visible = sessions.filter(
    (session) => session.message_count > 0 || session.session_id === currentId,
  );

  return (
    <div className={`sidebar ${open ? 'sidebar--open' : ''}`} aria-label="会话列表">
      <button type="button" className="sidebar__scrim" aria-label="关闭会话列表" onClick={onClose} />
      <aside className="sidebar__panel">
        <div className="sidebar__head">
          <div className="sidebar__brand">
            <span className="sidebar__brand-dot" aria-hidden="true" />
            <span>对话</span>
          </div>
          <button type="button" className="icon-button sidebar__close" onClick={onClose} aria-label="关闭">
            <CloseIcon />
          </button>
        </div>

        <button type="button" className="sidebar__new" onClick={onCreate} data-testid="new-session">
          <PlusIcon /> 新建对话
        </button>

        <nav className="sidebar__list" aria-label="我的会话">
          {visible.length === 0 ? (
            <p className="sidebar__empty">还没有会话</p>
          ) : (
            visible.map((session) => {
              const active = session.session_id === currentId;
              const editing = editingId === session.session_id;
              const heading = describeSession(session);
              const renamable = canRenameSession(session);
              return (
                <div
                  key={session.session_id}
                  className={`session ${active ? 'session--active' : ''}`}
                  data-session-id={session.session_id}
                  data-kind={heading.kind}
                >
                  {editing ? (
                    <form
                      className="session__edit"
                      onSubmit={(event) => {
                        event.preventDefault();
                        void commit();
                      }}
                    >
                      <input
                        ref={inputRef}
                        value={draft}
                        maxLength={60}
                        aria-label="会话名称"
                        onChange={(event) => setDraft(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === 'Escape') setEditingId(null);
                        }}
                      />
                      <button type="submit" className="button button--small button--primary">
                        保存
                      </button>
                      <button
                        type="button"
                        className="button button--small"
                        onClick={() => setEditingId(null)}
                      >
                        取消
                      </button>
                    </form>
                  ) : (
                    <>
                      <button
                        type="button"
                        className="session__main"
                        onClick={() => {
                          onSelect(session.session_id);
                          onClose();
                        }}
                        aria-current={active ? 'true' : undefined}
                        title={`${heading.title} · ${heading.subtitle}`}
                      >
                        <span className="session__heading">
                          <span className="session__title" title={heading.title}>
                            {heading.title}
                          </span>
                          <span className="session__channel" data-kind={heading.kind}>
                            {heading.badge}
                          </span>
                        </span>
                        {/* 副标题是完整会话键：CSS 省略号截断，悬停/读屏拿全量。 */}
                        <span className="session__subtitle" title={heading.subtitle}>
                          {heading.subtitle}
                        </span>
                        <span className="session__meta">
                          {session.message_count} 条 · {formatSessionTime(session.updated_at)}
                        </span>
                      </button>
                      {renamable ? (
                        <button
                          type="button"
                          className="session__edit-button icon-button"
                          aria-label={`改名：${heading.title}`}
                          title="改名"
                          onClick={() => startEdit(session)}
                        >
                          <PencilIcon />
                        </button>
                      ) : null}
                    </>
                  )}
                </div>
              );
            })
          )}
        </nav>

        <div className="sidebar__footer">
          <div className="sidebar__user">
            <span className="sidebar__user-name">{user.display_name || user.username}</span>
            <span className="sidebar__user-role">{user.role}</span>
          </div>
          <div className="sidebar__nav">
            <button type="button" className="sidebar__nav-item" onClick={onNavigateSettings}>
              <SettingsIcon /> 设置
            </button>
            <button type="button" className="sidebar__nav-item" onClick={onNavigatePersona}>
              <PersonaIcon /> 角色
            </button>
            <button type="button" className="sidebar__nav-item" onClick={onLogout}>
              <LogoutIcon /> 退出
            </button>
          </div>
        </div>
      </aside>
    </div>
  );
}
