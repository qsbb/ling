/**
 * 「职责绑定」视图：五个职责一屏看全，点哪行就在原地展开编辑。
 *
 * ## 为什么是分组列表而不是卡片
 *
 * 职责只有五个，但卡片式（三个大卡 + 两个折叠）会让"还有哪些没配"这件事
 * 需要翻两处才能答上来。分组列表把五行摊在同一屏，状态点直接标在行尾——
 * 扫一眼就知道哪条断了。展开编辑时不跳页、不弹窗，其余四行的状态仍在视野里。
 *
 * ## 为什么这里没有温度 / 输出上限 / 超时 / 日额度
 *
 * 那四项属于**供应商**（"这家怎么连、这家一天能用多少"），已经挪到
 * 供应商页的「高级配置」。留在职责里的话，同一家供应商下的五个职责各有
 * 一份，改一次要改五处，而默认值还会悄悄盖掉供应商的设置。
 *
 * 温度更进一步：它已经不是可配置项了，请求里不发送这个字段，用上游默认值。
 */

import type {
  ModelHealthResponse,
  ProfileDraftInput,
  ProviderDraftInput,
  SettingsConfig,
} from '../../types';
import { AlertIcon, CheckIcon } from '../Icons';
import { EXTRA_ROLES, PRIMARY_ROLES, MsSwitch, RoleIcon, StatusDot, type RoleMeta } from './shared';

interface Props {
  config: SettingsConfig;
  providers: ProviderDraftInput[];
  profiles: ProfileDraftInput[];
  health: ModelHealthResponse | null;
  dirtyCount: number;
  saving: boolean;
  /** 当前展开的职责 key；null = 全部收起。 */
  openRole: string | null;
  onToggleRole: (profile: string) => void;
  onPatchProfile: (profile: string, patch: Partial<ProfileDraftInput>) => void;
  onDiscard: () => void;
  onSave: () => void;
  onGoProviders: (providerId: string) => void;
}

type Tone = 'ok' | 'warn' | 'danger' | 'idle';

interface RoleStatus {
  tone: Tone;
  /** 行尾的结论，越短越好——它要和"供应商 · 模型"挤在同一行。 */
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
    // 摘要列已经写了"未绑定"，这里只补**后果**。两边都写"未绑定"会读成
    // "未绑定 · 未绑定 · 看不到图片"——像是两个不同的状态。
    const why = config.unbound_degradations?.[meta.key];
    return { tone: 'warn', text: why ?? '会走降级路径' };
  }
  const provider = providers.find((item) => item.provider_id === profile.provider_id);
  if (!provider) {
    return { tone: 'danger', text: `供应商「${profile.provider_id}」已不在清单里` };
  }
  if (!provider.enabled) {
    return {
      tone: 'danger',
      text: '供应商已停用',
      action: { label: '去启用', providerId: provider.provider_id },
    };
  }
  if (!profile.model_id) {
    return { tone: 'danger', text: '还没有选模型' };
  }
  const model = (provider.models ?? []).find((item) => item.model_id === profile.model_id);
  if (!model) {
    return {
      tone: 'warn',
      text: '模型不在这一家的清单里',
      action: { label: '去清单', providerId: provider.provider_id },
    };
  }
  if (!model.enabled) {
    return {
      tone: 'danger',
      text: '模型已停用',
      action: { label: '去清单', providerId: provider.provider_id },
    };
  }
  if (!profile.enabled) {
    return { tone: 'warn', text: '这个职责被停用了' };
  }
  const record = health?.health[`${provider.provider_id}/${profile.model_id}`];
  if (record?.open) {
    return { tone: 'danger', text: '连续失败，已暂时熔断' };
  }
  if (record && record.consecutive_failures > 0) {
    return { tone: 'danger', text: `最近一次失败（连续 ${record.consecutive_failures} 次）` };
  }
  if (record?.last_latency_ms != null) {
    return { tone: 'ok', text: `正常 · ${record.last_latency_ms} 毫秒` };
  }
  return { tone: 'idle', text: '还没调用过' };
}

/** 行尾摘要：`供应商 · 模型`，没绑就写"未绑定"。 */
function bindingSummary(profile: ProfileDraftInput | undefined): string {
  if (!profile?.provider_id) return '未绑定';
  return `${profile.provider_id} · ${profile.model_id || '—'}`;
}

