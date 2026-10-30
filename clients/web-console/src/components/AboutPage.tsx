/**
 * 关于与版本：展示当前运行版本，手动检查 GitHub Release，并在条件齐备时
 * 发起一次安装更新。
 *
 * 这里的边界：
 * - 打开页面不联网检查 GitHub，只在用户点击时查一次；
 * - 安装目标必须来自 Release 结果里齐全的 release_id/tag/commit，
 *   缺任何一项都不显示安装按钮，绝不在前端猜目标；
 * - “已有更新”只认 queued/running/needs_manual：历史 succeeded/failed 不阻断
 *   下一次安装，但要求先做一次新的检查（Release 与当前版本一起刷新）；
 * - 确认需要手输完整 tag，提交只发一次；409/变化类错误要求重新检查，
 *   不自动重发；
 * - 断连不等于完成或失败：继续轮询（带取消与退避），只有任务 succeeded 且
 *   GET /v1/version 的 commit 等于 job.target_commit 才显示更新完成；
 * - 提交响应丢失时按“结果未知”处理：只查状态、不重发，并拒绝把提交前的
 *   旧任务当成本次提交的成功；
 * - 没有取消/忽略接口，所以也不造这类按钮（needs_manual 是任务终态、manual_required
 * 与 protocol_unsupported 是门禁原因，都由宿主处理；前端只如实说明，不提供忽略/
 * 取消/自动重试入口）。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import type {
  ReleaseCheck,
  UpdateJob,
  UpdateRequest,
  UpdateStatus,
  VersionInfo,
} from '../types';
import { AlertIcon, ArrowLeftIcon, CheckIcon, RefreshIcon } from './Icons';

/** 任务进行中的轮询间隔：约 3 秒；后端状态也是按这个量级更新的。 */
const POLL_INTERVAL_MS = 3000;
/** 断连/提交结果未知时的退避节奏：先快后慢，上限 8 秒，避免打满服务端。 */
const RECOVERY_DELAYS_MS = [1500, 2500, 4000, 8000];
/** 更新成功后最多自动核对多少次版本接口，之后交给手动“重新验证”。 */
const MAX_VERIFY_POLLS = 20;
/** 提交响应丢失后，最多查多少次状态来确认这次提交是否落地。 */
const SUBMIT_RECOVERY_ATTEMPTS = 12;

interface AboutPageProps {
  onBack: () => void;
}

type CheckState =
  | { status: 'idle' }
  | { status: 'checking' }
  | { status: 'done'; result: ReleaseCheck; stale?: boolean }
  | { status: 'error'; error: ExplainedError };

type Connection = 'ok' | 'waiting';

type PollMode = 'job' | 'recover';

type StatusResult =
  | { kind: 'ok'; status: UpdateStatus }
  | { kind: 'waiting' }
  /** 更新的请求代际已经推进，这次响应属于旧代际，不能 apply。 */
  | { kind: 'stale' }
  | { kind: 'fatal' }
  | { kind: 'aborted' };

type VersionResult = 'ok' | 'waiting' | 'error' | 'stale' | 'aborted';

interface PendingSubmission {
  /** 提交发起时刻（ISO），用于说明与日志定位，不用于猜提交是否落地。 */
  startedAt: string;
  /** 提交前已知任务的 request_id；恢复时只有“换了新任务”才算本次提交。 */
  baselineRequestId: string | null;
  /** 已查询过多少次状态。 */
  attempts: number;
  /** 这次提交的目标；恢复时新任务必须目标一致，不能把别人的任务当自己的。 */
  targetTag: string;
  targetCommit: string;
}

/** 重新提交（人工风险流程）的显式状态；普通“重新检查”不能走到这里。 */
type ResubmitState =
  | { status: 'idle' }
  | { status: 'checking' }
  | { status: 'confirm'; release: ReleaseCheck; target: InstallTarget; version: VersionInfo }
  | { status: 'blocked'; message: string }
  | { status: 'error'; message: string };

export interface ReleasePresentation {
  tone: 'ok' | 'info' | 'warn';
  title: string;
  detail: string;
}

export interface InstallTarget {
  releaseId: number;
  tag: string;
  commit: string;
}

export type JobViewKind =
  | 'progress'
  | 'waiting'
  | 'succeeded'
  | 'verifying'
  | 'unverifiable'
  | 'failed'
  | 'manual';

export interface JobView {
  kind: JobViewKind;
  title: string;
  detail: string;
}

export interface UpdateErrorInfo {
  reason: string;
  hint: string;
  /** 需要重新检查 Release / 当前版本，不能直接重发。 */
  needsRecheck: boolean;
  /** 连接中断：结果未知，不能当作失败处理或重发。 */
  connectionLost: boolean;
}

/**
 * 两条门禁原因（不是任务终态）的文案：状态栏/安装按钮（`REASON_TEXT`）与提交被拒的
 * 503 横幅（`UPDATE_DETAIL_TEXT`）共用同一组字符串，避免两处各自演化。
 *
 * - `manual_required`：执行器明确要求人工恢复，只能在宿主机核验后解除；
 * - `protocol_unsupported`：API 与执行器协议不一致，重试不会变好。
 */
const GATE_REASON_TEXT: Record<string, string> = {
  manual_required: '更新执行器要求人工核验：需在宿主机处理（adopt-current）后才会解除',
  protocol_unsupported: '更新协议不兼容：API 与执行器需要同批升级，重试不会生效',
};

const REASON_TEXT: Record<string, string> = {
  disabled: '更新通道未启用',
  not_owner: '只有所有者可以发起更新',
  executor_offline: '更新执行器不在线',
  channel_unavailable: '更新通道不可用',
  installed_commit_unknown: '当前构建提交未知，无法校验更新结果',
  version_drift: '运行版本与安装记录不一致',
  update_in_progress: '已有更新正在进行',
  ...GATE_REASON_TEXT,
  release_unverifiable: '无法校验该 Release 的目标提交',
  not_newer: '该 Release 不比当前版本更新',
};

const STAGE_TEXT: Record<string, string> = {
  queued: '排队',
  preflight: '预检',
  backup: '备份',
  fetch: '拉取',
  verify_source: '校验源码',
  build: '构建',
  deploy: '部署',
  verify: '验证',
  complete: '完成',
};

const STAGE_ORDER = [
  'queued',
  'preflight',
  'backup',
  'fetch',
  'verify_source',
  'build',
  'deploy',
  'verify',
  'complete',
];

const UPDATE_DETAIL_TEXT: Record<string, string> = {
  release_changed: 'Release 在你确认之后发生了变化',
  installed_version_changed: '当前运行的版本在确认之后发生了变化',
  update_in_progress: '已有更新正在进行',
  disabled: '更新通道未启用',
  executor_offline: '更新执行器不在线',
  not_owner: '只有所有者可以发起更新',
  version_drift: '运行版本与安装记录不一致',
  installed_commit_unknown: '当前构建提交未知',
  channel_unavailable: '更新通道不可用',
  release_unverifiable: '无法校验该 Release 的目标提交',
  not_newer: '该 Release 不比当前版本更新',
  invalid_request_body: '确认信息不完整或已失效', invalid_request: '确认信息不完整或已失效',
  // 门禁原因：提交被拒时 `version.py:702-706` 的 `_gate_error` 复用
  // `provider_unavailable` 错误码（503），必须按这里的文案显示，不能落到
  // `errors.ts` 的“模型供应商不可用”。
  ...GATE_REASON_TEXT,
};

/**
 * `needs_manual` 是**任务终态**（更新任务结束在“需要人工处理”），不是 gate reason；
 * 它表示上一次任务必须先在宿主机处理（adopt-current）才能再次安装。
 *
 * 与之相邻但不同：gate reason `manual_required` 是**门禁原因**（服务端判定执行器
 * 要求人工核验，见 REASON_TEXT）；两者都禁用安装按钮，都不提供忽略/取消/自动重试。
 */
export const MANUAL_BLOCK_TEXT = '上一次更新需要人工处理，完成后才能再次安装';

/** 后端稳定状态 → 一句可执行的界面文案；单测直接覆盖这个映射。 */
export function describeUpdateReason(reason: string | null | undefined): string {
  if (!reason) return '更新通道未启用';
  return REASON_TEXT[reason] ?? `当前不可更新（${reason}）`;
}

