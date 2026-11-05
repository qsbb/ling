/**
 * 镜像模式更新面板（P5 第三批）的纯逻辑与渲染断言。
 *
 * 这里保护的是设计原文的硬约束，不是样式细节：
 * - `installable=false` / 更新器离线 / 没有恢复点 → 按钮禁用 + 展示后端码表文案；
 * - 成功只认三项核验（运行容器 digest、应用提交、迁移 head）齐全且一致；
 * - 阶段只映射真实码表，未知阶段有中文兜底（不把英文码值打到页面上）；
 * - 渲染结果里**没有百分比、没有剩余时间、没有回退按钮**；
 * - 断线不清空任务：重连后仍是同一个 request_id。
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  AboutPage,
  IMAGE_STAGE_UNKNOWN_LABEL,
  IMAGE_UPDATE_STAGES,
  ImageUpdatePage,
  ImageUpdatePanel,
  type ImageUpdatePanelProps,
  applyImageUpdatePoll,
  buildImageUpdateSubmission,
  canSubmitImageUpdate,
  describeImageConnection,
  describeImageUpdateOldVersion,
  describeImageUpdateState,
  explainImageSubmitError,
  imagePollDelayMs,
  imageRequestLanded,
  imageUpdateGate,
  imageUpdateStageIndex,
  imageUpdateStageLabel,
  imageUpdateTarget,
  imageUpdaterStateLabel,
  initialImageUpdateState,
  isValidImageRequestId,
  newImageUpdateRequestId,
  verifyOutcome,
  withSubmittedRequest,
} from './components/AboutPage';
import { ApiError } from './api';
import type {
  ImageUpdateTask,
  ImageUpdateView,
  ImageUpdateViewTarget,
  VersionInfo,
} from './types';

const REQUEST_ID = '0123456789abcdef0123456789abcdef';
const DIGEST_CURRENT = `ghcr.io/qsbb/ling-api@sha256:${'a'.repeat(64)}`;
const DIGEST_TARGET = `ghcr.io/qsbb/ling-api@sha256:${'b'.repeat(64)}`;
const COMMIT_CURRENT = 'c'.repeat(40);
const COMMIT_TARGET = 'd'.repeat(40);
const OTHER_DIGEST = `ghcr.io/qsbb/ling-api@sha256:${'e'.repeat(64)}`;

/** 可信发布目录给出的目标（后端 `view.target` 的固定四字段）。 */
const CATALOG_TARGET: ImageUpdateViewTarget = {
  version: '1.1.0',
  digest: DIGEST_TARGET,
  commit: COMMIT_TARGET,
  has_migration: true,
};

const VERSION: VersionInfo = {
  schema_version: 'v1',
  current: '0.1.0',
  commit: COMMIT_CURRENT,
  web_manifest_sha256: 'a'.repeat(64),
};

function makeTask(overrides: Partial<ImageUpdateTask> = {}): ImageUpdateTask {
  return {
    request_id: REQUEST_ID,
    state: 'running',
    state_message: '更新进行中',
    stage: 'prepare',
    stage_message: '正在下载并预检目标镜像',
    started_at: '2026-11-04T00:00:00Z',
    updated_at: '2026-11-04T00:01:00Z',
    finished_at: null,
    target: { digest: DIGEST_TARGET, commit: COMMIT_TARGET },
    previous: { digest: DIGEST_CURRENT, commit: COMMIT_CURRENT },
    migration: { attempted: false, current_before: null, current_after: null, target_head: 'head-2' },
    backup: {
      id: 'backup-1',
      complete: true,
      tombstones_state: 'ok',
      media_state: 'present',
      keys_root: 'present',
      keys_env_set: 'true',
    },
    maintenance: { state: 'active', verified: false },
    rollback: { attempted: false, verified: false },
    still_on_old_version: null,
    failure: null,
    warnings: [],
    next_actions: [],
    ...overrides,
  };
}

function makeView(overrides: Partial<ImageUpdateView> = {}): ImageUpdateView {
  return {
    schema_version: 'v1',
    image_mode: true,
    enabled: true,
    installable: true,
    reason: null,
    message: null,
    next_actions: [],
    current: { digest: DIGEST_CURRENT, commit: COMMIT_CURRENT },
    updater: {
      online: true,
      state: 'idle',
      protocol: 1,
      version: '1.0.0',
      updated_at: '2026-11-04T00:00:00Z',
      age_seconds: 2,
    },
    backup_gate: {
      restore_point: true,
      complete: true,
      backup_id: 'backup-1',
      tombstones_state: 'ok',
      media_state: 'present',
      keys_root: 'present',
      keys_env_set: 'true',
      reason: 'ok',
      reason_message: '备份目录里有可用的恢复点',
      checked_at: '2026-11-04T00:00:00Z',
      source: 'backup_gate',
    },
    catalog: {
      state: 'ok',
      state_message: '已读到可信发布目录',
      reason: null,
      reason_message: null,
      entries: 1,
      current_version: '1.0.0',
    },
    available_targets: [],
    target: null,
    task: null,
    request: null,
    ...overrides,
  };
}

