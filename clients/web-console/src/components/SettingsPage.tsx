import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import { formatSessionTime } from '../format';
import type {
  DraftResponse,
  ProbeResult,
  ProfileDraftInput,
  ProviderDraftInput,
  SettingsConfig,
  ValidationIssue,
} from '../types';
import { AlertIcon, ArrowLeftIcon, CheckIcon, RefreshIcon } from './Icons';

interface SettingsPageProps {
  onBack: () => void;
  onTestMode: (value: boolean) => void;
}

interface PageMessage {
  kind: 'ok' | 'error';
  text: string;
  detail?: string;
}

function toProfileDraft(profile: SettingsConfig['profiles'][number]): ProfileDraftInput {
  return {
    profile: profile.profile,
    provider_id: profile.provider_id,
    model_id: profile.model_id,
    fallback_provider_id: profile.fallback_provider_id,
    fallback_model_id: profile.fallback_model_id,
    enabled: profile.enabled,
    temperature: profile.temperature,
    max_tokens: profile.max_tokens,
    timeout_seconds: profile.timeout_seconds,
    daily_cost_limit: profile.daily_cost_limit,
  };
}

function toNumber(raw: string, fallback: number): number {
  const value = Number.parseFloat(raw);
  return Number.isFinite(value) ? value : fallback;
}

function optionalNumber(raw: string): number | null {
  if (raw.trim() === '') return null;
  const value = Number.parseFloat(raw);
  return Number.isFinite(value) ? value : null;
}

function Issues({ issues }: { issues: ValidationIssue[] }) {
  if (issues.length === 0) return null;
  return (
    <ul className="issues">
      {issues.map((issue) => (
        <li key={`${issue.field}:${issue.message}`}>
          <code>{issue.field}</code> {issue.message}
        </li>
      ))}
    </ul>
  );
}

