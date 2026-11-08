/**
 * 「关于与版本 → 更新」卡片（docs/UPDATE-UX-DESIGN.md 四态）。
 *
 * 这里保护的是设计原文的硬约束，不是样式细节：
 *
 * - 首屏只有当前版本、一句话结论（后端给的 `message`）、检查时间和**一个**主按钮；
 * - 四态各一个按钮：检查更新 / 更新到 vX / 重新检查 / 重试检查；非 owner 是查看详情；
 *   活动任务覆盖为「查看进度」，且不许再发检查或安装；
 * - 「检查更新」是真检查：POST 检查请求 → 重读 GET 直到 `checked_at` 变新；
 *   10 秒去抖、20 秒超时、失败不清上次结论；
 * - 「更新内容」只在可更新 / 有新版暂不可用两态出现，内容原样来自后端；
 * - digest / commit / 原因码只在默认折叠的「详情/诊断」里，原因码必须配一句人话；
 * - 长内容不撑出横向滚动（375 / 1440）。
 */
import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  AboutPage,
  IMAGE_CHECK_DEBOUNCE_MS,
  IMAGE_CHECK_FAILED_TEXT,
  IMAGE_CHECK_POLL_MS,
  IMAGE_CHECK_TIMEOUT_MS,
  IMAGE_CHECK_TIMEOUT_TEXT,
  IMAGE_NOTES_COLLAPSE_LIMIT,
  IMAGE_STAGE_UNKNOWN_LABEL,
  IMAGE_UPDATE_REUSE_CHECK_MS,
  IMAGE_UPDATE_STAGES,
  ImageUpdatePage,
  ImageUpdatePanel,
  type ImageUpdatePanelProps,
  applyImageUpdatePoll,
  buildImageUpdateSubmission,
  canStartImageCheck,
  describeImageCheckedAt,
  describeImageConnection,
  describeImageLastTaskLine,
  describeImageTaskLine,
  explainImageSubmitError,
  formatImageCheckAge,
  formatImageCheckTime,
  imageCatalogSourceText,
  imageCatalogStateText,
  imageCheckNotice,
  imagePollDelayMs,
  imageTaskActivity,
  imageUpdateConfirmAfterCheck,
  imageUpdateFailureText,
  imageUpdateNotesView,
  imageUpdatePrimaryAction,
  imageUpdateReasonText,
  imageUpdateVerification,
  imageUpdateStageIndex,
  imageUpdateStageLabel,
  imageUpdateTaskStateText,
  imageUpdaterStateLabel,
  imageVersionLabel,
  initialImageCheckAttempt,
  initialImageUpdateState,
  isImageUpdateNotesVisible,
  isNewCheckTime,
  readCatalogCheck,
  readCheckIdFromAck,
  isSafeImageUpdateUrl,
  isValidImageRequestId,
  newImageUpdateRequestId,
  pickImageUpdateNotesRelease,
  runImageUpdateCheck,
  withSubmittedRequest,
} from './components/AboutPage';
import { ApiError } from './api';
import type { ImageUpdateNewerReleaseV2, ImageUpdateTaskV2, ImageUpdateV2 } from './types';

const REQUEST_ID = '0123456789abcdef0123456789abcdef';
const CURRENT_DIGEST = `ghcr.io/qsbb/ling-api@sha256:${'a'.repeat(64)}`;
const TARGET_DIGEST = `ghcr.io/qsbb/ling-api@sha256:${'b'.repeat(64)}`;
const CURRENT_COMMIT = 'c'.repeat(40);
const TARGET_COMMIT = 'd'.repeat(40);

/** 渲染用的固定“现在”：不依赖真实时钟。 */
const NOW = Date.parse('2026-11-09T06:20:00Z');

function iso(offsetMs: number): string {
  return new Date(NOW + offsetMs).toISOString();
}

function makeNewer(
  overrides: Partial<ImageUpdateNewerReleaseV2> = {},
): ImageUpdateNewerReleaseV2 {
  return {
    version: '0.0.4',
    // registry 发现通常拿不到目标版本的构建提交；has_migration 已废弃、恒为 null。
    commit: null,
    has_migration: null,
    notes: ['她的一天：前夜生成次日安排，计划不等于已完成', '修复发送链路重试时丢消息的问题'],
    notes_url: 'https://github.com/qsbb/ling/blob/main/CHANGELOG.md',
    compatible: true,
    incompatible_reason: null,
    digest: TARGET_DIGEST,
    summary: '日程子系统与发送链路的可靠性修复',
    ...overrides,
  };
}

function makeTask(overrides: Partial<ImageUpdateTaskV2> = {}): ImageUpdateTaskV2 {
  return {
    state: 'running',
    stage: 'prepare',
    version: '0.0.4',
    failed_code: null,
    request_id: REQUEST_ID,
    ...overrides,
  };
}

function makeView(overrides: Partial<ImageUpdateV2> = {}): ImageUpdateV2 {
  return {
    schema_version: 'v1',
    state: 'latest',
    message: '当前 v0.0.3 已是最新（检查于 11 月 9 日 14:20）。',
    checked_at: iso(-3 * 60_000),
    current: { version: '0.0.3', commit: CURRENT_COMMIT, digest: CURRENT_DIGEST },
    newer: [],
    target: null,
    task: null,
    details: {
      catalog: {
        state: 'ok',
        state_message: '已读到可信发布目录',
        reason: null,
        reason_message: null,
        source: 'remote',
        checked_at: iso(-3 * 60_000),
        entries: 0,
      },
      updater: { online: true, state: 'idle', protocol: 1, version: '1.0.0' },
      backup_gate: {
        restore_point: true,
        complete: true,
        reason: null,
        reason_message: null,
        checked_at: iso(-60 * 60_000),
      },
    },
    ...overrides,
  };
}

function availableView(overrides: Partial<ImageUpdateV2> = {}): ImageUpdateV2 {
  return makeView({
    state: 'available',
    message: '发现新版 v0.0.4，可以更新。',
    newer: [makeNewer()],
    target: { version: '0.0.4', digest: TARGET_DIGEST, commit: null },
    ...overrides,
  });
}

function blockedView(overrides: Partial<ImageUpdateV2> = {}): ImageUpdateV2 {
  return makeView({
    state: 'newer_unavailable',
    message: '发现新版 v0.0.4，但暂时不能更新：新版已发布，但暂时取不到安装包。',
    newer: [
      makeNewer({
        compatible: false,
        digest: null,
        incompatible_reason: 'digest_unresolved',
      }),
    ],
    target: null,
    ...overrides,
  });
}

function unknownView(overrides: Partial<ImageUpdateV2> = {}): ImageUpdateV2 {
  return makeView({
    state: 'unknown',
    message: '暂时无法确认是否有新版本：更新服务暂时没有响应。',
    ...overrides,
  });
}

function renderPanel(overrides: Partial<ImageUpdatePanelProps> = {}): string {
  const props: ImageUpdatePanelProps = {
    state: { ...initialImageUpdateState(), view: makeView() },
    canWrite: true,
    checking: false,
    checkNotice: null,
    confirming: false,
    riskAcknowledged: false,
    submitting: false,
    submitError: null,
    nowMs: NOW,
    onCheck: () => undefined,
    onStartConfirm: () => undefined,
    onCancelConfirm: () => undefined,
    onToggleRisk: () => undefined,
    onConfirm: () => undefined,
    ...overrides,
  };
  return renderToStaticMarkup(<ImageUpdatePanel {...props} />);
}

