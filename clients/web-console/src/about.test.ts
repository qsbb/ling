/**
 * 关于与更新页的纯逻辑与 API 路由测试。
 *
 * 不引入 DOM 测试库：这里保护的是固定 API 路径/请求头、响应形状、
 * 安装目标的判断与状态文案；渲染细节仍由 typecheck/build 与
 * tests/e2e/update_browser.mjs 兜底。
 */
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api } from './api';
import {
  MANUAL_BLOCK_TEXT,
  buildUpdateRequest,
  describeJob,
  describeRelease,
  describeStage,
  describeUpdateReason,
  explainUpdateError,
  installBlocker,
  installTarget,
  isJobRunning,
  isSubmittedJob,
  jobBlocksNewUpdate,
  pollDelayMs,
  sameCommit,
  stageProgress,
  terminalJobIdentity,
  describeStatusNote,
} from './components/AboutPage';
import { parseRoute, routePath } from './route';
import type { ReleaseCheck, UpdateStatus, VersionInfo } from './types';
import type { UpdateJob } from './types';

const VERSION: VersionInfo = {
  schema_version: 'v1',
  current: '0.1.0',
  commit: '7b2370238f0b7e9da1b9e3f3f0b4a4d7f0b8c9a1',
  web_manifest_sha256: 'a'.repeat(64),
};

const TARGET_COMMIT = 'c0ffee1234567890abcdef1234567890abcdef12';

const RELEASE: ReleaseCheck = {
  schema_version: 'v1',
  status: 'ok',
  current: '0.1.0',
  latest: 'v0.2.0',
  published_at: '2026-10-30T00:00:00Z',
  html_url: 'https://github.com/qsbb/ling/releases/tag/v0.2.0',
  is_newer: true,
  release_id: 42,
  target_commit: TARGET_COMMIT,
  update_eligible: true,
  update_reason: null,
};

const STATUS: UpdateStatus = {
  schema_version: 'v1',
  enabled: true,
  reason: null,
  executor: { online: true, last_seen_at: '2026-10-30T10:00:00Z' },
  current_job: null,
};

const JOB: UpdateJob = {
  request_id: 'req-1',
  state: 'running',
  stage: 'build',
  target_tag: 'v0.2.0',
  target_commit: TARGET_COMMIT,
  created_at: '2026-10-30T10:00:00Z',
  updated_at: '2026-10-30T10:01:00Z',
  error_code: null,
  message: null,
};

/** installBlocker 的“一切齐备”输入；各用例只覆盖需要变化的字段。 */
const BASE_INPUT = {
  target: installTarget(RELEASE),
  version: VERSION,
  status: STATUS,
  job: null as UpdateJob | null,
  recheckRequired: false,
  connection: 'ok' as const,
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

describe('about route', () => {
  it('解析 /settings/about 并可反向生成同一路径', () => {
    expect(parseRoute('/settings/about')).toEqual({ name: 'about' });
    expect(parseRoute('/settings/about/')).toEqual({ name: 'about' });
    expect(routePath({ name: 'about' })).toBe('/settings/about');
  });

  it('不会把未知设置子路径误判成 about', () => {
    expect(parseRoute('/settings/about-extra')).toEqual({ name: 'chat', sessionId: null });
  });
});

describe('about api', () => {
  it('调用固定版本接口并保留响应形状', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(VERSION));
    vi.stubGlobal('fetch', fetchMock);

    await expect(api.version()).resolves.toEqual(VERSION);
    expect(fetchMock).toHaveBeenCalledWith('/v1/version', { credentials: 'same-origin' });
  });

  it('只通过固定路径手动检查 Release', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(RELEASE));
    vi.stubGlobal('fetch', fetchMock);

    await expect(api.checkRelease()).resolves.toEqual(RELEASE);
    expect(fetchMock).toHaveBeenCalledWith('/v1/version/check-release', {
      credentials: 'same-origin',
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('只读拉取更新状态且禁用缓存', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(STATUS));
    vi.stubGlobal('fetch', fetchMock);

    await expect(api.updateStatus()).resolves.toEqual(STATUS);
    expect(fetchMock).toHaveBeenCalledWith('/v1/version/update', {
      credentials: 'same-origin',
      cache: 'no-store',
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('轮询取消会原样抛出 AbortError，不伪装成网络故障', async () => {
    const abort = new DOMException('aborted', 'AbortError');
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(abort));
    const controller = new AbortController();

    await expect(api.updateStatus(controller.signal)).rejects.toBe(abort);
  });

  it('创建更新请求时带上 JSON 与 X-Ling-Update 标记', async () => {
    const created = jsonResponse({ ...JOB, state: 'queued', stage: 'queued' }, 201);
    const fetchMock = vi.fn().mockResolvedValue(created);
    vi.stubGlobal('fetch', fetchMock);

    const body = buildUpdateRequest(RELEASE, VERSION, ' v0.2.0 ');
    expect(body).not.toBeNull();
    await expect(api.requestUpdate(body!)).resolves.toMatchObject({ request_id: 'req-1' });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe('/v1/version/update-requests');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('same-origin');
    expect(init.headers).toEqual({
      'Content-Type': 'application/json',
      'X-Ling-Update': '1',
    });
    expect(JSON.parse(init.body as string)).toEqual({
      confirm_tag: 'v0.2.0',
      expected: {
        release_id: 42,
        tag: 'v0.2.0',
        commit: TARGET_COMMIT,
        current_version: '0.1.0',
        current_commit: VERSION.commit,
      },
    });
  });

  it('409 原样保留 detail，供页面要求重新检查', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse(
          {
            error: {
              code: 'conflict',
              message: 'release changed',
              retriable: false,
              detail: 'release_changed',
            },
          },
          409,
        ),
      ),
    );

    const error = await api.requestUpdate(buildUpdateRequest(RELEASE, VERSION, 'v0.2.0')!).catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(409);
    expect((error as ApiError).detail).toBe('release_changed');
  });
});

