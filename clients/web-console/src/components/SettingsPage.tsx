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
  ProfileResolution,
  ProviderDraftInput,
  QuickConfigPreview,
  SettingsConfig,
  SlotDraftInput,
  SlotView,
  ValidationIssue,
} from '../types';
import { AlertIcon, ArrowLeftIcon, RefreshIcon } from './Icons';
import { ProvidersPanel } from './model-settings/ProvidersPanel';
import { RolesPanel } from './model-settings/RolesPanel';
import {
  MessageBanner,
  missingProfile,
  profileEquals,
  providerEquals,
  toProfileDraft,
  toProviderDraft,
  visibleRoleKeys,
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
  // 今日 token 用量。取不到就是空对象：额度显示是锦上添花，
  // 不该因为这条端点失败而让整个设置页打不开。
  const [usage, setUsage] = useState<
    Record<string, { day: string; used_tokens: number; estimated: boolean }>
  >({});
  const [providers, setProviders] = useState<ProviderDraftInput[]>([]);
  const [profiles, setProfiles] = useState<ProfileDraftInput[]>([]);
  const [slots, setSlots] = useState<SlotDraftInput[]>([]);
  // 「实际生效」由**服务端**算。这里存的是最近一次 `/settings/resolve` 的结果；
  // 前端不根据 profiles/slots 自己推一份，两份算法迟早不一致。
  const [resolutions, setResolutions] = useState<ProfileResolution[]>([]);
  const [slotViews, setSlotViews] = useState<SlotView[]>([]);
  const [quickConfig, setQuickConfig] = useState<QuickConfigPreview | null>(null);
  const [quickConfigBusy, setQuickConfigBusy] = useState(false);
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
        const [nextConfig, nextHealth, nextUsage] = await Promise.all([
          api.settingsConfig(),
          // 健康度是锦上添花：接口失败不该让整页打不开。
          api.settingsHealth().catch(() => null),
          api.settingsUsage().catch(() => null),
        ]);
        setConfig(nextConfig);
        setHealth(nextHealth);
        setUsage(nextUsage?.usage ?? {});
        setResolutions(nextConfig.profile_resolutions ?? []);
        setSlotViews(nextConfig.slots ?? []);
        if (resetDrafts) {
          const drafts = nextConfig.providers.map(toProviderDraft);
          setProviders(drafts);
          setProfiles(nextConfig.profiles.map(toProfileDraft));
          setSlots(
            (nextConfig.slots ?? []).map((slot) => ({
              slot: slot.slot,
              provider_id: slot.provider_id,
              model_id: slot.model_id,
            })),
          );
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

  // 草稿一变就问一次服务端「那现在每个职责会跑哪个模型」。
  //
  // 为什么要问、而不是在本文件里按 profiles/slots 推一遍：回退规则（显式 →
  // 所属槽位 → 不可用，只走一层）只能有一份实现。前端再写一遍，迟早会出现
  // "界面写 A、实际跑 B"，而那种不一致没有任何报错可查。
  //
  // 250ms 防抖：改一个下拉不该立刻打一发请求，但也不能慢到用户以为界面卡住。
  useEffect(() => {
    if (loading) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void api
        .settingsResolve({
          providers: providers.map((provider) => ({
            ...provider,
            models: provider.models ?? [],
          })),
          profiles,
          slots,
        })
        .then((preview) => {
          if (cancelled) return;
          setResolutions(preview.profile_resolutions);
          setSlotViews(preview.slots);
        })
        // 预览失败不用弹窗：已生效的那份仍在，用户按保存时服务端还会再判一次。
        .catch(() => undefined);
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [loading, providers, profiles, slots]);

  const requestQuickConfig = async () => {
    setQuickConfigBusy(true);
    try {
      const preview = await api.quickConfigPreview({
        providers: providers.map((provider) => ({
          ...provider,
          models: provider.models ?? [],
        })),
        profiles,
        slots,
      });
      setQuickConfig(preview);
    } catch (error) {
      const explained = explainError(error);
      setMessage({
        kind: 'error',
        text: `快速配置算不出来：${explained.reason}`,
        detail: explained.detail ?? explained.hint,
      });
    } finally {
      setQuickConfigBusy(false);
    }
  };

  /**
   * 应用快速配置：把服务端返回的那份 profiles 原样写进草稿。
   *
   * 不在前端拼"每条都改成跟随槽位"——那样又是两处算法。服务端已经算好
   * 该写什么，照抄即可。写入后仍需按「保存」才生效（弹窗里也写了这句）。
   */
  const applyQuickConfig = (next: ProfileDraftInput[]) => {
    setProfiles(next.map((item) => ({ ...item })));
    setQuickConfig(null);
  };

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
      slots: (config.slots ?? []).map((slot) => ({
        slot: slot.slot,
        provider_id: slot.provider_id,
        model_id: slot.model_id,
      })),
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
    // 默认槽位和职责在同一个「保存」下生效，所以算在同一笔改动里：
    // 分成两笔会让用户改了槽位、按钮却显示"没有未保存的改动"。
    for (const slot of slots) {
      const base = baseline.slots.find((item) => item.slot === slot.slot);
      if (!base) continue;
      if (base.provider_id !== slot.provider_id || base.model_id !== slot.model_id) {
        count += 1;
      }
    }
    return count;
  }, [baseline, profiles, slots]);

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
      current.map((provider) => {
        if (provider.provider_id !== providerId) return provider;
        const next = { ...provider, ...patch };
        if (patch.kind !== undefined && patch.kind !== provider.kind) {
          // 类型变了，服务端上次告诉我们的能力对新类型不再成立。清空后
          // 按"未知"处理（失败关闭）：保存前不把这家列进职责/槽位下拉，
          // 免得界面拿旧能力做一个可能错的过滤。
          //
          // 改回原类型时把能力恢复回来：否则"A→B→A"看起来没改动（保存
          // 按钮是灰的），但草稿里的能力已经丢了，下拉会一直少这一家。
          const original = baseline?.providers.find(
            (item) => item.provider_id === providerId,
          );
          next.capabilities =
            original && original.kind === patch.kind ? original.capabilities : undefined;
        }
        return next;
      }),
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
        // 服务端还没有这条记录：默认按「跟随槽位」起步（这是改版后的正常
        // 状态），而不是"选了空模型"。
        return [...current, { ...missingProfile(profile), ...patch, profile }];
      }
      return current.map((item) => (item.profile === profile ? { ...item, ...patch } : item));
    });
  };

  const patchSlot = (slot: string, patch: Partial<SlotDraftInput>) => {
    setSlots((current) => {
      if (!current.some((item) => item.slot === slot)) {
        return [
          ...current,
          { slot, provider_id: '', model_id: '', ...patch },
        ];
      }
      return current.map((item) => (item.slot === slot ? { ...item, ...patch } : item));
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
    setSlots(baseline.slots.map((slot) => ({ ...slot })));
  };

  const save = async () => {
    if (!config) return;
    setSaving(true);
    setMessage(null);
    try {
      const draft = await api.saveDraft({
        providers: providers.map((provider) => ({ ...provider, models: provider.models ?? [] })),
        profiles,
        slots,
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
          {/* 数字必须等于**列表行数**（服务端 known_profiles ∩ 分组），
              不是草稿里的绑定记录条数：后者只有 5 条，而列表有 7 行，
              tab 写 5 会让人以为有两条职责没渲染。 */}
          <span className="ms-tab__count">
            {visibleRoleKeys(config?.known_profiles).length}
          </span>
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
          resolutions={resolutions}
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
          usage={usage}
        />
      </div>

      <div className="ms-panel" hidden={tab !== 'roles'}>
        <RolesPanel
          config={config}
          providers={providers}
          profiles={profiles}
          slots={slots}
          resolutions={resolutions}
          slotViews={slotViews}
          quickConfig={quickConfig}
          quickConfigBusy={quickConfigBusy}
          health={health}
          dirtyCount={rolesDirty}
          saving={saving}
          onPatchProfile={patchProfile}
          onPatchSlot={patchSlot}
          onDiscard={discardRoles}
          onSave={() => void save()}
          onRequestQuickConfig={() => void requestQuickConfig()}
          onCloseQuickConfig={() => setQuickConfig(null)}
          onApplyQuickConfig={applyQuickConfig}
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
