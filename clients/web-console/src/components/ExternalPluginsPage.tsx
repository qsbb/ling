/**
 * 外部插件页（设置 → 连接与运行 → 外部插件，`/settings/plugins`）。
 *
 * ## 这一页解决的问题
 *
 * 外部插件是一个独立 HTTP 服务，能力（工具/命令/事件/宿主状态）逐项声明、
 * 逐项授予。登记之前，所有者只能读 `docs/EXTERNAL-PLUGINS.md` 手敲 curl，
 * 而且**失败时看不到原因**：接口统一回一句"请检查端点是否可达且符合 v1 契约"，
 * 真正的差异（版本区间不匹配 / id 变了 / manifest 不是 JSON）只写在记录里。
 * 这一页把那份记录画成"现在怎样、为什么、下一步点哪个按钮"。
 *
 * ## 版式与取舍
 *
 * 1. **一页两段，顺序就是操作顺序**：上面"登记"，下面"已登记"。登记表默认收起，
 *    为空时自动展开——空列表上展开一张表是这一页唯一的起始动作。
 * 2. **每行 = 一个插件**，行内左到右是：名字与版本 → 状态徽标 → 能力授予 →
 *    操作。桌面一行看完，窄屏按同一次序折成竖排（DOM 顺序不变，只改 `flex-direction`），
 *    所以读屏和视觉读到的是同一个顺序，不需要两套标记。
 * 3. **能力用勾选网格表达"声明 vs 授予"**：一个能力格同时显示声明与授予，
 *    而不是两列文字。用户要回答的问题是"这个能力到底生效了吗"，勾选状态就是答案。
 * 4. **失败原因写在那一行的展开区里**，紧贴操作按钮，不弹全局横幅：
 *    横幅离开上下文后，人还得自己找是哪条插件坏了。
 * 5. **不显示端点**：服务端任何响应都不回流 `endpoint`（实测），所以界面上
 *    没有这一列——不是遗漏，是服务端不给。登记表里写明"登记后请自行记住"。
 *
 * ## 不写进浏览器存储的东西
 *
 * 这一页只用**同源 cookie**（`credentials: 'same-origin'` 由 `api.ts` 统一处理），
 * 不读不写 `localStorage`/`sessionStorage`/`IndexedDB`，不把任何令牌、cookie 或
 * 登记请求体留在浏览器里。刷新后页面**重新向服务端要状态**，本地不留缓存——
 * 缓存一份插件状态只会让"服务端已经停用、页面还显示开着"这种错更难发现。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api';
import {
  CAPABILITY_ORDER,
  capabilityDetail,
  capabilityName,
  ENDPOINT_NOT_RETURNED,
  ENDPOINT_PROBE_HINT,
  buildRegisterBody,
  effectiveCapabilities,
  failureNote,
  grantMismatch,
  healthLine,
  pluginState,
  remedy,
  statusBadge,
  ungrantedCapabilities,
  validateEndpointInput,
  type PluginAction,
} from '../externalPlugins';
import type {
  ExternalCapability,
  ExternalPlugin,
  ExternalPluginStatusResponse,
  ExternalPluginsResponse,
} from '../types';
import { AlertIcon, ArrowLeftIcon, CheckIcon, RefreshIcon } from './Icons';
import '../external-plugins.css';

interface ExternalPluginsPageProps {
  onBack: () => void;
}

/** 单条插件正在跑的动作用这个联合；`null` = 当前没有在跑。 */
export type PluginActionKind = 'enable' | 'disable' | 'sync' | 'unregister' | 'check';

/** 单条插件的忙碌态：`id` + 正在跑哪个动作。 */
type Busy = { id: string; action: PluginActionKind } | null;

/** 一次操作的结果横幅（成功/失败都只留一条，避免堆一屏）。 */
interface Notice {
  tone: 'ok' | 'error';
  text: string;
  detail?: string;
  traceId?: string | null;
}