/** 取出详情区「目录来源」那一行的纯文本，避免整页其它「未知」干扰断言。 */
function catalogSourceTextOf(markup: string): string {
  const match = /data-testid="image-update-detail-catalog-source">([\s\S]*?)<\/dd>/.exec(markup);
  return (match?.[1] ?? '').replace(/<[^>]*>/g, '').trim();
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

function primaryButton(markup: string): string {
  const match = /<button\b[^>]*data-testid="image-update-primary"[^>]*>([\s\S]*?)<\/button>/.exec(
    markup,
  );
  expect(match, '主按钮必须存在').not.toBeNull();
  return (match?.[1] ?? '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

function actionKind(markup: string): string | null {
  const match = /data-testid="image-update-primary"[^>]*data-action="([^"]+)"/.exec(markup);
  return match?.[1] ?? null;
}

/** 面板所有状态都要满足的硬约束。 */
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

function fakeClock(start: number) {
  let current = start;
  return {
    now: () => current,
    advance: (ms: number) => {
      current += ms;
    },
    sleep: async (ms: number) => {
      current += ms;
    },
  };
}

function sequenceReader(views: Array<ImageUpdateV2 | Error>): () => Promise<ImageUpdateV2> {
  let index = 0;
  return async () => {
    const item = views[Math.min(index, views.length - 1)];
    index += 1;
    if (item === undefined || item instanceof Error) {
      throw item ?? new Error('没有更多视图');
    }
    return item;
  };
}

/* ==================================================================== 四态 */

describe('四态：一句话结论 + 一个主按钮', () => {
  it('已是最新：显示后端给的结论、「检查更新」，不显示更新内容区', () => {
    const view = makeView();
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain(view.message);
    expect(primaryButton(markup)).toBe('检查更新');
    expect(actionKind(markup)).toBe('check');
    expect(markup).not.toContain('data-testid="image-update-notes"');
    expect(markup).toContain('检查于');
  });

  it('可更新：结论 +「更新到 v0.0.4」，并出现更新内容区', () => {
    const view = availableView();
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('发现新版 v0.0.4，可以更新。');
    expect(primaryButton(markup)).toBe('更新到 v0.0.4');
    expect(actionKind(markup)).toBe('update');
    expect(markup).toContain('data-testid="image-update-notes"');
  });

  it('有新版暂不可用：结论 +「重新检查」', () => {
    const view = blockedView();
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('但暂时不能更新');
    expect(primaryButton(markup)).toBe('重新检查');
    expect(actionKind(markup)).toBe('recheck');
    expect(markup).not.toContain('更新到 v0.0.4');
  });

  it('暂不能确认：结论 +「重试检查」，且不出现「已是最新」', () => {
    const view = unknownView();
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('暂时无法确认是否有新版本');
    expect(primaryButton(markup)).toBe('重试检查');
    expect(actionKind(markup)).toBe('retry');
    expect(markup).not.toContain('已是最新');
  });

  it('唯一主按钮：data-testid 只有一个，其他按钮都不是更新动作', () => {
    for (const view of [makeView(), availableView(), blockedView(), unknownView()]) {
      const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
      expect(markup.match(/data-testid="image-update-primary"/g)).toHaveLength(1);
      const updates = buttonTexts(markup).filter(
        (text) =>
          text.includes('检查更新') ||
          text.includes('更新到') ||
          text.includes('重新检查') ||
          text.includes('重试检查'),
      );
      expect(updates).toHaveLength(1);
    }
  });

  it('首屏不出现 digest、原因码与 sha256 字段', () => {
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view: blockedView() } });
    expect(markup).not.toContain('sha256');
    expect(markup).not.toContain('digest_unresolved');
    expect(markup).not.toContain('data-testid="image-update-details"');
    expect(markup).not.toContain('data-testid="image-update-detail-digest"');
  });

  it('服务端没给结论时不自己编一个', () => {
    const view = makeView({ message: '   ' });
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('服务端没有给出结论');
    expect(markup).not.toContain('已是最新');
  });

  it('版本号带 v 与不带 v 都不叠两个 v', () => {
    expect(imageVersionLabel('0.0.3')).toBe('v0.0.3');
    expect(imageVersionLabel('v0.0.4')).toBe('v0.0.4');
    expect(imageVersionLabel('')).toBeNull();
    expect(imageVersionLabel(null)).toBeNull();
    const markup = renderPanel({
      state: {
        ...initialImageUpdateState(),
        view: makeView({ current: { version: 'v0.0.3', commit: null, digest: null } }),
      },
    });
    expect(markup).toContain('当前 v0.0.3');
    expect(markup).not.toContain('vv0.0.3');
  });

  it('当前版本未知时如实写未知，不猜版本', () => {
    const markup = renderPanel({
      state: {
        ...initialImageUpdateState(),
        view: unknownView({ current: { version: null, commit: null, digest: null } }),
      },
    });
    expect(markup).toContain('当前版本未知');
  });

  it('一句话结论直接来自服务端 message（不是前端按状态拼的）', () => {
    const view = makeView({ state: 'latest', message: '服务端自定义的一句话。' });
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('服务端自定义的一句话。');
    expect(markup).not.toContain('当前 v0.0.3 已是最新');
  });
});

/* ================================================================ 非 owner */

describe('非 owner：同一套四态，但主按钮是「查看详情」', () => {
  it('四态都只给「查看详情」，没有安装/检查写入按钮', () => {
    for (const view of [makeView(), availableView(), blockedView(), unknownView()]) {
      const markup = renderPanel({
        state: { ...initialImageUpdateState(), view },
        canWrite: false,
      });
      expect(primaryButton(markup)).toBe('查看详情');
      expect(actionKind(markup)).toBe('details');
      const texts = buttonTexts(markup);
      expect(texts.some((text) => text.includes('更新到'))).toBe(false);
      expect(texts.some((text) => text.includes('检查更新'))).toBe(false);
      expect(texts.some((text) => text.includes('重新检查'))).toBe(false);
      expect(texts.some((text) => text.includes('重试检查'))).toBe(false);
      // 主按钮就是详情开关，不再重复给一个「详情/诊断」小按钮。
      expect(markup).not.toContain('data-testid="image-update-details-toggle"');
    }
  });

  it('非 owner 即使进入确认块也不能提交', () => {
    const markup = renderPanel({
      state: { ...initialImageUpdateState(), view: availableView() },
      canWrite: false,
      confirming: true,
      riskAcknowledged: true,
    });
    expect(markup).toContain('data-testid="image-update-confirm"');
    expect(markup).toMatch(/data-testid="image-update-submit"[^>]*disabled=""/);
  });

  it('非 owner 遇到活动任务时仍能展开详情（主按钮让给「查看进度」）', () => {
    const view = makeView({ task: makeTask({ state: 'running', stage: 'backup' }) });
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view }, canWrite: false });
    expect(primaryButton(markup)).toBe('查看进度');
    expect(markup).toContain('data-testid="image-update-details-toggle"');
  });

  it('owner 在可更新态能拿到安装主按钮与详情开关', () => {
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view: availableView() } });
    expect(primaryButton(markup)).toBe('更新到 v0.0.4');
    expect(markup).toContain('data-testid="image-update-details-toggle"');
  });
});

/* ================================================================ 活动任务 */

