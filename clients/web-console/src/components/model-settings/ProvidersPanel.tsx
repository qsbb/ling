/**
 * 「供应商」视图：左边一家一家列出来，右边只编辑当前这一家。
 *
 * 和旧设置页最大的区别不是外观，而是**一次只面对一家**：类型、地址、密钥、
 * 模型清单都在同一张卡里。改动提醒在卡的顶部（改动未保存 + 撤销），真正
 * 落盘的动作只有底部「测试连接」右边的「保存」，不让用户在
 * "保存草稿 → 发布"之间猜。
 */

import { useMemo, useRef, useState } from 'react';
import { api } from '../../api';
import { explainError } from '../../errors';
import type {
  ProbeResult,
  ProfileResolution,
  ProviderDraftInput,
  ProviderModel,
  SettingsConfig,
} from '../../types';
import { AlertIcon, CheckIcon, PlusIcon, RefreshIcon } from '../Icons';
import {
  MsSwitch,
  PROVIDER_KIND_OPTIONS,
  StatusDot,
  capabilityLabel,
  formatAgo,
  hostOf,
  kindLabel,
  providerEquals,
  providerGlyph,
  providerShapeIssues,
  roleShort,
  rolesForModel,
  toProviderDraft,
} from './shared';

interface Props {
  config: SettingsConfig;
  providers: ProviderDraftInput[];
  /** 服务端算的「谁在用哪个模型」。职责标签与删除确认都读它。 */
  resolutions: ProfileResolution[];
  selectedId: string | null;
  saving: boolean;
  /** 「供应商」页里没保存的改动数（含其他家与运行设置），决定保存按钮是否可用。 */
  dirtyCount: number;
  onSelect: (providerId: string) => void;
  onPatch: (providerId: string, patch: Partial<ProviderDraftInput>) => void;
  onAddProvider: (provider: ProviderDraftInput) => void;
  onDiscard: () => void;
  onSave: () => void;
  onSecretsChanged: () => void;
  onShowHistory: () => void;
  testMode: boolean;
  logDenials: boolean;
  onTestMode: (value: boolean) => void;
  onLogDenials: (value: boolean) => void;
  /** 今日 token 用量（键是 provider_id）。取不到时为空对象。 */
  usage: Record<string, { day: string; used_tokens: number; estimated: boolean }>;
}

/** `-1` / null → null（不限制）；其余原样。 */
function limitOf(value: number | null): number | null {
  if (value === null || value < 0) return null;
  return value;
}

function limitLabel(value: number | null): string {
  const effective = limitOf(value);
  if (effective === null) return '不限制';
  return effective.toLocaleString();
}

/**
 * 额度输入框：**留空显示灰色的 -1**，代表不限制。
 *
 * 为什么不直接用 placeholder="不限制"：用户会以为"空着"和"填 0"是两回事，
 * 而服务端的约定只有 -1 这一个"关掉"的值。让空输入框直接显示 -1，
 * 等于把约定摊在界面上——用户看到的是一个真实会提交的值，不是提示文字。
 */
function LimitField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: number | null;
  onChange: (value: number | null) => void;
}) {
  const shown = value === null || value < 0 ? '' : String(value);
  return (
    <label className="ms-field">
      <span className="ms-field__label">{label}</span>
      <input
        className="ms-input"
        type="number"
        min={-1}
        value={shown}
        // 空值时显示 -1 作为灰色虚字（placeholder），用户一旦输入就变成实值。
        placeholder="-1"
        onChange={(event) => {
          const raw = event.target.value.trim();
          if (raw === '') {
            // 清空 = 提交 -1（不限制），而不是 null（"没设置"）。
            onChange(-1);
            return;
          }
          const next = Number.parseInt(raw, 10);
          onChange(Number.isFinite(next) ? next : -1);
        }}
      />
    </label>
  );
}

function newModelError(models: ProviderModel[], modelId: string): string {
  if (!modelId) return '模型 ID 不能为空';
  if (models.some((model) => model.model_id === modelId)) return '这个模型已经在清单里了';
  return '';
}

/** API 地址输入框的占位提示：三种协议写法不一样，别让人对着一个空框猜。 */
function baseUrlPlaceholder(kind: string): string {
  if (kind === 'fake') return '内置供应商不需要地址';
  if (kind === 'cloudflare_embedding') {
    return '可留空（用账号 ID 拼官方端点），或填完整地址';
  }
  if (kind === 'siliconflow_rerank') return 'https://api.siliconflow.cn/v1';
  return 'https://…/v1';
}

