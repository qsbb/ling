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
  ImageUpdateCurrentV2,
  ImageUpdateNewerReleaseV2,
  ImageUpdateSubmission,
  ImageUpdateTargetV2,
  ImageUpdateTaskV2,
  ImageUpdateV2,
  ReleaseCheck,
  UpdateJob,
  UpdateRequest,
  UpdateStatus,
  VersionInfo,
} from '../types';
import { AlertIcon, ArrowLeftIcon, CheckIcon, RefreshIcon } from './Icons';
import '../update-card.css';

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

/* ==================================================== 镜像模式：更新卡片（四态）
 *
 * 设计依据 `docs/UPDATE-UX-DESIGN.md`（本批的权威）：
 *
 * - 首屏只有：当前版本、**服务端给的一句话结论**、检查时间、一个主按钮。
 *   三段横幅、原因码墙、安装门禁 notes、目标 `sha256` 都不再上首屏。
 * - 四态与按钮：已是最新 →「检查更新」；可更新 →「更新到 vX」；
 *   有新版暂不可用 →「重新检查」；暂不能确认 →「重试检查」。
 *   活动任务覆盖结论展示（「正在更新 vX：阶段」+「查看进度」），此时不许再检查/安装。
 *   非 owner 用同一套四态结论，但主按钮是「查看详情」，不写任何请求。
 * - 「检查更新」是**真检查**：`POST /v1/version/image-update-checks` 写一个检查请求，
 *   等更新器认领并重新取目录/解析摘要；随后每 2 秒重读 GET，按回执的检查编号匹配
 *   `details.catalog.check`（对不上时退回 `checked_at` 变新）。最多等 20 秒，超时
 *   如实说「更新器没有回应」；失败/超时**不清掉上次结论**。
 * - 「更新内容」只在可更新 / 有新版暂不可用两态出现，内容全部来自后端
 *   （`notes` 是发布方原话，前端不改写、不总结、不推断）；`notes_url` 才给
 *   「完整更新日志」链接。
 * - 详情/诊断默认折叠：digest/commit、目录来源与状态、更新器、恢复点、`newer[]`
 *   的不可用原因、推荐目标、最近任务。原因码只允许出现在这里，且必须配一句人话。
 */

const IMAGE_REQUEST_ID_RE = /^[0-9a-f]{32}$/;
const IMAGE_DIGEST_RE = /^ghcr\.io\/qsbb\/ling-api@sha256:[0-9a-f]{64}$/;

/** 空闲轮询 15 秒、任务进行中 3 秒（只读状态文件，很轻）。 */
const IMAGE_POLL_ACTIVE_MS = 3000;
const IMAGE_POLL_IDLE_MS = 15000;

/** 点「检查更新」后每 2 秒读一次，最多等 20 秒。 */
export const IMAGE_CHECK_POLL_MS = 2000;
export const IMAGE_CHECK_TIMEOUT_MS = 20000;
/** 前端去抖：10 秒内重复点击不再发请求（服务端锁与 30 秒限流是最终保证）。 */
export const IMAGE_CHECK_DEBOUNCE_MS = 10000;
/** 「更新到 vX」先复用 30 秒内刚完成的检查，不重复打扰更新器。 */
export const IMAGE_UPDATE_REUSE_CHECK_MS = 30000;

export const IMAGE_CHECK_FAILED_TEXT = '这次检查没成功';
export const IMAGE_CHECK_TIMEOUT_TEXT = '检查超时（更新器没有回应），你可以稍后重试';

/** 「更新内容」最多平铺 8 条，其余折叠。 */
export const IMAGE_NOTES_COLLAPSE_LIMIT = 8;

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

/** 任务状态码 → 中文（只用于详情；首屏不出现码值）。 */
export function imageUpdateTaskStateText(state: string | null | undefined): string {
  switch (state) {
    case 'running':
      return '进行中';
    case 'queued':
    case 'pending':
      return '等待开始';
    case 'succeeded':
      return '已完成';
    case 'failed_not_started':
      return '失败（未进入迁移）';
    case 'needs_manual':
      return '需要人工处理';
    case 'resolved':
      return '已人工确认';
    default:
      return state ? '状态未识别' : '没有任务记录';
  }
}

/**
 * 失败码 → 人话。设计文档要求：原因码只进详情，且必须配一句人话；
 * 未知码一律退回「更新器没有给出可读的失败说明」，不显示自由文本。
 */
const IMAGE_FAILURE_TEXT: Record<string, string> = {
  request_rejected: '更新器拒绝了这次请求',
  already_on_target: '当前已经在目标版本上',
  duplicate_request: '重复的更新请求',
  expected_current_mismatch: '提交之后当前运行的版本变了',
  preflight_failed: '预检没有通过',
  maintenance_gate_failed: '维护门禁没有通过',
  pull_failed: '拉取目标镜像失败',
  backup_failed: '备份没有完成',
  migration_failed: '数据库迁移失败',
  switch_failed: '切换新版本失败',
  old_service_unverified: '没能确认旧服务状态',
  reconcile_needs_manual: '需要人工对账',
  state_unreadable: '任务状态读不出来',
  previous_task_needs_manual: '上次更新还没有处理完',
  install_lock_busy: '已有更新在执行',
  env_write_failed: '写入部署配置失败',
  internal_error: '更新器内部错误',
};

export function imageUpdateFailureText(code: string | null | undefined): string {
  if (code === null || code === undefined || code === '') return '没有记录失败码';
  return IMAGE_FAILURE_TEXT[code] ?? '更新器没有给出可读的失败说明';
}

/**
 * 原因码 → 人话。键就是设计文档 §「判定数据与文案映射」里的机器事实；
 * 未知码只给「暂时无法确认更新条件，请重新检查」，不暴露自由文本。
 */
export const IMAGE_REASON_TEXT: Record<string, string> = {
  digest_unresolved: '新版已发布，但暂时取不到安装包；这不是你的操作问题，请稍后重新检查。',
  not_in_min_from: '这个版本不能直接跨级安装；请先更新到页面提示的过渡版本，再检查一次。',
  migration_unavailable: '这个版本需要的数据库迁移尚未备齐；这不是你的操作问题，请等待修复后再检查。',
  backup_gate_missing: '更新前需要一个可用备份；请先完成一次备份，再重新检查。',
  update_disabled: '本机尚未启用网页更新；请在部署配置中启用后再检查。',
  updater_offline: '更新服务暂时没有响应；请确认更新器容器在运行，然后重试。',
  manual_required: '上次更新尚未处理完；请先展开上次更新，按提示处理后再检查。',
  catalog_missing: '发布目录暂时不可用；请稍后重试，若持续如此请检查部署包。',
  catalog_unreadable: '发布目录暂时不可用；请稍后重试，若持续如此请检查部署包。',
  catalog_invalid: '发布目录暂时不可用；请稍后重试，若持续如此请检查部署包。',
  catalog_schema_unsupported: '发布目录的格式版本不受支持，请更新部署包后再检查。',
  catalog_tool_missing: '更新器缺少目录校验工具，请先升级更新器。',
  catalog_unavailable: '发布目录暂时不可用；请稍后重试，若持续如此请检查部署包。',
  not_owner: '只有所有者可以发起更新。',
  no_available_target: '发布目录里没有适用于当前版本的新版本。',
};

