/**
 * 记忆内容页（/memory）的回归测试。
 *
 * 这一页最容易出的两类错，测试都盯着：
 *
 * 1. **整页被一个错误盖住**。核心记忆 403 时列表必须照常显示，反之亦然——
 *    所以 `MemoryContentBody` 被单独渲染，用「核心失败 + 列表成功」证明互不传染。
 * 2. **假装能力存在**。没有分页、没有单条详情端点、没有后端 kind/时间筛选，
 *    所以测试同时断言界面**没有**「下一页」，且截断说明必须出现「没有分页」。
 *
 * 没有 DOM 测试库（仓库的取舍与 `dashboard.test.ts` / `about.test.ts` 一致）：
 * 用 `react-dom/server` 渲染受控组件做结构断言；交互（撤回、include=all、
 * forget 确认、375px）交给 `tests/e2e/memory_browser.mjs` 的真 Chromium。
 * 确认流程本身的状态迁移用纯 reducer 单测覆盖。
 */
import { createElement as h, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  AWARENESS_TERM,
  DEFAULT_FILTERS,
  INITIAL_FORGET,
  MEMORY_POLICY_PUBLISHED_MESSAGE,
  MEMORY_POLICY_STATUS,
  MemoryContentBody,
  MemoryDangerZone,
  MemoryListSection,
  MemoryPage,
  MemorySettingsTab,
  canSubmitForget,
  confidenceLabel,
  coreOriginLabel,
  filterMemories,
  forgetReducer,
  formatAbsoluteTime,
  formatRelativeTime,
  groupByKind,
  includeMode,
  includeSummary,
  kindLabel,
  limitNotice,
  memoryErrorCopy,
  memoryFooterNote,
  memoryTabFromHref,
  memoryTabHref,
  originLabel,
  sortByCreatedDesc,
  type ForgetState,
  type MemoryHandlers,
  type SectionState,
  type TombstoneFlags,
} from './components/MemoryPage';
import { CoreMemorySection } from './components/MemoryPage';
import { parseRoute, routePath } from './route';
import type {
  CoreMemoryItem,
  CoreMemoryListResponse,
  MemoryListResponse,
  MemoryRecord,
} from './types';

/* ------------------------------------------------------------------ 夹具 */

const MEM_A: MemoryRecord = {
  memory_id: 'mem_a',
  kind: 'assertion',
  text: '她住在杭州',
  origin: 'user_statement',
  visibility: 'subject_private',
  confidence: 0.8,
  retracted: false,
  retract_reason: null,
  supersedes: null,
  superseded_by: null,
  source_event_ids: ['turn:1'],
  valid_until: null,
  created_at: '2026-10-30T10:00:00+00:00',
  archived_at: null,
};

const MEM_B_TOMBSTONE: MemoryRecord = {
  ...MEM_A,
  memory_id: 'mem_b',
  text: '她搬到上海了',
  retracted: true,
  retract_reason: '地址过时',
  superseded_by: 'mem_c',
  created_at: '2026-10-01T10:00:00+00:00',
};

const MEM_C_ARCHIVED: MemoryRecord = {
  ...MEM_A,
  memory_id: 'mem_c',
  kind: 'episode',
  text: '去年冬天一起看过雪',
  origin: 'external_observation',
  created_at: '2025-12-01T10:00:00+00:00',
  archived_at: '2026-09-01T00:00:00+00:00',
};

function listResponse(rows: MemoryRecord[], overrides: Partial<MemoryListResponse> = {}): MemoryListResponse {
  return {
    memories: rows,
    limit: 50,
    total: rows.length,
    has_more: false,
    include: null,
    note: '默认只含未撤回且未归档的记忆',
    ...overrides,
  };
}

function ok<T>(data: T): SectionState<T> {
  return { status: 'ok', data, error: null };
}

const HANDLERS: MemoryHandlers = {
  retract: async () => undefined,
  supersede: async () => undefined,
  saveCore: async () => undefined,
  deleteCore: async () => undefined,
  forget: async () => 0,
  reloadCore: () => undefined,
  reloadList: () => undefined,
};

const FLAGS_OFF: TombstoneFlags = { retracted: false, archived: false };

function renderList(
  props: Partial<Parameters<typeof MemoryListSection>[0]> & {
    state?: SectionState<MemoryListResponse>;
  } = {},
): string {
  const state = props.state ?? ok(listResponse([MEM_A]));
  return renderToStaticMarkup(
    h(MemoryListSection, {
      state,
      include: props.include ?? null,
      flags: props.flags ?? FLAGS_OFF,
      onFlagsChange: props.onFlagsChange ?? (() => undefined),
      limit: props.limit ?? 50,
      onLimitChange: props.onLimitChange ?? (() => undefined),
      filters: props.filters ?? DEFAULT_FILTERS,
      onFiltersChange: props.onFiltersChange ?? (() => undefined),
      handlers: props.handlers ?? HANDLERS,
      onReload: props.onReload ?? (() => undefined),
    }),
  );
}

function renderBody(
  core: SectionState<CoreMemoryListResponse>,
  list: SectionState<MemoryListResponse>,
  overrides: Partial<Parameters<typeof MemoryContentBody>[0]> = {},
): string {
  return renderToStaticMarkup(
    h(MemoryContentBody, {
      core,
      list,
      include: overrides.include ?? null,
      flags: overrides.flags ?? FLAGS_OFF,
      onFlagsChange: overrides.onFlagsChange ?? (() => undefined),
      limit: overrides.limit ?? 50,
      onLimitChange: overrides.onLimitChange ?? (() => undefined),
      filters: overrides.filters ?? DEFAULT_FILTERS,
      onFiltersChange: overrides.onFiltersChange ?? (() => undefined),
      forget: overrides.forget ?? INITIAL_FORGET,
      onForgetAction: overrides.onForgetAction ?? (() => undefined),
      handlers: overrides.handlers ?? HANDLERS,
      notice: overrides.notice ?? null,
      onDismissNotice: overrides.onDismissNotice ?? (() => undefined),
      onReloadAll: overrides.onReloadAll ?? (() => undefined),
    }),
  );
}

const CORE_ITEM: CoreMemoryItem = {
  core_id: 'core_1',
  label: '称呼',
  text: '叫我小林',
  priority: 10,
  scope: 'private',
  origin_kind: 'manual',
  created_at: '2026-10-01T09:00:00+00:00',
  updated_at: '2026-10-20T09:00:00+00:00',
};