describe('活动任务覆盖结论展示，不许再检查或安装', () => {
  it('进行中：正在更新 vX + 阶段，主按钮「查看进度」', () => {
    const view = makeView({ task: makeTask({ state: 'running', stage: 'backup', version: '0.0.4' }) });
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('正在更新 v0.0.4');
    expect(markup).toContain('做一致备份（恢复点）');
    expect(primaryButton(markup)).toBe('查看进度');
    expect(actionKind(markup)).toBe('progress');
    const texts = buttonTexts(markup);
    expect(texts.some((text) => text.includes('检查更新'))).toBe(false);
    expect(texts.some((text) => text.includes('更新到'))).toBe(false);
  });

  it('排队中：写「更新请求已提交，等待开始」，主按钮仍是「查看进度」', () => {
    const view = makeView({ task: makeTask({ state: 'queued', stage: '', version: '0.0.4' }) });
    expect(describeImageTaskLine(view)).toBe('更新请求已提交，等待开始');
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('更新请求已提交，等待开始');
    expect(primaryButton(markup)).toBe('查看进度');
  });

  it('任务进行中即使 checking=true 也不变成「正在检查…」', () => {
    const view = makeView({ task: makeTask({ state: 'running', stage: 'migrate' }) });
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view }, checking: true });
    expect(primaryButton(markup)).toBe('查看进度');
    expect(actionKind(markup)).toBe('progress');
  });

  it('非 owner 的活动任务：主按钮是「查看进度」（不是检查/安装）', () => {
    const view = makeView({ task: makeTask({ state: 'running', stage: 'switch' }) });
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view }, canWrite: false });
    expect(primaryButton(markup)).toBe('查看进度');
  });

  it('旧失败任务只收成一行，且不压住新的发现结论', () => {
    const view = makeView({
      task: makeTask({
        state: 'failed_not_started',
        stage: 'backup',
        failed_code: 'backup_failed',
        version: null,
      }),
    });
    expect(describeImageLastTaskLine(view)).toBe('上次更新失败：备份没有完成');
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('上次更新失败：备份没有完成');
    expect(primaryButton(markup)).toBe('检查更新');
    expect(imageTaskActivity(view)).toBe('none');
  });

  it('成功的旧任务也不直接写「更新成功」：三项核验不一致要说出来', () => {
    const verified = makeView({
      current: { version: '0.0.4', commit: CURRENT_COMMIT, digest: CURRENT_DIGEST },
      task: makeTask({
        state: 'succeeded',
        stage: 'verify',
        target: { digest: CURRENT_DIGEST, commit: CURRENT_COMMIT },
        migration: {
          attempted: true,
          current_before: 'head-1',
          current_after: 'head-2',
          target_head: 'head-2',
        },
      }),
    });
    expect(describeImageLastTaskLine(verified)).toContain('三项核验一致');
    expect(describeImageLastTaskLine(verified)).not.toContain('更新成功');

    const mismatch = makeView({
      task: makeTask({
        state: 'succeeded',
        stage: 'verify',
        target: { digest: TARGET_DIGEST, commit: CURRENT_COMMIT },
        migration: { attempted: true, target_head: 'head-2', current_after: 'head-2' },
      }),
    });
    expect(describeImageLastTaskLine(mismatch)).toContain('三项核验不一致');

    const incomplete = makeView({
      task: makeTask({
        state: 'succeeded',
        stage: 'verify',
        target: { digest: CURRENT_DIGEST, commit: null },
        migration: { attempted: true, target_head: 'head-2', current_after: 'head-2' },
      }),
    });
    expect(describeImageLastTaskLine(incomplete)).toContain('三项核验值不全');
  });

  it('needs_manual 任务首屏保留可发现的提示', () => {
    const view = makeView({ task: makeTask({ state: 'needs_manual', stage: 'verify' }) });
    expect(describeImageLastTaskLine(view)).toContain('上次更新需要处理');
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('上次更新需要处理');
  });

  it('任务行/活动任务都没有假进度', () => {
    for (const view of [
      makeView({ task: makeTask({ state: 'running', stage: 'prepare' }) }),
      makeView({ task: makeTask({ state: 'queued', stage: '' }) }),
    ]) {
      expectNoFakeProgress(renderPanel({ state: { ...initialImageUpdateState(), view } }));
    }
  });
});

/* ================================================================ 更新内容 */

describe('「更新内容」只显示后端给的，不自己编', () => {
  it('可更新：summary + items + 完整更新日志链接', () => {
    const view = availableView();
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('data-testid="image-update-notes-summary"');
    expect(markup).toContain('日程子系统与发送链路的可靠性修复');
    expect(markup).toContain('她的一天：前夜生成次日安排，计划不等于已完成');
    expect(markup).toContain('修复发送链路重试时丢消息的问题');
    expect(markup).toContain('data-testid="image-update-notes-url"');
    expect(markup).toContain('完整更新日志');
    expect(markup).toContain('https://github.com/qsbb/ling/blob/main/CHANGELOG.md');
    expect(markup).toContain('更新内容（v0.0.4）');
  });

  it('items 为空：显示「这一版没有提供更新说明」', () => {
    const view = availableView({ newer: [makeNewer({ notes: [] })] });
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('这一版没有提供更新说明');
    expect(markup).not.toContain('data-testid="image-update-notes-items"');
  });

  it('没有 notes_url 就不显示「完整更新日志」', () => {
    const view = availableView({ newer: [makeNewer({ notes_url: null })] });
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).not.toContain('data-testid="image-update-notes-url"');
    expect(markup).not.toContain('完整更新日志');
  });

  it('notes_url 不是 http(s) 时不渲染链接（不把 javascript: 当日志链接）', () => {
    expect(isSafeImageUpdateUrl('https://example.test/a')).toBe(true);
    expect(isSafeImageUpdateUrl('http://example.test/a')).toBe(true);
    expect(isSafeImageUpdateUrl('javascript:alert(1)')).toBe(false);
    expect(isSafeImageUpdateUrl('mailto:a@b.c')).toBe(false);
    expect(isSafeImageUpdateUrl('/relative')).toBe(false);
    expect(isSafeImageUpdateUrl(null)).toBe(false);
    const view = availableView({ newer: [makeNewer({ notes_url: 'javascript:alert(1)' })] });
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).not.toContain('data-testid="image-update-notes-url"');
    expect(markup).not.toContain('javascript:');
  });

  it('超过 8 条折叠：默认只平铺 8 条，并给展开按钮', () => {
    const notes = Array.from({ length: 11 }, (_, index) => `第 ${index + 1} 条更新说明`);
    const view = availableView({ newer: [makeNewer({ notes })] });
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    const itemsHtml = /data-testid="image-update-notes-items"[\s\S]*?<\/ul>/.exec(markup)?.[0] ?? '';
    expect((itemsHtml.match(/<li\b/g) ?? []).length).toBe(IMAGE_NOTES_COLLAPSE_LIMIT);
    expect(markup).toContain('展开其余 3 条');
    expect(markup).toContain('data-testid="image-update-notes-more"');
  });

  it('有新版暂不可用也显示更新内容；已是最新/暂不能确认不显示', () => {
    expect(isImageUpdateNotesVisible(blockedView())).toBe(true);
    expect(isImageUpdateNotesVisible(availableView())).toBe(true);
    expect(isImageUpdateNotesVisible(makeView())).toBe(false);
    expect(isImageUpdateNotesVisible(unknownView())).toBe(false);
    for (const view of [makeView(), unknownView()]) {
      expect(renderPanel({ state: { ...initialImageUpdateState(), view } })).not.toContain(
        'data-testid="image-update-notes"',
      );
    }
  });

  it('没有 summary 就不编摘要，items 仍原样显示', () => {
    const view = availableView({ newer: [makeNewer({ summary: null })] });
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).not.toContain('data-testid="image-update-notes-summary"');
    expect(markup).toContain('她的一天：前夜生成次日安排，计划不等于已完成');
  });

  it('推荐目标在 newer 里找不到对应条目时，不拿别的新版本说明冒充', () => {
    const view = availableView({
      newer: [makeNewer({ version: '0.0.5', notes: ['这是 0.0.5 的说明'] })],
    });
    expect(pickImageUpdateNotesRelease(view)).toBeNull();
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).not.toContain('这是 0.0.5 的说明');
    expect(markup).toContain('这一版没有提供更新说明');
  });

  it('讲哪一版：优先推荐目标，其次最高新版本，没有新版就没有内容', () => {
    const target = availableView();
    expect(pickImageUpdateNotesRelease(target)?.version).toBe('0.0.4');
    // 与后端同口径：先在「只差摘要解析」的一档里取最早一版（0.0.4），
    // 那一档为空才取最高新版本。
    const mixed = blockedView({
      newer: [
        makeNewer({ version: '0.0.6', compatible: false, incompatible_reason: 'not_in_min_from' }),
        makeNewer({ version: '0.0.5', compatible: false, incompatible_reason: 'digest_unresolved' }),
        makeNewer({ version: '0.0.4', compatible: false, incompatible_reason: 'digest_unresolved' }),
      ],
    });
    expect(pickImageUpdateNotesRelease(mixed)?.version).toBe('0.0.4');
    const blockedAll = blockedView({
      newer: [
        makeNewer({ version: '0.0.6', compatible: false, incompatible_reason: 'not_in_min_from' }),
        makeNewer({ version: '0.0.5', compatible: false, incompatible_reason: 'not_in_min_from' }),
      ],
    });
    expect(pickImageUpdateNotesRelease(blockedAll)?.version).toBe('0.0.6');
    expect(pickImageUpdateNotesRelease(makeView())).toBeNull();
    expect(imageUpdateNotesView(makeView())).toBeNull();
  });
});