export function imageUpdateReasonText(code: string | null | undefined): string {
  if (code === null || code === undefined || code === '') {
    return '暂时无法确认更新条件，请重新检查。';
  }
  return IMAGE_REASON_TEXT[code] ?? '暂时无法确认更新条件，请重新检查。';
}

/** 目录状态码 → 中文（详情用）。 */
export function imageCatalogStateText(state: string | null | undefined): string {
  switch (state) {
    case 'ok':
      return '正常';
    case 'missing':
      return '缺失';
    case 'invalid':
      return '无效';
    default:
      return '未知';
  }
}

/** 目录来源 → 中文（详情用）；`registry` 是当前来源（可信目录已删除，改为直接查 GHCR）。 */
export function imageCatalogSourceText(source: string | null | undefined): string {
  switch (source) {
    case 'registry':
      return '镜像仓库（GHCR）';
    case 'remote':
      return '远程发布目录';
    case 'local':
      return '本机发布目录（远程读取失败时的回退）';
    case 'none':
      return '没有读到发布目录';
    default:
      return '未知';
  }
}

/** 恢复点门禁 → 中文。 */
export function imageBackupGateText(
  gate: { restore_point?: boolean | null } | null | undefined,
): string {
  if (gate === null || gate === undefined) return '未知';
  if (gate.restore_point === true) return '可用';
  if (gate.restore_point === false) return '不可用';
  return '未知';
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

/** `0.0.3` / `v0.0.3` 都显示成 `v0.0.3`（不叠两个 v）。 */
export function imageVersionLabel(version: string | null | undefined): string | null {
  if (version === null || version === undefined || version.trim() === '') return null;
  const trimmed = version.trim();
  return trimmed.startsWith('v') ? trimmed : `v${trimmed}`;
}

/* ------------------------------------------------------------ 检查时间 */

/** 绝对时间：`11 月 9 日 14:20`；读不出时间时 null（不编一个时间）。 */
export function formatImageCheckTime(checkedAt: string | null | undefined): string | null {
  if (!checkedAt) return null;
  const ms = Date.parse(checkedAt);
  if (Number.isNaN(ms)) return null;
  const date = new Date(ms);
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日 ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 相对时间：`刚刚` / `X 分钟前` / `X 小时前` / `X 天前`。 */
export function formatImageCheckAge(checkedAt: string | null | undefined, nowMs: number): string | null {
  if (!checkedAt) return null;
  const ms = Date.parse(checkedAt);
  if (Number.isNaN(ms)) return null;
  const delta = nowMs - ms;
  if (delta < 60_000) return '刚刚';
  const minutes = Math.floor(delta / 60_000);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

/** 首屏第三行：`检查于 11 月 9 日 14:20（3 分钟前）`；没检查过就说没检查过。 */
export function describeImageCheckedAt(
  checkedAt: string | null | undefined,
  nowMs: number,
): string {
  const absolute = formatImageCheckTime(checkedAt);
  const age = formatImageCheckAge(checkedAt, nowMs);
  if (absolute === null) return '检查于 未知（还没有成功检查过）';
  return `检查于 ${absolute}${age === null ? '' : `（${age}）`}`;
}

/* ------------------------------------------------------------ 真检查 */

export type ImageCheckStatus = 'idle' | 'checking' | 'done' | 'failed' | 'timed_out';

export interface ImageCheckAttempt {
  status: ImageCheckStatus;
  /** 最近一次真正发出检查请求的时刻（毫秒）：10 秒去抖用。 */
  startedAtMs: number | null;
  /** 最近一次检查收尾（成功或超时）的时刻：30 秒复用用。 */
  finishedAtMs: number | null;
  /** 失败/超时的一行人话；成功后清空。 */
  notice: string | null;
}

export function initialImageCheckAttempt(): ImageCheckAttempt {
  return { status: 'idle', startedAtMs: null, finishedAtMs: null, notice: null };
}

/** 10 秒内重复点击不再发请求；检查进行中也直接挡住。 */
export function canStartImageCheck(attempt: ImageCheckAttempt, nowMs: number): boolean {
  if (attempt.status === 'checking') return false;
  if (attempt.startedAtMs === null) return true;
  return nowMs - attempt.startedAtMs >= IMAGE_CHECK_DEBOUNCE_MS;
}

/** 失败/超时要显示的一行人话（成功为 null）。 */
export function imageCheckNotice(attempt: ImageCheckAttempt): string | null {
  if (attempt.status === 'failed') return IMAGE_CHECK_FAILED_TEXT;
  if (attempt.status === 'timed_out') return `${IMAGE_CHECK_FAILED_TEXT}：${IMAGE_CHECK_TIMEOUT_TEXT}`;
  return null;
}

/**
 * `checked_at` 是否变新。
 *
 * - 上次从来没有成功检查过（baseline=null）：任何非空时间都算新结果；
 * - 时间相同 = 还是上次的结果，继续等；
 * - 两个时间都能解析时必须严格变新（时钟回拨不算“这次检查”的结果）；
 * - 解析不了时，只要字符串变了就当真（宁可多认一次，也不把新结果当旧的）。
 */
export function isNewCheckTime(
  next: string | null | undefined,
  baseline: string | null | undefined,
): boolean {
  if (!next) return false;
  if (!baseline) return true;
  if (next === baseline) return false;
  const nextMs = Date.parse(next);
  const baselineMs = Date.parse(baseline);
  if (Number.isNaN(nextMs) || Number.isNaN(baselineMs)) return true;
  return nextMs > baselineMs;
}

export interface ImageCheckResult {
  status: 'skipped' | 'ok' | 'failed' | 'timed_out';
  attempt: ImageCheckAttempt;
  /** `ok` 时是刚读到的视图；失败/超时时是**上次**的视图（结论不清空）。 */
  view: ImageUpdateV2 | null;
  notice: string | null;
}

export interface ImageCheckDeps {
  /** 写一次检查请求（后端 200/201/202 都算已写入）。 */
  postCheck: (checkId: string) => Promise<unknown>;
  /** 重读 GET；轮询期间读失败按「还没好」处理，不抛给用户。 */
  readView: () => Promise<ImageUpdateV2>;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
  newCheckId?: () => string;
  timeoutMs?: number;
  pollMs?: number;
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

/**
 * 回执里的检查编号：后端可能回同一个已排队/刚完成的 ID（幂等）。拿不到就返回 null，
 * 此时退回「`checked_at` 变新」判定。
 */
export function readCheckIdFromAck(ack: unknown): string | null {
  if (ack === null || typeof ack !== 'object') return null;
  const value = (ack as { request_id?: unknown }).request_id;
  return typeof value === 'string' && IMAGE_REQUEST_ID_RE.test(value) ? value : null;
}

export interface ImageUpdateCatalogCheckState {
  checkId: string | null;
  state: string | null;
}

/** 读出 `details.catalog.check`（旧更新器没有这个键 → null）。 */
export function readCatalogCheck(view: ImageUpdateV2 | null): ImageUpdateCatalogCheckState | null {
  const check = view?.details?.catalog?.check;
  if (check === null || check === undefined) return null;
  return {
    checkId: typeof check.check_id === 'string' ? check.check_id : null,
    state: typeof check.state === 'string' ? check.state : null,
  };
}

/**
 * 一次真正的按需检查：写请求 → 每 2 秒重读 GET →
 * `details.catalog.check.check_id` 匹配并到终态、或 `checked_at` 变新，才算完成。
 *
 * - POST 失败：`failed`，**保留**上次结论（`view` 原样回传），只加一行「这次检查没成功」；
 * - 等到 20 秒还没变新：`timed_out`，同样保留上次结论，文案说清更新器没有回应；
 * - 10 秒内再次调用：`skipped`，一个请求都不发；
 * - 检查本身以 `failed` 收尾（更新器写了终态）：把新结论交回去（`view`），
 *   同时保留一行「这次检查没成功」——不能把「检查失败」当成「没检查」。
 */
export async function runImageUpdateCheck(
  attempt: ImageCheckAttempt,
  previous: ImageUpdateV2 | null,
  deps: ImageCheckDeps,
): Promise<ImageCheckResult> {
  const now = deps.now ?? (() => Date.now());
  const sleep = deps.sleep ?? defaultSleep;
  const newCheckId = deps.newCheckId ?? newImageUpdateRequestId;
  const timeoutMs = deps.timeoutMs ?? IMAGE_CHECK_TIMEOUT_MS;
  const pollMs = deps.pollMs ?? IMAGE_CHECK_POLL_MS;

  if (!canStartImageCheck(attempt, now())) {
    return {
      status: 'skipped',
      attempt,
      view: previous,
      notice: imageCheckNotice(attempt),
    };
  }

  const startedAtMs = now();
  const checking: ImageCheckAttempt = {
    status: 'checking',
    startedAtMs,
    finishedAtMs: null,
    notice: null,
  };
  let checkId: string;
  try {
    checkId = newCheckId();
  } catch {
    const failed: ImageCheckAttempt = {
      ...checking,
      status: 'failed',
      finishedAtMs: now(),
      notice: IMAGE_CHECK_FAILED_TEXT,
    };
    return { status: 'failed', attempt: failed, view: previous, notice: imageCheckNotice(failed) };
  }
  let ackCheckId: string | null = null;
  try {
    ackCheckId = readCheckIdFromAck(await deps.postCheck(checkId));
  } catch {
    const failed: ImageCheckAttempt = {
      ...checking,
      status: 'failed',
      finishedAtMs: now(),
      notice: IMAGE_CHECK_FAILED_TEXT,
    };
    return { status: 'failed', attempt: failed, view: previous, notice: imageCheckNotice(failed) };
  }

  const baseline = previous?.checked_at ?? null;
  const deadline = startedAtMs + timeoutMs;
  // 注入的 sleep/now 不推进时钟时也不能死循环。
  const maxPolls = Math.max(1, Math.ceil(timeoutMs / Math.max(pollMs, 1)) + 2);
  for (let poll = 0; poll < maxPolls; poll += 1) {
    await sleep(pollMs);
    let view: ImageUpdateV2 | null = null;
    try {
      view = await deps.readView();
    } catch {
      view = null;
    }
    if (view !== null) {
      const catalogCheck = readCatalogCheck(view);
      const matchesCheck =
        ackCheckId !== null &&
        catalogCheck?.checkId === ackCheckId &&
        catalogCheck.state !== null &&
        catalogCheck.state !== 'running';
      const checkedFresh = isNewCheckTime(view.checked_at, baseline);
      if (matchesCheck || checkedFresh) {
        const checkFailed = matchesCheck && catalogCheck?.state === 'failed';
        const done: ImageCheckAttempt = {
          status: checkFailed ? 'failed' : 'done',
          startedAtMs,
          finishedAtMs: now(),
          notice: checkFailed ? IMAGE_CHECK_FAILED_TEXT : null,
        };
        return {
          status: checkFailed ? 'failed' : 'ok',
          attempt: done,
          view,
          notice: checkFailed ? imageCheckNotice(done) : null,
        };
      }
    }
    if (now() >= deadline) break;
  }

  const timedOut: ImageCheckAttempt = {
    status: 'timed_out',
    startedAtMs,
    finishedAtMs: now(),
    notice: IMAGE_CHECK_TIMEOUT_TEXT,
  };
  return {
    status: 'timed_out',
    attempt: timedOut,
    view: previous,
    notice: imageCheckNotice(timedOut),
  };
}

/* ------------------------------------------------------------ 任务 */

export type ImageTaskActivity = 'none' | 'running' | 'queued';

/**
 * 只有进行中/排队中的任务覆盖结论展示；旧任务不能压住新结论。
 *
 * 活动任务来自 `task.state === 'running'`；排队既可能是更新器还没认领的
 * `request`（后端保留的旧字段），也可能是任务状态直接写 queued/pending。
 */
export function imageTaskActivity(view: ImageUpdateV2 | null): ImageTaskActivity {
  const task = view?.task ?? null;
  const state = (task?.state ?? '').toLowerCase();
  if (state === 'running') return 'running';
  if (state === 'queued' || state === 'pending') return 'queued';
  if (view?.request != null) return 'queued';
  return 'none';
}

/** 首屏任务行：`正在更新 v0.0.4：下载并预检目标镜像` / 排队一句话。 */
export function describeImageTaskLine(view: ImageUpdateV2 | null): string | null {
  const activity = imageTaskActivity(view);
  if (activity === 'queued') return '更新请求已提交，等待开始';
  if (activity !== 'running') return null;
  const task = view?.task ?? null;
  const version = imageVersionLabel(task?.version ?? null);
  const stage = task?.stage ? imageUpdateStageLabel(task.stage) : '准备中';
  return version === null ? `正在更新：${stage}` : `正在更新 ${version}：${stage}`;
}

/** 旧任务收成一行（失败/需要处理/已完成），不占首屏主动作。 */
export function describeImageLastTaskLine(view: ImageUpdateV2 | null): string | null {
  const task = view?.task ?? null;
  if (task === null || imageTaskActivity(view) !== 'none') return null;
  if (task.state === 'needs_manual') return '上次更新需要处理：先按详情里的失败码处理，再检查。';
  if (task.state === 'succeeded') {
    // 不写「更新成功」：三项核验有任何一端缺失/不一致，就只能说「已结束」。
    const checks = imageUpdateVerification(task, view?.current ?? null);
    if (checks.some((check) => check.ok === false)) return '上次更新已结束，但三项核验不一致。';
    if (checks.some((check) => check.ok === null)) {
      return '上次更新已结束，但三项核验值不全，不能确认成功。';
    }
    return '上次更新已完成（三项核验一致）。';
  }
  if (task.state === 'failed_not_started' || task.failed_code !== null) {
    return `上次更新失败：${task.failure?.message ?? imageUpdateFailureText(task.failed_code)}`;
  }
  return null;
}

/* ------------------------------------------------------------ 三项核验 */

export interface ImageUpdateCheckValue {
  id: 'container_digest' | 'app_commit' | 'migration_head';
  label: string;
  /** 任务记录里的期望值（后端白名单投影，可能缺失）。 */
  expected: string | null;
  /** 现在能实测到的值：当前 API 的 digest/commit，或迁移后的实际 head。 */
  actual: string | null;
  /** null = 有一端缺失，无法确认（既不算一致，也不算不一致）。 */
  ok: boolean | null;
}

function verificationValue(
  id: ImageUpdateCheckValue['id'],
  label: string,
  expected: string | null,
  actual: string | null,
): ImageUpdateCheckValue {
  return {
    id,
    label,
    expected,
    actual,
    ok: expected === null || actual === null ? null : expected === actual,
  };
}

/**
 * 成功判定只认三项核验（设计 §任务记录）：运行容器 digest、应用提交、迁移 head。
 * 任一端缺失就返回 `ok: null`——不拿「退出码 0」或「页面又能打开了」当成功。
 * 卡片本身**不声称**「更新成功」，这三项只作为详情里的诊断事实。
 */
export function imageUpdateVerification(
  task: ImageUpdateTaskV2 | null,
  current: ImageUpdateCurrentV2 | null,
): ImageUpdateCheckValue[] {
  return [
    verificationValue(
      'container_digest',
      '运行容器 digest',
      task?.target?.digest ?? null,
      current?.digest ?? null,
    ),
    verificationValue(
      'app_commit',
      '应用提交',
      task?.target?.commit ?? null,
      current?.commit ?? null,
    ),
    verificationValue(
      'migration_head',
      '迁移 head',
      task?.migration?.target_head ?? null,
      task?.migration?.current_after ?? null,
    ),
  ];
}

/* ------------------------------------------------------------ 主按钮 */

export type ImageUpdateActionKind =
  | 'check'
  | 'update'
  | 'recheck'
  | 'retry'
  | 'details'
  | 'progress';

export interface ImageUpdateAction {
  kind: ImageUpdateActionKind;
  label: string;
  disabled: boolean;
}

/**
 * 卡片唯一的主按钮。四态各一个；活动任务/非 owner 优先覆盖。
 * `busy` 时按钮禁用并显示「正在检查…」。
 */
export function imageUpdatePrimaryAction(
  view: ImageUpdateV2 | null,
  options: { canWrite: boolean; busy?: boolean },
): ImageUpdateAction {
  const busy = options.busy === true;
  if (imageTaskActivity(view) !== 'none') {
    return { kind: 'progress', label: '查看进度', disabled: false };
  }
  if (!options.canWrite) {
    return { kind: 'details', label: '查看详情', disabled: false };
  }
  const checkingLabel = busy ? '正在检查…' : null;
  const state = view?.state ?? 'unknown';
  switch (state) {
    case 'available': {
      const version = imageVersionLabel(view?.target?.version ?? null);
      if (version === null) {
        return {
          kind: 'recheck',
          label: checkingLabel ?? '重新检查',
          disabled: busy,
        };
      }
      return {
        kind: 'update',
        label: checkingLabel ?? `更新到 ${version}`,
        disabled: busy,
      };
    }
    case 'latest':
      return { kind: 'check', label: checkingLabel ?? '检查更新', disabled: busy };
    case 'newer_unavailable':
      return { kind: 'recheck', label: checkingLabel ?? '重新检查', disabled: busy };
    default:
      return { kind: 'retry', label: checkingLabel ?? '重试检查', disabled: busy };
  }
}

/**
 * 点「更新到 vX」后的复查结论：目标版本与摘要都没变，才进入现有风险确认。
 * 版本、摘要、状态任何一项变化都停在这里（由页面显示新结论）。
 */
export function imageUpdateConfirmAfterCheck(
  before: ImageUpdateTargetV2 | null,
  after: ImageUpdateV2 | null,
): boolean {
  if (before === null || after === null) return false;
  if (after.state !== 'available' || after.target === null) return false;
  return after.target.version === before.version && after.target.digest === before.digest;
}

/* ------------------------------------------------------------ 更新内容 */

export function isImageUpdateNotesVisible(view: ImageUpdateV2 | null): boolean {
  const state = view?.state;
  return state === 'available' || state === 'newer_unavailable';
}

function versionParts(version: string): number[] | null {
  const match = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(version.trim());
  if (match === null) return null;
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

/** 严格 x.y.z 比较；任一侧解析不了就返回 0（不猜）。 */
function compareImageVersion(left: string, right: string): number {
  const a = versionParts(left);
  const b = versionParts(right);
  if (a === null || b === null) return 0;
  for (let index = 0; index < 3; index += 1) {
    const x = a[index] ?? 0;
    const y = b[index] ?? 0;
    if (x !== y) return x - y;
  }
  return 0;
}

/**
 * 「更新内容」讲哪一版：
 *
 * 1. 有推荐目标 → 只讲目标那一版（目录里没有对应条目就不显示内容，不拿别的新版本
 *    的说明冒充「这次要装的版本」）；
 * 2. 有新版暂不可用（没有目标）→ 与后端 `_blocked_reason` 同一口径：先看
 *    「原因只是摘要没解析（`digest_unresolved`）或没有原因」的那一档，取**最早**
 *    （最低）一版；这一档为空才取最高新版本——首屏那句话说的是哪一版，内容就
 *    说哪一版，前端不另造版本关系。
 */
export function pickImageUpdateNotesRelease(
  view: ImageUpdateV2 | null,
): ImageUpdateNewerReleaseV2 | null {
  const newer = view?.newer ?? [];
  if (newer.length === 0) return null;
  const targetVersion = view?.target?.version ?? null;
  if (targetVersion !== null) {
    return newer.find((entry) => entry.version === targetVersion) ?? null;
  }
  const direct = newer.filter(
    (entry) => entry.incompatible_reason === null || entry.incompatible_reason === 'digest_unresolved',
  );
  const pool = direct.length > 0 ? direct : newer;
  let chosen: ImageUpdateNewerReleaseV2 | null = null;
  for (const entry of pool) {
    if (chosen === null) {
      chosen = entry;
      continue;
    }
    const comparison = compareImageVersion(entry.version, chosen.version);
    if (direct.length > 0 ? comparison < 0 : comparison > 0) chosen = entry;
  }
  return chosen;
}

export interface ImageUpdateNotesView {
  version: string | null;
  summary: string | null;
  items: string[];
  url: string | null;
}

/** 只放行 http(s) 链接；后端给别的 scheme 就不显示「完整更新日志」。 */
export function isSafeImageUpdateUrl(url: string | null | undefined): url is string {
  if (typeof url !== 'string' || url.trim() === '') return false;
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:';
  } catch {
    return false;
  }
}

/**
 * 更新内容（只在可更新 / 有新版暂不可用两态返回）。
 *
 * `notes` 是发布方原话：不过滤、不改写、不排序；空数组交给页面显示
 * 「这一版没有提供更新说明」。`summary` 只在后端真的给了才显示。
 */
export function imageUpdateNotesView(view: ImageUpdateV2 | null): ImageUpdateNotesView | null {
  if (view === null || !isImageUpdateNotesVisible(view)) return null;
  const release = pickImageUpdateNotesRelease(view);
  const rawNotes = release?.notes ?? [];
  const items = Array.isArray(rawNotes)
    ? rawNotes.filter((note): note is string => typeof note === 'string' && note.trim() !== '')
    : [];
  const summary =
    typeof release?.summary === 'string' && release.summary.trim() !== ''
      ? release.summary.trim()
      : null;
  return {
    version: release?.version ?? view.target?.version ?? null,
    summary,
    items,
    url: isSafeImageUpdateUrl(release?.notes_url) ? release.notes_url : null,
  };
}

/* ------------------------------------------------------ 轮询 / 断线重连 */

export type ImageUpdateConnection = 'ok' | 'waiting';

export interface ImageUpdatePanelState {
  view: ImageUpdateV2 | null;
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
  incoming: ImageUpdateV2 | null,
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
  const hint =
    state.failures >= 3
      ? ' 连续多次读不到状态：确认后端进程与网络可用，或稍后重试。'
      : '';
  return { title: '正在重新连接…', detail: `${base}${hint}` };
}

/** 下一次轮询的等待时间：断线退避；任务进行中/排队 3 秒，其余 15 秒。 */
export function imagePollDelayMs(state: ImageUpdatePanelState): number {
  if (state.connection === 'waiting') {
    const index = Math.min(Math.max(state.failures - 1, 0), RECOVERY_DELAYS_MS.length - 1);
    return RECOVERY_DELAYS_MS[index] ?? IMAGE_POLL_ACTIVE_MS;
  }
  if (state.view === null) return IMAGE_POLL_ACTIVE_MS;
  return imageTaskActivity(state.view) === 'none' ? IMAGE_POLL_IDLE_MS : IMAGE_POLL_ACTIVE_MS;
}

/* -------------------------------------------------------------- 提交请求体 */

/**
 * POST 的**固定 5 键**请求体（多一个键后端就 422）。
 *
 * 目标只取 `view.target.digest`（可信目录 + 摘要已解析）；`task`/请求记录都不回填。
 * 拿不到目标/当前摘要、目标格式不对、或目标与当前相同，一律返回 null。
 */
export function buildImageUpdateSubmission(
  view: ImageUpdateV2 | null,
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
    return {
      reason: error.message || '提交被拒绝',
      hint: '已刷新当前状态；按页面给出的原因与下一步处理。',
      refresh: true,
    };
  }
  return { reason: '提交失败', hint: '刷新后按状态说明处理。', refresh: true };
}

/* ------------------------------------------------------------------ 面板 */

export interface ImageUpdatePanelProps {
  state: ImageUpdatePanelState;
  /** owner 才能写检查/安装请求；false 时主按钮是「查看详情」，一个请求都不发。 */
  canWrite: boolean;
  /** 正在等一次按需检查的结果（按钮 loading + 「正在检查…」）。 */
  checking: boolean;
  /** 失败/超时的一行人话；成功为 null。 */
  checkNotice: string | null;
  confirming: boolean;
  riskAcknowledged: boolean;
  submitting: boolean;
  submitError: ImageUpdateSubmitError | null;
  /** 渲染用的“现在”（测试注入，避免依赖真实时钟）。 */
  nowMs?: number;
  /** 测试/嵌入用：详情区默认展开。 */
  defaultDetailsOpen?: boolean;
  onCheck: () => void;
  onStartConfirm: () => void;
  onCancelConfirm: () => void;
  onToggleRisk: () => void;
  onConfirm: () => void;
}

/**
 * 纯展示组件：全部输入来自 props，没有请求、没有定时器，便于用
 * `renderToStaticMarkup` 断言首屏只有「当前版本 / 一句话结论 / 检查于 / 一个主按钮」。
 */
export function ImageUpdatePanel(props: ImageUpdatePanelProps) {
  const { state, canWrite, checking, checkNotice, confirming, riskAcknowledged, submitting, submitError } =
    props;
  const [detailsOpen, setDetailsOpen] = useState(props.defaultDetailsOpen === true);
  const [taskOpen, setTaskOpen] = useState(false);
  const [notesExpanded, setNotesExpanded] = useState(false);

  const view = state.view;
  const nowMs = props.nowMs ?? Date.now();
  const current = view?.current ?? { version: null, commit: null, digest: null };
  const task = view?.task ?? null;
  const target = view?.target ?? null;
  const newer = view?.newer ?? [];
  const catalog = view?.details?.catalog ?? {};
  const updater = view?.details?.updater ?? {};
  const backupGate = view?.details?.backup_gate ?? null;

  const activity = imageTaskActivity(view);
  const action = imageUpdatePrimaryAction(view, { canWrite, busy: checking });
  const connection = describeImageConnection(state);
  const taskLine = describeImageTaskLine(view);
  const lastTaskLine = describeImageLastTaskLine(view);
  const notes = imageUpdateNotesView(view);
  const notesItems =
    notes === null
      ? []
      : notesExpanded
        ? notes.items
        : notes.items.slice(0, IMAGE_NOTES_COLLAPSE_LIMIT);
  const hiddenNotes = notes === null ? 0 : notes.items.length - notesItems.length;
  const message = typeof view?.message === 'string' ? view.message.trim() : '';
  // 活动任务覆盖结论展示（服务端也会把 message 换成进度；这里用同一份结构化事实，
  // 保证文案固定为「正在更新 vX：阶段」/「更新请求已提交，等待开始」）。
  const conclusion =
    activity !== 'none' && taskLine !== null
      ? taskLine
      : view === null
        ? '正在读取更新状态…'
        : message !== ''
          ? message
          : '服务端没有给出结论';
  const versionLabel = imageVersionLabel(current.version);

  const onPrimary = () => {
    switch (action.kind) {
      case 'progress':
        setTaskOpen((open) => !open);
        return;
      case 'details':
        setDetailsOpen((open) => !open);
        return;
      case 'update':
        props.onStartConfirm();
        return;
      default:
        props.onCheck();
    }
  };

  return (
    <section className="panel update-card" aria-label="更新" data-testid="image-update-panel">
      <p className="update-card__version" data-testid="image-update-current">
        {versionLabel === null ? '当前版本未知' : `当前 ${versionLabel}`}
      </p>
      <p className="update-card__conclusion" data-testid="image-update-conclusion">
        {conclusion}
      </p>
      <p className="update-card__checked" data-testid="image-update-checked">
        {describeImageCheckedAt(view?.checked_at ?? null, nowMs)}
      </p>

      {lastTaskLine !== null ? (
        <p className="update-card__last-task" data-testid="image-update-last-task">
          {lastTaskLine}
        </p>
      ) : null}

      {connection !== null ? (
        <div className="update-card__notice update-card__notice--warn" data-testid="image-update-reconnect">
          <strong>{connection.title}</strong>
          <span>{connection.detail}</span>
        </div>
      ) : null}

      {checkNotice !== null ? (
        <p className="update-card__notice update-card__notice--warn" data-testid="image-update-check-notice">
          {checkNotice}
        </p>
      ) : null}

      <div className="update-card__actions">
        <button
          type="button"
          className="button button--primary update-card__primary"
          data-testid="image-update-primary"
          data-action={action.kind}
          aria-expanded={
            action.kind === 'progress' ? taskOpen : action.kind === 'details' ? detailsOpen : undefined
          }
          onClick={onPrimary}
          disabled={action.disabled || submitting}
        >
          {action.label}
        </button>
        {canWrite || activity !== 'none' ? (
          <button
            type="button"
            className="ghost-button"
            data-testid="image-update-details-toggle"
            aria-expanded={detailsOpen}
            onClick={() => setDetailsOpen((open) => !open)}
          >
            详情/诊断 {detailsOpen ? '▴' : '▾'}
          </button>
        ) : null}
      </div>

      {notes !== null ? (
        <section className="update-notes" data-testid="image-update-notes">
          <h3 className="update-notes__title">
            更新内容
            {imageVersionLabel(notes.version) === null
              ? ''
              : `（${imageVersionLabel(notes.version) ?? ''}）`}
          </h3>
          {notes.summary === null ? null : (
            <p className="update-notes__summary" data-testid="image-update-notes-summary">
              {notes.summary}
            </p>
          )}
          {notes.items.length === 0 ? (
            <p className="update-notes__empty" data-testid="image-update-notes-empty">
              这一版没有提供更新说明
            </p>
          ) : (
            <ul className="update-notes__list" data-testid="image-update-notes-items">
              {notesItems.map((item, index) => (
                <li key={`${index}-${item}`}>{item}</li>
              ))}
            </ul>
          )}
          {hiddenNotes > 0 ? (
            <button
              type="button"
              className="ghost-button update-notes__more"
              data-testid="image-update-notes-more"
              onClick={() => setNotesExpanded(true)}
            >
              展开其余 {hiddenNotes} 条
            </button>
          ) : null}
          {notes.url === null ? null : (
            <a
              className="update-notes__link"
              data-testid="image-update-notes-url"
              href={notes.url}
              target="_blank"
              rel="noreferrer noopener"
            >
              完整更新日志
            </a>
          )}
        </section>
      ) : null}

      {taskOpen && task !== null ? (
        <div className="update-task" data-testid="image-update-task-detail">
          <h3 className="update-details__title">任务进度</h3>
          <dl className="about-facts about-facts--tight">
            <dt>任务状态</dt>
            <dd>
              {task.state_message ?? imageUpdateTaskStateText(task.state)}
              {task.state ? <span className="about-facts__muted">（{task.state}）</span> : null}
            </dd>
            <dt>阶段</dt>
            <dd>{task.stage_message ?? imageUpdateStageLabel(task.stage)}</dd>
            <dt>目标版本</dt>
            <dd>
              {imageVersionLabel(task.version) ?? (
                <span className="about-facts__muted">目标版本未记录</span>
              )}
            </dd>
            <dt>失败码</dt>
            <dd>
              {task.failed_code === null ? (
                <span className="about-facts__muted">没有失败码</span>
              ) : (
                <>
                  <code>{task.failed_code}</code>{' '}
                  {task.failure?.message ?? imageUpdateFailureText(task.failed_code)}
                </>
              )}
            </dd>
            <dt>三项核验</dt>
            <dd>
              <ul className="update-details__list">
                {imageUpdateVerification(task, current).map((check) => (
                  <li className="update-details__item" key={check.id}>
                    {check.label}：
                    <code title={check.expected ?? undefined}>{shortRef(check.expected)}</code>{' '}
                    对照 <code title={check.actual ?? undefined}>{shortRef(check.actual)}</code>{' '}
                    <span className="about-facts__muted">
                      {check.ok === null ? '缺失，无法确认' : check.ok ? '一致' : '不一致'}
                    </span>
                  </li>
                ))}
              </ul>
            </dd>
          </dl>
        </div>
      ) : null}

      {detailsOpen ? (
        <div className="update-details" data-testid="image-update-details">
          <h3 className="update-details__title">详情/诊断</h3>
          <dl className="about-facts about-facts--tight">
            <dt>当前版本</dt>
            <dd data-testid="image-update-detail-version">
              {current.version === null ? (
                <span className="about-facts__muted">未知</span>
              ) : (
                <code>{current.version}</code>
              )}
            </dd>
            <dt>当前 commit</dt>
            <dd data-testid="image-update-detail-commit">
              <code>{shortRef(current.commit)}</code>
            </dd>
            <dt>当前 digest</dt>
            <dd data-testid="image-update-detail-digest">
              <code title={current.digest ?? undefined}>{shortDigestRef(current.digest)}</code>
            </dd>
            <dt>目录来源</dt>
            <dd data-testid="image-update-detail-catalog-source">
              {imageCatalogSourceText(catalog.source)}
            </dd>
            <dt>目录检查时间</dt>
            <dd data-testid="image-update-detail-catalog-time">
              {formatImageCheckTime(catalog.checked_at) ?? '未知'}
            </dd>
            <dt>目录状态</dt>
            <dd data-testid="image-update-detail-catalog-state">
              {imageCatalogStateText(catalog.state)}
              {catalog.state_message ? (
                <span className="about-facts__muted">：{catalog.state_message}</span>
              ) : null}
            </dd>
            <dt>目录原因</dt>
            <dd data-testid="image-update-detail-catalog-reason">
              {catalog.reason ? (
                <>
                  <code>{catalog.reason}</code>{' '}
                  {catalog.reason_message ?? imageUpdateReasonText(catalog.reason)}
                </>
              ) : (
                <span className="about-facts__muted">没有原因码</span>
              )}
            </dd>
            <dt>更新器</dt>
            <dd data-testid="image-update-detail-updater">
              {updater.online === true ? '在线' : updater.online === false ? '离线' : '未知'}
              {`（${imageUpdaterStateLabel(updater.state ?? null)}）`}
              {updater.protocol === null || updater.protocol === undefined ? null : (
                <span className="about-facts__muted"> · 协议 v{updater.protocol}</span>
              )}
            </dd>
            <dt>恢复点</dt>
            <dd data-testid="image-update-detail-backup">
              {imageBackupGateText(backupGate)}
              {backupGate?.reason ? (
                <>
                  {' '}
                  <code>{backupGate.reason}</code>{' '}
                  {backupGate.reason_message ?? imageUpdateReasonText(backupGate.reason)}
                </>
              ) : null}
              <span className="about-facts__muted">
                {' '}
                · 检查于 {formatImageCheckTime(backupGate?.checked_at) ?? '未知'}
              </span>
            </dd>
            <dt>新版列表</dt>
            <dd data-testid="image-update-detail-newer">
              {newer.length === 0 ? (
                <span className="about-facts__muted">目录里没有更高的版本</span>
              ) : (
                <ul className="update-details__list">
                  {newer.map((entry) => (
                    <li
                      className="update-details__item"
                      key={`${entry.version}:${entry.commit ?? 'unknown'}`}
                    >
                      <code>{entry.version}</code>{' '}
                      {/* 目标版本的构建提交通常拿不到（registry 发现）；没有就整截不渲染。 */}
                      {entry.commit ? (
                        <span className="about-facts__muted">（{shortSha(entry.commit)}）</span>
                      ) : null}
                      {entry.compatible ? (
                        <span className="about-facts__muted">可直接安装</span>
                      ) : entry.incompatible_reason === null ? (
                        <span className="about-facts__muted">原因未提供</span>
                      ) : (
                        <>
                          <code>{entry.incompatible_reason}</code>{' '}
                          <span className="about-facts__muted">
                            {entry.incompatible_reason_message ??
                              imageUpdateReasonText(entry.incompatible_reason)}
                          </span>
                        </>
                      )}
                      {/* has_migration 已废弃且当前恒为 null：只说明判定归属，不推断有无迁移。 */}
                      {entry.has_migration === null || entry.has_migration === undefined ? (
                        <>
                          {' '}
                          <span className="about-facts__muted">迁移由迁移器判断</span>
                        </>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </dd>
            <dt>推荐目标</dt>
            <dd data-testid="image-update-detail-target">
              {target === null ? (
                <span className="about-facts__muted">没有可提交的目标</span>
              ) : (
                <>
                  <code>{imageVersionLabel(target.version) ?? target.version}</code>{' '}
                  <code title={target.digest}>{shortDigestRef(target.digest)}</code>
                  {/* registry 发现拿不到目标版本的构建提交是常态：没有就不显示这截。 */}
                  {target.commit ? (
                    <span className="about-facts__muted">提交 {shortRef(target.commit)}</span>
                  ) : null}
                </>
              )}
            </dd>
            <dt>当前请求 ID</dt>
            <dd data-testid="image-update-detail-request-id">
              {task?.request_id ? (
                <code>{task.request_id}</code>
              ) : view?.request?.request_id ? (
                <code>{view.request.request_id}</code>
              ) : (
                <span className="about-facts__muted">没有进行中的请求</span>
              )}
            </dd>
            <dt>最近任务</dt>
            <dd data-testid="image-update-detail-task">
              {task === null ? (
                <span className="about-facts__muted">没有任务记录</span>
              ) : (
                <>
                  {task.state_message ?? imageUpdateTaskStateText(task.state)}
                  {task.state ? <span className="about-facts__muted">（{task.state}）</span> : null}
                  {` · 阶段 ${imageUpdateStageLabel(task.stage)}`}
                  {task.failed_code === null ? null : (
                    <>
                      {' · '}
                      <code>{task.failed_code}</code>{' '}
                      {task.failure?.message ?? imageUpdateFailureText(task.failed_code)}
                    </>
                  )}
                </>
              )}
            </dd>
            {task === null ? null : (
              <>
                <dt>三项核验</dt>
                <dd data-testid="image-update-detail-verification">
                  <ul className="update-details__list">
                    {imageUpdateVerification(task, current).map((check) => (
                      <li className="update-details__item" key={check.id}>
                        {check.label}：
                        <code title={check.expected ?? undefined}>
                          {shortRef(check.expected)}
                        </code>{' '}
                        对照{' '}
                        <code title={check.actual ?? undefined}>{shortRef(check.actual)}</code>{' '}
                        <span className="about-facts__muted">
                          {check.ok === null ? '缺失，无法确认' : check.ok ? '一致' : '不一致'}
                        </span>
                      </li>
                    ))}
                  </ul>
                </dd>
              </>
            )}
          </dl>
        </div>
      ) : null}

      {submitError !== null ? (
        <div className="banner banner--error" data-testid="image-update-submit-error">
          <AlertIcon />
          <div>
            <strong>{submitError.reason}</strong>
            {submitError.hint !== '' ? <div className="banner__detail">{submitError.hint}</div> : null}
          </div>
        </div>
      ) : null}

      {confirming ? (
        <div className="update-confirm" data-testid="image-update-confirm">
          <p className="update-confirm__lead">
            确认更新到{' '}
            {target === null ? '目标版本' : (imageVersionLabel(target.version) ?? '目标版本')}
          </p>
          <dl className="about-facts">
            <dt>当前</dt>
            <dd>
              <code>{imageVersionLabel(current.version) ?? '未知'}</code>{' '}
              <span className="about-facts__muted">{shortDigestRef(current.digest)}</span>
            </dd>
            <dt>目标</dt>
            <dd>
              <code title={target?.digest ?? undefined}>
                {target === null ? '未知' : shortDigestRef(target.digest)}
              </code>
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
            <li>
              是否成功只以运行容器 digest、应用提交与迁移 head 三项核验为准，不以命令退出码或页面重连为准。
            </li>
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
              disabled={!riskAcknowledged || submitting || !canWrite || target === null}
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
  initialView?: ImageUpdateV2 | null;
  /**
   * 显式给出写权限（测试/嵌入用）。不传时读 `GET /v1/auth/me`，只有 `role=owner`
   * 才能写检查/安装请求；读不到按只读处理（fail closed）。
   */
  initialCanWrite?: boolean | null;
}

export function ImageUpdatePage({
  onBack,
  initialView = null,
  initialCanWrite = null,
}: ImageUpdatePageProps) {
  const [state, setState] = useState<ImageUpdatePanelState>(() =>
    initialView === null
      ? initialImageUpdateState()
      : applyImageUpdatePoll(initialImageUpdateState(), initialView),
  );
  const [canWrite, setCanWrite] = useState(initialCanWrite === true);
  const [confirming, setConfirming] = useState(false);
  const [riskAcknowledged, setRiskAcknowledged] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<ImageUpdateSubmitError | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkNotice, setCheckNotice] = useState<string | null>(null);
  const [pollNonce, setPollNonce] = useState(0);

  const aliveRef = useRef(true);
  const stateRef = useRef(state);
  const submittingRef = useRef(false);
  const checkingRef = useRef(false);
  const attemptRef = useRef<ImageCheckAttempt>(initialImageCheckAttempt());

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
    if (initialCanWrite !== null) return;
    let alive = true;
    void (async () => {
      try {
        const me = await api.me();
        if (alive) setCanWrite(me.user.role === 'owner');
      } catch {
        if (alive) setCanWrite(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [initialCanWrite]);

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
      let incoming: ImageUpdateV2 | null = null;
      let failed = false;
      try {
        incoming = await api.imageUpdate(controller.signal);
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

  const mergeView = useCallback((incoming: ImageUpdateV2) => {
    const next = applyImageUpdatePoll(stateRef.current, incoming);
    stateRef.current = next;
    setState(next);
  }, []);

  const runCheck = useCallback(async (): Promise<ImageCheckResult> => {
    if (!canWrite) {
      // 非 owner 一个写请求都不发（主按钮此时是「查看详情」，这里只是兜底）。
      return {
        status: 'skipped',
        attempt: attemptRef.current,
        view: stateRef.current.view,
        notice: imageCheckNotice(attemptRef.current),
      };
    }
    if (checkingRef.current) {
      return {
        status: 'skipped',
        attempt: attemptRef.current,
        view: stateRef.current.view,
        notice: imageCheckNotice(attemptRef.current),
      };
    }
    const nowMs = Date.now();
    if (!canStartImageCheck(attemptRef.current, nowMs)) {
      return {
        status: 'skipped',
        attempt: attemptRef.current,
        view: stateRef.current.view,
        notice: imageCheckNotice(attemptRef.current),
      };
    }
    checkingRef.current = true;
    setChecking(true);
    setCheckNotice(null);
    const baselineView = stateRef.current.view;
    const result = await runImageUpdateCheck(attemptRef.current, baselineView, {
      postCheck: (checkId) => api.imageUpdateCheck(checkId),
      readView: () => api.imageUpdate(),
    });
    attemptRef.current = result.attempt;
    // 只有真的读到新视图才合并：POST 失败/超时要保留上次结论。
    if (result.view !== null && result.view !== baselineView) mergeView(result.view);
    setCheckNotice(result.notice);
    checkingRef.current = false;
    setChecking(false);
    return result;
  }, [canWrite, mergeView]);

  const beginUpdate = useCallback(async () => {
    if (!canWrite) return;
    const before = stateRef.current.view;
    if (before === null || before.state !== 'available' || before.target === null) return;
    const lastFinished = attemptRef.current.finishedAtMs;
    if (
      attemptRef.current.status === 'done' &&
      lastFinished !== null &&
      Date.now() - lastFinished < IMAGE_UPDATE_REUSE_CHECK_MS
    ) {
      setSubmitError(null);
      setRiskAcknowledged(false);
      setConfirming(true);
      return;
    }
    const result = await runCheck();
    if (result.status !== 'ok') return;
    if (!imageUpdateConfirmAfterCheck(before.target, result.view)) return;
    setSubmitError(null);
    setRiskAcknowledged(false);
    setConfirming(true);
  }, [canWrite, runCheck]);

  const submit = useCallback(async () => {
    if (submittingRef.current || !canWrite) return;
    const snapshot = stateRef.current.view;
    const targetDigest = snapshot?.target?.digest ?? null;
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
    const body = buildImageUpdateSubmission(snapshot, targetDigest, requestId);
    if (body === null) {
      setSubmitError({
        reason: '还没有可提交的目标',
        hint: '先检查更新，确认目标摘要与当前摘要后再提交。',
        refresh: true,
      });
      setConfirming(false);
      setRiskAcknowledged(false);
      return;
    }
    submittingRef.current = true;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const ack = await api.requestImageUpdate(body);
      const next = withSubmittedRequest(stateRef.current, ack.request_id);
      stateRef.current = next;
      setState(next);
      setConfirming(false);
      setRiskAcknowledged(false);
      setPollNonce((value) => value + 1);
    } catch (error) {
      setSubmitError(explainImageSubmitError(error));
      setConfirming(false);
      setRiskAcknowledged(false);
      setPollNonce((value) => value + 1);
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }, [canWrite]);

  return (
    <section className="page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回设置
      </button>

      <h1 className="page__title">关于与版本</h1>

      <ImageUpdatePanel
        state={state}
        canWrite={canWrite}
        checking={checking}
        checkNotice={checkNotice}
        confirming={confirming}
        riskAcknowledged={riskAcknowledged}
        submitting={submitting}
        submitError={submitError}
        onCheck={() => {
          void runCheck();
        }}
        onStartConfirm={() => {
          void beginUpdate();
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

type ImageModeProbe = { status: 'probing' } | { status: 'done'; imageView: ImageUpdateV2 | null };

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
      let imageView: ImageUpdateV2 | null = null;
      try {
        const view = await api.imageUpdate(controller.signal);
        // 显式 `image_mode: false` 才是源码模式；字段缺失按镜像模式处理。
        imageView = view.image_mode === false ? null : view;
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