function RoleRow({
  meta,
  profile,
  providers,
  config,
  health,
  open,
  onToggle,
  onPatchProfile,
  onGoProviders,
}: {
  meta: RoleMeta;
  profile: ProfileDraftInput | undefined;
  providers: ProviderDraftInput[];
  config: SettingsConfig;
  health: ModelHealthResponse | null;
  open: boolean;
  onToggle: () => void;
  onPatchProfile: (profile: string, patch: Partial<ProfileDraftInput>) => void;
  onGoProviders: (providerId: string) => void;
}) {
  const status = statusOf(meta, profile, providers, config, health);
  const provider = providers.find((item) => item.provider_id === profile?.provider_id);
  // 停用的模型也要留在下拉里：旧绑定可能正指着它，把它藏起来会让用户
  // 以为"没绑"，然后重选一个自己其实想留着的模型。
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
      // 换了供应商，旧的备用组合就不再合理：它会指向另一家的模型 ID。
      fallback_provider_id: null,
      fallback_model_id: null,
    });
  };

  return (
    <div className={`ms-row${open ? ' ms-row--open' : ''}`}>
      <button
        type="button"
        className="ms-row__head"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={`role-panel-${meta.key}`}
      >
        <span className="ms-row__icon" aria-hidden="true">
          <RoleIcon role={meta.key} size={15} />
        </span>
        <span className="ms-row__title">
          <span className="ms-row__name">
            {meta.label} <code>{meta.key}</code>
          </span>
          <span className="ms-row__hint">{meta.hint}</span>
        </span>
        <span className="ms-row__binding">
          {bindingSummary(profile)}
          <span className={`ms-row__status ms-row__status--${status.tone}`}>
            {status.tone === 'ok' ? (
              <CheckIcon size={12} />
            ) : status.tone === 'idle' ? (
              <StatusDot tone="idle" />
            ) : (
              <AlertIcon size={12} />
            )}
            {status.text}
          </span>
        </span>
        <span className="ms-row__chevron" aria-hidden="true">
          ›
        </span>
      </button>

      {open ? (
        <div className="ms-row__body" id={`role-panel-${meta.key}`}>
          <div className="ms-row__fields">
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
            <label className="ms-field">
              <span className="ms-field__label">备用供应商</span>
              <select
                className="ms-input"
                value={profile?.fallback_provider_id ?? ''}
                onChange={(event) =>
                  onPatchProfile(meta.key, {
                    fallback_provider_id: event.target.value || null,
                    fallback_model_id: event.target.value ? profile?.fallback_model_id ?? null : null,
                  })
                }
              >
                <option value="">未设置</option>
                {providers.map((item) => (
                  <option key={item.provider_id} value={item.provider_id}>
                    {item.provider_id}
                    {item.enabled ? '' : '（已停用，回退不会生效）'}
                  </option>
                ))}
              </select>
            </label>
            <label className="ms-field">
              <span className="ms-field__label">备用模型</span>
              <select
                className="ms-input"
                value={profile?.fallback_model_id ?? ''}
                disabled={!profile?.fallback_provider_id}
                onChange={(event) =>
                  onPatchProfile(meta.key, { fallback_model_id: event.target.value || null })
                }
              >
                <option value="">未设置</option>
                {(fallbackProvider?.models ?? []).map((model) => (
                  <option key={model.model_id} value={model.model_id}>
                    {model.model_id}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {!provider || (provider.models ?? []).length === 0 ? (
            <p className="ms-row__empty">
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

          {/* 生成参数去哪了：这是**必须写出来的一句**。
              否则用户会以为"温度/上限/超时"这一版做丢了，然后到处找。 */}
          <p className="ms-row__note">
            生成参数跟随「供应商」页的设置；温度不再单独配置。
          </p>

          <div className="ms-row__foot">
            <span className={`ms-row__detail ms-row__detail--${status.tone}`}>
              {status.tone === 'ok' ? <CheckIcon size={13} /> : status.tone === 'idle' ? <StatusDot tone="idle" /> : <AlertIcon size={13} />}
              <span>{status.text}</span>
              {status.action ? (
                <button
                  type="button"
                  className="ms-link"
                  onClick={() => onGoProviders(status.action!.providerId)}
                >
                  {status.action.label}
                </button>
              ) : null}
            </span>
            <span className="ms-row__switch">
              <span className="ms-field__label">启用</span>
              <MsSwitch
                checked={profile?.enabled ?? true}
                label={`${meta.label} 参与路由`}
                onChange={(value) => onPatchProfile(meta.key, { enabled: value })}
              />
            </span>
            <span className="ms-row__spacer" />
            <button type="button" className="ms-btn ms-btn--sm" onClick={onToggle}>
              完成
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** 分组：行在界面上按"这件事属于哪一类"排，而不是按 profile 名字的字母序。 */
const GROUPS: { title: string; note: string; keys: string[] }[] = [
  { title: '对话', note: '3 个 · 主回复链', keys: ['dialogue.fast', 'dialogue.deep', 'dialogue.group'] },
  { title: '感知', note: '1 个 · 只在发图时触发', keys: ['perception.vision'] },
  { title: '记忆', note: '1 个 · 每轮结束异步跑', keys: ['memory.extract'] },
];

export function RolesPanel({
  config,
  providers,
  profiles,
  health,
  dirtyCount,
  saving,
  openRole,
  onToggleRole,
  onPatchProfile,
  onDiscard,
  onSave,
  onGoProviders,
}: Props) {
  const byProfile = new Map(profiles.map((profile) => [profile.profile, profile]));
  // 只列**真的会生效**的五个职责；不可绑的见服务端的 RESERVED_PROFILES。
  const metaByKey = new Map<string, RoleMeta>(
    [...PRIMARY_ROLES, ...EXTRA_ROLES].map((meta) => [meta.key, meta] as const),
  );
  const unbound = profiles.filter((profile) => !profile.provider_id).length;
  const needsCheck = profiles.filter((profile) => {
    const meta = metaByKey.get(profile.profile);
    if (!meta) return false;
    const status = statusOf(meta, profile, providers, config, health);
    return status.tone === 'danger';
  }).length;
  const bound = profiles.filter((profile) => profile.provider_id).length;

  return (
    <section className="ms-roles" aria-label="职责绑定">
      <header className="ms-roles__head">
        <div>
          <h2>每个职责，各指一个模型</h2>
          <p className="ms-muted">
            五个职责，一行一个。点开就地改，不跳页。没配的会走降级路径：她还能聊天，
            但可能看不见图、记不住太细的事。
          </p>
          <p className="ms-roles__counts">
            <span>5 个职责</span>
            <span>·</span>
            <span>{bound} 个已配置</span>
            {unbound > 0 ? (
              <>
                <span>·</span>
                <span className="ms-roles__counts--warn">{unbound} 个未绑定</span>
              </>
            ) : null}
            {needsCheck > 0 ? (
              <>
                <span>·</span>
                <span className="ms-roles__counts--danger">{needsCheck} 项需要处理</span>
              </>
            ) : null}
          </p>
        </div>
      </header>

      <div className="ms-role-groups">
        {GROUPS.map((group) => {
          const keys = group.keys.filter((key) => metaByKey.has(key));
          if (keys.length === 0) return null;
          return (
            <div className="ms-role-group" key={group.title}>
              <div className="ms-role-group__head">
                <span className="ms-role-group__title">{group.title}</span>
                <span className="ms-role-group__note">{group.note}</span>
              </div>
              {keys.map((key) => {
                const meta = metaByKey.get(key)!;
                return (
                  <RoleRow
                    key={key}
                    meta={meta}
                    profile={byProfile.get(key)}
                    providers={providers}
                    config={config}
                    health={health}
                    open={openRole === key}
                    onToggle={() => onToggleRole(key)}
                    onPatchProfile={onPatchProfile}
                    onGoProviders={onGoProviders}
                  />
                );
              })}
            </div>
          );
        })}
      </div>

      <p className="ms-hint">
        一次只展开一行，其余保持只读。未绑定的两条不报错，只会静默降级：
        群聊退回日常对话，记忆提炼用主对话模型代跑。
      </p>

      <footer className="ms-savebar" role="status">
        {dirtyCount > 0 ? <span className="ms-savebar__dot" aria-hidden="true" /> : null}
        <span>{dirtyCount > 0 ? `有 ${dirtyCount} 处改动未保存` : '没有未保存的改动'}</span>
        <span className="ms-savebar__spacer" />
        <button
          type="button"
          className="ms-btn ms-btn--sm"
          disabled={dirtyCount === 0 || saving}
          onClick={onDiscard}
        >
          撤销
        </button>
        <button
          type="button"
          className="ms-btn ms-btn--primary ms-btn--sm"
          disabled={dirtyCount === 0 || saving}
          onClick={onSave}
        >
          {saving ? '保存中…' : '保存'}
        </button>
      </footer>
    </section>
  );
}
