/**
 * 「模型与生成」：五个默认槽位 + 每个职责一行。
 *
 * ## 为什么是「一行一个下拉」（A 方案）
 *
 * 用户的原话是"方便选模型"。所以主操作就是**挑一个模型**：点开下拉，
 * 看到的就是模型清单，没有中间概念（不用先把职责指到槽位）。
 * 「跟随默认」只是下拉里的**第一项**，不是另一套页面。
 *
 * ## 界面上的三种来源，必须分得开
 *
 * - 实线 + 橄榄绿「单独指定」= 你亲手选的
 * - 虚线 + 灰「继承」= 没单独选，跟着默认槽位走
 * - 红底「已回退」= 原来选的模型不可用了，暂时由槽位顶着
 *
 * 三者都来自**服务端**的 `profile_resolutions`，前端一个字节都不推。
 * 自己推一份的话，界面说 A、实际跑 B 这类问题永远查不出来。
 *
 * ## 行尾为什么固定留一列「实际生效」
 *
 * 不管配置怎么来的，用户永远能在同一列看到真正会跑的模型。只写"跟随默认"
 * 是不够的——他要的是"到底是谁"。
 */

import { useMemo } from 'react';
import type {
  MemorySource,
  ModelHealthResponse,
  ProfileDraftInput,
  ProfileResolution,
  ProviderDraftInput,
  QuickConfigPreview,
  SettingsConfig,
  SlotDraftInput,
  SlotView,
} from '../../types';
import { AlertIcon, CheckIcon } from '../Icons';
import {
  EXTRA_ROLES,
  PRIMARY_ROLES,
  ROLE_GROUPS,
  RoleIcon,
  bindingCapabilityMismatch,
  capabilityLabel,
  effectiveNote,
  groupSlotOptions,
  kindLabel,
  parseSlotSelectValue,
  providersForProfile,
  roleBadgeState,
  roleLabel,
  slotDraftDirty,
  slotLabel,
  slotOptions,
  slotSelectValue,
  visibleRoleKeys,
  type RoleBadgeState,
  type RoleMeta,
} from './shared';

interface Props {
  config: SettingsConfig;
  providers: ProviderDraftInput[];
  profiles: ProfileDraftInput[];
  slots: SlotDraftInput[];
  /** 服务端算好的「实际生效」。改草稿时会重新请求，见 SettingsPage。 */
  resolutions: ProfileResolution[];
  slotViews: SlotView[];
  quickConfig: QuickConfigPreview | null;
  quickConfigBusy: boolean;
  /** 熔断/延迟来自 `/settings/health`：一个指到坏供应商的职责要看得见。 */
  health: ModelHealthResponse | null;
  dirtyCount: number;
  saving: boolean;
  onPatchProfile: (profile: string, patch: Partial<ProfileDraftInput>) => void;
  onPatchSlot: (slot: string, patch: Partial<SlotDraftInput>) => void;
  onDiscard: () => void;
  onSave: () => void;
  onRequestQuickConfig: () => void;
  onCloseQuickConfig: () => void;
  onApplyQuickConfig: (profiles: ProfileDraftInput[]) => void;
}

/**
 * 下拉里「跟随默认」这一项的值。
 *
 * 用一个不可能和 `pid::mid` 撞的字符串，而不是空串：空串在 `<select>` 里
 * 天然表示"没选"，把它当成一个**有意义的选项**会让"没选"和"跟随"混在一起。
 */
export const FOLLOW_SLOT = '__follow_slot__';

/** 草稿 → 下拉的选中值。 */
export function roleSelectValue(profile: ProfileDraftInput | undefined): string {
  if (!profile || profile.mode === 'slot') return FOLLOW_SLOT;
  return `${profile.provider_id}::${profile.model_id}`;
}

/**
 * 下拉的选中值 → 草稿补丁。
 *
 * `null` = 选了「跟随默认」（要把单独指定清掉）。模型 ID 里本身可能含
 * `::`，所以只按**第一个**分隔符切，而不是 `split('::')`。
 */
export function parseRoleSelectValue(
  value: string,
): { providerId: string; modelId: string } | null {
  if (value === FOLLOW_SLOT) return null;
  const index = value.indexOf('::');
  if (index < 0) return null;
  return { providerId: value.slice(0, index), modelId: value.slice(index + 2) };
}

