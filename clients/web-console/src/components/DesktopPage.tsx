/**
 * Windows 桌面任务（`/desktop`）：所有者把一台 Windows 电脑接到 Ling 上，
 * 让它打开记事本、写入一段文字并逐字回读。
 *
 * ## 这一页回答的三个问题
 *
 * 1. **这台电脑能接任务吗**（顶部「电脑」一块：名称、连接、接收开关 + 一个
 *    由它们算出来的结论）。结论只在一处出现，下面各块不再重复"当前不可提交"。
 * 2. **Ling 做到哪一步了**（「这一步」：在途任务的四步进度 + 结果 + 取消）。
 * 3. **此刻有哪些操作**（配对 / 恢复接收 / 提交 / 暂停 / 授权 / 撤销）。
 *    不能做的操作**留在原地禁用**并给一句原因，不做成"点下去才知道"。
 *
 * ## 状态的事实来源
 *
 * 设备、暂停、任务、授权全部来自服务端；本地只保存**输入框内容**和
 * **一次待提交的编号**。刷新、页面往返、丢失响应之后，`GET /v1/desktop`
 * 会把这些原样给回来——不在 localStorage 里存任何"我以为的状态"。
 * 唯一的例外是配对令牌：它只在 `PUT /pair` 的响应里存在，离开这次交互就清掉
 * （不落 localStorage、不进 URL）。刷新后顶多丢了令牌，重新生成即可，
 * 那一刻界面会明说"旧令牌已不可见"。
 *
 * ## 为什么令牌面板不自动消失
 *
 * 令牌要粘贴到另一台电脑的终端里。自动超时消失会让人在最后一步丢掉它，
 * 而重新生成会**替换令牌并中断在途任务**。所以它留在原地，只靠两个明确的
 * 出口清掉：点「我已粘贴好」/离开页面。「离开就清掉」由组件卸载保证。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, api } from '../api';
import {
  DESKTOP_POLL_MS,
  isActive,
  MAX_DEVICE_NAME,
  checkGrantWindow,
  checkNote,
  conflictHint,
  describeDevice,
  formatRange,
  grantIsLive,
  grantView,
  isoToLocalInput,
  launchCommand,
  localInputToIso,
  maskToken,
  noteLength,
  outcomeOf,
  sinceText,
  stepsOf,
  activeTaskOf,
  capabilityOf,
  clockOf,
} from '../desktop';
import { newRequestId, startDesktopPolling } from '../desktop-polling';
import type { DesktopActivityGrant, DesktopStatus, DesktopTask } from '../desktop';
import { explainError, type ExplainedError } from '../errors';
import { AlertIcon, CheckIcon, RefreshIcon } from './Icons';
import '../desktop.css';

/* ------------------------------------------------------------------ 小件 */

/** 复制到剪贴板。非安全上下文没有 `navigator.clipboard`，退回到选区复制。 */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // 权限被拒时落到下面的兜底
  }
  try {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

function useCopy(): { copied: string | null; copy: (key: string, text: string) => Promise<void> } {
  const [copied, setCopied] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);
  const copy = useCallback(async (key: string, text: string) => {
    const ok = await copyText(text);
    setCopied(ok ? key : `failed:${key}`);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(null), 2400);
  }, []);
  return { copied, copy };
}

export function ErrorBanner({
  error,
  onRetry,
  testid,
}: {
  error: ExplainedError;
  onRetry?: () => void;
  testid?: string;
}): React.ReactElement {
  return (
    <div className="banner banner--error desktop-banner" role="alert" data-testid={testid}>
      <AlertIcon />
      <div className="desktop-banner__body">
        <strong>{error.reason}</strong>
        {error.hint ? <div className="banner__detail">{error.hint}</div> : null}
        {error.detail ? <div className="desktop-banner__code">{error.detail}</div> : null}
      </div>
      {onRetry ? (
        <button type="button" className="button button--small" onClick={onRetry}>
          <RefreshIcon /> 重试
        </button>
      ) : null}
    </div>
  );
}

/** 把 409 的 detail 折成一句"接下来点哪里"，而不是抛一句通用冲突。 */
function writeError(error: unknown, fallback: string): ExplainedError {
  const explained = explainError(error);
  if (error instanceof ApiError && error.code === 'conflict') {
    return { ...explained, reason: error.message || explained.reason, hint: conflictHint(error.detail, fallback) };
  }
  if (error instanceof ApiError && error.code === 'invalid_request' && error.detail) {
    // 422/400 的 detail 是出错字段（`text` / `request_id` / `end_at`…）
    return { ...explained, hint: `后端拒绝了这个字段：${error.detail}` };
  }
  return explained;
}

/* -------------------------------------------------------------- 设备面板 */