/* ================================================================ 真检查 */

describe('「检查更新」是真检查：POST 一次，然后等 checked_at 变新', () => {
  it('POST 一个 32 位 hex 检查编号，随后读到新 checked_at 才算完成', async () => {
    const clock = fakeClock(NOW);
    const previous = makeView();
    const fresh = makeView({ checked_at: iso(2000), message: '刚刚检查过。' });
    const posted: string[] = [];
    const result = await runImageUpdateCheck(initialImageCheckAttempt(), previous, {
      postCheck: async (id) => {
        posted.push(id);
      },
      readView: sequenceReader([previous, fresh]),
      now: clock.now,
      sleep: clock.sleep,
      newCheckId: () => REQUEST_ID,
    });
    expect(posted).toEqual([REQUEST_ID]);
    expect(isValidImageRequestId(posted[0] ?? '')).toBe(true);
    expect(result.status).toBe('ok');
    expect(result.view?.checked_at).toBe(fresh.checked_at);
    expect(result.attempt.status).toBe('done');
    expect(result.notice).toBeNull();
  });

  it('默认编号生成器给出 32 位小写 hex', async () => {
    const clock = fakeClock(NOW);
    const posted: string[] = [];
    const result = await runImageUpdateCheck(initialImageCheckAttempt(), makeView(), {
      postCheck: async (id) => {
        posted.push(id);
      },
      readView: async () => makeView({ checked_at: iso(2000) }),
      now: clock.now,
      sleep: clock.sleep,
    });
    expect(result.status).toBe('ok');
    expect(posted[0]).toMatch(/^[0-9a-f]{32}$/);
  });

  it('10 秒内重复点击只发一次请求', async () => {
    const clock = fakeClock(NOW);
    const posted: string[] = [];
    const first = await runImageUpdateCheck(initialImageCheckAttempt(), makeView(), {
      postCheck: async (id) => {
        posted.push(id);
      },
      readView: async () => makeView({ checked_at: iso(2000) }),
      now: clock.now,
      sleep: clock.sleep,
      newCheckId: () => REQUEST_ID,
    });
    expect(posted).toHaveLength(1);

    // 完成时刻就在 now()：10 秒内再点不写请求。
    const second = await runImageUpdateCheck(first.attempt, first.view, {
      postCheck: async (id) => {
        posted.push(id);
      },
      readView: async () => makeView({ checked_at: iso(4000) }),
      now: clock.now,
      sleep: clock.sleep,
      newCheckId: () => REQUEST_ID,
    });
    expect(second.status).toBe('skipped');
    expect(posted).toHaveLength(1);
    expect(canStartImageCheck(first.attempt, clock.now())).toBe(false);

    // 过了 10 秒才允许下一次。
    clock.advance(IMAGE_CHECK_DEBOUNCE_MS);
    expect(canStartImageCheck(first.attempt, clock.now())).toBe(true);
    const third = await runImageUpdateCheck(first.attempt, first.view, {
      postCheck: async (id) => {
        posted.push(id);
      },
      readView: async () => makeView({ checked_at: iso(20000) }),
      now: clock.now,
      sleep: clock.sleep,
      newCheckId: () => REQUEST_ID,
    });
    expect(third.status).toBe('ok');
    expect(posted).toHaveLength(2);
  });

  it('POST 失败：保留上次结论，只加一行「这次检查没成功」', async () => {
    const clock = fakeClock(NOW);
    const previous = makeView();
    const result = await runImageUpdateCheck(initialImageCheckAttempt(), previous, {
      postCheck: async () => {
        throw new ApiError(500, 'internal_error', '服务器内部错误', true);
      },
      readView: async () => makeView({ checked_at: iso(2000) }),
      now: clock.now,
      sleep: clock.sleep,
      newCheckId: () => REQUEST_ID,
    });
    expect(result.status).toBe('failed');
    expect(result.view).toBe(previous);
    expect(result.notice).toContain(IMAGE_CHECK_FAILED_TEXT);
    expect(result.attempt.status).toBe('failed');
    expect(imageCheckNotice(result.attempt)).toContain('这次检查没成功');
  });

  it('20 秒等不到新 checked_at：超时，仍保留上次结论', async () => {
    const clock = fakeClock(NOW);
    const previous = makeView();
    const result = await runImageUpdateCheck(initialImageCheckAttempt(), previous, {
      postCheck: async () => undefined,
      // checked_at 没变 = 还是上次的结果，不能算这次完成。
      readView: async () => previous,
      now: clock.now,
      sleep: clock.sleep,
      newCheckId: () => REQUEST_ID,
    });
    expect(result.status).toBe('timed_out');
    expect(result.view).toBe(previous);
    expect(result.notice).toContain('这次检查没成功');
    expect(result.notice).toContain(IMAGE_CHECK_TIMEOUT_TEXT);
    expect(clock.now() - NOW).toBeGreaterThanOrEqual(IMAGE_CHECK_TIMEOUT_MS);
  });

  it('用后端回执的检查编号匹配终态：checked_at 没变也能确认是这次完成', async () => {
    const clock = fakeClock(NOW);
    const previous = makeView();
    const finished = makeView({
      checked_at: previous.checked_at,
      details: {
        ...previous.details,
        catalog: {
          ...previous.details.catalog,
          check: { check_id: REQUEST_ID, state: 'succeeded' },
        },
      },
    });
    const result = await runImageUpdateCheck(initialImageCheckAttempt(), previous, {
      postCheck: async () => ({ schema_version: 'v1', request_id: REQUEST_ID, accepted: true }),
      readView: async () => finished,
      now: clock.now,
      sleep: clock.sleep,
      newCheckId: () => REQUEST_ID,
    });
    expect(result.status).toBe('ok');
    expect(result.view).toBe(finished);
  });

  it('别的检查编号完成、checked_at 也没变：不算这次的结果', async () => {
    const clock = fakeClock(NOW);
    const previous = makeView();
    const other = makeView({
      checked_at: previous.checked_at,
      details: {
        ...previous.details,
        catalog: {
          ...previous.details.catalog,
          check: { check_id: 'f'.repeat(32), state: 'succeeded' },
        },
      },
    });
    const result = await runImageUpdateCheck(initialImageCheckAttempt(), previous, {
      postCheck: async () => ({ request_id: REQUEST_ID }),
      readView: async () => other,
      now: clock.now,
      sleep: clock.sleep,
      newCheckId: () => REQUEST_ID,
    });
    expect(result.status).toBe('timed_out');
    expect(result.view).toBe(previous);
  });

  it('更新器把这次检查记为 failed：交出可读的新结论，并保留「这次检查没成功」', async () => {
    const clock = fakeClock(NOW);
    const previous = makeView();
    const failedCheck = makeView({
      state: 'unknown',
      message: '暂时无法确认是否有新版本：发布目录暂时不可用。',
      checked_at: null,
      details: {
        ...previous.details,
        catalog: {
          ...previous.details.catalog,
          check: { check_id: REQUEST_ID, state: 'failed' },
        },
      },
    });
    const result = await runImageUpdateCheck(initialImageCheckAttempt(), previous, {
      postCheck: async () => ({ request_id: REQUEST_ID }),
      readView: async () => failedCheck,
      now: clock.now,
      sleep: clock.sleep,
      newCheckId: () => REQUEST_ID,
    });
    expect(result.status).toBe('failed');
    expect(result.view).toBe(failedCheck);
    expect(result.notice).toContain(IMAGE_CHECK_FAILED_TEXT);
  });

  it('回执编号只认 32 位小写 hex；缺字段时退回 checked_at 判定', async () => {
    expect(readCheckIdFromAck({ request_id: REQUEST_ID })).toBe(REQUEST_ID);
    expect(readCheckIdFromAck({ request_id: 'NOT-HEX' })).toBeNull();
    expect(readCheckIdFromAck(null)).toBeNull();
    expect(readCheckIdFromAck('nope')).toBeNull();
    expect(
      readCatalogCheck(
        makeView({
          details: {
            ...makeView().details,
            catalog: { ...makeView().details.catalog, check: { check_id: REQUEST_ID, state: 'running' } },
          },
        }),
      ),
    ).toEqual({ checkId: REQUEST_ID, state: 'running' });
    expect(readCatalogCheck(makeView())).toBeNull();
  });

  it('轮询中 GET 断线一次不算失败，恢复后照常收尾', async () => {
    const clock = fakeClock(NOW);
    const previous = makeView();
    const fresh = makeView({ checked_at: iso(4000) });
    const result = await runImageUpdateCheck(initialImageCheckAttempt(), previous, {
      postCheck: async () => undefined,
      readView: sequenceReader([new Error('network down'), fresh]),
      now: clock.now,
      sleep: clock.sleep,
      newCheckId: () => REQUEST_ID,
    });
    expect(result.status).toBe('ok');
    expect(result.view?.checked_at).toBe(fresh.checked_at);
  });

  it('checked_at 判断：null→有值算新，相同不算新，回拨不算新', () => {
    expect(isNewCheckTime(iso(0), null)).toBe(true);
    expect(isNewCheckTime(null, iso(0))).toBe(false);
    expect(isNewCheckTime(iso(0), iso(0))).toBe(false);
    expect(isNewCheckTime(iso(2000), iso(0))).toBe(true);
    expect(isNewCheckTime(iso(-2000), iso(0))).toBe(false);
    expect(isNewCheckTime('not-a-date', iso(0))).toBe(true);
  });

  it('去抖判定：初始可发、进行中不可发、10 秒后恢复', () => {
    const attempt = initialImageCheckAttempt();
    expect(canStartImageCheck(attempt, NOW)).toBe(true);
    expect(
      canStartImageCheck({ ...attempt, status: 'checking', startedAtMs: NOW }, NOW + 1),
    ).toBe(false);
    expect(canStartImageCheck({ ...attempt, startedAtMs: NOW }, NOW + 1)).toBe(false);
    expect(
      canStartImageCheck({ ...attempt, startedAtMs: NOW }, NOW + IMAGE_CHECK_DEBOUNCE_MS),
    ).toBe(true);
  });

  it('检查中按钮 loading：禁用 +「正在检查…」，上次结论与检查时间都还在', () => {
    const view = makeView();
    const markup = renderPanel({
      state: { ...initialImageUpdateState(), view },
      checking: true,
    });
    expect(primaryButton(markup)).toBe('正在检查…');
    expect(markup).toMatch(/data-testid="image-update-primary"[^>]*disabled=""/);
    expect(markup).toContain(view.message);
    expect(markup).toContain('检查于');
  });

  it('检查失败/超时：上次结论不清掉，多一行「这次检查没成功」', () => {
    for (const notice of [
      IMAGE_CHECK_FAILED_TEXT,
      `${IMAGE_CHECK_FAILED_TEXT}：${IMAGE_CHECK_TIMEOUT_TEXT}`,
    ]) {
      const view = makeView();
      const markup = renderPanel({
        state: { ...initialImageUpdateState(), view },
        checkNotice: notice,
      });
      expect(markup).toContain('这次检查没成功');
      expect(markup).toContain(view.message);
      expect(markup).toContain('检查于');
    }
  });

  it('检查完成后「检查于」跟着变新', () => {
    const before = renderPanel({
      state: { ...initialImageUpdateState(), view: makeView({ checked_at: iso(-3 * 60_000) }) },
    });
    const after = renderPanel({
      state: { ...initialImageUpdateState(), view: makeView({ checked_at: iso(-10_000) }) },
    });
    expect(before).toContain('3 分钟前');
    expect(after).toContain('刚刚');
    expect(after).not.toContain('3 分钟前');
  });
});

