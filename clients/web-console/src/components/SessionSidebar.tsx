import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { formatSessionTime } from '../format';
import type { SessionRecord, UserInfo } from '../types';
import {
  CloseIcon,
  LogoutIcon,
  PencilIcon,
  PersonaIcon,
  PinIcon,
  PlusIcon,
  SettingsIcon,
} from './Icons';
import './session-sidebar.css';

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
  Partial<
    Pick<SessionRecord, 'display_name' | 'channel' | 'peer_id' | 'contact_unavailable'>
  >;

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
  const channel = (session.channel ?? '').trim().toLowerCase();
  const ref =
    channel === 'web'
      ? null
      : parseChannelRef(rawTitle) ?? parseChannelRef(session.window_id) ?? parseChannelRef(session.subject_id);
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
 * 这条会话是否显示「允许晚点回」。
 *
 * 服务端只对 OneBot 私聊实现这个设置（群聊 / 网页 / 官方 QQ 机器人一律 422），
 * 所以界面同样只在那里出现——不让用户点一个注定被拒的开关。
 *
 * **判据是契约字段，不是窗口键的形状。** `SessionRecord.channel` 是协议
 * （`web` / `onebot` / `qqbot`），不是实例命名空间：`LING_ONEBOT_INSTANCE_ID`
 * 可以随便起名，窗口键可能是 `fixture-bridge:private:100200303`、
 * `default:private:…`，甚至不带前缀。按 `onebot-` 这种前缀去猜，自定义
 * 命名空间的私聊会**静默不显示**开关——服务端明明 200 可用。
 *
 * 所以这里只问两件契约上稳定的事：
 * 1. 会话类型是不是私聊（`describeSession` 从窗口键或 `peer_id` 得出）；
 * 2. 渠道是不是 `onebot`（群聊由类型排除，官方 QQ 机器人的渠道是 `qqbot`）。
 *
 * 旧服务端没有 `channel` 字段时无法确认渠道，按不显示处理——宁可少显示，
 * 也不要在 qqbot 会话上摆一个必然 422 的控件。
 */
export function supportsOneBotDeferred(session: SessionLike): boolean {
  if (describeSession(session).kind !== 'private') return false;
  return (session.channel ?? '').trim().toLowerCase() === 'onebot';
}

export function canManageSessionOrder(role: string): boolean {
  return role === 'owner' || role === 'admin';
}

export function canRenameSession(session: SessionLike, role?: string): boolean {
  return describeSession(session).kind === 'web' || canManageSessionOrder(role ?? '');
}

/**
 * 列表里"一行能读到什么"：
 *
 * 名称 → 群/私聊 → 选中 → 近期活动。操作（改名/置顶/排序/外部 ID）不进主行，
 * 收进每条的「更多」菜单，避免图七那种"每条常驻四个图标"把可读信息挤走。
 */
export interface SessionRow {
  session: SessionRecord;
  heading: SessionHeading;
  active: boolean;
  renamable: boolean;
  /** 平台侧联系已不在（群被退/被踢、好友已删）。整行置灰但仍可点开。 */
  unavailable: boolean;
  /** 徽标位置要显示的短标签；正常会话是「群」「私聊」，不可用时换成状态词。 */
  badge: string;
  /** 同一分组（置顶 / 非置顶）里的上下邻居，用于排序项是否可用。 */
  previous: SessionRecord | null;
  next: SessionRecord | null;
}

/**
 * 「不在」的三种原因 → 徽标上的短词。
 *
 * 为什么换掉徽标而不是只在悬停里说：一条灰掉的会话如果不写明原因，
 * 用户的第一反应永远是「我怎么不在了」。徽标位本来就是「这条是什么」，
 * 而「曾经是群、现在不在了」比单纯一个「群」更该占这个位置。
 *
 * 三个词都是**两个汉字**，与「群」「私聊」占同样宽度——置灰是排版手段，
 * 不该让列表的列宽跟着跳。
 */
const UNAVAILABLE_LABEL: Record<string, string> = {
  left: '已退群',
  kicked: '被移出',
  friend_deleted: '已删友',
};

export function unavailableLabel(session: SessionLike): string | null {
  const reason = session.contact_unavailable;
  if (!reason) return null;
  // 未知取值（新服务端加了第四种原因、这里还没更新）也要置灰：
  // 「我们收到了一个不可用状态」比「认得出是哪种」更重要。
  return UNAVAILABLE_LABEL[reason] ?? '不在';
}

/**
 * 分组 + 组内顺序。
 *
 * **置顶的排到前面**，组内保持后端给的顺序（= 最近活跃倒序）。
 *
 * 为什么不在这里再按 `updated_at` 排一次：后端 `sessions.py` 已经按
 * `pinned_at DESC, 最近活跃 DESC` 给过顺序了。前端重排一遍就有了两套
 * 排序规则，哪天两边不一致就是"列表看着乱但说不清哪不对"。
 *
 * 空气泡（新建后没说话）不占位置——除当前这条正在看以外。
 */