function coreResponse(items: CoreMemoryItem[], limit = 20): CoreMemoryListResponse {
  return { items, limit, note: '核心记忆每轮注入' };
}

function renderDanger(state: ForgetState): string {
  return renderToStaticMarkup(
    h(MemoryDangerZone, {
      state,
      dispatch: () => undefined,
      handlers: HANDLERS,
    }),
  );
}

function withNode(markup: ReactNode): string {
  return renderToStaticMarkup(h('div', null, markup));
}

/* ------------------------------------------------------------------ 路由 */

describe('记忆页路由（/memory）', () => {
  it('/memory 解析成 memory 路由，routePath 生成 /memory', () => {
    expect(parseRoute('/memory')).toEqual({ name: 'memory' });
    expect(parseRoute('/memory/')).toEqual({ name: 'memory' });
    expect(routePath({ name: 'memory' })).toBe('/memory');
  });

  it('旧路径 /settings/memory 现在是指向 /memory?tab=settings 的重定向', () => {
    // Step 4 之前它解析成 memory（一页内容）；现在它是重定向表的精确条目，
    // 由壳层 replace 到带 tab 的新地址。`memoryTabFromHref` 仍认它，
    // 所以首帧开的 tab 与 replace 之后一致（不会先开错再跳一次）。
    expect(parseRoute('/settings/memory')).toEqual({
      name: 'legacyRedirect',
      to: '/memory?tab=settings',
    });
  });

  it('tab 深链来自 query；旧路径默认开调参 tab', () => {
    expect(memoryTabFromHref('/memory')).toBe('content');
    expect(memoryTabFromHref('/memory?tab=settings')).toBe('settings');
    expect(memoryTabFromHref('/memory?tab=settings&x=1')).toBe('settings');
    // 不认识的 tab 值不猜，回到内容 tab
    expect(memoryTabFromHref('/memory?tab=whatever')).toBe('content');
    // 旧书签：/settings/memory 打开的是「记忆与学习」调参页
    expect(memoryTabFromHref('/settings/memory')).toBe('settings');
  });

  it('tab 链接是可深链的路径，而不是 hash', () => {
    expect(memoryTabHref('content')).toBe('/memory');
    expect(memoryTabHref('settings')).toBe('/memory?tab=settings');
    expect(memoryTabHref('settings')).not.toContain('#');
  });
});

/* ------------------------------------------------------------------ 渲染 */

describe('记忆页整体渲染', () => {
  const html = renderToStaticMarkup(h(MemoryPage, {}));

  it('渲染两个 tab，当前只选中内容 tab', () => {
    expect(html).toContain('记忆内容');
    expect(html).toContain('记忆与学习（调参）');
    expect(html.match(/aria-selected="true"/g) ?? []).toHaveLength(1);
    expect(html).toContain('aria-selected="true"');
    expect(html).toContain('data-testid="mem-tab-content"');
    expect(html).toContain('data-testid="mem-tab-settings"');
  });

  it('首屏是各分段的加载态，不是整页 spinner 或错误', () => {
    expect(html).toContain('正在读取核心记忆');
    expect(html).toContain('正在读取记忆列表');
  });

  it('把契约边界写在页面上：无详情端点、无分页', () => {
    expect(html).toContain('没有单条详情端点');
    expect(html).toContain('没有分页');
    expect(html).toContain('单次最多取 200 条');
  });

  it('不假装有分页控件', () => {
    expect(html).not.toContain('下一页');
    expect(html).not.toContain('上一页');
    expect(html).not.toContain('page=2');
  });
});

/* ------------------------------------------------------------------ 核心记忆 */