/** 一个「可提交」的视图：有排队请求 → 目标摘要已知。 */
function installableView(overrides: Partial<ImageUpdateView> = {}): ImageUpdateView {
  return makeView({
    request: {
      request_id: REQUEST_ID,
      readable: true,
      target_digest: DIGEST_TARGET,
      created_at: '2026-11-04T00:00:00Z',
    },
    ...overrides,
  });
}

function renderPanel(overrides: Partial<ImageUpdatePanelProps> = {}): string {
  const props: ImageUpdatePanelProps = {
    version: VERSION,
    state: {
      view: makeView(),
      connection: 'ok',
      taskRequestId: null,
      submittedRequestId: null,
      failures: 0,
    },
    confirming: false,
    riskAcknowledged: false,
    submitting: false,
    submitError: null,
    onRefresh: () => undefined,
    onStartConfirm: () => undefined,
    onCancelConfirm: () => undefined,
    onToggleRisk: () => undefined,
    onConfirm: () => undefined,
    ...overrides,
  };
  return renderToStaticMarkup(<ImageUpdatePanel {...props} />);
}

function buttonTexts(markup: string): string[] {
  const texts: string[] = [];
  const pattern = /<button\b[^>]*>([\s\S]*?)<\/button>/g;
  let match: RegExpExecArray | null = pattern.exec(markup);
  while (match !== null) {
    const inner = match[1] ?? '';
    texts.push(
      inner
        .replace(/<[^>]*>/g, ' ')
        .replace(/\s+/g, ' ')
        .trim(),
    );
    match = pattern.exec(markup);
  }
  return texts;
}

/** 面板的所有状态都要满足的硬约束。 */
function expectNoFakeProgress(markup: string): void {
  expect(markup).not.toContain('%');
  expect(markup).not.toContain('剩余时间');
  expect(markup).not.toContain('预计');
}

function expectNoRollbackButton(markup: string): void {
  for (const text of buttonTexts(markup)) {
    expect(text).not.toContain('回退');
    expect(text).not.toContain('降级');
    expect(text).not.toContain('恢复旧版本');
  }
  expect(markup).not.toMatch(/data-testid="[^"]*(rollback|downgrade)/i);
}

describe('installable=false：禁用并展示后端给定文案', () => {
  const blocked = makeView({
    installable: false,
    reason: 'update_disabled',
    message: '镜像更新通道未启用（LING_UPDATE_ENABLED 不是 1）',
    next_actions: ['由宿主把 LING_UPDATE_ENABLED 设为 1 后再试'],
  });

  it('按钮禁用', () => {
    expect(canSubmitImageUpdate(blocked)).toBe(false);
    expect(imageUpdateGate(blocked).enabled).toBe(false);
  });

  it('展示 message 与 next_actions（原样，不另编理由）', () => {
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view: blocked } });
    expect(markup).toContain('镜像更新通道未启用（LING_UPDATE_ENABLED 不是 1）');
    expect(markup).toContain('由宿主把 LING_UPDATE_ENABLED 设为 1 后再试');
    expect(markup).toContain('data-testid="image-update-start"');
    // 按钮带 disabled
    expect(markup).toMatch(/data-testid="image-update-start"[^>]*disabled=""/);
    expect(describeImageUpdateState(blocked).title).toBe('当前不能更新');
    expect(describeImageUpdateState(blocked).detail).toContain('见下方说明');
  });
});

describe('更新器离线', () => {
  it('后端说离线：禁用 + 展示后端原因与下一步', () => {
    const view = makeView({
      installable: false,
      reason: 'updater_offline',
      message: '更新器离线：状态已过期，无法确认它还能执行更新',
      next_actions: ['检查 ling-updater 容器状态与状态目录写权限，等它重新写状态'],
      updater: { ...makeView().updater, online: false, state: 'idle' },
    });
    expect(canSubmitImageUpdate(view)).toBe(false);
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('更新器离线：状态已过期，无法确认它还能执行更新');
    expect(markup).toContain('检查 ling-updater 容器状态与状态目录写权限，等它重新写状态');
    expect(markup).toContain('离线');
  });

  it('后端自相矛盾（说可安装但离线）：前端仍禁用并补一句说明', () => {
    const view = installableView({
      updater: { ...makeView().updater, online: false },
    });
    const gate = imageUpdateGate(view);
    expect(gate.enabled).toBe(false);
    expect(gate.notes.join(' ')).toContain('更新器当前不在线');
  });
});