/* ================================================================ 详情折叠 */

describe('详情/诊断默认折叠：digest 与原因码只在展开后出现', () => {
  it('折叠时不渲染 digest / 原因码', () => {
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view: blockedView() } });
    expect(markup).not.toContain('sha256');
    expect(markup).not.toContain('digest_unresolved');
    expect(markup).not.toContain('data-testid="image-update-details"');
  });

  it('展开后出现 digest、推荐目标、原因码，并且原因码旁边有人话', () => {
    const base = blockedView();
    const view = blockedView({
      details: {
        ...base.details,
        catalog: { ...base.details.catalog, reason: 'catalog_missing' },
      },
    });
    const markup = renderPanel({
      state: { ...initialImageUpdateState(), view },
      defaultDetailsOpen: true,
    });
    expect(markup).toContain('data-testid="image-update-details"');
    // 当前 digest（`blockedView` 没有推荐目标，详情里只有当前运行摘要）。
    expect(markup).toContain('sha256:aaaaaaaaaaaa');
    expect(markup).toContain('digest_unresolved');
    expect(markup).toContain(imageUpdateReasonText('digest_unresolved'));
    expect(markup).toContain('catalog_missing');
    expect(markup).toContain(imageUpdateReasonText('catalog_missing'));
    expect(markup).toContain('暂时取不到安装包');
  });

  it('详情列出设计文档要求的诊断字段', () => {
    const markup = renderPanel({
      state: { ...initialImageUpdateState(), view: blockedView() },
      defaultDetailsOpen: true,
    });
    for (const testId of [
      'image-update-detail-version',
      'image-update-detail-commit',
      'image-update-detail-digest',
      'image-update-detail-catalog-source',
      'image-update-detail-catalog-time',
      'image-update-detail-catalog-state',
      'image-update-detail-catalog-reason',
      'image-update-detail-updater',
      'image-update-detail-backup',
      'image-update-detail-newer',
      'image-update-detail-target',
      'image-update-detail-request-id',
      'image-update-detail-task',
    ]) {
      expect(markup).toContain(`data-testid="${testId}"`);
    }
    expect(markup).toContain('远程发布目录');
    expect(markup).toContain('在线');
    expect(markup).toContain('协议 v1');
  });

  it('目录来源是 registry 时显示镜像仓库（GHCR），缺失时仍显示未知', () => {
    const base = makeView();
    const viewWithSource = (source: string | null) =>
      makeView({
        details: {
          ...base.details,
          catalog: { ...base.details.catalog, source },
        },
      });

    const registryMarkup = renderPanel({
      state: { ...initialImageUpdateState(), view: viewWithSource('registry') },
      defaultDetailsOpen: true,
    });
    expect(catalogSourceTextOf(registryMarkup)).toBe('镜像仓库（GHCR）');
    expect(catalogSourceTextOf(registryMarkup)).not.toBe('未知');

    const missingMarkup = renderPanel({
      state: { ...initialImageUpdateState(), view: viewWithSource(null) },
      defaultDetailsOpen: true,
    });
    expect(catalogSourceTextOf(missingMarkup)).toBe('未知');
  });

  it('失败码在详情里也配人话；未知码给兜底文案', () => {
    const view = makeView({
      task: makeTask({ state: 'failed_not_started', stage: 'backup', failed_code: 'backup_failed' }),
    });
    const markup = renderPanel({
      state: { ...initialImageUpdateState(), view },
      defaultDetailsOpen: true,
    });
    expect(markup).toContain('backup_failed');
    expect(markup).toContain('备份没有完成');
    expect(imageUpdateFailureText('weird_code')).toBe('更新器没有给出可读的失败说明');
    expect(imageUpdateReasonText('weird_code')).toBe('暂时无法确认更新条件，请重新检查。');
    expect(imageUpdateReasonText(null)).toContain('暂时无法确认');
  });

  it('三项核验：任务记录与本机实测逐项对照，缺失不判一致', () => {
    const task = makeTask({
      state: 'succeeded',
      stage: 'verify',
      request_id: REQUEST_ID,
      target: { digest: CURRENT_DIGEST, commit: CURRENT_COMMIT },
      migration: {
        attempted: true,
        current_before: 'head-1',
        current_after: 'head-2',
        target_head: 'head-2',
      },
    });
    const view = makeView({ task });
    const checks = imageUpdateVerification(task, view.current);
    expect(checks.map((check) => check.id)).toEqual([
      'container_digest',
      'app_commit',
      'migration_head',
    ]);
    expect(checks.every((check) => check.ok === true)).toBe(true);

    const missing = imageUpdateVerification(
      makeTask({ target: { digest: CURRENT_DIGEST, commit: null } }),
      view.current,
    );
    expect(missing.find((check) => check.id === 'app_commit')?.ok).toBeNull();

    const mismatch = imageUpdateVerification(
      makeTask({
        target: { digest: TARGET_DIGEST, commit: CURRENT_COMMIT },
        migration: { attempted: true, current_before: 'head-1', current_after: 'head-1', target_head: 'head-2' },
      }),
      view.current,
    );
    expect(mismatch.find((check) => check.id === 'container_digest')?.ok).toBe(false);
    expect(mismatch.find((check) => check.id === 'migration_head')?.ok).toBe(false);

    const markup = renderPanel({
      state: { ...initialImageUpdateState(), view },
      defaultDetailsOpen: true,
    });
    expect(markup).toContain('data-testid="image-update-detail-verification"');
    expect(markup).toContain('运行容器 digest');
    expect(markup).toContain('应用提交');
    expect(markup).toContain('迁移 head');
    expect(markup).toContain(REQUEST_ID);
  });

  it('没有任务时不伪造请求 ID 与三项核验', () => {
    const markup = renderPanel({
      state: { ...initialImageUpdateState(), view: makeView() },
      defaultDetailsOpen: true,
    });
    expect(markup).toContain('data-testid="image-update-detail-request-id"');
    expect(markup).toContain('没有进行中的请求');
    expect(markup).toContain('没有任务记录');
  });

  it('任务详情默认收起：首屏只有一行任务提示，不声称「更新成功」', () => {
    const view = makeView({ task: makeTask({ state: 'running', stage: 'migrate' }) });
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).not.toContain('data-testid="image-update-task-detail"');
    expect(markup).toContain('执行数据库迁移');
    expect(markup).not.toContain('更新成功');
  });

  it('目录状态码翻成人话，不把英文码值当结论', () => {
    expect(imageCatalogStateText('ok')).toBe('正常');
    expect(imageCatalogStateText('missing')).toBe('缺失');
    expect(imageCatalogStateText('invalid')).toBe('无效');
    expect(imageCatalogStateText('weird')).toBe('未知');
    expect(imageCatalogSourceText('registry')).toBe('镜像仓库（GHCR）');
    expect(imageCatalogSourceText('remote')).toBe('远程发布目录');
    expect(imageCatalogSourceText('local')).toContain('本机发布目录');
    expect(imageCatalogSourceText('none')).toBe('没有读到发布目录');
    expect(imageCatalogSourceText(undefined)).toBe('未知');
  });
});