const STATE_LABEL: Record<string, string> = {
  explicit: '单独指定',
  inherit: '继承',
  fell_back: '已回退',
  legacy: '旧版兼容',
  unavailable: '不可用',
  // 不是"没配"：环境变量正在提供这项能力，只是不在网页绑定里。
  env: '环境变量',
};

/* ------------------------------------------------------------------ 槽位 */

/**
 * 一个默认槽位：**点开就能换模型**（没有"更换默认"编辑门）。
 *
 * 选中值是**结构化的 `(provider_id, model_id)` 对**（`provider::model`，
 * 只按第一个 `::` 切）。为什么不只存模型名：同一个模型名可以由两家供应商
 * 提供，按名字反查供应商在"同名"时会静默选错另一家。
 *
 * 形态照职责行：一个下拉，`供应商 · 协议` 是不可选的分组头，组下是模型名。
 * 当前项前面打勾并带 `aria-selected`。选中只改这一个槽位的草稿，
 * 保存/放弃仍走全页底部那一对按钮。
 *
 * 边界：
 * - 没有供应商、或没有任何**启用**的对话模型 → 选择器禁用并写「先添加模型」，
 *   页面照常可用（不是白屏，也不是假装能选）；
 * - 历史绑了已停用的模型/不提供 chat 的供应商 → 选项保留显示、但 `disabled`
 *   不可新选，并在卡片里说明怎么换；
 * - 服务端 `resolve` 还没回来时只说「等待服务端确认」，**不**把前端推算值
 *   冒充「实际生效」。
 */
function SlotCard({
  view,
  saved,
  providers,
  slotDraft,
  saving,
  onPatch,
}: {
  view: SlotView;
  /** 服务端**已保存**的这一行（`/settings/config` 的 slots）。用来标"未保存"。 */
  saved: SlotView | undefined;
  providers: ProviderDraftInput[];
  slotDraft: SlotDraftInput | undefined;
  saving: boolean;
  onPatch: (patch: Partial<SlotDraftInput>) => void;
}) {
  const providerId = slotDraft?.provider_id ?? view.provider_id;
  const modelId = slotDraft?.model_id ?? view.model_id;
  const provider = providers.find((item) => item.provider_id === providerId);
  const selected = slotSelectValue(providerId, modelId);

  // 选项与"能不能选"的判断全在 `shared.slotOptions`（纯函数、有单测）：
  // 默认槽位只收**对话**供应商，历史不匹配/已停用的项保留显示但不可新选。
  const { options, selectableCount, emptyHint } = slotOptions(
    providers,
    providerId,
    modelId,
  );
  const groups = groupSlotOptions(options);
  const canPick = selectableCount > 0;
  const providerMismatch = bindingCapabilityMismatch(provider, 'chat');

  // 草稿 vs 已保存的槽位：只用来显示"未保存"，不用来推算生效模型。
  const dirty = slotDraftDirty(slotDraft, saved);
  // 服务端对**当前草稿**的结论（resolve 会重算 slots）。草稿刚改、
  // 服务端还没回来时两边不一致 → 只提示等待，不显示任何前端推算值。
  const awaitingServer = providerId !== view.provider_id || modelId !== view.model_id;
  const roles = view.roles.map(roleLabel);

  return (
    <div className={`ms-slot${providerId ? ' ms-slot--set' : ''}`}>
      <div className="ms-slot__top">
        <span className="ms-slot__name">
          {view.label}
          {dirty ? <span className="ms-slot__dirty">未保存</span> : null}
        </span>
        <span className="ms-slot__hint">{view.hint}</span>
      </div>

      <select
        className="ms-input ms-select ms-slot__select"
        aria-label={`${view.label} 槽位用哪个模型`}
        value={selected}
        disabled={!canPick || saving}
        onChange={(event) => {
          const picked = parseSlotSelectValue(event.target.value);
          // 空值 = 不指定（把槽位清回"没配"）。选中只改这一个槽位的草稿。
          onPatch(
            picked
              ? { provider_id: picked.providerId, model_id: picked.modelId }
              : { provider_id: '', model_id: '' },
          );
        }}
      >
        <option value="">{canPick ? '— 选择模型 —' : '先添加模型'}</option>
        {groups.map((group) => (
          <optgroup key={group.providerId} label={group.label}>
            {group.items.map((item) => (
              <option
                key={item.value}
                value={item.value}
                disabled={!item.selectable}
                aria-selected={item.value === selected}
              >
                {item.value === selected ? '✓ ' : ''}
                {item.model_id}
                {item.model_enabled ? '' : '（已停用）'}
              </option>
            ))}
          </optgroup>
        ))}
      </select>

      {emptyHint ? <span className="ms-slot__empty-hint">{emptyHint}</span> : null}

      <div className="ms-slot__effective">
        <span className="ms-slot__effective-head">实际生效</span>
        {awaitingServer ? (
          <span className="ms-slot__effective-note">等待服务端确认…</span>
        ) : view.usable ? (
          <>
            <span className="ms-slot__effective-model">
              <CheckIcon size={12} />
              {view.model_id}
            </span>
            <span className="ms-slot__effective-note">
              {view.provider_id} · {kindLabel(provider?.kind ?? '')}
            </span>
          </>
        ) : view.configured ? (
          <span className="ms-slot__effective-model ms-slot__effective-model--bad">
            <AlertIcon size={12} />
            已配置但不可用（供应商或模型被停用）
          </span>
        ) : (
          <span className="ms-slot__effective-model ms-slot__effective-model--bad">
            还没配：跟随它的职责会显示不可用
          </span>
        )}
      </div>

      {providerMismatch ? (
        <span className="ms-slot__warn" role="status">
          <AlertIcon size={12} />
          这家不提供对话能力，填进默认槽位会发布成功、调用必失败；换一家对话供应商。
        </span>
      ) : null}
      {!providerMismatch && modelId && provider && !modelEnabled(provider, modelId) ? (
        <span className="ms-slot__warn" role="status">
          <AlertIcon size={12} />
          现在这个模型「{modelId}」已停用：保留显示是为了让你看清现状，换个启用的模型。
        </span>
      ) : null}

      <div className="ms-slot__foot">
        {roles.length > 0 ? (
          <span>{roles.join(' / ')} 跟着它</span>
        ) : (
          <span className="ms-faint">暂时没有职责跟着它</span>
        )}
      </div>
    </div>
  );
}