describe('恢复点门禁', () => {
  it('restore_point=false：禁用 + 展示原因', () => {
    const view = makeView({
      installable: false,
      reason: 'backup_gate_missing',
      message: '没有可用的恢复点：备份目录里看不到完整的备份 manifest，更新前的备份/恢复门禁未就绪',
      next_actions: ['在宿主跑一次备份，确认备份目录里有完整的 manifest'],
      backup_gate: {
        ...makeView().backup_gate,
        restore_point: false,
        complete: false,
        backup_id: null,
        reason: 'no_manifest',
        reason_message: '备份目录里还没有任何 manifest（没有可用的恢复点）',
      },
    });
    expect(canSubmitImageUpdate(view)).toBe(false);
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('没有可用的恢复点');
    expect(markup).toContain('备份目录里还没有任何 manifest（没有可用的恢复点）');
    expect(markup).toContain('在宿主跑一次备份，确认备份目录里有完整的 manifest');
  });

  it('说可安装但没有恢复点：前端仍禁用', () => {
    const view = installableView({
      backup_gate: { ...makeView().backup_gate, restore_point: false, reason_message: null },
    });
    const gate = imageUpdateGate(view);
    expect(gate.enabled).toBe(false);
    expect(gate.notes.join(' ')).toContain('恢复点');
  });
});

describe('目标来源与禁用条件', () => {
  it('没有目标：显示暂无可用目标并禁用', () => {
    const view = makeView();
    expect(imageUpdateTarget(view).source).toBe('none');
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('暂无可用目标');
    expect(canSubmitImageUpdate(view)).toBe(false);
    expect(imageUpdateGate(view).notes.join(' ')).toContain('暂无可用目标');
  });

  it('排队请求给出目标，但请求已在队列里 → 不重复提交', () => {
    const view = installableView();
    expect(imageUpdateTarget(view)).toEqual({
      digest: DIGEST_TARGET,
      commit: null,
      source: 'request',
    });
    // 目标有了，但请求已经排队：后端会在 update_in_progress 上拒绝，前端先禁用。
    expect(canSubmitImageUpdate(view)).toBe(false);
    expect(imageUpdateGate(view).notes.join(' ')).toContain('已有排队中的更新请求');
  });

  it('后端给出目标摘要时（发布目录/外部来源）→ 可提交', () => {
    const view = makeView();
    expect(canSubmitImageUpdate(view)).toBe(false);
    expect(canSubmitImageUpdate(view, DIGEST_TARGET)).toBe(true);
    const gate = imageUpdateGate(view, DIGEST_TARGET);
    expect(gate.enabled).toBe(true);
    expect(gate.notes).toEqual([]);
    const markup = renderPanel({
      state: { ...initialImageUpdateState(), view },
      targetDigest: DIGEST_TARGET,
    });
    expect(markup).toContain('（外部提供）');
    expect(markup).not.toMatch(/data-testid="image-update-start"[^>]*disabled=""/);
  });

  it('目标等于当前摘要 / 已有任务进行中 / 已有排队请求：禁用', () => {
    expect(
      canSubmitImageUpdate(
        installableView({
          request: { ...installableView().request!, target_digest: DIGEST_CURRENT },
        }),
      ),
    ).toBe(false);
    expect(
      canSubmitImageUpdate(
        makeView({
          request: null,
          task: makeTask({ state: 'running' }),
        }),
      ),
    ).toBe(false);
    expect(
      canSubmitImageUpdate(
        makeView({
          installable: false,
          reason: 'update_in_progress',
          message: '已有更新请求在进行中，不能重复提交',
          request: { ...installableView().request! },
        }),
      ),
    ).toBe(false);
  });

  it('源码模式视图不走镜像通道', () => {
    const view = makeView({ image_mode: false, installable: false, message: null });
    expect(canSubmitImageUpdate(view)).toBe(false);
    expect(imageUpdateGate(view).notes.join(' ')).toContain('不是镜像模式');
  });
});