export function ExternalPluginsPage({ onBack }: ExternalPluginsPageProps) {
  const [data, setData] = useState<ExternalPluginsResponse | null>(null);
  const [loadError, setLoadError] = useState<Notice | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [loading, setLoading] = useState(true);
  /** 每行的健康探测结果；`null` = 这一页打开后没探测过。 */
  const [health, setHealth] = useState<Record<string, ExternalPluginStatusResponse>>({});
  const [formOpen, setFormOpen] = useState(false);
  /** 取消登记的二次确认（按 id）；登记是破坏性动作，不能一点就删。 */
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal, options?: { keepHealth?: boolean }) => {
    try {
      const next = await api.externalPlugins(signal);
      setData(next);
      /*
       * 重读列表**默认**丢掉旧的健康探测结果：它是某一刻的观测，而这次重读
       * 很可能正是因为记录变了（同步改了 id、启停改了状态）。留着它会出现
       * "状态：标识已变" + "端点可达，manifest 与登记 id 一致" 同时挂在一条
       * 记录上——两句话都各自为真，但它们说的是不同的时刻，凑在一起就是假话。
       *
       * `keepHealth` 只给「检查端点」这一条路径用：那个动作刚刚**新鲜**地
       * 探测过一次，紧接着的列表重读是它的一部分，不该反手把结果擦掉
       * （第一版就是在这里把刚拿到的探测结果清空了）。
       */
      if (!options?.keepHealth) setHealth({});
      setLoadError(null);
      return next;
    } catch (error) {
      if (signal?.aborted) return null;
      setLoadError(noticeFromError(error));
      return null;
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  // 空列表时自动展开登记表：这是这一页唯一的"下一步"。
  const plugins = data?.plugins ?? [];
  useEffect(() => {
    if (data && data.plugins.length === 0) setFormOpen(true);
  }, [data]);

  /**
   * 跑一个动作，失败后**无论成败都重读列表**。
   *
   * 为什么失败也要重读：`enable`/`sync` 在标识不匹配时返回 422，而服务端
   * **已经提交了停用**。只在成功时刷新，界面会停在"还开着"的旧状态上。
   */
  const runAction = useCallback(
    async (
      id: string,
      action: PluginActionKind,
      fn: () => Promise<string>,
      options?: { keepHealth?: boolean },
    ) => {
      setBusy({ id, action });
      setNotice(null);
      try {
        const text = await fn();
        setNotice({ tone: 'ok', text });
      } catch (error) {
        setNotice(noticeFromError(error));
      } finally {
        setBusy(null);
        await refresh(undefined, options);
      }
    },
    [refresh],
  );

  const onToggle = (plugin: ExternalPlugin) => {
    const enable = pluginState(plugin) !== 'active';
    void runAction(plugin.id, enable ? 'enable' : 'disable', async () => {
      const row = await api.setExternalPluginEnabled(plugin.id, enable);
      return enable
        ? `已启用 ${row.name}（版本 ${row.version}）。`
        : `已停用 ${row.name}：命令、工具与事件都不再调用它。`;
    });
  };

  const onSync = (plugin: ExternalPlugin) => {
    void runAction(plugin.id, 'sync', async () => {
      const row = await api.syncExternalPlugin(plugin.id);
      const dropped = plugin.capabilities.filter((cap) => !row.capabilities.includes(cap));
      return dropped.length > 0
        ? `已同步 ${row.name}：新声明去掉了 ${dropped
            .map(capabilityName)
            .join('、')}，对应的授权也一并撤掉了。`
        : `已同步 ${row.name}（版本 ${row.version}）。`;
    });
  };

  const onCheck = (plugin: ExternalPlugin) => {
    void runAction(plugin.id, 'check', async () => {
      const status = await api.externalPluginStatus(plugin.id);
      setHealth((current) => ({ ...current, [plugin.id]: status }));
      return status.reachable ? `${status.name}：端点可达。` : `${status.name}：端点不可达或标识不一致。`;
    }, { keepHealth: true });
  };

  const onUnregister = (plugin: ExternalPlugin) => {
    setConfirmDelete(null);
    void runAction(plugin.id, 'unregister', async () => {
      await api.unregisterExternalPlugin(plugin.id);
      setHealth((current) => {
        const next = { ...current };
        delete next[plugin.id];
        return next;
      });
      return `已取消登记 ${plugin.name}。`;
    });
  };

  const onRegistered = useCallback(
    async (row: ExternalPlugin) => {
      setNotice({
        tone: 'ok',
        text: `已登记 ${row.name}（${row.id}@${row.version}），授权 ${row.granted_capabilities.length} 项${
          row.enabled ? '，已启用' : '，尚未启用'
        }。`,
      });
      setFormOpen(false);
      await refresh();
    },
    [refresh],
  );

  return (
    <section className="page ep-page" data-testid="external-plugins-page">
      <header className="ep-head">
        <button type="button" className="back-link" onClick={onBack}>
          <ArrowLeftIcon size={15} /> 设置
        </button>
        <h1 className="page__title">外部插件</h1>
        <p className="page__hint">
          第三方 HTTP 服务提供工具、命令、事件和宿主状态。插件代码不进 Ling 进程，端点由你登记。
        </p>
      </header>

      {notice ? <NoticeBar notice={notice} onDismiss={() => setNotice(null)} /> : null}
      {loadError ? (
        <div className="ep-notice" data-tone="error" role="alert" data-testid="ep-load-error">
          <AlertIcon />
          <div>
            <strong>{loadError.text}</strong>
            {loadError.detail ? <p>{loadError.detail}</p> : null}
            {loadError.traceId ? <p className="ep-trace">trace {loadError.traceId}</p> : null}
          </div>
          <button type="button" className="button button--small" onClick={() => void refresh()}>
            重试
          </button>
        </div>
      ) : null}

      <RegisterSection
        open={formOpen}
        onToggle={() => setFormOpen((value) => !value)}
        onRegistered={onRegistered}
        hasPlugins={plugins.length > 0}
      />

      <section className="ep-list" aria-labelledby="ep-list-title">
        <h2 className="ep-list__title" id="ep-list-title">
          已登记
          <span className="ep-list__count" data-testid="ep-count">
            {plugins.length}
          </span>
          <button
            type="button"
            className="ep-refresh"
            onClick={() => void refresh()}
            disabled={loading}
            aria-label="重新读取服务端状态"
          >
            <RefreshIcon size={14} /> 重新读取
          </button>
        </h2>

        {loading && data === null ? (
          <p className="page__loading">正在读取…</p>
        ) : data === null ? (
          /*
           * 读失败时**不画空态**。403（不是管理员）或网络断了都会走到这里，
           * 而"还没有登记任何插件"是一句关于**事实**的断言——没有读到数据时
           * 我们并不知道有没有插件。两者同时出现会让人以为登记被删了。
           * 上面的错误横幅已经说明了原因和下一步。
           * 反过来：**曾经读到过**（`data !== null`）时某次刷新失败，列表照旧显示——
           * 拿掉一份已知内容，比留着它并挂一条"这次没读到"更糟。
           */
          <p className="ep-empty ep-empty--failed" data-testid="ep-unread">
            列表没读到，所以这里不显示「已登记」的内容。
          </p>
        ) : plugins.length === 0 ? (
          <p className="ep-empty" data-testid="ep-empty">
            还没有登记任何插件。填上面那张表，或者在别处登记后点「重新读取」。
          </p>
        ) : (
          <ul className="ep-rows">
            {plugins.map((plugin) => (
              <PluginRow
                key={plugin.id}
                plugin={plugin}
                health={health[plugin.id] ?? null}
                busy={busy?.id === plugin.id ? busy.action : null}
                anyBusy={busy !== null}
                confirming={confirmDelete === plugin.id}
                onAskDelete={() => setConfirmDelete(plugin.id)}
                onCancelDelete={() => setConfirmDelete(null)}
                onToggle={() => onToggle(plugin)}
                onSync={() => onSync(plugin)}
                onCheck={() => onCheck(plugin)}
                onUnregister={() => onUnregister(plugin)}
              />
            ))}
          </ul>
        )}

        {data && data.legacy.length > 0 ? <LegacyList legacy={data.legacy} /> : null}
      </section>
    </section>
  );
}

/* ---------------------------------------------------------------- 登记表单 */

interface RegisterSectionProps {
  open: boolean;
  onToggle: () => void;
  onRegistered: (row: ExternalPlugin) => Promise<void> | void;
  hasPlugins: boolean;
}

/**
 * 登记表。
 *
 * 端点能不能通、manifest 合不合规**只能由服务端回答**（它会 GET `/v1/manifest`），
 * 所以这里不预判，只做两件本地就能确定的事：地址格式（与服务端同一套规则，
 * 提前反馈）和"至少授一项能力"。
 *
 * `enabled` 默认 `false`：登记只是把记录写下来，启用会真实探测端点；
 * 两个动作分开，失败时才知道是"登记失败"还是"启用失败"。
 */
export function RegisterSection({ open, onToggle, onRegistered, hasPlugins }: RegisterSectionProps) {
  const [endpoint, setEndpoint] = useState('');
  const [expectedId, setExpectedId] = useState('');
  const [granted, setGranted] = useState<ExternalCapability[]>([]);
  const [enabled, setEnabled] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<Notice | null>(null);
  const endpointRef = useRef<HTMLInputElement>(null);

  const endpointError = endpoint.trim() ? validateEndpointInput(endpoint) : null;
  const canSubmit =
    !submitting && endpoint.trim().length > 0 && endpointError === null && granted.length > 0;

  const toggleGrant = (cap: ExternalCapability) => {
    setGranted((current) =>
      current.includes(cap) ? current.filter((item) => item !== cap) : [...current, cap],
    );
  };

  const submit = async () => {
    const localError = validateEndpointInput(endpoint);
    if (localError) {
      setError({ tone: 'error', text: localError });
      endpointRef.current?.focus();
      return;
    }
    if (granted.length === 0) {
      setError({ tone: 'error', text: '至少勾一项能力——一项都不授的登记不会做任何事。' });
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const row = await api.registerExternalPlugin(
        buildRegisterBody({ endpoint, expectedId, granted, enabled }),
      );
      setEndpoint('');
      setExpectedId('');
      setGranted([]);
      setEnabled(false);
      await onRegistered(row);
    } catch (failure) {
      setError(registerFailure(failure));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="ep-register" data-testid="ep-register">
      <button
        type="button"
        className="ep-register__toggle"
        aria-expanded={open}
        onClick={onToggle}
      >
        <span className="ep-register__plus" aria-hidden="true">
          {open ? '−' : '+'}
        </span>
        登记一个插件
        {open ? null : <span className="ep-register__hint">{hasPlugins ? '' : '从这里开始'}</span>}
      </button>

      {open ? (
        <div className="ep-form">
          <div className="ep-field">
            <label htmlFor="ep-endpoint">端点地址</label>
            <input
              id="ep-endpoint"
              ref={endpointRef}
              type="text"
              inputMode="url"
              spellCheck={false}
              autoComplete="off"
              placeholder="http://127.0.0.1:8096"
              value={endpoint}
              onChange={(event) => setEndpoint(event.target.value)}
              aria-invalid={endpointError !== null}
              aria-describedby="ep-endpoint-help"
            />
            <p className="ep-field__help" id="ep-endpoint-help">
              {ENDPOINT_PROBE_HINT}
            </p>
            {endpointError ? (
              <p className="ep-field__error" role="status">
                {endpointError}
              </p>
            ) : null}
          </div>

          <div className="ep-field ep-field--short">
            <label htmlFor="ep-expected">
              期望 id <span className="ep-optional">可选</span>
            </label>
            <input
              id="ep-expected"
              type="text"
              spellCheck={false}
              autoComplete="off"
              placeholder="example.notes"
              value={expectedId}
              onChange={(event) => setExpectedId(event.target.value)}
              aria-describedby="ep-expected-help"
            />
            <p className="ep-field__help" id="ep-expected-help">
              填了就要求端点声明的 id 与它一致，防止登记到别的服务上。留空以端点为准。
            </p>
          </div>

          <fieldset className="ep-grants">
            <legend>授权</legend>
            {CAPABILITY_ORDER.map((cap) => (
              <label className="ep-grant" key={cap} data-checked={granted.includes(cap)}>
                <input
                  type="checkbox"
                  checked={granted.includes(cap)}
                  onChange={() => toggleGrant(cap)}
                  data-testid={`ep-grant-${cap}`}
                />
                <span className="ep-grant__body">
                  <span className="ep-grant__label">{capabilityName(cap)}</span>
                  <span className="ep-grant__detail">{capabilityDetail(cap)}</span>
                </span>
              </label>
            ))}
          </fieldset>

          <div className="ep-form__foot">
            <label className="ep-enable-now">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(event) => setEnabled(event.target.checked)}
                data-testid="ep-enable-now"
              />
              登记后立即启用
            </label>
            <button
              type="button"
              className="button button--primary"
              onClick={() => void submit()}
              disabled={!canSubmit}
              data-testid="ep-submit"
            >
              {submitting ? '登记中…' : '登记'}
            </button>
          </div>

          <p className="ep-form__note">{ENDPOINT_NOT_RETURNED}</p>
          {error ? <NoticeBar notice={error} onDismiss={() => setError(null)} compact /> : null}
        </div>
      ) : null}
    </section>
  );
}

/* ------------------------------------------------------------------ 单行 */

interface PluginRowProps {
  plugin: ExternalPlugin;
  health: ExternalPluginStatusResponse | null;
  busy: PluginActionKind | null;
  anyBusy: boolean;
  confirming: boolean;
  onAskDelete: () => void;
  onCancelDelete: () => void;
  onToggle: () => void;
  onSync: () => void;
  onCheck: () => void;
  onUnregister: () => void;
}

/**
 * 一条登记。
 *
 * 行内固定顺序：名字 → 状态 → 能力 → 操作。展开区只放**这一行特有的**信息
 * （失败原因、下一步、健康探测、事件/工具/命令清单），所以它跟着行走，
 * 不会和别的插件的错误混在一起。
 */
export function PluginRow({
  plugin,
  health,
  busy,
  anyBusy,
  confirming,
  onAskDelete,
  onCancelDelete,
  onToggle,
  onSync,
  onCheck,
  onUnregister,
}: PluginRowProps) {
  const state = pluginState(plugin);
  const badge = statusBadge(plugin);
  const fix = remedy(plugin);
  const mismatch = grantMismatch(plugin);
  const missing = ungrantedCapabilities(plugin);
  const effective = effectiveCapabilities(plugin);
  const active = state === 'active';

  return (
    <li
      className="ep-row"
      data-state={state}
      data-testid={`ep-row-${plugin.id}`}
      aria-busy={busy !== null}
    >
      <div className="ep-row__main">
        <div className="ep-row__id">
          <span className="ep-row__name">{plugin.name}</span>
          <code className="ep-row__slug">
            {plugin.id}@{plugin.version}
          </code>
        </div>

        <span className="ep-badge" data-tone={badge.tone} data-testid={`ep-state-${plugin.id}`}>
          {badge.label}
        </span>

        <ul className="ep-caps" aria-label="能力（声明 / 授予）">
          {CAPABILITY_ORDER.map((cap) => {
            const declared = plugin.capabilities.includes(cap);
            const granted = plugin.granted_capabilities.includes(cap);
            const on = declared && granted;
            return (
              <li
                key={cap}
                className="ep-cap"
                data-on={on}
                data-declared={declared}
                title={
                  !declared
                    ? `插件未声明${capabilityName(cap)}`
                    : granted
                      ? `已声明并授予：${capabilityDetail(cap)}`
                      : `已声明但未授予，不会被调用`
                }
              >
                <span className="ep-cap__mark" aria-hidden="true">
                  {on ? <CheckIcon size={12} /> : null}
                </span>
                <span className="ep-cap__label">{capabilityName(cap)}</span>
              </li>
            );
          })}
        </ul>

        <div className="ep-row__actions">
          <button
            type="button"
            className={`button button--small${active ? '' : ' button--primary'}`}
            onClick={onToggle}
            disabled={anyBusy}
            data-testid={`ep-toggle-${plugin.id}`}
          >
            {busy === 'enable' ? '启用中…' : busy === 'disable' ? '停用中…' : active ? '停用' : '启用'}
          </button>
          <button
            type="button"
            className="button button--small"
            onClick={onSync}
            disabled={anyBusy}
            title="重新读取端点上的 manifest"
            data-testid={`ep-sync-${plugin.id}`}
          >
            {busy === 'sync' ? '同步中…' : '同步'}
          </button>
          <button
            type="button"
            className="ep-icon-button"
            onClick={onCheck}
            disabled={anyBusy}
            aria-label={`检查 ${plugin.name} 的端点`}
            title="检查端点"
            data-testid={`ep-check-${plugin.id}`}
          >
            <RefreshIcon size={14} />
          </button>
          {confirming ? (
            <span className="ep-confirm">
              <button
                type="button"
                className="button button--small button--danger"
                onClick={onUnregister}
                disabled={anyBusy}
                data-testid={`ep-confirm-delete-${plugin.id}`}
              >
                确认取消
              </button>
              <button type="button" className="ghost-button" onClick={onCancelDelete} disabled={anyBusy}>
                取消
              </button>
            </span>
          ) : (
            <button
              type="button"
              className="ghost-button ep-remove"
              onClick={onAskDelete}
              disabled={anyBusy}
              data-testid={`ep-delete-${plugin.id}`}
            >
              {busy === 'unregister' ? '取消中…' : '取消登记'}
            </button>
          )}
        </div>
      </div>

      {fix ? (
        <div className="ep-fix" data-tone={badge.tone} data-testid={`ep-fix-${plugin.id}`}>
          <div className="ep-fix__text">
            <strong>{fix.reason}</strong>
            <p>{fix.next}</p>
          </div>
          {fix.action ? (
            <ActionHint action={fix.action} plugin={plugin} onSync={onSync} onUnregister={onAskDelete} onToggle={onToggle} onCheck={onCheck} />
          ) : null}
        </div>
      ) : null}

      {mismatch ? (
        <p className="ep-grant-note" data-testid={`ep-grant-note-${plugin.id}`}>
          {mismatch}。当前真正生效的是 {effective.length === 0 ? '没有能力' : effective.map(capabilityName).join('、')}
          {missing.length > 0 ? '。要放开缺少的那几项，用同 id 端点重新登记（会覆盖授权）。' : '。'}
        </p>
      ) : null}

      <dl className="ep-meta">
        <div>
          <dt>兼容版本</dt>
          <dd>
            {plugin.min_host_version} – {plugin.max_host_version}
          </dd>
        </div>
        <div>
          <dt>事件</dt>
          <dd>{plugin.events.length > 0 ? plugin.events.join('、') : '—'}</dd>
        </div>
        <div>
          <dt>工具</dt>
          <dd>{plugin.tools.length > 0 ? plugin.tools.join('、') : '—'}</dd>
        </div>
        <div>
          <dt>命令</dt>
          <dd>{plugin.commands.length > 0 ? plugin.commands.join('、') : '—'}</dd>
        </div>
        <div>
          <dt>最近检查</dt>
          <dd>{formatWhen(plugin.last_checked_at)}</dd>
        </div>
      </dl>

      {/*
        失败状态（标识变了 / 版本不兼容 / 不可达）本身已经是更强的结论，
        这时再挂一条"探测结果"只会让两句话互相打架。所以只在记录认为自己
        可用时展示探测结果——它回答的是"能不能调"，不是"要不要修"。
      */}
      {health && state !== 'blocked' ? (
        <p className="ep-health" data-ok={health.reachable} data-testid={`ep-health-${plugin.id}`}>
          {healthLine(health)}
          <span className="ep-health__when">{formatWhen(health.health_checked_at)}</span>
        </p>
      ) : null}
    </li>
  );
}

/** 修复建议对应的按钮文案；按钮本身在行内已有，这里只指出"按哪个"。 */
function ActionHint({
  action,
  plugin,
  onSync,
  onUnregister,
  onToggle,
  onCheck,
}: {
  action: PluginAction;
  plugin: ExternalPlugin;
  onSync: () => void;
  onUnregister: () => void;
  onToggle: () => void;
  onCheck: () => void;
}) {
  switch (action) {
    case 'enable':
      return (
        <button type="button" className="button button--small" onClick={onToggle}>
          重试启用
        </button>
      );
    case 'sync':
      return (
        <button type="button" className="button button--small" onClick={onSync} title="重新读取 manifest">
          同步
        </button>
      );
    case 'unregister':
      return (
        <button type="button" className="button button--small" onClick={onUnregister}>
          取消登记
        </button>
      );
    case 'recheck':
      return (
        <button type="button" className="button button--small" onClick={onCheck}>
          检查端点
        </button>
      );
    case 'regrant':
      return (
        <span className="ep-fix__hint">
          {plugin.id} 需要重新登记才能补授
        </span>
      );
  }
}

/* ------------------------------------------------------------------ 旧入口 */

/** 旧 `tool-plugins.json` 摘要：只读，且**不是** v1 插件（不能在这里启停）。 */
function LegacyList({ legacy }: { legacy: ExternalPluginsResponse['legacy'] }) {
  return (
    <section className="ep-legacy" data-testid="ep-legacy">
      <h3 className="ep-legacy__title">旧的只读工具插件</h3>
      <p className="ep-legacy__note">
        来自 tool-plugins.json，继续用 /plugin catalog 访问；它们不是 v1 插件，不能在这里启停，
        也不会自动变成外部插件。
      </p>
      <ul className="ep-legacy__items">
        {legacy.map((item) => (
          <li key={item.id}>
            <span>{item.name}</span>
            <code>{item.id}</code>
            <span data-on={item.enabled}>{item.enabled ? '已启用' : '已停用'}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/* ------------------------------------------------------------------ 通用 */

function NoticeBar({
  notice,
  onDismiss,
  compact,
}: {
  notice: Notice;
  onDismiss: () => void;
  compact?: boolean;
}) {
  return (
    <div
      className={`ep-notice${compact ? ' ep-notice--compact' : ''}`}
      data-tone={notice.tone}
      role={notice.tone === 'error' ? 'alert' : 'status'}
      data-testid="ep-notice"
    >
      {notice.tone === 'error' ? <AlertIcon /> : <CheckIcon />}
      <div className="ep-notice__body">
        <strong>{notice.text}</strong>
        {notice.detail ? <p>{notice.detail}</p> : null}
        {notice.traceId ? <p className="ep-trace">trace {notice.traceId}</p> : null}
      </div>
      <button type="button" className="ghost-button" onClick={onDismiss} aria-label="关闭提示">
        关闭
      </button>
    </div>
  );
}

function noticeFromError(error: unknown): Notice {
  if (error instanceof ApiError) {
    return {
      tone: 'error',
      text: error.message || '请求失败',
      detail: error.status === 403 ? failureNote({ message: error.message, code: 'forbidden' }) : undefined,
      traceId: error.traceId,
    };
  }
  return { tone: 'error', text: error instanceof Error ? error.message : '请求失败' };
}

/**
 * 登记失败时把"一句话错误"翻译成"下一步"。
 *
 * 服务端的登记失败只有三种文案（实测）：ID 不一致、已登记、通用兜底。
 * 通用兜底覆盖了 manifest 不可达 / 不是 JSON / 版本区间不匹配 / 能力不合法——
 * 页面无法区分它们，所以只能把可能的四种原因都列出来，并给出**怎么逐项排除**，
 * 而不是假装知道是哪一个。
 */
function registerFailure(error: unknown): Notice {
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return {
        tone: 'error',
        text: '只有所有者和管理员能登记外部插件。',
        detail: failureNote({ message: error.message, code: 'forbidden' }),
        traceId: error.traceId,
      };
    }
    if (error.message.includes('ID 与端点声明不一致')) {
      return {
        tone: 'error',
        text: '端点声明的 id 与「期望 id」不一致。',
        detail: '确认地址指向的是你想登记的插件；如果以端点为准，把「期望 id」留空再登记。',
        traceId: error.traceId,
      };
    }
    if (error.message.includes('已登记')) {
      return {
        tone: 'error',
        text: '这个插件已经登记过。',
        detail: '在下面这行上点「同步」重读 manifest；要改授权就用同一 id 重新登记（覆盖）。',
        traceId: error.traceId,
      };
    }
    if (error.status === 422 || error.code === 'invalid_request') {
      return {
        tone: 'error',
        text: '登记没有通过，端点没能给出可用的 manifest。',
        detail:
          '按这个顺序排查：① 地址在 API 进程里可达（容器部署时 127.0.0.1 是容器自己，不是宿主）；' +
          '② GET {地址}/v1/manifest 返回的是 JSON 而不是登录页或错误页；③ min/max_host_version 包含当前 Ling 版本；' +
          '④ 声明的能力都在 tools/commands/events/host.state 之内，且与工具、命令、事件的声明一致。',
        traceId: error.traceId,
      };
    }
    return { tone: 'error', text: error.message || '登记失败', traceId: error.traceId };
  }
  return { tone: 'error', text: error instanceof Error ? error.message : '登记失败' };
}

/** 时间：本地时间 + 相对时间，`null` 显示"从未"。 */
export function formatWhen(iso: string | null): string {
  if (!iso) return '从未';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const diffMs = Date.now() - date.getTime();
  const minutes = Math.round(diffMs / 60000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)} 小时前`;
  return date.toLocaleString('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** 供静态渲染测试复用：拿到能力标签而不引整页。 */
export const capabilityLabel = capabilityName;