describe('核心记忆分段', () => {
  it('空态说明还没有核心记忆，并给出可照抄的例子', () => {
    const html = renderToStaticMarkup(
      h(CoreMemorySection, { state: ok(coreResponse([])), handlers: HANDLERS, onReload: () => undefined }),
    );
    expect(html).toContain('data-testid="mem-core-empty"');
    expect(html).toContain('还没有核心记忆');
    expect(html).toContain('叫我小林');
  });

  it('失败态显示原因与重试，且说明不影响其它分段', () => {
    const html = renderToStaticMarkup(
      h(CoreMemorySection, {
        state: {
          status: 'error',
          data: null,
          error: {
            reason: '服务端内部错误',
            hint: '可以重试',
            retriable: true,
            code: 'internal',
            detail: null,
            traceId: 'tr_x',
          },
        },
        handlers: HANDLERS,
        onReload: () => undefined,
      }),
    );
    expect(html).toContain('核心记忆读不到：服务端内部错误');
    expect(html).toContain('这一块失败不影响页面其它分段');
    expect(html).toContain('trace：tr_x');
    expect(html).toMatch(/<button[^>]*>.*重试<\/button>/);
    expect(html).not.toContain('还没有核心记忆');
  });

  it('403 按账号粒度写，不出现「该会话」；且不给重试按钮（重试无用）', () => {
    const html = renderToStaticMarkup(
      h(CoreMemorySection, {
        state: {
          status: 'error',
          data: null,
          error: {
            reason: '没有权限访问该会话',
            hint: '',
            retriable: false,
            code: 'forbidden',
            detail: null,
            traceId: 'tr_403',
          },
        },
        handlers: HANDLERS,
        onReload: () => undefined,
      }),
    );
    expect(html).toContain('核心记忆读不到：当前账号没有访问权限');
    // 核心记忆是账号级资源：不能说成「该会话」，也不能让人以为换会话能读
    expect(html).not.toContain('该会话');
    expect(html).toContain('账号级资源');
    expect(html).toContain('不按会话分');
    expect(html).toContain('data-testid="mem-section-no-retry"');
    expect(html).toContain('重试不会有不同结果');
    expect(html).toContain('data-retriable="false"');
    // 无权限时不给可点的重试入口（403 重试无用）
    expect(html).not.toMatch(/<button[^>]*>.*重试<\/button>/);
    // 只有账号闸那一类原因才给改密入口；普通 403 不放「点了没用」的链接
    expect(html).not.toContain('href="/settings/account"');
    expect(html).toContain('trace：tr_403');
  });

  it('403 若带 must_change_password，给出可执行的指路，且不承诺自动恢复', () => {
    const html = renderToStaticMarkup(
      h(CoreMemorySection, {
        state: {
          status: 'error',
          data: null,
          error: {
            reason: '没有权限访问该会话',
            hint: '',
            retriable: false,
            code: 'forbidden',
            detail: 'must_change_password',
            traceId: null,
          },
        },
        handlers: HANDLERS,
        onReload: () => undefined,
      }),
    );
    expect(html).toContain('当前账号没有访问权限');
    expect(html).toContain('首次登录还没改初始口令');
    // 改密入口是真链接（route.ts 里既有 /settings/account），不是「账号页」纯文本
    expect(html).toContain('href="/settings/account"');
    expect(html).toContain('设置 → 账号');
    expect(html).not.toContain('账号页');
    // 恢复方式是可执行动作：点页脚的「重新读取全部」，或重新进入本页
    expect(html).toContain('重新读取全部');
    expect(html).toContain('或重新进入本页');
    // 2026-11 验收实测：停在 403 的 4 秒内 `/v1/memory`、`/v1/core-memory` 请求数为 0，
    // 也没有 focus / visibilitychange 重新取数。这句不实陈述不许再写回来。
    expect(html).not.toContain('本页会自动恢复');
    expect(html).not.toContain('该会话');
  });

  it('读取失败时表单可编辑，但保存按钮禁用并写明原因（报告以此为准）', () => {
    const html = renderToStaticMarkup(
      h(CoreMemorySection, {
        state: {
          status: 'error',
          data: null,
          error: {
            reason: '服务端内部错误',
            hint: '',
            retriable: true,
            code: 'internal',
            detail: null,
            traceId: null,
          },
        },
        handlers: HANDLERS,
        onReload: () => undefined,
      }),
    );
    // 输入框没有 disabled：表单仍可填写
    expect(html).not.toMatch(/<input[^>]*disabled/);
    // 但保存按钮禁用：canSubmit 要求 status === 'ok'
    expect(html).toMatch(/<button[^>]*disabled[^>]*>保存核心记忆<\/button>/);
    // 界面必须解释这个差异，不能让人以为「能打字就能保存」
    expect(html).toContain('data-testid="mem-core-form-blocked"');
    expect(html).toContain('可以填写');
    expect(html).toContain('重试成功');
  });

  it('403 时表单说明不指向不存在的重试，而是说权限修好前不启用', () => {
    const html = renderToStaticMarkup(
      h(CoreMemorySection, {
        state: {
          status: 'error',
          data: null,
          error: {
            reason: '没有权限访问该会话',
            hint: '',
            retriable: false,
            code: 'forbidden',
            detail: null,
            traceId: null,
          },
        },
        handlers: HANDLERS,
        onReload: () => undefined,
      }),
    );
    const blocked = html.slice(html.indexOf('data-testid="mem-core-form-blocked"'));
    expect(blocked).toContain('当前账号没有访问权限');
    expect(blocked).toContain('可以填写');
    expect(blocked).not.toContain('重试成功');
  });

  it('注入说明写清例外：群聊过滤 / 800 字符预算 / 完全隔离模式', () => {
    const html = renderToStaticMarkup(
      h(CoreMemorySection, { state: ok(coreResponse([CORE_ITEM])), handlers: HANDLERS, onReload: () => undefined }),
    );
    expect(html).toContain('不保证全部注入');
    expect(html).toContain('群聊里只注入作用域为「群聊 / 全局」的条目');
    expect(html).toContain('800 字符');
    expect(html).toContain('按优先级截断');
    expect(html).toContain('完全隔离');
    // 旧的绝对化口径不能再出现（实际群聊会过滤、超预算会截断）
    expect(html).not.toContain('每一轮对话都会注入');
  });

  it('列表里显示 label / 内容 / 优先级 / 作用域与两个时间', () => {
    const html = renderToStaticMarkup(
      h(CoreMemorySection, {
        state: ok(coreResponse([CORE_ITEM])),
        handlers: HANDLERS,
        onReload: () => undefined,
      }),
    );
    expect(html).toContain('称呼');
    expect(html).toContain('叫我小林');
    expect(html).toContain('优先级 10');
    expect(html).toContain('私聊');
    expect(html).toContain('来源 手动写入');
    expect(html).toContain(formatAbsoluteTime(CORE_ITEM.created_at));
    expect(html).toContain(formatAbsoluteTime(CORE_ITEM.updated_at));
    expect(html).toContain('1 / 20 条');
  });

  it('满 20 条时新增被禁用并说明原因（覆盖已有 label 仍可用）', () => {
    const items = Array.from({ length: 20 }, (_, index) => ({
      ...CORE_ITEM,
      core_id: `core_${index}`,
      label: `label_${index}`,
    }));
    const html = renderToStaticMarkup(
      h(CoreMemorySection, { state: ok(coreResponse(items)), handlers: HANDLERS, onReload: () => undefined }),
    );
    expect(html).toContain('已达 20 条上限');
    expect(html).toContain('新增按钮已禁用');
    expect(html).toContain('20 / 20 条');
    // 没有填写 label 时提交按钮必须是 disabled（不存在「点一下能绕过上限」）
    expect(html).toMatch(/<button[^>]*disabled[^>]*>保存核心记忆<\/button>/);
  });

  it('删除是软删除，二次确认里写清「没有恢复入口」', () => {
    // 确认面板只在点「停用…」后出现；这里用 SSR 无法点，改由 markdown/E2E 覆盖，
    // 本用例先锁住「删除入口文案不冒充硬删除」。
    const html = renderToStaticMarkup(
      h(CoreMemorySection, { state: ok(coreResponse([CORE_ITEM])), handlers: HANDLERS, onReload: () => undefined }),
    );
    expect(html).toContain('删除是软删除');
    expect(html).toContain('没有恢复入口');
    expect(html).toContain('停用…');
  });
});

/* ------------------------------------------------------------------ 列表状态 */

