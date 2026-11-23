/**
 * Windows 桌面任务：**纯函数**的语义层（无 React、无 fetch）。
 *
 * 为什么把这些提到这里：这一页最难的不是渲染，而是"此刻到底能不能提交、
 * 下一步该由谁做什么"。设备三态 × 暂停 × 在途任务 × 回执步骤，组合起来有
 * 十几种情形，如果判断散落在 JSX 的 `&&` 里，就没人能证明"已覆盖全部组合"——
 * 而且测试只能对着渲染字符串猜。这里把它们收成几张小表 + 一次判定，
 * `desktop.test.ts` 直接对组合做穷举断言，`desktop.browser.test.tsx`
 * 只负责"画出来是否溢出、按钮是否真的可点"。
 *
 * 后端载荷字段名取自 `src/ling/contracts/desktop.py`（`DesktopStatus` /
 * `DesktopTask` / `DesktopDevice` / `DesktopActivityGrant`），不另造近似的形状。
 */

/** 设备在线状态。`unavailable` = 伴生端在线，但交互桌面不可用（锁屏等）。 */
export type DesktopDeviceState = 'offline' | 'unavailable' | 'ready';

export type DesktopTaskState =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'interrupted'
  | 'cancelled';

/** 服务端**已经收到**的最后一步回执。`null` = 还没有任何回执。 */
export type DesktopTaskStep = 'opened' | 'written' | 'verified' | null;

export interface DesktopDevice {
  name: string;
  state: DesktopDeviceState;
  last_seen_at: string | null;
}

export interface DesktopTask {
  id: string;
  kind: 'notepad_write';
  text: string;
  state: DesktopTaskState;
  step: DesktopTaskStep;
  reason: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  source: 'manual' | 'life';
  source_segment_id: string | null;
}

export interface DesktopStatus {
  device: DesktopDevice | null;
  paused: boolean;
  tasks: DesktopTask[];
}

export type GrantDecision =
  | 'pending'
  | 'deciding'
  | 'rest'
  | 'queued'
  | 'expired'
  | 'revoked'
  | 'unavailable';

export interface DesktopActivityGrant {
  request_id: string;
  text: string;
  start_at: string;
  end_at: string;
  decision: GrantDecision;
  task_id: string | null;
}

export interface DesktopActivityStatus {
  grant: DesktopActivityGrant | null;
}

/** 轮询间隔。2 秒与伴生端心跳同量级：步骤推进（opened→written→verified）能看见。 */
export const DESKTOP_POLL_MS = 2000;

/** 文本上限与后端合同一致（`MAX_NOTE_LENGTH = 2000`）。 */
export const MAX_NOTE_LENGTH = 2000;
/** 电脑名称上限（`PairDesktop.name` 最长 64）。 */
export const MAX_DEVICE_NAME = 64;

const ACTIVE_STATES: ReadonlySet<DesktopTaskState> = new Set(['queued', 'running']);

export function isActive(task: DesktopTask): boolean {
  return ACTIVE_STATES.has(task.state);
}

/** 在途任务（后端保证最多一条）。 */
export function activeTaskOf(tasks: readonly DesktopTask[]): DesktopTask | null {
  return tasks.find(isActive) ?? null;
}

/* ------------------------------------------------------------- 文本校验 */

export type NoteCheck = { ok: true } | { ok: false; message: string };

/**
 * 提交前的本地校验。
 *
 * 只拦后端**一定**会拒的情况（空/全空白、超长），逐字保留其余内容——
 * 换行、连续空格、emoji、中文都不能被前端"顺手规整"掉：这段字会原样进记事本
 * 编辑区，并被逐字回读比对，任何前端侧的 trim 都会制造 readback_mismatch。
 *
 * 长度按 Unicode 码点算（`Array.from`）而不是 `String.length`：后端是
 * Pydantic 的 `max_length` 按**码点**计，而 `length` 按 UTF-16 码元计，
 * 一行 emoji 会被算成两倍长，前端就会拒绝一段后端本来接受、记事本也放得下的文字。
 */
