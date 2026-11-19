import { useEffect, useState } from 'react';
import '../window-policy.css';
import {
  api,
  ApiError,
  personaId,
  type WindowDialoguePolicy,
  type WindowDialoguePolicyField,
  type WindowDialoguePolicyFields,
  type WindowDialoguePolicyOriginStatus,
  type WindowDialoguePolicySource,
  type WindowDialoguePolicyValue,
  type WindowOriginBindingChange,
  type WindowOriginBindingOptions,
  type WindowOriginBindingPreview,
  type WindowPolicyChange,
  type WindowPolicyPreview,
} from '../api';
import type { ConnectorAccessGroup, PersonaSummary } from '../types';

type PolicyFieldState = WindowDialoguePolicyFields[WindowDialoguePolicyField];

interface PolicyFieldDefinition {
  key: WindowDialoguePolicyField;
  label: string;
  kind: 'persona' | 'boolean' | 'integer';
  trueLabel?: string;
  falseLabel?: string;
}

const FIELDS: PolicyFieldDefinition[] = [
  { key: 'persona_id', label: '人格', kind: 'persona' },
  {
    key: 'private_may_stay_silent',
    label: '私聊可沉默',
    kind: 'boolean',
    trueLabel: '允许沉默',
    falseLabel: '不允许选择沉默',
  },
  {
    key: 'group_mention_requires_reply',
    label: '群里被点名',
    kind: 'boolean',
    trueLabel: '必须回应',
    falseLabel: '由模型决定',
  },
  {
    key: 'group_attention_deferred',
    label: '群聊判断',
    kind: 'boolean',
    trueLabel: '先进入队列',
    falseLabel: '即时判断',
  },
  { key: 'group_turn_budget', label: '群聊发言预算', kind: 'integer' },
  {
    key: 'rest_may_skip',
    label: '休息时',
    kind: 'boolean',
    trueLabel: '允许跳过',
    falseLabel: '仍会判断',
  },
];

const EMPTY_SELECTIONS = Object.fromEntries(FIELDS.map(({ key }) => [key, 'inherit'])) as Record<WindowDialoguePolicyField, string>;

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : '无法连接服务端，请稍后重试';
}

function fieldState(
  policy: { fields: WindowDialoguePolicyFields },
  key: WindowDialoguePolicyField,
): PolicyFieldState {
  return policy.fields[key];
}

function originStatus(policy: WindowDialoguePolicy): WindowDialoguePolicyOriginStatus {
  return policy.origin_status;
}

function sessionOverride(field: PolicyFieldState): { hasOverride: boolean; value: WindowDialoguePolicyValue } {
  return { hasOverride: field.has_session_override, value: field.session_override };
}

function selectionFor(field: PolicyFieldState, definition: PolicyFieldDefinition): string {
  const current = sessionOverride(field);
  if (!current.hasOverride) return 'inherit';
  if (definition.kind === 'persona') {
    return current.value === null ? 'persona:none' : `persona:${String(current.value)}`;
  }
  if (definition.kind === 'boolean') return current.value === true ? 'value:true' : 'value:false';
  return `value:${String(current.value)}`;
}

function sourceLabel(source: WindowDialoguePolicySource): string {
  if (source === 'session') return '本会话';
  if (source === 'instance') return '平台实例';
  return '全局';
}

function personaName(id: string | null, personas: PersonaSummary[]): string {
  if (id === null) return '不注入人格';
  const persona = personas.find((item) => personaId(item) === id);
  return persona?.display_name?.trim() || persona?.name?.trim() || id;
}

function valueLabel(
  definition: PolicyFieldDefinition,
  value: WindowDialoguePolicyValue,
  personas: PersonaSummary[],
): string {
  if (definition.kind === 'persona') return personaName(typeof value === 'string' ? value : null, personas);
  if (definition.kind === 'integer') return `${String(value)} 次`;
  return value === true ? definition.trueLabel ?? '开启' : definition.falseLabel ?? '关闭';
}