describe('记忆列表：空态 / 失败 / 截断', () => {
  it('默认口径的空态提示墓碑默认不显示', () => {
    const html = renderList({ state: ok(listResponse([])), include: null });
    expect(html).toContain('data-testid="mem-list-empty"');
    expect(html).toContain('没有现行记忆');
    expect(html).toContain('显示已撤回 / 显示已归档');
  });

  it('include=all 的空态不说「可能被撤回」，因为已经取回墓碑了', () => {
    const html = renderList({
      state: ok(listResponse([], { include: 'all' })),
      include: 'all',
      flags: { retracted: true, archived: true },
    });
    expect(html).toContain('没有任何记忆（包括已撤回与已归档）');
    expect(html).not.toContain('可能是条目被撤回或归档了');
  });

  it('失败态显示原因、重试与作用范围，不显示空态文案', () => {
    const html = renderList({
      state: {
        status: 'error',
        data: null,
        error: {
          reason: '服务端内部错误',
          hint: '可以重试',
          retriable: true,
          code: 'internal',
          detail: null,
          traceId: null,
        },
      },
    });
    expect(html).toContain('记忆列表读不到：服务端内部错误');
    expect(html).toContain('重试');
    expect(html).toContain('这一块失败不影响页面其它分段');
    expect(html).not.toContain('没有现行记忆');
  });

  it('列表分段遇到账号闸 403 时，也给同一个可点的改密入口', () => {
    const html = renderList({
      state: {
        status: 'error',
        data: null,
        error: {
          reason: '没有权限访问该会话',
          hint: '',
          retriable: false,
          code: 'forbidden',
          detail: 'must_change_password',
          traceId: null,
        },
      },
    });
    expect(html).toContain('记忆列表读不到：当前账号没有访问权限');
    expect(html).toContain('href="/settings/account"');
    expect(html).toContain('设置 → 账号');
    expect(html).not.toContain('本页会自动恢复');
  });

  it('has_more 时明说没有分页、最多 200 条，不画假翻页', () => {
    const data = listResponse([MEM_A], { total: 260, has_more: true, limit: 200 });
    const html = renderList({ state: ok(data), limit: 200 });
    expect(html).toContain('data-testid="mem-limit-notice"');
    expect(html).toContain('只显示了最近 1 条');
    expect(html).toContain('接口没有分页');
    expect(html).toContain('最大 200');
    expect(html).not.toContain('下一页');
  });

  it('墓碑开关映射到契约的 include（两开关 → all）', () => {
    expect(includeMode({ retracted: false, archived: false })).toBeNull();
    expect(includeMode({ retracted: true, archived: false })).toBe('retracted');
    expect(includeMode({ retracted: false, archived: true })).toBe('archived');
    expect(includeMode({ retracted: true, archived: true })).toBe('all');
    expect(includeSummary(null)).toContain('默认只看现行记忆');
    expect(includeSummary('retracted')).toContain('仍不含已归档');
    expect(includeSummary('archived')).toContain('仍不含已撤回');
    expect(includeSummary('all')).toContain('含既撤回又归档');
  });

  it('始终说明「最多显示 N 条」，不靠 has_more 才说', () => {
    const html = renderList({ state: ok(listResponse([MEM_A])) });
    expect(html).toContain('data-testid="mem-page-size"');
    expect(html).toContain('本次');
    expect(html).toContain('最多显示 50 条');
    expect(html).toContain('没有分页');
  });

  it('渲染 include 说明与两个显式开关', () => {
    const html = renderList({ include: 'retracted', flags: { retracted: true, archived: false } });
    expect(html).toContain('显示已撤回（include=retracted）');
    expect(html).toContain('显示已归档（include=archived）');
    expect(html).toContain('include=retracted：现行记忆 + 已撤回');
    expect(html).toContain('不参与召回');
  });
});

/* ------------------------------------------------------------------ 列表行 */

describe('记忆列表行：时间 / 来源 / 置信度 / 墓碑标记', () => {
  it('显示绝对时间（相对时间只是括号里的辅助）', () => {
    const html = renderList({ state: ok(listResponse([MEM_A])) });
    expect(html).toContain(formatAbsoluteTime(MEM_A.created_at));
    expect(html).toContain('记于');
    expect(html).toMatch(/（\d+ (分钟|小时|天|个月|年)前）|（刚刚）/);
  });

  it('显示 origin 中文与置信度百分比', () => {
    const html = renderList({ state: ok(listResponse([MEM_A])) });
    expect(html).toContain('来源：你说的');
    expect(html).toContain('置信度 80%');
  });

  it('已撤回的墓碑标出状态与理由，且没有撤回/纠正按钮（只读）', () => {
    const html = renderList({
      state: ok(listResponse([MEM_B_TOMBSTONE])),
      include: 'retracted',
      flags: { retracted: true, archived: false },
    });
    expect(html).toContain('已撤回：地址过时');
    expect(html).toContain('墓碑：只读');
    expect(html).not.toContain('撤回…');
    expect(html).not.toContain('纠正…');
  });

  it('被纠正的旧条目标「已被纠正」并给出新条目 id', () => {
    const html = renderList({
      state: ok(listResponse([MEM_B_TOMBSTONE])),
      include: 'all',
      flags: { retracted: true, archived: true },
    });
    expect(html).toContain('已被纠正（新条目 mem_c）');
  });

  it('已归档条目标「已归档」，并保留归档时间', () => {
    const html = renderList({
      state: ok(listResponse([MEM_C_ARCHIVED])),
      include: 'archived',
      flags: { retracted: false, archived: true },
    });
    expect(html).toContain('已归档');
    expect(html).toContain(formatAbsoluteTime(MEM_C_ARCHIVED.archived_at));
  });

  it('模型推测单独标「推测，不是事实」', () => {
    const inferred = { ...MEM_A, origin: 'model_inference' };
    const html = renderList({ state: ok(listResponse([inferred])) });
    expect(html).toContain('推测，不是事实');
    expect(html).toContain('来源：模型推测');
  });

  it('详情只用列表数据渲染（含 memory_id / 可见范围 / 来源事件），并注明没有单条端点', () => {
    const html = renderList({ state: ok(listResponse([MEM_A])) });
    // summary 在 320px 下不能折出 2–3 字孤行，所以文案压短；
    // 「没有单条详情端点」的完整口径仍在页头 .mem__boundary（见整体渲染用例）。
    expect(html).toContain('详情（列表数据，无单条详情接口）');
    expect(html).toContain('mem_a');
    expect(html).toContain('仅本人（subject_private）');
    expect(html).toContain('turn:1');
  });

  it('按 kind 分段，已知三类固定顺序，未知 kind 原样显示', () => {
    const rows = [
      { ...MEM_A, memory_id: 'p', kind: 'procedure', text: '先看日程再看记忆' },
      { ...MEM_A, memory_id: 'e', kind: 'episode', text: '昨天去了湖边' },
      { ...MEM_A, memory_id: 'a', kind: 'assertion', text: '她住在杭州' },
      { ...MEM_A, memory_id: 'x', kind: 'mystery', text: '神秘类型' },
    ];
    const groups = groupByKind(rows);
    expect(groups.map((group) => group.kind)).toEqual([
      'episode',
      'assertion',
      'procedure',
      'mystery',
    ]);
    const html = renderList({ state: ok(listResponse(rows)) });
    expect(html.indexOf(kindLabel('episode'))).toBeLessThan(html.indexOf(kindLabel('assertion')));
    expect(html).toContain('神秘类型');
  });

  it('时间倒序（服务端已排序，前端也不漂）', () => {
    const rows = [
      { ...MEM_A, memory_id: 'old', created_at: '2026-01-01T00:00:00+00:00' },
      { ...MEM_A, memory_id: 'new', created_at: '2026-10-30T10:00:00+00:00' },
    ];
    expect(sortByCreatedDesc(rows).map((row) => row.memory_id)).toEqual(['new', 'old']);
  });
});

