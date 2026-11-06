/**
 * 人设页（`/persona`）：两个 tab。
 *
 * - **角色设定**（①身份与人格）：显示名 / 系统提示 / 性格特征 / 叙事边界 + 时间线。
 *   这是本页原本的内容，一字未改。
 * - **对话与主动性**（⑤）：原本在 `/settings/dialogue`。Step 4 按用户拍板
 *   （设置只放程序与运维）把它迁回了这里——改「要不要开口」是行为调参，
 *   不是运维。表单本体仍是 `DialoguePage`（PolicyForm），只换了宿主。
 *
 * tab 是 URL 而不是组件状态（`?tab=dialogue`）：`/settings/dialogue` 的旧书签
 * 由壳层 `replace` 到这里，刷新/分享/后退都必须停在同一个 tab（DESIGN §1）。
 *
 * 两个 tab **各自加载自己的数据**：角色设定读 `/v1/persona` + 时间线，对话与主动性
 * 读 `/v1/settings/config`。把它们串成一串会让「角色设定读失败」连坐掉调参表单，
 * 而这两件事本来互不依赖。
 */
import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import { formatSessionTime } from '../format';
import { navigate, useLocationHref } from '../route';
import { personaTabFromHref, personaTabHref, type PersonaTab } from '../tabs';
import type { Persona, PersonaScenario, TimelineEvent } from '../types';
import { DialoguePage } from './DialoguePage';
import { AlertIcon, ArrowLeftIcon, RefreshIcon } from './Icons';
import { PageTabPanel, PageTabs, type PageTabSpec } from './PageTabs';

/** 与服务端 `domains/persona/scenarios.py` 的 MAX_SCENARIO_CHARS 对齐。 */
const MAX_SCENARIO_CHARS = 600;

/**
 * 把编辑中的草稿收成 PUT 的 payload。
 *
 * **空串不发送**：接口是整组替换语义，缺键就是"没有覆盖"，于是"清空"
 * 和"恢复默认"是同一条路径。发送空串反而是另一种意思（服务端也会把它
 * 丢掉），两种写法在日志里看起来一样、行为靠实现细节撑着，不值得。
 */
export function scenarioPayload(drafts: Record<string, string>): Record<string, string> {
  const payload: Record<string, string> = {};
  for (const [key, value] of Object.entries(drafts)) {
    const text = value.trim();
    if (text) payload[key] = text;
  }
  return payload;
}

/** 服务端给的覆盖 → 编辑草稿。默认值不进草稿：它由占位提示展示。 */
export function scenarioDraftsFrom(persona: Persona): Record<string, string> {
  return { ...(persona.scenario_prompts ?? {}) };
}

interface ScenarioEditorProps {
  scenarios: PersonaScenario[];
  drafts: Record<string, string>;
  onChange: (key: string, value: string) => void;
}

/**
 * 「场景处理」区：同一份人设，在不同场合该怎么处理。
 *
 * 空着的输入框不是"没写"，而是"用默认"——所以占位提示直接显示默认文本，
 * 而不是放一句"请输入"。想看清默认到底写了什么，不应该需要先删掉自己的话。
 */
