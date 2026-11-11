/**
 * 通用「策略表单」：数字字段 + 草稿 → 发布。
 *
 * 为什么抽出来：④ 记忆与学习 和 ⑧ 生活与活动 的表单结构完全一样。
 * 复制两份的话，上一次踩过的坑（`min=1 step=5` 让默认值 120 非法，
 * 浏览器静默拦截提交、按钮点了没反应）会在其中一份里重演。
 * 步长、校验、severity 处理都收在一处。
 */
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import { voiceIssueFromError } from '../voiceSettings';
import { AlertIcon, ArrowLeftIcon, RefreshIcon } from './Icons';

export interface PolicyField {
  key: string;
  label: string;
  hint: string;
  /**
   * number：普通数字。minutes：存的是"当天第几分钟"，但用时间选择器编辑。
   * 让用户手填 1380 表示 23:00 是不可用的。
   */
  kind?: 'number' | 'minutes' | 'bool';
  /**
   * 'any' 表示不限制步长。**整数用 1，小数一律用 'any'。**
   * HTML 的 step 以 min 为基准，写死步长很容易让默认值自己就非法。
   */
  step?: number | 'any';
  min?: number;
  max?: number;
}

interface PolicyFormProps {
  title: string;
  hint: string;
  fields: PolicyField[];
  /** 从 /v1/settings/config 的哪一段读初值。 */
  section: 'memory' | 'life' | 'dialogue';
  onBack: () => void;
  /** 返回按钮文案。记忆页把它当 tab 用，写「返回设置」会指错地方。 */
  backLabel?: string;
  /**
   * 发布成功后的提示。默认文案声称「下一轮对话起生效」——
   * **只有确认运行时真的读取这一段配置时才能用默认值**。
   * 记忆的 memory 段截至本次实现尚未接线，调用方必须传诚实文案。
   */
  publishedMessage?: string;
  /**
   * 数值字段之外的**面板**（语音设置）。渲染在数值字段之后、动作按钮之前。
   *
   * 为什么在这里开槽而不是让调用方自己画一个并列的 `<form>`：保存是
   * "一次草稿 + 一次发布"，两段内容必须进**同一个**请求。分成两个表单就会出现
   * "改音色 → 保存 → 顺手把刚改的数值也覆盖回去"（或反过来）。
   *
   * 渲染函数拿得到 `issues`（按 `error.field` 收敛出的字段级错误）与 `saving`，
   * 所以面板能把错误标到自己的输入框上，并在保存/上传期间禁用自己。
   */
  sections?: (context: PolicyFormSectionContext) => ReactNode;
  /**
   * 保存前的**拦截**：返回非空字符串就**不发请求**，并把它当提示显示。
   *
   * 与 `sections` 配对使用：面板知道自己缺什么（比如选了 MiniMax 却没填
   * `voice_id`），但只有这里能真的挡住 `saveDraft`。返回 `null` 才继续。
   *
   * **它不复制后端的校验规则**：后端仍有唯一的那份判定
   * （`ports/config.py::voice_requirement_issue()`），这里只拦"发出去也注定被拒"
   * 的显然情况，好让提示在点保存时就出现。
   */
  beforeSave?: () => BeforeSaveBlock | null;
  /**
   * 外部占用（如参考音频上传中）：为 true 时**禁用保存**。
   *
   * 为什么由外面给而不是面板自己禁用按钮：按钮在 `PolicyForm` 里。
   * 上传中保存会存下一份没有 `reference_asset_id` 的音色，而页面随后显示
   * "已上传"——两边不一致，且没有任何报错。
   */
  busy?: boolean;
  /**
   * 本段里**不是数字**的字段（音色的 `voice` 对象、`voice_willingness` 枚举）。
   *
   * 在**保存那一刻**调用，返回值浅合并进同一次 `PUT /v1/settings/draft` 的本段：
   *
   * ```json
   * { "dialogue": { "group_turn_budget": 3, "voice": {...}, "voice_willingness": "natural" } }
   * ```
   *
   * 为什么不直接放进 `fields`：`fields` 的 `Values` 是 `Record<string, number>`，
   * 把对象/枚举硬塞进去，等于让每个数字字段自己记着"我不是数字"。
   *
   * 为什么不另发一个请求：一次保存必须是一个草稿 + 一次发布，否则"改音色但没改
   * 数字"会连带把两段各自存一次，其中一段失败时界面上分不清哪段生效了。
   */
  extraValues?: () => Record<string, unknown>;
  /**
   * 草稿的 `validation_errors`（含非本段的段外提示）交给调用方看一遍。
   *
   * 为什么需要：本段里**不是数字**的字段（音色）由调用方自己维护，而它们的
   * 校验结果只在 `validation_errors` 里出现。`PolicyForm` 只标那些能对上
   * `fields[].key` 的，剩下的必须交给"知道那些字段的人"，否则错误只出现在
   * 横幅里、对不上任何输入框。
   */
  onDraftIssues?: (issues: readonly DraftIssue[]) => void;
  /** 草稿/发布请求**抛错**时的回调：`error.field` 挂在 `ApiError` 上。 */
  onSaveError?: (error: unknown) => void;
}