/* ------------------------------------------------------------------ 筛选 */

describe('前端筛选（kind / 时间）', () => {
  const now = new Date('2026-11-01T00:00:00Z');

  it('kind 过滤只保留对应类型', () => {
    const rows = [MEM_A, MEM_C_ARCHIVED];
    expect(filterMemories(rows, { kind: 'episode', range: 'all' }, now)).toEqual([MEM_C_ARCHIVED]);
  });

  it('时间范围过滤按 created_at，范围外的不显示', () => {
    const rows = [MEM_A, MEM_C_ARCHIVED];
    expect(filterMemories(rows, { kind: 'all', range: '30d' }, now)).toEqual([MEM_A]);
  });

  it('created_at 为空的旧数据不在时间范围里冒充', () => {
    const noTime = { ...MEM_A, memory_id: 'no_time', created_at: null };
    expect(filterMemories([noTime], { kind: 'all', range: '7d' }, now)).toEqual([]);
    expect(filterMemories([noTime], { kind: 'all', range: 'all' }, now)).toEqual([noTime]);
  });

  it('筛选只作用于已取回的数据：界面明说已取回 X 条 / 筛选后 Y 条', () => {
    const html = renderList({ state: ok(listResponse([MEM_A, MEM_C_ARCHIVED])) });
    expect(html).toContain('只过滤已取回的条目');
    expect(html).toContain('已取回 2 条');
  });

  it('筛选后为空时给专门的空态，而不是说「没有记忆」', () => {
    const html = renderList({
      state: ok(listResponse([MEM_C_ARCHIVED])),
      filters: { kind: 'assertion', range: 'all' },
    });
    expect(html).toContain('data-testid="mem-filter-empty"');
    expect(html).toContain('没有条目符合当前的类型 / 时间筛选');
  });
});

/* ------------------------------------------------------------------ 部分失败 */

describe('部分失败：一块失败不盖住整页', () => {
  it('核心记忆 403、列表成功时，列表内容照常渲染', () => {
    const html = renderBody(
      {
        status: 'error',
        data: null,
        error: {
          reason: '没有权限访问该会话',
          hint: '',
          retriable: false,
          code: 'forbidden',
          detail: null,
          traceId: null,
        },
      },
      ok(listResponse([MEM_A])),
    );
    // 核心分段：错误可见，且按账号粒度写（不是「该会话」）
    expect(html).toContain('核心记忆读不到：当前账号没有访问权限');
    expect(html).not.toContain('该会话');
    // 列表分段：真实内容可见，没有被整页错误吞掉
    expect(html).toContain('她住在杭州');
    expect(html).toContain('data-testid="mem-list"');
    // 没有整页级错误面板（dash-error 是整页失败的样子）
    expect(html).not.toContain('dash-error');
  });

  it('列表失败、核心记忆成功时，核心记忆照常渲染', () => {
    const html = renderBody(
      ok(coreResponse([CORE_ITEM])),
      {
        status: 'error',
        data: null,
        error: {
          reason: '服务端内部错误',
          hint: '',
          retriable: true,
          code: 'internal',
          detail: null,
          traceId: null,
        },
      },
    );
    expect(html).toContain('记忆列表读不到：服务端内部错误');
    expect(html).toContain('叫我小林');
    expect(html).not.toContain('dash-error');
  });

  it('两个分段都失败时仍是两条独立错误，不是一句「加载失败」', () => {
    const failure = {
      status: 'error' as const,
      data: null,
      error: {
        reason: '网络断了',
        hint: '确认后端可用',
        retriable: true,
        code: 'network_error',
        detail: null,
        traceId: null,
      },
    };
    const html = renderBody(failure, { ...failure });
    expect(html).toContain('核心记忆读不到：无法连接服务端');
    expect(html).toContain('记忆列表读不到：无法连接服务端');
    expect((html.match(/data-testid="mem-section-error"/g) ?? []).length).toBe(2);
    // 网络失败可重试：两个分段各自给出重试按钮
    expect((html.match(/<button[^>]*>.*?重试<\/button>/g) ?? []).length).toBe(2);
  });

  it('三类失败可区分：403 无权限（重试无用）/ 500 可重试 / 网络失败可重试', () => {
    const section = (code: string, retriable: boolean, detail: string | null = null) => ({
      status: 'error' as const,
      data: null,
      error: {
        reason: '没有权限访问该会话',
        hint: '通用提示',
        retriable,
        code,
        detail,
        traceId: null,
      },
    });

    const forbidden = renderBody(section('forbidden', false), ok(listResponse([MEM_A])));
    const internal = renderBody(section('internal', true), ok(listResponse([MEM_A])));
    const offline = renderBody(section('network_error', true), ok(listResponse([MEM_A])));

    // 403：账号级、重试无用、没有重试按钮
    expect(forbidden).toContain('核心记忆读不到：当前账号没有访问权限');
    expect(forbidden).toContain('重试不会有不同结果');
    expect(forbidden).not.toContain('可以重试');
    expect(forbidden).not.toContain('服务端内部错误');
    expect(forbidden).not.toMatch(/<button[^>]*>.*?重试<\/button>/);

    // 500：可重试，且绝不能被说成权限问题
    expect(internal).toContain('核心记忆读不到：服务端内部错误');
    expect(internal).toContain('不是权限问题');
    expect(internal).toContain('可以直接重试');
    expect(internal).not.toContain('没有访问权限');
    expect(internal).not.toContain('该会话');
    expect(internal).toMatch(/<button[^>]*>.*?重试<\/button>/);

    // 网络失败：可重试，也不同于权限问题
    expect(offline).toContain('核心记忆读不到：无法连接服务端');
    expect(offline).toContain('请求没有到达后端');
    expect(offline).toMatch(/<button[^>]*>.*?重试<\/button>/);

    // 三条原因必须互不相同（不许一句「加载失败」糊过去）
    const reasons = [forbidden, internal, offline].map(
      (markup) => markup.match(/核心记忆读不到：([^<]+)/)?.[1] ?? '',
    );
    expect(new Set(reasons).size).toBe(3);
  });

  it('业务错误原样透出，不替服务端编原因', () => {
    const html = renderBody(
      ok(coreResponse([CORE_ITEM])),
      {
        status: 'error',
        data: null,
        error: {
          reason: 'limit 必须在 1..200 之间',
          hint: '',
          retriable: false,
          code: 'invalid_request',
          detail: null,
          traceId: null,
        },
      },
    );
    expect(html).toContain('记忆列表读不到：limit 必须在 1..200 之间');
    // 不可重试的请求错误同样不该出现重试按钮
    expect(html).not.toMatch(/<button[^>]*>.*?重试<\/button>/);
  });
});