export function checkNote(text: string): NoteCheck {
  if (!text.trim()) return { ok: false, message: '写一点内容再提交：记事本里至少要有可回读的文字。' };
  const length = Array.from(text).length;
  if (length > MAX_NOTE_LENGTH) {
    return { ok: false, message: `超出 ${length - MAX_NOTE_LENGTH} 个字符（上限 ${MAX_NOTE_LENGTH}）。` };
  }
  // 控制字符会被后端以 invalid_request 拒掉；这里提前说清是哪一类，不猜具体位置。
  if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(text)) {
    return { ok: false, message: '文本里含不可写入记事本的控制字符（除换行、制表符外）。' };
  }
  return { ok: true };
}

export function noteLength(text: string): number {
  return Array.from(text).length;
}

/* ------------------------------------------------------- 设备与可操作性 */

/**
 * 用户的**意图**状态：她想让这台电脑接任务吗。
 *
 * 这是界面上"暂停 / 恢复"按钮的字面意思，也是与 `device.state` **正交**的一维。
 * 正交必须显式保留：后端在"暂停时伴生端掉线"之后，`paused` 仍为 true、
 * `state` 变成 offline；若把两者压成一个枚举，恢复按钮的可用性就只能靠猜。
 */
export type ReceiveIntent = 'accepting' | 'paused';

export function receiveIntent(status: DesktopStatus): ReceiveIntent {
  return status.paused ? 'paused' : 'accepting';
}

export interface DesktopCapability {
  /** 能不能现在提交一段测试文本。 */
  canSubmit: boolean;
  /** 能不能点「恢复接收」。 */
  canResume: boolean;
  /** 不能提交的原因（可行动的一句话）；能提交时为 null。 */
  blocker: string | null;
  /** 这台电脑此刻的连接状况，一句话。 */
  connection: string;
}

/**
 * 三态 × 暂停 → 能做什么。
 *
 * 判据（与后端 `PUT /control`、`POST /tasks` 的校验顺序一致）：
 * - 没配对：什么都不能做，先配对。
 * - 已暂停：不能提交；恢复需要 `state === 'ready'`（后端对非就绪返回
 *   `409 desktop_not_ready`）。这就是"恢复接收需要设备就绪"这条要求。
 * - 未暂停：必须 `ready` 才能提交。`unavailable` 是伴生端在线但桌面不可用
 *   （锁屏、切走了窗口），下一步是回 Windows 解锁——不是"重新配对"。
 */
export function capabilityOf(status: DesktopStatus | null): DesktopCapability {
  const device = status?.device ?? null;
  if (!status || !device) {
    return {
      canSubmit: false,
      canResume: false,
      blocker: '还没有配对 Windows 电脑。先生成配对令牌，在那台电脑上启动伴生端。',
      connection: '未配对',
    };
  }
  const connection = describeDevice(device);
  if (status.paused) {
    // 暂停时三种设备状态都是"不能提交、不能立刻恢复"，但**下一步不一样**：
    // 只有 ready 是"点恢复接收"；unavailable 要回 Windows 解锁；
    // offline 要去确认伴生端还在跑。合成一句会让锁屏的人去重启进程、
    // 让断线的人去解锁屏幕——判定值相同并不代表原因相同。
    return {
      canSubmit: false,
      canResume: device.state === 'ready',
      blocker:
        device.state === 'ready'
          ? '接收已暂停。点「恢复接收」后就可以提交。'
          : device.state === 'unavailable'
            ? '接收已暂停，且 Windows 交互桌面不可用（锁屏或切到了别的窗口）。回那台电脑解锁并让桌面保持前台，「恢复接收」才会可用。'
            : '接收已暂停，伴生端当前离线。确认那台电脑上的 ling 伴生端仍在运行，连接回来后「恢复接收」才会可用。',
      connection,
    };
  }
  if (device.state !== 'ready') {
    return {
      canSubmit: false,
      canResume: false,
      blocker:
        device.state === 'unavailable'
          ? '伴生端在线，但 Windows 交互桌面不可用（锁屏或切到了别的窗口）。'
          : '伴生端离线。确认那台电脑上的 ling 伴生端仍在运行。',
      connection,
    };
  }
  return { canSubmit: true, canResume: false, blocker: null, connection };
}

