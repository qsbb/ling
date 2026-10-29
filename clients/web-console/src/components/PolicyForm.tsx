/**
 * 通用「策略表单」：数字字段 + 草稿 → 发布。
 *
 * 为什么抽出来：④ 记忆与学习 和 ⑧ 生活与活动 的表单结构完全一样。
 * 复制两份的话，上一次踩过的坑（`min=1 step=5` 让默认值 120 非法，
 * 浏览器静默拦截提交、按钮点了没反应）会在其中一份里重演。
 * 步长、校验、severity 处理都收在一处。
 */
import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
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
}

type Values = Record<string, number>;

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

export function PolicyForm({ title, hint, fields, section, onBack }: PolicyFormProps) {
  const [values, setValues] = useState<Values | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<ExplainedError | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
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
    if (!values || saving) return;
    setSaving(true);
    setError(null);
    setMessage(null);
    try {
      const draft = await api.saveDraft({ [section]: values });
      // 只有 severity=error 才阻断；warning 是"别的供应商还没存密钥"这类
      // 与本段策略无关的提示，不该拦住保存。
      const blocking = draft.validation_errors.filter(
        (issue) => (issue.severity ?? 'error') === 'error',
      );
      if (blocking.length > 0) {
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
      setMessage('已发布。新策略会在下一轮对话起生效。');
      await load();
    } catch (err) {
      setError(explainError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回设置
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
            </label>
          ))}

          <div className="memory-form__actions">
            <button type="submit" className="button button--primary" disabled={saving}>
              {saving ? '发布中…' : '保存并发布'}
            </button>
            <button type="button" className="button" onClick={() => void load()}>
              放弃修改
            </button>
          </div>
        </form>
      ) : null}
    </section>
  );
}