export function buildSessionRows(
  sessions: SessionRecord[],
  currentId: string | null,
  role: string,
): SessionRow[] {
  const visible = sessions.filter(
    (session) => session.message_count > 0 || session.session_id === currentId,
  );
  const ordered = [
    ...visible.filter((session) => session.pinned),
    ...visible.filter((session) => !session.pinned),
  ];
  const canReorder = canManageSessionOrder(role);
  return ordered.map((session, index) => {
    const previous = ordered[index - 1] ?? null;
    const next = ordered[index + 1] ?? null;
    const sameGroup = (neighbor: SessionRecord | null) =>
      neighbor !== null && Boolean(neighbor.pinned) === Boolean(session.pinned);
    const unavailable = unavailableLabel(session);
    return {
      session,
      heading: describeSession(session),
      active: session.session_id === currentId,
      renamable: canRenameSession(session, role),
      unavailable: unavailable !== null,
      badge: unavailable ?? describeSession(session).badge,
      previous: canReorder && sameGroup(previous) ? previous : null,
      next: canReorder && sameGroup(next) ? next : null,
    };
  });
}

interface SessionSidebarProps {
  sessions: SessionRecord[];
  currentId: string | null;
  open: boolean;
  user: UserInfo;
  onClose: () => void;
  onSelect: (sessionId: string) => void;
  onCreate: () => void;
  onRename: (sessionId: string, title: string) => Promise<boolean>;
  onPin: (sessionId: string, pinned: boolean) => Promise<void>;
  onMove: (sessionId: string, anchorId: string, before: boolean) => Promise<void>;
  onNavigateSettings: () => void;
  onNavigatePersona: () => void;
  onLogout: () => void;
}

