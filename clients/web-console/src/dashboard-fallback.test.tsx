/**
 * 总览：上游回退（兜底值）必须被说成兜底值。
 *
 * 用户原话：「当看手机时间什么的这种依赖日程什么的上游的，当上游依赖不可用时，
 * 回退到自己的默认参数」。后端已经这么做了（`src/ling/domains/fallback.py`），
 * 但**前端原来完全不显示这件事**：日程真的算出来的「下次大约 14:20」和日程读不到
 * 时兜底算出来的「下次大约 14:20」长得一模一样。不标来源 = 把兜底值伪装成她的
 * 真实状态，违背本项目「不编造」的底线。
 *
 * 这组测试钉三件事：
 * 1. 回退时那句人话真的在（并且带上"不是她的真实状态"）；
 * 2. 正常时**不**出现任何额外噪音（不是每块都挂一个"正常"标签）；
 * 3. 标记缺失 = 不渲染提示，也不编一句原因出来。
 *
 * 载荷字段名全部取自后端真实响应：`sections.phone.data.fallback` /
 * `sections.presence.data.fallback`，形状为 `contracts/v1.py::FallbackNotice`
 * （`upstream` / `used` / `state` / `source` / `reason`）。下面的样例值是直接
 * 用后端 `resolve_input(...).notice()` + `FallbackNotice.model_dump(mode="json")`
 * 生成的，不是手写的近似值。
 *
 * 排版（375 / 1440 不横向溢出、提示真的出现在页面上）交给
 * `dashboard-fallback.browser.test.tsx`（真 Chromium）。
 */
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  DashboardBody,
  FallbackHint,
  fallbackNoticeOf,
  PhoneBody,
} from './components/DashboardPage';
import type { FallbackNotice } from './types';

const NOOP = () => undefined;
const AS_OF = '2026-11-08T02:30:00Z';

/* ------------------------------------------------------------ 真实载荷片段 */

/**
 * 后端在「今天还没有日程」时的 phone 段标记（原样，含中文 reason）。
 *
 * 来源：`phone/service.py::_default_reason(empty)` 经
 * `resolve_input(...).notice()` → `FallbackNotice.model_dump(mode="json")`。
 */
const PHONE_FALLBACK_EMPTY: FallbackNotice = {
  upstream: 'schedule',
  used: true,
  state: 'empty',
  source: 'domain_default',
  reason: '今天还没有日程，先按默认每 20 分钟看一次手机',
};

/** 日程读不到（超时/查询失败）时的那一条。 */
const PHONE_FALLBACK_UNAVAILABLE: FallbackNotice = {
  upstream: 'schedule',
  used: true,
  state: 'unavailable',
  source: 'domain_default',
  reason: '日程暂时读不到，先按默认每 20 分钟看一次手机',
};

/** 正常：日程按她的时间轴算出来的。`reason` 必须是 null。 */
const PHONE_NO_FALLBACK: FallbackNotice = {
  upstream: 'schedule',
  used: false,
  state: 'ready',
  source: 'upstream',
  reason: null,
};

/** 场景不明时 presence 段的标记（`presence/service.py::SCENE_DEFAULT_REASON`）。 */
const PRESENCE_FALLBACK: FallbackNotice = {
  upstream: 'scene',
  used: true,
  state: 'empty',
  source: 'domain_default',
  reason: '场景不明（没有有效场景 / 已过期），按未知处理，不冒充她在做什么',
};

/* ------------------------------------------------------------ 直接渲染组件 */

function renderPhone(data: Record<string, unknown>): string {
  return renderToStaticMarkup(h(PhoneBody, { data }));
}

function renderDashboard(sections: Record<string, unknown>): string {
  return renderToStaticMarkup(
    h(DashboardBody, {
      data: { schema_version: 'v1', generated_at: AS_OF, timezone: 'Asia/Shanghai', sections } as never,
      onOpenNow: NOOP,
      onOpenChat: NOOP,
      onOpenSettings: NOOP,
      onRetry: NOOP,
    }),
  );
}

/** 一块「查到了、非空」的分段。 */
function section(data: Record<string, unknown>): Record<string, unknown> {
  return { status: 'ok', as_of: AS_OF, data };
}