export function ScenarioEditor({ scenarios, drafts, onChange }: ScenarioEditorProps) {
  const customCount = scenarios.filter(
    (s) => (drafts[s.key] ?? '').trim().length > 0,
  ).length;

  return (
    <div className="scenario-block">
      <div className="scenario-block__head">
        <h3>场景处理</h3>
        <span className="panel__count">
          {customCount > 0 ? `${customCount} 条自己写的` : '全部用默认'}
        </span>
      </div>
      <p className="panel__note">
        不同场合她该怎么做，会在这个场合真的出现时注入提示词。空着就用默认（灰色字），
        写了自己的话按你写的来。
      </p>
      <div className="scenarios">
        {scenarios.map((scenario) => {
          const draft = drafts[scenario.key] ?? '';
          const custom = draft.trim().length > 0;
          return (
            <div className="scenario" key={scenario.key}>
              <div className="scenario__head">
                <span className="scenario__label">{scenario.label}</span>
                <span
                  className={`scenario__state${custom ? ' scenario__state--custom' : ''}`}
                >
                  {custom ? '自己写的' : '默认'}
                </span>
                {custom ? (
                  <button
                    type="button"
                    className="button button--small scenario__reset"
                    onClick={() => onChange(scenario.key, '')}
                    data-testid={`scenario-reset-${scenario.key}`}
                  >
                    恢复默认
                  </button>
                ) : null}
              </div>
              <p className="scenario__when">{scenario.when}</p>
              <textarea
                className="field__input field__textarea scenario__input"
                rows={3}
                maxLength={MAX_SCENARIO_CHARS}
                aria-label={`${scenario.label}的处理方式`}
                placeholder={scenario.default_text}
                value={draft}
                onChange={(event) => onChange(scenario.key, event.target.value)}
                data-testid={`scenario-input-${scenario.key}`}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}

interface PersonaPageProps {
  /** 离开本页（回对话）。 */
  onBack: () => void;
}

export const PERSONA_TABS: readonly PageTabSpec<PersonaTab>[] = [
  { key: 'persona', label: '角色设定', testid: 'persona-tab-profile' },
  { key: 'dialogue', label: '对话与主动性', testid: 'persona-tab-dialogue' },
];

export function PersonaPage({ onBack }: PersonaPageProps) {
  const href = useLocationHref();
  const tab = personaTabFromHref(href);
  const openTab = (next: PersonaTab) => navigate(personaTabHref(next));

  return (
    <section className="page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回对话
      </button>

      <h1 className="page__title">人设</h1>
      <p className="page__hint">
        她是谁、怎么说话、什么时候开口。记忆内容在
        <button type="button" className="link-button" onClick={() => navigate('/memory')}>
          记忆页
        </button>
        。
      </p>

      <PageTabs label="人设页分区" tabs={PERSONA_TABS} current={tab} onSelect={openTab} />

      <PageTabPanel tab="persona" current={tab}>
        <PersonaSettings />
      </PageTabPanel>
      <PageTabPanel tab="dialogue" current={tab}>
        <DialoguePage onBack={() => openTab('persona')} backLabel="返回角色设定" />
      </PageTabPanel>
    </section>
  );
}

/**
 * ① 身份与人格的本体。原来是整个 `PersonaPage`，现在只负责自己那一块：
 * 外壳（返回 / 标题 / tab 条）由上面的 `PersonaPage` 提供，这里不再自带
 * `.page`，否则 tab 面板里会再缩进一次并多出一层内层滚动条。
 */
function PersonaSettings() {
  const [persona, setPersona] = useState<Persona | null>(null);
  const [displayName, setDisplayName] = useState('');
  const [systemPrompt, setSystemPrompt] = useState('');
  const [traitsText, setTraitsText] = useState('{}');
  const [boundariesText, setBoundariesText] = useState('');
  // 场景覆盖的编辑草稿：只有"自己写过"的键会出现在这里，默认值靠占位提示展示。
  const [scenarioDrafts, setScenarioDrafts] = useState<Record<string, string>>({});
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
      setScenarioDrafts(scenarioDraftsFrom(current));
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
        scenario_prompts: scenarioPayload(scenarioDrafts),
      });
      setPersona(updated);
      setScenarioDrafts(scenarioDraftsFrom(updated));
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
      <div className="page__loading">
        <span className="spinner" aria-hidden="true" /> 正在读取角色设定…
      </div>
    );
  }

  if (loadError) {
    return (
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
    );
  }

  return (
    <div className="domain-body">
      <div className="page__head">
        <div>
          <h2 className="page__title">角色设定</h2>
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

        {persona ? (
          <ScenarioEditor
            scenarios={persona.scenarios}
            drafts={scenarioDrafts}
            onChange={(key, value) =>
              setScenarioDrafts((prev) => ({ ...prev, [key]: value }))
            }
          />
        ) : null}

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