describe('installTarget', () => {
  it('eligible 且字段齐全时给出安装目标', () => {
    expect(installTarget(RELEASE)).toEqual({
      releaseId: 42,
      tag: 'v0.2.0',
      commit: TARGET_COMMIT,
    });
  });

  it('旧服务端缺新字段时不制造目标', () => {
    expect(installTarget({ ...RELEASE, release_id: undefined })).toBeNull();
    expect(installTarget({ ...RELEASE, target_commit: null })).toBeNull();
    expect(installTarget({ ...RELEASE, update_eligible: undefined })).toBeNull();
    expect(installTarget({ ...RELEASE, update_eligible: false })).toBeNull();
  });

  it('非 ok / 空标签 / 非法 release_id 都不算目标', () => {
    expect(installTarget({ ...RELEASE, status: 'no_release' })).toBeNull();
    expect(installTarget({ ...RELEASE, latest: '  ' })).toBeNull();
    expect(installTarget({ ...RELEASE, release_id: 0 })).toBeNull();
    expect(installTarget({ ...RELEASE, release_id: 1.5 })).toBeNull();
    expect(installTarget(null)).toBeNull();
  });
});

describe('buildUpdateRequest', () => {
  it('用当前版本与提交做乐观并发校验', () => {
    expect(buildUpdateRequest(RELEASE, VERSION, 'v0.2.0')).toEqual({
      confirm_tag: 'v0.2.0',
      expected: {
        release_id: 42,
        tag: 'v0.2.0',
        commit: TARGET_COMMIT,
        current_version: '0.1.0',
        current_commit: VERSION.commit,
      },
    });
  });

  it('缺少当前提交或目标时返回 null，调用方不得发请求', () => {
    expect(buildUpdateRequest(RELEASE, { ...VERSION, commit: null }, 'v0.2.0')).toBeNull();
    expect(buildUpdateRequest(RELEASE, null, 'v0.2.0')).toBeNull();
    expect(buildUpdateRequest({ ...RELEASE, update_eligible: false }, VERSION, 'v0.2.0')).toBeNull();
  });
});