export function describeDevice(device: DesktopDevice): string {
  switch (device.state) {
    case 'ready':
      return '已连接，桌面可用';
    case 'unavailable':
      return '伴生端在线，桌面不可用';
    case 'offline':
      return '离线';
    default: {
      const exhaustive: never = device.state;
      return exhaustive;
    }
  }
}

/* ------------------------------------------------------- 步骤与结果 */

export interface StepView {
  key: 'queued' | 'opened' | 'written' | 'verified';
  label: string;
  /** 该步是否已完成。 */
  done: boolean;
  /** 该步是否正在做（服务端已收到前一步回执，等这一步）。 */
  current: boolean;
}

const STEP_ORDER: ReadonlyArray<Exclude<StepView['key'], 'queued'>> = ['opened', 'written', 'verified'];

/** 四格的顺序（下标即顺序）：当前格与"已完成"都按它算。 */
const labelsOrder: ReadonlyArray<StepView['key']> = ['queued', 'opened', 'written', 'verified'];

const STEP_LABELS: Record<Exclude<StepView['key'], 'queued'>, string> = {
  opened: '打开记事本',
  written: '写入文本',
  verified: '回读校验',
};

function stepRank(step: DesktopTaskStep): number {
  if (step === null) return 0;
  return STEP_ORDER.indexOf(step) + 1;
}

/**
 * 四步进度：领取 → 打开 → 写入 → 回读。
 *
 * 完成度只看"服务端已收到的回执"：`step === 'opened'` 表示打开已回执、
 * 正等写入。所以已完成 = 已收到的回执数，当前格 = 它后面那一格——
 * 这里刻意不按 `state` 推断"可能已经写到哪"，因为界面唯一可信的事实来源是
 * 回执（`docs/WINDOWS-DESKTOP.md` 的原话：“记录服务端已经收到的最后一步回执”）。
 *
 * 步进与结果**分离**：`interrupted + written` 是真实组合（写入完成但租期到期），
 * 这时进度条停在"写入已完成、回读未做",结果单独说"被中断"，不能把进度抹掉
 * 假装什么都没发生。
 */
export function stepsOf(task: DesktopTask): StepView[] {
  /**
   * 一格是"当前"当且仅当**它还需要一份回执**（它是最后一格已完成的下一步）。
   * 所以当前格由 `step` 直接决定，不经过任何"第几个"的换算：
   *
   * - `null`      → 「等待领取」是当前格（还没人领）；
   * - `'opened'`  → 「写入文本」是当前格（打开已回执、正等写入）；
   * - `'written'` → 「回读校验」是当前格；
   * - `'verified'`→ 没有当前格（三份回执都到了）。
   *
   * 写成"下标换算"（`index === receipts + 1` 之类）正是这里连出两次缺陷的原因：
   * 一次让 `null` 同时点亮两格，一次让刚回执完的那格也算"正在做"。
   * 当前格既然由 `step` 唯一决定，就直接把它写成一张表，不再做算术。
   */
  const currentKey: StepView['key'] | null =
    task.step === null ? 'queued'
      : task.step === 'opened' ? 'written'
        : task.step === 'written' ? 'verified'
          : null;
  // 已完成 = 当前格之前的那些格 + （对 `verified` 而言）全部三格回执。
  const currentIndex = currentKey === null ? labelsOrder.length : labelsOrder.indexOf(currentKey);
  const labels: Array<{ key: StepView['key']; label: string }> = [
    { key: 'queued', label: '等待领取' },
    ...STEP_ORDER.map((key) => ({ key, label: STEP_LABELS[key] })),
  ];
  return labels.map((step, index) => ({
    ...step,
    done: index < currentIndex,
    // 只有还在途的任务才有"正在做"：已结束的任务停在结果上，不画进行中的格。
    current: isActive(task) && step.key === currentKey,
  }));
}

/**
 * 这一条任务此刻的一句话结论。
 *
 * 成功文案是产品要求的那一句：写入并**从记事本回读**一致——此时文字是记事本
 * 编辑区里**待保存**的内容，不是已经落盘的文件。界面上必须这么说，
 * 否则用户会以为文件已经存好了。
 */