/** 后端 `ValidationIssue.to_dict()` 的形状。 */
export interface DraftIssue {
  field: string;
  message: string;
  severity?: string;
}

/** `sections` 渲染函数拿到的上下文。 */
export interface PolicyFormSectionContext {
  /** 本页正在保存（草稿或发布）。面板据此禁用输入。 */
  saving: boolean;
  /** 字段级错误，键是 `error.field` 按 `section` 收敛后的短名（如 `voice_id`）。 */
  issues: ReadonlyMap<string, string>;
}

/** `beforeSave` 的返回：`field` 用来把提示标到具体输入框上。 */
export interface BeforeSaveBlock {
  field: string;
  message: string;
}

type Values = Record<string, number>;

/**
 * `error.field` 收敛成表单里的短名。
 *
 * 后端给的是全路径（`dialogue.voice.voice_id`、`memory.relative_score_floor`）。
 * 面板只关心自己那一段，所以去掉 `section.` 前缀；**不认识的前缀原样保留**，
 * 这样"把一条不属于本页的错误标到本页输入框"不会被静默吞掉——它标不上，
 * 但会出现在横幅里。
 */
export function fieldKeyWithinSection(field: string, section: string): string {
  const prefix = `${section}.`;
  return field.startsWith(prefix) ? field.slice(prefix.length) : field;
}

/**
 * 从任意保存失败里取出 `error.field` 与它的文案。
 *
 * 复用 `voiceIssueFromError`（它读的是 `connectorErrorField()`，也就是 `api.ts`
 * 里挂在 `ApiError` 实例上的 `field`），不在这里重写一遍取字段的方式：
 * 两处各写一份 `(error as any).field` 迟早不一致，而不一致的表现是"某条错误
 * 再也不标到输入框上"，没有任何报错。
 */


/** 当天分钟数 → HH:MM（用于 <input type="time">）。 */
function minutesToHhmm(total: number): string {
  const safe = ((Math.round(total) % 1440) + 1440) % 1440;
  const hh = String(Math.floor(safe / 60)).padStart(2, '0');
  const mm = String(safe % 60).padStart(2, '0');
  return `${hh}:${mm}`;
}

/** HH:MM → 当天分钟数。空值按 0 处理，避免变成 NaN 写进配置。 */
function hhmmToMinutes(value: string): number {
  const [hh, mm] = value.split(':');
  const h = Number(hh);
  const m = Number(mm);
  if (!Number.isFinite(h) || !Number.isFinite(m)) return 0;
  return h * 60 + m;
}