/* ------------------------------------------------------------------ 页脚文案 */

describe('页脚文案跟着分段状态走（403 不再说「重试按钮就在那个分段里」）', () => {
  const forbiddenError = {
    reason: '没有权限访问该会话',
    hint: '',
    retriable: false,
    code: 'forbidden',
    detail: null,
    traceId: null,
  };
  const forbidden = (detail: string | null = null) => ({
    status: 'error' as const,
    data: null,
    error: { ...forbiddenError, detail },
  });
  const internal = {
    status: 'error' as const,
    data: null,
    error: {
      reason: '服务端内部错误',
      hint: '可以重试',
      retriable: true,
      code: 'internal',
      detail: null,
      traceId: null,
    },
  };

  it('两个分段都 403：页脚不承诺重试按钮，改为「不提供重试」', () => {
    const html = renderBody(forbidden(), forbidden());
    expect(html).toContain('data-foot-mode="non-retriable"');
    expect(html).not.toContain('重试按钮就在那个分段里');
    expect(html).toContain('不提供重试按钮');
    // 页脚必须给可照做的下一步，不能只说「按那个分段里的说明处理」（那分段里没有可执行办法）
    expect(html).toContain('先按该分段里写的原因处理');
    expect(html).toContain('设置 → 账号');
    expect(html).toContain('改完密码');
    expect(html).toContain('重新读取全部');
    expect(html).not.toContain('请按那个分段里的说明处理');
    expect(html).toContain('失败的「核心记忆、记忆列表」');
    // 「重新读取全部」仍然是可点的刷新入口
    expect(html).toMatch(/<button[^>]*>.*?重新读取全部<\/button>/);
  });

  it('只有一个分段 403（另一个成功）也不承诺「分段里有重试按钮」', () => {
    const html = renderBody(forbidden('must_change_password'), ok(listResponse([MEM_A])));
    expect(html).toContain('data-foot-mode="non-retriable"');
    expect(html).not.toContain('重试按钮就在那个分段里');
    expect(html).toContain('失败的「核心记忆」');
  });

  it('可重试失败（500）：页脚仍说重试按钮在分段里', () => {
    const html = renderBody(internal, ok(listResponse([MEM_A])));
    expect(html).toContain('data-foot-mode="retriable"');
    expect(html).toContain('重试按钮就在那个分段里');
    expect(html).not.toContain('不提供重试按钮');
  });

  it('无失败：页脚是通用说明，带 data-foot-mode="idle"', () => {
    const html = renderBody(ok(coreResponse([CORE_ITEM])), ok(listResponse([MEM_A])));
    expect(html).toContain('data-foot-mode="idle"');
    expect(html).toContain('「重新读取全部」会同时刷新核心记忆与记忆列表');
  });

  it('memoryFooterNote 是纯函数：按 retryable 分流并点名分段', () => {
    const both403 = memoryFooterNote(forbidden(), forbidden());
    expect(both403.mode).toBe('non-retriable');
    expect(both403.nonRetriable).toEqual(['核心记忆', '记忆列表']);
    expect(both403.retriable).toEqual([]);
    expect(both403.text).not.toContain('重试按钮就在那个分段里');
    // 不可重试分支必须指路到可照做的动作（改密 → 重新读取），而不是只说「按分段里的说明处理」
    expect(both403.text).toContain('先按该分段里写的原因处理');
    expect(both403.text).toContain('设置 → 账号');
    expect(both403.text).toContain('重新读取全部');
    expect(both403.text).not.toContain('请按那个分段里的说明处理');

    const retriableNote = memoryFooterNote(internal, ok(listResponse([MEM_A])));
    expect(retriableNote.mode).toBe('retriable');
    expect(retriableNote.retriable).toEqual(['核心记忆']);
    expect(retriableNote.text).toContain('重试按钮就在那个分段里');

    const idleNote = memoryFooterNote(ok(coreResponse([])), ok(listResponse([])));
    expect(idleNote.mode).toBe('idle');
    expect(idleNote.nonRetriable).toEqual([]);
  });
});

/* ------------------------------------------------------------------ 错误文案 */