/** 模型 ID 的例子按能力分开：嵌入/重排的模型名和 chat 完全不同。 */
function modelIdPlaceholder(kind: string): string {
  if (kind === 'cloudflare_embedding') return '模型 ID，例如 @cf/baai/bge-m3';
  if (kind === 'siliconflow_rerank') return '模型 ID，例如 BAAI/bge-reranker-v2-m3';
  return '模型 ID，例如 gpt-4o-mini';
}

export function ProvidersPanel({
  config,
  providers,
  resolutions,
  selectedId,
  saving,
  dirtyCount,
  onSelect,
  onPatch,
  onAddProvider,
  onDiscard,
  onSave,
  onSecretsChanged,
  onShowHistory,
  testMode,
  logDenials,
  onTestMode,
  onLogDenials,
  usage: usageMap,
}: Props) {
  const [newProviderOpen, setNewProviderOpen] = useState(false);
  const [newProvider, setNewProvider] = useState({
    provider_id: '',
    kind: 'openai_compatible',
    base_url: '',
    // Cloudflare 的账号 ID 存在 `extra.account_id`（对应
    // `LING_EMBEDDING_ACCOUNT_ID`）。放这里而不是顶层字段，是因为它是
    // 供应商的私有连接参数，服务端 `ProviderIn.extra` 已经能收。
    account_id: '',
  });
  const [newProviderError, setNewProviderError] = useState('');
  const [addingModel, setAddingModel] = useState(false);
  const [newModel, setNewModel] = useState({ model_id: '' });
  const [modelError, setModelError] = useState('');
  const [secretEditing, setSecretEditing] = useState<string | null>(null);
  const [secretInput, setSecretInput] = useState('');
  const [secretBusy, setSecretBusy] = useState(false);
  const [probeBusy, setProbeBusy] = useState(false);
  const [probes, setProbes] = useState<Record<string, ProbeResult>>({});
  const runRef = useRef<HTMLDetailsElement>(null);
  const [actionError, setActionError] = useState('');

  const selected = providers.find((provider) => provider.provider_id === selectedId) ?? null;
  const persisted = useMemo(
    () => new Set(config.providers.map((provider) => provider.provider_id)),
    [config.providers],
  );
  const baselineProviders = useMemo(
    () => config.providers.map(toProviderDraft),
    [config.providers],
  );
  const baseline = selected
    ? baselineProviders.find((provider) => provider.provider_id === selected.provider_id)
    : undefined;
  const isDirty = Boolean(selected && (!baseline || !providerEquals(selected, baseline)));
  const probe = selected ? probes[selected.provider_id] : undefined;
  const usage = selected ? usageMap[selected.provider_id] : undefined;
  const missingFromProbe = useMemo(() => {
    if (!probe?.ok) return [];
    const known = new Set((selected?.models ?? []).map((model) => model.model_id));
    return probe.models.filter((modelId) => !known.has(modelId)).slice(0, 8);
  }, [probe, selected]);

  // 类型下拉以**服务端**的 `known_kinds` 为准：后端不认识的类型不该出现在
  // 这里，后端新加的类型则不用等前端发版。标签仍然来自前端的共用表，
  // 认不出来时 `kindLabel` 会原样显示 kind。
  const selectedKind = selected?.kind;
  const kindOptions = useMemo<{ value: string; label: string }[]>(() => {
    const known = new Set(config.known_kinds ?? []);
    // 服务端没报（旧后端 / 测试夹具）时不把下拉清空：那是比"少一个过滤"更糟的
    // 失败——用户连类型都选不了。
    const options: { value: string; label: string }[] = (
      known.size > 0
        ? PROVIDER_KIND_OPTIONS.filter((option) => known.has(option.value))
        : [...PROVIDER_KIND_OPTIONS]
    ).map((option) => ({ value: option.value, label: option.label }));
    // 当前这家的 kind 不在清单里（服务端更新前后短暂不一致）时也要能显示，
    // 否则 `<select>` 会退化成空白，用户以为类型丢了。
    if (selectedKind && !options.some((option) => option.value === selectedKind)) {
      options.push({ value: selectedKind, label: kindLabel(selectedKind) });
    }
    return options;
  }, [config.known_kinds, selectedKind]);

  // 「还差什么」只是提前提示：条件与后端一致，但**不阻止**保存草稿。
  // 发布时后端仍会再判一次（它是唯一真相）。
  const shapeIssues = selected ? providerShapeIssues(selected) : [];
  const newProviderIssues = providerShapeIssues({
    provider_id: newProvider.provider_id,
    kind: newProvider.kind,
    base_url: newProvider.base_url,
    extra: newProvider.account_id.trim()
      ? { account_id: newProvider.account_id.trim() }
      : {},
  });

  const accountIdOf = (provider: ProviderDraftInput): string => {
    const value = provider.extra?.account_id;
    return typeof value === 'string' ? value : '';
  };

  const patchAccountId = (provider: ProviderDraftInput, value: string) => {
    const extra = { ...(provider.extra ?? {}) };
    if (value.trim()) extra.account_id = value;
    else delete extra.account_id;
    onPatch(provider.provider_id, { extra });
  };

  // 嵌入/重排没有 `/chat/completions` 形状的端点，`ModelRouter.probe` 对它们
  // 只会报"不是对话供应商"。按钮不该长得像能用。
  const probeUnsupported = Boolean(
    selected?.capabilities && !selected.capabilities.includes('chat'),
  );

  const patchModels = (models: ProviderModel[]) => {
    if (!selected) return;
    onPatch(selected.provider_id, { models });
  };

  const addModel = (modelId: string) => {
    if (!selected) return;
    const cleanId = modelId.trim();
    const error = newModelError(selected.models ?? [], cleanId);
    if (error) {
      setModelError(error);
      return;
    }
    patchModels([...(selected.models ?? []), { model_id: cleanId, note: '', enabled: true }]);
    setNewModel({ model_id: '' });
    setModelError('');
    setAddingModel(false);
  };

  const removeModel = (model: ProviderModel) => {
    const bound = rolesForModel(resolutions, selected?.provider_id ?? '', model.model_id);
    if (
      bound.length > 0 &&
      !window.confirm(
        `${model.model_id} 正被「${bound.map((item) => roleShort(item.profile)).join('、')}」使用。移除后职责会指向一个不在清单里的模型，仍然可用但会提示需要检查。继续？`,
      )
    ) {
      return;
    }
    patchModels((selected?.models ?? []).filter((item) => item.model_id !== model.model_id));
  };

  const submitNewProvider = () => {
    const providerId = newProvider.provider_id.trim();
    if (!providerId) {
      setNewProviderError('先给它起个名字');
      return;
    }
    if (providers.some((provider) => provider.provider_id === providerId)) {
      setNewProviderError(`「${providerId}」已经存在了`);
      return;
    }
    onAddProvider({
      provider_id: providerId,
      kind: newProvider.kind,
      base_url: newProvider.base_url.trim() || null,
      enabled: false,
      extra: newProvider.account_id.trim()
        ? { account_id: newProvider.account_id.trim() }
        : {},
      models: [],
      // 新供应商的默认值：60 秒超时、200k 输出上限、200k 日额度。
      // 后两项是**具体数字**而不是"不限制"：一个默认敞开的上限等于没有上限，
      // 用户第一次接上中转站就会被它的账单教育。留空（前端提交 -1）才是不限制。
      timeout_seconds: 60,
      max_output_tokens: 200000,
      daily_token_limit: 200000,
    });
    setNewProvider({
      provider_id: '',
      kind: 'openai_compatible',
      base_url: '',
      account_id: '',
    });
    setNewProviderError('');
    setNewProviderOpen(false);
  };

  const saveSecret = async () => {
    if (!selected || !secretInput.trim()) return;
    setSecretBusy(true);
    setActionError('');
    try {
      await api.setSecret(selected.provider_id, secretInput.trim());
      setSecretInput('');
      setSecretEditing(null);
      onSecretsChanged();
    } catch (error) {
      setActionError(explainError(error).reason);
    } finally {
      setSecretBusy(false);
    }
  };

  const deleteSecret = async () => {
    if (!selected) return;
    if (!window.confirm(`删除「${selected.provider_id}」已保存的密钥？之后调用会返回 secret_missing。`)) {
      return;
    }
    setSecretBusy(true);
    setActionError('');
    try {
      await api.deleteSecret(selected.provider_id);
      onSecretsChanged();
    } catch (error) {
      setActionError(explainError(error).reason);
    } finally {
      setSecretBusy(false);
    }
  };

  const runProbe = async () => {
    if (!selected) return;
    setProbeBusy(true);
    setActionError('');
    try {
      const result = await api.probeProvider(selected.provider_id);
      setProbes((current) => ({ ...current, [selected.provider_id]: result }));
    } catch (error) {
      const explained = explainError(error);
      setProbes((current) => ({
        ...current,
        [selected.provider_id]: {
          provider_id: selected.provider_id,
          ok: false,
          detail: explained.detail ?? explained.reason,
          latency_ms: 0,
          models: [],
        },
      }));
    } finally {
      setProbeBusy(false);
    }
  };

  const saved = selected ? config.providers.find((p) => p.provider_id === selected.provider_id) : null;
  const canProbe = Boolean(
    selected && persisted.has(selected.provider_id) && !isDirty && !probeUnsupported,
  );
  const probeTone = probe ? (probe.ok ? 'ok' : 'danger') : 'idle';
  const roleCount = selected
    ? (selected.models ?? []).reduce(
        (total, model) => total + rolesForModel(resolutions, selected.provider_id, model.model_id).length,
        0,
      )
    : 0;

  return (
    <div className="ms-providers">
      <div className="ms-layout">
        <aside className="ms-rail" aria-label="供应商">
          <div className="ms-rail__head">
            <span>供应商</span>
            <span className="ms-rail__count">{providers.length}</span>
          </div>
          <div className="ms-rail__list">
            {providers.map((provider) => {
              const active = provider.provider_id === selectedId;
              const providerProbe = probes[provider.provider_id];
              return (
                <button
                  key={provider.provider_id}
                  type="button"
                  className={`ms-rail-item${active ? ' ms-rail-item--active' : ''}`}
                  onClick={() => {
                    onSelect(provider.provider_id);
                    setAddingModel(false);
                    setModelError('');
                    setSecretEditing(null);
                  }}
                  aria-current={active ? 'true' : undefined}
                >
                  <span className={`ms-glyph${active ? ' ms-glyph--active' : ''}`}>
                    {providerGlyph(provider.provider_id)}
                  </span>
                  <span className="ms-rail-item__main">
                    <span className="ms-rail-item__name">{provider.provider_id}</span>
                    <span className="ms-rail-item__meta">
                      {provider.kind === 'fake'
                        ? '本地测试'
                        : provider.capabilities && !provider.capabilities.includes('chat')
                          ? // 嵌入/重排可能只填账号 ID、没有 base_url，
                            // 显示"未填写地址"会让人以为没配好。
                            kindLabel(provider.kind)
                          : hostOf(provider.base_url)}
                    </span>
                  </span>
                  <span
                    className={`ms-chip ${provider.enabled ? 'ms-chip--on' : ''}${
                      providerProbe && !providerProbe.ok ? ' ms-chip--bad' : ''
                    }`}
                  >
                    {providerProbe && !providerProbe.ok
                      ? '要检查'
                      : provider.enabled
                        ? '使用中'
                        : '已停用'}
                  </span>
                </button>
              );
            })}
          </div>
          {newProviderOpen ? (
            <div className="ms-new-provider">
              <label className="ms-field">
                <span className="ms-field__label">名字 / ID</span>
                <input
                  className="ms-input"
                  autoFocus
                  value={newProvider.provider_id}
                  placeholder="例如 星辰中转"
                  onChange={(event) =>
                    setNewProvider((current) => ({ ...current, provider_id: event.target.value }))
                  }
                />
              </label>
              <label className="ms-field">
                <span className="ms-field__label">类型</span>
                <select
                  className="ms-input"
                  value={newProvider.kind}
                  onChange={(event) =>
                    setNewProvider((current) => ({ ...current, kind: event.target.value }))
                  }
                >
                  {kindOptions.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="ms-field">
                <span className="ms-field__label">API 地址</span>
                <input
                  className="ms-input"
                  value={newProvider.base_url}
                  placeholder={baseUrlPlaceholder(newProvider.kind)}
                  onChange={(event) =>
                    setNewProvider((current) => ({ ...current, base_url: event.target.value }))
                  }
                />
              </label>
              {newProvider.kind === 'cloudflare_embedding' ? (
                <label className="ms-field">
                  <span className="ms-field__label">账号 ID（Account ID）</span>
                  <input
                    className="ms-input ms-input--mono"
                    value={newProvider.account_id}
                    placeholder="例如 023e105f4ecef8ad9ca31a8372d0c353"
                    onChange={(event) =>
                      setNewProvider((current) => ({
                        ...current,
                        account_id: event.target.value,
                      }))
                    }
                  />
                  <span className="ms-hint">
                    写到供应商的 extra.account_id（对应环境变量
                    LING_EMBEDDING_ACCOUNT_ID）。填了它就可以不填 API 地址。
                  </span>
                </label>
              ) : null}
              {newProviderIssues.length > 0 ? (
                <p className="ms-hint ms-hint--warn">
                  发布前还差：{newProviderIssues.map((issue) => issue.message).join('；')}
                </p>
              ) : null}
              {newProviderError ? (
                <p className="ms-error-text" role="alert">
                  {newProviderError}
                </p>
              ) : null}
              <div className="ms-new-provider__actions">
                <button type="button" className="ms-btn ms-btn--primary" onClick={submitNewProvider}>
                  添加
                </button>
                <button
                  type="button"
                  className="ms-btn"
                  onClick={() => {
                    setNewProviderOpen(false);
                    setNewProviderError('');
                  }}
                >
                  取消
                </button>
              </div>
            </div>
          ) : (
            <button type="button" className="ms-add-rail" onClick={() => setNewProviderOpen(true)}>
              <PlusIcon size={15} /> 新增供应商
            </button>
          )}
        </aside>

        {selected ? (
          <section className="ms-card" aria-label={`${selected.provider_id} 的设置`}>
            <header className="ms-provider-head">
              <div className="ms-provider-head__id">
                <span className="ms-glyph ms-glyph--lg">{providerGlyph(selected.provider_id)}</span>
                <div>
                  <h2 className="ms-provider-title">
                    {selected.provider_id}
                  </h2>
                  <p className="ms-provider-meta">
                    {(selected.models ?? []).length} 个模型
                    {selected.capabilities && selected.capabilities.length > 0
                      ? ` · 提供 ${selected.capabilities.map(capabilityLabel).join(' / ')}`
                      : ''}
                    {roleCount > 0 ? ` · ${roleCount} 个职责在用` : ''}
                    {probe?.checked_at ? ` · 上次检查 ${formatAgo(probe.checked_at)}` : ''}
                  </p>
                </div>
              </div>
              <div className="ms-provider-head__actions">
                <label className="ms-inline-switch">
                  <span>启用</span>
                  <MsSwitch
                    checked={selected.enabled}
                    label={`${selected.provider_id} 启用`}
                    onChange={(value) => onPatch(selected.provider_id, { enabled: value })}
                  />
                </label>
                <button
                  type="button"
                  className="ms-btn"
                  disabled={!canProbe || probeBusy}
                  title={
                    probeUnsupported
                      ? '嵌入 / 重排供应商没有对话端点，不经过模型路由，不能测试连接'
                      : !persisted.has(selected.provider_id)
                        ? '先保存这家供应商，才能测试连接'
                        : isDirty
                          ? '先保存改动，测试的才是你正在编辑的配置'
                          : '测试已保存的配置'
                  }
                  onClick={() => void runProbe()}
                >
                  <RefreshIcon size={13} /> {probeBusy ? '检查中…' : '测试连接'}
                </button>
                <button
                  type="button"
                  className="ms-btn ms-btn--primary"
                  onClick={onSave}
                  disabled={saving || dirtyCount === 0}
                  title={dirtyCount > 0 ? '把这次改动保存生效' : '没有未保存的改动'}
                >
                  {saving ? '保存中…' : '保存'}
                </button>
                {/* 修订历史仍然要有个入口，但不该占一颗按钮的位置：
                    收进一个小字链接，改坏的配置才有地方回滚。 */}
                <button
                  type="button"
                  className="ms-link ms-provider-head__history"
                  onClick={onShowHistory}
                >
                  修订历史
                </button>
              </div>
            </header>

            {selected.enabled ? null : (
              <p className="ms-note ms-note--quiet">
                停用后，她的对话不会再走这家；地址、密钥和模型清单都留着，随时可以再启用。
              </p>
            )}

            {isDirty ? (
              <div className="ms-dirtybar" role="status">
                <span className="ms-dirtybar__dot" aria-hidden="true" />
                <span>改动未保存</span>
                <span className="ms-dirtybar__spacer" />
                <button type="button" className="ms-link" onClick={onDiscard} disabled={saving}>
                  撤销
                </button>
              </div>
            ) : null}

            <div className="ms-form-grid">
              <label className="ms-field">
                <span className="ms-field__label">ID</span>
                <input
                  className="ms-input ms-input--mono"
                  value={selected.provider_id}
                  disabled
                  title="供应商 ID 是职责绑定的引用键；要改名请新建一家再迁移职责"
                />
              </label>
              <label className="ms-field">
                <span className="ms-field__label">类型</span>
                <select
                  className="ms-input"
                  value={selected.kind}
                  onChange={(event) =>
                    onPatch(selected.provider_id, { kind: event.target.value })
                  }
                >
                  {kindOptions.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="ms-field ms-field--wide">
                <span className="ms-field__label">
                  API 地址{selected.kind === 'cloudflare_embedding' ? '（可留空）' : ''}
                </span>
                <input
                  className="ms-input ms-input--mono"
                  value={selected.base_url ?? ''}
                  placeholder={baseUrlPlaceholder(selected.kind)}
                  disabled={selected.kind === 'fake'}
                  onChange={(event) =>
                    onPatch(selected.provider_id, { base_url: event.target.value || null })
                  }
                />
              </label>
              {selected.kind === 'openai_compatible' || selected.kind === 'local' ? (
                <div className="ms-field ms-field--wide">
                  <span className="ms-field__label">端点支持原生工具调用</span>
                  <MsSwitch
                    checked={selected.extra?.native_tools === true}
                    label="端点支持原生工具调用"
                    onChange={(value) =>
                      onPatch(selected.provider_id, {
                        extra: { ...selected.extra, native_tools: value },
                      })
                    }
                  />
                  <span className="ms-hint">
                    确认该端点及对话模型支持流式工具调用后开启。
                  </span>
                </div>
              ) : null}
              {selected.kind === 'cloudflare_embedding' ? (
                <label className="ms-field ms-field--wide">
                  <span className="ms-field__label">账号 ID（Account ID）</span>
                  <input
                    className="ms-input ms-input--mono"
                    value={accountIdOf(selected)}
                    placeholder="例如 023e105f4ecef8ad9ca31a8372d0c353"
                    onChange={(event) => patchAccountId(selected, event.target.value)}
                  />
                  <span className="ms-hint">
                    写到供应商的 extra.account_id（对应环境变量
                    LING_EMBEDDING_ACCOUNT_ID）。填了它就可以不填 API 地址；两样都给也合法。
                  </span>
                </label>
              ) : null}
            </div>

            {shapeIssues.length > 0 ? (
              <div className="ms-shape" role="status">
                <AlertIcon size={13} />
                <span>
                  发布前还差：{shapeIssues.map((issue) => issue.message).join('；')}
                  （可以先保存草稿，发布时服务端会再判一次）
                </span>
              </div>
            ) : null}
            {selected.capabilities === undefined ? (
              <p className="ms-hint ms-hint--warn">
                这家供应商的能力还没被服务端确认（新建或刚改过类型）。保存一次后，它才会出现在职责与默认槽位的下拉里。
              </p>
            ) : null}

            <div className="ms-secret">
              <span className="ms-field__label">密钥</span>
              {secretEditing === selected.provider_id ? (
                <div className="ms-secret__edit">
                  <input
                    className="ms-input"
                    type="password"
                    autoComplete="new-password"
                    autoFocus
                    placeholder="粘贴新密钥（保存后只显示尾号）"
                    aria-label={`${selected.provider_id} 新密钥`}
                    value={secretInput}
                    onChange={(event) => setSecretInput(event.target.value)}
                  />
                  <button
                    type="button"
                    className="ms-btn ms-btn--primary ms-btn--sm"
                    disabled={secretBusy || !secretInput.trim()}
                    onClick={() => void saveSecret()}
                  >
                    {secretBusy ? '保存中…' : '保存密钥'}
                  </button>
                  <button
                    type="button"
                    className="ms-btn ms-btn--sm"
                    onClick={() => {
                      setSecretEditing(null);
                      setSecretInput('');
                    }}
                  >
                    取消
                  </button>
                </div>
              ) : (
                <div className="ms-secret__summary">
                  {saved?.has_secret ? (
                    <>
                      <code className="ms-secret__mask">••••••••••••</code>
                      <span className="ms-secret__status">
                        <CheckIcon size={13} /> 已保存 · 尾号 {saved.secret_last4}
                      </span>
                    </>
                  ) : (
                    <span className="ms-secret__status ms-secret__status--missing">未保存密钥</span>
                  )}
                  <span className="ms-secret__spacer" />
                  <button
                    type="button"
                    className="ms-link"
                    onClick={() => {
                      setSecretEditing(selected.provider_id);
                      setSecretInput('');
                      setActionError('');
                    }}
                  >
                    {saved?.has_secret ? '更换' : '填写'}
                  </button>
                  {saved?.has_secret ? (
                    <button
                      type="button"
                      className="ms-link ms-link--danger"
                      disabled={secretBusy}
                      onClick={() => void deleteSecret()}
                    >
                      删除
                    </button>
                  ) : null}
                </div>
              )}
              <p className="ms-hint">
                密钥只保存在服务端并加密，页面永远只显示后四位。
                {persisted.has(selected.provider_id)
                  ? ''
                  : ' 新供应商先保存一次，密钥才会跟着这家一起生效。'}
              </p>
            </div>

            <div className="ms-models">
              <div className="ms-models__head">
                <h3>模型</h3>
                <span className="ms-muted">
                  {(selected.models ?? []).length} 个 · 停用的模型不再被职责推荐
                </span>
              </div>
              <div className="ms-model-list">
                {(selected.models ?? []).length === 0 ? (
                  <div className="ms-empty ms-empty--models">
                    <p>还没有模型清单。</p>
                    <p className="ms-muted">
                      {probeUnsupported
                        ? '嵌入 / 重排供应商没有 /models 端点，请手动添加模型 ID。'
                        : '手动添加，或保存后点「测试连接」让服务端把 /models 里返回的模型带回来。'}
                    </p>
                  </div>
                ) : null}
                {(selected.models ?? []).map((model) => {
                  const bound = rolesForModel(resolutions, selected.provider_id, model.model_id);
                  return (
                    <div className={`ms-model${model.enabled ? '' : ' ms-model--off'}`} key={model.model_id}>
                      <div className="ms-model__main">
                        <code className="ms-model__id">{model.model_id}</code>
                      </div>
                      <div className="ms-model__roles">
                        {bound.length === 0 ? (
                          <span className="ms-muted ms-model__nobind">未绑定职责</span>
                        ) : (
                          bound.map((binding) => (
                            <span className="ms-rolechip" key={binding.profile}>
                              {roleShort(binding.profile)}
                            </span>
                          ))
                        )}
                      </div>
                      <MsSwitch
                        checked={model.enabled}
                        label={`${model.model_id} 启用`}
                        onChange={(value) =>
                          patchModels(
                            (selected.models ?? []).map((item) =>
                              item.model_id === model.model_id ? { ...item, enabled: value } : item,
                            ),
                          )
                        }
                      />
                      <button
                        type="button"
                        className="ms-icon-btn"
                        aria-label={`从清单移除 ${model.model_id}`}
                        title="从清单移除"
                        onClick={() => removeModel(model)}
                      >
                        ×
                      </button>
                    </div>
                  );
                })}
              </div>

              {addingModel ? (
                <div className="ms-model-add">
                  <input
                    className="ms-input"
                    autoFocus
                    value={newModel.model_id}
                    placeholder={modelIdPlaceholder(selected.kind)}
                    aria-label="新模型 ID"
                    onChange={(event) =>
                      setNewModel((current) => ({ ...current, model_id: event.target.value }))
                    }
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') addModel(newModel.model_id);
                      if (event.key === 'Escape') setAddingModel(false);
                    }}
                  />
                  <button
                    type="button"
                    className="ms-btn ms-btn--primary ms-btn--sm"
                    onClick={() => addModel(newModel.model_id)}
                  >
                    添加
                  </button>
                  <button
                    type="button"
                    className="ms-btn ms-btn--sm"
                    onClick={() => {
                      setAddingModel(false);
                      setNewModel({ model_id: '' });
                      setModelError('');
                    }}
                  >
                    取消
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  className="ms-add-model"
                  onClick={() => {
                    setAddingModel(true);
                    setModelError('');
                  }}
                >
                  <PlusIcon size={14} /> 添加模型
                </button>
              )}
              {modelError ? (
                <p className="ms-error-text" role="alert">
                  {modelError}
                </p>
              ) : null}

              {missingFromProbe.length > 0 ? (
                <div className="ms-found">
                  <span className="ms-found__label">
                    检查发现 {missingFromProbe.length} 个清单里还没有的模型
                  </span>
                  <span className="ms-found__items">
                    {missingFromProbe.map((modelId) => (
                      <button
                        type="button"
                        className="ms-found__chip"
                        key={modelId}
                        onClick={() => addModel(modelId)}
                        title={`把 ${modelId} 加入清单`}
                      >
                        + {modelId}
                      </button>
                    ))}
                  </span>
                </div>
              ) : null}

              {(selected.models ?? []).length > 0 && roleCount === 0 ? (
                <p className="ms-hint">
                  {probeUnsupported
                    ? '这一家的模型还没有职责在用。去「职责绑定」把嵌入或重排指过来，它才会真的接活。'
                    : '这一家的模型还没有职责在用。去「职责绑定」把日常、群聊、看图或记忆指过来，它才会真的接活。'}
                </p>
              ) : null}
            </div>

            {/* 高级配置放在**模型下方**：先回答"这家有哪些模型"，
                再回答"这家怎么连、能用多少"。三项都跟着这家供应商走，
                不是跟着某个职责走。 */}
            <details className="ms-adv" open>
              <summary>
                <span>高级配置</span>
                <span className="ms-adv__summary">
                  超时 {selected.timeout_seconds}s · 输出上限{' '}
                  {limitLabel(selected.max_output_tokens)} · 日限额{' '}
                  {limitLabel(selected.daily_token_limit)}
                </span>
              </summary>
              <div className="ms-adv__body">
                <div className="ms-adv__grid">
                  <label className="ms-field">
                    <span className="ms-field__label">超时（秒）</span>
                    <input
                      className="ms-input"
                      type="number"
                      min={1}
                      max={600}
                      value={selected.timeout_seconds}
                      onChange={(event) =>
                        onPatch(selected.provider_id, {
                          timeout_seconds: Number.parseFloat(event.target.value) || 60,
                        })
                      }
                    />
                  </label>
                  <LimitField
                    label="单次输出上限（tokens）"
                    value={selected.max_output_tokens}
                    onChange={(value) =>
                      onPatch(selected.provider_id, { max_output_tokens: value })
                    }
                  />
                  <LimitField
                    label="日限额（tokens）"
                    value={selected.daily_token_limit}
                    onChange={(value) =>
                      onPatch(selected.provider_id, { daily_token_limit: value })
                    }
                  />
                </div>
                <p className="ms-adv__note">
                  超时按秒计；输出上限与日限额填 -1 表示不限制。这三项对这家供应商下的所有模型生效，
                  温度交给模型默认值。
                </p>
                {usage ? (
                  <p className="ms-adv__usage">
                    今日已用 {usage.used_tokens.toLocaleString()} tokens
                    {limitOf(selected.daily_token_limit) !== null
                      ? ` / 上限 ${limitOf(selected.daily_token_limit)!.toLocaleString()}`
                      : ' · 无上限'}
                    {usage.estimated ? '（含估算值：部分调用没拿到供应商回执）' : ''}
                  </p>
                ) : null}
              </div>
            </details>

            {actionError ? (
              <div className="ms-banner ms-banner--error" role="alert">
                <AlertIcon size={14} /> {actionError}
              </div>
            ) : null}

            <footer className="ms-card-foot">
              {/* 底部只留探测结果。测试连接与保存已经提到卡片右上，
                  它们是"对这家做什么"，不该和"上次检查结果"挤在同一行。 */}
              <div className={`ms-probe ms-probe--${probeTone}`}>
                <StatusDot tone={probeTone} />
                {probe ? (
                  <span>
                    {probe.ok ? '连接正常' : '连接失败'}
                    {probe.ok ? ` · ${probe.latency_ms} 毫秒` : ''}
                    {probe.checked_at ? ` · ${formatAgo(probe.checked_at)}` : ''}
                    {!probe.ok && probe.detail ? ` · ${probe.detail}` : ''}
                  </span>
                ) : (
                  <span className="ms-muted">还没有检查过连接</span>
                )}
              </div>
            </footer>
          </section>
        ) : (
          <section className="ms-card">
            <div className="ms-empty">
              <p>还没有供应商。</p>
              <p className="ms-muted">新增一家兼容接口，填上地址与密钥，再给它加模型清单。</p>
            </div>
          </section>
        )}
      </div>

      <details className="ms-run" ref={runRef}>
        <summary>运行设置</summary>
        <div className="ms-run__body">
          <div className="ms-run__row">
            <div>
              <strong>测试模式</strong>
              <p className="ms-muted">
                开启后对话由确定性 fake provider 生成，界面会一直显示「测试模式」横幅。
                它证明链路可用，不代表真实模型能力。
              </p>
            </div>
            <MsSwitch checked={testMode} label="测试模式" onChange={onTestMode} />
          </div>
          <div className="ms-run__row">
            <div>
              <strong>入站拒绝日志</strong>
              <p className="ms-muted">
                白名单拒绝始终生效，这里只决定是否逐条写 WARNING。关掉不影响拒绝，也不影响计数。
              </p>
            </div>
            <MsSwitch checked={logDenials} label="入站拒绝日志" onChange={onLogDenials} />
          </div>
          <p className="ms-hint">这两项和模型改动一起保存、一起生效。</p>
        </div>
      </details>

    </div>
  );
}