export function PolicyForm({
  title,
  hint,
  fields,
  section,
  onBack,
  backLabel = '返回设置',
  publishedMessage = '已发布。新策略会在下一轮对话起生效。',
  sections,
  beforeSave,
  busy = false,
  extraValues,
  onDraftIssues,
  onSaveError,
}: PolicyFormProps) {
  const [values, setValues] = useState<Values | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<ExplainedError | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  /**
   * 字段级错误：`error.field`（或草稿 `validation_errors[].field`）按段收敛后的短名。
   *
   * 为什么单独存而不是塞进 `ExplainedError`：横幅回答"为什么没保存成"，
   * 这一份回答"哪一格错了"。一条错误可以两者都有；多条字段错误就只有一个横幅。
   */
  const [issues, setIssues] = useState<ReadonlyMap<string, string>>(new Map());

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setIssues(new Map());
    try {
      const config = await api.settingsConfig();
      const raw = config[section] as unknown as Values;
      setValues(raw);
    } catch (err) {
      setError(explainError(err));
    } finally {
      setLoading(false);
    }
  }, [section]);

  useEffect(() => {
    void load();
  }, [load]);

  const update = (key: string, raw: string) => {
    setValues((current) => (current ? { ...current, [key]: Number(raw) } : current));
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    // `busy` = 外部占用（上传中）。**必须在这里挡**：按钮禁用只是外观，
    // 回车提交同样会走到这条路径。
    if (!values || saving || busy) return;
    // 面板的保存前拦截：缺 `voice_id` 这类"发出去也注定被拒"的不必绕一圈 422。
    const blocked = beforeSave?.() ?? null;
    if (blocked) {
      setIssues(new Map([[fieldKeyWithinSection(blocked.field, section), blocked.message]]));
      setError({
        reason: '还没有填完，这次没有保存',
        hint: blocked.message,
        retriable: false,
        code: 'validation',
        detail: blocked.field,
        traceId: null,
      });
      setMessage(null);
      return;
    }
    setSaving(true);
    setError(null);
    setMessage(null);
    setIssues(new Map());
    try {
      // 附加字段在**保存这一刻**取：面板上刚改的音色必须进这一次请求体，
      // 否则界面显示已配好、库里还是上一份（"控件能看见、值到不了后端"）。
      const extra = extraValues?.() ?? {};
      const draft = await api.saveDraft({ [section]: { ...values, ...extra } });
      // 段外的字段（音色）由调用方自己去标——它在 `fields` 里根本没有对应项。
      onDraftIssues?.(draft.validation_errors);
      // 只有 severity=error 才阻断；warning 是"别的供应商还没存密钥"这类
      // 与本段策略无关的提示，不该拦住保存。
      const blocking = draft.validation_errors.filter(
        (issue) => (issue.severity ?? 'error') === 'error',
      );
      if (blocking.length > 0) {
        // 逐条标到对应输入框：后端给的 `field` 是唯一依据，不在前端重判。
        setIssues(
          new Map(
            blocking
              .filter((issue) => issue.field)
              .map((issue) => [fieldKeyWithinSection(issue.field, section), issue.message]),
          ),
        );
        setError({
          reason: '草稿未通过校验',
          hint: blocking.map((e) => e.message).join('；'),
          retriable: false,
          code: 'validation',
          detail: null,
          traceId: null,
        });
        return;
      }
      await api.publish(draft.revision);
      setMessage(publishedMessage);
      await load();
    } catch (err) {
      // 422/409 的 `error.field` 挂在 `ApiError` 上（见 `api.ts::connectorWriteError`）。
      const fieldIssue = voiceIssueFromError(err);
      if (fieldIssue) {
        setIssues(new Map([[fieldKeyWithinSection(fieldIssue.field, section), fieldIssue.message]]));
      }
      onSaveError?.(err);
      setError(explainError(err));
    } finally {
      setSaving(false);
    }
  };

  const disabled = saving || busy;

  return (
    <section className="page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> {backLabel}
      </button>

      <h1 className="page__title">{title}</h1>
      <p className="page__hint">{hint}</p>

      {loading ? <p className="page__hint">加载中…</p> : null}

      {error ? (
        <div className="banner banner--error">
          <AlertIcon />
          <div>
            <strong>{error.reason}</strong>
            {error.hint ? <div className="banner__detail">{error.hint}</div> : null}
          </div>
          <button type="button" className="button button--small" onClick={() => void load()}>
            <RefreshIcon /> 重试
          </button>
        </div>
      ) : null}

      {message ? <div className="banner banner--ok">{message}</div> : null}

      {values ? (
        <form className="memory-form" onSubmit={save}>
          {fields.map((field) => (
            <label key={field.key} className="memory-form__row">
              <span className="memory-form__head">
                <span className="memory-form__label">{field.label}</span>
                {field.kind === 'bool' ? (
                  <input
                    className="memory-form__check"
                    type="checkbox"
                    checked={Boolean(values[field.key])}
                    onChange={(event) =>
                      setValues((current) =>
                        current
                          ? { ...current, [field.key]: event.target.checked ? 1 : 0 }
                          : current,
                      )
                    }
                  />
                ) : field.kind === 'minutes' ? (
                  <input
                    className="memory-form__input"
                    type="time"
                    value={minutesToHhmm(values[field.key] ?? 0)}
                    onChange={(event) =>
                      setValues((current) =>
                        current
                          ? { ...current, [field.key]: hhmmToMinutes(event.target.value) }
                          : current,
                      )
                    }
                  />
                ) : (
                  <input
                    className="memory-form__input"
                    type="number"
                    value={values[field.key] ?? ''}
                    step={field.step ?? 'any'}
                    min={field.min}
                    max={field.max}
                    onChange={(event) => update(field.key, event.target.value)}
                  />
                )}
              </span>
              <span className="memory-form__hint">{field.hint}</span>
              {issues.get(field.key) ? (
                <span className="memory-form__error" role="alert">
                  {issues.get(field.key)}
                </span>
              ) : null}
            </label>
          ))}

          {sections ? sections({ saving: disabled, issues }) : null}

          <div className="memory-form__actions">
            <button type="submit" className="button button--primary" disabled={disabled}>
              {busy ? '上传中…' : saving ? '发布中…' : '保存并发布'}
            </button>
            <button type="button" className="button" onClick={() => void load()} disabled={disabled}>
              放弃修改
            </button>
          </div>
        </form>
      ) : null}
    </section>
  );
}