describe('错误文案：按资源粒度 + 可操作性', () => {
  const forbidden = {
    reason: '没有权限访问该会话',
    hint: '',
    retriable: false,
    code: 'forbidden',
    detail: null,
    traceId: null,
  };

  it('403 是账号级资源，不说「该会话」，也不给重试', () => {
    const core = memoryErrorCopy('core', forbidden);
    expect(core.reason).toBe('当前账号没有访问权限');
    expect(core.hint).toContain('核心记忆是账号级资源');
    expect(core.hint).not.toContain('该会话');
    expect(core.retryable).toBe(false);

    const list = memoryErrorCopy('list', forbidden);
    expect(list.reason).toBe('当前账号没有访问权限');
    expect(list.hint).toContain('记忆列表是账号级资源');
    expect(list.hint).not.toContain('该会话');
    expect(list.retryable).toBe(false);
  });

  it('403 + detail=must_change_password 指向真实原因（账号闸），并给出可执行步骤', () => {
    const copy = memoryErrorCopy('core', { ...forbidden, detail: 'must_change_password' });
    expect(copy.hint).toContain('首次登录还没改初始口令');
    expect(copy.hint).toContain('设置 → 账号');
    expect(copy.hint).toContain('重新读取全部');
    expect(copy.hint).toContain('或重新进入本页');
    // 页面不会自动恢复：不要写回「本页会自动恢复」
    expect(copy.hint).not.toContain('本页会自动恢复');
    expect(copy.retryable).toBe(false);
  });

  it('500 可重试、明说不是权限问题；网络失败可重试', () => {
    const internal = memoryErrorCopy('core', {
      reason: '服务端内部错误',
      hint: '可以重试；持续失败请查看服务端日志。',
      retriable: true,
      code: 'internal',
      detail: null,
      traceId: null,
    });
    expect(internal.reason).toBe('服务端内部错误');
    expect(internal.hint).toContain('不是权限问题');
    expect(internal.retryable).toBe(true);

    const offline = memoryErrorCopy('list', {
      reason: '无法连接服务端',
      hint: '',
      retriable: true,
      code: 'network_error',
      detail: null,
      traceId: null,
    });
    expect(offline.reason).toBe('无法连接服务端');
    expect(offline.hint).toContain('请求没有到达后端');
    expect(offline.retryable).toBe(true);
  });

  it('没有错误对象时不编原因', () => {
    expect(memoryErrorCopy('core', null).reason).toBe('请求失败');
  });
});

/* ------------------------------------------------------------------ 危险操作 */

describe('危险操作：遗忘全部的二次确认流', () => {
  it('初始只能点「遗忘全部记忆…」，且明说无法撤销', () => {
    const html = renderDanger(INITIAL_FORGET);
    expect(html).toContain('危险操作');
    expect(html).toContain('无法撤销');
    expect(html).toContain('遗忘全部记忆…');
    // 首屏影响面也要按实际写（三类 MemoryRow 都会被撤回）
    expect(html).toContain('所有记忆行');
    expect(html).not.toContain('长期记忆标记为已撤回');
    expect(html).not.toContain('确认遗忘全部');
  });

  it('第一次点击进入二次确认（不执行），勾选之前确认按钮禁用', () => {
    const stepOne = forgetReducer(INITIAL_FORGET, { type: 'begin' });
    expect(stepOne.step).toBe('confirm');
    expect(stepOne.ack).toBe(false);
    expect(canSubmitForget(stepOne)).toBe(false);

    const html = renderDanger(stepOne);
    expect(html).toContain('二次确认：遗忘全部记忆');
    expect(html).toContain('我明白这会清空当前账号可见的记忆');
    // 影响面按实际后端（forget_subject）写：所有 kind 的 MemoryRow 都撤回，
    // 不是只有「长期记忆（断言）」。
    expect(html).toContain('记忆行（情节 / 断言 / 程序三类都有');
    expect(html).toContain('情节片段');
    expect(html).toContain('用户请求清除');
    expect(html).not.toContain('长期记忆（断言）');
    expect(html).toMatch(/<button[^>]*disabled[^>]*>确认遗忘全部<\/button>/);

    // 只勾选、不提交：状态是 confirm + ack，仍未调用接口
    const acked = forgetReducer(stepOne, { type: 'ack', value: true });
    expect(canSubmitForget(acked)).toBe(true);
    expect(acked.step).toBe('confirm');
  });

  it('提交只进入 pending；行数一律以服务端返回为准', () => {
    const acked = forgetReducer(forgetReducer(INITIAL_FORGET, { type: 'begin' }), {
      type: 'ack',
      value: true,
    });
    const pending = forgetReducer(acked, { type: 'submit' });
    expect(pending.pending).toBe(true);
    expect(pending.count).toBeNull();
    // pending 中重复提交不产生副作用
    expect(forgetReducer(pending, { type: 'submit' })).toEqual(pending);

    const done = forgetReducer(pending, { type: 'succeeded', count: 7 });
    expect(done.step).toBe('done');
    expect(done.count).toBe(7);
    const html = renderDanger(done);
    expect(html).toContain('data-testid="mem-forget-count"');
    expect(html).toContain('7');
    expect(html).toContain('四类受影响行数之和');
    expect(html).toContain('记忆行（情节 / 断言 / 程序三类 kind）');
    expect(html).toContain('软清除');
    expect(html).toContain('无法撤销');
  });

  it('取消回到初始态；失败保留二次确认并显示原因', () => {
    const confirm = forgetReducer(INITIAL_FORGET, { type: 'begin' });
    expect(forgetReducer(confirm, { type: 'cancel' })).toEqual(INITIAL_FORGET);

    const failed = forgetReducer(confirm, {
      type: 'failed',
      error: {
        reason: '服务端内部错误',
        hint: '可以重试',
        retriable: true,
        code: 'internal',
        detail: null,
        traceId: null,
      },
    });
    expect(failed.step).toBe('confirm');
    expect(failed.pending).toBe(false);
    expect(failed.error?.reason).toBe('服务端内部错误');
  });

  it('未勾选时 ack 变化被忽略（confirm 之外不接受勾选）', () => {
    expect(forgetReducer(INITIAL_FORGET, { type: 'ack', value: true })).toEqual(INITIAL_FORGET);
    const acked = forgetReducer(
      forgetReducer(INITIAL_FORGET, { type: 'begin' }),
      { type: 'ack', value: true },
    );
    expect(forgetReducer(acked, { type: 'submit' }).pending).toBe(true);
  });
});

/* ------------------------------------------------------------------ 术语 */

describe('术语统一：意识条目', () => {
  const stepConfirm = forgetReducer(INITIAL_FORGET, { type: 'begin' });
  const renders: Array<[string, string]> = [
    ['首屏', renderDanger(INITIAL_FORGET)],
    ['二次确认', renderDanger(stepConfirm)],
    ['结果态', renderDanger(forgetReducer(stepConfirm, { type: 'succeeded', count: 3 }))],
    ['整页', renderToStaticMarkup(h(MemoryPage, {}))],
  ];

  it('只说「意识条目」，不混「觉察记录」/「awareness」', () => {
    expect(AWARENESS_TERM).toBe('意识条目');
    for (const [where, markup] of renders) {
      expect(markup, where).toContain(AWARENESS_TERM);
      expect(markup, where).not.toContain('觉察');
      expect(markup, where).not.toContain('awareness');
      expect(markup, where).not.toContain('Awareness');
    }
  });

  it('遗忘影响面四处提法一致（不出现同物异名）', () => {
    const text = renders.map(([, markup]) => markup).join('');
    // 「意识条目」的每次出现都必须是这个词本身，不能被别的写法替换
    expect((text.match(/意识条目/g) ?? []).length).toBeGreaterThanOrEqual(3);
  });
});