describe('installBlocker', () => {
  const base = BASE_INPUT;

  it('条件齐备时可安装', () => {
    expect(installBlocker(base)).toBeNull();
  });

  it('未启用 / 非 owner / 离线 / 无提交都给出禁用原因', () => {
    expect(installBlocker({ ...base, status: { ...STATUS, enabled: false, reason: 'disabled' } })).toBe(
      '更新通道未启用',
    );
    expect(installBlocker({ ...base, status: { ...STATUS, reason: 'not_owner' } })).toBe(
      '只有所有者可以发起更新',
    );
    expect(
      installBlocker({
        ...base,
        status: { ...STATUS, executor: { online: false, last_seen_at: null } },
      }),
    ).toBe('更新执行器不在线');
    expect(installBlocker({ ...base, version: { ...VERSION, commit: null } })).toBe(
      '当前构建提交未知，无法校验更新结果',
    );
  });

  it('只挡 queued / running / needs_manual', () => {
    expect(installBlocker({ ...base, job: { ...JOB, state: 'queued', stage: 'queued' } })).toBe(
      '已有更新正在进行',
    );
    expect(installBlocker({ ...base, job: { ...JOB, state: 'running' } })).toBe('已有更新正在进行');
    expect(installBlocker({ ...base, job: { ...JOB, state: 'needs_manual', stage: 'verify' } })).toBe(
      MANUAL_BLOCK_TEXT,
    );
    // 后端 current_job 也按同一规则判定。
    expect(
      installBlocker({ ...base, status: { ...STATUS, current_job: { ...JOB, state: 'running' } } }),
    ).toBe('已有更新正在进行');
    expect(
      installBlocker({
        ...base,
        status: { ...STATUS, current_job: { ...JOB, state: 'needs_manual', stage: 'verify' } },
      }),
    ).toBe(MANUAL_BLOCK_TEXT);
  });

  it('历史 succeeded / failed 不再永久阻断下一次安装', () => {
    const done: UpdateJob = { ...JOB, state: 'succeeded', stage: 'complete' };
    const failed: UpdateJob = { ...JOB, state: 'failed', stage: 'deploy' };
    expect(installBlocker({ ...base, job: done })).toBeNull();
    expect(installBlocker({ ...base, job: failed })).toBeNull();
    expect(installBlocker({ ...base, status: { ...STATUS, current_job: done } })).toBeNull();
    expect(installBlocker({ ...base, status: { ...STATUS, current_job: failed } })).toBeNull();
  });

  it('没有进行中任务时，过期的 update_in_progress reason 不能永久禁用', () => {
    expect(installBlocker({ ...base, status: { ...STATUS, reason: 'update_in_progress' } })).toBeNull();
    expect(
      installBlocker({
        ...base,
        status: {
          ...STATUS,
          reason: 'update_in_progress',
          current_job: { ...JOB, state: 'succeeded', stage: 'complete' },
        },
      }),
    ).toBeNull();
  });

  it('过期结果、未读到状态、无目标、等待连接时都禁用', () => {
    expect(installBlocker({ ...base, recheckRequired: true })).toContain('重新检查');
    expect(installBlocker({ ...base, status: null })).toBe('正在读取更新状态');
    expect(installBlocker({ ...base, target: null })).toBe('还没有可安装的目标');
    expect(installBlocker({ ...base, connection: 'waiting' })).toBe('正在等待服务恢复');
  });
});

