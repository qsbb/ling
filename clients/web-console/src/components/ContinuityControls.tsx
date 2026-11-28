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
          <form className="life-controls__edit-form" onSubmit={(event) => {
            event.preventDefault();
            onSave(item.state === 'disabled' ? 'disabled' : 'active', text.trim());
            setEditing(false);
          }}>
            <input className="life-controls__input" aria-label="修改内容" value={text} maxLength={160} required disabled={busy}
              onChange={(event) => setText(event.target.value)} />
            <button className="button button--small button--primary" type="submit" disabled={busy || text.trim().length < 2}>保存</button>
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

export function ContinuityControls({
  onChanged,
  syncVersion = 0,
}: {
  onChanged: () => Promise<void>;
  syncVersion?: number;
}) {
  const [data, setData] = useState<RelationshipPreferences | null>(null);
  const [text, setText] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const reload = () => api.relationshipPreferences().then(setData);
  useEffect(() => {
    void reload().catch((cause) => setError(explainError(cause).reason));
  }, [syncVersion]);

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
    <section className="life-controls" aria-label="联系与表达设置">
      <div className="life-controls__head">
        <h3>联系与表达</h3>
        <span className="life-controls__rule">原有定时提醒按各自规则执行，不受主动分享开关影响。</span>
      </div>
      {error ? <p className="life-controls__error" role="alert">{error}</p> : null}
      {data ? (
        <>
          <div className="life-controls__pause">
            <button
              type="button"
              className={`button button--small${data.share_pause.paused ? ' button--primary' : ''}`}
              disabled={busy}
              onClick={() => void run(() => api.setSharePause(!data.share_pause.paused))}
            >
              {data.share_pause.paused ? '恢复主动分享' : '暂停主动分享'}
            </button>
            <span className="life-controls__status">
              {data.share_pause.paused
                ? `已暂停主动分享${data.share_pause.until ? `（至 ${formatSessionTime(data.share_pause.until)}）` : ''}`
                : '当前允许按节奏主动分享'}
            </span>
          </div>
          <form className="life-controls__form" onSubmit={(event) => {
            event.preventDefault();
            const value = text.trim();
            if (value.length < 2) return;
            void run(async () => { await api.addTopicBoundary(value); setText(''); });
          }}>
            <label className="life-controls__field">
              <span>不希望提起的话题</span>
              <input className="life-controls__input" value={text} maxLength={160} placeholder="例如：最近别提某段旧事" disabled={busy}
                onChange={(event) => setText(event.target.value)} />
            </label>
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