export function DevicePanel({
  status,
  onResume,
  onPause,
  busy,
}: {
  status: DesktopStatus;
  onResume: () => void;
  onPause: () => void;
  busy: boolean;
}): React.ReactElement {
  const device = status.device;
  const capability = capabilityOf(status);
  const intent = status.paused ? 'paused' : 'accepting';
  return (
    <section className="desktop-card desktop-device" data-testid="desktop-device">
      <header className="desktop-card__head">
        <h2 className="desktop-card__title">电脑</h2>
        {device ? (
          <span className="desktop-state" data-testid="desktop-device-state" data-state={device.state}>
            <span className="desktop-state__dot" aria-hidden="true" />
            {describeDevice(device)}
          </span>
        ) : null}
      </header>

      {device ? (
        <div className="desktop-device__id">
          <span className="desktop-device__name" data-testid="desktop-device-name">{device.name}</span>
          <span className="desktop-device__seen" data-testid="desktop-device-seen">
            最近心跳 {sinceText(device.last_seen_at)}
          </span>
        </div>
      ) : (
        <p className="desktop-card__note">还没有配对任何电脑。生成令牌后在那台电脑上启动伴生端。</p>
      )}

      {/*
        没有配对时**不渲染接收开关**：`paused` 后端仍为 true，但那个 true 的含义是
        "还没配过"，不是"所有者暂停了"。画一个「恢复接收」会让人点下去拿到
        `409 not_paired`——一个必然失败的按钮不是状态，是误导。
      */}
      {device ? (
      <div className="desktop-receive" data-testid="desktop-receive" data-intent={intent}>
        <span className="desktop-receive__label">接收任务</span>
        <span className="desktop-receive__value" data-testid="desktop-receive-value">
          {intent === 'accepting' ? '已允许' : '已暂停'}
        </span>
        {intent === 'accepting' ? (
          <button
            type="button"
            className="button button--small"
            data-testid="desktop-pause"
            disabled={busy}
            onClick={onPause}
          >
            暂停接收
          </button>
        ) : (
          <button
            type="button"
            className="button button--small"
            data-testid="desktop-resume"
            disabled={busy || !capability.canResume}
            title={capability.canResume ? undefined : (capability.blocker ?? undefined)}
            onClick={onResume}
          >
            {busy ? '处理中…' : '恢复接收'}
          </button>
        )}
      </div>
      ) : null}

      {/*
        结论只出现在**提交区**（`desktop-submit-blocked`）：那一句"现在为什么不能提交"
        紧贴着被禁用的按钮，读的人不用在两块之间来回配对。这里不再重复一遍——
        同一句话在同一屏出现两次，只会让人怀疑它们是两个不同的原因。
        这里只保留"可以提交"这一种正向结论：它是页面上唯一的"什么都没挡住"信号。
      */}
      {capability.canSubmit ? (
        <p className="desktop-verdict" data-testid="desktop-verdict" data-tone="ok">
          这台电脑可以接一条测试任务。
        </p>
      ) : null}
    </section>
  );
}

/* ------------------------------------------------------------ 配对面板 */