export function describeStage(stage: string): string {
  return STAGE_TEXT[stage] ?? stage;
}

export function stageProgress(stage: string): number {
  const index = STAGE_ORDER.indexOf(stage);
  if (index < 0) return 0;
  return Math.round(((index + 1) / STAGE_ORDER.length) * 100);
}

/** 提交比对：大小写与首尾空白不敏感，但要求完整一致。 */
export function sameCommit(
  left: string | null | undefined,
  right: string | null | undefined,
): boolean {
  if (!left || !right) return false;
  return left.trim().toLowerCase() === right.trim().toLowerCase();
}

/** 只有排队/运行中的任务才算“进行中”，才需要按秒轮询。 */
export function isJobRunning(job: UpdateJob | null | undefined): boolean {
  return job?.state === 'queued' || job?.state === 'running';
}

/**
 * 会不会挡住下一次安装。历史 succeeded/failed 不挡（否则一次成功或失败之后
 * 就再也不能更新）；needs_manual 挡，因为只能由宿主处理。
 */
export function jobBlocksNewUpdate(job: UpdateJob | null | undefined): boolean {
  if (!job) return false;
  return isJobRunning(job) || job.state === 'needs_manual';
}

/**
 * 提交响应丢失后，状态里出现的是不是“这次提交”产生的任务。
 * 只有 request_id 与提交前的基线不同才算；否则那就是提交前的旧任务，
 * 不能拿来当本次提交的成功或失败。传入目标时还要求 tag/commit 一致，
 * 避免把另一个目标的新任务误认成自己的提交。
 */
export function isSubmittedJob(
  candidate: UpdateJob | null | undefined,
  baselineRequestId: string | null,
  target?: { tag: string; commit: string } | null,
): boolean {
  if (!candidate) return false;
  if (candidate.request_id === baselineRequestId) return false;
  if (target) {
    if (candidate.target_tag !== target.tag) return false;
    if (!sameCommit(candidate.target_commit, target.commit)) return false;
  }
  return true;
}

/** 终态身份：request_id + state。同状态的新任务也必须被当成新终态。 */
export function terminalJobIdentity(job: UpdateJob | null | undefined): string | null {
  if (!job) return null;
  if (job.state !== 'succeeded' && job.state !== 'failed' && job.state !== 'needs_manual') {
    return null;
  }
  return `${job.request_id}:${job.state}`;
}

/**
 * “当前状态”一栏的文案。门禁已经把过期的 update_in_progress 视为可安装，
 * 这里必须给出同一套解释，不能一边放开按钮一边说“已有更新正在进行”。
 */
export function describeStatusNote(
  status: UpdateStatus | null,
  job: UpdateJob | null,
): string | null {
  if (!status || !status.enabled || !status.reason) return null;
  const activeJob = jobBlocksNewUpdate(status.current_job)
    ? status.current_job
    : jobBlocksNewUpdate(job)
      ? job
      : null;
  if (activeJob) {
    // 有阻塞任务时门禁只认“进行中/需人工处理”，文案也跟门禁一致。
    return activeJob.state === 'needs_manual'
      ? MANUAL_BLOCK_TEXT
      : describeUpdateReason('update_in_progress');
  }
  if (status.reason === 'update_in_progress') {
    return '服务端上次报告有更新进行中，但当前没有可核实的任务；安装门禁按最新任务状态判断。';
  }
  return describeUpdateReason(status.reason);
}

/** 轮询节奏：任务进行中固定 3 秒；断连/待确认时退避到 8 秒。 */
export function pollDelayMs(failures: number, mode: PollMode): number {
  if (mode === 'job') return POLL_INTERVAL_MS;
  const index = Math.min(Math.max(failures, 0), RECOVERY_DELAYS_MS.length - 1);
  return RECOVERY_DELAYS_MS[index] ?? 8000;
}

/**
 * 从 Release 结果里取安装目标。
 * 只有后端明确说 eligible 且 release_id/tag/commit 齐全时才返回；
 * 缺失任何一项一律 null —— 前端不拼凑目标。
 */
export function installTarget(result: ReleaseCheck | null): InstallTarget | null {
  if (!result || result.status !== 'ok' || result.update_eligible !== true) return null;
  const releaseId = result.release_id;
  if (typeof releaseId !== 'number' || !Number.isInteger(releaseId) || releaseId <= 0) return null;
  const tag = typeof result.latest === 'string' ? result.latest.trim() : '';
  const commit = typeof result.target_commit === 'string' ? result.target_commit.trim() : '';
  if (!tag || !commit) return null;
  return { releaseId, tag, commit };
}

export interface InstallBlockerInput {
  target: InstallTarget | null;
  version: VersionInfo | null;
  status: UpdateStatus | null;
  /** 本地已知任务（POST 返回值或最近一次状态轮询）。 */
  job: UpdateJob | null;
  recheckRequired: boolean;
  /** 断连时状态可能过期，先等恢复，避免重复提交。 */
  connection: Connection;
}

/** 返回禁用原因；null 表示可以安装。顺序即界面展示优先级。 */
export function installBlocker({
  target,
  version,
  status,
  job,
  recheckRequired,
  connection,
}: InstallBlockerInput): string | null {
  if (connection === 'waiting') return '正在等待服务恢复';
  if (recheckRequired) return 'Release 结果已过期，请重新检查';
  if (!status) return '正在读取更新状态';
  if (!status.enabled) return describeUpdateReason(status.reason ?? 'disabled');
  // 先看任务状态：只有 queued/running/needs_manual 才算进行中。
  const serverJob = status.current_job;
  const blockingJob = jobBlocksNewUpdate(serverJob)
    ? serverJob
    : jobBlocksNewUpdate(job)
      ? job
      : null;
  if (blockingJob) {
    return blockingJob.state === 'needs_manual'
      ? MANUAL_BLOCK_TEXT
      : describeUpdateReason('update_in_progress');
  }
  // 没有进行中的任务时，update_in_progress 只可能是过期字段，不能据此永久禁用。
  if (status.reason && status.reason !== 'update_in_progress') {
    return describeUpdateReason(status.reason);
  }
  if (!status.executor?.online) return describeUpdateReason('executor_offline');
  if (!target) return '还没有可安装的目标';
  if (!version) return '尚未读取当前版本';
  if (!version.current) return '当前版本未知';
  if (!version.commit) return describeUpdateReason('installed_commit_unknown');
  return null;
}

/** 组装 POST body；任何一项缺失都返回 null，调用方据此不发请求。 */
export function buildUpdateRequest(
  result: ReleaseCheck | null,
  version: VersionInfo | null,
  confirmTag: string,
): UpdateRequest | null {
  const target = installTarget(result);
  const currentVersion = version?.current?.trim() ?? '';
  const currentCommit = version?.commit?.trim() ?? '';
  if (!target || !currentVersion || !currentCommit) return null;
  return {
    confirm_tag: confirmTag.trim(),
    expected: {
      release_id: target.releaseId,
      tag: target.tag,
      commit: target.commit,
      current_version: currentVersion,
      current_commit: currentCommit,
    },
  };
}

export function explainUpdateError(error: unknown): UpdateErrorInfo {
  if (error instanceof ApiError && (error.status === 0 || error.code === 'network_error')) {
    return {
      reason: '与服务端的连接已断开',
      hint: '请求可能已经送达；页面会继续查询更新状态，不要重复提交。',
      needsRecheck: false,
      connectionLost: true,
    };
  }
  const base = explainError(error);
  const detail = error instanceof ApiError ? error.detail : null;
  const code = error instanceof ApiError ? error.code : '';
  const status = error instanceof ApiError ? error.status : 0;
  const detailText = detail ? UPDATE_DETAIL_TEXT[detail] : undefined;
  // 更新通道的 503 复用 `provider_unavailable` 这个错误码（`version.py:702-706` 的
  // `_gate_error`，以及 `:765-770` 的 `release_unverifiable`），与模型供应商无关：
  // 只按 detail 的门禁文案显示，绝不回落到 `explainError()` 的“模型供应商不可用”
  // 及其“检查供应商地址与密钥”提示。
  if (status === 503 && code === 'provider_unavailable') {
    return {
      reason: detailText ?? '当前不满足更新条件',
      hint: '更新门禁在提交前发生了变化：先刷新更新状态确认当前原因，按提示处理后再重新检查；不要重复提交。',
      needsRecheck: false,
      connectionLost: false,
    };
  }
  const reason = detailText ?? base.reason;
  const needsRecheck =
    detail === 'release_changed' || detail === 'installed_version_changed' || status === 409;
  if (needsRecheck) {
    return {
      reason,
      hint: '重新检查 Release 与当前版本，确认目标没有变化后再决定是否安装；不要直接重发。',
      needsRecheck: true,
      connectionLost: false,
    };
  }
  return { reason, hint: base.hint, needsRecheck: false, connectionLost: false };
}