/** 其余分段给一份最小可用载荷，让整页能渲染出来（只关心 phone / presence 两块）。 */
function sectionsWith(overrides: Record<string, Record<string, unknown>>): Record<string, unknown> {
  const base: Record<string, Record<string, unknown>> = {
    activity: section({ state: 'running', kind: 'reading', origin: 'simulated' }),
    presence: section({
      availability: 'available',
      energy: 70,
      social_load: 20,
      describe: '状态正常，可以随时说话',
      fallback: { ...PHONE_NO_FALLBACK, upstream: 'scene' },
    }),
    affect: section({ emotion: 'calm', intensity: 0.4 }),
    phone: section({
      state: 'idle',
      last_looked_at: '2026-11-08T02:10:00Z',
      next_look_at: '2026-11-08T02:50:00Z',
      next_look_interval_minutes: 20,
      fallback: PHONE_NO_FALLBACK,
    }),
    proactive: section({ pending_count: 0, delivery_enabled: false, gate_reasons: [] }),
    agenda: section({ plans: [], commitments: [], goals: [] }),
    memories: section({ items: [] }),
    timeline: section({ items: [] }),
    relationship: section({ familiarity: 0.2, trust: 0.1 }),
    connectivity: section({ channels: [], bound_model_roles: [] }),
  };
  return { ...base, ...overrides };
}

/* ------------------------------------------------------------ 手机卡片：回退 */

describe('手机卡片：兜底值必须被说成兜底值', () => {
  it('日程还没生成 → 显示原因，并明说不是她的真实状态', () => {
    const html = renderPhone({
      state: 'idle',
      last_looked_at: '2026-11-08T02:10:00Z',
      next_look_at: '2026-11-08T02:50:00Z',
      next_look_interval_minutes: 20,
      fallback: PHONE_FALLBACK_EMPTY,
    });
    // 后端给的那句人话原样显示（不翻译成错误码、不改写成技术话）
    expect(html).toContain('今天还没有日程，先按默认每 20 分钟看一次手机');
    // 关键的一句：不能让读者把兜底值当成她的状态
    expect(html).toContain('不是她此刻的真实状态');
    // 标记本身可定位（浏览器测试会按它取元素）
    expect(html).toContain('data-testid="dash-fallback"');
    expect(html).toContain('data-upstream="schedule"');
    // 原本的时刻信息照常保留：提示是附加说明，不是把内容替换掉
    expect(html).toContain('下次大约');
  });

  it('日程读不到 → 同上（这是"读不到"不是"没安排"，但都算回退）', () => {
    const html = renderPhone({
      state: 'idle',
      last_looked_at: null,
      next_look_at: '2026-11-08T02:50:00Z',
      fallback: PHONE_FALLBACK_UNAVAILABLE,
    });
    expect(html).toContain('日程暂时读不到');
    expect(html).toContain('不是她此刻的真实状态');
  });

  it('后端只给了 used=true 没给 reason → 仍然说出"默认参数"，不编原因', () => {
    const html = renderPhone({
      state: 'idle',
      next_look_at: '2026-11-08T02:50:00Z',
      fallback: { ...PHONE_FALLBACK_EMPTY, reason: null },
    });
    expect(html).toContain('日程暂时用不上，看手机的节奏先按默认参数显示');
    // 没有 reason 时不许出现任何具体原因（编原因就是另一种伪造）
    expect(html).not.toContain('今天还没有日程');
    expect(html).not.toContain('读不到');
  });

  it('正在看时不显示提示：此刻没有依赖回退参数的可见值', () => {
    /*
     * 正在看时后端不排下一次（状态机规则 2），卡上唯一的"下次"是
     * 「等她收起手机」——那是规则不是参数。此时挂一句"不是她此刻的真实状态"，
     * 读者会以为"她正在看手机"这件事也是猜的，等于用修误导的方式再误导一次。
     */
    const html = renderPhone({
      state: 'watching',
      watching_app: 'qq',
      watching_since: '2026-11-08T02:20:00Z',
      next_look_at: null,
      fallback: PHONE_FALLBACK_EMPTY,
    });
    expect(html).toContain('正在看手机');
    expect(html).toContain('等她收起手机');
    expect(html).not.toContain('dash-fallback');
    expect(html).not.toContain('不是她此刻的真实状态');
  });

  it('正常（used=false）时一个字的额外提示都没有', () => {
    const html = renderPhone({
      state: 'idle',
      last_looked_at: '2026-11-08T02:10:00Z',
      next_look_at: '2026-11-08T02:50:00Z',
      next_look_interval_minutes: 20,
      fallback: PHONE_NO_FALLBACK,
    });
    expect(html).toContain('下次大约');
    expect(html).not.toContain('dash-fallback');
    expect(html).not.toContain('默认');
    expect(html).not.toContain('不是她此刻的真实状态');
  });

  it('字段整个缺失时不当成"正常"之外的东西：不渲染提示也不编原因', () => {
    // 旧后端不返回 `fallback`：这时没有任何依据说它是不是回退。
    // 结论是"不显示提示"，但**不能**顺手补一句"一切正常"——那是我们没依据的断言。
    const html = renderPhone({
      state: 'idle',
      last_looked_at: '2026-11-08T02:10:00Z',
      next_look_at: '2026-11-08T02:50:00Z',
    });
    expect(html).not.toContain('dash-fallback');
    expect(html).not.toContain('正常');
    expect(html).not.toContain('默认');
  });
});