function modelEnabled(provider: ProviderDraftInput, modelId: string): boolean {
  const model = (provider.models ?? []).find((item) => item.model_id === modelId);
  return Boolean(model?.enabled);
}

/* ---------------------------------------------------------------- 职责行 */

function RoleRow({
  meta,
  profile,
  resolution,
  providers,
  slots,
  health,
  requiredCapability,
  memorySource,
  onPatch,
}: {
  meta: RoleMeta;
  profile: ProfileDraftInput | undefined;
  resolution: ProfileResolution | undefined;
  providers: ProviderDraftInput[];
  slots: SlotView[];
  health: ModelHealthResponse | null;
  /** 服务端 `profile_capabilities[role]`。null = 这个角色不受能力约束。 */
  requiredCapability: string | null;
  /** 服务端 `memory_sources[role]`；undefined = 这个角色不报来源。 */
  memorySource: MemorySource | undefined;
  onPatch: (patch: Partial<ProfileDraftInput>) => void;
}) {
  // 环境变量提供的记忆能力没有绑定，`profile_resolutions` 会说「不可用」。
  // 那是真的（网页绑定确实没有），但能力本身是开着的——不能把这两件事
  // 混成一个红字，否则用户会去"修"一个正在工作的配置。
  //
  // 判定与摘要计数共用 `roleBadgeState`：两边各算一次就会漂（验收抓到过
  // "摘要说 4 个继承、徽标里却有 1 个旧版兼容"）。
  const state = roleBadgeState(resolution, profile, memorySource);
  const envSourced = state === 'env';
  // 「配置来源」那行只在"网页没单独指定"时才需要说明（binding 的由徽标表达）。
  const draftExplicit =
    profile?.mode === 'explicit' && Boolean(profile.provider_id);
  // 嵌入/重排没有默认槽位，下拉里不该出现「跟随默认」：选了它也解析不出
  // 模型。服务端的 `slot: null` 是唯一判据（不要在前端按角色名硬编码）。
  const followAllowed = resolution === undefined || resolution.slot !== null;
  // 还没拿到服务端结论时不要编一个槽位名出来：`跟随默认 · 默认` 读起来像
  // 真有这么个槽位。宁可只写「跟随默认」。
  const followLabel = resolution?.slot
    ? `跟随默认 · ${slotLabel(slots, resolution.slot)}`
    : '跟随默认';
  const unboundLabel = memorySource === 'env' ? '未绑定 · 使用环境变量' : '未绑定';
  // 停用的供应商/模型也要留在下拉里：旧绑定可能正指着它们，藏起来会让
  // 用户以为"没选"，然后重选一个自己其实想留着的东西。
  const selectable = providers.filter(
    (item) => item.enabled || item.provider_id === profile?.provider_id,
  );
  // 能力过滤：只列具备该能力的供应商。**当前已绑的那家始终保留**，
  // 历史错配由下面的警告说明——从列表里消失会让人以为从来没绑过。
  const providerOptions = providersForProfile(
    selectable,
    requiredCapability,
    profile?.provider_id,
  );
  const boundProvider = providers.find((item) => item.provider_id === profile?.provider_id);
  const providerMismatch =
    Boolean(profile?.mode === 'explicit' && profile.provider_id) &&
    bindingCapabilityMismatch(boundProvider, requiredCapability);
  const effective = resolution?.effective ?? null;
  // 熔断/延迟按 `provider/model` 记在服务端。**只显示，不推断**：它是"上次
  // 真的调了"的证据，不是配置判断——所以不参与「可用/不可用」的结论。
  const record = effective
    ? health?.health[`${effective.provider_id}/${effective.model_id}`]
    : undefined;

  return (
    <div className={`ms-role-row ms-role-row--${state}`}>
      <span className="ms-role-row__icon" aria-hidden="true">
        <RoleIcon role={meta.key} size={15} />
      </span>
      <span className="ms-role-row__title">
        <span className="ms-role-row__name">
          {meta.label} <code>{meta.key}</code>
        </span>
        <span className="ms-role-row__hint">{meta.hint}</span>
      </span>

      <span className="ms-role-row__pick">
        <select
          className={`ms-input ms-select ms-select--${state}`}
          aria-label={`${meta.label} 用哪个模型`}
          value={roleSelectValue(profile)}
          onChange={(event) => {
            const picked = parseRoleSelectValue(event.target.value);
            if (!picked) {
              // 「跟随默认 / 未绑定」：把单独指定清掉。留着旧值会让"跟随"
              // 看起来还在用那个模型（服务端也会清，这里同步是为了草稿诚实）。
              onPatch({ mode: 'slot', provider_id: '', model_id: '' });
              return;
            }
            onPatch({
              mode: 'explicit',
              provider_id: picked.providerId,
              model_id: picked.modelId,
            });
          }}
        >
          <option value={FOLLOW_SLOT}>{followAllowed ? followLabel : unboundLabel}</option>
          {providerOptions.map((provider) => (
            <optgroup
              key={provider.provider_id}
              label={`${provider.provider_id}${
                provider.enabled ? '' : '（已停用）'
              } · ${kindLabel(provider.kind)}`}
            >
              {(provider.models ?? []).map((model) => (
                <option
                  key={`${provider.provider_id}::${model.model_id}`}
                  value={`${provider.provider_id}::${model.model_id}`}
                >
                  {model.model_id}
                  {model.enabled ? '' : '（已停用）'}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        <span className={`ms-badge ms-badge--${state}`}>{STATE_LABEL[state]}</span>
      </span>

      <span className="ms-role-row__effective">
        <span className="ms-role-row__effective-head">实际生效</span>
        {envSourced ? (
          <span className="ms-role-row__effective-model">
            <CheckIcon size={12} />
            环境变量
          </span>
        ) : effective ? (
          <span
            className={`ms-role-row__effective-model${
              state === 'unavailable' || providerMismatch
                ? ' ms-role-row__effective-model--bad'
                : ''
            }`}
          >
            {state === 'unavailable' || providerMismatch ? (
              <AlertIcon size={12} />
            ) : (
              <CheckIcon size={12} />
            )}
            {effective.model_id}
          </span>
        ) : (
          <span className="ms-role-row__effective-model ms-role-row__effective-model--bad">
            <AlertIcon size={12} />
            不可用
          </span>
        )}
        {envSourced ? (
          <span className="ms-role-row__effective-note">
            正在用环境变量里配的模型；具体模型名不在网页配置里
          </span>
        ) : providerMismatch ? (
          <span className="ms-role-row__effective-note ms-role-row__effective-note--bad">
            你单独选的，但这家不具备这个职责需要的能力
          </span>
        ) : (
          <span className="ms-role-row__effective-note">{effectiveNote(resolution, slots)}</span>
        )}
        {record?.open ? (
          <span className="ms-role-row__effective-note ms-role-row__effective-note--bad">
            连续失败，已暂时熔断
          </span>
        ) : record && record.consecutive_failures > 0 ? (
          <span className="ms-role-row__effective-note ms-role-row__effective-note--bad">
            最近一次失败（连续 {record.consecutive_failures} 次）
          </span>
        ) : record?.last_latency_ms != null ? (
          <span className="ms-role-row__effective-note">
            上次调用 {record.last_latency_ms} 毫秒
          </span>
        ) : null}
        {profile?.enabled === false ? (
          <button
            type="button"
            className="ms-link"
            aria-label={`重新启用${meta.label}`}
            onClick={() => onPatch({ enabled: true })}
          >
            已停用 · 重新启用
          </button>
        ) : (
          <button
            type="button"
            className="ms-link ms-link--faint"
            aria-label={`停用${meta.label}`}
            onClick={() => onPatch({ enabled: false })}
          >
            停用
          </button>
        )}
      </span>

      {providerMismatch ? (
        <span className="ms-role-row__warn" role="status">
          <AlertIcon size={12} />
          <span>
            已绑的 {boundProvider?.provider_id ?? profile?.provider_id} 只提供「
            {knownCapabilityText(boundProvider)}」，这个职责需要「
            {capabilityLabel(requiredCapability ?? '')}」；发布会被服务端拦下，
            换一家具备该能力的供应商。
          </span>
        </span>
      ) : null}

      {memorySource !== undefined && (memorySource === 'binding' || !draftExplicit) ? (
        <span
          className={`ms-role-row__source ms-role-row__source--${
            memorySource === 'binding' ? 'binding' : memorySource === 'env' ? 'env' : 'none'
          }`}
          role="status"
        >
          <span className="ms-role-row__source-label">配置来源</span>
          {memorySource === 'binding' ? (
            <span>来自网页绑定</span>
          ) : memorySource === 'env' ? (
            <span>来自环境变量，改成网页绑定后环境变量不再生效。</span>
          ) : (
            <span>没有配置：这项能力现在是关的。</span>
          )}
        </span>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------ 槽位与职责的取值转换 */

// 这两个是**槽位**专用（不存在"跟随默认"这一档），与上面的职责行转换分开写：
// 合用一个函数会让"槽位选了空值"和"职责选了跟随"看起来是同一种状态。

/**
 * 槽位选中值的对外导出（实现与单测都在 `shared.slotSelectValue`）。
 *
 * 保留这里的再导出是为了让页面与其他调用方只 import 一个模块；
 * 转换本身是纯函数，重算不会与界面漂开。
 */
export { parseSlotSelectValue, slotSelectValue };

/** 一个槽位下拉的可选项与"能不能选"，同样是纯函数（见 `shared.slotOptions`）。 */
export { groupSlotOptions, slotOptions };

/** 供应商已知能力 → 人话；未知时直接说"能力未确认"。 */
function knownCapabilityText(provider: ProviderDraftInput | undefined): string {
  const capabilities = provider?.capabilities;
  if (!capabilities) return '能力未确认';
  return capabilities.map(capabilityLabel).join('/');
}

/* ------------------------------------------------------------- 快速配置 */


function QuickConfigDialog({
  preview,
  busy,
  onCancel,
  onConfirm,
}: {
  preview: QuickConfigPreview | null;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="ms-modal" role="presentation">
      <div className="ms-modal__backdrop" onClick={onCancel} />
      <div
        className="ms-modal__card ms-modal__card--wide"
        role="dialog"
        aria-modal="true"
        aria-label="快速配置"
      >
        <header className="ms-modal__head">
          <div>
            <h2>快速配置</h2>
            <p className="ms-muted">
              把每个职责指回它自己的默认模型。你单独指定的
              <strong>也会被覆盖</strong>——下面逐条列出会发生什么。
            </p>
          </div>
          <button type="button" className="ms-icon-btn" aria-label="关闭" onClick={onCancel}>
            ×
          </button>
        </header>

        {busy || !preview ? (
          <p className="ms-muted">正在计算…</p>
        ) : (
          <>
            {/* 桌面宽度不够时让**表格**横向滚动，而不是把 `daily-chat`
                在连字符处拆成两行。窄屏表格是堆叠布局，用不到这层。 */}
            <div className="ms-quick-wrap">
              <table className="ms-quick">
                <thead>
                  <tr>
                    <th scope="col">职责</th>
                    <th scope="col">配置为</th>
                    <th scope="col">具体模型</th>
                    <th scope="col">变化</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.rows.map((row) => (
                    <tr key={row.profile} className={`ms-quick__row--${row.change_kind}`}>
                      {/*
                        `data-label` 是窄屏堆叠布局用的：375px 下四列表格会把
                        `perception.vision`、`fast-mini` 这类标识符拆成一字一行，
                        整个 diff 变得不可读。窄屏改成"每条一段、标签在左值在右"，
                        标签就取这里的值——和表头共用同一份文案，不会说岔。
                      */}
                      <td data-label="职责">
                        {roleLabel(row.profile)} <code>{row.profile}</code>
                      </td>
                      <td data-label="配置为">{row.slot_label}</td>
                      <td data-label="具体模型">{row.model_id ?? '—'}</td>
                      <td data-label="变化">{row.change}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/*
              两段式而不是一段长句：375 下这一段只有 ~322px，合成一段时
              "写入后仍需按页面底部的「保存」才生效。" 会在末行剩 2–3 个字
              （验收 G3 实测末行「生效。」）。拆成两行后每行都能独立放下。
            */}
            <p className="ms-muted ms-quick__note">
              {preview.summary.overrides > 0
                ? `会覆盖 ${preview.summary.overrides} 条你单独选的。`
                : '没有需要覆盖的单独指定。'}
              {preview.summary.still_unavailable > 0
                ? ` 另有 ${preview.summary.still_unavailable} 条仍然不可用：先去把对应的默认模型配好。`
                : ''}
            </p>
            <p className="ms-muted ms-quick__note">
              写入后还要按页面底部的「保存」才生效。
            </p>

            <div className="ms-modal__foot">
              <button type="button" className="ms-btn" onClick={onCancel}>
                取消
              </button>
              <button
                type="button"
                className="ms-btn ms-btn--primary"
                disabled={busy}
                onClick={onConfirm}
              >
                覆盖 {preview.summary.total} 个职责
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 主体 */

export function RolesPanel({
  config,
  providers,
  profiles,
  slots,
  resolutions,
  slotViews,
  quickConfig,
  quickConfigBusy,
  health,
  dirtyCount,
  saving,
  onPatchProfile,
  onPatchSlot,
  onDiscard,
  onSave,
  onRequestQuickConfig,
  onCloseQuickConfig,
  onApplyQuickConfig,
}: Props) {
  const byProfile = useMemo(() => new Map(profiles.map((item) => [item.profile, item])), [profiles]);
  const byResolution = useMemo(
    () => new Map(resolutions.map((item) => [item.profile, item])),
    [resolutions],
  );
  const slotDrafts = useMemo(
    () => new Map(slots.map((item) => [item.slot, item])),
    [slots],
  );
  // `config.slots` 是**已保存**的那一份；`slotViews` 是服务端对当前草稿的
  // resolve 结果（改草稿后 250ms 才回来）。两者分开，"未保存"才有依据。
  const savedSlots = useMemo(
    () => new Map((config.slots ?? []).map((item) => [item.slot, item])),
    [config.slots],
  );
  const metaByKey = useMemo(
    () => new Map<string, RoleMeta>([...PRIMARY_ROLES, ...EXTRA_ROLES].map((meta) => [meta.key, meta])),
    [],
  );
  // 只渲染服务端说"可绑"的角色：界面上多一行绑了没用的，比少一行更糟。
  // 同一份判定也用于 tab 数字与摘要总数（见 SettingsPage），三处不许各算一次。
  const visibleKeys = useMemo(
    () => visibleRoleKeys(config.known_profiles),
    [config.known_profiles],
  );
  const visible = useMemo(() => new Set(visibleKeys), [visibleKeys]);
  const profileCapabilities = config.profile_capabilities ?? {};
  const memorySources = useMemo(
    () => config.memory_sources ?? {},
    [config.memory_sources],
  );

  /**
   * 摘要计数 = 列表徽标计数。
   *
   * 逐行调**同一个** `roleBadgeState`，不另写一套 if：验收实测过两边漂开
   * （摘要说「4 个继承」，而徽标是 3 继承 + 1 旧版兼容）。旧版兼容现在单独
   * 一档，摘要里也单独列出来——它是"没配模型时借主对话代跑"，既不是用户
   * 选的，也不是槽位给的。
   */
  const counts = useMemo(() => {
    const result: Record<RoleBadgeState, number> = {
      explicit: 0,
      inherit: 0,
      fell_back: 0,
      legacy: 0,
      unavailable: 0,
      env: 0,
    };
    for (const key of visibleKeys) {
      const state = roleBadgeState(
        byResolution.get(key),
        byProfile.get(key),
        memorySources[key],
      );
      result[state] += 1;
    }
    return result;
  }, [visibleKeys, byResolution, byProfile, memorySources]);

  return (
    <section className="ms-roles" aria-label="模型与生成">
      <header className="ms-roles__head">
        <div>
          <h2>每个职责，各指一个模型</h2>
          <p className="ms-muted">
            给每项职责选择合适的模型——点开下拉就能换；
            再决定每条职责要不要单独指定。下面那行永远是
            <strong>这一行实际会跑的模型</strong>。
          </p>
          <p className="ms-roles__counts">
            {/* 总数取**渲染出来的行数**，不是绑定记录条数：tab 上的数字
                也曾写成 profiles.length(5) 而列表是 7 行。 */}
            <span>{visibleKeys.length} 个职责</span>
            <span>·</span>
            <span>{counts.explicit} 个单独指定</span>
            <span>·</span>
            <span>{counts.inherit} 个继承</span>
            {counts.legacy > 0 ? (
              <>
                <span>·</span>
                <span className="ms-roles__counts--legacy">{counts.legacy} 个旧版兼容</span>
              </>
            ) : null}
            {counts.fell_back > 0 ? (
              <>
                <span>·</span>
                <span className="ms-roles__counts--warn">{counts.fell_back} 个已回退</span>
              </>
            ) : null}
            {counts.unavailable > 0 ? (
              <>
                <span>·</span>
                <span className="ms-roles__counts--danger">{counts.unavailable} 个不可用</span>
              </>
            ) : null}
            {counts.env > 0 ? (
              <>
                <span>·</span>
                <span className="ms-roles__counts--env">{counts.env} 个来自环境变量</span>
              </>
            ) : null}
          </p>
        </div>
        <div className="ms-roles__actions">
          <button
            type="button"
            className="ms-btn ms-btn--sm ms-btn--primary"
            // 保存中不接第二次点击：否则同一份草稿会连开两个发布请求。
            disabled={quickConfigBusy || saving}
            onClick={onRequestQuickConfig}
          >
            快速配置
          </button>
        </div>
      </header>

      <div className="ms-slots" aria-label="默认模型">
        {slotViews.map((view) => (
          <SlotCard
            key={view.slot}
            view={view}
            saved={savedSlots.get(view.slot)}
            providers={providers}
            slotDraft={slotDrafts.get(view.slot)}
            saving={saving}
            onPatch={(patch) => onPatchSlot(view.slot, patch)}
          />
        ))}
      </div>

      <div className="ms-role-groups">
        {ROLE_GROUPS.map((group) => {
          const keys = group.keys.filter((key) => visible.has(key) && metaByKey.has(key));
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
                    resolution={byResolution.get(key)}
                    providers={providers}
                    slots={slotViews}
                    health={health}
                    requiredCapability={profileCapabilities[key] ?? null}
                    memorySource={
                      // `hasOwnProperty` 而不是 `?? null`：键存在且值为 null
                      // 表示"两处都没配"，与"服务端没报这个角色"是两回事。
                      Object.prototype.hasOwnProperty.call(memorySources, key)
                        ? memorySources[key]
                        : undefined
                    }
                    onPatch={(patch) => onPatchProfile(key, patch)}
                  />
                );
              })}
            </div>
          );
        })}
      </div>

      <p className="ms-hint">
        没有模型可用的职责不会自己找替补：状态就是「不可用」，原因写在行尾。
        回退只有一层——先看你单独指定的，再它所属的默认模型，到此为止。
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

      {quickConfig ? (
        <QuickConfigDialog
          preview={quickConfig}
          busy={quickConfigBusy}
          onCancel={onCloseQuickConfig}
          onConfirm={() => onApplyQuickConfig(quickConfig.profiles)}
        />
      ) : null}
    </section>
  );
}