export function SettingsPage({ onBack, onTestMode }: SettingsPageProps) {
  const [config, setConfig] = useState<SettingsConfig | null>(null);
  const [providerDrafts, setProviderDrafts] = useState<ProviderDraftInput[]>([]);
  const [profileDrafts, setProfileDrafts] = useState<ProfileDraftInput[]>([]);
  const [testModeDraft, setTestModeDraft] = useState(false);
  const [note, setNote] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<ExplainedError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<PageMessage | null>(null);
  const [draft, setDraft] = useState<DraftResponse | null>(null);
  const [secretInputs, setSecretInputs] = useState<Record<string, string>>({});
  const [probes, setProbes] = useState<Record<string, ProbeResult>>({});
  const [addingProfile, setAddingProfile] = useState('');
  const [newProvider, setNewProvider] = useState({
    provider_id: '',
    kind: 'openai_compatible',
    base_url: 'https://api.openai.com/v1',
  });
  const [showNewProvider, setShowNewProvider] = useState(false);

  const loadConfig = useCallback(
    async (resetDrafts: boolean) => {
      setLoadError(null);
      try {
        const data = await api.settingsConfig();
        setConfig(data);
        setTestModeDraft((current) => (resetDrafts ? data.test_mode : current));
        if (resetDrafts) {
          setProviderDrafts(
            data.providers.map((provider) => ({
              provider_id: provider.provider_id,
              kind: provider.kind,
              base_url: provider.base_url,
              enabled: provider.enabled,
              extra: provider.extra,
            })),
          );
          setProfileDrafts(data.profiles.map(toProfileDraft));
        }
        onTestMode(data.test_mode);
      } catch (error) {
        setLoadError(explainError(error));
      } finally {
        setLoading(false);
      }
    },
    [onTestMode],
  );

  useEffect(() => {
    void loadConfig(true);
  }, [loadConfig]);

  const availableProfiles = useMemo(() => {
    const bound = new Set(profileDrafts.map((profile) => profile.profile));
    return (config?.known_profiles ?? []).filter((profile) => !bound.has(profile));
  }, [config, profileDrafts]);

  const providersForSelect = providerDrafts.map((provider) => provider.provider_id);

  const patchProfile = (profile: string, patch: Partial<ProfileDraftInput>) => {
    setProfileDrafts((prev) =>
      prev.map((item) => (item.profile === profile ? { ...item, ...patch } : item)),
    );
  };

  const patchProvider = (providerId: string, patch: Partial<ProviderDraftInput>) => {
    setProviderDrafts((prev) =>
      prev.map((item) => (item.provider_id === providerId ? { ...item, ...patch } : item)),
    );
  };

  const saveDraft = async () => {
    setBusy('draft');
    setMessage(null);
    try {
      const result = await api.saveDraft({
        providers: providerDrafts,
        profiles: profileDrafts,
        test_mode: testModeDraft,
        note: note.trim() || undefined,
      });
      setDraft(result);
      setMessage(
        result.validation_errors.length === 0
          ? { kind: 'ok', text: `草稿修订 #${result.revision} 已保存，可继续编辑或发布` }
          : {
              kind: 'error',
              text: `草稿修订 #${result.revision} 已保存，但有 ${result.validation_errors.length} 处校验问题`,
            },
      );
      await loadConfig(false);
    } catch (error) {
      const explained = explainError(error);
      setMessage({ kind: 'error', text: explained.reason, detail: explained.detail ?? explained.hint });
    } finally {
      setBusy(null);
    }
  };

  const publish = async (revision: number) => {
    setBusy(`publish-${revision}`);
    setMessage(null);
    try {
      const result = await api.publish(revision);
      setMessage({ kind: 'ok', text: `已发布修订 #${result.published_revision}，后续轮次立即生效` });
      await loadConfig(false);
    } catch (error) {
      const explained = explainError(error);
      setMessage({
        kind: 'error',
        text: `发布失败：${explained.reason}`,
        detail: explained.detail ?? explained.hint,
      });
    } finally {
      setBusy(null);
    }
  };

  const rollback = async (revision: number) => {
    if (!window.confirm(`把修订 #${revision} 重新发布为当前生效配置？`)) return;
    setBusy(`rollback-${revision}`);
    setMessage(null);
    try {
      const result = await api.rollback(revision);
      setMessage({ kind: 'ok', text: `已回滚并发布修订 #${result.published_revision}` });
      await loadConfig(false);
    } catch (error) {
      const explained = explainError(error);
      setMessage({
        kind: 'error',
        text: `回滚失败：${explained.reason}`,
        detail: explained.detail ?? explained.hint,
      });
    } finally {
      setBusy(null);
    }
  };

  const saveSecret = async (providerId: string) => {
    const secret = (secretInputs[providerId] ?? '').trim();
    if (!secret) return;
    setBusy(`secret-${providerId}`);
    setMessage(null);
    try {
      const status = await api.setSecret(providerId, secret);
      setSecretInputs((prev) => ({ ...prev, [providerId]: '' }));
      setMessage({ kind: 'ok', text: `${providerId} 密钥已保存（尾号 ${status.last4}，原文不再回显）` });
      await loadConfig(false);
    } catch (error) {
      const explained = explainError(error);
      setMessage({ kind: 'error', text: `密钥保存失败：${explained.reason}` });
    } finally {
      setBusy(null);
    }
  };

  const deleteSecret = async (providerId: string) => {
    if (!window.confirm(`删除 ${providerId} 已保存的密钥？`)) return;
    setBusy(`secret-${providerId}`);
    setMessage(null);
    try {
      await api.deleteSecret(providerId);
      setMessage({ kind: 'ok', text: `${providerId} 密钥已删除` });
      await loadConfig(false);
    } catch (error) {
      const explained = explainError(error);
      setMessage({ kind: 'error', text: `删除失败：${explained.reason}` });
    } finally {
      setBusy(null);
    }
  };

  const probe = async (providerId: string) => {
    setBusy(`probe-${providerId}`);
    try {
      const result = await api.probeProvider(providerId);
      setProbes((prev) => ({ ...prev, [providerId]: result }));
    } catch (error) {
      const explained = explainError(error);
      setProbes((prev) => ({
        ...prev,
        [providerId]: {
          provider_id: providerId,
          ok: false,
          detail: explained.detail ?? explained.reason,
          latency_ms: 0,
          models: [],
        },
      }));
    } finally {
      setBusy(null);
    }
  };

  const addProvider = () => {
    const providerId = newProvider.provider_id.trim();
    if (!providerId) return;
    if (providerDrafts.some((provider) => provider.provider_id === providerId)) {
      setMessage({ kind: 'error', text: `供应商 ${providerId} 已存在` });
      return;
    }
    setProviderDrafts((prev) => [
      ...prev,
      {
        provider_id: providerId,
        kind: newProvider.kind,
        base_url: newProvider.base_url.trim() || null,
        enabled: true,
        extra: {},
      },
    ]);
    setShowNewProvider(false);
    setNewProvider({ provider_id: '', kind: 'openai_compatible', base_url: 'https://api.openai.com/v1' });
    setMessage({ kind: 'ok', text: `已添加 ${providerId} 到草稿，记得保存草稿并保存密钥` });
  };

  const addProfile = () => {
    if (!addingProfile) return;
    const providerId = providersForSelect[0] ?? '';
    setProfileDrafts((prev) => [
      ...prev,
      {
        profile: addingProfile,
        provider_id: providerId,
        model_id: '',
        fallback_provider_id: null,
        fallback_model_id: null,
        enabled: true,
        temperature: 0.8,
        max_tokens: null,
        timeout_seconds: 60,
        daily_cost_limit: null,
      },
    ]);
    setAddingProfile('');
  };

  const publishedRevision = config && config.revisions.find((revision) => revision.state === 'published');

  if (loading) {
    return (
      <div className="page">
        <div className="page__loading">
          <span className="spinner" aria-hidden="true" /> 正在读取设置…
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
          <button type="button" className="button button--small" onClick={() => void loadConfig(true)}>
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
          <h1 className="page__title">模型设置</h1>
          <p className="page__subtitle">
            唯一权威设置在服务端；浏览器不保存密钥。当前生效修订 #{config?.revision ?? 0}
            {publishedRevision ? `（${publishedRevision.state}）` : ''}
          </p>
        </div>
        <button type="button" className="button button--small" onClick={() => void loadConfig(false)}>
          <RefreshIcon /> 刷新
        </button>
      </div>

      {message ? (
        <div className={`banner banner--${message.kind}`} role="status">
          <div>{message.text}</div>
          {message.detail ? <div className="banner__detail">{message.detail}</div> : null}
        </div>
      ) : null}

      <section className="panel">
        <div className="panel__head">
          <h2>测试模式</h2>
          <label className="switch">
            <input
              type="checkbox"
              checked={testModeDraft}
              onChange={(event) => setTestModeDraft(event.target.checked)}
            />
            <span>{testModeDraft ? '开启（fake provider）' : '关闭（需绑定真实供应商）'}</span>
          </label>
        </div>
        <p className="panel__note">
          测试模式下对话由确定性 fake provider 生成，界面会持续显示“测试模式”横幅；
          它证明链路可用，不代表真实模型能力。
        </p>
      </section>

      <section className="panel">
        <div className="panel__head">
          <h2>职责绑定</h2>
          <span className="panel__count">{profileDrafts.length} 项</span>
        </div>
        <p className="panel__note">
          按职责选择模型：对话、记忆、语音等各自绑定。校验与发布由服务端确定性代码完成。
        </p>

        {/* 未绑定职责的降级后果。系统不会因此报错，会走降级路径——
            看起来一切正常，只是能力悄悄变窄。不提示的话，用户只会觉得
            "她好像记不住事"，无从判断是没配模型。 */}
        {Object.keys(config?.unbound_degradations ?? {}).length > 0 ? (
          <div className="banner banner--warn" data-testid="unbound-warning">
            <AlertIcon />
            <div>
              <strong>这些职责还没绑定模型，对应能力会降级</strong>
              <ul className="banner__list">
                {Object.entries(config?.unbound_degradations ?? {}).map(([profile, why]) => (
                  <li key={profile}>
                    <code>{profile}</code>：{why}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        ) : null}

        <div className="profiles">
          {profileDrafts.map((profile) => (
            <article className="profile-card" key={profile.profile}>
              <header className="profile-card__head">
                <code className="profile-card__name">{profile.profile}</code>
                <label className="switch switch--small">
                  <input
                    type="checkbox"
                    checked={profile.enabled}
                    onChange={(event) => patchProfile(profile.profile, { enabled: event.target.checked })}
                  />
                  <span>{profile.enabled ? '启用' : '停用'}</span>
                </label>
              </header>
              <div className="form-grid">
                <label className="field">
                  <span className="field__label">供应商</span>
                  <select
                    className="field__input"
                    value={profile.provider_id}
                    onChange={(event) =>
                      patchProfile(profile.profile, { provider_id: event.target.value })
                    }
                  >
                    <option value="">— 选择 —</option>
                    {providersForSelect.map((providerId) => (
                      <option key={providerId} value={providerId}>
                        {providerId}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span className="field__label">模型 ID</span>
                  <input
                    className="field__input"
                    value={profile.model_id}
                    onChange={(event) => patchProfile(profile.profile, { model_id: event.target.value })}
                  />
                </label>
                <label className="field">
                  <span className="field__label">温度</span>
                  <input
                    className="field__input"
                    type="number"
                    step="0.1"
                    min="0"
                    max="2"
                    value={profile.temperature}
                    onChange={(event) =>
                      patchProfile(profile.profile, {
                        temperature: toNumber(event.target.value, profile.temperature),
                      })
                    }
                  />
                </label>
                <label className="field">
                  <span className="field__label">超时（秒）</span>
                  <input
                    className="field__input"
                    type="number"
                    min="1"
                    max="600"
                    value={profile.timeout_seconds}
                    onChange={(event) =>
                      patchProfile(profile.profile, {
                        timeout_seconds: toNumber(event.target.value, profile.timeout_seconds),
                      })
                    }
                  />
                </label>
                <label className="field">
                  <span className="field__label">最大 tokens</span>
                  <input
                    className="field__input"
                    type="number"
                    min="1"
                    value={profile.max_tokens ?? ''}
                    placeholder="不限"
                    onChange={(event) =>
                      patchProfile(profile.profile, { max_tokens: optionalNumber(event.target.value) })
                    }
                  />
                </label>
                <label className="field">
                  <span className="field__label">每日费用上限</span>
                  <input
                    className="field__input"
                    type="number"
                    min="0"
                    step="0.01"
                    value={profile.daily_cost_limit ?? ''}
                    placeholder="不限"
                    onChange={(event) =>
                      patchProfile(profile.profile, {
                        daily_cost_limit: optionalNumber(event.target.value),
                      })
                    }
                  />
                </label>
                <label className="field">
                  <span className="field__label">回退供应商</span>
                  <select
                    className="field__input"
                    value={profile.fallback_provider_id ?? ''}
                    onChange={(event) =>
                      patchProfile(profile.profile, {
                        fallback_provider_id: event.target.value || null,
                        fallback_model_id: event.target.value ? profile.fallback_model_id : null,
                      })
                    }
                  >
                    <option value="">无回退</option>
                    {providersForSelect.map((providerId) => (
                      <option key={providerId} value={providerId}>
                        {providerId}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span className="field__label">回退模型 ID</span>
                  <input
                    className="field__input"
                    value={profile.fallback_model_id ?? ''}
                    disabled={!profile.fallback_provider_id}
                    onChange={(event) =>
                      patchProfile(profile.profile, {
                        fallback_model_id: event.target.value || null,
                      })
                    }
                  />
                </label>
              </div>
            </article>
          ))}
        </div>

        <div className="inline-form">
          <select
            className="field__input"
            value={addingProfile}
            onChange={(event) => setAddingProfile(event.target.value)}
            aria-label="选择要新增的职责"
          >
            <option value="">— 添加职责绑定 —</option>
            {availableProfiles.map((profile) => (
              <option key={profile} value={profile}>
                {profile}
              </option>
            ))}
          </select>
          <button type="button" className="button button--small" onClick={addProfile} disabled={!addingProfile}>
            添加
          </button>
        </div>
      </section>

      <section className="panel">
        <div className="panel__head">
          <h2>供应商</h2>
          <button
            type="button"
            className="button button--small"
            onClick={() => setShowNewProvider((value) => !value)}
          >
            {showNewProvider ? '收起' : '添加供应商'}
          </button>
        </div>
        <p className="panel__note">
          密钥只保存在服务端并加密存储；这里永远只显示后四位与指纹，输入框绝不会回填已存值。
        </p>

        {showNewProvider ? (
          <div className="inline-form inline-form--wrap">
            <label className="field field--compact">
              <span className="field__label">provider_id</span>
              <input
                className="field__input"
                value={newProvider.provider_id}
                onChange={(event) =>
                  setNewProvider((prev) => ({ ...prev, provider_id: event.target.value }))
                }
                placeholder="例如 openai"
              />
            </label>
            <label className="field field--compact">
              <span className="field__label">类型</span>
              <select
                className="field__input"
                value={newProvider.kind}
                onChange={(event) => setNewProvider((prev) => ({ ...prev, kind: event.target.value }))}
              >
                {(config?.known_kinds ?? ['fake', 'openai_compatible', 'local']).map((kind) => (
                  <option key={kind} value={kind}>
                    {kind}
                  </option>
                ))}
              </select>
            </label>
            <label className="field field--compact field--wide">
              <span className="field__label">Base URL</span>
              <input
                className="field__input"
                value={newProvider.base_url}
                onChange={(event) =>
                  setNewProvider((prev) => ({ ...prev, base_url: event.target.value }))
                }
                placeholder="https://api.openai.com/v1"
              />
            </label>
            <button type="button" className="button button--small button--primary" onClick={addProvider}>
              加入草稿
            </button>
          </div>
        ) : null}

        <div className="providers">
          {providerDrafts.map((provider) => {
            const saved = config?.providers.find((item) => item.provider_id === provider.provider_id);
            const probeResult = probes[provider.provider_id];
            return (
              <article className="provider" key={provider.provider_id}>
                <header className="provider__head">
                  <div>
                    <code className="provider__id">{provider.provider_id}</code>
                    <span className="provider__kind">{provider.kind}</span>
                  </div>
                  <label className="switch switch--small">
                    <input
                      type="checkbox"
                      checked={provider.enabled}
                      onChange={(event) =>
                        patchProvider(provider.provider_id, { enabled: event.target.checked })
                      }
                    />
                    <span>{provider.enabled ? '启用' : '停用'}</span>
                  </label>
                </header>

                <div className="form-grid">
                  <label className="field">
                    <span className="field__label">类型</span>
                    <select
                      className="field__input"
                      value={provider.kind}
                      onChange={(event) =>
                        patchProvider(provider.provider_id, { kind: event.target.value })
                      }
                    >
                      {(config?.known_kinds ?? ['fake', 'openai_compatible', 'local']).map((kind) => (
                        <option key={kind} value={kind}>
                          {kind}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field field--wide">
                    <span className="field__label">Base URL</span>
                    <input
                      className="field__input"
                      value={provider.base_url ?? ''}
                      placeholder={provider.kind === 'fake' ? 'fake 不需要' : 'https://...'}
                      onChange={(event) =>
                        patchProvider(provider.provider_id, { base_url: event.target.value || null })
                      }
                    />
                  </label>
                </div>

                <div className="secret">
                  <div className="secret__status">
                    {saved?.has_secret ? (
                      <span className="secret__saved">
                        <CheckIcon size={13} /> 已保存 · 尾号 {saved.secret_last4}
                        {saved.secret_fingerprint ? (
                          <code className="secret__fingerprint">{saved.secret_fingerprint.slice(0, 16)}</code>
                        ) : null}
                      </span>
                    ) : (
                      <span className="secret__missing">未保存密钥</span>
                    )}
                  </div>
                  <div className="secret__row">
                    <input
                      className="field__input"
                      type="password"
                      autoComplete="new-password"
                      placeholder="粘贴新密钥（保存后只显示尾号，页面不回显）"
                      aria-label={`${provider.provider_id} 密钥`}
                      value={secretInputs[provider.provider_id] ?? ''}
                      onChange={(event) =>
                        setSecretInputs((prev) => ({
                          ...prev,
                          [provider.provider_id]: event.target.value,
                        }))
                      }
                    />
                    <button
                      type="button"
                      className="button button--small button--primary"
                      disabled={
                        busy === `secret-${provider.provider_id}` ||
                        !(secretInputs[provider.provider_id] ?? '').trim()
                      }
                      onClick={() => void saveSecret(provider.provider_id)}
                    >
                      保存密钥
                    </button>
                    <button
                      type="button"
                      className="button button--small"
                      disabled={!saved?.has_secret || busy === `secret-${provider.provider_id}`}
                      onClick={() => void deleteSecret(provider.provider_id)}
                    >
                      删除
                    </button>
                    <button
                      type="button"
                      className="button button--small"
                      disabled={busy === `probe-${provider.provider_id}`}
                      onClick={() => void probe(provider.provider_id)}
                    >
                      {busy === `probe-${provider.provider_id}` ? '检查中…' : '连接检查'}
                    </button>
                  </div>
                  {probeResult ? (
                    <div className={`probe probe--${probeResult.ok ? 'ok' : 'error'}`}>
                      <strong>{probeResult.ok ? '连接正常' : '连接失败'}</strong>
                      <span>{probeResult.detail}</span>
                      <span>{probeResult.latency_ms} ms</span>
                      {probeResult.models.length > 0 ? (
                        <code>{probeResult.models.slice(0, 6).join(', ')}</code>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </article>
            );
          })}
        </div>
      </section>

      <section className="panel">
        <div className="panel__head">
          <h2>发布</h2>
        </div>
        <label className="field">
          <span className="field__label">修订备注（可选）</span>
          <input
            className="field__input"
            value={note}
            maxLength={200}
            placeholder="例如：把 dialogue.fast 切到 openai"
            onChange={(event) => setNote(event.target.value)}
          />
        </label>
        <div className="panel__actions">
          <button
            type="button"
            className="button button--primary"
            onClick={() => void saveDraft()}
            disabled={busy === 'draft'}
          >
            {busy === 'draft' ? '保存中…' : '保存草稿'}
          </button>
          {draft ? (
            <>
              <span className="panel__hint">
                草稿修订 #{draft.revision} · {draft.validation_errors.length} 个校验问题
              </span>
              <button
                type="button"
                className="button"
                onClick={() => void publish(draft.revision)}
                disabled={busy === `publish-${draft.revision}` || draft.validation_errors.length > 0}
              >
                发布草稿 #{draft.revision}
              </button>
            </>
          ) : null}
        </div>
        {draft && draft.validation_errors.length > 0 ? <Issues issues={draft.validation_errors} /> : null}
      </section>

      <section className="panel">
        <div className="panel__head">
          <h2>修订历史</h2>
          <span className="panel__count">最近 {config?.revisions.length ?? 0} 条</span>
        </div>
        <ul className="revisions">
          {(config?.revisions ?? []).map((revision) => (
            <li className="revision" key={revision.revision}>
              <div className="revision__main">
                <span className="revision__number">#{revision.revision}</span>
                <span className={`badge badge--${revision.state}`}>{revision.state}</span>
                <span className="revision__time">
                  {formatSessionTime(revision.published_at ?? revision.created_at) || '—'}
                </span>
              </div>
              <div className="revision__side">
                {revision.note ? <span className="revision__note">{revision.note}</span> : null}
                {revision.validation_errors.length > 0 ? (
                  <span className="revision__issues">{revision.validation_errors.length} 个问题</span>
                ) : null}
                {revision.state !== 'published' ? (
                  <>
                    <button
                      type="button"
                      className="button button--small"
                      disabled={busy === `publish-${revision.revision}`}
                      onClick={() => void publish(revision.revision)}
                    >
                      发布
                    </button>
                    <button
                      type="button"
                      className="button button--small"
                      disabled={busy === `rollback-${revision.revision}`}
                      onClick={() => void rollback(revision.revision)}
                    >
                      回滚到此
                    </button>
                  </>
                ) : (
                  <span className="revision__current">当前生效</span>
                )}
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