describe('可信发布目录：view.target 是目标的第一来源', () => {
  it('有 view.target：按钮可点，版本/摘要/提交/迁移都来自后端', () => {
    const view = makeView({ target: CATALOG_TARGET });
    expect(imageUpdateTarget(view)).toEqual({
      digest: DIGEST_TARGET,
      commit: COMMIT_TARGET,
      source: 'catalog',
      version: '1.1.0',
      hasMigration: true,
    });
    expect(canSubmitImageUpdate(view)).toBe(true);
    const gate = imageUpdateGate(view);
    expect(gate.enabled).toBe(true);
    expect(gate.notes).toEqual([]);
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('（可信发布目录）');
    expect(markup).toContain('1.1.0');
    expect(markup).toContain('含数据库迁移');
    expect(markup).not.toMatch(/data-testid="image-update-start"[^>]*disabled=""/);
  });

  it('没有 view.target：按钮禁用并显示暂无可用目标', () => {
    const view = makeView({ target: null });
    expect(imageUpdateTarget(view).source).toBe('none');
    expect(canSubmitImageUpdate(view)).toBe(false);
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('暂无可用目标');
    expect(markup).toMatch(/data-testid="image-update-start"[^>]*disabled=""/);
  });

  it('view.target 优先于已排队的请求（旧请求不算可选目标）', () => {
    const view = makeView({
      target: CATALOG_TARGET,
      request: {
        request_id: REQUEST_ID,
        readable: true,
        target_digest: OTHER_DIGEST,
        created_at: '2026-11-04T00:00:00Z',
      },
    });
    const target = imageUpdateTarget(view);
    expect(target.source).toBe('catalog');
    expect(target.digest).toBe(DIGEST_TARGET);
    expect(target.commit).toBe(COMMIT_TARGET);
    // 目标来自目录，但已有排队请求：仍然不能重复提交。
    expect(canSubmitImageUpdate(view)).toBe(false);
    expect(imageUpdateGate(view).notes.join(' ')).toContain('已有排队中的更新请求');
  });

  it('目标摘要等于当前摘要：禁用并说明不需要更新', () => {
    const view = makeView({
      target: { ...CATALOG_TARGET, digest: DIGEST_CURRENT },
    });
    expect(canSubmitImageUpdate(view)).toBe(false);
    expect(imageUpdateGate(view).notes.join(' ')).toContain('不需要更新');
  });

  it('后端说 no_available_target：禁用并展示码表文案', () => {
    const view = makeView({
      installable: false,
      reason: 'no_available_target',
      message: '暂无可用的更新目标（发布目录里没有适用于当前版本的新版本）',
      next_actions: ['核对发行包里的 deploy/releases.json（catalog.py validate）'],
    });
    expect(canSubmitImageUpdate(view)).toBe(false);
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('暂无可用的更新目标（发布目录里没有适用于当前版本的新版本）');
    expect(markup).toContain('核对发行包里的 deploy/releases.json');
  });

  it('目录不可用但后端仍说可安装：前端仍禁用（fail closed）', () => {
    const view = makeView({
      target: null,
      catalog: {
        state: 'missing',
        state_message: '可信发布目录缺失（发行包里没有 deploy/releases.json）',
        reason: 'catalog_missing',
        reason_message: '发行包里没有可信发布目录（deploy/releases.json），无法确定可用目标',
        entries: 0,
        current_version: null,
      },
    });
    expect(canSubmitImageUpdate(view)).toBe(false);
    expect(imageUpdateGate(view).notes.join(' ')).toContain('暂无可用目标');
  });
});

describe('still_on_old_version 三态', () => {
  it('null 显示「未能确认」，不说成「仍在旧版本」', () => {
    const text = describeImageUpdateOldVersion(makeTask({ still_on_old_version: null }));
    expect(text).toContain('未能确认');
    expect(text).not.toContain('仍在旧版本');

    const markup = renderPanel({
      state: {
        ...initialImageUpdateState(),
        view: makeView({ task: makeTask({ state: 'failed_not_started', still_on_old_version: null }) }),
      },
    });
    expect(markup).toContain('未能确认');
    expect(markup).not.toContain('仍在旧版本');
  });

  it('true/false 用实测口径', () => {
    expect(describeImageUpdateOldVersion(makeTask({ still_on_old_version: true }))).toContain(
      '仍在旧版本',
    );
    expect(describeImageUpdateOldVersion(makeTask({ still_on_old_version: false }))).toContain(
      '不在旧版本',
    );
    expect(describeImageUpdateOldVersion(null)).toContain('没有任务记录');
  });
});