/* -------------------------------------------------- 手机卡片：从没看过（empty） */

describe('手机卡片：empty 态里也要说实话', () => {
  /*
   * 后端 `dashboard.py::phone()` 的 empty 分支**专门**保留了
   * `next_look_interval_minutes` + `fallback`，注释写得很清楚：最常见的空态里
   * 反而最容易看不到回退说明。前端原来用 `emptyText('手机')` 一句
   * 「她还没拿起过手机。」盖掉整块，把那句话又藏起来了。
   */
  const EMPTY_PHONE = { next_look_interval_minutes: 20, fallback: PHONE_FALLBACK_EMPTY };

  it('不说"手机放下了"（她没拿起过），但把默认节奏与来源说清楚', () => {
    const html = renderPhone(EMPTY_PHONE);
    expect(html).toContain('她还没拿起过手机。');
    expect(html).not.toContain('手机放下了');
    expect(html).toContain('大约每 20 分钟看一次');
    expect(html).toContain('今天还没有日程');
    expect(html).toContain('不是她此刻的真实状态');
  });

  it('整页渲染时 empty 分支不再被"她还没拿起过手机"一句盖掉', () => {
    const markup = renderDashboard(
      sectionsWith({ phone: { status: 'empty', as_of: AS_OF, data: EMPTY_PHONE } }),
    );
    expect(markup).toContain('她还没拿起过手机。');
    expect(markup).toContain('今天还没有日程，先按默认每 20 分钟看一次手机');
    expect(markup).toContain('不是她此刻的真实状态');
  });
});

/* ------------------------------------------------------------ 可用性卡片：场景 */

describe('可用性卡片：场景回退要能被看见，但别冤枉可用性', () => {
  /*
   * presence 段的 `fallback` 讲的是**场景**（`resolve_scene()` 回退到领域默认
   * `unknown`），而 `describe` 讲的是**可用性**（从精力/社交负担算出来的）。
   * 两者不是一回事：场景不明不意味着"可以随时说话"这句是兜底值。
   * 所以这里钉两件事——场景默认值有名字，可用性自己的那句话不被误标成默认。
   */
  it('场景不明时把场景的默认值说出来（后端原话 + "不是她的真实状态"）', () => {
    const markup = renderDashboard(
      sectionsWith({
        presence: section({
          availability: 'available',
          energy: 70,
          social_load: 20,
          describe: '状态正常，可以随时说话',
          fallback: PRESENCE_FALLBACK,
        }),
      }),
    );
    // 场景不明的原话（后端 `SCENE_DEFAULT_REASON`）在页面上
    expect(markup).toContain('场景不明');
    expect(markup).toContain('不是她此刻的真实状态');
  });

  it('可用性自己的说明照常显示，不被挂上"默认"标签', () => {
    const markup = renderDashboard(
      sectionsWith({
        presence: section({
          availability: 'available',
          energy: 70,
          social_load: 20,
          // 后端 `view.describe() or "状态正常，可以随时说话"` 补出来的那句
          describe: '状态正常，可以随时说话',
          fallback: PRESENCE_FALLBACK,
        }),
      }),
    );
    /*
     * 这条曾经写成「“状态正常，可以随时说话”是按默认情况说的」——错的：
     * 那句话来自 availability，跟场景回退无关。误导的修正是另一种误导。
     */
    expect(markup).not.toContain('是按默认情况说的');
    expect(markup).toContain('状态正常，可以随时说话');
  });

  it('场景正常时照常印 describe，不加任何标签', () => {
    const markup = renderDashboard(
      sectionsWith({
        presence: section({
          availability: 'low',
          energy: 40,
          social_load: 80,
          describe: '你精神一般，回复可以短一点',
          fallback: { ...PHONE_NO_FALLBACK, upstream: 'scene' },
        }),
      }),
    );
    expect(markup).toContain('你精神一般，回复可以短一点');
    expect(markup).not.toContain('dash-fallback');
  });

  it('只有场景回退、手机正常时，整页恰好一条提示（不是每块都挂）', () => {
    const markup = renderDashboard(
      sectionsWith({
        presence: section({
          availability: 'available',
          energy: 70,
          social_load: 20,
          describe: '状态正常，可以随时说话',
          fallback: PRESENCE_FALLBACK,
        }),
      }),
    );
    expect((markup.match(/data-testid="dash-fallback"/g) ?? []).length).toBe(1);
    expect(markup).toContain('data-upstream="scene"');
    expect(markup).not.toContain('data-upstream="schedule"');
  });
});

