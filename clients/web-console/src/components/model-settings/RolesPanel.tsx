/**
 * 「职责绑定」视图：把三件最要紧的事各自指给一个供应商 + 一个模型。
 *
 * 这里刻意不展示「供应商清单」和「密钥」——那是另一页的事。每一行只回答
 * 三个问题：这件事交给谁、那个模型现在能不能用、不能用时点哪里去修。
 */

import type {
  ModelHealthResponse,
  ProfileDraftInput,
  ProviderDraftInput,
  SettingsConfig,
} from '../../types';
import { AlertIcon, CheckIcon } from '../Icons';
import {
  EXTRA_ROLES,
  PRIMARY_ROLES,
  RoleIcon,
  StatusDot,
  type RoleMeta,
} from './shared';

interface Props {
  config: SettingsConfig;
  providers: ProviderDraftInput[];
  profiles: ProfileDraftInput[];
  health: ModelHealthResponse | null;
  dirtyCount: number;
  saving: boolean;
  onPatchProfile: (profile: string, patch: Partial<ProfileDraftInput>) => void;
  onDiscard: () => void;
  onSave: () => void;
  onGoProviders: (providerId: string) => void;
}

type Tone = 'ok' | 'warn' | 'danger' | 'idle';

interface RoleStatus {
  tone: Tone;
  text: string;
  action?: { label: string; providerId: string };
}

function statusOf(
  meta: RoleMeta,
  profile: ProfileDraftInput | undefined,
  providers: ProviderDraftInput[],
  config: SettingsConfig,
  health: ModelHealthResponse | null,
): RoleStatus {
  if (!profile || !profile.provider_id) {
    const why = config.unbound_degradations?.[meta.key];
    return {
      tone: 'warn',
      text: why ? `未配置 · ${why}` : '未配置 · 这件事会走降级路径',
    };
  }
  const provider = providers.find((item) => item.provider_id === profile.provider_id);
  if (!provider) {
    return { tone: 'danger', text: `需要检查：供应商「${profile.provider_id}」已不在清单里` };
  }
  if (!provider.enabled) {
    return {
      tone: 'danger',
      text: `需要检查：供应商「${provider.provider_id}」已停用，这个职责调用时会失败`,
      action: { label: '去启用', providerId: provider.provider_id },
    };
  }
  if (!profile.model_id) {
    return { tone: 'danger', text: '需要检查：还没有选模型' };
  }
  const model = (provider.models ?? []).find((item) => item.model_id === profile.model_id);
  if (!model) {
    return {
      tone: 'warn',
      text: `模型「${profile.model_id}」不在这一家的清单里，仍会按原样调用；建议先加入清单`,
      action: { label: '去清单', providerId: provider.provider_id },
    };
  }
  if (!model.enabled) {
    return {
      tone: 'danger',
      text: `需要检查：模型「${profile.model_id}」已停用，这个职责仍指向它`,
      action: { label: '去清单', providerId: provider.provider_id },
    };
  }
  if (!profile.enabled) {
    return { tone: 'warn', text: '这个职责本身被停用了，不会参与路由' };
  }
  const record = health?.health[`${provider.provider_id}/${profile.model_id}`];
  if (record?.open) {
    return { tone: 'danger', text: '最近连续失败，已暂时熔断；等冷却结束或先检查供应商' };
  }
  if (record && record.consecutive_failures > 0) {
    return {
      tone: 'danger',
      text: `最近一次调用失败，已连续 ${record.consecutive_failures} 次`,
    };
  }
  if (record?.last_latency_ms != null) {
    return { tone: 'ok', text: `最近一次调用正常 · ${record.last_latency_ms} 毫秒` };
  }
  return { tone: 'idle', text: '还没有调用过；第一次对话后这里会显示最近一次的结果' };
}

function NumberField({
  label,
  value,
  onChange,
  min,
  max,
  step,
  placeholder,
}: {
  label: string;
  value: number | null;
  onChange: (value: number | null) => void;
  min?: number;
  max?: number;
  step?: number;
  placeholder?: string;
}) {
  return (
    <label className="ms-field">
      <span className="ms-field__label">{label}</span>
      <input
        className="ms-input"
        type="number"
        min={min}
        max={max}
        step={step}
        value={value ?? ''}
        placeholder={placeholder}
        onChange={(event) => {
          if (event.target.value.trim() === '') {
            onChange(null);
            return;
          }
          const next = Number.parseFloat(event.target.value);
          onChange(Number.isFinite(next) ? next : null);
        }}
      />
    </label>
  );
}

