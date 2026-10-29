import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import { formatSessionTime } from '../format';
import type { Persona, TimelineEvent } from '../types';
import { AlertIcon, ArrowLeftIcon, RefreshIcon } from './Icons';

interface PersonaPageProps {
  onBack: () => void;
}

export function PersonaPage({ onBack }: PersonaPageProps) {
  const [persona, setPersona] = useState<Persona | null>(null);
  const [displayName, setDisplayName] = useState('');
  const [systemPrompt, setSystemPrompt] = useState('');
  const [traitsText, setTraitsText] = useState('{}');
  const [boundariesText, setBoundariesText] = useState('');
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<ExplainedError | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [current, timeline] = await Promise.all([api.persona(), api.timeline(50)]);
      setPersona(current);
      setDisplayName(current.display_name);
      setSystemPrompt(current.system_prompt);
      setTraitsText(JSON.stringify(current.traits ?? {}, null, 2));
      setBoundariesText(current.boundaries.join('\n'));
      setEvents(timeline.events);
    } catch (error) {
      setLoadError(explainError(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (saving) return;
    setFormError(null);
    setMessage(null);
    let traits: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(traitsText || '{}');
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setFormError('traits 必须是 JSON 对象，例如 {"tone": "温和"}');
        return;
      }
      traits = parsed as Record<string, unknown>;
    } catch {
      setFormError('traits 不是合法 JSON');
      return;
    }
    const boundaries = boundariesText
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);
    setSaving(true);
    try {
      const updated = await api.updatePersona({
        display_name: displayName.trim(),
        system_prompt: systemPrompt,
        traits,
        boundaries,
      });
      setPersona(updated);
      setMessage(`已保存为修订 #${updated.revision}，后续轮次立即生效`);
      const timeline = await api.timeline(50);
      setEvents(timeline.events);
    } catch (error) {
      setFormError(explainError(error).reason);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="page">
        <div className="page__loading">
          <span className="spinner" aria-hidden="true" /> 正在读取角色设定…
        </div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="page">
        <button type="button" className="back-link" onClick={onBack}>
          <ArrowLeftIcon /> 返回对话
        </button>
        <div className="load-error" role="alert">
          <div className="load-error__head">
            <AlertIcon />
            <strong>{loadError.reason}</strong>
          </div>
          {loadError.hint ? <p>{loadError.hint}</p> : null}
          <button type="button" className="button button--small" onClick={() => void load()}>
            重新读取
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="page">
      <div className="page__head">
        <div>
          <button type="button" className="back-link" onClick={onBack}>
            <ArrowLeftIcon /> 返回对话
          </button>
          <h1 className="page__title">角色设定</h1>
          <p className="page__subtitle">
            {persona ? `persona_id ${persona.persona_id} · 当前修订 #${persona.revision}` : ''}
            。修改会写入新的 personas 修订，并立即影响后续轮次。
          </p>
        </div>
        <button type="button" className="button button--small" onClick={() => void load()}>
          <RefreshIcon /> 刷新
        </button>
      </div>

      {message ? (
        <div className="banner banner--ok" role="status">
          {message}
        </div>
      ) : null}

      <form className="panel" onSubmit={(event) => void save(event)}>
        <div className="panel__head">
          <h2>设定</h2>
          <span className="panel__count">服务端权威</span>
        </div>
        <label className="field">
          <span className="field__label">显示名</span>
          <input
            className="field__input"
            value={displayName}
            maxLength={64}
            onChange={(event) => setDisplayName(event.target.value)}
          />
        </label>
        <label className="field">
          <span className="field__label">系统提示（人格设定）</span>
          <textarea
            className="field__input field__textarea"
            rows={8}
            maxLength={8000}
            value={systemPrompt}
            onChange={(event) => setSystemPrompt(event.target.value)}
          />
        </label>
        <label className="field">
          <span className="field__label">性格特征（JSON 对象）</span>
          <textarea
            className="field__input field__textarea field__mono"
            rows={5}
            value={traitsText}
            onChange={(event) => setTraitsText(event.target.value)}
          />
        </label>
        <label className="field">
          <span className="field__label">叙事边界（每行一条）</span>
          <textarea
            className="field__input field__textarea"
            rows={4}
            value={boundariesText}
            onChange={(event) => setBoundariesText(event.target.value)}
          />
        </label>

        {formError ? (
          <div className="banner banner--error" role="alert">
            {formError}
          </div>
        ) : null}

        <div className="panel__actions">
          <button type="submit" className="button button--primary" disabled={saving}>
            {saving ? '保存中…' : '保存设定'}
          </button>
          <span className="panel__hint">保存会新建 persona 修订，后续轮次立即生效</span>
        </div>
      </form>

      <section className="panel">
        <div className="panel__head">
          <h2>角色时间线</h2>
          <span className="panel__count">最近 {events.length} 条</span>
        </div>
        <p className="panel__note">
          时间线是服务端权威事件序列（含来源与可见范围），不是浏览器拼出来的记录。
        </p>
        {events.length === 0 ? (
          <p className="panel__note">还没有事件。</p>
        ) : (
          <ul className="timeline">
            {events.map((event) => (
              <li className="timeline__item" key={event.id}>
                <div className="timeline__main">
                  <span className="timeline__type">{event.type}</span>
                  <span className="timeline__summary">{event.summary}</span>
                </div>
                <div className="timeline__meta">
                  <span>rev {event.revision}</span>
                  <span>{event.origin}</span>
                  <span>{event.visibility}</span>
                  <span>{formatSessionTime(event.occurred_at)}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