/* ============================================ 新契约可空性：commit / has_migration */

describe('新契约可空性：commit 与 has_migration 为 null 时的渲染', () => {
  it('提交为空时不渲染 "null" 或「缺失」占位，也不臆断迁移', () => {
    // availableView 的 newer / target 都按新契约给 null（registry 发现拿不到提交）。
    const markup = renderPanel({
      state: { ...initialImageUpdateState(), view: availableView() },
      defaultDetailsOpen: true,
    });
    expect(markup).not.toContain('null');
    expect(markup).not.toContain('（缺失）');
    expect(markup).not.toContain('提交 缺失');
    expect(markup).not.toContain('包含迁移');
    expect(markup).not.toContain('不需要迁移');
    // 唯一允许的迁移表述：判定归属交给迁移器。
    expect(markup).toContain('迁移由迁移器判断');
    expect(markup).toContain('0.0.4');
  });

  it('有构建提交时只显示短 sha，不回显整串', () => {
    const markup = renderPanel({
      state: {
        ...initialImageUpdateState(),
        view: availableView({ newer: [makeNewer({ commit: TARGET_COMMIT })] }),
      },
      defaultDetailsOpen: true,
    });
    expect(markup).toContain('（ddddddd）');
    expect(markup).not.toContain('null');
  });

  it('has_migration=false 也不翻译成「不需要迁移」或「包含迁移」', () => {
    const markup = renderPanel({
      state: {
        ...initialImageUpdateState(),
        view: availableView({ newer: [makeNewer({ has_migration: false })] }),
      },
      defaultDetailsOpen: true,
    });
    expect(markup).not.toContain('不需要迁移');
    expect(markup).not.toContain('包含迁移');
    expect(markup).not.toContain('null');
  });
});

/* ================================================================== 窄屏 */

describe('样式：375 / 1440 不横向溢出', () => {
  const css = readFileSync(new URL('./update-card.css', import.meta.url), 'utf8');
  const baseCss = readFileSync(new URL('./styles.css', import.meta.url), 'utf8');

  it('卡片样式不用 white-space: nowrap，长内容允许任意位置换行', () => {
    expect(css).not.toMatch(/white-space\s*:\s*nowrap/i);
    expect(css).toMatch(/overflow-wrap\s*:\s*anywhere/);
    expect(css).toMatch(/min-width\s*:\s*0/);
    expect(css).toMatch(/@media \(max-width: 640px\)/);
  });

  it('渲染出来的每个类名都真有样式（不会出现无样式的裸容器）', () => {
    const markup = [
      renderPanel({ state: { ...initialImageUpdateState(), view: makeView() } }),
      renderPanel({ state: { ...initialImageUpdateState(), view: availableView() } }),
      renderPanel({
        state: { ...initialImageUpdateState(), view: blockedView() },
        defaultDetailsOpen: true,
      }),
      renderPanel({
        state: { ...initialImageUpdateState(), view: makeView({ task: makeTask() }) },
        confirming: true,
        defaultDetailsOpen: true,
        submitError: { reason: '提交被拒绝', hint: '稍后重试', refresh: true },
      }),
    ].join('');
    const classNames = [...markup.matchAll(/class="([^"]+)"/g)]
      .flatMap((match) => (match[1] ?? '').split(/\s+/))
      .filter((name) => name.length > 0);
    expect(new Set(classNames).size).toBeGreaterThan(10);
    const unstyled = [...new Set(classNames)].filter(
      (name) => !new RegExp(`\\.${name}(?![\\w-])`).test(css + baseCss),
    );
    expect(unstyled, '这些类名没有对应样式').toEqual([]);
  });

  it('卡片不写死宽度、不用内联样式，长 digest 不会把页面撑开', () => {
    const markup = renderPanel({
      state: { ...initialImageUpdateState(), view: blockedView() },
      defaultDetailsOpen: true,
    });
    expect(markup).not.toMatch(/style="[^"]*width/i);
    expect(markup).not.toMatch(/style="[^"]*white-space/i);
    expect(css).not.toMatch(/\.update-card[^{]*\{[^}]*width\s*:\s*\d+px/);
    expect(css).not.toMatch(/\.update-notes[^{]*\{[^}]*width\s*:\s*\d+px/);
  });
});