/* ------------------------------------------------------------ 标记解析本身 */

describe('fallbackNoticeOf：形状不认识就不猜', () => {
  it('取到布尔 used 才认；缺字段/类型不对一律 null', () => {
    expect(fallbackNoticeOf({ fallback: PHONE_FALLBACK_EMPTY })).toEqual(PHONE_FALLBACK_EMPTY);
    expect(fallbackNoticeOf({})).toBeNull();
    expect(fallbackNoticeOf({ fallback: null })).toBeNull();
    expect(fallbackNoticeOf({ fallback: 'yes' })).toBeNull();
    // used 不是布尔：宁可"没有标记"，也不把字符串 'false' 读成真
    expect(fallbackNoticeOf({ fallback: { upstream: 'schedule', used: 'false' } })).toBeNull();
    expect(fallbackNoticeOf(null)).toBeNull();
  });

  it('reason 不是字符串时按"没有原因"处理，而不是把对象印到界面上', () => {
    const html = renderToStaticMarkup(
      h(FallbackHint, {
        notice: { ...PHONE_FALLBACK_EMPTY, reason: { why: 'x' } as unknown as string },
        scope: '看手机的节奏',
      }),
    );
    expect(html).not.toContain('[object Object]');
    expect(html).toContain('看手机的节奏先按默认参数显示');
  });

  it('认不出的上游名原样显示，不硬翻成"日程"', () => {
    const html = renderToStaticMarkup(
      h(FallbackHint, {
        notice: { ...PHONE_FALLBACK_EMPTY, upstream: 'mystery', reason: null },
        scope: '看手机的节奏',
      }),
    );
    expect(html).toContain('mystery');
  });
});

/* --------------------------------------------------- 「正在忙什么」（媒体） */

/**
 * 用户原话：「他在忙，可以给他变成他的真实活动状态，放到"正在看手机"那里，
 * 显示当前的状态」。
 *
 * 这些用例钉住两件事：
 *
 * 1. **看视频 ≠ 看手机**，所以它是一张独立的卡，不是塞进手机卡里的一句话；
 * 2. **别人的会话只该知道"忙"**——后端按主体过滤过，前端不能假装知道更多。
 */
describe('正在忙什么：媒体活动是真实状态，不是含糊的「在忙」', () => {
  it('自己的会话：说出在看什么、从什么时候起', () => {
    const html = renderDashboard({
      phone: section({ state: 'idle' }),
      media: section({
        kind: 'video_watch',
        started_at: '2026-11-08T02:12:00Z',
        in_my_session: true,
        activity_id: 'act_1',
      }),
    });
    expect(html).toContain('正在忙什么');
    expect(html).toContain('正在看视频');
    expect(html).toContain('看视频');
    // 起算时间要给出来，否则"正在看"没有时间感
    expect(html).toMatch(/从 \d{2}:\d{2} 起/);
    // 自己这边不该出现"细节不在这一侧"那句
    expect(html).not.toContain('细节不在这一侧');
  });

  it('别人的会话：只说"正忙着看东西"，不说在看什么、也不说细节在哪', () => {
    const html = renderDashboard({
      phone: section({ state: 'idle' }),
      media: section({
        kind: 'bilibili_feed',
        started_at: '2026-11-08T02:12:00Z',
        in_my_session: false,
      }),
    });
    expect(html).toContain('正忙着看东西');
    // 「刷视频」是别人的会话内容，不该出现在这一侧
    expect(html).not.toContain('正在刷视频');
    // 也不该反过来泄露"是别人的会话"这种元信息措辞之外的东西，
    // 但必须诚实地说明这一侧知道得有限
    expect(html).toContain('细节不在这一侧');
  });

  it('没有媒体活动时整张卡不渲染（不摆空壳）', () => {
    const html = renderDashboard({
      phone: section({ state: 'idle' }),
      media: { status: 'empty', data: {} },
    });
    expect(html).not.toContain('正在忙什么');
  });

  it('未知 kind 原样显示，不猜一个更好听的名字', () => {
    const html = renderDashboard({
      phone: section({ state: 'idle' }),
      media: section({ kind: 'livestream', started_at: null, in_my_session: true }),
    });
    expect(html).toContain('正在看直播');
  });
});
