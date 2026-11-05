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
import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api, parseErrorResponse } from '../api';
import { explainError, type ExplainedError } from '../errors';
import type {
  ImageUpdateSubmission,
  ImageUpdateSubmissionAck,
  ImageUpdateTargetRef,
  ImageUpdateTask,
  ImageUpdateView,
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
  // 门禁原因：提交被拒时 `_gate_error`（`version.py:708-712`）复用
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

/** 与后端 `protocol.MAX_BACKUP_WARNINGS` 对齐；多出来的条目不再展示。 */
const MAX_BACKUP_WARNINGS = 8;

/**
 * 任务里的**备份告警**（可选字段 `backup_warnings`，后端协议仍是 v1）。
 *
 * 为什么要有这一层：执行器对 `media_state=partial`（媒体归档时有文件正在上传）
 * 选择「判通过 + 告警」，如果不显式展示，运维只会看到“更新完成”，等到真要恢复
 * 才发现媒体缺文件。
 *
 * 容错：字段缺失（旧执行器不写）、不是数组、条目不是字符串/为空，都只是“没有
 * 这一条告警”，绝不让页面抛异常；未知的多余字段一律忽略。
 */
export function backupWarnings(job: UpdateJob | null | undefined): string[] {
  const raw: unknown = job?.backup_warnings;
  if (!Array.isArray(raw)) return [];
  const notes: string[] = [];
  for (const item of raw) {
    if (typeof item !== 'string') continue;
    const text = item.trim();
    if (!text) continue;
    notes.push(text);
    if (notes.length >= MAX_BACKUP_WARNINGS) break;
  }
  return notes;
}

export interface BackupWarningNote {
  title: string;
  detail: string;
  items: string[];
}

/**
 * 备份告警的界面文案；没有告警时返回 null（页面就不显示任何东西）。
 *
 * 文案必须说清后果：备份整体通过 → 升级继续，但媒体归档可能漏文件 →
 * 恢复时要人工核对 → 细节在宿主机的 `verify-warnings.txt`（不是本页折叠区）。
 */
export function describeBackupWarnings(
  job: UpdateJob | null | undefined,
): BackupWarningNote | null {
  const items = backupWarnings(job);
  if (items.length === 0) return null;
  const succeeded = job?.state === 'succeeded';
  return {
    title: succeeded ? '升级成功，但备份不完整，需要人工核对' : '备份不完整，需要人工核对',
    detail:
      '数据库与环境文件的备份完整，但媒体归档可能漏文件（备份时有文件正在上传）；' +
      '恢复时需人工核对，详见宿主备份目录里的 verify-warnings.txt。',
    items,
  };
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
  // 更新通道的 503 复用 `provider_unavailable` 这个错误码（`_gate_error`，`version.py:708-712`；
  // 以及 `create_update_request` 里 `release_unverifiable` 的分支，`version.py:772-777`），与模型供应商无关：
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

function SourceAboutPage({ onBack }: AboutPageProps) {
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

  const backupWarning = describeBackupWarnings(job);
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
      {backupWarning ? (
        // 显著位置、不被折叠：升级「成功」但备份不完整时，这是最容易被忽略、
        // 后果又最贵的一条（恢复时缺媒体就是真的缺）。
        <div
          className="banner banner--warn update-backup-warnings"
          role="status"
          data-testid="backup-warnings"
        >
          <AlertIcon />
          <div>
            <strong data-testid="backup-warnings-title">{backupWarning.title}</strong>
            <div className="banner__detail" data-testid="backup-warnings-detail">
              {backupWarning.detail}
            </div>
            <ul className="banner__list">
              {backupWarning.items.map((note, index) => (
                <li key={`${index}-${note}`}>{note}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
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

/* ============================================================ 镜像模式更新面板
 *
 * 设计依据 docs/EASY-INSTALL-UPDATE-DESIGN.md §1（日常更新）、§5（一次网页更新的
 * 实际顺序）、§6（故障时给用户什么结果）。
 *
 * 与上面的源码模式通道**刻意分开**：`AboutPage` 按 `image_mode` 二选一，镜像模式
 * 不渲染「检查最新 Release / 安装 <tag>」，源码模式不渲染本面板——否则用户分不清
 * 哪个「更新」按钮是真的。
 *
 * 本面板只展示后端给的事实，不自造进度：
 * - 阶段只来自 `task.stage` 的真实码表（prepare/backup/migrate/switch/verify），
 *   **没有百分比、没有剩余时间**（后端本来就不产生这类字段）；
 * - 失败原因与下一步只用后端码表文案（`failure.message` / `next_actions` /
 *   `message` / `next_actions`），前端不另编理由；
 * - 成功必须三项核验（运行容器 digest、应用提交、迁移 head）齐全且一致；
 * - 没有「回退版本」按钮：设计 §6 末行明确禁止会误导的一键恢复。
 */

const IMAGE_REQUEST_ID_RE = /^[0-9a-f]{32}$/;
const IMAGE_DIGEST_RE = /^ghcr\.io\/qsbb\/ling-api@sha256:[0-9a-f]{64}$/;

/** 进行中的轮询节奏：3 秒（与源码通道一致）；空闲 15 秒（只读本地状态文件，很轻）。 */
const IMAGE_POLL_ACTIVE_MS = 3000;
const IMAGE_POLL_IDLE_MS = 15000;

/** 未知阶段/更新器状态的兜底：只给中文，绝不把英文码值打到页面上。 */
export const IMAGE_STAGE_UNKNOWN_LABEL = '处理中（阶段不可识别）';

/** 更新器的真实阶段顺序（与后端 `IMAGE_STAGE_MESSAGES` / 更新器 `STAGES` 一致）。 */
export const IMAGE_UPDATE_STAGES = ['prepare', 'backup', 'migrate', 'switch', 'verify'] as const;

const IMAGE_STAGE_LABELS: Record<string, string> = {
  prepare: '下载并预检目标镜像',
  backup: '做一致备份（恢复点）',
  migrate: '执行数据库迁移',
  switch: '切换并启动新版本',
  verify: '验证新版本',
};

/** 真实阶段 → 中文标签；未知阶段给兜底文案，不显示英文原文。 */
export function imageUpdateStageLabel(stage: string | null | undefined): string {
  if (stage === null || stage === undefined || stage === '') return '尚未开始';
  return IMAGE_STAGE_LABELS[stage] ?? IMAGE_STAGE_UNKNOWN_LABEL;
}

/** 阶段在真实顺序里的下标；未知/缺失给 -1（不猜进度）。 */
export function imageUpdateStageIndex(stage: string | null | undefined): number {
  if (!stage) return -1;
  return IMAGE_UPDATE_STAGES.indexOf(stage as (typeof IMAGE_UPDATE_STAGES)[number]);
}

/** 更新器状态码 → 中文；未知状态不显示英文码值。 */
export function imageUpdaterStateLabel(state: string | null | undefined): string {
  switch (state) {
    case 'idle':
      return '空闲';
    case 'busy':
      return '正在执行任务';
    case 'needs_manual':
      return '需要人工处理';
    default:
      return '状态未知';
  }
}

function shortDigestRef(value: string | null): string {
  if (!value) return '未知';
  const marker = '@sha256:';
  const at = value.indexOf(marker);
  if (at >= 0) return `sha256:${value.slice(at + marker.length, at + marker.length + 12)}…`;
  return value.length > 20 ? `${value.slice(0, 20)}…` : value;
}

function shortRef(value: string | null): string {
  if (!value) return '缺失';
  return value.length > 24 ? `${value.slice(0, 24)}…` : value;
}

export function newImageUpdateRequestId(): string {
  const cryptoApi = globalThis.crypto;
  if (!cryptoApi || typeof cryptoApi.getRandomValues !== 'function') {
    throw new Error('当前环境没有安全随机数，无法生成更新请求编号');
  }
  const bytes = new Uint8Array(16);
  cryptoApi.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

export function isValidImageRequestId(value: string): boolean {
  return IMAGE_REQUEST_ID_RE.test(value);
}

/**
 * 目标来自哪里：可信发布目录 / 已排队请求 / 任务记录 / 外部提供 / 没有目标。
 */
export type ImageUpdateTargetSource = 'catalog' | 'request' | 'task' | 'external' | 'none';

export interface ImageUpdateTarget extends ImageUpdateTargetRef {
  source: ImageUpdateTargetSource;
  /** 只有发布目录目标带版本/迁移信息（请求、任务记录里没有版本）。 */
  version?: string;
  hasMigration?: boolean;
}

/**
 * 目标摘要的唯一来源。
 *
 * 优先级：后端给的**可信发布目录**目标 → 排队中的请求 → 任务记录。目录目标来自
 * `GET /v1/version/image-update` 的 `target`（后端从更新器状态的
 * `recommended_target` 透出，只给 version/digest/commit/has_migration 四个字段），
 * 是唯一"真正可选"的目标；请求/任务只是"已经发生过或正在排队"的事实。
 *
 * 三者都没有就诚实地说「暂无可用目标」，绝不在前端拼一个目标。
 */
export function imageUpdateTarget(view: ImageUpdateView | null): ImageUpdateTarget {
  if (view === null) return { digest: null, commit: null, source: 'none' };
  const catalogTarget = view.target;
  if (catalogTarget !== null) {
    return {
      digest: catalogTarget.digest,
      commit: catalogTarget.commit,
      source: 'catalog',
      version: catalogTarget.version,
      hasMigration: catalogTarget.has_migration,
    };
  }
  const request = view.request;
  if (request !== null && request.target_digest !== null) {
    const commit =
      view.task !== null && view.task.target.digest === request.target_digest
        ? view.task.target.commit
        : null;
    return { digest: request.target_digest, commit, source: 'request' };
  }
  const task = view.task;
  if (task !== null && task.target.digest !== null) {
    return { digest: task.target.digest, commit: task.target.commit, source: 'task' };
  }
  return { digest: null, commit: null, source: 'none' };
}

export interface ImageUpdateGate {
  enabled: boolean;
  targetDigest: string | null;
  /** 前端的一致性检查：后端说「可安装」但字段互相矛盾时补一句诚实的说明。 */
  notes: string[];
}

/**
 * 「更新」按钮能不能提交。
 *
 * `installable === false` 时不在这里另写理由——后端已经给了 `message` 与
 * `next_actions`（码表脱敏文案），面板原样展示；这里只在**后端说可安装、但字段
 * 互相矛盾**（离线、无恢复点、当前摘要未知、已有任务、没有目标…）时补一句说明，
 * 并把按钮禁用（fail closed）。
 */
export function imageUpdateGate(
  view: ImageUpdateView | null,
  targetDigest?: string | null,
): ImageUpdateGate {
  const target = targetDigest === undefined ? imageUpdateTarget(view).digest : targetDigest;
  if (view === null) {
    return {
      enabled: false,
      targetDigest: target,
      notes: ['还没有读到更新状态，先点「检查更新」。'],
    };
  }
  const notes: string[] = [];
  if (!view.image_mode) notes.push('当前不是镜像模式部署，镜像更新通道不可用。');
  if (view.installable) {
    if (view.updater.online !== true) notes.push('更新器当前不在线，不能提交。');
    if (view.backup_gate.restore_point !== true) notes.push('没有可用的恢复点，备份门禁未就绪。');
    if (view.current.digest === null || view.current.commit === null) {
      notes.push('当前运行的镜像摘要或提交未知，不能提交。');
    }
    if (view.task !== null && view.task.state === 'running') {
      notes.push('已有更新任务在进行中，等它结束再提交。');
    }
    if (view.request !== null) notes.push('已有排队中的更新请求，不能重复提交。');
    if (target === null) {
      notes.push(
        '暂无可用目标：可信发布目录里没有适用于当前版本的新版本，本页不能发起更新。',
      );
    } else if (view.current.digest !== null && target === view.current.digest) {
      notes.push('目标摘要与当前运行摘要相同，不需要更新。');
    }
  }
  const enabled = view.image_mode && view.installable === true && notes.length === 0;
  return { enabled, targetDigest: target, notes };
}

export function canSubmitImageUpdate(
  view: ImageUpdateView | null,
  targetDigest?: string | null,
): boolean {
  return imageUpdateGate(view, targetDigest).enabled;
}

/* ---------------------------------------------------------------- 三项核验 */

export type ImageVerificationCheckId = 'container_digest' | 'app_commit' | 'migration_head';

export interface ImageVerificationCheck {
  id: ImageVerificationCheckId;
  label: string;
  /** 任务记录里的期望值（后端白名单投影，可能缺失）。 */
  expected: string | null;
  /** 现在能实测到的值：当前 API 报告的 digest/commit，或迁移后的 head。 */
  actual: string | null;
  ok: boolean;
}

export type ImageVerificationState = 'unavailable' | 'unfinished' | 'verified' | 'mismatch';

export interface ImageUpdateVerification {
  state: ImageVerificationState;
  checks: ImageVerificationCheck[];
  verified: boolean;
}

function verificationCheck(
  id: ImageVerificationCheckId,
  label: string,
  expected: string | null,
  actual: string | null,
): ImageVerificationCheck {
  return { id, label, expected, actual, ok: expected !== null && actual !== null && expected === actual };
}

/**
 * 成功判定：**只认三项核验**（设计 §1 末句）。
 *
 * 1. 运行容器 digest：任务记录的目标 digest vs 当前 API 自己报告的 digest；
 * 2. 应用提交：任务记录的目标 commit vs 当前 API 的构建提交；
 * 3. 迁移 head：任务记录的目标 head vs 迁移后的实际 head。
 *
 * 任一项缺失或不一致 → 不判成功（不拿「Compose 退出 0」或「页面又连上了」当成功）。
 * 参数是整个视图而不是单独的 task，因为 1/2 必须和**当前进程**报的值比对。
 */
export function verifyOutcome(view: ImageUpdateView | null): ImageUpdateVerification {
  const task = view?.task ?? null;
  if (view === null || task === null) {
    return {
      state: 'unavailable',
      verified: false,
      checks: [
        verificationCheck('container_digest', '运行容器 digest', null, null),
        verificationCheck('app_commit', '应用提交', null, null),
        verificationCheck('migration_head', '迁移 head', null, null),
      ],
    };
  }
  const checks = [
    verificationCheck('container_digest', '运行容器 digest', task.target.digest, view.current.digest),
    verificationCheck('app_commit', '应用提交', task.target.commit, view.current.commit),
    verificationCheck(
      'migration_head',
      '迁移 head',
      task.migration.target_head,
      task.migration.current_after,
    ),
  ];
  const verified = task.state === 'succeeded' && checks.every((check) => check.ok);
  const state: ImageVerificationState =
    task.state !== 'succeeded' ? 'unfinished' : verified ? 'verified' : 'mismatch';
  return { state, checks, verified };
}

/**
 * `still_on_old_version` 的三态文案。
 *
 * `null` 表示更新器**没有实测**（后端约定：没实测就不敢断言），必须显示「未能确认」，
 * 不能显示成「仍在旧版本」——那会把一个未知状态说成事实。
 */
export function describeImageUpdateOldVersion(task: ImageUpdateTask | null): string {
  if (task === null) return '没有任务记录，无法判断当前版本状态。';
  if (task.still_on_old_version === true) return '已实测：仍在旧版本（本次更新没有进入迁移）。';
  if (task.still_on_old_version === false) return '已实测：当前不在旧版本。';
  return '未能确认（更新器没有实测旧服务状态）。';
}

/* ------------------------------------------------------------ 状态与重连 */

export type ImageUpdateTone = 'ok' | 'info' | 'warn' | 'danger';

export interface ImageUpdatePresentation {
  tone: ImageUpdateTone;
  title: string;
  detail: string;
}

/** 面板顶部的状态横幅：只翻译后端码表 + 三项核验结论，不预测进度。 */
export function describeImageUpdateState(view: ImageUpdateView | null): ImageUpdatePresentation {
  if (view === null) {
    return { tone: 'info', title: '正在读取更新状态', detail: '还没有拿到更新器状态；稍等或点「检查更新」。' };
  }
  if (!view.image_mode) {
    return {
      tone: 'warn',
      title: '镜像更新通道不可用',
      detail: view.message ?? '后端没有把这次部署判定为镜像模式。',
    };
  }
  const task = view.task;
  if (task === null) {
    if (view.installable) {
      return {
        tone: 'info',
        title: '可以更新',
        detail: '更新器在线、恢复点齐全。点「更新」后会先要求二次确认；更新期间服务短暂停机。',
      };
    }
    return {
      tone: 'warn',
      title: '当前不能更新',
      detail: '后端给出了不可安装的原因与下一步，见下方说明；「更新」按钮已禁用。',
    };
  }
  if (task.state === 'running') {
    return {
      tone: 'info',
      title: '正在更新',
      detail: `${task.stage_message ?? imageUpdateStageLabel(task.stage)}。页面断线不会中断任务，重连后继续显示同一个任务。`,
    };
  }
  if (task.state === 'succeeded') {
    const verification = verifyOutcome(view);
    if (verification.verified) {
      return {
        tone: 'ok',
        title: '更新完成（三项核验通过）',
        detail: `运行容器 digest、应用提交与迁移 head 与任务记录一致：${shortDigestRef(view.current.digest)}。`,
      };
    }
    return {
      tone: 'warn',
      title: '更新已结束，但核验未通过',
      detail: '后端报告任务成功，但三项核验没有全部通过；以下是实测差异，不把这一状态说成「更新完成」。',
    };
  }
  if (task.state === 'failed_not_started') {
    return {
      tone: 'warn',
      title: '更新未开始，未进入迁移',
      detail:
        task.still_on_old_version === true
          ? `${task.state_message}；更新器已实测旧服务正常。`
          : `${task.state_message}；${describeImageUpdateOldVersion(task)}`,
    };
  }
  if (task.state === 'needs_manual') {
    return {
      tone: 'danger',
      title: '更新未完成，需要维护处理',
      detail: `${task.state_message}；维护门禁可能仍在（公开端口保持关闭），按任务编号人工核对后再恢复。`,
    };
  }
  return { tone: 'info', title: task.state_message, detail: '该任务已人工确认，不再阻断新的更新。' };
}

export interface ImageConnectionNotice {
  title: string;
  detail: string;
}

/** 断线横幅：断线既不是失败也不是完成，任务与 request_id 都保留。 */
export function describeImageConnection(state: ImageUpdatePanelState): ImageConnectionNotice | null {
  if (state.connection === 'ok') return null;
  const requestId = state.taskRequestId ?? state.submittedRequestId;
  const base =
    requestId === null
      ? '暂时读不到更新状态；重连后会继续读取同一个任务。断线不算失败，也不会新建任务。'
      : `暂时读不到更新状态；后台任务 ${requestId} 不受影响，重连后会继续显示同一个任务。断线不算失败，也不会新建任务。`;
  // 连续失败才升级成「可执行的恢复指引」，第一次断开先如实说在重连。
  const hint =
    state.failures >= 3
      ? ' 连续多次读不到状态：确认后端进程与网络可用，或稍后点「检查更新」。'
      : '';
  return { title: '正在重新连接…', detail: `${base}${hint}` };
}

/* ------------------------------------------------------ 轮询 / 断线重连模型 */

export type ImageUpdateConnection = 'ok' | 'waiting';

export interface ImageUpdatePanelState {
  view: ImageUpdateView | null;
  connection: ImageUpdateConnection;
  /** 最近一次成功读到的任务 ID：断线时**保留**，用于说明「还是同一个任务」。 */
  taskRequestId: string | null;
  /** 本页提交成功后回执里的 request_id；断线重连后仍用它核对同一个任务。 */
  submittedRequestId: string | null;
  /** 连续轮询失败次数（退避用，成功清零）。 */
  failures: number;
}

export function initialImageUpdateState(): ImageUpdatePanelState {
  return { view: null, connection: 'ok', taskRequestId: null, submittedRequestId: null, failures: 0 };
}

/**
 * 一次轮询的结果合并。
 *
 * `incoming === null` 表示这次没读到（断线/超时）：**不清空** view、任务 ID 或
 * 自己提交的 request_id，只把连接状态标成 waiting——断线不是失败，也不是完成。
 */
export function applyImageUpdatePoll(
  current: ImageUpdatePanelState,
  incoming: ImageUpdateView | null,
): ImageUpdatePanelState {
  if (incoming === null) {
    return { ...current, connection: 'waiting', failures: current.failures + 1 };
  }
  return {
    view: incoming,
    connection: 'ok',
    taskRequestId: incoming.task?.request_id ?? null,
    submittedRequestId: current.submittedRequestId,
    failures: 0,
  };
}

export function withSubmittedRequest(
  current: ImageUpdatePanelState,
  requestId: string,
): ImageUpdatePanelState {
  return { ...current, submittedRequestId: requestId };
}

/** 下一次轮询的等待时间：断线退避，进行中 3 秒，空闲 15 秒。 */
export function imagePollDelayMs(state: ImageUpdatePanelState): number {
  if (state.connection === 'waiting') {
    const index = Math.min(Math.max(state.failures - 1, 0), RECOVERY_DELAYS_MS.length - 1);
    return RECOVERY_DELAYS_MS[index] ?? IMAGE_POLL_ACTIVE_MS;
  }
  const view = state.view;
  if (view === null) return IMAGE_POLL_ACTIVE_MS;
  if (view.task?.state === 'running') return IMAGE_POLL_ACTIVE_MS;
  if (view.request !== null) return IMAGE_POLL_ACTIVE_MS;
  return IMAGE_POLL_IDLE_MS;
}

/* -------------------------------------------------------------- 提交请求体 */

/**
 * POST 的**固定 5 键**请求体（多一个键后端就 422）。
 *
 * 拿不到目标/当前摘要、目标格式不对、或目标与当前相同，一律返回 null——宁可
 * 不让按钮生效，也不提交一个后端会拒绝的请求。
 */
export function buildImageUpdateSubmission(
  view: ImageUpdateView | null,
  targetDigest: string | null,
  requestId: string,
): ImageUpdateSubmission | null {
  if (view === null || targetDigest === null) return null;
  if (!isValidImageRequestId(requestId)) return null;
  if (!IMAGE_DIGEST_RE.test(targetDigest)) return null;
  if (view.current.digest === null || view.current.commit === null) return null;
  if (view.current.digest === targetDigest) return null;
  return {
    request_id: requestId,
    target_digest: targetDigest,
    expected_current_digest: view.current.digest,
    expected_current_commit: view.current.commit,
    operator_confirmed: true,
  };
}

/** 提交回执丢失后，用同一个 request_id 判断请求是否已经落地（不重复提交）。 */
export function imageRequestLanded(view: ImageUpdateView | null, requestId: string | null): boolean {
  if (view === null || requestId === null) return false;
  return view.request?.request_id === requestId || view.task?.request_id === requestId;
}

export interface ImageUpdateSubmitError {
  /** 后端码表/接口消息（已脱敏），直接展示。 */
  reason: string;
  hint: string;
  /** 需要重新读一次状态（提交被拒的原因属于门禁，答案在 GET 里）。 */
  refresh: boolean;
}

export function explainImageSubmitError(error: unknown): ImageUpdateSubmitError {
  if (error instanceof ApiError) {
    if (error.code === 'network_error' || error.status === 0) {
      return {
        reason: '提交时连接中断，结果未知',
        hint: '不要重复提交：重新检查更新，看是否已经出现同一个 request_id；本页不会自动重发。',
        refresh: true,
      };
    }
    if (error.status === 401) {
      return { reason: '登录已过期，需要重新登录', hint: '重新登录后再发起更新。', refresh: false };
    }
    if (error.status === 429) {
      return { reason: error.message || '提交过于频繁', hint: '等待一分钟后重试。', refresh: false };
    }
    if (error.status === 422) {
      return {
        reason: error.message || '请求格式未被接受',
        hint: '本页只提交固定 5 键请求；刷新后重试。',
        refresh: true,
      };
    }
    return { reason: error.message || '提交被拒绝', hint: '已刷新当前状态；按页面给出的原因与下一步处理。', refresh: true };
  }
  return { reason: '提交失败', hint: '刷新后按状态说明处理。', refresh: true };
}

/* ------------------------------------------------------------------ 传输层 */

/**
 * 镜像通道的两个端点在这里直接 fetch：`api.ts` 不在本批改动范围内。
 *
 * 401 仍要回到登录表单：下面借用一次 `api.version()`（它走统一的 401 处理）触发
 * 应用层的登录回跳，然后照常把错误交给调用方。
 */
async function fetchImageUpdateView(signal?: AbortSignal): Promise<ImageUpdateView> {
  let response: Response;
  try {
    response = await fetch('/v1/version/image-update', {
      credentials: 'same-origin',
      cache: 'no-store',
      ...(signal ? { signal } : {}),
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new ApiError(
      0,
      'network_error',
      '无法连接服务端，请确认后端已启动或网络可用',
      true,
      cause instanceof Error ? cause.message : null,
    );
  }
  if (response.status === 401) await api.version().catch(() => undefined);
  if (!response.ok) throw await parseErrorResponse(response);
  return (await response.json()) as ImageUpdateView;
}

async function postImageUpdateRequest(
  body: ImageUpdateSubmission,
): Promise<ImageUpdateSubmissionAck> {
  let response: Response;
  try {
    response = await fetch('/v1/version/image-update-requests', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Ling-Update': '1' },
      body: JSON.stringify(body),
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new ApiError(
      0,
      'network_error',
      '提交时无法连接服务端，结果未知',
      true,
      cause instanceof Error ? cause.message : null,
    );
  }
  if (response.status === 401) await api.version().catch(() => undefined);
  if (!response.ok) throw await parseErrorResponse(response);
  return (await response.json()) as ImageUpdateSubmissionAck;
}

/* ------------------------------------------------------------------ 面板 */

function imageToneClass(tone: ImageUpdateTone): string {
  switch (tone) {
    case 'ok':
      return 'banner--ok';
    case 'warn':
      return 'banner--warn';
    case 'danger':
      return 'banner--error';
    default:
      return 'banner--info';
  }
}

export interface ImageUpdatePanelProps {
  version: VersionInfo | null;
  state: ImageUpdatePanelState;
  /**
   * 目标摘要的**显式覆盖**（可选的第二个来源）。
   *
   * 默认来源是后端 `GET /v1/version/image-update` 的 `target`（可信发布目录）；
   * 这个参数只在调用方手里有别的目标摘要时使用。传 null 与不传等价（都不放行），
   * 目标缺失时页面显示「暂无可用目标」并禁用，绝不假装可以更新。
   */
  targetDigest?: string | null;
  confirming: boolean;
  riskAcknowledged: boolean;
  submitting: boolean;
  submitError: ImageUpdateSubmitError | null;
  onRefresh: () => void;
  onStartConfirm: () => void;
  onCancelConfirm: () => void;
  onToggleRisk: () => void;
  onConfirm: () => void;
}

/**
 * 纯展示组件：全部输入都来自 props，没有任何 hook/请求，便于用
 * `renderToStaticMarkup` 直接断言「没有百分比、没有剩余时间、没有回退按钮」。
 */
export function ImageUpdatePanel(props: ImageUpdatePanelProps) {
  const { version, state, confirming, riskAcknowledged, submitting, submitError } = props;
  const view = state.view;
  const task = view?.task ?? null;
  const externalTarget = props.targetDigest;
  const target: ImageUpdateTarget =
    externalTarget === undefined || externalTarget === null
      ? imageUpdateTarget(view)
      : { digest: externalTarget, commit: null, source: 'external' };
  const gate = imageUpdateGate(view, externalTarget);
  const presentation = describeImageUpdateState(view);
  const notice = describeImageConnection(state);
  const verification = verifyOutcome(view);
  const stageIndex = imageUpdateStageIndex(task?.stage ?? null);
  const oldVersionText = describeImageUpdateOldVersion(task);
  // 断线期间不让人提交：视图可能是断线前的旧状态，POST 也大概率失败。
  const canStart = gate.enabled && state.connection === 'ok';

  return (
    <section className="panel" data-testid="image-update-panel">
      <div className="panel__head">
        <h2>更新（镜像模式）</h2>
        <span className="panel__count" data-testid="image-update-gate">
          {gate.enabled ? '可以更新' : '当前不可提交'}
        </span>
        <button
          type="button"
          className="button button--small"
          data-testid="image-update-refresh"
          onClick={props.onRefresh}
          disabled={submitting}
        >
          <RefreshIcon /> 检查更新
        </button>
      </div>

      <dl className="about-facts" data-testid="image-update-facts">
        <dt>当前版本</dt>
        <dd>
          <code>{version?.current ?? '未知'}</code>{' '}
          <span className="about-facts__muted">
            {version?.commit ? shortSha(version.commit) : '构建提交未注入'}
          </span>
        </dd>
        <dt>当前 digest</dt>
        <dd data-testid="image-update-current-digest">
          {view?.current.digest ? (
            <code title={view.current.digest}>{shortDigestRef(view.current.digest)}</code>
          ) : (
            <span className="about-facts__muted">未知（LING_APP_IMAGE 未注入）</span>
          )}
        </dd>
        <dt>目标版本</dt>
        <dd data-testid="image-update-target">
          {target.digest === null ? (
            <span className="about-facts__muted">暂无可用目标</span>
          ) : (
            <>
              {target.version ? <code>{target.version}</code> : null}
              <code title={target.digest}>{shortDigestRef(target.digest)}</code>
              {target.commit ? (
                <span className="about-facts__muted"> 提交 {shortSha(target.commit)}</span>
              ) : null}
              <span className="about-facts__muted">
                {target.source === 'catalog'
                  ? '（可信发布目录）'
                  : target.source === 'request'
                    ? '（已排队的请求）'
                    : target.source === 'task'
                      ? '（任务记录）'
                      : '（外部提供）'}
              </span>
              {target.hasMigration ? (
                <span className="about-facts__muted"> 含数据库迁移</span>
              ) : null}
            </>
          )}
        </dd>
        <dt>更新器</dt>
        <dd data-testid="image-update-updater">
          {view ? (
            <>
              {view.updater.online ? '在线' : '离线'}
              {`（${imageUpdaterStateLabel(view.updater.state)}）`}
              {view.updater.protocol !== null ? (
                <span className="about-facts__muted"> 协议 v{view.updater.protocol}</span>
              ) : null}
            </>
          ) : (
            <span className="about-facts__muted">未知</span>
          )}
        </dd>
        <dt>恢复点</dt>
        <dd data-testid="image-update-backup-gate">
          {view ? (
            <>
              {view.backup_gate.restore_point ? '可用' : '不可用'}
              {!view.backup_gate.restore_point && view.backup_gate.reason_message ? (
                <span className="about-facts__muted">：{view.backup_gate.reason_message}</span>
              ) : null}
            </>
          ) : (
            <span className="about-facts__muted">未知</span>
          )}
        </dd>
      </dl>

      {notice ? (
        <div className="banner banner--warn" data-testid="image-update-reconnect">
          <AlertIcon />
          <div>
            <strong>{notice.title}</strong>
            <div className="banner__detail">{notice.detail}</div>
          </div>
        </div>
      ) : null}

      <div className={`banner ${imageToneClass(presentation.tone)}`} data-testid="image-update-state">
        {presentation.tone === 'ok' ? <CheckIcon /> : <AlertIcon />}
        <div>
          <strong>{presentation.title}</strong>
          <div className="banner__detail">{presentation.detail}</div>
        </div>
      </div>

      {view !== null && !view.installable ? (
        <div className="banner banner--warn" data-testid="image-update-blocked">
          <AlertIcon />
          <div>
            <strong>{view.message ?? '当前不满足更新条件。'}</strong>
            {view.next_actions.length > 0 ? (
              <ul className="banner__list" data-testid="image-update-next-actions">
                {view.next_actions.map((action) => (
                  <li key={action}>{action}</li>
                ))}
              </ul>
            ) : null}
          </div>
        </div>
      ) : null}

      {gate.notes.length > 0 ? (
        <ul className="banner__list" data-testid="image-update-gate-notes">
          {gate.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}

      {task ? (
        <div className="update-job" data-testid="image-update-task">
          <div className="update-job__head">
            <strong>{task.state_message}</strong>
            <code data-testid="image-update-request-id">
              {task.request_id ?? state.submittedRequestId ?? '请求编号未记录'}
            </code>
          </div>
          {task.state === 'running' ? (
            <>
              <p className="update-job__detail" data-testid="image-update-stage">
                当前阶段：{task.stage_message ?? imageUpdateStageLabel(task.stage)}
              </p>
              <p className="update-job__detail" data-testid="image-update-stage-line">
                {IMAGE_UPDATE_STAGES.map((stage, index) => (
                  <span key={stage}>
                    {index > 0 ? ' → ' : ''}
                    {imageUpdateStageLabel(stage)}
                    {index === stageIndex ? '（当前）' : ''}
                  </span>
                ))}
              </p>
            </>
          ) : task.stage !== null ? (
            <p className="update-job__detail" data-testid="image-update-stage">
              最后阶段：{imageUpdateStageLabel(task.stage)}
            </p>
          ) : null}
          {task.state !== 'running' ? (
            <p className="update-job__detail" data-testid="image-update-old-version">
              {oldVersionText}
            </p>
          ) : null}
          {task.warnings.length > 0 ? (
            <ul className="banner__list" data-testid="image-update-warnings">
              {task.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ) : null}
          {task.failure ? (
            <div className="banner banner--error" data-testid="image-update-failure">
              <AlertIcon />
              <div>
                <strong>{task.failure.message}</strong>
                {task.failure.stage !== null ? (
                  <div className="banner__detail">
                    停止阶段：{imageUpdateStageLabel(task.failure.stage)}
                  </div>
                ) : null}
                {task.next_actions.length > 0 ? (
                  <ul className="banner__list" data-testid="image-update-failure-actions">
                    {task.next_actions.map((action) => (
                      <li key={action}>{action}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {task !== null && task.state === 'succeeded' ? (
        <div className="update-job" data-testid="image-update-verification">
          <div className="update-job__head">
            <strong>
              {verification.verified ? '三项核验通过：更新完成' : '三项核验未通过：不能算更新完成'}
            </strong>
          </div>
          <dl className="about-facts about-facts--tight">
            {verification.checks.map((check) => (
              <Fragment key={check.id}>
                <dt>{check.label}</dt>
                <dd data-testid={`image-update-check-${check.id}`}>
                  {check.ok ? <CheckIcon /> : <AlertIcon />}{' '}
                  期望 <code title={check.expected ?? undefined}>{shortRef(check.expected)}</code>{' '}
                  实际 <code title={check.actual ?? undefined}>{shortRef(check.actual)}</code>{' '}
                  <span className="about-facts__muted">
                    {check.ok
                      ? '一致'
                      : check.expected === null || check.actual === null
                        ? '缺失，无法确认'
                        : '不一致'}
                  </span>
                </dd>
              </Fragment>
            ))}
          </dl>
        </div>
      ) : null}

      {submitError ? (
        <div className="banner banner--error" data-testid="image-update-submit-error">
          <AlertIcon />
          <div>
            <strong>{submitError.reason}</strong>
            {submitError.hint ? <div className="banner__detail">{submitError.hint}</div> : null}
          </div>
        </div>
      ) : null}

      <div className="panel__actions">
        {!confirming ? (
          <button
            type="button"
            className="button"
            data-testid="image-update-start"
            onClick={props.onStartConfirm}
            disabled={!canStart || submitting}
          >
            更新
          </button>
        ) : null}
        {!gate.enabled && !confirming && gate.notes.length > 0 ? (
          <span className="panel__hint" data-testid="image-update-blocked-hint">
            {gate.notes[0]}
          </span>
        ) : null}
      </div>

      {confirming ? (
        <div className="update-confirm" data-testid="image-update-confirm">
          <p className="update-confirm__lead">
            确认更新到 {target.digest === null ? '目标摘要' : shortDigestRef(target.digest)}
          </p>
          <dl className="about-facts">
            <dt>当前</dt>
            <dd>
              <code>{version?.current ?? '未知'}</code>{' '}
              <span className="about-facts__muted">{shortDigestRef(view?.current.digest ?? null)}</span>
            </dd>
            <dt>目标</dt>
            <dd>
              <code>{target.digest ?? '未知'}</code>
            </dd>
          </dl>
          <ul className="update-risk">
            <li>
              更新会短暂停机：维护门禁生效期间控制台、机器人和定时任务都会暂停写入，正在生成的回复会中断。
            </li>
            <li>
              更新前会做一次一致备份；但数据库迁移一旦执行就不可撤销，系统不会自动恢复旧镜像或旧数据，
              失败后需要人工处理。
            </li>
            <li>是否成功只以运行容器 digest、应用提交与迁移 head 三项核验为准，不以命令退出码或页面重连为准。</li>
          </ul>
          <label className="update-risk-ack">
            <input
              type="checkbox"
              checked={riskAcknowledged}
              onChange={props.onToggleRisk}
              data-testid="image-update-risk-ack"
            />
            <span>我已知晓会短暂停机，且数据库迁移不可撤销</span>
          </label>
          <div className="panel__actions">
            <button
              type="button"
              className="button button--small"
              data-testid="image-confirm-cancel"
              onClick={props.onCancelConfirm}
              disabled={submitting}
            >
              取消
            </button>
            <button
              type="button"
              className="button button--small"
              data-testid="image-update-submit"
              onClick={props.onConfirm}
              disabled={!riskAcknowledged || submitting || !canStart}
            >
              {submitting ? '提交中…' : '确认更新并重启'}
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}

/* ------------------------------------------------------------------ 页面 */

export interface ImageUpdatePageProps {
  onBack: () => void;
  initialView?: ImageUpdateView | null;
}

export function ImageUpdatePage({ onBack, initialView = null }: ImageUpdatePageProps) {
  const [state, setState] = useState<ImageUpdatePanelState>(() =>
    initialView === null
      ? initialImageUpdateState()
      : applyImageUpdatePoll(initialImageUpdateState(), initialView),
  );
  const [version, setVersion] = useState<VersionInfo | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [riskAcknowledged, setRiskAcknowledged] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<ImageUpdateSubmitError | null>(null);
  const [pollNonce, setPollNonce] = useState(0);

  const aliveRef = useRef(true);
  const stateRef = useRef(state);
  const submittingRef = useRef(false);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  useEffect(() => {
    let disposed = false;
    void (async () => {
      try {
        const next = await api.version();
        if (!disposed) setVersion(next);
      } catch {
        // 版本号只是展示；读不到时保持「未知」，不猜。
      }
    })();
    return () => {
      disposed = true;
    };
  }, []);

  useEffect(() => {
    let disposed = false;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;

    const schedule = (delay: number) => {
      if (disposed) return;
      timer = setTimeout(() => {
        void poll();
      }, delay);
    };

    const poll = async () => {
      if (disposed) return;
      let incoming: ImageUpdateView | null = null;
      let failed = false;
      try {
        incoming = await fetchImageUpdateView(controller.signal);
      } catch (error) {
        if (isAbortError(error) || disposed) return;
        failed = true;
      }
      if (disposed || !aliveRef.current) return;
      const next = applyImageUpdatePoll(stateRef.current, failed ? null : incoming);
      stateRef.current = next;
      setState(next);
      schedule(imagePollDelayMs(next));
    };

    void poll();
    return () => {
      disposed = true;
      controller.abort();
      if (timer !== null) clearTimeout(timer);
    };
  }, [pollNonce, initialView]);

  const submit = useCallback(async () => {
    if (submittingRef.current) return;
    const snapshot = stateRef.current;
    const target = imageUpdateTarget(snapshot.view);
    let requestId: string;
    try {
      requestId = newImageUpdateRequestId();
    } catch {
      setSubmitError({
        reason: '无法生成安全的请求编号',
        hint: '当前浏览器不支持安全随机数；换用受支持的浏览器后重试。',
        refresh: false,
      });
      return;
    }
    const body = buildImageUpdateSubmission(snapshot.view, target.digest, requestId);
    if (body === null) {
      setSubmitError({
        reason: '还没有可提交的目标',
        hint: '先点「检查更新」，确认目标摘要与当前摘要后再提交。',
        refresh: true,
      });
      setConfirming(false);
      setRiskAcknowledged(false);
      setPollNonce((value) => value + 1);
      return;
    }
    submittingRef.current = true;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const ack = await postImageUpdateRequest(body);
      const next = withSubmittedRequest(stateRef.current, ack.request_id);
      stateRef.current = next;
      setState(next);
      setConfirming(false);
      setRiskAcknowledged(false);
      setPollNonce((value) => value + 1);
    } catch (error) {
      // 提交被拒/结果未知：一律收起二次确认，先重新读状态再让人决定下一步。
      setSubmitError(explainImageSubmitError(error));
      setConfirming(false);
      setRiskAcknowledged(false);
      setPollNonce((value) => value + 1);
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }, []);

  const refresh = useCallback(() => {
    setSubmitError(null);
    setPollNonce((value) => value + 1);
  }, []);

  return (
    <section className="page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回设置
      </button>

      <h1 className="page__title">关于与版本</h1>

      <ImageUpdatePanel
        version={version}
        state={state}
        confirming={confirming}
        riskAcknowledged={riskAcknowledged}
        submitting={submitting}
        submitError={submitError}
        onRefresh={refresh}
        onStartConfirm={() => {
          setSubmitError(null);
          setRiskAcknowledged(false);
          setConfirming(true);
        }}
        onCancelConfirm={() => {
          setConfirming(false);
          setRiskAcknowledged(false);
        }}
        onToggleRisk={() => setRiskAcknowledged((value) => !value)}
        onConfirm={() => {
          void submit();
        }}
      />
    </section>
  );
}

/* ------------------------------------------------------------ 模式二选一 */

type ImageModeProbe = { status: 'probing' } | { status: 'done'; imageView: ImageUpdateView | null };

function AboutProbe({ onBack }: AboutPageProps) {
  return (
    <section className="page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回设置
      </button>
      <h1 className="page__title">关于与版本</h1>
      <p className="panel__note" data-testid="about-probe">
        读取更新通道…
      </p>
    </section>
  );
}

/**
 * 页面入口：先读一次 `GET /v1/version/image-update` 决定走哪套 UI。
 *
 * - `image_mode === true` → 镜像面板（本文件下半部分）；
 * - 否则（含端点不存在/读不到的旧服务端）→ 源码模式页面，行为与改动前一致。
 *
 * 两套 UI **不同时渲染**：镜像模式不会短暂出现「安装 <tag>」按钮，源码模式也不会
 * 出现「更新」按钮，用户不需要猜哪个是真的。
 */
export function AboutPage({ onBack }: AboutPageProps) {
  const [probe, setProbe] = useState<ImageModeProbe>({ status: 'probing' });

  useEffect(() => {
    let alive = true;
    const controller = new AbortController();
    void (async () => {
      let imageView: ImageUpdateView | null = null;
      try {
        const view = await fetchImageUpdateView(controller.signal);
        imageView = view.image_mode ? view : null;
      } catch (error) {
        if (isAbortError(error)) return;
        imageView = null;
      }
      if (!alive) return;
      setProbe({ status: 'done', imageView });
    })();
    return () => {
      alive = false;
      controller.abort();
    };
  }, []);

  if (probe.status === 'probing') return <AboutProbe onBack={onBack} />;
  if (probe.imageView !== null) {
    return <ImageUpdatePage onBack={onBack} initialView={probe.imageView} />;
  }
  return <SourceAboutPage onBack={onBack} />;
}