function selectionValue(
  definition: PolicyFieldDefinition,
  selection: string,
): { inherited: true } | { inherited: false; value: WindowDialoguePolicyValue } {
  if (selection === 'inherit') return { inherited: true };
  if (definition.kind === 'persona') {
    return selection === 'persona:none'
      ? { inherited: false, value: null }
      : { inherited: false, value: selection.slice('persona:'.length) };
  }
  if (definition.kind === 'boolean') return { inherited: false, value: selection === 'value:true' };
  return { inherited: false, value: Number(selection.slice('value:'.length)) };
}

function changeFor(
  policy: WindowDialoguePolicy,
  selections: Record<WindowDialoguePolicyField, string>,
): WindowPolicyChange {
  const set: WindowPolicyChange['set'] = {};
  const clear: WindowDialoguePolicyField[] = [];

  for (const definition of FIELDS) {
    const current = sessionOverride(fieldState(policy, definition.key));
    const selected = selectionValue(definition, selections[definition.key]);
    if (selected.inherited) {
      if (current.hasOverride) clear.push(definition.key);
    } else if (!current.hasOverride || selected.value !== current.value) {
      Object.assign(set, { [definition.key]: selected.value });
    }
  }

  return { set, clear };
}

function changedFields(change: WindowPolicyChange): WindowDialoguePolicyField[] {
  const setFields = FIELDS
    .filter(({ key }) => Object.hasOwn(change.set, key))
    .map(({ key }) => key);
  return [...setFields, ...change.clear];
}

function personaOptions(policy: WindowDialoguePolicy, personas: PersonaSummary[]): Array<{ value: string; label: string }> {
  const options = new Map<string, string>();
  for (const persona of personas) {
    const id = personaId(persona);
    if (id) options.set(id, persona.display_name?.trim() || persona.name?.trim() || id);
  }

  const field = fieldState(policy, 'persona_id');
  const knownIds = [field.global_value, field.effective, field.session_override, field.instance_override];
  for (const value of knownIds) {
    if (typeof value === 'string' && value && !options.has(value)) options.set(value, value);
  }

  return [...options].map(([id, label]) => ({ value: `persona:${id}`, label }));
}

function originChoice(scope: string | null, instanceId: string | null): string {
  return scope && instanceId ? `${scope}:${instanceId}` : '';
}

function originLabel(
  policy: WindowDialoguePolicy,
  options: WindowOriginBindingOptions | null,
): string {
  if (policy.origin_status === 'unknown') return '来源未知';
  if (policy.origin_status === 'not_applicable') return '网页';
  const candidate = options?.candidates.find((item) =>
    item.scope === policy.instance_scope && item.instance_id === policy.instance_id,
  );
  return candidate?.display_name || `${policy.instance_scope}/${policy.instance_id}`;
}

