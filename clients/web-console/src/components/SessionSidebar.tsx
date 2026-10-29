import { useEffect, useRef, useState } from 'react';
import { formatSessionTime, truncate } from '../format';
import type { SessionRecord, UserInfo } from '../types';
import {
  CloseIcon,
  LogoutIcon,
  PencilIcon,
  PersonaIcon,
  PlusIcon,
  SettingsIcon,
} from './Icons';

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
    setDraft(session.title);
  };

  const commit = async () => {
    if (!editingId) return;
    const id = editingId;
    await onRename(id, draft);
    setEditingId(null);
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
          {sessions.length === 0 ? (
            <p className="sidebar__empty">还没有会话</p>
          ) : (
            sessions.map((session) => {
              const active = session.session_id === currentId;
              const editing = editingId === session.session_id;
              return (
                <div
                  key={session.session_id}
                  className={`session ${active ? 'session--active' : ''}`}
                  data-session-id={session.session_id}
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
                      >
                        <span className="session__title">{truncate(session.title, 40)}</span>
                        <span className="session__meta">
                          {session.message_count} 条 · {formatSessionTime(session.updated_at)}
                        </span>
                      </button>
                      <button
                        type="button"
                        className="session__edit-button icon-button"
                        aria-label={`改名：${session.title}`}
                        title="改名"
                        onClick={() => startEdit(session)}
                      >
                        <PencilIcon />
                      </button>
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