describe('任务状态判定', () => {
  it('isJobRunning 只认 queued / running', () => {
    expect(isJobRunning({ ...JOB, state: 'queued', stage: 'queued' })).toBe(true);
    expect(isJobRunning(JOB)).toBe(true);
    expect(isJobRunning({ ...JOB, state: 'succeeded', stage: 'complete' })).toBe(false);
    expect(isJobRunning({ ...JOB, state: 'failed' })).toBe(false);
    expect(isJobRunning({ ...JOB, state: 'needs_manual' })).toBe(false);
    expect(isJobRunning(null)).toBe(false);
  });

  it('jobBlocksNewUpdate 只挡 queued / running / needs_manual', () => {
    expect(jobBlocksNewUpdate({ ...JOB, state: 'queued' })).toBe(true);
    expect(jobBlocksNewUpdate(JOB)).toBe(true);
    expect(jobBlocksNewUpdate({ ...JOB, state: 'needs_manual' })).toBe(true);
    expect(jobBlocksNewUpdate({ ...JOB, state: 'succeeded', stage: 'complete' })).toBe(false);
    expect(jobBlocksNewUpdate({ ...JOB, state: 'failed' })).toBe(false);
    expect(jobBlocksNewUpdate(null)).toBe(false);
  });

  it('isSubmittedJob 只把新 request_id 认成本次提交', () => {
    // POST 响应丢失后，状态里仍是提交前的旧任务：不能当本次提交的结果。
    expect(isSubmittedJob({ ...JOB, request_id: 'req-old' }, 'req-old')).toBe(false);
    expect(isSubmittedJob({ ...JOB, request_id: 'req-new' }, 'req-old')).toBe(true);
    expect(isSubmittedJob({ ...JOB, request_id: 'req-new' }, null)).toBe(true);
    expect(isSubmittedJob(null, 'req-old')).toBe(false);
    expect(isSubmittedJob(null, null)).toBe(false);
  });

  it('isSubmittedJob 传入目标时还要求 tag/commit 一致', () => {
    const target = { tag: 'v0.2.0', commit: TARGET_COMMIT };
    expect(isSubmittedJob({ ...JOB, request_id: 'req-new' }, 'req-old', target)).toBe(true);
    expect(
      isSubmittedJob({ ...JOB, request_id: 'req-new', target_tag: 'v0.9.0' }, 'req-old', target),
    ).toBe(false);
    expect(
      isSubmittedJob(
        { ...JOB, request_id: 'req-new', target_commit: 'f'.repeat(40) },
        'req-old',
        target,
      ),
    ).toBe(false);
  });

  it('terminalJobIdentity 按 request_id:state 区分同状态的新终态', () => {
    expect(terminalJobIdentity(null)).toBeNull();
    expect(terminalJobIdentity(JOB)).toBeNull();
    expect(terminalJobIdentity({ ...JOB, request_id: 'req-old', state: 'succeeded' })).toBe(
      'req-old:succeeded',
    );
    expect(terminalJobIdentity({ ...JOB, request_id: 'req-new', state: 'succeeded' })).not.toBe(
      terminalJobIdentity({ ...JOB, request_id: 'req-old', state: 'succeeded' }),
    );
    expect(
      terminalJobIdentity({ ...JOB, request_id: 'req-new', state: 'failed' }),
    ).toBe('req-new:failed');
  });

  it('describeStatusNote 不把过期的 update_in_progress 说成“已有更新正在进行”', () => {
    expect(
      describeStatusNote(
        { ...STATUS, reason: 'update_in_progress', current_job: null },
        null,
      ),
    ).toContain('当前没有可核实的任务');
    expect(
      describeStatusNote(
        { ...STATUS, reason: 'update_in_progress', current_job: { ...JOB, state: 'running' } },
        null,
      ),
    ).toBe('已有更新正在进行');
    expect(
      describeStatusNote(
        { ...STATUS, reason: 'update_in_progress', current_job: null },
        { ...JOB, state: 'running' },
      ),
    ).toBe('已有更新正在进行');
    expect(
      describeStatusNote(
        {
          ...STATUS,
          reason: 'not_owner',
          current_job: { ...JOB, state: 'needs_manual', stage: 'verify' },
        },
        null,
      ),
    ).toBe(MANUAL_BLOCK_TEXT);
    expect(describeStatusNote({ ...STATUS, reason: 'not_owner' }, null)).toBe(
      '只有所有者可以发起更新',
    );
    expect(describeStatusNote({ ...STATUS, enabled: false, reason: 'disabled' }, null)).toBeNull();
    expect(describeStatusNote({ ...STATUS, reason: null }, null)).toBeNull();
  });

  it('pollDelayMs：进行中固定 3 秒，断连/待确认退避且封顶', () => {
    expect(pollDelayMs(0, 'job')).toBe(3000);
    expect(pollDelayMs(9, 'job')).toBe(3000);
    expect(pollDelayMs(0, 'recover')).toBe(1500);
    const delays = [0, 1, 2, 3, 9].map((count) => pollDelayMs(count, 'recover'));
    expect(delays).toEqual([...delays].sort((a, b) => a - b));
    expect(pollDelayMs(1, 'recover')).toBeGreaterThan(pollDelayMs(0, 'recover'));
    expect(pollDelayMs(9, 'recover')).toBe(8000);
  });
});