/* ============================================================== 纯逻辑边界 */

describe('阶段与状态码只翻译成中文', () => {
  it('每个真实阶段都有中文标签，且不显示英文码值', () => {
    for (const stage of IMAGE_UPDATE_STAGES) {
      const label = imageUpdateStageLabel(stage);
      expect(label).not.toBe('');
      expect(label).not.toBe(IMAGE_STAGE_UNKNOWN_LABEL);
      expect(label).not.toMatch(/[A-Za-z]/);
    }
    expect(IMAGE_UPDATE_STAGES).toEqual(['prepare', 'backup', 'migrate', 'switch', 'verify']);
    expect(imageUpdateStageIndex('backup')).toBe(1);
    expect(imageUpdateStageIndex('download')).toBe(-1);
  });

  it('未知阶段有兜底，缺失阶段写「尚未开始」', () => {
    expect(IMAGE_STAGE_UNKNOWN_LABEL).toBe('处理中（阶段不可识别）');
    expect(imageUpdateStageLabel('download')).toBe(IMAGE_STAGE_UNKNOWN_LABEL);
    expect(imageUpdateStageLabel(null)).toBe('尚未开始');
    expect(imageUpdateStageLabel('')).toBe('尚未开始');
  });

  it('更新器状态与任务状态也用中文，未知状态不显示码值', () => {
    expect(imageUpdaterStateLabel('idle')).toBe('空闲');
    expect(imageUpdaterStateLabel('needs_manual')).toBe('需要人工处理');
    expect(imageUpdaterStateLabel('weird')).toBe('状态未知');
    expect(imageUpdateTaskStateText('running')).toBe('进行中');
    expect(imageUpdateTaskStateText('queued')).toBe('等待开始');
    expect(imageUpdateTaskStateText('failed_not_started')).toBe('失败（未进入迁移）');
    expect(imageUpdateTaskStateText('needs_manual')).toBe('需要人工处理');
    expect(imageUpdateTaskStateText('weird')).toBe('状态未识别');
    expect(imageUpdateTaskStateText(null)).toBe('没有任务记录');
  });

  it('任务活动判定只认进行中/排队（排队也认后端保留的 request 字段）', () => {
    expect(imageTaskActivity(null)).toBe('none');
    expect(imageTaskActivity(makeView({ task: makeTask({ state: 'running' }) }))).toBe('running');
    expect(imageTaskActivity(makeView({ task: makeTask({ state: 'queued' }) }))).toBe('queued');
    expect(imageTaskActivity(makeView({ task: makeTask({ state: 'succeeded' }) }))).toBe('none');
    expect(imageTaskActivity(makeView({ task: makeTask({ state: 'failed_not_started' }) }))).toBe(
      'none',
    );
    expect(
      imageTaskActivity(
        makeView({ request: { request_id: REQUEST_ID } }),
      ),
    ).toBe('queued');
  });

  it('任务行：进行中带版本与阶段，排队一句话', () => {
    expect(
      describeImageTaskLine(
        makeView({ task: makeTask({ state: 'running', stage: 'switch', version: '0.0.4' }) }),
      ),
    ).toBe('正在更新 v0.0.4：切换并启动新版本');
    expect(
      describeImageTaskLine(
        makeView({ task: makeTask({ state: 'running', stage: '', version: null }) }),
      ),
    ).toBe('正在更新：准备中');
    expect(
      describeImageTaskLine(makeView({ request: { request_id: REQUEST_ID } })),
    ).toBe('更新请求已提交，等待开始');
    expect(describeImageTaskLine(null)).toBeNull();
  });

  it('排队请求（request 非空、没有任务）：主按钮「查看进度」，结论说等待开始', () => {
    const view = makeView({ request: { request_id: REQUEST_ID } });
    const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
    expect(markup).toContain('更新请求已提交，等待开始');
    expect(primaryButton(markup)).toBe('查看进度');
    expect(actionKind(markup)).toBe('progress');
  });

  it('检查时间：相对与绝对口径都能读', () => {
    expect(formatImageCheckAge(iso(-30_000), NOW)).toBe('刚刚');
    expect(formatImageCheckAge(iso(-3 * 60_000), NOW)).toBe('3 分钟前');
    expect(formatImageCheckAge(iso(-2 * 60 * 60_000), NOW)).toBe('2 小时前');
    expect(formatImageCheckAge(iso(-30 * 60 * 60_000), NOW)).toBe('1 天前');
    expect(formatImageCheckAge(null, NOW)).toBeNull();
    expect(formatImageCheckAge('nonsense', NOW)).toBeNull();
    expect(formatImageCheckTime(null)).toBeNull();
    expect(formatImageCheckTime('nonsense')).toBeNull();
    expect(describeImageCheckedAt(null, NOW)).toBe('检查于 未知（还没有成功检查过）');
    expect(describeImageCheckedAt(iso(-3 * 60_000), NOW)).toContain('3 分钟前');
  });
});

/* ================================================================ 主按钮矩阵 */

describe('主按钮判定矩阵', () => {
  it('四态 + 权限 + 忙碌状态', () => {
    expect(imageUpdatePrimaryAction(makeView(), { canWrite: true })).toEqual({
      kind: 'check',
      label: '检查更新',
      disabled: false,
    });
    expect(imageUpdatePrimaryAction(availableView(), { canWrite: true }).label).toBe(
      '更新到 v0.0.4',
    );
    expect(imageUpdatePrimaryAction(blockedView(), { canWrite: true }).label).toBe('重新检查');
    expect(imageUpdatePrimaryAction(unknownView(), { canWrite: true }).label).toBe('重试检查');
    expect(imageUpdatePrimaryAction(availableView(), { canWrite: true, busy: true })).toEqual({
      kind: 'update',
      label: '正在检查…',
      disabled: true,
    });
    expect(imageUpdatePrimaryAction(availableView(), { canWrite: false }).kind).toBe('details');
    expect(imageUpdatePrimaryAction(makeView({ task: makeTask() }), { canWrite: false }).kind).toBe(
      'progress',
    );
    // 后端说可更新却没有目标：不给「更新到 v?」，退成重新检查。
    expect(imageUpdatePrimaryAction(availableView({ target: null }), { canWrite: true }).kind).toBe(
      'recheck',
    );
    expect(imageUpdatePrimaryAction(null, { canWrite: true }).kind).toBe('retry');
  });

  it('「更新到 vX」复查：目标版本与摘要都没变才进入确认', () => {
    const before = { version: '0.0.4', digest: TARGET_DIGEST, commit: TARGET_COMMIT };
    expect(imageUpdateConfirmAfterCheck(before, availableView())).toBe(true);
    expect(
      imageUpdateConfirmAfterCheck(
        before,
        availableView({ target: { ...before, digest: CURRENT_DIGEST } }),
      ),
    ).toBe(false);
    expect(
      imageUpdateConfirmAfterCheck(
        before,
        availableView({ target: { ...before, version: '0.0.5' } }),
      ),
    ).toBe(false);
    expect(imageUpdateConfirmAfterCheck(before, blockedView())).toBe(false);
    expect(imageUpdateConfirmAfterCheck(before, null)).toBe(false);
    expect(imageUpdateConfirmAfterCheck(null, availableView())).toBe(false);
    expect(IMAGE_UPDATE_REUSE_CHECK_MS).toBe(30000);
    expect(IMAGE_CHECK_POLL_MS).toBe(2000);
    expect(IMAGE_CHECK_TIMEOUT_MS).toBe(20000);
    expect(IMAGE_CHECK_DEBOUNCE_MS).toBe(10000);
  });
});

/* ============================================================ 轮询与提交体 */