function RoleCard({
  meta,
  profile,
  providers,
  config,
  health,
  compact = false,
  onPatchProfile,
  onGoProviders,
}: {
  meta: RoleMeta;
  profile: ProfileDraftInput | undefined;
  providers: ProviderDraftInput[];
  config: SettingsConfig;
  health: ModelHealthResponse | null;
  compact?: boolean;
  onPatchProfile: (profile: string, patch: Partial<ProfileDraftInput>) => void;
  onGoProviders: (providerId: string) => void;
}) {
  const status = statusOf(meta, profile, providers, config, health);
  const provider = providers.find((item) => item.provider_id === profile?.provider_id);
  const models = (provider?.models ?? []).filter(
    (model) => model.enabled || model.model_id === profile?.model_id,
  );
  const enabledProviders = providers.filter(
    (item) => item.enabled || item.provider_id === profile?.provider_id,
  );
  const fallbackProvider = providers.find(
    (item) => item.provider_id === profile?.fallback_provider_id,
  );
  const setProvider = (providerId: string) => {
    const next = providers.find((item) => item.provider_id === providerId);
    const modelId = (next?.models ?? []).find((model) => model.enabled)?.model_id ?? '';
    onPatchProfile(meta.key, {
      provider_id: providerId,
      model_id: modelId,
      fallback_provider_id: null,
      fallback_model_id: null,
    });
  };

  return (
    <article className={`ms-role${compact ? ' ms-role--compact' : ''}`}>
      <header className="ms-role__head">
        <span className="ms-role__icon" aria-hidden="true">
          <RoleIcon role={meta.key} size={compact ? 14 : 16} />
        </span>
        <div className="ms-role__title">
          <h3>
            {meta.label}
            <code>{meta.key}</code>
          </h3>
          {compact ? null : <p>{meta.hint}</p>}
        </div>
      </header>

      <div className="ms-role__fields">
        <label className="ms-field">
          <span className="ms-field__label">供应商</span>
          <select
            className="ms-input"
            value={profile?.provider_id ?? ''}
            onChange={(event) => setProvider(event.target.value)}
          >
            <option value="">— 选择供应商 —</option>
            {enabledProviders.map((item) => (
              <option key={item.provider_id} value={item.provider_id}>
                {item.provider_id}
                {item.enabled ? '' : '（已停用）'}
              </option>
            ))}
          </select>
        </label>
        <label className="ms-field">
          <span className="ms-field__label">模型</span>
          <select
            className="ms-input"
            value={profile?.model_id ?? ''}
            disabled={!provider}
            onChange={(event) => onPatchProfile(meta.key, { model_id: event.target.value })}
          >
            <option value="">— 选择模型 —</option>
            {models.map((model) => (
              <option key={model.model_id} value={model.model_id}>
                {model.model_id}
                {model.enabled ? '' : '（已停用）'}
              </option>
            ))}
          </select>
        </label>
      </div>

      {!provider || (provider.models ?? []).length === 0 ? (
        <p className="ms-role__empty">
          {provider
            ? `「${provider.provider_id}」还没有模型清单，先到供应商页加一个。`
            : '先选一家供应商，再选模型。'}
          {provider ? (
            <button
              type="button"
              className="ms-link"
              onClick={() => onGoProviders(provider.provider_id)}
            >
              去添加
            </button>
          ) : null}
        </p>
      ) : null}

      <footer className="ms-role__foot">
        <span className={`ms-role__status ms-role__status--${status.tone}`}>
          {status.tone === 'ok' ? (
            <CheckIcon size={13} />
          ) : status.tone === 'idle' ? (
            <StatusDot tone="idle" />
          ) : (
            <AlertIcon size={13} />
          )}
          <span>{status.text}</span>
        </span>
        {status.action ? (
          <button
            type="button"
            className="ms-link"
            onClick={() => onGoProviders(status.action!.providerId)}
          >
            {status.action.label}
          </button>
        ) : null}
      </footer>

      {profile ? (
        <details className="ms-role__advanced">
          <summary>高级参数</summary>
          <div className="ms-role__advanced-body">
            <div className="ms-form-grid ms-form-grid--compact">
              <NumberField
                label="温度"
                value={profile.temperature}
                min={0}
                max={2}
                step={0.1}
                onChange={(value) =>
                  onPatchProfile(meta.key, { temperature: value ?? profile.temperature })
                }
              />
              <NumberField
                label="超时（秒）"
                value={profile.timeout_seconds}
                min={1}
                max={600}
                onChange={(value) =>
                  onPatchProfile(meta.key, {
                    timeout_seconds: value ?? profile.timeout_seconds,
                  })
                }
              />
              <NumberField
                label="最大 tokens"
                value={profile.max_tokens}
                min={1}
                placeholder="不限"
                onChange={(value) => onPatchProfile(meta.key, { max_tokens: value })}
              />
              <NumberField
                label="每日费用上限"
                value={profile.daily_cost_limit}
                min={0}
                step={0.01}
                placeholder="不限"
                onChange={(value) => onPatchProfile(meta.key, { daily_cost_limit: value })}
              />
              <label className="ms-field">
                <span className="ms-field__label">回退供应商</span>
                <select
                  className="ms-input"
                  value={profile.fallback_provider_id ?? ''}
                  onChange={(event) =>
                    onPatchProfile(meta.key, {
                      fallback_provider_id: event.target.value || null,
                      fallback_model_id: event.target.value ? profile.fallback_model_id : null,
                    })
                  }
                >
                  <option value="">无回退</option>
                  {providers.map((item) => (
                    <option key={item.provider_id} value={item.provider_id}>
                      {item.provider_id}
                      {item.enabled ? '' : '（已停用，回退不会生效）'}
                    </option>
                  ))}
                </select>
              </label>
              <label className="ms-field">
                <span className="ms-field__label">回退模型</span>
                <select
                  className="ms-input"
                  value={profile.fallback_model_id ?? ''}
                  disabled={!profile.fallback_provider_id}
                  onChange={(event) =>
                    onPatchProfile(meta.key, { fallback_model_id: event.target.value || null })
                  }
                >
                  <option value="">— 选择 —</option>
                  {(fallbackProvider?.models ?? []).map((model) => (
                    <option key={model.model_id} value={model.model_id}>
                      {model.model_id}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <label className="ms-checkbox">
              <input
                type="checkbox"
                checked={profile.enabled}
                onChange={(event) => onPatchProfile(meta.key, { enabled: event.target.checked })}
              />
              <span>让这个职责参与路由（取消勾选等于临时关掉它）</span>
            </label>
          </div>
        </details>
      ) : null}
    </article>
  );
}

export function RolesPanel({
  config,
  providers,
  profiles,
  health,
  dirtyCount,
  saving,
  onPatchProfile,
  onDiscard,
  onSave,
  onGoProviders,
}: Props) {
  const byProfile = new Map(profiles.map((profile) => [profile.profile, profile]));
  const extras = EXTRA_ROLES.filter((meta) => config.known_profiles.includes(meta.key));

  return (
    <section className="ms-roles" aria-label="职责绑定">
      <header className="ms-roles__head">
        <div>
          <h2>每个职责，各指一个模型</h2>
          <p className="ms-muted">
            没配的职责会走降级路径：她还能聊天，但可能看不见图、记不住太细的事。
            高级参数（温度、回退）收在每一张卡里，默认值通常就够用。
          </p>
        </div>
        <div className="ms-roles__actions">
          {dirtyCount > 0 ? <span className="ms-dirty">改了 {dirtyCount} 处</span> : null}
          <button
            type="button"
            className="ms-btn ms-btn--sm"
            disabled={dirtyCount === 0 || saving}
            onClick={onDiscard}
          >
            还原
          </button>
          <button
            type="button"
            className="ms-btn ms-btn--primary ms-btn--sm"
            disabled={dirtyCount === 0 || saving}
            onClick={onSave}
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </header>

      <div className="ms-role-list">
        {PRIMARY_ROLES.map((meta) => (
          <RoleCard
            key={meta.key}
            meta={meta}
            profile={byProfile.get(meta.key)}
            providers={providers}
            config={config}
            health={health}
            onPatchProfile={onPatchProfile}
            onGoProviders={onGoProviders}
          />
        ))}
      </div>

      {extras.length > 0 ? (
        <details className="ms-extra">
          <summary>
            更多职责
            <span className="ms-muted">群聊、记忆提炼；不配也能用，只是会退到主对话模型</span>
          </summary>
          <div className="ms-role-list">
            {extras.map((meta) => (
              <RoleCard
                key={meta.key}
                meta={meta}
                profile={byProfile.get(meta.key)}
                providers={providers}
                config={config}
                health={health}
                compact
                onPatchProfile={onPatchProfile}
                onGoProviders={onGoProviders}
              />
            ))}
          </div>
        </details>
      ) : null}
    </section>
  );
}