/** 菜单在同一时刻只能开一个：开着 A 点 B 是"换到 B"，不是两个都开。 */
function useExclusiveMenu() {
  const [openId, setOpenIdState] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  /**
   * `close` 必须是**稳定引用**：它被写进「换会话就收起」那个 effect 的依赖里，
   * 每次渲染换一个新函数会让 effect 每帧重跑，把用户刚展开的菜单立刻收掉。
   */
  const close = useCallback(() => setOpenIdState(null), []);
  const setOpenId = setOpenIdState;

  useEffect(() => {
    if (openId === null) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (rootRef.current?.contains(target)) return;
      setOpenId(null);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpenId(null);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [openId]);

  return {
    openId,
    rootRef,
    toggle: (id: string) => {
      setOpenId((current) => (current === id ? null : id) as string | null);
    },
    close,
  };
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
  onPin,
  onMove,
  onNavigateSettings,
  onNavigatePersona,
  onLogout,
}: SessionSidebarProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [orderBusy, setOrderBusy] = useState(false);
  const [savingName, setSavingName] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const menu = useExclusiveMenu();
  const rows = useMemo(
    () => buildSessionRows(sessions, currentId, user.role),
    [sessions, currentId, user.role],
  );

  useEffect(() => {
    if (editingId) inputRef.current?.focus();
  }, [editingId]);

  // 换会话 / 收起侧栏时把菜单与"按需展开的外部 ID"一起收掉：
  // 留着它们等于让上一条会话的操作挂在新会话上。
  useEffect(() => {
    menu.close();
    setDetailId(null);
  }, [currentId, menu.close]);

  const startEdit = (session: SessionRecord) => {
    setEditingId(session.session_id);
    // 改的是给人看的名字，不是机器键。
    setDraft(describeSession(session).title);
    menu.close();
  };

  const commit = async () => {
    const session = sessions.find((item) => item.session_id === editingId);
    if (!session || savingName || (describeSession(session).kind === 'web' && !draft.trim())) return;
    setSavingName(true);
    try {
      if (await onRename(session.session_id, draft)) {
        setEditingId((current) => (current === session.session_id ? null : current));
      }
    } finally {
      setSavingName(false);
    }
  };

  const changeOrder = async (action: () => Promise<void>) => {
    if (orderBusy) return;
    setOrderBusy(true);
    try {
      await action();
    } finally {
      setOrderBusy(false);
      menu.close();
    }
  };

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
          {rows.length === 0 ? (
            <p className="sidebar__empty">还没有会话</p>
          ) : (
            rows.map(({ session, heading, active, renamable, unavailable, badge, previous, next }) => {
              const editing = editingId === session.session_id;
              const menuOpen = menu.openId === session.session_id;
              const detailOpen = detailId === session.session_id;
              return (
                <div
                  key={session.session_id}
                  ref={menuOpen ? menu.rootRef : undefined}
                  className={`session ${active ? 'session--active' : ''} ${
                    unavailable ? 'session--unavailable' : ''
                  } ${menuOpen ? 'session--menu-open' : ''}`}
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
                        placeholder={heading.kind === 'web' ? undefined : '使用渠道名称'}
                        disabled={savingName}
                        onChange={(event) => setDraft(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === 'Escape') setEditingId(null);
                        }}
                      />
                      <button
                        type="submit"
                        className="button button--small button--primary"
                        disabled={savingName || (heading.kind === 'web' && !draft.trim())}
                      >
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
                        // 悬停只给名字：外部键（窗口键/会话 id）会出现在
                        // 「会话信息」里，不跟着主行的提示浮出来。
                        title={heading.title}
                      >
                        <span className="session__heading">
                          <span className="session__title" title={heading.title}>
                            {heading.title}
                          </span>
                          {session.pinned ? (
                            <span className="session__pin-flag" title="已置顶">
                              <PinIcon filled />
                            </span>
                          ) : null}
                          <span
                            className="session__channel"
                            // 置灰的会话把徽标换成状态词，样式也跟着换成"不在"，
                            // 免得「已退群」还带着群聊那块暖色底看起来像在用的群。
                            data-kind={unavailable ? 'unavailable' : heading.kind}
                          >
                            {badge}
                          </span>
                        </span>
                        {/* 主行只读"名称 + 群/私聊 + 近期活动"。外部键按需展开，
                            不占默认阅读宽度。 */}
                        <span className="session__meta">
                          <span className="session__time">{formatSessionTime(session.updated_at)}</span>
                          <span className="session__count">{session.message_count} 条</span>
                        </span>
                      </button>
                      <button
                        type="button"
                        className="session__more icon-button"
                        aria-label={`更多操作：${heading.title}`}
                        aria-haspopup="menu"
                        aria-expanded={menuOpen}
                        title="更多操作"
                        onClick={() => menu.toggle(session.session_id)}
                      >
                        <span aria-hidden="true">⋯</span>
                      </button>
                      {menuOpen ? (
                        <div className="session__menu" role="menu" data-testid="session-menu">
                          {renamable ? (
                            <button
                              type="button"
                              role="menuitem"
                              className="session__menu-item"
                              aria-label={`改名：${heading.title}`}
                              onClick={() => startEdit(session)}
                            >
                              <PencilIcon /> 改名
                            </button>
                          ) : null}
                          {renamable ? (
                            <button
                              type="button"
                              role="menuitem"
                              className={`session__menu-item ${session.pinned ? 'is-pinned' : ''}`}
                              aria-label={
                                session.pinned ? `取消置顶：${heading.title}` : `置顶：${heading.title}`
                              }
                              aria-pressed={Boolean(session.pinned)}
                              data-testid="session-pin"
                              disabled={orderBusy}
                              onClick={() => void changeOrder(() => onPin(session.session_id, !session.pinned))}
                            >
                              <PinIcon filled={Boolean(session.pinned)} />
                              {session.pinned ? '取消置顶' : '置顶'}
                            </button>
                          ) : null}
                          {previous ? (
                            <button
                              type="button"
                              role="menuitem"
                              className="session__menu-item"
                              aria-label={`上移：${heading.title}`}
                              disabled={orderBusy}
                              onClick={() => {
                                void changeOrder(() => onMove(session.session_id, previous.session_id, true));
                              }}
                            >
                              ↑ 上移
                            </button>
                          ) : null}
                          {next ? (
                            <button
                              type="button"
                              role="menuitem"
                              className="session__menu-item"
                              aria-label={`下移：${heading.title}`}
                              disabled={orderBusy}
                              onClick={() => {
                                void changeOrder(() => onMove(session.session_id, next.session_id, false));
                              }}
                            >
                              ↓ 下移
                            </button>
                          ) : null}
                          <button
                            type="button"
                            role="menuitem"
                            className="session__menu-item"
                            aria-expanded={detailOpen}
                            aria-label={`会话信息：${heading.title}`}
                            onClick={() => setDetailId(detailOpen ? null : session.session_id)}
                          >
                            会话信息
                          </button>
                        </div>
                      ) : null}
                    </>
                  )}
                  {detailOpen && !editing ? (
                    <dl className="session__detail" data-testid="session-detail">
                      <div>
                        <dt>会话键</dt>
                        <dd className="session__subtitle" title={heading.subtitle}>
                          {heading.subtitle}
                        </dd>
                      </div>
                      {session.group_reply_max_parts != null ? (
                        <div>
                          <dt>每轮最多</dt>
                          <dd>{session.group_reply_max_parts} 条</dd>
                        </div>
                      ) : null}
                      <div>
                        <dt>建立于</dt>
                        <dd>{formatSessionTime(session.created_at)}</dd>
                      </div>
                      <div>
                        <dt>最近活动</dt>
                        <dd>{formatSessionTime(session.updated_at)}</dd>
                      </div>
                    </dl>
                  ) : null}
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