describe('成功三项核验', () => {
  function succeededView(overrides: Partial<ImageUpdateTask> = {}): ImageUpdateView {
    return makeView({
      current: { digest: DIGEST_TARGET, commit: COMMIT_TARGET },
      task: makeTask({
        state: 'succeeded',
        state_message: '更新完成',
        stage: 'verify',
        stage_message: '正在验证新版本',
        migration: {
          attempted: true,
          current_before: 'head-1',
          current_after: 'head-2',
          target_head: 'head-2',
        },
        maintenance: { state: 'released', verified: true },
        still_on_old_version: false,
        ...overrides,
      }),
    });
  }

  it('三项齐全且一致 → 成功，且面板列出三项', () => {
    const view = succeededView();
    const outcome = verifyOutcome(view);
    expect(outcome.verified).toBe(true);
    expect(outcome.state).toBe('verified');
    expect(outcome.checks.map((check) => check.id)).toEqual([
      'container_digest',
      'app_commit',
      'migration_head',
    ]);
    expect(outcome.checks.every((check) => check.ok)).toBe(true);

    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('运行容器 digest');
    expect(markup).toContain('应用提交');
    expect(markup).toContain('迁移 head');
    expect(markup).toContain('三项核验通过');
    expect(markup).toContain('data-testid="image-update-check-container_digest"');
    expect(markup).toContain('data-testid="image-update-check-app_commit"');
    expect(markup).toContain('data-testid="image-update-check-migration_head"');
  });

  it('任一项缺失 → 不判成功', () => {
    const missingCommit = verifyOutcome(succeededView({ target: { digest: DIGEST_TARGET, commit: null } }));
    expect(missingCommit.verified).toBe(false);
    expect(missingCommit.state).toBe('mismatch');
    expect(missingCommit.checks.find((check) => check.id === 'app_commit')?.ok).toBe(false);

    const missingHead = verifyOutcome(
      succeededView({
        migration: { attempted: true, current_before: 'head-1', current_after: null, target_head: 'head-2' },
      }),
    );
    expect(missingHead.verified).toBe(false);
    expect(missingHead.checks.find((check) => check.id === 'migration_head')?.ok).toBe(false);

    const markup = renderPanel({
      state: { ...initialImageUpdateState(), view: succeededView({ target: { digest: DIGEST_TARGET, commit: null } }) },
    });
    expect(markup).not.toContain('三项核验通过');
    expect(markup).toContain('三项核验未通过');
    expect(markup).toContain('缺失，无法确认');
  });

  it('任一项不一致 → 不判成功', () => {
    const digestMismatch = verifyOutcome(
      makeView({
        current: { digest: DIGEST_CURRENT, commit: COMMIT_TARGET },
        task: makeTask({
          state: 'succeeded',
          migration: { attempted: true, current_before: 'head-1', current_after: 'head-2', target_head: 'head-2' },
        }),
      }),
    );
    expect(digestMismatch.verified).toBe(false);
    expect(digestMismatch.checks.find((check) => check.id === 'container_digest')?.ok).toBe(false);

    const headMismatch = verifyOutcome(
      makeView({
        current: { digest: DIGEST_TARGET, commit: COMMIT_TARGET },
        task: makeTask({
          state: 'succeeded',
          migration: { attempted: true, current_before: 'head-1', current_after: 'head-1', target_head: 'head-2' },
        }),
      }),
    );
    expect(headMismatch.verified).toBe(false);
    expect(headMismatch.checks.find((check) => check.id === 'migration_head')?.ok).toBe(false);
  });

  it('任务还没结束 / 没有任务：不判成功', () => {
    expect(verifyOutcome(makeView({ task: makeTask({ state: 'running' }) })).state).toBe('unfinished');
    expect(verifyOutcome(makeView({ task: null })).state).toBe('unavailable');
    expect(verifyOutcome(null).verified).toBe(false);
  });
});