export function PairingPanel({
  status,
  token,
  onPair,
  onUnpair,
  onTokenDone,
  busy,
  url,
}: {
  status: DesktopStatus | null;
  token: string | null;
  onPair: (name: string) => Promise<void>;
  onUnpair: () => Promise<void>;
  onTokenDone: () => void;
  busy: boolean;
  url: string;
}): React.ReactElement {
  const paired = status?.device ?? null;
  const [name, setName] = useState(paired?.name ?? '');
  const [replacing, setReplacing] = useState(false);
  const [confirmUnpair, setConfirmUnpair] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const { copied, copy } = useCopy();

  // 设备名跟随后端：刷新后输入框不该停在"未配对时的空值"。
  useEffect(() => {
    if (paired && !replacing) setName(paired.name);
  }, [paired, replacing]);

  const trimmed = name.trim();
  const nameProblem =
    trimmed.length === 0
      ? '先给这台电脑起个名字（例如「客厅 Windows」）。'
      : trimmed.length > MAX_DEVICE_NAME
        ? `名字最多 ${MAX_DEVICE_NAME} 个字符。`
        : null;

  const submitPair = async () => {
    if (nameProblem) {
      setLocalError(nameProblem);
      return;
    }
    setLocalError(null);
    await onPair(trimmed);
    setReplacing(false);
  };

  return (
    <section className="desktop-card" data-testid="desktop-pairing">
      <header className="desktop-card__head">
        <h2 className="desktop-card__title">{paired ? '配对' : '配对这台电脑'}</h2>
        {paired ? (
          <button
            type="button"
            className="ghost-button"
            data-testid="desktop-repair-toggle"
            onClick={() => {
              setReplacing((v) => !v);
              setConfirmUnpair(false);
            }}
          >
            {replacing ? '收起' : '重新配对'}
          </button>
        ) : null}
      </header>

      {!paired || replacing ? (
        <div className="desktop-form">
          <label className="desktop-field">
            <span className="desktop-field__label">电脑名称</span>
            <input
              className="desktop-field__input"
              data-testid="desktop-name"
              value={name}
              maxLength={MAX_DEVICE_NAME * 2}
              placeholder="客厅 Windows"
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          {replacing ? (
            <p className="desktop-danger-note" data-testid="desktop-repair-warning">
              重新配对该电脑会立即替换旧令牌、终止在途任务，并暂停接收。之后要用新令牌在那台电脑上重启伴生端。
            </p>
          ) : null}
          {localError ? <p className="desktop-inline-error" role="alert">{localError}</p> : null}
          <div className="desktop-form__actions">
            <button
              type="button"
              className="button button--primary"
              data-testid="desktop-pair-submit"
              disabled={busy}
              onClick={() => void submitPair()}
            >
              {busy ? '生成中…' : paired ? '替换令牌并重新配对' : '生成配对令牌'}
            </button>
          </div>
        </div>
      ) : (
        <p className="desktop-card__note">
          已配对。需要换令牌或换一台电脑时用「重新配对」；那会中断正在执行的任务。
        </p>
      )}

      {token ? (
        <div className="desktop-token" data-testid="desktop-token">
          <div className="desktop-token__head">
            <strong>配对令牌</strong>
            <span className="desktop-token__warn">只显示这一次</span>
          </div>
          <div className="desktop-token__row">
            <code className="desktop-token__value" data-testid="desktop-token-value">{token}</code>
            <button
              type="button"
              className="button button--small"
              data-testid="desktop-token-copy"
              onClick={() => void copy('token', token)}
            >
              {copied === 'token' ? <><CheckIcon /> 已复制</> : '复制令牌'}
            </button>
          </div>
          {copied?.startsWith('failed') ? (
            <p className="desktop-inline-error">浏览器没有给出剪贴板权限。请手动选中上面的令牌复制。</p>
          ) : null}

          <ol className="desktop-steps-list">
            <li>
              在<b>已登录、未锁屏</b>的 Windows 上，用普通用户打开终端，装好 Python 3.12+ 并在 Ling 源码目录运行：
              <div className="desktop-command">
                <pre data-testid="desktop-command">{launchCommand(url)}</pre>
                <button
                  type="button"
                  className="ghost-button"
                  data-testid="desktop-command-copy"
                  onClick={() => void copy('command', launchCommand(url))}
                >
                  {copied === 'command' ? '已复制' : '复制命令'}
                </button>
              </div>
            </li>
            <li>
              终端提示 <b>配对令牌</b> 时粘贴上面这一串（只有 {token.length} 个字符，不含空格）。
              也可以提前设成环境变量 <code>LING_DESKTOP_TOKEN</code>。
            </li>
            <li>
              回到这一页等「电脑」变成<b>已连接，桌面可用</b>；如果还是暂停，点「恢复接收」。
            </li>
          </ol>
          <div className="desktop-token__foot">
            <span className="desktop-token__masked" title="令牌在页面上只以掩码显示">
              页面上仅显示 {maskToken(token)}
            </span>
            <button type="button" className="ghost-button" data-testid="desktop-token-dismiss" onClick={onTokenDone}>
              我已粘贴好，隐藏令牌
            </button>
          </div>
        </div>
      ) : null}

      {paired ? (
        <div className="desktop-unpair">
          {confirmUnpair ? (
            <div className="desktop-confirm" data-testid="desktop-unpair-confirm">
              <p>解除配对会撤销令牌、终止在途任务，但<b>保留任务历史</b>。</p>
              <div className="desktop-form__actions">
                <button
                  type="button"
                  className="button button--small"
                  data-testid="desktop-unpair-submit"
                  disabled={busy}
                  onClick={() => {
                    setConfirmUnpair(false);
                    void onUnpair();
                  }}
                >
                  解除配对
                </button>
                <button type="button" className="ghost-button" onClick={() => setConfirmUnpair(false)}>
                  取消
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              className="ghost-button desktop-unpair__open"
              data-testid="desktop-unpair"
              onClick={() => setConfirmUnpair(true)}
            >
              解除配对
            </button>
          )}
        </div>
      ) : null}
    </section>
  );
}

/* ------------------------------------------------------------ 任务面板 */

/**
 * 提交区。
 *
 * - **多行**：`textarea`，`white-space: pre-wrap`，不 trim、不折叠空格。
 * - **同一次提交只用一个编号**：编号在"开始提交"时生成并保存，`catch` 里的
 *   「重试」复用它；只有**成功之后**（或换了一段文本再提交）才换新编号。
 * - 在途任务期间输入框仍可编辑（用户可以先写好下一条），提交按钮禁用并说明
 *   原因——输入被锁住会让人以为界面卡了。
 */