export function outcomeOf(task: DesktopTask): { tone: 'ok' | 'error' | 'warn' | 'idle'; title: string; detail: string } {
  switch (task.state) {
    case 'succeeded':
      return {
        tone: 'ok',
        title: '已写入并从记事本回读一致',
        // 这句是**用户能看见的正文**，所以不加 Markdown 记号：界面上不会渲染 `**`，
        // 加进去只会原样露出星号（真实浏览器验收里出现过一次）。
        detail: '文字此刻在 Windows 记事本的编辑区里，是待保存的内容；Ling 不会替你按保存。',
      };
    case 'failed':
      return {
        tone: 'error',
        title: `失败：${reasonText(task.reason)}`,
        detail:
          task.reason === 'readback_mismatch'
            ? '记事本里的文字与提交内容不一致。常见原因：编辑控件被别的应用占用，或中文输入法仍在合成中。'
            : '这一次动作没有走完。提交新任务前先确认 Windows 桌面处于可操作状态。',
      };
    case 'interrupted':
      return {
        tone: 'warn',
        title: `中断：${reasonText(task.reason)}`,
        detail: interruptedHint(task),
      };
    case 'cancelled':
      return {
        tone: 'idle',
        title: `已取消：${reasonText(task.reason)}`,
        detail:
          task.reason === 'queue_expired'
            ? '15 秒内没有伴生端来领取。确认那台电脑的终端仍在运行，再提交一次。'
            : '这条任务没有执行。可以直接提交下一条。',
      };
    case 'running':
      return { tone: 'idle', title: '执行中', detail: currentStepText(task) };
    case 'queued':
      return {
        tone: 'idle',
        title: '等待伴生端领取',
        detail: '大约 2 秒一次心跳，最多等 15 秒；超时会被标成「已取消（排队超时）」。',
      };
    default: {
      const exhaustive: never = task.state;
      return exhaustive;
    }
  }
}

export function currentStepText(task: DesktopTask): string {
  const touched = stepRank(task.step);
  const next = STEP_ORDER[touched];
  if (!next) return '三步都已回执';
  return `已回执「${touched > 0 ? STEP_LABELS[STEP_ORDER[touched - 1] as Exclude<StepView['key'], 'queued'>] : '领取'}」，正在等「${STEP_LABELS[next]}」。`;
}

const REASON_TEXT: Record<string, string> = {
  owner_paused: '所有者暂停接收',
  owner_cancelled: '所有者取消',
  pairing_changed: '重新配对或解除配对',
  queue_expired: '排队超时',
  lease_expired: '执行超时（租期到期）',
  connection_lost: '连接中断',
  desktop_unavailable: 'Windows 桌面不可用',
  window_changed: '前台窗口变化',
  open_failed: '无法打开记事本',
  editor_unavailable: '编辑区不可用',
  editor_error: '读写编辑区失败',
  readback_mismatch: '回读与提交内容不一致',
  authorization_expired: '授权已到期',
  authorization_revoked: '授权已撤销',
};

/** 后端 `reason` → 人话。未登记的原因原样显示，不吞掉（排查时要看到真值）。 */
export function reasonText(reason: string | null): string {
  if (!reason) return '未给出原因';
  return REASON_TEXT[reason] ?? reason;
}

/** 中断原因 → 下一步该谁做什么。每一条都要能指向一个具体动作。 */
function interruptedHint(task: DesktopTask): string {
  switch (task.reason) {
    case 'owner_paused':
      return '暂停会终止在途任务。恢复接收后，这条保持中断结果，需要的话重新提交一条。';
    case 'pairing_changed':
      return '重新配对替换了令牌，旧的在途任务随之终止。用新令牌在 Windows 上重启伴生端后再提交。';
    case 'lease_expired':
      return task.step === 'written'
        ? '文本可能已经写进编辑区，但 20 秒租期内没有收到回读。到那台电脑上看一眼记事本；此处不会重放这次写入。'
        : '20 秒租期内没有走完。检查 Windows 是否弹出对话框或切走了焦点，然后重新提交。';
    case 'connection_lost':
      return '伴生端与 Ling 的连接断了。断线期间的步骤不会重放，重新提交一次即可。';
    case 'desktop_unavailable':
      return 'Windows 当时锁屏或切到了别的会话。解锁并使桌面保持前台，恢复接收后再提交。';
    case 'window_changed':
      return '操作期间前台切走了。让记事本保持前台，避免在此期间切窗口或切标签。';
    case 'authorization_expired':
    case 'authorization_revoked':
      return '限时授权已结束，这条自主活动随之中止。历史记录保留；需要时另开一次授权。';
    default:
      return '这条任务没有走完。按当前状态处理后提交新任务。';
  }
}