describe('阶段映射', () => {
  it('每个真实阶段都有中文标签，且不显示英文码值', () => {
    for (const stage of IMAGE_UPDATE_STAGES) {
      const label = imageUpdateStageLabel(stage);
      expect(label).not.toBe('');
      expect(label).not.toBe(IMAGE_STAGE_UNKNOWN_LABEL);
      expect(label).not.toMatch(/[A-Za-z]/);
    }
    expect(IMAGE_UPDATE_STAGES).toEqual(['prepare', 'backup', 'migrate', 'switch', 'verify']);
    expect(imageUpdateStageIndex('backup')).toBe(1);
  });

  it('未知阶段有兜底：不崩、不显示英文原文', () => {
    for (const stage of ['download', 'preflight', 'pull', 'reconcile', '???']) {
      const label = imageUpdateStageLabel(stage);
      expect(label).toBe(IMAGE_STAGE_UNKNOWN_LABEL);
      expect(label).not.toMatch(/[A-Za-z]/);
    }
    expect(imageUpdateStageLabel(null)).toBe('尚未开始');
    expect(imageUpdateStageIndex('download')).toBe(-1);
  });

  it('更新器状态码也用中文，未知状态不显示码值', () => {
    expect(imageUpdaterStateLabel('idle')).toBe('空闲');
    expect(imageUpdaterStateLabel('needs_manual')).toBe('需要人工处理');
    expect(imageUpdaterStateLabel('unknown')).toBe('状态未知');
    expect(imageUpdaterStateLabel('weird_state')).toBe('状态未知');
  });

  it('进行中面板只显示真实阶段，没有假进度', () => {
    const markup = renderPanel({
      state: {
        ...initialImageUpdateState(),
        view: makeView({ task: makeTask({ state: 'running', stage: 'backup' }) }),
      },
    });
    expect(markup).toContain('做一致备份（恢复点）');
    expect(markup).toContain('（当前）');
    expect(markup).toContain('正在下载并预检目标镜像');
    expectNoFakeProgress(markup);
  });
});

describe('渲染结果里没有假进度、没有回退按钮', () => {
  const views: Array<[string, ImageUpdateView]> = [
    ['无任务', makeView()],
    ['可安装', installableView()],
    ['不可安装', makeView({ installable: false, reason: 'updater_offline', message: '更新器离线', next_actions: ['检查更新器容器'] })],
    ['进行中', makeView({ task: makeTask({ state: 'running', stage: 'migrate' }) })],
    [
      '成功',
      makeView({
        current: { digest: DIGEST_TARGET, commit: COMMIT_TARGET },
        task: makeTask({
          state: 'succeeded',
          migration: { attempted: true, current_before: 'h1', current_after: 'h2', target_head: 'h2' },
        }),
      }),
    ],
    [
      '失败',
      makeView({
        task: makeTask({
          state: 'failed_not_started',
          stage: 'backup',
          still_on_old_version: true,
          failure: { code: 'backup_failed', stage: 'backup', message: '一致备份未通过校验，未执行迁移' },
          next_actions: ['人工确认旧服务（容器、宿主端口、/readyz）是否正常'],
        }),
      }),
    ],
    [
      '需要人工处理',
      makeView({
        task: makeTask({
          state: 'needs_manual',
          stage: 'switch',
          still_on_old_version: false,
          failure: { code: 'switch_failed', stage: 'switch', message: '已迁移：目标版本启动或验证失败，更新未完成，需要维护处理' },
          next_actions: ['维护门禁仍在：公开端口保持关闭，按任务 ID 人工核对后再恢复'],
        }),
      }),
    ],
  ];

  it('所有状态下都没有百分比/剩余时间/预计', () => {
    for (const [label, view] of views) {
      const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
      expect(markup.length, label).toBeGreaterThan(0);
      expectNoFakeProgress(markup);
    }
    const confirmingMarkup = renderPanel({
      state: { ...initialImageUpdateState(), view: makeView() },
      targetDigest: DIGEST_TARGET,
      confirming: true,
    });
    expectNoFakeProgress(confirmingMarkup);
  });

  it('所有状态下都没有回退/降级按钮', () => {
    for (const [label, view] of views) {
      expectNoRollbackButton(renderPanel({ state: { ...initialImageUpdateState(), view } }));
      expectNoRollbackButton(
        renderPanel({
          state: { ...initialImageUpdateState(), view },
          targetDigest: DIGEST_TARGET,
          confirming: true,
        }),
      );
      expect(label).not.toBe('');
    }
    // 失败状态下能看到码表原因与下一步，但不给一键恢复入口。
    const failed = renderPanel({ state: { ...initialImageUpdateState(), view: views[5]![1] } });
    expect(failed).toContain('一致备份未通过校验，未执行迁移');
    expect(failed).toContain('人工确认旧服务（容器、宿主端口、/readyz）是否正常');
  });
});