describe('状态文案', () => {
  it('覆盖后端 reason 枚举', () => {
    expect(describeUpdateReason('disabled')).toBe('更新通道未启用');
    expect(describeUpdateReason('not_owner')).toContain('所有者');
    expect(describeUpdateReason('executor_offline')).toBe('更新执行器不在线');
    expect(describeUpdateReason('channel_unavailable')).toBe('更新通道不可用');
    expect(describeUpdateReason('installed_commit_unknown')).toContain('构建提交');
    expect(describeUpdateReason('version_drift')).toContain('不一致');
    expect(describeUpdateReason('update_in_progress')).toBe('已有更新正在进行');
    expect(describeUpdateReason('release_unverifiable')).toContain('无法校验');
    expect(describeUpdateReason('not_newer')).toContain('不比当前版本更新');
    expect(describeUpdateReason(null)).toBe('更新通道未启用');
    expect(describeUpdateReason('something_new')).toContain('something_new');
  });

  it('覆盖 stage 枚举且进度单调递增', () => {
    const stages = [
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
    let last = 0;
    for (const stage of stages) {
      expect(describeStage(stage)).not.toBe('');
      const value = stageProgress(stage);
      expect(value).toBeGreaterThan(last);
      last = value;
    }
    expect(stageProgress('complete')).toBe(100);
    expect(stageProgress('unknown')).toBe(0);
  });
});

describe('门禁原因文案（manual_required / protocol_unsupported）', () => {
  it('manual_required 有专门中文文案，不再显示原样码', () => {
    const text = describeUpdateReason('manual_required');
    expect(text).not.toContain('当前不可更新');
    expect(text).not.toContain('manual_required');
    expect(text).toContain('人工核验');
    expect(text).toContain('adopt-current');
    expect(text).toContain('宿主机');
  });

  it('protocol_unsupported 说明需同批升级、重试不会生效', () => {
    const text = describeUpdateReason('protocol_unsupported');
    expect(text).not.toContain('当前不可更新');
    expect(text).not.toContain('protocol_unsupported');
    expect(text).toContain('协议不兼容');
    expect(text).toContain('同批升级');
    expect(text).toContain('重试不会生效');
  });

  it('两条文案彼此可区分，且都不承诺自动恢复', () => {
    const manual = describeUpdateReason('manual_required');
    const protocol = describeUpdateReason('protocol_unsupported');
    expect(manual).not.toBe(protocol);
    expect(manual).not.toBe(MANUAL_BLOCK_TEXT);
    for (const text of [manual, protocol]) {
      expect(text).not.toContain('自动重试');
      expect(text).not.toContain('忽略');
      expect(text).not.toContain('已取消');
    }
  });

  it('manual_required 是门禁原因，needs_manual 是任务终态，两者文案不同来源', () => {
    // needs_manual 走任务终态：任务卡片 + MANUAL_BLOCK_TEXT，不是 REASON_TEXT 的键。
    expect(installBlocker({ ...BASE_INPUT, job: { ...JOB, state: 'needs_manual', stage: 'verify' } })).toBe(
      MANUAL_BLOCK_TEXT,
    );
    // 门禁原因走 REASON_TEXT，文案明确指向宿主机处理，而不是“上一次更新”。
    expect(describeUpdateReason('manual_required')).toContain('人工核验');
    expect(describeUpdateReason('manual_required')).not.toBe(MANUAL_BLOCK_TEXT);
    expect(MANUAL_BLOCK_TEXT).toContain('上一次更新');
  });

  it('界面“当前状态”栏渲染两条门禁文案', () => {
    expect(describeStatusNote({ ...STATUS, reason: 'manual_required' }, null)).toBe(
      describeUpdateReason('manual_required'),
    );
    expect(describeStatusNote({ ...STATUS, reason: 'protocol_unsupported' }, null)).toBe(
      describeUpdateReason('protocol_unsupported'),
    );
  });
});

describe('门禁原因禁用安装按钮且没有解除入口', () => {
  it('两条新门禁原因都返回禁用文案（安装按钮 disabled={blocker !== null}）', () => {
    for (const reason of ['manual_required', 'protocol_unsupported']) {
      const blocker = installBlocker({ ...BASE_INPUT, status: { ...STATUS, reason } });
      expect(blocker).toBe(describeUpdateReason(reason));
      expect(blocker).not.toBeNull();
    }
  });

  it('即使已经拿到目标与当前版本，也照样禁用（门禁先于目标判断）', () => {
    const withTarget = { ...BASE_INPUT, target: installTarget(RELEASE), version: VERSION };
    expect(
      installBlocker({ ...withTarget, status: { ...STATUS, reason: 'manual_required' } }),
    ).toBe(describeUpdateReason('manual_required'));
    expect(
      installBlocker({ ...withTarget, status: { ...STATUS, reason: 'protocol_unsupported' } }),
    ).toBe(describeUpdateReason('protocol_unsupported'));
  });

  it('历史 succeeded / failed 任务不能绕过这两条门禁', () => {
    for (const state of ['succeeded', 'failed'] as const) {
      const job: UpdateJob = { ...JOB, state, stage: state === 'succeeded' ? 'complete' : 'deploy' };
      expect(installBlocker({ ...BASE_INPUT, job, status: { ...STATUS, reason: 'manual_required' } })).toBe(
        describeUpdateReason('manual_required'),
      );
      expect(
        installBlocker({ ...BASE_INPUT, job, status: { ...STATUS, reason: 'protocol_unsupported' } }),
      ).toBe(describeUpdateReason('protocol_unsupported'));
    }
  });

  it('既有门禁原因行为未被破坏', () => {
    const cases: Array<[string, string]> = [
      ['disabled', '更新通道未启用'],
      ['not_owner', '只有所有者可以发起更新'],
      ['executor_offline', '更新执行器不在线'],
      ['channel_unavailable', '更新通道不可用'],
      ['installed_commit_unknown', '当前构建提交未知，无法校验更新结果'],
      ['version_drift', '运行版本与安装记录不一致'],
      ['update_in_progress', '已有更新正在进行'],
      ['release_unverifiable', '无法校验该 Release 的目标提交'],
      ['not_newer', '该 Release 不比当前版本更新'],
    ];
    for (const [reason, text] of cases) {
      expect(describeUpdateReason(reason)).toBe(text);
      if (reason === 'update_in_progress') {
        // 过期字段不永久禁用；有 running 任务时才禁用（由任务分支负责）。
        expect(installBlocker({ ...BASE_INPUT, status: { ...STATUS, reason } })).toBeNull();
        expect(
          installBlocker({
            ...BASE_INPUT,
            status: { ...STATUS, reason, current_job: { ...JOB, state: 'running' } },
          }),
        ).toBe(text);
      } else {
        expect(installBlocker({ ...BASE_INPUT, status: { ...STATUS, reason } })).toBe(text);
      }
    }
    // needs_manual 仍是任务终态文案，不是门禁文案。
    expect(installBlocker({ ...BASE_INPUT, job: { ...JOB, state: 'needs_manual', stage: 'verify' } })).toBe(
      MANUAL_BLOCK_TEXT,
    );
    expect(installBlocker({ ...BASE_INPUT, job: { ...JOB, state: 'queued', stage: 'queued' } })).toBe(
      '已有更新正在进行',
    );
  });

  it('安装/确认按钮仍由 blocker 控制，且源码没有忽略/取消/强制/自动重试入口', () => {
    const source = readFileSync(new URL('./components/AboutPage.tsx', import.meta.url), 'utf8');
    // 安装与确认提交都只看 blocker（门禁原因 → 按钮 disabled）。
    expect(source).toContain('disabled={blocker !== null || submitting}');
    expect(source).toContain('disabled={!tagMatches || submitting || blocker !== null}');
    // 没有为门禁原因另造解除入口。
    for (const testId of [
      'update-ignore',
      'update-dismiss',
      'update-force',
      'update-skip',
      'update-cancel',
      'update-auto-retry',
    ]) {
      expect(source).not.toContain(testId);
    }
    // 没有针对门禁原因的忽略/清除/强制放行函数。
    expect(source).not.toMatch(/function\s+(ignore|dismiss|forceUnlock|clearGate)\w*\s*\(/);
  });
});

describe('describeJob', () => {
  it('queued/running 显示阶段与目标，不提前说完成', () => {
    const view = describeJob(JOB, VERSION.commit, 'ok');
    expect(view.kind).toBe('progress');
    expect(view.title).toBe('正在更新');
    expect(view.detail).toContain('构建');
    expect(view.detail).toContain('v0.2.0');
    expect(view.detail).not.toContain('更新完成');
  });

  it('断连时等待恢复，不算完成也不判失败', () => {
    const view = describeJob(JOB, VERSION.commit, 'waiting');
    expect(view.kind).toBe('waiting');
    expect(view.detail).toContain('不等于更新完成或失败');
  });

  it('succeeded 且 commit 相等才算完成', () => {
    const view = describeJob(
      { ...JOB, state: 'succeeded', stage: 'complete' },
      TARGET_COMMIT,
      'ok',
    );
    expect(view.kind).toBe('succeeded');
    expect(view.title).toBe('更新完成');
  });

  it('succeeded 但服务端仍是旧提交时继续等待，不谎报完成', () => {
    const view = describeJob(
      { ...JOB, state: 'succeeded', stage: 'complete' },
      VERSION.commit,
      'ok',
    );
    expect(view.kind).toBe('verifying');
    expect(view.title).toContain('等待新版本生效');
    expect(view.detail).toContain('不会自动刷新');
  });

  it('没有构建提交时明确说无法校验', () => {
    const view = describeJob(
      { ...JOB, state: 'succeeded', stage: 'complete' },
      null,
      'ok',
    );
    expect(view.kind).toBe('unverifiable');
    expect(view.detail).toContain('LING_COMMIT');
  });

  it('failed 指向宿主日志且不承诺自动回滚', () => {
    const view = describeJob(
      {
        ...JOB,
        state: 'failed',
        stage: 'deploy',
        error_code: 'deploy_failed',
        message: '容器退出',
      },
      VERSION.commit,
      'ok',
    );
    expect(view.kind).toBe('failed');
    expect(view.title).toBe('更新失败');
    expect(view.detail).toContain('查看宿主更新日志');
    expect(view.detail).toContain('不会自动回滚');
  });

  it('needs_manual 要求人工处理，不提自动恢复', () => {
    const view = describeJob(
      { ...JOB, state: 'needs_manual', stage: 'verify' },
      VERSION.commit,
      'ok',
    );
    expect(view.kind).toBe('manual');
    expect(view.title).toBe('需要人工处理');
    expect(view.detail).toContain('手动处理');
    expect(view.detail).toContain('adopt-current');
    expect(view.detail).toContain('不会自动回滚');
    expect(view.detail).toContain('数据库也不会自动恢复');
  });
});

describe('sameCommit', () => {
  it('忽略大小写与首尾空白，空值不算相等', () => {
    expect(sameCommit(TARGET_COMMIT, TARGET_COMMIT.toUpperCase())).toBe(true);
    expect(sameCommit(` ${TARGET_COMMIT} `, TARGET_COMMIT)).toBe(true);
    expect(sameCommit(null, TARGET_COMMIT)).toBe(false);
    expect(sameCommit(VERSION.commit, TARGET_COMMIT)).toBe(false);
  });
});

describe('explainUpdateError', () => {
  it('409 / release_changed 要求重新检查而不是重发', () => {
    const info = explainUpdateError(
      new ApiError(409, 'conflict', 'conflict', false, 'release_changed'),
    );
    expect(info.reason).toContain('Release');
    expect(info.needsRecheck).toBe(true);
    expect(info.connectionLost).toBe(false);
    expect(info.hint).toContain('不要直接重发');
  });

  it('安装版本变化同样要求重新检查', () => {
    const info = explainUpdateError(
      new ApiError(409, 'conflict', 'conflict', false, 'installed_version_changed'),
    );
    expect(info.reason).toContain('当前运行的版本');
    expect(info.needsRecheck).toBe(true);
  });

  it('断连标记为结果未知，不判失败', () => {
    const info = explainUpdateError(new ApiError(0, 'network_error', '无法连接服务端', true));
    expect(info.connectionLost).toBe(true);
    expect(info.needsRecheck).toBe(false);
    expect(info.hint).toContain('不要重复提交');
  });

  it('owner/执行器类错误给出可读原因但不要求重新检查', () => {
    const info = explainUpdateError(new ApiError(403, 'forbidden', 'forbidden', false, 'not_owner'));
    expect(info.reason).toBe('只有所有者可以发起更新');
    expect(info.needsRecheck).toBe(false);
  });

  // 提交路径的 503 复用 `provider_unavailable` 错误码（后端 _gate_error），
  // 与“模型供应商”无关。以下 4 个用例覆盖两条门禁 reason × 两种实际响应形态：
  // 带 gate detail（用户已确认 target 后提交），以及 detail 缺失/未知（代理改写或未来新码）；
  // 末尾再补一个同类补充（disabled 也是 503 + provider_unavailable）。
  it('503 gate manual_required（带 detail）：按门禁文案显示，绝不显示模型供应商', () => {
    const info = explainUpdateError(
      new ApiError(
        503,
        'provider_unavailable',
        '更新执行器需要人工恢复（adopt-current）后才能继续',
        true,
        'manual_required',
      ),
    );
    // 与状态栏/安装按钮共用同一句文案（GATE_REASON_TEXT）
    expect(info.reason).toBe(describeUpdateReason('manual_required'));
    expect(info.reason).toContain('人工核验');
    expect(info.reason).toContain('adopt-current');
    expect(info.reason).not.toContain('模型供应商');
    expect(info.hint).not.toContain('供应商');
    expect(info.hint).not.toContain('密钥');
    expect(info.needsRecheck).toBe(false);
    expect(info.connectionLost).toBe(false);
  });

  it('503 gate protocol_unsupported（带 detail）：说明同批升级、重试无效，不显示模型供应商', () => {
    const info = explainUpdateError(
      new ApiError(
        503,
        'provider_unavailable',
        '更新通道协议不兼容或请求文件无法识别，需要人工处理',
        true,
        'protocol_unsupported',
      ),
    );
    expect(info.reason).toBe(describeUpdateReason('protocol_unsupported'));
    expect(info.reason).toContain('协议不兼容');
    expect(info.reason).toContain('同批升级');
    expect(info.reason).toContain('重试不会生效');
    expect(info.reason).not.toContain('模型供应商');
    expect(info.hint).not.toContain('供应商');
    expect(info.hint).not.toContain('密钥');
    expect(info.needsRecheck).toBe(false);
  });

  it('503 gate manual_required（detail 缺失）：退回门禁兜底文案，仍不显示模型供应商', () => {
    const info = explainUpdateError(
      new ApiError(503, 'provider_unavailable', '更新执行器需要人工恢复（adopt-current）后才能继续', true),
    );
    expect(info.reason).toBe('当前不满足更新条件');
    expect(info.reason).not.toContain('模型供应商');
    expect(info.hint).not.toContain('供应商');
    expect(info.hint).not.toContain('密钥');
    expect(info.needsRecheck).toBe(false);
    // 兜底只针对 503 + provider_unavailable：其它错误码仍走自己的文案，不被吞掉
    expect(
      explainUpdateError(new ApiError(503, 'provider_error', '模型供应商返回错误', true)).reason,
    ).toBe('模型供应商返回错误');
  });

  it('503 gate protocol_unsupported（detail 是未知新码）：同样不显示模型供应商', () => {
    const info = explainUpdateError(
      new ApiError(503, 'provider_unavailable', '当前不满足更新条件', true, 'future_gate_reason'),
    );
    expect(info.reason).toBe('当前不满足更新条件');
    expect(info.reason).not.toContain('模型供应商');
    expect(info.hint).not.toContain('供应商');
    expect(info.hint).not.toContain('密钥');
    expect(info.needsRecheck).toBe(false);
    expect(info.connectionLost).toBe(false);
  });

  it('其余 gate reason 的 503 同样用门禁文案（如 disabled 不再是模型供应商错误）', () => {
    const info = explainUpdateError(
      new ApiError(503, 'provider_unavailable', 'Web 更新通道未启用', true, 'disabled'),
    );
    expect(info.reason).toBe('更新通道未启用');
    expect(info.reason).not.toContain('模型供应商');
    expect(info.hint).not.toContain('供应商');
  });

  // 后端 422 的 detail 是 `invalid_request_body`（`version.py:68`、`:747`），不是
  // `invalid_request`：`UPDATE_DETAIL_TEXT` 两个键都保留（后者为旧键别名），否则
  // 非法 body 在提交横幅会落到 `errors.ts` 的通用“请求参数不合法”。
  it('422 / invalid_request_body：显示专门的确认失效文案，而不是通用请求参数文案', () => {
    const info = explainUpdateError(
      new ApiError(422, 'invalid_request', '更新请求格式无效', false, 'invalid_request_body'),
    );
    expect(info.reason).toBe('确认信息不完整或已失效');
    expect(info.reason).not.toBe('请求参数不合法');
    expect(info.needsRecheck).toBe(false);
    expect(info.connectionLost).toBe(false);
  });

  it('旧 detail 键 invalid_request 仍命中同一句文案（兼容保留）', () => {
    const info = explainUpdateError(
      new ApiError(422, 'invalid_request', '更新请求格式无效', false, 'invalid_request'),
    );
    expect(info.reason).toBe('确认信息不完整或已失效');
  });

  it('其余 422 detail 不受影响：仍走通用“请求参数不合法”', () => {
    const details = [
      'request_body_too_large',
      'content_type_required',
      'update_header_required',
      'origin_required',
      'origin_mismatch',
    ];
    for (const detail of details) {
      const info = explainUpdateError(
        new ApiError(422, 'invalid_request', '更新请求格式无效', false, detail),
      );
      expect(info.reason).toBe('请求参数不合法');
      expect(info.needsRecheck).toBe(false);
    }
  });
});

describe('describeRelease', () => {
  it('可安装时提示在下方确认', () => {
    const view = describeRelease(RELEASE);
    expect(view.tone).toBe('ok');
    expect(view.title).toBe('有新版本可用');
    expect(view.detail).toContain('v0.2.0');
    expect(view.detail).toContain('下方确认安装');
  });

  it('目标信息不全时退回部署流程，不制造可安装目标', () => {
    const view = describeRelease({ ...RELEASE, target_commit: null });
    expect(view.detail).toContain('部署流程');
  });

  it('相同或更旧版本显示已是最新', () => {
    const view = describeRelease({ ...RELEASE, latest: '0.1.0', is_newer: false });
    expect(view.title).toBe('已是最新');
  });

  it('无 Release 与无法检查是不同状态', () => {
    expect(
      describeRelease({
        ...RELEASE,
        status: 'no_release',
        latest: null,
        is_newer: null,
      }).title,
    ).toBe('当前没有 Release');
    expect(
      describeRelease({
        ...RELEASE,
        status: 'unavailable',
        latest: null,
        is_newer: null,
      }).title,
    ).toBe('暂时无法检查 Release');
  });

  it('无法比较的标签不冒充已是最新', () => {
    const view = describeRelease({ ...RELEASE, latest: 'nightly', is_newer: null });
    expect(view.tone).toBe('warn');
    expect(view.title).toContain('无法比较');
  });
});