describe('断线重连不清任务', () => {
  it('轮询失败保留视图与任务编号，恢复后仍是同一个任务', () => {
    const running = makeView({ task: makeTask({ state: 'running', stage: 'prepare' }) });
    const first = applyImageUpdatePoll(initialImageUpdateState(), running);
    expect(first.taskRequestId).toBe(REQUEST_ID);
    expect(first.connection).toBe('ok');

    const submitted = withSubmittedRequest(first, REQUEST_ID);
    const lost = applyImageUpdatePoll(submitted, null);
    expect(lost.connection).toBe('waiting');
    expect(lost.failures).toBe(1);
    expect(lost.view).toBe(first.view);
    expect(lost.taskRequestId).toBe(REQUEST_ID);
    expect(lost.submittedRequestId).toBe(REQUEST_ID);
    expect(describeImageConnection(lost)?.title).toBe('正在重新连接…');
    expect(describeImageConnection(lost)?.detail).toContain(REQUEST_ID);

    const recovered = applyImageUpdatePoll(
      lost,
      makeView({ task: makeTask({ state: 'running', stage: 'verify' }) }),
    );
    expect(recovered.connection).toBe('ok');
    expect(recovered.failures).toBe(0);
    expect(recovered.view?.task?.stage).toBe('verify');
    expect(describeImageConnection(recovered)).toBeNull();
  });

  it('退避节奏：断线 1.5/2.5/4/8 秒，进行中 3 秒，空闲 15 秒', () => {
    let state = initialImageUpdateState();
    expect(imagePollDelayMs(state)).toBe(3000);
    state = applyImageUpdatePoll(state, makeView());
    expect(imagePollDelayMs(state)).toBe(15000);
    state = applyImageUpdatePoll(state, makeView({ task: makeTask({ state: 'running' }) }));
    expect(imagePollDelayMs(state)).toBe(3000);
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

  it('断线时面板显示「正在重新连接」并保留旧任务行', () => {
    const first = applyImageUpdatePoll(
      initialImageUpdateState(),
      makeView({ task: makeTask({ state: 'running', stage: 'backup' }) }),
    );
    const lost = applyImageUpdatePoll(first, null);
    const markup = renderPanel({ state: lost });
    expect(markup).toContain('正在重新连接');
    expect(markup).toContain(REQUEST_ID);
    expect(markup).toContain('做一致备份（恢复点）');
    expect(markup).not.toContain('更新失败');
  });

  it('连续失败后给出可执行的恢复指引（仍不说成失败）', () => {
    let state = applyImageUpdatePoll(initialImageUpdateState(), null);
    expect(describeImageConnection(state)?.detail).not.toContain('确认后端进程');
    state = applyImageUpdatePoll(state, null);
    state = applyImageUpdatePoll(state, null);
    expect(describeImageConnection(state)?.detail).toContain('确认后端进程与网络可用');
    expect(describeImageConnection(state)?.title).toBe('正在重新连接…');
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

  it('请求体是固定 5 键，目标只来自可信目录的 target', () => {
    const view = availableView();
    const body = buildImageUpdateSubmission(view, view.target?.digest ?? null, REQUEST_ID);
    expect(body).not.toBeNull();
    expect(Object.keys(body ?? {}).sort()).toEqual([
      'expected_current_commit',
      'expected_current_digest',
      'operator_confirmed',
      'request_id',
      'target_digest',
    ]);
    expect(body?.operator_confirmed).toBe(true);
    expect(body?.expected_current_digest).toBe(CURRENT_DIGEST);
    expect(body?.expected_current_commit).toBe(CURRENT_COMMIT);
    expect(body?.target_digest).toBe(TARGET_DIGEST);
  });

  it('任务里的摘要不能回填成目标：没有 view.target 就不构造请求体', () => {
    const view = makeView({ task: makeTask({ version: '0.0.4' }) });
    expect(view.target).toBeNull();
    expect(buildImageUpdateSubmission(view, null, REQUEST_ID)).toBeNull();
  });

  it('目标缺失/格式不对/与当前相同/当前摘要未知：不构造请求体', () => {
    const view = availableView();
    expect(buildImageUpdateSubmission(view, 'ghcr.io/qsbb/ling-api:latest', REQUEST_ID)).toBeNull();
    expect(buildImageUpdateSubmission(view, CURRENT_DIGEST, REQUEST_ID)).toBeNull();
    expect(buildImageUpdateSubmission(view, TARGET_DIGEST, 'not-a-request-id')).toBeNull();
    expect(
      buildImageUpdateSubmission(
        availableView({ current: { version: '0.0.3', commit: CURRENT_COMMIT, digest: null } }),
        TARGET_DIGEST,
        REQUEST_ID,
      ),
    ).toBeNull();
  });

  it('提交错误：连接中断按「结果未知」处理，不让人重复提交', () => {
    const info = explainImageSubmitError(
      new ApiError(0, 'network_error', '提交时无法连接服务端，结果未知', true),
    );
    expect(info.reason).toContain('结果未知');
    expect(info.hint).toContain('不要重复提交');
    expect(info.refresh).toBe(true);

    const conflict = explainImageSubmitError(
      new ApiError(
        409,
        'conflict',
        '已有更新请求在进行中，不能重复提交',
        false,
        'update_in_progress',
      ),
    );
    expect(conflict.reason).toBe('已有更新请求在进行中，不能重复提交');
    expect(conflict.refresh).toBe(true);
    expect(explainImageSubmitError(new Error('boom')).refresh).toBe(true);
  });

  it('确认块写明停机与迁移不可撤销，未勾选不能提交', () => {
    const markup = renderPanel({
      state: { ...initialImageUpdateState(), view: availableView() },
      confirming: true,
    });
    expect(markup).toContain('短暂停机');
    expect(markup).toContain('不可撤销');
    expect(markup).toContain('data-testid="image-update-risk-ack"');
    expect(markup).toMatch(/data-testid="image-update-submit"[^>]*disabled=""/);

    const acknowledged = renderPanel({
      state: { ...initialImageUpdateState(), view: availableView() },
      confirming: true,
      riskAcknowledged: true,
    });
    expect(acknowledged).not.toMatch(/data-testid="image-update-submit"[^>]*disabled=""/);
  });

  it('所有状态都没有百分比/剩余时间，也没有回退按钮', () => {
    for (const view of [
      makeView(),
      availableView(),
      blockedView(),
      unknownView(),
      makeView({ task: makeTask({ state: 'running', stage: 'migrate' }) }),
      makeView({ task: makeTask({ state: 'failed_not_started', failed_code: 'backup_failed' }) }),
    ]) {
      const markup = renderPanel({ state: { ...initialImageUpdateState(), view } });
      expectNoFakeProgress(markup);
      expectNoRollbackButton(markup);
    }
  });
});

/* ================================================================ 页面装配 */

describe('页面装配', () => {
  it('ImageUpdatePage 用 initialView 直接渲染卡片，不再显示加载态', () => {
    const markup = renderToStaticMarkup(
      <ImageUpdatePage onBack={() => undefined} initialView={availableView()} initialCanWrite />,
    );
    expect(markup).toContain('data-testid="image-update-panel"');
    expect(markup).toContain('关于与版本');
    expect(markup).toContain('返回设置');
    expect(markup).toContain('更新到 v0.0.4');
    expectNoFakeProgress(markup);
    expectNoRollbackButton(markup);
  });

  it('AboutPage 探测镜像模式期间两套 UI 都不渲染（避免两个更新按钮同时出现）', () => {
    const markup = renderToStaticMarkup(<AboutPage onBack={() => undefined} />);
    expect(markup).toContain('读取更新通道');
    expect(markup).not.toContain('检查最新 Release');
    expect(markup).not.toContain('data-testid="image-update-panel"');
  });

  it('ImageUpdatePage 没给 initialCanWrite 时按只读渲染（fail closed）', () => {
    const markup = renderToStaticMarkup(
      <ImageUpdatePage onBack={() => undefined} initialView={availableView()} />,
    );
    expect(primaryButton(markup)).toBe('查看详情');
  });
});