describe('二次确认', () => {
  it('确认框写明短暂停机与迁移不可撤销，未勾选风险不能提交', () => {
    const markup = renderPanel({
      state: { ...initialImageUpdateState(), view: makeView() },
      targetDigest: DIGEST_TARGET,
      confirming: true,
    });
    expect(markup).toContain('短暂停机');
    expect(markup).toContain('不可撤销');
    expect(markup).toContain('迁移');
    expect(markup).toContain('data-testid="image-update-submit"');
    expect(markup).toMatch(/data-testid="image-update-submit"[^>]*disabled=""/);
    expect(markup).toContain('data-testid="image-update-risk-ack"');

    const acknowledged = renderPanel({
      state: { ...initialImageUpdateState(), view: makeView() },
      targetDigest: DIGEST_TARGET,
      confirming: true,
      riskAcknowledged: true,
    });
    expect(acknowledged).not.toMatch(/data-testid="image-update-submit"[^>]*disabled=""/);
  });
});

describe('断线重连', () => {
  it('轮询失败不清任务，重连后仍是同一个 request_id', () => {
    const running = makeView({ task: makeTask({ state: 'running', stage: 'prepare' }) });
    const first = applyImageUpdatePoll(initialImageUpdateState(), running);
    expect(first.taskRequestId).toBe(REQUEST_ID);
    expect(first.connection).toBe('ok');

    const submitted = withSubmittedRequest(first, REQUEST_ID);
    const lost = applyImageUpdatePoll(submitted, null);
    expect(lost.connection).toBe('waiting');
    expect(lost.failures).toBe(1);
    expect(lost.view).toBe(first.view); // 断线不清任务
    expect(lost.taskRequestId).toBe(REQUEST_ID);
    expect(lost.submittedRequestId).toBe(REQUEST_ID);

    const recovered = applyImageUpdatePoll(
      lost,
      makeView({ task: makeTask({ state: 'running', stage: 'verify' }) }),
    );
    expect(recovered.connection).toBe('ok');
    expect(recovered.failures).toBe(0);
    expect(recovered.taskRequestId).toBe(REQUEST_ID);
    expect(recovered.submittedRequestId).toBe(REQUEST_ID);
    expect(recovered.view?.task?.stage).toBe('verify');
    expect(describeImageConnection(recovered)).toBeNull();
  });

  it('断线时不让人提交（视图可能是断线前的旧状态）', () => {
    const first = applyImageUpdatePoll(initialImageUpdateState(), makeView());
    const lost = applyImageUpdatePoll(first, null);
    const markup = renderPanel({ state: lost, targetDigest: DIGEST_TARGET });
    expect(markup).toMatch(/data-testid="image-update-start"[^>]*disabled=""/);
    expect(markup).toContain('正在重新连接');
  });

  it('断线时面板显示「正在重新连接」并保留任务编号', () => {
    const first = applyImageUpdatePoll(
      initialImageUpdateState(),
      makeView({ task: makeTask({ state: 'running', stage: 'backup' }) }),
    );
    const lost = applyImageUpdatePoll(first, null);
    const notice = describeImageConnection(lost);
    expect(notice?.title).toBe('正在重新连接…');
    expect(notice?.detail).toContain(REQUEST_ID);

    const markup = renderPanel({ state: lost });
    expect(markup).toContain('正在重新连接');
    expect(markup).toContain(REQUEST_ID);
    expect(markup).not.toContain('更新失败');
    expect(markup).toContain('做一致备份（恢复点）'); // 断线前的阶段仍然显示
  });

  it('退避：断线后退避到 8 秒上限，进行中 3 秒，空闲 15 秒', () => {
    let state = initialImageUpdateState();
    expect(imagePollDelayMs(state)).toBe(3000);
    state = applyImageUpdatePoll(state, makeView());
    expect(imagePollDelayMs(state)).toBe(15000);
    state = applyImageUpdatePoll(state, null);
    expect(imagePollDelayMs(state)).toBe(1500);
    state = applyImageUpdatePoll(state, null);
    expect(imagePollDelayMs(state)).toBe(2500);
    state = applyImageUpdatePoll(state, null);
    expect(imagePollDelayMs(state)).toBe(4000);
    state = applyImageUpdatePoll(state, null);
    expect(imagePollDelayMs(state)).toBe(8000);
    state = applyImageUpdatePoll(state, null);
    expect(imagePollDelayMs(state)).toBe(8000);
  });

  it('连续失败后给出可执行的恢复指引（仍不说成失败）', () => {
    let state = applyImageUpdatePoll(initialImageUpdateState(), null);
    expect(describeImageConnection(state)?.detail).not.toContain('检查更新');
    state = applyImageUpdatePoll(state, null);
    state = applyImageUpdatePoll(state, null);
    const notice = describeImageConnection(state);
    expect(notice?.detail).toContain('确认后端进程与网络可用');
    expect(notice?.title).toBe('正在重新连接…');
  });
});