function WindowOriginBinding({
  sessionId,
  policy,
  personas,
  onSaved,
}: {
  sessionId: string;
  policy: WindowDialoguePolicy;
  personas: PersonaSummary[];
  onSaved: (policy: WindowDialoguePolicy) => void;
}) {
  const [options, setOptions] = useState<WindowOriginBindingOptions | null>(null);
  const [selection, setSelection] = useState('');
  const [preview, setPreview] = useState<WindowOriginBindingPreview | null>(null);
  const [change, setChange] = useState<WindowOriginBindingChange | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [available, setAvailable] = useState(true);

  useEffect(() => {
    let active = true;
    api.windowOriginBinding(sessionId).then((result) => {
      if (!active) return;
      setOptions(result);
      setSelection(result.manually_bound
        ? originChoice(policy.instance_scope, policy.instance_id)
        : '');
    }).catch((reason: unknown) => {
      if (!active) return;
      if (reason instanceof ApiError && reason.status === 409) {
        setAvailable(false);
        return;
      }
      setError(errorText(reason));
    });
    return () => { active = false; };
  }, [policy.instance_id, policy.instance_scope, policy.origin_status, sessionId]);

  if (!available) return null;

  const currentChoice = options?.manually_bound
    ? originChoice(policy.instance_scope, policy.instance_id)
    : '';
  const selectedCandidate = options?.candidates.find((candidate) =>
    originChoice(candidate.scope, candidate.instance_id) === selection,
  );
  const canBind = Boolean(selectedCandidate && selection !== currentChoice);
  const canUnbind = Boolean(options?.manually_bound && selection === '');

  async function previewChange(next: WindowOriginBindingChange) {
    setBusy(true);
    setError('');
    try {
      const result = await api.previewWindowOriginBinding(sessionId, next);
      setChange(next);
      setPreview(result);
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setBusy(false);
    }
  }

  async function saveChange() {
    if (!preview || !change) return;
    setBusy(true);
    setError('');
    try {
      const saved = await api.saveWindowOriginBinding(
        sessionId, change, preview.preview_token,
      );
      onSaved(saved);
      const refreshed = await api.windowOriginBinding(sessionId);
      setOptions(refreshed);
      setSelection(refreshed.manually_bound
        ? originChoice(saved.instance_scope, saved.instance_id)
        : '');
      setPreview(null);
      setChange(null);
    } catch (reason) {
      setPreview(null);
      setChange(null);
      setError(errorText(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="window-policy__origin" aria-label="平台来源">
      <div className="window-policy__origin-heading">
        <strong>{originLabel(policy, options)}</strong>
        {options?.manually_bound ? <span>·</span> : null}
        {options?.manually_bound ? (
          <span>{policy.instance_scope}/{policy.instance_id}</span>
        ) : null}
      </div>
      {options?.candidates.length ? (
        <div className="window-policy__origin-controls">
          <label htmlFor={`origin-binding-${sessionId}`}>平台实例</label>
          <select
            id={`origin-binding-${sessionId}`}
            aria-label="平台实例来源"
            value={selection}
            disabled={busy || Boolean(preview)}
            onChange={(event) => {
              setSelection(event.target.value);
              setPreview(null);
              setChange(null);
            }}
          >
            <option value="">未绑定</option>
            {options.candidates.map((candidate) => (
              <option
                key={`${candidate.scope}:${candidate.instance_id}`}
                value={originChoice(candidate.scope, candidate.instance_id)}
              >
                {candidate.display_name} · {candidate.instance_id}{candidate.enabled ? '' : ' · 已停用'}
              </option>
            ))}
          </select>
          {canBind ? (
            <button type="button" className="button button--small" disabled={busy}
              onClick={() => void previewChange({
                scope: selectedCandidate!.scope,
                instance_id: selectedCandidate!.instance_id,
              })}>
              预览绑定
            </button>
          ) : null}
          {canUnbind ? (
            <button type="button" className="button button--small" disabled={busy}
              onClick={() => void previewChange({ scope: null, instance_id: null })}>
              预览解除
            </button>
          ) : null}
        </div>
      ) : (
        <span className="window-policy__origin-empty">没有匹配的实例</span>
      )}
      {preview && change ? (
        <div className="window-policy__preview" role="status">
          <p>
            {originLabel(preview.current, options)}{' → '}
            {preview.target?.display_name || '来源未知'}
          </p>
          {FIELDS.filter((definition) => {
            const before = preview.current.fields[definition.key];
            const after = preview.proposed.fields[definition.key];
            return before.effective !== after.effective || before.source !== after.source;
          }).map((definition) => {
            const before = preview.current.fields[definition.key];
            const after = preview.proposed.fields[definition.key];
            return (
              <p key={definition.key}>
                {definition.label}: {valueLabel(definition, before.effective, personas)} · {sourceLabel(before.source)}
                {' → '}
                {valueLabel(definition, after.effective, personas)} · {sourceLabel(after.source)}
              </p>
            );
          })}
          <button type="button" className="button button--primary button--small" disabled={busy}
            onClick={() => void saveChange()}>
            确认
          </button>
          <button type="button" className="button button--small" disabled={busy}
            onClick={() => { setPreview(null); setChange(null); }}>
            返回
          </button>
        </div>
      ) : null}
      {error ? <p role="alert">{error}</p> : null}
    </section>
  );
}

type AccessTarget = { kind: 'group' | 'private'; id: string };

function WindowAllowlist({ target }: { target: AccessTarget }) {
  const [access, setAccess] = useState<ConnectorAccessGroup | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fieldKey = target.kind === 'group' ? 'LING_ACCESS_GROUPS' : 'LING_ACCESS_LIST';
  const value = access?.fields.find((field) => field.key === fieldKey)?.value;
  const items = Array.isArray(value) ? value : typeof value === 'string' && value !== '' ? value.split(',') : [];
  const mode = access?.fields.find((field) => field.key === 'LING_ACCESS_MODE')?.value;
  const listed = items.includes(target.id);
  const enabled = access?.editable && mode === 'whitelist' && value !== undefined;

  useEffect(() => {
    let active = true;
    void api.connectors().then((result) => {
      if (active) setAccess(result.access ?? null);
    }).catch((reason: unknown) => {
      if (active) setError(errorText(reason));
    }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, []);

  async function update(checked: boolean) {
    if (!access || !enabled || busy) return;
    setBusy(true);
    setError(null);
    try {
      const next = checked ? [...items, target.id] : items.filter((item) => item !== target.id);
      const result = await api.saveConnectorConfig('access', { set: { [fieldKey]: next }, clear: [] }, access.generation);
      if (result.state !== 'active' && result.state !== 'unchanged') {
        setError(result.error_safe || '白名单更改未生效');
      }
    } catch (reason) {
      setError(errorText(reason));
    }
    try {
      setAccess((await api.connectors()).access ?? null);
    } catch (reason) {
      setAccess(null);
      setError(errorText(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="window-policy__access">
      <label>
        <input type="checkbox" checked={listed} disabled={busy || !enabled}
          onChange={(event) => void update(event.target.checked)} />
        {target.kind === 'group' ? '本群在白名单中' : '该账号在白名单中'}
      </label>
      {access && mode !== 'whitelist' ? <small>当前未启用白名单模式。</small> : null}
      {mode === 'whitelist' && value === undefined ? <small>服务端不支持快捷修改。</small> : null}
      {!busy && !access && !error ? <small>当前无法读取出入许可。</small> : null}
      {error ? <p role="alert">{error}</p> : null}
    </div>
  );
}

export function WindowPolicy({ sessionId, onClose, accessTarget }: {
  sessionId: string;
  onClose: () => void;
  accessTarget?: AccessTarget;
}) {
  const [policy, setPolicy] = useState<WindowDialoguePolicy | null>(null);
  const [selections, setSelections] = useState(EMPTY_SELECTIONS);
  const [personas, setPersonas] = useState<PersonaSummary[]>([]);
  const [preview, setPreview] = useState<WindowPolicyPreview | null>(null);
  const [previewChange, setPreviewChange] = useState<WindowPolicyChange | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [personaError, setPersonaError] = useState('');

  useEffect(() => {
    let active = true;
    api.windowDialoguePolicy(sessionId).then((result) => {
      if (!active) return;
      setPolicy(result);
      setSelections(Object.fromEntries(
        FIELDS.map((definition) => [definition.key, selectionFor(fieldState(result, definition.key), definition)]),
      ) as Record<WindowDialoguePolicyField, string>);
    }).catch((reason: unknown) => {
      if (active) setError(errorText(reason));
    });
    api.listPersonas().then((result) => {
      if (active) setPersonas(result.items);
    }).catch((reason: unknown) => {
      if (active) setPersonaError(errorText(reason));
    });
    return () => { active = false; };
  }, [sessionId]);

  const change = policy ? changeFor(policy, selections) : null;
  const canPreview = change !== null && changedFields(change).length > 0;

  async function previewPolicy() {
    if (!change || !canPreview) return;
    setBusy(true);
    setError('');
    try {
      const result = await api.previewWindowPolicy(sessionId, change);
      setPreviewChange(change);
      setPreview(result);
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!preview || !previewChange) return;
    setBusy(true);
    setError('');
    try {
      const saved = await api.saveWindowPolicy(sessionId, previewChange, preview.preview_token);
      setPolicy(saved);
      setSelections(Object.fromEntries(
        FIELDS.map((definition) => [definition.key, selectionFor(fieldState(saved, definition.key), definition)]),
      ) as Record<WindowDialoguePolicyField, string>);
      setPreview(null);
      setPreviewChange(null);
    } catch (reason) {
      setPreview(null);
      setPreviewChange(null);
      setError(errorText(reason));
    } finally {
      setBusy(false);
    }
  }

  function updateSelection(key: WindowDialoguePolicyField, value: string) {
    setSelections((current) => ({ ...current, [key]: value }));
    setPreview(null);
    setPreviewChange(null);
  }

  return (
    <section className="window-policy" aria-label="会话设置">
      <div className="window-policy__heading">
        <strong>
          会话设置
          {policy && originStatus(policy) === 'unknown' ? <span> · 平台实例来源未知</span> : null}
        </strong>
        <button type="button" className="ghost-button" onClick={onClose}>关闭</button>
      </div>
      {accessTarget ? <WindowAllowlist key={`${accessTarget.kind}:${accessTarget.id}`} target={accessTarget} /> : null}
      {policy ? (
        <>
          <div aria-label="对话规则">
            {FIELDS.map((definition) => {
              const field = fieldState(policy, definition.key);
              const id = `window-policy-${definition.key}`;
              return (
                <div key={definition.key} style={{
                  display: 'grid',
                  gridTemplateColumns: 'minmax(0, 1fr) minmax(0, min(24rem, 45%))',
                  width: '100%',
                  alignItems: 'center',
                  gap: '0.75rem',
                  padding: '0.4rem 0',
                  borderBottom: '1px solid var(--border, #ded6c8)',
                }}>
                  <div>
                    <label htmlFor={id}>{definition.label}</label>
                    <output style={{ display: 'block', color: 'var(--text-muted, #766e61)', fontSize: '0.85rem' }}>
                      {valueLabel(definition, field.effective, personas)} · {sourceLabel(field.source)}
                    </output>
                  </div>
                  <select
                    id={id}
                    value={selections[definition.key]}
                    disabled={busy}
                    onChange={(event) => updateSelection(definition.key, event.target.value)}
                  >
                    <option value="inherit">继承上层</option>
                    {definition.kind === 'persona' ? (
                      <>
                        <option value="persona:none">不注入人格</option>
                        {personaOptions(policy, personas).map((option) => (
                          <option key={option.value} value={option.value}>{option.label}</option>
                        ))}
                      </>
                    ) : definition.kind === 'boolean' ? (
                      <>
                        <option value="value:true">{definition.trueLabel}</option>
                        <option value="value:false">{definition.falseLabel}</option>
                      </>
                    ) : Array.from({ length: 20 }, (_, index) => index + 1).map((value) => (
                      <option key={value} value={`value:${value}`}>{value} 次</option>
                    ))}
                  </select>
                </div>
              );
            })}
          </div>
          {policy.origin_status !== 'not_applicable' ? (
            <WindowOriginBinding
              sessionId={sessionId}
              policy={policy}
              personas={personas}
              onSaved={setPolicy}
            />
          ) : null}
          {preview ? (
            <div className="window-policy__preview" role="status">
              <ul style={{ flexBasis: '100%', margin: 0, paddingLeft: '1.2rem' }}>
                {changedFields(previewChange ?? { set: {}, clear: [] }).map((key) => {
                  const definition = FIELDS.find((item) => item.key === key);
                  if (!definition) return null;
                  const current = fieldState(preview.current, key);
                  const proposed = fieldState(preview.proposed, key);
                  return (
                    <li key={key}>
                      <strong>{definition.label}</strong>{' '}
                      {valueLabel(definition, current.effective, personas)} · {sourceLabel(current.source)}{' → '}
                      {valueLabel(definition, proposed.effective, personas)} · {sourceLabel(proposed.source)}
                    </li>
                  );
                })}
              </ul>
              <button type="button" className="button button--primary button--small" disabled={busy} onClick={() => void save()}>确认保存</button>
              <button type="button" className="button button--small" disabled={busy} onClick={() => { setPreview(null); setPreviewChange(null); }}>返回修改</button>
            </div>
          ) : (
            <button type="button" className="button button--small" disabled={busy || !canPreview} onClick={() => void previewPolicy()}>预览更改</button>
          )}
        </>
      ) : !error ? <p>正在读取会话设置…</p> : null}
      {error ? <p role="alert">{error}</p> : null}
      {personaError ? <p role="alert">{personaError}</p> : null}
    </section>
  );
}