/* --------------------------------------------------------- 冲突 detail */

/**
 * 409 的 `detail` → 可行动提示。
 *
 * 后端只给机器可读值（`not_paired` / `paused` / `desktop_not_ready` /
 * `task_in_progress` / `request_id_conflict` / `authorization_expired`），
 * 界面必须自己翻成"接下来点哪里"。翻不出来的原样回显 message，
 * 不编一个更好听的说法。
 */
export function conflictHint(detail: string | null, fallback: string): string {
  switch (detail) {
    case 'not_paired':
      return '这台电脑已解除配对。重新生成令牌并在 Windows 上重启伴生端。';
    case 'paused':
      return '接收处于暂停。先「恢复接收」（需要设备就绪），再提交。';
    case 'desktop_not_ready':
      return '伴生端还没就绪：确认 Windows 已登录、未锁屏，且伴生端终端仍在运行。';
    case 'task_in_progress':
      return '上一条还在等待或执行。等它结束，或先取消它，再提交新的。';
    case 'request_id_conflict':
      return '这个提交编号已经用于另一段文本。刷新后重新提交（新提交会自动换新编号）。';
    case 'authorization_expired':
      return '结束时间已经过去。把时段改到未来再保存。';
    default:
      return fallback;
  }
}

/* --------------------------------------------------------- 时间与人话 */

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

/**
 * ISO 时间 → 本地 `HH:mm:ss`。解析失败原样返回：编一个时间比显示原文更坏。
 */