/** 把后端稳定状态翻译成界面文案；单测直接覆盖这个映射。 */
export function describeRelease(result: ReleaseCheck): ReleasePresentation {
  if (result.status === 'no_release') {
    return {
      tone: 'info',
      title: '当前没有 Release',
      detail: '仓库还没有发布 Release，因此没有可安装的目标；这不影响当前运行的版本。',
    };
  }
  if (result.status === 'unavailable') {
    return {
      tone: 'warn',
      title: '暂时无法检查 Release',
      detail: 'GitHub 暂时不可达、请求超时或返回了无法识别的响应；不影响当前运行。',
    };
  }
  if (result.is_newer === true) {
    const installable = installTarget(result) !== null;
    return {
      tone: 'ok',
      title: '有新版本可用',
      detail: installable
        ? `最新发布是 ${result.latest ?? '未知版本'}，当前运行 ${result.current}；可以在下方确认安装。`
        : `最新发布是 ${result.latest ?? '未知版本'}，当前运行 ${result.current}；这里没有完整的目标信息，升级请走部署流程。`,
    };
  }
  if (result.is_newer === false) {
    return {
      tone: 'ok',
      title: '已是最新',
      detail: `最新发布 ${result.latest ?? '未知版本'} 不高于当前运行 ${result.current}。`,
    };
  }
  return {
    tone: 'warn',
    title: '发现发布，但版本号无法比较',
    detail: `最新标签是 ${result.latest ?? '未知版本'}，它或当前版本不是可识别的 semver。`,
  };
}

/** 任务状态 + 当前进程提交 → 界面状态。完成判定只认 commit 相等。 */
export function describeJob(
  job: UpdateJob,
  currentCommit: string | null,
  connection: Connection,
): JobView {
  if (job.state === 'queued' || job.state === 'running') {
    if (connection === 'waiting') {
      return {
        kind: 'waiting',
        title: '与服务端的连接已断开，正在等待恢复',
        detail: '这不等于更新完成或失败；恢复后继续查询当前任务，不会重复提交。',
      };
    }
    return {
      kind: 'progress',
      title: job.state === 'queued' ? '更新已排队' : '正在更新',
      detail: `阶段：${describeStage(job.stage)}（${job.stage}），目标 ${job.target_tag}。安装期间服务会短暂停机。`,
    };
  }
  if (job.state === 'succeeded') {
    if (sameCommit(currentCommit, job.target_commit)) {
      return {
        kind: 'succeeded',
        title: '更新完成',
        detail: `当前进程已报告目标提交 ${shortSha(job.target_commit)}。重新加载页面即可使用新版本。`,
      };
    }
    if (!currentCommit) {
      return {
        kind: 'unverifiable',
        title: '更新已执行，但无法校验结果',
        detail:
          '当前进程没有报告构建提交（LING_COMMIT 未注入），无法确认已加载目标提交；请查看宿主日志确认。',
      };
    }
    return {
      kind: 'verifying',
      title: '更新已执行，等待新版本生效',
      detail: `服务端仍报告提交 ${shortSha(currentCommit)}，目标是 ${shortSha(job.target_commit)}。确认宿主服务已重启后重新验证；页面不会自动刷新。`,
    };
  }
  if (job.state === 'failed') {
    return {
      kind: 'failed',
      title: '更新失败',
      detail: `在「${describeStage(job.stage)}」阶段停止。更新没有完成，服务可能仍在旧版本；请查看宿主更新日志后再决定下一步，系统不会自动回滚。`,
    };
  }
  return {
    kind: 'manual',
    title: '需要人工处理',
    detail:
      '更新没有走到可自动继续的状态。请在宿主机查看更新日志与服务状态并手动处理；让执行器 adopt-current 之后这里才会刷新。系统不会自动回滚，数据库也不会自动恢复，本页没有忽略或取消接口。',
  };
}

function shortSha(value: string | null): string {
  if (!value) return '未知';
  return value.length > 7 ? value.slice(0, 7) : value;
}

function shortDigest(value: string): string {
  return value.length > 16 ? `${value.slice(0, 16)}…` : value;
}

function formatPublished(value: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN');
}

function isAbortError(error: unknown): boolean {
  return (
    (error instanceof DOMException && error.name === 'AbortError') ||
    (error instanceof Error && error.name === 'AbortError')
  );
}

/**
 * 现有的 api.version / api.checkRelease 不接受 signal；这里用一个很小的
 * 取消包装，让调用方能主动放弃旧响应。取消只影响本页应用结果，
 * 不会被当成网络故障。
 */
function withAbort<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(new DOMException('aborted', 'AbortError'));
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(new DOMException('aborted', 'AbortError'));
    signal.addEventListener('abort', onAbort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener('abort', onAbort);
        resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener('abort', onAbort);
        reject(error);
      },
    );
  });
}

function isTerminalJob(job: UpdateJob | null): boolean {
  return job?.state === 'succeeded' || job?.state === 'failed' || job?.state === 'needs_manual';
}