/* ------------------------------------------------------------------ 调参 tab */

describe('「记忆与学习」调参 tab', () => {
  const html = renderToStaticMarkup(h(MemorySettingsTab, { onBack: () => undefined }));

  it('四个参数与它们的配置键都列出来', () => {
    expect(html).toContain('relative_score_floor');
    expect(html).toContain('rerank_top_n');
    expect(html).toContain('recency_half_life_days');
    expect(html).toContain('max_retention_days');
    expect(html).toContain('召回相关性下限');
    expect(html).toContain('精排头部条数');
    expect(html).toContain('时间衰减半衰期');
    expect(html).toContain('最长保留天数');
  });

  it('文案诚实：已接线、写清生效时机，并把两点例外说明白', () => {
    // 接线后页面必须说"已生效"，而不是旧的"未接线"
    expect(html).toContain('生效状态：已接线');
    expect(html).not.toContain('未接线');
    expect(html).not.toContain('接线由另一个任务修复中');
    expect(html).not.toContain('本页不承诺修改会生效');
    // 生效时机要具体：下一轮对话 / 下一个后台事件
    expect(html).toContain('下一轮');
    expect(html).toContain('下一个后台事件');
    // 例外一：没配重排模型时 rerank_top_n 不生效，必须明说原因
    expect(html).toContain('rerank_top_n');
    expect(html).toContain('不生效');
    expect(html).toContain('LING_RERANK');
    // 例外二：保留期是硬上限，调小立即生效、调大只影响之后写入的记忆
    expect(html).toContain('硬上限');
    expect(html).toContain('之后写入');
    // 常量本身也不许再写"未接线"
    expect(MEMORY_POLICY_STATUS).not.toContain('未接线');
  });

  it('发布提示同样写真实生效时机，并带上重排例外', () => {
    expect(MEMORY_POLICY_PUBLISHED_MESSAGE).toContain('下一轮对话起生效');
    expect(MEMORY_POLICY_PUBLISHED_MESSAGE).toContain('LING_RERANK');
    // 旧文案说"本次修改不会改变她的记忆行为"——接线后这句反过来变成假话
    expect(MEMORY_POLICY_PUBLISHED_MESSAGE).not.toContain('不会改变');
  });

  it('字段提示写清 rerank 与保留期的真实边界（不承诺不存在的效果）', () => {
    const rerankHint = html.includes('LING_RERANK_*');
    expect(rerankHint).toBe(true);
    expect(html).toContain('没配时这一项不生效');
    expect(html).toContain('调大只影响之后写入的记忆');
  });

  it('调参 tab 在 /memory?tab=settings 下才挂载（query 深链）', () => {
    expect(memoryTabFromHref('/memory?tab=settings')).toBe('settings');
    // SSR 下 useLocationHref 的 server snapshot 是 '/'，所以只渲染内容 tab；
    // 深链行为由 E2E 在真实浏览器里断言。
    const page = renderToStaticMarkup(h(MemoryPage, {}));
    expect(page).not.toContain('data-testid="mem-settings"');
  });
});

/* ------------------------------------------------------------------ 工具 */

describe('展示工具', () => {
  it('绝对时间固定为 YYYY-MM-DD HH:mm', () => {
    const date = new Date(2026, 10, 1, 8, 5, 0);
    expect(formatAbsoluteTime(date.toISOString())).toBe('2026-11-01 08:05');
    expect(formatAbsoluteTime(null)).toBe('时间未知');
    expect(formatAbsoluteTime('not-a-date')).toBe('时间未知');
  });

  it('相对时间分档，未来时间不冒充过去', () => {
    const now = new Date('2026-11-01T12:00:00Z');
    const minutesAgo = (minutes: number) => new Date(now.getTime() - minutes * 60_000).toISOString();
    expect(formatRelativeTime(minutesAgo(0), now)).toBe('刚刚');
    expect(formatRelativeTime(minutesAgo(3), now)).toBe('3 分钟前');
    expect(formatRelativeTime(minutesAgo(60 * 3), now)).toBe('3 小时前');
    expect(formatRelativeTime(minutesAgo(60 * 24 * 2), now)).toBe('2 天前');
    expect(formatRelativeTime(minutesAgo(60 * 24 * 45), now)).toBe('1 个月前');
    expect(formatRelativeTime('2026-11-02T00:00:00Z', now)).toBe('未来时间');
    expect(formatRelativeTime(null, now)).toBe('');
  });

  it('置信度只显示整数百分比', () => {
    expect(confidenceLabel(0.8)).toBe('置信度 80%');
    expect(confidenceLabel(0.123)).toBe('置信度 12%');
    expect(confidenceLabel('nonsense')).toBe('置信度未知');
  });

  it('来源与 kind 的中文表覆盖契约枚举', () => {
    for (const origin of [
      'simulated',
      'user_statement',
      'external_observation',
      'verified_source',
      'tool_receipt',
      'model_inference',
    ]) {
      expect(originLabel(origin)).not.toBe(origin);
    }
    for (const kind of ['episode', 'assertion', 'procedure']) {
      expect(kindLabel(kind)).not.toBe(kind);
    }
    // 认不出来的值原样显示，不吞
    expect(originLabel('weird')).toBe('weird');
    expect(kindLabel('weird')).toBe('weird');
    expect(coreOriginLabel('manual')).toBe('手动写入');
    expect(coreOriginLabel('weird')).toBe('weird');
  });

  it('limitNotice 只在 has_more 时出现，并说清没有分页', () => {
    expect(limitNotice(listResponse([MEM_A]))).toBeNull();
    const notice = limitNotice(listResponse([MEM_A], { total: 120, has_more: true, limit: 50 }));
    expect(notice).toContain('还有 119 条没有显示');
    expect(notice).toContain('没有分页');
  });

  it('withNode 帮助函数防止空渲染的假绿', () => {
    expect(withNode('x')).toContain('x');
  });
});
