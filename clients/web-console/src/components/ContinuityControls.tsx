import { useEffect, useState } from 'react';
import { api } from '../api';
import { explainError } from '../errors';
import { formatSessionTime } from '../format';
import type { RelationshipPreference, RelationshipPreferences } from '../types';

function PreferenceRow({
  item,
  busy,
  onSave,
}: {
  item: RelationshipPreference;
  busy: boolean;
  onSave: (state: 'active' | 'disabled', text?: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(item.text);
  return (
    <li className="life-list__row">
      <div className="life-list__main">
        <span>{item.text}</span>
        <small> · {item.kind === 'boundary' ? '话题边界' : '表达习惯'}</small>
        <small> · {item.scope === 'private' ? '私聊' : '来源群聊'}</small>
        {editing ? (
          <form onSubmit={(event) => {
            event.preventDefault();
            onSave(item.state === 'disabled' ? 'disabled' : 'active', text.trim());
            setEditing(false);
          }}>
            <input aria-label="修改内容" value={text} maxLength={160} required disabled={busy}
              onChange={(event) => setText(event.target.value)} />
            <button className="button button--small" type="submit" disabled={busy || text.trim().length < 2}>保存</button>
          </form>
        ) : null}
      </div>
      <div className="life-list__meta">
        {item.state === 'pending' ? <span>待审核 </span> : null}
        <button type="button" className="button button--small" disabled={busy}
          onClick={() => { setText(item.text); setEditing(!editing); }}>{editing ? '取消' : '修改'}</button>
        <button type="button" className="button button--small" disabled={busy}
          onClick={() => onSave(item.state === 'active' ? 'disabled' : 'active')}>
          {item.state === 'active' ? '停用' : item.state === 'pending' ? '审核通过' : '启用'}
        </button>
        {item.state === 'pending' ? (
          <button type="button" className="button button--small" disabled={busy}
            onClick={() => onSave('disabled')}>停用</button>
        ) : null}
      </div>
    </li>
  );
}

export function ContinuityControls({ onChanged }: { onChanged: () => Promise<void> }) {
  const [data, setData] = useState<RelationshipPreferences | null>(null);
  const [text, setText] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const reload = () => api.relationshipPreferences().then(setData);
  useEffect(() => {
    void reload().catch((cause) => setError(explainError(cause).reason));
  }, []);

  const run = async (write: () => Promise<unknown>) => {
    setBusy(true);
    setError('');
    try {
      await write();
      await Promise.all([reload(), onChanged()]);
    } catch (cause) {
      setError(explainError(cause).reason);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="联系与表达设置">
      <h3>联系与表达</h3>
      {error ? <p role="alert">{error}</p> : null}
      {data ? (
        <>
          <button type="button" className="button button--small" disabled={busy}
            onClick={() => void run(() => api.setSharePause(!data.share_pause.paused))}>
            {data.share_pause.paused ? '恢复主动分享' : '暂停主动分享'}
          </button>
          {data.share_pause.paused && data.share_pause.until ? (
            <small> 暂停至 {formatSessionTime(data.share_pause.until)}</small>
          ) : null}
          <form onSubmit={(event) => {
            event.preventDefault();
            const value = text.trim();
            if (value.length < 2) return;
            void run(async () => { await api.addTopicBoundary(value); setText(''); });
          }}>
            <label>不希望提起的话题 <input value={text} maxLength={160} disabled={busy}
              onChange={(event) => setText(event.target.value)} /></label>
            <button type="submit" className="button button--small" disabled={busy || text.trim().length < 2}>添加边界</button>
          </form>
          {data.preferences.length ? (
            <ul className="life-list">
              {data.preferences.map((item) => (
                <PreferenceRow key={item.id} item={item} busy={busy}
                  onSave={(state, value) => void run(() => api.editRelationshipPreference(item.id, state, value))} />
              ))}
            </ul>
          ) : null}
        </>
      ) : error ? (
        <button type="button" className="button button--small" disabled={busy} onClick={() => void run(reload)}>重试</button>
      ) : <p className="page__hint">读取设置中…</p>}
    </section>
  );
}