export function AboutPage({ onBack }: AboutPageProps) {
  const [info, setInfo] = useState<VersionInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<ExplainedError | null>(null);
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [updateError, setUpdateError] = useState<ExplainedError | null>(null);
  const [job, setJob] = useState<UpdateJob | null>(null);
  const [connection, setConnection] = useState<Connection>('ok');
  const [check, setCheck] = useState<CheckState>({ status: 'idle' });
  const [recheckRequired, setRecheckRequired] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [confirmTag, setConfirmTag] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<UpdateErrorInfo | null>(null);
  const [pendingSubmit, setPendingSubmit] = useState<PendingSubmission | null>(null);
  const [submitUnresolved, setSubmitUnresolved] = useState(false);
  const [pollPaused, setPollPaused] = useState(false);
  const [pollNonce, setPollNonce] = useState(0);
  const [resubmit, setResubmit] = useState<ResubmitState>({ status: 'idle' });
  const [resubmitTag, setResubmitTag] = useState('');
  const [resubmitRisk, setResubmitRisk] = useState(false);

  const aliveRef = useRef(true);
  const submittingRef = useRef(false);
  const checkingRef = useRef(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const confirmBoxRef = useRef<HTMLDivElement | null>(null);

  const infoRef = useRef<VersionInfo | null>(null);
  const jobRef = useRef<UpdateJob | null>(null);
  const statusRef = useRef<UpdateStatus | null>(null);
  const pendingRef = useRef<PendingSubmission | null>(null);

  /** 数据请求代际：只有仍是当前代际的响应才允许 apply。 */
  const versionSeqRef = useRef(0);
  const statusSeqRef = useRef(0);
  const reloadSeqRef = useRef(0);
  const checkSeqRef = useRef(0);
  /** 每出现一次“新终态身份”就 +1；检查开始后若它变化，检查结果不能清 recheck。 */
  const terminalEventRef = useRef(0);
  const previousTerminalRef = useRef<string | null>(null);
  const reloadControllerRef = useRef<AbortController | null>(null);
  const checkControllerRef = useRef<AbortController | null>(null);
  const resubmitControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      reloadControllerRef.current?.abort();
      checkControllerRef.current?.abort();
      resubmitControllerRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    infoRef.current = info;
  }, [info]);
  useEffect(() => {
    jobRef.current = job;
  }, [job]);
  useEffect(() => {
    statusRef.current = status;
  }, [status]);
  useEffect(() => {
    pendingRef.current = pendingSubmit;
  }, [pendingSubmit]);

  const setPending = useCallback((next: PendingSubmission | null) => {
    pendingRef.current = next;
    setPendingSubmit(next);
  }, []);

  const fetchVersion = useCallback(async (signal?: AbortSignal): Promise<VersionResult> => {
    const seq = versionSeqRef.current + 1;
    versionSeqRef.current = seq;
    try {
      const next = await withAbort(api.version(), signal);
      if (!aliveRef.current) return 'aborted';
      if (signal?.aborted) return 'aborted';
      if (seq !== versionSeqRef.current) return 'stale';
      infoRef.current = next;
      setInfo(next);
      setLoadError(null);
      return 'ok';
    } catch (error) {
      if (!aliveRef.current || signal?.aborted || isAbortError(error)) return 'aborted';
      if (seq !== versionSeqRef.current) return 'stale';
      const explained = explainError(error);
      if (explained.code === 'network_error' || explained.retriable) {
        // 连接问题不算“版本信息坏了”，交给等待恢复的横幅说明。
        setConnection('waiting');
        return 'waiting';
      }
      setLoadError(explained);
      return 'error';
    }
  }, []);

  const applyStatus = useCallback((next: UpdateStatus) => {
    statusRef.current = next;
    setStatus(next);
    const nextJob = next.current_job ?? null;
    jobRef.current = nextJob;
    setJob(nextJob);
    setUpdateError(null);
  }, []);

  const fetchStatus = useCallback(
    async (signal?: AbortSignal): Promise<StatusResult> => {
      const seq = statusSeqRef.current + 1;
      statusSeqRef.current = seq;
      try {
        const next = await api.updateStatus(signal);
        if (!aliveRef.current) return { kind: 'aborted' };
        if (signal?.aborted) return { kind: 'aborted' };
        if (seq !== statusSeqRef.current) return { kind: 'stale' };
        applyStatus(next);
        return { kind: 'ok', status: next };
      } catch (error) {
        if (!aliveRef.current || signal?.aborted || isAbortError(error)) {
          return { kind: 'aborted' };
        }
        if (seq !== statusSeqRef.current) return { kind: 'stale' };
        if (error instanceof ApiError && error.status === 404) {
          setUpdateError({
            reason: '服务端还没有更新接口',
            hint: '当前版本信息仍可查看；部署带更新通道的服务端后再回来。',
            retriable: false,
            code: 'not_found',
            detail: null,
            traceId: null,
          });
          return { kind: 'fatal' };
        }
        const explained = explainError(error);
        if (explained.code === 'network_error' || explained.retriable) {
          return { kind: 'waiting' };
        }
        setUpdateError(explained);
        return { kind: 'fatal' };
      }
    },
    [applyStatus],
  );

  const reloadAll = useCallback(async () => {
    const seq = reloadSeqRef.current + 1;
    reloadSeqRef.current = seq;
    const controller = new AbortController();
    reloadControllerRef.current?.abort();
    reloadControllerRef.current = controller;
    setLoading(true);
    const [versionResult, statusResult] = await Promise.all([
      fetchVersion(controller.signal),
      fetchStatus(controller.signal),
    ]);
    if (!aliveRef.current || seq !== reloadSeqRef.current) return;
    if (versionResult === 'waiting' || statusResult.kind === 'waiting') {
      setConnection('waiting');
    } else if (versionResult === 'ok' && statusResult.kind === 'ok') {
      setConnection('ok');
    }
    setLoading(false);
  }, [fetchStatus, fetchVersion]);

  useEffect(() => {
    void reloadAll();
  }, [reloadAll]);

  /**
   * 提交响应丢失后的状态对账：只有出现了新的 request_id、且目标一致，
   * 才算这次提交落地；旧任务或另一个目标的新任务都不能当成本次结果。
   */
  const attachPending = useCallback(
    (next: UpdateStatus): boolean => {
      const pending = pendingRef.current;
      if (!pending) return false;
      const candidate = next.current_job;
      const matched = isSubmittedJob(candidate, pending.baselineRequestId, {
        tag: pending.targetTag,
        commit: pending.targetCommit,
      });
      if (!matched) return false;
      setPending(null);
      setSubmitUnresolved(false);
      setPollPaused(false);
      setSubmitError(null);
      setResubmit({ status: 'idle' });
      setResubmitTag('');
      setResubmitRisk(false);
      return true;
    },
    [setPending],
  );

  const resolvePending = useCallback(
    (next: UpdateStatus) => {
      const pending = pendingRef.current;
      if (!pending) return;
      if (attachPending(next)) return;
      const attempts = pending.attempts + 1;
      const updated: PendingSubmission = { ...pending, attempts };
      setPending(updated);
      if (attempts >= SUBMIT_RECOVERY_ATTEMPTS) {
        // 只保留一条“无法确认”的提示（在待确认横幅里），不再叠一条错误横幅。
        setSubmitUnresolved(true);
        setRecheckRequired(true);
      }
    },
    [attachPending, setPending],
  );

  // 轮询：进行中的任务、断连恢复、提交结果未知，三种情况都要持续查询。
  // 取消/暂停、退避和“立即重试”都在这里收口；不会自动重发 POST。
  const runningJob = job && isJobRunning(job) ? job : null;
  const pendingActive = pendingSubmit && !submitUnresolved ? pendingSubmit : null;
  const pollMode: PollMode | null = pollPaused
    ? null
    : runningJob
      ? 'job'
      : pendingActive || connection === 'waiting'
        ? 'recover'
        : null;
  const pollKey =
    pollMode === null
      ? null
      : pollMode === 'job'
        ? `job:${runningJob?.request_id ?? ''}:${pollNonce}`
        : `recover:${pendingActive ? pendingActive.startedAt : 'waiting'}:${pollNonce}`;

  useEffect(() => {
    if (!pollKey || !pollMode) return;
    let cancelled = false;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const tick = async () => {
      const versionPromise: Promise<VersionResult | 'skipped'> =
        pollMode === 'recover'
          ? fetchVersion(controller.signal)
          : Promise.resolve<VersionResult | 'skipped'>('skipped');
      const [statusResult, versionResult] = await Promise.all([
        fetchStatus(controller.signal),
        versionPromise,
      ]);
      if (cancelled || !aliveRef.current) return;
      if (statusResult.kind === 'ok') {
        if (versionResult === 'waiting') {
          failures += 1;
          setConnection('waiting');
        } else if (versionResult === 'ok') {
          setConnection('ok');
        }
        resolvePending(statusResult.status);
      } else if (statusResult.kind === 'waiting') {
        failures += 1;
        setConnection('waiting');
      } else if (statusResult.kind === 'fatal') {
        // 接口不存在/权限类错误不会自愈，继续轮询只是噪音。
        if (pendingRef.current) {
          setSubmitUnresolved(true);
          setRecheckRequired(true);
        }
        return;
      } else if (statusResult.kind === 'aborted') {
        return;
      }
      if (cancelled) return;
      timer = setTimeout(tick, pollDelayMs(failures, pollMode));
    };
    // 任务进行中先等一个周期，避免和刚拿到的状态重复请求；
    // 断连/待确认则立刻试一次。
    timer = setTimeout(tick, pollMode === 'job' ? POLL_INTERVAL_MS : 0);
    return () => {
      cancelled = true;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [pollKey, pollMode, fetchStatus, fetchVersion, resolvePending]);

  // 成功后核对 GET /v1/version 的 commit；只在相等时才算完成。
  const currentCommit = info?.commit ?? null;
  useEffect(() => {
    if (!job || job.state !== 'succeeded') return;
    if (sameCommit(currentCommit, job.target_commit)) return;
    let cancelled = false;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      attempts += 1;
      const result = await fetchVersion();
      if (cancelled) return;
      if (result === 'ok') setConnection('ok');
      else if (result === 'waiting') setConnection('waiting');
      if (!cancelled && attempts < MAX_VERIFY_POLLS) {
        timer = setTimeout(tick, POLL_INTERVAL_MS);
      }
    };
    timer = setTimeout(tick, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [job, currentCommit, fetchVersion]);

  // 一次 succeeded/failed 之后 Release 结果已经过期：下一次安装必须先重新检查。
  // 关键按“终态身份”（request_id + state）比较：新的同状态终态也必须重新检查。
  const terminalKey = terminalJobIdentity(job);
  useEffect(() => {
    if (!terminalKey) return;
    if (terminalKey === previousTerminalRef.current) return;
    previousTerminalRef.current = terminalKey;
    terminalEventRef.current += 1;
    // 新终态让所有更早发出的数据响应失效；随后立刻拉一次当前版本，
    // 替代可能被旧副本带回的 running/old-version 快照。
    versionSeqRef.current += 1;
    statusSeqRef.current += 1;
    setRecheckRequired(true);
    setCheck((previous) => (previous.status === 'done' ? { ...previous, stale: true } : previous));
    void fetchVersion();
  }, [terminalKey, fetchVersion]);

  useEffect(() => {
    if (!confirming) return;
    inputRef.current?.focus();
    confirmBoxRef.current?.scrollIntoView({ block: 'nearest' });
  }, [confirming]);

  const runCheck = async () => {
    if (checkingRef.current) return;
    checkingRef.current = true;
    const controller = new AbortController();
    checkControllerRef.current?.abort();
    checkControllerRef.current = controller;
    const checkSeq = checkSeqRef.current + 1;
    checkSeqRef.current = checkSeq;
    const terminalAtStart = terminalEventRef.current;
    const hadPending = pendingRef.current !== null;
    const wasUnresolved = submitUnresolved;
    setCheck({ status: 'checking' });
    setConfirming(false);
    setConfirmTag('');
    setSubmitError(null);
    if (!hadPending) {
      setResubmit({ status: 'idle' });
      setResubmitTag('');
      setResubmitRisk(false);
    }
    try {
      // 409 常常是“当前版本变了”，只刷新 Release 会拿着旧 version 继续冲突。
      // 提交结果未知时还必须重查更新状态：只看 version/Release 无法判断是否已送达。
      const statusPromise: Promise<StatusResult | null> = hadPending
        ? fetchStatus(controller.signal)
        : Promise.resolve<StatusResult | null>(null);
      const releasePromise = withAbort(api.checkRelease(), controller.signal).then(
        (result) => ({ kind: 'ok' as const, result }),
        (error: unknown) => ({ kind: 'error' as const, error }),
      );
      const [versionResult, statusResult, releaseOutcome] = await Promise.all([
        fetchVersion(controller.signal),
        statusPromise,
        releasePromise,
      ]);
      if (!aliveRef.current || checkSeq !== checkSeqRef.current || controller.signal.aborted) return;

      let attached = false;
      if (statusResult?.kind === 'ok') {
        attached = attachPending(statusResult.status);
      }
      if (statusResult?.kind === 'waiting') setConnection('waiting');

      if (releaseOutcome.kind === 'error') {
        if (controller.signal.aborted || isAbortError(releaseOutcome.error)) return;
        setCheck({ status: 'error', error: explainError(releaseOutcome.error) });
        if (wasUnresolved && !attached) {
          // 重查失败不能丢失“可能已送达”的风险提示。
          setSubmitUnresolved(true);
          setRecheckRequired(true);
        }
        return;
      }

      const stillPending = hadPending && pendingRef.current !== null;
      const stale =
        terminalEventRef.current !== terminalAtStart ||
        versionResult !== 'ok' ||
        stillPending;
      setCheck({ status: 'done', result: releaseOutcome.result, stale });
      if (stillPending) {
        // 普通“重新检查”不是风险许可：pending 不在这里解除。
        if (wasUnresolved) setSubmitUnresolved(true);
        setRecheckRequired(true);
      } else if (!stale) {
        setRecheckRequired(false);
      }
      if (versionResult === 'waiting') setConnection('waiting');
      else if (versionResult === 'ok') setConnection((prev) => (prev === 'waiting' ? prev : 'ok'));
    } catch (error) {
      if (!aliveRef.current || checkSeq !== checkSeqRef.current || isAbortError(error)) return;
      setCheck({ status: 'error', error: explainError(error) });
      if (wasUnresolved) {
        setSubmitUnresolved(true);
        setRecheckRequired(true);
      }
    } finally {
      if (checkSeq === checkSeqRef.current) checkingRef.current = false;
    }
  };

  const target = check.status === 'done' ? installTarget(check.result) : null;
  const blocker = installBlocker({
    target,
    version: info,
    status,
    job,
    recheckRequired,
    connection,
  });
  const jobView = job ? describeJob(job, currentCommit, connection) : null;
  const staleReleasePresentation: ReleasePresentation = {
    tone: 'warn',
    title: 'Release 结果已过期',
    detail:
      '更新任务已经结束，目标与当前版本可能都已变化；请重新检查最新 Release 与当前运行版本后再决定是否安装。',
  };
  const presentation =
    check.status === 'done'
      ? check.stale
        ? staleReleasePresentation
        : describeRelease(check.result)
      : null;
  const published = check.status === 'done' ? formatPublished(check.result.published_at) : null;
  const tagMatches = target ? confirmTag.trim() === target.tag : false;
  const resubmitTagMatches =
    resubmit.status === 'confirm' ? resubmitTag.trim() === resubmit.target.tag : false;
  const installVisible =
    !pendingSubmit &&
    (!job || job.state === 'succeeded' || job.state === 'failed' || job.state === 'needs_manual');
  const statusNote = describeStatusNote(status, job);

  const openConfirm = () => {
    if (!target || blocker || submittingRef.current) return;
    setConfirmTag('');
    setSubmitError(null);
    setConfirming(true);
  };

  const cancelConfirm = () => {
    if (submittingRef.current) return;
    setConfirming(false);
    setConfirmTag('');
  };

  const submitUpdate = async () => {
    // 双击/回车重复触发时只放行第一次。
    if (submittingRef.current) return;
    const result = check.status === 'done' ? check.result : null;
    const request = buildUpdateRequest(result, info, confirmTag);
    if (!target || !request || !tagMatches || blocker) return;
    submittingRef.current = true;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const created = await api.requestUpdate(request);
      if (!aliveRef.current) return;
      const nextStatus = statusRef.current
        ? { ...statusRef.current, current_job: created }
        : statusRef.current;
      jobRef.current = created;
      statusRef.current = nextStatus;
      setJob(created);
      setStatus(nextStatus);
      setConfirming(false);
      setConfirmTag('');
    } catch (error) {
      if (!aliveRef.current) return;
      const explained = explainUpdateError(error);
      // 结果未知由待确认横幅统一说明，不再叠一条“连接已断开”的错误横幅。
      setSubmitError(explained.connectionLost ? null : explained);
      if (explained.connectionLost) {
        // 请求可能已经落地：只查状态，不重发。后续由轮询持续核对。
        const baseline =
          jobRef.current?.request_id ?? statusRef.current?.current_job?.request_id ?? null;
        // 提交结果未知时先收起确认区：恢复后若旧任务还在，不能让确认区自己冒出来。
        setConfirming(false);
        setConfirmTag('');
        setPending({
          startedAt: new Date().toISOString(),
          baselineRequestId: baseline,
          attempts: 0,
          targetTag: target.tag,
          targetCommit: target.commit,
        });
        setSubmitUnresolved(false);
        setPollPaused(false);
        setConnection('waiting');
      } else if (explained.needsRecheck) {
        setRecheckRequired(true);
        setConfirming(false);
        setConfirmTag('');
      }
    } finally {
      submittingRef.current = false;
      if (aliveRef.current) setSubmitting(false);
    }
  };

  /**
   * 人工“重新提交”流程。只在 unresolved 后显式使用：
   * 先成功刷新 status + version + Release，且 status 没有 active/manual 任务，
   * 再让用户额外勾选“先前请求可能已被接受”的风险确认；普通重新检查不会走到这里。
   */
  const startResubmit = async () => {
    if (!submitUnresolved || !pendingRef.current) return;
    if (resubmit.status === 'checking') return;
    const controller = new AbortController();
    resubmitControllerRef.current?.abort();
    resubmitControllerRef.current = controller;
    setResubmit({ status: 'checking' });
    setResubmitTag('');
    setResubmitRisk(false);
    try {
      const releasePromise = withAbort(api.checkRelease(), controller.signal).then(
        (result) => ({ kind: 'ok' as const, result }),
        (error: unknown) => ({ kind: 'error' as const, error }),
      );
      const [versionResult, statusResult, releaseOutcome] = await Promise.all([
        fetchVersion(controller.signal),
        fetchStatus(controller.signal),
        releasePromise,
      ]);
      if (!aliveRef.current || controller.signal.aborted) return;
      if (releaseOutcome.kind === 'error') {
        if (isAbortError(releaseOutcome.error)) return;
        setResubmit({
          status: 'error',
          message: `无法刷新 Release（${explainError(releaseOutcome.error).reason}）；继续保留“结果未知”，不会重发。`,
        });
        return;
      }
      if (statusResult.kind !== 'ok') {
        setResubmit({
          status: 'error',
          message: '无法确认更新状态；继续保留“结果未知”，不会重发。',
        });
        return;
      }
      if (versionResult !== 'ok') {
        setResubmit({
          status: 'error',
          message: '无法刷新当前版本；继续保留“结果未知”，不会重发。',
        });
        return;
      }
      setConnection('ok');
      const pendingNow = pendingRef.current;
      if (!pendingNow) {
        setResubmit({ status: 'idle' });
        return;
      }
      const current = statusResult.status;
      const candidate = current.current_job;
      if (
        candidate &&
        isSubmittedJob(candidate, pendingNow.baselineRequestId, {
          tag: pendingNow.targetTag,
          commit: pendingNow.targetCommit,
        })
      ) {
        // 重查期间接上了本次提交：直接恢复任务，不再提交。
        attachPending(current);
        setCheck({ status: 'done', result: releaseOutcome.result, stale: false });
        setResubmit({ status: 'idle' });
        return;
      }
      if (candidate && isSubmittedJob(candidate, pendingNow.baselineRequestId)) {
        setResubmit({
          status: 'blocked',
          message: `服务端出现了一个目标不同的更新任务（${candidate.target_tag}）；本页不会把它当成本次提交，也不会重新提交。请等待该任务结束或到宿主机核实。`,
        });
        setRecheckRequired(true);
        return;
      }
      if (jobBlocksNewUpdate(current.current_job)) {
        setResubmit({
          status: 'blocked',
          message: `服务端报告有更新任务正在进行（${current.current_job?.target_tag ?? '未知目标'}）；本页不会重新提交，请等待或到宿主机核实。`,
        });
        setRecheckRequired(true);
        return;
      }
      if (!current.enabled) {
        setResubmit({
          status: 'blocked',
          message: `更新通道当前不可用（${describeUpdateReason(current.reason ?? 'disabled')}），不能重新提交。`,
        });
        return;
      }
      if (current.reason && current.reason !== 'update_in_progress') {
        setResubmit({
          status: 'blocked',
          message: `更新门禁未放行（${describeUpdateReason(current.reason)}），不能重新提交。`,
        });
        return;
      }
      if (!current.executor?.online) {
        setResubmit({
          status: 'blocked',
          message: `更新执行器不在线（${describeUpdateReason('executor_offline')}），不能重新提交。`,
        });
        return;
      }
      const freshTarget = installTarget(releaseOutcome.result);
      const freshVersion = infoRef.current;
      if (!freshTarget) {
        setResubmit({
          status: 'blocked',
          message: 'Release 里没有完整可安装目标，不能重新提交。',
        });
        return;
      }
      if (!freshVersion?.current || !freshVersion.commit) {
        setResubmit({
          status: 'blocked',
          message: '当前版本缺少版本号或构建提交，不能重新提交。',
        });
        return;
      }
      // 状态仍是 baseline 不能证明没送达；这里只负责打开“需要额外风险确认”的入口。
      setCheck({ status: 'done', result: releaseOutcome.result, stale: false });
      setResubmit({
        status: 'confirm',
        release: releaseOutcome.result,
        target: freshTarget,
        version: freshVersion,
      });
    } catch (error) {
      if (!aliveRef.current || isAbortError(error)) return;
      setResubmit({
        status: 'error',
        message: `刷新失败（${explainError(error).reason}）；继续保留“结果未知”，不会重发。`,
      });
    }
  };

  const cancelResubmit = () => {
    if (submittingRef.current) return;
    setResubmit({ status: 'idle' });
    setResubmitTag('');
    setResubmitRisk(false);
  };

  const submitResubmit = async () => {
    if (submittingRef.current) return;
    if (resubmit.status !== 'confirm') return;
    if (!resubmitRisk || !resubmitTagMatches) return;
    const pending = pendingRef.current;
    if (!pending) return;
    const request = buildUpdateRequest(resubmit.release, resubmit.version, resubmitTag);
    if (!request) return;
    submittingRef.current = true;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const created = await api.requestUpdate(request);
      if (!aliveRef.current) return;
      const nextStatus = statusRef.current
        ? { ...statusRef.current, current_job: created }
        : statusRef.current;
      jobRef.current = created;
      statusRef.current = nextStatus;
      setJob(created);
      setStatus(nextStatus);
      setPending(null);
      setSubmitUnresolved(false);
      setPollPaused(false);
      setSubmitError(null);
      setResubmit({ status: 'idle' });
      setResubmitTag('');
      setResubmitRisk(false);
      setConfirming(false);
      setConfirmTag('');
    } catch (error) {
      if (!aliveRef.current) return;
      const explained = explainUpdateError(error);
      if (explained.connectionLost) {
        const baseline =
          pendingRef.current?.baselineRequestId ??
          jobRef.current?.request_id ??
          statusRef.current?.current_job?.request_id ??
          null;
        setPending({
          startedAt: new Date().toISOString(),
          baselineRequestId: baseline,
          attempts: 0,
          targetTag: resubmit.target.tag,
          targetCommit: resubmit.target.commit,
        });
        setSubmitUnresolved(false);
        setPollPaused(false);
        setConnection('waiting');
        setResubmit({ status: 'idle' });
        setResubmitTag('');
        setResubmitRisk(false);
      } else {
        // 409 等明确错误不吞掉，也不解锁普通安装：仍保留“结果未知”。
        setSubmitUnresolved(true);
        setRecheckRequired(true);
        setResubmit({
          status: 'error',
          message: `${explained.reason}${explained.hint ? `：${explained.hint}` : ''}。页面仍保留“结果未知”，不会自动重发。`,
        });
      }
    } finally {
      submittingRef.current = false;
      if (aliveRef.current) setSubmitting(false);
    }
  };

  const retryVerify = () => {
    void fetchVersion();
  };

  const togglePolling = () => {
    setPollPaused((prev) => !prev);
    if (pollPaused) setPollNonce((value) => value + 1);
  };

  const retryNow = () => {
    setPollPaused(false);
    if (pendingSubmit) {
      setPending({ ...pendingSubmit, attempts: 0 });
      setSubmitUnresolved(false);
      setSubmitError(null);
      setRecheckRequired(false);
      setResubmit({ status: 'idle' });
      setResubmitTag('');
      setResubmitRisk(false);
    }
    setPollNonce((value) => value + 1);
  };

  const checkDone = check.status === 'done';

  const jobHeadline = jobView ? (
    <div
      className={`update-job update-job--${jobView.kind}`}
      data-testid="update-job"
      data-job-state={job?.state}
      data-request-id={job?.request_id}
    >
      <div className="update-job__head">
        <strong>{jobView.title}</strong>
        <code>{job?.target_tag}</code>
      </div>
      {job && (job.state === 'queued' || job.state === 'running') ? (
        <div
          className="update-progress"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={stageProgress(job.stage)}
        >
          <span style={{ width: `${stageProgress(job.stage)}%` }} />
        </div>
      ) : null}
      <p className="update-job__detail">{jobView.detail}</p>
      {job?.message ? <p className="update-job__detail">{job.message}</p> : null}
      {job?.error_code ? (
        <p className="update-job__detail">
          错误码 <code>{job.error_code}</code>
        </p>
      ) : null}
      {jobView.kind === 'succeeded' ? (
        <div className="panel__actions">
          <button
            type="button"
            className="button button--small"
            data-testid="update-reload"
            onClick={() => window.location.reload()}
          >
            <RefreshIcon /> 重新加载页面
          </button>
        </div>
      ) : null}
      {jobView.kind === 'verifying' || jobView.kind === 'unverifiable' ? (
        <div className="panel__actions">
          <button
            type="button"
            className="button button--small"
            data-testid="update-reverify"
            onClick={retryVerify}
          >
            <RefreshIcon /> 重新验证
          </button>
        </div>
      ) : null}
    </div>
  ) : null;

  return (
    <section className="page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回设置
      </button>

      <h1 className="page__title">关于与版本</h1>

      <section className="panel">
        <div className="panel__head">
          <h2>当前运行</h2>
          <button
            type="button"
            className="button button--small"
            onClick={() => void reloadAll()}
            disabled={loading}
          >
            <RefreshIcon /> 刷新
          </button>
        </div>

        {loading ? <p className="panel__note">读取版本信息…</p> : null}

        {loadError ? (
          <div className="banner banner--error">
            <AlertIcon />
            <div>
              <strong>{loadError.reason}</strong>
              {loadError.hint ? <div className="banner__detail">{loadError.hint}</div> : null}
            </div>
          </div>
        ) : null}

        {info ? (
          <dl className="about-facts" data-testid="about-version-facts">
            <dt>版本</dt>
            <dd>
              <code>{info.current}</code>
            </dd>
            <dt>构建提交</dt>
            <dd>
              {info.commit ? (
                <code title={info.commit}>{shortDigest(info.commit)}</code>
              ) : (
                <span className="about-facts__muted">未注入（LING_COMMIT 未设置）</span>
              )}
            </dd>
            <dt>前端清单</dt>
            <dd>
              {info.web_manifest_sha256 ? (
                <code title={info.web_manifest_sha256}>
                  {shortDigest(info.web_manifest_sha256)}
                </code>
              ) : (
                <span className="about-facts__muted">未找到发布清单</span>
              )}
            </dd>
          </dl>
        ) : null}
      </section>

      <section className="panel">
        <div className="panel__head">
          <h2>GitHub Release</h2>
        </div>
        <div className="panel__actions">
          <button
            type="button"
            className="button"
            data-testid="check-release"
            onClick={() => void runCheck()}
            disabled={check.status === 'checking'}
          >
            {check.status === 'checking' ? (
              <>检查中…</>
            ) : (
              <>
                <RefreshIcon /> 检查最新 Release
              </>
            )}
          </button>
        </div>

        {presentation && check.status === 'done' ? (
          <div
            className={`banner ${
              presentation.tone === 'ok'
                ? 'banner--ok'
                : presentation.tone === 'info'
                  ? 'banner--info'
                  : 'banner--warn'
            }`}
            data-testid="release-result"
          >
            {presentation.tone === 'ok' ? <CheckIcon /> : <AlertIcon />}
            <div>
              <strong>{presentation.title}</strong>
              <div className="banner__detail">{presentation.detail}</div>
              {published ? (
                <div className="banner__detail">发布时间：{published}</div>
              ) : null}
              {check.result.html_url ? (
                <a
                  className="about-release__link"
                  href={check.result.html_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  在 GitHub 查看 Release
                </a>
              ) : null}
            </div>
          </div>
        ) : null}

        {check.status === 'error' ? (
          <div className="banner banner--error" data-testid="release-error">
            <AlertIcon />
            <div>
              <strong>{check.error.reason}</strong>
              {check.error.hint ? <div className="banner__detail">{check.error.hint}</div> : null}
            </div>
          </div>
        ) : null}
      </section>

      <section className="panel">
        <div className="panel__head">
          <h2>安装更新</h2>
          {runningJob ? <span className="panel__count">每 3 秒刷新</span> : null}
          {pollPaused && pollMode === null && (pendingSubmit || connection === 'waiting') ? (
            <span className="panel__count">自动刷新已暂停</span>
          ) : null}
        </div>

        {status ? (
          <dl className="about-facts about-facts--tight">
            <dt>更新通道</dt>
            <dd>
              {status.enabled
                ? '已启用'
                : status.reason && status.reason !== 'disabled'
                  ? `未启用：${describeUpdateReason(status.reason)}`
                  : '未启用'}
            </dd>
            <dt>执行器</dt>
            <dd>
              {status.executor?.online
                ? '在线'
                : `离线${
                    status.executor?.last_seen_at
                      ? `（最近心跳 ${formatPublished(status.executor.last_seen_at)}）`
                      : ''
                  }`}
            </dd>
            {statusNote ? (
              <>
                <dt>当前状态</dt>
                <dd data-testid="update-status-reason">{statusNote}</dd>
              </>
            ) : null}
          </dl>
        ) : null}

        {!status && loading ? <p className="panel__note">读取更新状态…</p> : null}

        {pendingSubmit ? (
          <div className="banner banner--warn" data-testid="update-pending">
            <AlertIcon />
            <div>
              <strong>
                {submitUnresolved
                  ? '没能确认这次提交是否已送达'
                  : pollPaused
                    ? '已暂停自动查询（提交结果仍未知）'
                    : '提交后没有收到响应，正在确认'}
              </strong>
              <div className="banner__detail">
                {submitUnresolved
                  ? '状态里仍然没有出现可确认的新任务。请求可能没有送达，也可能已送达但状态副本仍旧；页面不会自动重发。重新检查会同时读取更新状态、当前版本与 Release，不会解除这份风险提示。'
                  : pollPaused
                    ? '请求可能已经送达，也可能没有。恢复自动查询后会接上同一份状态，不会重复提交。'
                    : `请求可能已经送达。页面每 ${Math.round(
                        pollDelayMs(0, 'recover') / 1000,
                      )} 秒左右查询一次更新状态，恢复后自动接上，不会重复提交。`}
              </div>
              <div className="panel__actions">
                <button
                  type="button"
                  className="button button--small"
                  data-testid="update-pending-retry"
                  onClick={retryNow}
                >
                  <RefreshIcon /> 立即查询状态
                </button>
                <button
                  type="button"
                  className="button button--small"
                  data-testid="update-pending-pause"
                  onClick={togglePolling}
                  disabled={submitUnresolved}
                >
                  {pollPaused ? '继续自动查询' : '停止自动查询'}
                </button>
                {submitUnresolved ? (
                  <button
                    type="button"
                    className="button button--small"
                    data-testid="update-recheck"
                    onClick={() => void runCheck()}
                    disabled={check.status === 'checking'}
                  >
                    <RefreshIcon /> 重新检查状态、版本与 Release
                  </button>
                ) : null}
              </div>
            </div>
          </div>
        ) : connection === 'waiting' && !runningJob ? (
          <div className="banner banner--warn" data-testid="update-connection">
            <AlertIcon />
            <div>
              <strong>
                {pollPaused ? '已暂停自动重试' : '与服务端的连接已断开，正在自动重试'}
              </strong>
              <div className="banner__detail">
                这不等于更新完成或失败；恢复后会继续查询当前任务，不会重复提交。
              </div>
              <div className="panel__actions">
                <button
                  type="button"
                  className="button button--small"
                  data-testid="update-connection-retry"
                  onClick={retryNow}
                >
                  <RefreshIcon /> 立即重试
                </button>
                <button
                  type="button"
                  className="button button--small"
                  data-testid="update-connection-pause"
                  onClick={togglePolling}
                >
                  {pollPaused ? '继续自动重试' : '停止自动重试'}
                </button>
              </div>
            </div>
          </div>
        ) : null}

        {pendingSubmit && submitUnresolved ? (
          <div className="update-resubmit" data-testid="update-resubmit-panel">
            <p className="update-confirm__lead">确实没有送达时的人工恢复</p>
            <p className="panel__note">
              只有在宿主机或服务端确认<strong>从未收到</strong>这次请求时才使用这里的流程。
              它会先重新读取更新状态、当前版本与 Release；如果仍然看不到新任务，还会要求你额外确认
              “先前的请求可能已经被接受”的风险。
            </p>
            {resubmit.status === 'idle' ? (
              <div className="panel__actions">
                <button
                  type="button"
                  className="button button--small"
                  data-testid="update-resubmit"
                  onClick={() => void startResubmit()}
                  disabled={check.status === 'checking' || submitting}
                >
                  <RefreshIcon /> 核对状态后重新提交
                </button>
              </div>
            ) : null}
            {resubmit.status === 'checking' ? (
              <p className="panel__note" data-testid="update-resubmit-checking">
                正在刷新更新状态、当前版本与 Release…
              </p>
            ) : null}
            {resubmit.status === 'blocked' || resubmit.status === 'error' ? (
              <div className="banner banner--warn" data-testid="update-resubmit-blocked">
                <AlertIcon />
                <div>
                  <strong>{resubmit.message}</strong>
                  <div className="panel__actions">
                    <button
                      type="button"
                      className="button button--small"
                      onClick={() => void startResubmit()}
                      disabled={submitting}
                      data-testid="update-resubmit-retry"
                    >
                      <RefreshIcon /> 再核对一次
                    </button>
                  </div>
                </div>
              </div>
            ) : null}
            {resubmit.status === 'confirm' ? (
              <div className="update-confirm" data-testid="update-resubmit-confirm">
                <p className="update-confirm__lead">重新提交前必须确认风险</p>
                <dl className="about-facts">
                  <dt>当前</dt>
                  <dd>
                    <code>{resubmit.version.current}</code>{' '}
                    <span className="about-facts__muted">
                      {shortSha(resubmit.version.commit)}
                    </span>
                  </dd>
                  <dt>目标</dt>
                  <dd>
                    <code>{resubmit.target.tag}</code>{' '}
                    <span className="about-facts__muted">
                      {shortSha(resubmit.target.commit)}
                    </span>
                  </dd>
                </dl>
                <ul className="update-risk">
                  <li>
                    先前的请求<strong>可能已经被服务端接受</strong>；如果它已经送达，这次重新提交可能产生第二个更新任务。
                  </li>
                  <li>
                    只有确认服务端始终没有收到上次请求时才应继续；否则请等待状态恢复或到宿主机核实。
                  </li>
                  <li>重新提交仍会短暂停机，失败可能需人工处理，系统不会自动回滚。</li>
                </ul>
                <label className="update-risk-ack">
                  <input
                    type="checkbox"
                    data-testid="update-resubmit-risk"
                    checked={resubmitRisk}
                    onChange={(event) => setResubmitRisk(event.target.checked)}
                    disabled={submitting}
                  />
                  <span>
                    我明白先前的请求可能已被接受；我仍要重新提交更新请求。
                  </span>
                </label>
                <label className="field">
                  <span className="field__label">
                    输入完整标签 <code>{resubmit.target.tag}</code> 以确认
                  </span>
                  <input
                    className="field__input field__mono"
                    type="text"
                    value={resubmitTag}
                    autoComplete="off"
                    spellCheck={false}
                    placeholder={resubmit.target.tag}
                    data-testid="update-resubmit-input"
                    aria-label={`输入完整标签 ${resubmit.target.tag} 以确认重新提交`}
                    onChange={(event) => setResubmitTag(event.target.value)}
                    disabled={submitting}
                  />
                </label>
                {!resubmitTagMatches && resubmitTag.length > 0 ? (
                  <p className="panel__hint" data-testid="update-resubmit-tag-mismatch">
                    标签需要完全一致（区分大小写）。
                  </p>
                ) : null}
                <div className="panel__actions">
                  <button
                    type="button"
                    className="button button--small"
                    onClick={cancelResubmit}
                    disabled={submitting}
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    className="button button--small"
                    data-testid="update-resubmit-submit"
                    onClick={() => void submitResubmit()}
                    disabled={!resubmitRisk || !resubmitTagMatches || submitting}
                  >
                    {submitting ? '提交中…' : '仍然重新提交'}
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        ) : null}

        {updateError ? (
          <div className="banner banner--warn" data-testid="update-status-error">
            <AlertIcon />
            <div>
              <strong>{updateError.reason}</strong>
              {updateError.hint ? <div className="banner__detail">{updateError.hint}</div> : null}
            </div>
          </div>
        ) : null}

        {submitError && !pendingSubmit ? (
          <div className="banner banner--error" data-testid="update-submit-error">
            <AlertIcon />
            <div>
              <strong>{submitError.reason}</strong>
              {submitError.hint ? <div className="banner__detail">{submitError.hint}</div> : null}
              {submitError.needsRecheck ? (
                <div className="panel__actions">
                  <button
                    type="button"
                    className="button button--small"
                    onClick={() => void runCheck()}
                    disabled={check.status === 'checking'}
                    data-testid="update-recheck"
                  >
                    <RefreshIcon /> 重新检查 Release 与当前版本
                  </button>
                </div>
              ) : null}
            </div>
          </div>
        ) : null}

        {/* 提交结果未知时先不显示旧任务卡：旧 succeeded 不能被当成本次提交的成功。 */}
        {job && !pendingSubmit ? jobHeadline : null}

        {job && !pendingSubmit && isTerminalJob(job) ? (
          <p className="panel__note" data-testid="update-history">
            {job.state === 'needs_manual'
              ? '这次任务需要先在宿主机处理并 adopt-current；处理完更新状态才会刷新，本页不能忽略或取消。'
              : '这是最近一次更新的结果，不会阻止下一次安装；重新检查 Release 与当前版本后可以再次确认。'}
          </p>
        ) : null}

        {installVisible ? (
          <>
            {!checkDone ? (
              <p className="panel__note" data-testid="update-need-check">
                先检查最新 Release，拿到可安装的目标后才能确认。
              </p>
            ) : null}

            {checkDone && !target ? (
              <p className="panel__note" data-testid="update-target-none">
                {check.result.status === 'no_release'
                  ? '当前没有 Release，没有可安装的目标。'
                  : check.result.status === 'unavailable'
                    ? '暂时无法确认目标版本，不能安装。'
                    : check.result.update_eligible === false
                      ? `Release 不可安装：${describeUpdateReason(check.result.update_reason)}`
                      : check.result.is_newer === false
                        ? '当前版本已经是发布版本，不需要安装。'
                        : 'Release 缺少目标提交或发布编号，不能安装。'}
              </p>
            ) : null}

            {target && !confirming ? (
              <div className="panel__actions">
                <button
                  type="button"
                  className="button"
                  data-testid="update-install"
                  onClick={openConfirm}
                  disabled={blocker !== null || submitting}
                >
                  安装 {target.tag}
                </button>
                {blocker ? (
                  <span className="panel__hint" data-testid="update-blocked">
                    {blocker}
                  </span>
                ) : null}
                {recheckRequired ? (
                  <button
                    type="button"
                    className="button button--small"
                    data-testid="update-recheck-inline"
                    onClick={() => void runCheck()}
                    disabled={check.status === 'checking'}
                  >
                    <RefreshIcon /> 重新检查
                  </button>
                ) : null}
              </div>
            ) : null}

            {target && confirming ? (
              <div className="update-confirm" data-testid="update-confirm" ref={confirmBoxRef}>
                <p className="update-confirm__lead">确认安装 {target.tag}</p>
                <dl className="about-facts">
                  <dt>当前</dt>
                  <dd>
                    <code>{info?.current ?? '未知'}</code>{' '}
                    <span className="about-facts__muted">{shortSha(info?.commit ?? null)}</span>
                  </dd>
                  <dt>目标</dt>
                  <dd>
                    <code>{target.tag}</code>{' '}
                    <span className="about-facts__muted">{shortSha(target.commit)}</span>
                  </dd>
                </dl>
                <ul className="update-risk">
                  <li>安装会拉取并构建新版本，期间服务短暂停机，正在生成的回复会中断。</li>
                  <li>如果包含数据库迁移，迁移之后失败需要人工处理；系统不会自动回滚。</li>
                  <li>数据库不会被自动恢复或回滚到安装前的状态。</li>
                </ul>
                <label className="field">
                  <span className="field__label">
                    输入完整标签 <code>{target.tag}</code> 以确认
                  </span>
                  <input
                    ref={inputRef}
                    className="field__input field__mono"
                    type="text"
                    value={confirmTag}
                    autoComplete="off"
                    spellCheck={false}
                    placeholder={target.tag}
                    data-testid="update-confirm-input"
                    aria-label={`输入完整标签 ${target.tag} 以确认`}
                    onChange={(event) => setConfirmTag(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') {
                        event.preventDefault();
                        void submitUpdate();
                      } else if (event.key === 'Escape') {
                        event.preventDefault();
                        cancelConfirm();
                      }
                    }}
                    disabled={submitting}
                  />
                </label>
                {!tagMatches && confirmTag.length > 0 ? (
                  <p className="panel__hint" data-testid="update-tag-mismatch">
                    标签需要完全一致（区分大小写）。
                  </p>
                ) : null}
                <div className="panel__actions">
                  <button
                    type="button"
                    className="button button--small"
                    onClick={cancelConfirm}
                    disabled={submitting}
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    className="button button--small"
                    data-testid="update-submit"
                    onClick={() => void submitUpdate()}
                    disabled={!tagMatches || submitting || blocker !== null}
                  >
                    {submitting ? '提交中…' : '确认安装并重启'}
                  </button>
                </div>
              </div>
            ) : null}
          </>
        ) : null}
      </section>
    </section>
  );
}
