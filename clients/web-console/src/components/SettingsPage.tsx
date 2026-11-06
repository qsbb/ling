/**
 * 模型设置：两个视图，一份草稿。
 *
 * 旧页面是一张长表单：供应商、职责、发布、修订历史全堆在一起，用户得先理解
 * "草稿 → 发布"才敢改。新页面把问题拆成两句人话：
 *
 *   1. 供应商页：哪几家能用、每家的模型清单是什么；
 *   2. 职责绑定页：日常 / 深度 / 看图片各交给谁。
 *
 * 底下仍然是服务端的草稿 → 校验 → 发布：按「保存」= 保存草稿 + 立即发布，
 * 修订历史与回滚没有消失，只是收进了「⋯」。这样既有审计链，又不用让用户
 * 先学会控制面的术语。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import { formatSessionTime } from '../format';
import type {
  ModelHealthResponse,
  ProfileDraftInput,
  ProviderDraftInput,
  SettingsConfig,
  ValidationIssue,
} from '../types';
import { AlertIcon, ArrowLeftIcon, RefreshIcon } from './Icons';
import { ProvidersPanel } from './model-settings/ProvidersPanel';
import { RolesPanel } from './model-settings/RolesPanel';
import {
  MessageBanner,
  emptyProfile,
  profileEquals,
  providerEquals,
  toProfileDraft,
  toProviderDraft,
  type PanelMessage,
} from './model-settings/shared';
import '../model-settings.css';

interface SettingsPageProps {
  onBack: () => void;
  onTestMode: (value: boolean) => void;
}

type Tab = 'providers' | 'roles';

function blockingErrors(issues: ValidationIssue[]): ValidationIssue[] {
  return issues.filter((issue) => (issue.severity ?? 'error') === 'error');
}

export function SettingsPage({ onBack, onTestMode }: SettingsPageProps) {
  const [config, setConfig] = useState<SettingsConfig | null>(null);
  const [health, setHealth] = useState<ModelHealthResponse | null>(null);
  const [providers, setProviders] = useState<ProviderDraftInput[]>([]);
  const [profiles, setProfiles] = useState<ProfileDraftInput[]>([]);
  const [testMode, setTestMode] = useState(false);
  const [logDenials, setLogDenials] = useState(true);
  const [tab, setTab] = useState<Tab>('providers');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<ExplainedError | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<PanelMessage | null>(null);
  const [issues, setIssues] = useState<ValidationIssue[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [revisionBusy, setRevisionBusy] = useState<number | null>(null);

  const loadConfig = useCallback(
    async (resetDrafts: boolean) => {
      setLoadError(null);
      try {
        const [nextConfig, nextHealth] = await Promise.all([
          api.settingsConfig(),
          // 健康度是锦上添花：接口失败不该让整页打不开。
          api.settingsHealth().catch(() => null),
        ]);
        setConfig(nextConfig);
        setHealth(nextHealth);
        if (resetDrafts) {
          const drafts = nextConfig.providers.map(toProviderDraft);
          setProviders(drafts);
          setProfiles(nextConfig.profiles.map(toProfileDraft));
          setTestMode(nextConfig.test_mode);
          setLogDenials(nextConfig.log_access_denials);
          setSelectedId((current) => {
            if (current && drafts.some((provider) => provider.provider_id === current)) {
              return current;
            }
            return (
              drafts.find((provider) => provider.enabled)?.provider_id ??
              drafts[0]?.provider_id ??
              null
            );
          });
          setIssues([]);
        }
        onTestMode(nextConfig.test_mode);
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

  useEffect(() => {
    if (!historyOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setHistoryOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [historyOpen]);

  const baseline = useMemo(() => {
    if (!config) return null;
    return {
      providers: config.providers.map(toProviderDraft),
      profiles: config.profiles.map(toProfileDraft),
      testMode: config.test_mode,
      logDenials: config.log_access_denials,
    };
  }, [config]);

  const rolesDirty = useMemo(() => {
    if (!baseline) return 0;
    let count = 0;
    for (const profile of profiles) {
      const base = baseline.profiles.find((item) => item.profile === profile.profile);
      if (!base || !profileEquals(profile, base)) count += 1;
    }
    for (const base of baseline.profiles) {
      if (!profiles.some((profile) => profile.profile === base.profile)) count += 1;
    }
    return count;
  }, [baseline, profiles]);

  const providersDirty = useMemo(() => {
    if (!baseline) return 0;
    let count = 0;
    for (const provider of providers) {
      const base = baseline.providers.find((item) => item.provider_id === provider.provider_id);
      if (!base || !providerEquals(provider, base)) count += 1;
    }
    for (const base of baseline.providers) {
      if (!providers.some((provider) => provider.provider_id === base.provider_id)) count += 1;
    }
    if (baseline.testMode !== testMode) count += 1;
    if (baseline.logDenials !== logDenials) count += 1;
    return count;
  }, [baseline, providers, testMode, logDenials]);

  const patchProvider = (providerId: string, patch: Partial<ProviderDraftInput>) => {
    setProviders((current) =>
      current.map((provider) =>
        provider.provider_id === providerId ? { ...provider, ...patch } : provider,
      ),
    );
  };

  const addProvider = (provider: ProviderDraftInput) => {
    setProviders((current) => [...current, provider]);
    setSelectedId(provider.provider_id);
    setTab('providers');
  };

  const patchProfile = (profile: string, patch: Partial<ProfileDraftInput>) => {
    setProfiles((current) => {
      if (!current.some((item) => item.profile === profile)) {
        return [...current, { ...emptyProfile(profile, '', ''), ...patch, profile }];
      }
      return current.map((item) => (item.profile === profile ? { ...item, ...patch } : item));
    });
  };

  const discardProviders = () => {
    if (!baseline) return;
    setProviders(baseline.providers.map((provider) => ({ ...provider, models: [...(provider.models ?? [])] })));
    setTestMode(baseline.testMode);
    setLogDenials(baseline.logDenials);
  };

  const discardRoles = () => {
    if (!baseline) return;
    setProfiles(baseline.profiles.map((profile) => ({ ...profile })));
  };

  const save = async () => {
    if (!config) return;
    setSaving(true);
    setMessage(null);
    try {
      const draft = await api.saveDraft({
        providers: providers.map((provider) => ({ ...provider, models: provider.models ?? [] })),
        profiles,
        test_mode: testMode,
        log_access_denials: logDenials,
      });
      setIssues(draft.validation_errors);
      const blocking = blockingErrors(draft.validation_errors);
      if (blocking.length > 0) {
        setMessage({
          kind: 'error',
          text: `还没有生效：${blocking.length} 处需要先修`,
          detail: blocking
            .slice(0, 5)
            .map((issue) => `${issue.field}：${issue.message}`)
            .join('\n'),
        });
        return;
      }
      const published = await api.publish(draft.revision);
      setMessage({
        kind: 'ok',
        text: `已保存并生效 · 修订 #${published.published_revision}`,
      });
      await loadConfig(true);
    } catch (error) {
      const explained = explainError(error);
      setMessage({
        kind: 'error',
        text: `保存失败：${explained.reason}`,
        detail: explained.detail ?? explained.hint,
      });
    } finally {
      setSaving(false);
    }
  };

  const republish = async (revision: number, mode: 'publish' | 'rollback') => {
    setRevisionBusy(revision);
    setMessage(null);
    try {
      const result =
        mode === 'publish'
          ? await api.publish(revision)
          : await api.rollback(revision);
      setMessage({
        kind: 'ok',
        text: mode === 'publish' ? `已发布修订 #${result.published_revision}` : `已回滚并发布修订 #${result.published_revision}`,
      });
      setHistoryOpen(false);
      await loadConfig(true);
    } catch (error) {
      const explained = explainError(error);
      setMessage({
        kind: 'error',
        text: `${mode === 'publish' ? '发布' : '回滚'}失败：${explained.reason}`,
        detail: explained.detail ?? explained.hint,
      });
    } finally {
      setRevisionBusy(null);
    }
  };

  if (loading) {
    return (
      <div className="page">
        <div className="page__loading">
          <span className="spinner" aria-hidden="true" /> 正在读取模型设置…
        </div>
      </div>
    );
  }

  if (loadError || !config) {
    return (
      <div className="page">
        <button type="button" className="back-link" onClick={onBack}>
          <ArrowLeftIcon /> 返回设置
        </button>
        <div className="load-error" role="alert">
          <div className="load-error__head">
            <AlertIcon />
            <strong>{loadError?.reason ?? '设置读取失败'}</strong>
          </div>
          {loadError?.hint ? <p>{loadError.hint}</p> : null}
          <button type="button" className="button button--small" onClick={() => void loadConfig(true)}>
            <RefreshIcon /> 重新读取
          </button>
        </div>
      </div>
    );
  }

  const warnings = issues.filter((issue) => issue.severity === 'warning');
  const published = config.revisions.find((revision) => revision.state === 'published');

  return (
    <div className="page model-page">
      <header className="ms-head">
        <div className="ms-head__main">
          <button type="button" className="back-link" onClick={onBack}>
            <ArrowLeftIcon /> 返回设置
          </button>
          <h1 className="ms-head__title">模型与生成</h1>
          <p className="ms-head__sub">
            哪些模型可以用，以及每件事交给谁。改完按「保存」一起生效；密钥不会回显。
          </p>
        </div>
        <div className="ms-head__meta">
          <span className="ms-revision">
            当前修订 #{config.revision}
            {published?.published_at ? ` · ${formatSessionTime(published.published_at)}` : ''}
          </span>
          {config.test_mode ? <span className="ms-chip ms-chip--test">测试模式</span> : null}
          <button
            type="button"
            className="ms-icon-btn"
            aria-label="刷新"
            title="重新读取已生效的配置"
            onClick={() => void loadConfig(true)}
          >
            <RefreshIcon size={14} />
          </button>
        </div>
      </header>

      <div className="ms-tabs" role="tablist" aria-label="模型设置">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'providers'}
          className={`ms-tab${tab === 'providers' ? ' ms-tab--active' : ''}`}
          onClick={() => setTab('providers')}
        >
          供应商
          {providersDirty > 0 ? <span className="ms-tab__dot" aria-label="有未保存的改动" /> : null}
          <span className="ms-tab__count">{providers.length}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'roles'}
          className={`ms-tab${tab === 'roles' ? ' ms-tab--active' : ''}`}
          onClick={() => setTab('roles')}
        >
          职责绑定
          <span className="ms-tab__count">{profiles.length}</span>
        </button>
      </div>

      {message ? <MessageBanner message={message} /> : null}

      {issues.length > 0 ? (
        <div className={`ms-issues${warnings.length === issues.length ? ' ms-issues--warn' : ''}`}>
          <strong>
            {warnings.length === issues.length ? '可以保存，但这些地方值得看一眼' : '校验问题'}
          </strong>
          <ul>
            {issues.slice(0, 8).map((issue) => (
              <li key={`${issue.field}:${issue.message}`}>
                <code>{issue.field}</code> {issue.message}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="ms-panel" hidden={tab !== 'providers'}>
        <ProvidersPanel
          config={config}
          providers={providers}
          profiles={profiles}
          selectedId={selectedId}
          saving={saving}
          dirtyCount={providersDirty}
          onSelect={setSelectedId}
          onPatch={patchProvider}
          onAddProvider={addProvider}
          onDiscard={discardProviders}
          onSave={() => void save()}
          onSecretsChanged={() => void loadConfig(false)}
          onShowHistory={() => setHistoryOpen(true)}
          testMode={testMode}
          logDenials={logDenials}
          onTestMode={setTestMode}
          onLogDenials={setLogDenials}
        />
      </div>

      <div className="ms-panel" hidden={tab !== 'roles'}>
        <RolesPanel
          config={config}
          providers={providers}
          profiles={profiles}
          health={health}
          dirtyCount={rolesDirty}
          saving={saving}
          onPatchProfile={patchProfile}
          onDiscard={discardRoles}
          onSave={() => void save()}
          onGoProviders={(providerId) => {
            setSelectedId(providerId);
            setTab('providers');
          }}
        />
      </div>

      <p className="ms-footnote">
        保存会新建一个配置修订并立即发布；改坏了可以在「修订历史」里回滚。
        密钥只存在服务端，接口永远不会把原文回传给浏览器。
      </p>

      {historyOpen ? (
        <div className="ms-modal" role="presentation">
          <div className="ms-modal__backdrop" onClick={() => setHistoryOpen(false)} />
          <div
            className="ms-modal__card"
            role="dialog"
            aria-modal="true"
            aria-label="修订历史"
          >
            <header className="ms-modal__head">
              <div>
                <h2>修订历史</h2>
                <p className="ms-muted">
                  最近 {config.revisions.length} 条。发布一个旧修订 = 回滚，会新建一次发布记录。
                </p>
              </div>
              <button
                type="button"
                className="ms-icon-btn"
                aria-label="关闭"
                onClick={() => setHistoryOpen(false)}
              >
                ×
              </button>
            </header>
            <ul className="ms-revisions">
              {config.revisions.map((revision) => {
                const isCurrent = revision.state === 'published';
                return (
                  <li className="ms-revision-row" key={revision.revision}>
                    <div className="ms-revision-row__main">
                      <strong>#{revision.revision}</strong>
                      <span className={`ms-chip${isCurrent ? ' ms-chip--on' : ''}`}>
                        {isCurrent ? '当前生效' : revision.state}
                      </span>
                      <span className="ms-muted">
                        {formatSessionTime(revision.published_at ?? revision.created_at) || '—'}
                      </span>
                    </div>
                    <div className="ms-revision-row__side">
                      {revision.note ? <span className="ms-revision-row__note">{revision.note}</span> : null}
                      {revision.validation_errors.length > 0 ? (
                        <span className="ms-muted">{revision.validation_errors.length} 个问题</span>
                      ) : null}
                      {!isCurrent ? (
                        <>
                          <button
                            type="button"
                            className="ms-btn ms-btn--sm"
                            disabled={revisionBusy === revision.revision}
                            onClick={() => void republish(revision.revision, 'publish')}
                          >
                            发布
                          </button>
                          <button
                            type="button"
                            className="ms-btn ms-btn--sm"
                            disabled={revisionBusy === revision.revision}
                            onClick={() => void republish(revision.revision, 'rollback')}
                          >
                            回滚到此
                          </button>
                        </>
                      ) : null}
                    </div>
                  </li>
                );
              })}
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}