export function clockOf(iso: string | null): string {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** 相对时长（"3 秒前"）。用于 `last_seen_at`：它回答的是"刚才还在吗"。 */
export function sinceText(iso: string | null, now = Date.now()): string {
  if (!iso) return '从未连接';
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return iso;
  const seconds = Math.round((now - at) / 1000);
  if (seconds < 0) {
    // 两边时钟有偏差时不要显示"-3 秒前"。但也不要写出"33004 秒后"这种读数：
    // 心跳时间在未来，通常意味着本机时钟落后了几小时，这时该说的量级是小时。
    const ahead = -seconds;
    if (ahead < 60) return `${ahead} 秒后`;
    if (ahead < 3600) return `${Math.round(ahead / 60)} 分钟后`;
    return `${Math.round(ahead / 3600)} 小时后（本机时钟可能偏了）`;
  }
  if (seconds < 5) return '刚刚';
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.round(hours / 24)} 天前`;
}

/**
 * 本地 `datetime-local` 值 ↔ 带时区的 ISO 8601。
 *
 * 浏览器给的 `2026-11-24T01:30` 是**本地墙钟**，没有时区；后端要求
 * `AwareDatetime`。直接塞给后端会被 422 拒（而且看起来像"时间填错了"）。
 * 这里用 `Date` 的本地解释转成 UTC ISO，后缀写成 `Z`——往返在夏令时切换点
 * 之外是精确的，切换点内由浏览器按当地规则解析，这与用户看到的时间一致。
 */
export function localInputToIso(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString();
}

/** ISO → `datetime-local` 的初值（本地时区，分钟精度）。 */
export function isoToLocalInput(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

export function formatRange(startIso: string, endIso: string): string {
  const start = new Date(startIso);
  const end = new Date(endIso);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return `${startIso} → ${endIso}`;
  const sameDay = start.toDateString() === end.toDateString();
  const day = `${start.getMonth() + 1} 月 ${start.getDate()} 日`;
  const hm = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return sameDay ? `${day} ${hm(start)}–${hm(end)}` : `${day} ${hm(start)} → ${end.getMonth() + 1} 月 ${end.getDate()} 日 ${hm(end)}`;
}

/* ------------------------------------------------------------ 授权判定 */

export interface GrantView {
  /** 面板上的一句结论。 */
  title: string;
  detail: string;
  /** 该决定是否还在推进（能否写"之后会更新"）。 */
  settled: boolean;
  /** 对应任务的 id（`queued` 时后端给）。 */
  taskId: string | null;
}

/**
 * 授权的七种决定 → 人话。
 *
 * 关键区分（要求里点名的那条）：`queued` **只是已入队**，不代表写成功；
 * 真实的写入结果在任务列表里按 `task_id` 找。所以 `queued` 的 detail 必须
 * 把读者推到任务列表，而不是说"已经写了"。
 */
export function grantView(grant: DesktopActivityGrant): GrantView {
  switch (grant.decision) {
    case 'pending':
      return {
        title: '等待时段与设备',
        detail: '还没到授权时段，或电脑暂时不可用。进入时段且桌面就绪后会交给模型决定写还是休息。',
        settled: false,
        taskId: null,
      };
    case 'deciding':
      return {
        title: '模型正在决定写还是休息',
        detail: '它会结合人设与当前时段选择；结果出现后这里会更新。',
        settled: false,
        taskId: null,
      };
    case 'rest':
      return {
        title: '本次按安排休息',
        detail: '模型（或日程的休息安排）选择了不写。每份授权最多执行一次，不会在本时段再写。',
        settled: true,
        taskId: null,
      };
    case 'queued':
      return {
        title: '已入队，等伴生端领取',
        detail: '入队不等于写成功。结果看下面任务列表里对应的那条（按「来自授权」标记）。',
        settled: false,
        taskId: grant.task_id,
      };
    case 'expired':
      return { title: '授权已到期', detail: '时段结束。需要的话另开一次授权。', settled: true, taskId: null };
    case 'revoked':
      return { title: '授权已撤销', detail: '当次已入队或执行的活动随授权结束。历史保留。', settled: true, taskId: null };
    case 'unavailable':
      return {
        title: '本次无法安排',
        detail: '模型不可用，或给出的决定在当时无法执行。可以重新授权一次。',
        settled: true,
        taskId: null,
      };
    default: {
      const exhaustive: never = grant.decision;
      return exhaustive;
    }
  }
}

/** 授权是否还"活着"（能被后续轮询改变）。已结束的决定不必再追它。 */
export function grantIsLive(grant: DesktopActivityGrant | null): boolean {
  if (!grant) return false;
  return ['pending', 'deciding', 'queued'].includes(grant.decision);
}

export function checkGrantWindow(startLocal: string, endLocal: string, now = Date.now()): NoteCheck {
  const start = localInputToIso(startLocal);
  const end = localInputToIso(endLocal);
  if (!start || !end) return { ok: false, message: '把开始和截止时间都填上。' };
  if (Date.parse(end) <= Date.parse(start)) return { ok: false, message: '截止时间要晚于开始时间。' };
  if (Date.parse(end) <= now) return { ok: false, message: '这段时间已经过去，后端会以「授权已过」拒绝。' };
  return { ok: true };
}

/* ------------------------------------------------------------ 令牌遮蔽 */

/** 令牌：43 字符，后端用 base64url 生成（`new_token()`）。展示时只露头尾。 */
export function maskToken(token: string): string {
  if (token.length <= 10) return token;
  return `${token.slice(0, 4)}…${token.slice(-4)}`;
}

/**
 * 启动命令（后端文档第 2 步原文，保留 `py -m` 两段式）。
 *
 * 令牌**不写进命令**：写进去就会被 shell 历史、截图、粘贴板带走，
 * 而它只在一次响应里可见。命令用环境变量占位，让粘贴发生在一个明确的
 * "这个框是放令牌的"位置——这正是要求里"把启动命令和需要粘贴令牌的位置交代清楚"。
 */
export function launchCommand(url: string): string {
  return `py -m pip install .\npy -m connectors.desktop --url ${url || 'https://你的-ling-地址'}`;
}