describe('提交请求体与请求编号', () => {
  it('request_id 是 32 位小写 hex', () => {
    const id = newImageUpdateRequestId();
    expect(id).toMatch(/^[0-9a-f]{32}$/);
    expect(isValidImageRequestId(id)).toBe(true);
    expect(isValidImageRequestId(id.toUpperCase())).toBe(false);
    expect(isValidImageRequestId(id.slice(1))).toBe(false);
    expect(isValidImageRequestId('z'.repeat(32))).toBe(false);
  });

  it('请求体是固定 5 键，且带上期望当前摘要', () => {
    const view = installableView();
    const body = buildImageUpdateSubmission(view, DIGEST_TARGET, REQUEST_ID);
    expect(body).not.toBeNull();
    expect(Object.keys(body!).sort()).toEqual([
      'expected_current_commit',
      'expected_current_digest',
      'operator_confirmed',
      'request_id',
      'target_digest',
    ]);
    expect(body?.operator_confirmed).toBe(true);
    expect(body?.expected_current_digest).toBe(DIGEST_CURRENT);
    expect(body?.expected_current_commit).toBe(COMMIT_CURRENT);
    expect(body?.target_digest).toBe(DIGEST_TARGET);
  });

  it('目标缺失/格式不对/与当前相同/当前摘要未知：不构造请求体', () => {
    const view = installableView();
    expect(buildImageUpdateSubmission(view, null, REQUEST_ID)).toBeNull();
    expect(buildImageUpdateSubmission(view, 'ghcr.io/qsbb/ling-api:latest', REQUEST_ID)).toBeNull();
    expect(buildImageUpdateSubmission(view, DIGEST_CURRENT, REQUEST_ID)).toBeNull();
    expect(buildImageUpdateSubmission(view, DIGEST_TARGET, 'not-a-request-id')).toBeNull();
    expect(
      buildImageUpdateSubmission(
        makeView({ current: { digest: null, commit: COMMIT_CURRENT } }),
        DIGEST_TARGET,
        REQUEST_ID,
      ),
    ).toBeNull();
  });

  it('回执丢失时用同一个 request_id 判断是否落地', () => {
    expect(imageRequestLanded(makeView(), REQUEST_ID)).toBe(false);
    expect(imageRequestLanded(installableView(), REQUEST_ID)).toBe(true);
    expect(
      imageRequestLanded(makeView({ task: makeTask({ request_id: REQUEST_ID }) }), REQUEST_ID),
    ).toBe(true);
    expect(imageRequestLanded(installableView(), 'f'.repeat(32))).toBe(false);
    expect(imageRequestLanded(null, REQUEST_ID)).toBe(false);
  });

  it('提交错误：连接中断按「结果未知」处理，不让人重复提交', () => {
    const info = explainImageSubmitError(
      new ApiError(0, 'network_error', '提交时无法连接服务端，结果未知', true),
    );
    expect(info.reason).toContain('结果未知');
    expect(info.hint).toContain('不要重复提交');
    expect(info.refresh).toBe(true);

    const conflict = explainImageSubmitError(
      new ApiError(409, 'conflict', '已有更新请求在进行中，不能重复提交', false, 'update_in_progress'),
    );
    expect(conflict.reason).toBe('已有更新请求在进行中，不能重复提交');
    expect(conflict.refresh).toBe(true);

    expect(explainImageSubmitError(new Error('boom')).refresh).toBe(true);
  });
});

describe('页面装配', () => {
  it('ImageUpdatePage 用 initialView 直接渲染面板，不再显示加载态', () => {
    const markup = renderToStaticMarkup(
      <ImageUpdatePage onBack={() => undefined} initialView={installableView()} />,
    );
    expect(markup).toContain('data-testid="image-update-panel"');
    expect(markup).toContain('关于与版本');
    expect(markup).toContain('返回设置');
    expect(markup).toContain('sha256:bbbbbbbbbbbb');
    expectNoFakeProgress(markup);
    expectNoRollbackButton(markup);
  });

  it('AboutPage 探测镜像模式期间两套 UI 都不渲染（避免两个更新按钮同时出现）', () => {
    const markup = renderToStaticMarkup(<AboutPage onBack={() => undefined} />);
    expect(markup).toContain('读取更新通道');
    expect(markup).not.toContain('检查最新 Release');
    expect(markup).not.toContain('data-testid="image-update-panel"');
  });
});