export function SubmitPanel({
  status,
  onSubmit,
  busy,
  onCancel,
  cancelBusy,
}: {
  status: DesktopStatus;
  onSubmit: (text: string, requestId: string) => Promise<boolean>;
  busy: boolean;
  onCancel: (task: DesktopTask) => void;
  cancelBusy: boolean;
}): React.ReactElement {
  const [text, setText] = useState('');
  const [localError, setLocalError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  // 失败后保留这段文字与编号，给出「用同一个编号重试」。
  const [retry, setRetry] = useState<{ text: string; requestId: string } | null>(null);
  // 成功后把输入框清空，但**保留一份可继续编辑的内容**：用户可能想再写一段相近的。
  const [lastSent, setLastSent] = useState<string | null>(null);
  const active = activeTaskOf(status.tasks);
  const capability = capabilityOf(status);
  const length = noteLength(text);
  const overLimit = length > 2000;

  const submit = async (payload = text, requestId?: string) => {
    const check = checkNote(payload);
    if (!check.ok) {
      setLocalError(check.message);
      return;
    }
    setLocalError(null);
    const id = requestId ?? pendingId ?? newRequestId();
    setPendingId(id);
    const ok = await onSubmit(payload, id);
    if (ok) {
      setRetry(null);
      setPendingId(null);
      setLastSent(payload);
      // 只清空"刚提交的这段"：用户已经改了内容就别覆盖他的新草稿。
      setText((current) => (current === payload ? '' : current));
    } else {
      setRetry({ text: payload, requestId: id });
      // 编号留给下一次重试复用；新提交会显式换新号。
      setPendingId(id);
    }
  };

  const startFresh = () => {
    // 换新编号：上一段已经用旧号失败过，继续用旧号会被判成同号异文。
    setPendingId(null);
    setRetry(null);
    setLocalError(null);
  };

  const disabledReason = !capability.canSubmit
    ? capability.blocker
    : active
      ? '上一条还在等待或执行。等它结束，或先取消它。'
      : null;

  return (
    <section className="desktop-card desktop-submit" data-testid="desktop-submit">
      <header className="desktop-card__head">
        <h2 className="desktop-card__title">写一段测试文本</h2>
        <span className="desktop-card__meta" data-testid="desktop-submit-count" data-over={overLimit ? 'true' : 'false'}>
          {length} / 2000
        </span>
      </header>

      <textarea
        className="desktop-textarea"
        data-testid="desktop-note"
        value={text}
        rows={5}
        placeholder={'例如：第一行\n第二行带空格和 emoji 😀'}
        onChange={(event) => {
          setText(event.target.value);
          if (localError) setLocalError(null);
          // 内容变了就不再是"同一段文本的重试"：旧编号作废，避免同号异文。
          if (retry && event.target.value !== retry.text) setRetry(null);
        }}
      />

      <p className="desktop-card__note">
        文字会原样进入记事本编辑区并被逐字回读。换行、连续空格、中文与 emoji 都保留。
      </p>

      {localError ? <p className="desktop-inline-error" role="alert" data-testid="desktop-note-error">{localError}</p> : null}

      {retry && capability.canSubmit && !active ? (
        <div className="desktop-retry" data-testid="desktop-submit-retry">
          <span>刚才那次没有送达服务端（或服务端没接受）。可以用同一个提交编号重试，不会产生两条任务。</span>
          <div className="desktop-form__actions">
            <button
              type="button"
              className="button button--small button--primary"
              disabled={busy}
              onClick={() => void submit(retry.text, retry.requestId)}
            >
              用编号 {retry.requestId.slice(0, 8)}… 重试
            </button>
            <button type="button" className="ghost-button" onClick={startFresh}>
              换新编号重来
            </button>
          </div>
        </div>
      ) : null}

      <div className="desktop-form__actions desktop-submit__actions">
        <button
          type="button"
          className="button button--primary"
          data-testid="desktop-submit-button"
          disabled={busy || disabledReason !== null}
          title={disabledReason ?? undefined}
          onClick={() => void submit()}
        >
          {busy ? '提交中…' : '提交到记事本'}
        </button>
        {disabledReason ? (
          <span className="desktop-submit__blocked" data-testid="desktop-submit-blocked">{disabledReason}</span>
        ) : null}
        {active ? (
          <button
            type="button"
            className="button button--small"
            data-testid="desktop-cancel-active"
            disabled={cancelBusy}
            onClick={() => onCancel(active)}
          >
            {cancelBusy ? '取消中…' : '取消这条任务'}
          </button>
        ) : null}
      </div>

      {lastSent !== null ? (
        <div className="desktop-last-sent" data-testid="desktop-last-sent">
          <span>已提交的内容仍保留在记事本里，可以继续编辑后提交新的一条。</span>
          <button type="button" className="ghost-button" onClick={() => setText(lastSent)}>
            把上一段放回输入框
          </button>
        </div>
      ) : null}
    </section>
  );
}

/** 一条任务的进度条：四步。 */
export function StepTrack({ task }: { task: DesktopTask }): React.ReactElement {
  return (
    <ol className="desktop-track" data-testid="desktop-track" data-state={task.state}>
      {stepsOf(task).map((step) => (
        <li
          key={step.key}
          className="desktop-track__step"
          data-done={step.done ? 'true' : 'false'}
          data-current={step.current ? 'true' : 'false'}
        >
          <span className="desktop-track__mark" aria-hidden="true" />
          <span className="desktop-track__label">{step.label}</span>
        </li>
      ))}
    </ol>
  );
}

/** 任务行：结果、原因、步进、时间。列表与「当前这一步」共用同一份渲染。 */
export function TaskRow({
  task,
  onCancel,
  cancelBusy,
  expanded,
  compact,
}: {
  task: DesktopTask;
  onCancel?: (task: DesktopTask) => void;
  cancelBusy?: boolean;
  expanded?: boolean;
  /**
   * 在「最近任务」里复现在途任务时用：只留一行"状态 + 去上面操作"，
   * 不重复铺开进度与全文（那些在上面「这一步」里已经是主视图）。
   */
  compact?: boolean;
}): React.ReactElement {
  const outcome = outcomeOf(task);
  const active = task.state === 'queued' || task.state === 'running';
  return (
    <li className="desktop-task" data-testid="desktop-task" data-state={task.state} data-expanded={expanded ? 'true' : 'false'}>
      <div className="desktop-task__head">
        <span className={`desktop-outcome desktop-outcome--${outcome.tone}`} data-testid="desktop-task-outcome">
          {outcome.title}
        </span>
        <span className="desktop-task__meta">
          {task.source === 'life' ? (
            <span className="desktop-tag" data-testid="desktop-task-source-life">来自授权</span>
          ) : null}
          <span title={task.created_at}>{clockOf(task.created_at)}</span>
          {task.reason ? <code className="desktop-task__reason">{task.reason}</code> : null}
        </span>
      </div>

      {compact ? (
        <p className="desktop-task__detail">
          正在执行，进度见上方「这一步」。
        </p>
      ) : (
        <>
          <StepTrack task={task} />
          <p className="desktop-task__detail">{outcome.detail}</p>
        </>
      )}

      {compact ? null : (
        <p className="desktop-task__text" data-testid="desktop-task-text">
          {task.text}
        </p>
      )}

      <div className="desktop-task__foot">
        <span className="desktop-task__times">
          提交 {clockOf(task.created_at)}
          {task.started_at ? ` · 开始 ${clockOf(task.started_at)}` : ''}
          {task.finished_at ? ` · 结束 ${clockOf(task.finished_at)}` : ''}
        </span>
        {active && onCancel ? (
          <button
            type="button"
            className="button button--small"
            data-testid="desktop-task-cancel"
            disabled={cancelBusy}
            onClick={() => onCancel(task)}
          >
            {cancelBusy ? '取消中…' : '取消'}
          </button>
        ) : null}
      </div>
    </li>
  );
}

export function CurrentPanel({
  status,
  onCancel,
  cancelBusy,
}: {
  status: DesktopStatus;
  onCancel: (task: DesktopTask) => void;
  cancelBusy: boolean;
}): React.ReactElement | null {
  const active = activeTaskOf(status.tasks);
  // 没有在途任务时**整块不渲染**：一张只写"现在没有在途任务"的卡片占掉一屏宽度
  // 而不提供任何信息；最近一条的结果本来就在「最近任务」里，那里是它的家。
  if (!active) return null;
  return (
    <section className="desktop-card desktop-current" data-testid="desktop-current">
      <header className="desktop-card__head">
        <h2 className="desktop-card__title">这一步</h2>
        <span className="desktop-card__meta">{active.state === 'running' ? '执行中' : '排队中'}</span>
      </header>
      <ul className="desktop-tasks desktop-tasks--single">
        <TaskRow task={active} onCancel={onCancel} cancelBusy={cancelBusy} expanded />
      </ul>
    </section>
  );
}

export function HistoryPanel({
  tasks,
  onCancel,
  cancelBusy,
}: {
  tasks: DesktopTask[];
  onCancel?: (task: DesktopTask) => void;
  cancelBusy?: boolean;
}): React.ReactElement {
  return (
    <section className="desktop-card" data-testid="desktop-history">
      <header className="desktop-card__head">
        <h2 className="desktop-card__title">最近任务</h2>
        <span className="desktop-card__meta">最近 {tasks.length} 条</span>
      </header>
      {tasks.length === 0 ? (
        <p className="desktop-card__note">还没有任务。成功的一条会显示「已写入并从记事本回读」。</p>
      ) : (
        <ul className="desktop-tasks">
          {tasks.map((task) => (
            <TaskRow
              key={task.id}
              task={task}
              onCancel={onCancel}
              cancelBusy={cancelBusy}
              // 在途任务已经在上面的「这一步」里展开显示；历史里再画一遍会让
              // 同一条任务在一屏出现两次（同一个 testid 命中两个元素）。
              compact={isActive(task)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

/* ------------------------------------------------------------ 授权面板 */

export function GrantPanel({
  grant,
  onSave,
  onRevoke,
  busy,
  status,
}: {
  grant: DesktopActivityGrant | null;
  onSave: (body: { start_at: string; end_at: string; text: string }) => Promise<boolean>;
  onRevoke: () => Promise<void>;
  busy: boolean;
  status: DesktopStatus;
}): React.ReactElement {
  const now = Date.now();
  const defaultStart = useMemo(() => isoToLocalInput(new Date(now + 10 * 60_000).toISOString()), [now]);
  const defaultEnd = useMemo(() => isoToLocalInput(new Date(now + 70 * 60_000).toISOString()), [now]);
  const [startLocal, setStartLocal] = useState(grant ? isoToLocalInput(grant.start_at) : defaultStart);
  const [endLocal, setEndLocal] = useState(grant ? isoToLocalInput(grant.end_at) : defaultEnd);
  const [text, setText] = useState(grant?.text ?? '');
  const [localError, setLocalError] = useState<string | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  // 编号由页面层持有（`grantRequestIdRef`）：面板只负责收集内容，
  // 重试沿用同一编号的语义不能分散在两个组件里。
  const view = grant ? grantView(grant) : null;
  const capability = capabilityOf(status);
  const length = noteLength(text);
  const canAuthorize = status.device !== null;

  const save = async () => {
    const windowCheck = checkGrantWindow(startLocal, endLocal);
    if (!windowCheck.ok) {
      setLocalError(windowCheck.message);
      return;
    }
    const check = checkNote(text);
    if (!check.ok) {
      setLocalError(check.message);
      return;
    }
    const start = localInputToIso(startLocal);
    const end = localInputToIso(endLocal);
    if (!start || !end) {
      setLocalError('时间格式无法解析。');
      return;
    }
    setLocalError(null);
    await onSave({ start_at: start, end_at: end, text });
  };

  return (
    <section className="desktop-card desktop-grant" data-testid="desktop-grant">
      <header className="desktop-card__head">
        <h2 className="desktop-card__title">限时授权</h2>
        {view ? (
          <span className="desktop-grant__decision" data-testid="desktop-grant-decision" data-decision={grant!.decision}>
            {view.title}
          </span>
        ) : (
          <span className="desktop-card__meta">未授权</span>
        )}
      </header>

      {grant && view ? (
        <div className="desktop-grant__current" data-testid="desktop-grant-current">
          <p className="desktop-grant__range">{formatRange(grant.start_at, grant.end_at)}</p>
          <p className="desktop-grant__detail">{view.detail}</p>
          {view.taskId ? (
            <p className="desktop-grant__task" data-testid="desktop-grant-task">
              对应任务 <code>{view.taskId.slice(0, 8)}…</code>，结果在「最近任务」里按「来自授权」找。
            </p>
          ) : null}
          <pre className="desktop-grant__text">{grant.text}</pre>
          {grant.decision !== 'revoked' ? (
            confirmRevoke ? (
              <div className="desktop-confirm" data-testid="desktop-grant-revoke-confirm">
                <p>撤销后当次已入队或执行的活动会随授权结束；已完成的写作记录留在生活时间线里。</p>
                <div className="desktop-form__actions">
                  <button
                    type="button"
                    className="button button--small"
                    data-testid="desktop-grant-revoke-submit"
                    disabled={busy}
                    onClick={() => {
                      setConfirmRevoke(false);
                      void onRevoke();
                    }}
                  >
                    撤销授权
                  </button>
                  <button type="button" className="ghost-button" onClick={() => setConfirmRevoke(false)}>
                    取消
                  </button>
                </div>
              </div>
            ) : (
              <button
                type="button"
                className="ghost-button"
                data-testid="desktop-grant-revoke"
                onClick={() => setConfirmRevoke(true)}
              >
                撤销这次授权
              </button>
            )
          ) : null}
        </div>
      ) : null}

      <p className="desktop-card__note">
        在指定时段里，模型结合人设与当前安排决定<b>写作</b>还是<b>休息</b>；日程已安排休息时直接休息。
        每份授权最多执行一次，保存在本地，结果只从真实回读产生。
      </p>

      {canAuthorize ? (
        <div className="desktop-form desktop-grant__form">
          <div className="desktop-grant__times">
            <label className="desktop-field">
              <span className="desktop-field__label">开始</span>
              <input
                type="datetime-local"
                className="desktop-field__input"
                data-testid="desktop-grant-start"
                value={startLocal}
                onChange={(event) => setStartLocal(event.target.value)}
              />
            </label>
            <label className="desktop-field">
              <span className="desktop-field__label">截止</span>
              <input
                type="datetime-local"
                className="desktop-field__input"
                data-testid="desktop-grant-end"
                value={endLocal}
                onChange={(event) => setEndLocal(event.target.value)}
              />
            </label>
          </div>
          <label className="desktop-field">
            <span className="desktop-field__label">
              许可写入的原文 <span className="desktop-field__count">{length} / 2000</span>
            </span>
            <textarea
              className="desktop-textarea desktop-textarea--short"
              data-testid="desktop-grant-text"
              rows={4}
              value={text}
              onChange={(event) => setText(event.target.value)}
            />
          </label>
          {localError ? <p className="desktop-inline-error" role="alert" data-testid="desktop-grant-error">{localError}</p> : null}
          <div className="desktop-form__actions">
            <button
              type="button"
              className="button button--primary"
              data-testid="desktop-grant-save"
              disabled={busy}
              onClick={() => void save()}
            >
              {busy ? '保存中…' : grant ? '替换为这份授权' : '保存授权'}
            </button>
            {capability.canSubmit ? null : (
              <span className="desktop-card__meta">
                授权可以先保存；真正写入时会再核对设备。
              </span>
            )}
          </div>
        </div>
      ) : (
        <p className="desktop-card__note">配对一台电脑之后才能授权。</p>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ 页面 */

/** 两次拉取的任务集合是否同构（用于判断"是不是有新进展"）。 */
function sameTaskIds(a: readonly DesktopTask[], b: readonly DesktopTask[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((task, index) => task.id === b[index]?.id && task.state === b[index]?.state && task.step === b[index]?.step);
}

export interface DesktopPageProps {
  /** 应用壳层给出的站点根地址，用于拼启动命令里的 `--url`。 */
  siteUrl: string;
}

export function DesktopPage({ siteUrl }: DesktopPageProps): React.ReactElement {
  const [status, setStatus] = useState<DesktopStatus | null>(null);
  const [grant, setGrant] = useState<DesktopActivityGrant | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [loadError, setLoadError] = useState<ExplainedError | null>(null);
  const [actionError, setActionError] = useState<ExplainedError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  /** 授权保存的待用编号：失败后重试沿用，成功后才换新号。 */
  const grantRequestIdRef = useRef<string | null>(null);

  /**
   * 拉一次真实状态（设备 + 任务 + 授权）。
   *
   * 两条都用同一个 signal：轮询一拍要么都成功，要么一起放弃。
   * 授权拉失败**不**清掉已有的 grant——那会把"读不到"显示成"没有授权"，
   * 正是这一页最不该出现的假象；错误走上方的错误条。
   */
  const refresh = useCallback(async (signal: AbortSignal) => {
    const next = await api.desktopStatus(signal);
    // 一次性提示（"已提交…"）是**对上一个动作**的回执，下一拍真实状态到了就该退场：
    // 任务变成 succeeded 之后还挂着"等待伴生端领取"，会把已经完成的事说成在等。
    // 这里按"任务集合变了"判断，而不是按时间自动消失——时间到了但状态没变时，
    // 那条提示仍然是对的，不该被计时器抹掉。
    setStatus((previous) => {
      if (previous && !sameTaskIds(previous.tasks, next.tasks)) setNotice(null);
      return next;
    });
    setLoaded(true);
    setForbidden(false);
    setLoadError(null);
    try {
      const autonomy = await api.desktopAutonomy(signal);
      setGrant(autonomy.grant);
    } catch (error) {
      // 403 会由下面统一抛；其余错误保留旧 grant，只显示错误。
      if (error instanceof ApiError && error.status === 403) throw error;
      setLoadError(explainError(error));
    }
  }, []);

  // 轮询：进页面立刻一拍，离开/卸载时清定时器并 abort 在途请求。
  useEffect(() => {
    let disposed = false;
    const stop = startDesktopPolling({
      intervalMs: DESKTOP_POLL_MS,
      // 授权还有"活着"的决定时也要继续轮询（pending/deciding/queued 会被后端改写）；
      // grant 状态变化不影响下一拍，这里只用来选间隔——固定 2 秒足够。
      fetchOnce: async (signal) => {
        if (disposed) return;
        await refresh(signal);
      },
      onSuccess: () => {
        if (!disposed) setLoadError(null);
      },
      onError: (error) => {
        if (disposed) return;
        if (error instanceof ApiError && error.status === 403) {
          setForbidden(true);
          return;
        }
        setLoadError(explainError(error));
        // 首拍失败也要离开"加载中"，否则页面永远停在骨架屏。
        setLoaded(true);
      },
    });
    return () => {
      disposed = true;
      stop();
    };
  }, [refresh]);

  const runAction = useCallback(
    async (key: string, action: () => Promise<DesktopStatus | void>, success?: string) => {
      setBusy(key);
      setActionError(null);
      setNotice(null);
      try {
        const next = await action();
        if (next) {
          setStatus(next);
          setLoaded(true);
        } else {
          // 204（解除配对）：重新拉一次，拿到真实状态而不是自己猜。
          const fresh = await api.desktopStatus();
          setStatus(fresh);
          const autonomy = await api.desktopAutonomy().catch(() => null);
          if (autonomy) setGrant(autonomy.grant);
        }
        if (success) setNotice(success);
      } catch (error) {
        if (error instanceof ApiError && error.status === 403) {
          setForbidden(true);
          return;
        }
        setActionError(writeError(error, '按最新状态再试一次；必要时刷新页面。'));
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  const pair = async (name: string) => {
    await runAction(
      'pair',
      async () => {
        const result = await api.pairDesktop(name);
        setToken(result.token);
        const fresh = await api.desktopStatus();
        setGrant(null);
        return fresh;
      },
      '已生成新的配对令牌。接收处于暂停，等伴生端就绪后恢复。',
    );
  };

  const unpair = async () => {
    await runAction(
      'unpair',
      async () => {
        await api.unpairDesktop();
        setToken(null);
        setGrant(null);
      },
      '已解除配对，任务历史保留。',
    );
  };

  const resume = async () => {
    await runAction('resume', () => api.controlDesktop(false), '已恢复接收。');
  };

  const pause = async () => {
    await runAction(
      'pause',
      () => api.controlDesktop(true),
      '已暂停接收。已经开始的单次动作可能仍会完成，页面会保留最后收到的步骤。',
    );
  };

  /** 返回 true 表示服务端已接受，输入框可以清空。 */
  const submit = async (text: string, requestId: string): Promise<boolean> => {
    setBusy('submit');
    setActionError(null);
    try {
      const task = await api.createDesktopTask(requestId, text);
      setStatus((current) =>
        current ? { ...current, tasks: [task, ...current.tasks.filter((item) => item.id !== task.id)] } : current,
      );
      setNotice('已提交，等待伴生端领取。');
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.status === 403) {
        setForbidden(true);
        return false;
      }
      setActionError(writeError(error, '刷新看一下真实状态，再决定是重试还是换新编号。'));
      return false;
    } finally {
      setBusy(null);
    }
  };

  const cancel = async (task: DesktopTask) => {
    setCancelBusy(true);
    setActionError(null);
    try {
      const updated = await api.cancelDesktopTask(task.id);
      setStatus((current) =>
        current
          ? { ...current, tasks: current.tasks.map((item) => (item.id === updated.id ? updated : item)) }
          : current,
      );
      setNotice(
        updated.state === 'cancelled'
          ? '已取消这条任务。可以随时提交下一条。'
          : `这条任务已经结束（${updated.state}），取消没有改变结果。`,
      );
    } catch (error) {
      if (error instanceof ApiError && error.status === 403) {
        setForbidden(true);
        return;
      }
      setActionError(writeError(error, '刷新看一下这条任务的真实状态。'));
    } finally {
      setCancelBusy(false);
    }
  };

  const saveGrant = async (body: { start_at: string; end_at: string; text: string }): Promise<boolean> => {
    setBusy('grant');
    setActionError(null);
    setNotice(null);
    try {
      // 同一份授权重试沿用同一个编号：后端对"同号同内容"返回已有授权而不是报冲突。
      grantRequestIdRef.current ??= newRequestId();
      const result = await api.authorizeDesktopActivity({ request_id: grantRequestIdRef.current, ...body });
      grantRequestIdRef.current = null;
      setGrant(result.grant);
      setNotice('授权已保存。');
      return true;
    } catch (error) {
      // 失败时保留编号：下一次「保存」就是同一份内容的重试。
      if (error instanceof ApiError && error.status === 403) {
        setForbidden(true);
        return false;
      }
      setActionError(writeError(error, '检查时段与设备状态后再保存。'));
      return false;
    } finally {
      setBusy(null);
    }
  };

  const revokeGrant = async () => {
    setBusy('revoke');
    setActionError(null);
    try {
      const result = await api.revokeDesktopActivity();
      setGrant(result.grant);
      setNotice('授权已撤销。');
    } catch (error) {
      setActionError(writeError(error, '刷新看一下授权状态。'));
    } finally {
      setBusy(null);
    }
  };

  /* --------------------------------------------------------- 403 / 加载 */

  if (forbidden) {
    return (
      <section className="page" data-testid="desktop-forbidden">
        <h1 className="page__title">Windows 桌面任务</h1>
        <div className="banner banner--error desktop-banner" role="alert">
          <AlertIcon />
          <div className="desktop-banner__body">
            <strong>这个页面只对所有者开放</strong>
            <div className="banner__detail">
              当前账号是普通成员，接口返回 403。配对令牌能让那台电脑登录并写记事本，
              所以这里只给所有者。
            </div>
          </div>
        </div>
      </section>
    );
  }

  if (!loaded && !status) {
    return (
      <section className="page" data-testid="desktop-loading">
        <h1 className="page__title">Windows 桌面任务</h1>
        <p className="page__loading"><span className="spinner" aria-hidden="true" /> 正在读取电脑与任务状态…</p>
      </section>
    );
  }

  // 首拍就失败、且没有任何状态可显示：给一个可重试的整页错误。
  if (!status) {
    return (
      <section className="page" data-testid="desktop-load-error">
        <h1 className="page__title">Windows 桌面任务</h1>
        <ErrorBanner
          error={loadError ?? { reason: '读不到桌面状态', hint: '稍后重试。', retriable: true, code: 'internal', detail: null, traceId: null }}
          onRetry={() => {
            setLoadError(null);
            void api.desktopStatus().then((next) => {
              setStatus(next);
              setLoaded(true);
            }).catch((error) => setLoadError(explainError(error)));
          }}
        />
      </section>
    );
  }

  const grantLive = grantIsLive(grant);

  return (
    <section className="page desktop-page" data-testid="desktop-page">
      <header className="page__head">
        <div>
          <h1 className="page__title">Windows 桌面任务</h1>
          <p className="page__subtitle">
            让 Ling 在一台 Windows 电脑上打开记事本、写入一段文字，从编辑区逐字回读。
            只有所有者能配对、暂停与授权。
          </p>
        </div>
        <button
          type="button"
          className="ghost-button"
          data-testid="desktop-refresh"
          onClick={() =>
            void refresh(new AbortController().signal).catch((error) => setLoadError(explainError(error)))
          }
        >
          <RefreshIcon /> 刷新
        </button>
      </header>

      {loadError ? <ErrorBanner error={loadError} testid="desktop-load-error-banner" /> : null}
      {actionError ? <ErrorBanner error={actionError} testid="desktop-action-error" /> : null}
      {notice ? (
        <div className="banner banner--ok desktop-banner" role="status" data-testid="desktop-notice">
          <CheckIcon />
          <div className="desktop-banner__body">{notice}</div>
        </div>
      ) : null}

      {grantLive ? (
        <p className="desktop-live-note" data-testid="desktop-polling-note">
          授权还在推进，本页每 {DESKTOP_POLL_MS / 1000} 秒刷新一次，决定变化会自己出现。
        </p>
      ) : null}

      <div className="desktop-grid">
        <DevicePanel
          status={status}
          busy={busy === 'resume' || busy === 'pause'}
          onResume={() => void resume()}
          onPause={() => void pause()}
        />
        <PairingPanel
          status={status}
          token={token}
          busy={busy === 'pair' || busy === 'unpair'}
          url={siteUrl}
          onPair={pair}
          onUnpair={unpair}
          onTokenDone={() => setToken(null)}
        />
      </div>

      {/*
        顺序 = 三个问题被问到的顺序：
        ①「能接任务吗」（上面两块）
        ②「Ling 做到哪一步了」（这一步 + 最近任务）
        ③「我要做什么」（提交 / 授权）。
        之前在途任务藏在输入框下面，进页面先看到一个大输入框，得往下滚才知道
        上一条是成功了还是卡住了——那正是这一页最该先回答的问题。
      */}
      <CurrentPanel status={status} onCancel={(task) => void cancel(task)} cancelBusy={cancelBusy} />

      <HistoryPanel tasks={status.tasks} onCancel={(task) => void cancel(task)} cancelBusy={cancelBusy} />

      <SubmitPanel
        status={status}
        busy={busy === 'submit'}
        onSubmit={submit}
        onCancel={(task) => void cancel(task)}
        cancelBusy={cancelBusy}
      />

      <GrantPanel
        grant={grant}
        status={status}
        busy={busy === 'grant' || busy === 'revoke'}
        onSave={saveGrant}
        onRevoke={revokeGrant}
      />
    </section>
  );
}
