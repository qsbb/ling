/**
 * 外部插件页的纯逻辑与结构回归。
 *
 * 每条断言都对着**一个具体的错误实现**，而不是"看起来对不对"：
 *
 * - `pluginState` 的三态分别测：错误的实现最容易把 `enabled=true` 当成可用，
 *   于是"开着但版本不兼容"被画成"已启用"——用户去聊天里调用，才发现没反应。
 * - `remedy` 按 `status` 分支测：如果把所有失败合并成一句"不可用"，
 *   "去启用"和"去重新登记"这两个完全相反的动作就会被混成同一个建议。
 * - 渲染断言盯着**声明 vs 授予**两列同时出现：只画一列的实现会在这里红。
 *
 * 渲染用 `react-dom/server`，与仓库 `dashboard.test.ts` / `logs.test.ts` 同一套办法
 * （没有 jsdom）。真实浏览器里的点击与窄屏在 `external-plugins.browser.test.tsx`。
 */
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  CAPABILITY_ORDER,
  ENDPOINT_PROBE_HINT,
  buildRegisterBody,
  capabilityDetail,
  capabilityName,
  effectiveCapabilities,
  failureNote,
  grantMismatch,
  healthLine,
  pluginState,
  remedy,
  statusBadge,
  ungrantedCapabilities,
  validateEndpointInput,
} from './externalPlugins';
import { ExternalPluginsPage, PluginRow, formatWhen } from './components/ExternalPluginsPage';
import type { ExternalPlugin, ExternalPluginStatusResponse } from './types';

function plugin(overrides: Partial<ExternalPlugin> = {}): ExternalPlugin {
  return {
    id: 'example.notes',
    name: '隔离测试插件',
    version: '1.2.0',
    api_version: 'ling-plugin/1',
    min_host_version: '0.0.15',
    max_host_version: '0.0.15',
    capabilities: ['tools', 'commands', 'events', 'host.state'],
    events: ['turn.completed'],
    tools: ['lookup'],
    commands: ['lookup'],
    granted_capabilities: ['tools', 'commands'],
    enabled: true,
    status: 'ready',
    last_error: null,
    last_checked_at: '2026-11-23T15:40:05.766147+00:00',
    ...overrides,
  };
}

const noop = () => undefined;

const rowProps = {
  health: null,
  busy: null,
  anyBusy: false,
  confirming: false,
  onAskDelete: noop,
  onCancelDelete: noop,
  onToggle: noop,
  onSync: noop,
  onCheck: noop,
  onUnregister: noop,
};

/* ------------------------------------------------------------------ 三态 */

describe('pluginState：开启、可用是两件事', () => {
  it('开着且 ready 才是 active', () => {
    expect(pluginState({ enabled: true, status: 'ready' })).toBe('active');
  });

  it('开着但版本不兼容 → blocked，不是 active', () => {
    // 错误实现：`return enabled ? 'active' : 'off'`。这条会红。
    expect(pluginState({ enabled: true, status: 'incompatible' })).toBe('blocked');
  });

  it('关着且 ready → off（下一步是启用，不是修复）', () => {
    expect(pluginState({ enabled: false, status: 'ready' })).toBe('off');
  });

  it('服务端自己停掉的（关着且 unavailable）→ blocked，不是 off', () => {
    // 这一条是写实现时被测试逼出来的：`set_enabled` 失败时会**先把 enabled 置 false
    // 再抛异常**。如果先判 enabled，`identity_mismatch` / `incompatible` 都会被画成
    // 普通的「已停用」，用户点「启用」只会再撞一次 422。
    expect(pluginState({ enabled: false, status: 'unavailable' })).toBe('blocked');
    expect(statusBadge({ enabled: false, status: 'unavailable' })).toEqual({
      tone: 'error',
      label: '端点不可达',
    });
  });

  it('手动关掉的（关着且 ready）才是 off', () => {
    expect(pluginState({ enabled: false, status: 'ready' })).toBe('off');
    expect(statusBadge({ enabled: false, status: 'ready' })).toEqual({ tone: 'off', label: '已停用' });
  });

  it('registered 与 ready 同等可用（服务端 allows() 就是这么写的）', () => {
    expect(pluginState({ enabled: true, status: 'registered' })).toBe('active');
  });
});

describe('statusBadge：未知状态原样显示', () => {
  it('不认识的状态不改写成「不可用」', () => {
    // 错误实现：default 返回「不可用」——那等于替服务端编了一个理由。
    expect(statusBadge({ enabled: true, status: 'quarantined' })).toEqual({
      tone: 'warn',
      label: 'quarantined',
    });
  });

  it('identity_mismatch 的文案指出「已停用」', () => {
    expect(statusBadge({ enabled: false, status: 'identity_mismatch' })).toEqual({
      tone: 'error',
      label: '标识已变，已停用',
    });
  });
});

/* ------------------------------------------------------------------ 建议 */

describe('remedy：下一步是不同的动作', () => {
  it('能工作的插件没有建议', () => {
    expect(remedy(plugin())).toBeNull();
  });

  it('停用但就绪 → 没有建议块（行内「启用」已经说了这件事）', () => {
    // 之前的实现给了一个 action:'enable' 的建议块，于是同一行里出现
    // 两颗「启用」按钮 + 一段"已登记但没启用"。一个信息说三遍。
    expect(remedy(plugin({ enabled: false }))).toBeNull();
  });

  it('标识变化 → 建议取消登记（而不是再同步一次）', () => {
    // 错误实现：所有失败都建议 sync。同步会再撞一次 422，用户原地打转。
    const fix = remedy(
      plugin({ enabled: false, status: 'identity_mismatch', last_error: '插件 ID 与端点声明不一致，已停用' }),
    );
    expect(fix?.action).toBe('unregister');
    expect(fix?.reason).toContain('ID 与端点声明不一致');
  });

  it('版本不兼容 → 建议同步（让发布者放宽区间后重读）', () => {
    expect(remedy(plugin({ enabled: true, status: 'incompatible' }))?.action).toBe('sync');
  });

  it('不可达 → 建议重试启用，并指出容器内 127.0.0.1 不是宿主', () => {
    const fix = remedy(plugin({ enabled: false, status: 'unavailable', last_error: 'PluginUnavailable: 无法读取插件 manifest' }));
    expect(fix?.action).toBe('enable');
    expect(fix?.next).toContain('127.0.0.1');
  });

  it('未知状态 → 有描述但 action 落到 sync，不编一个按不动的按钮', () => {
    expect(remedy(plugin({ status: 'weird_state' }))?.action).toBe('sync');
  });

  it('失败原因优先用 last_error，没有才用兜底文案', () => {
    const withError = remedy(plugin({ enabled: false, status: 'unavailable', last_error: '具体原因' }));
    expect(withError?.reason).toBe('具体原因');
    const without = remedy(plugin({ enabled: false, status: 'unavailable', last_error: null }));
    expect(without?.reason).toContain('没有连上');
  });
});

/* ------------------------------------------------------------------ 授权 */

describe('能力授予：声明与授予是两列', () => {
  it('未授予的是差集，且按固定顺序', () => {
    expect(
      ungrantedCapabilities(plugin({ granted_capabilities: ['tools'] })),
    ).toEqual(['commands', 'events', 'host.state']);
  });

  it('真正生效的只有交集', () => {
    // 授予了插件没声明的能力：服务端调用前会过滤，页面也不能算进去。
    const effective = effectiveCapabilities(
      plugin({ capabilities: ['tools'], granted_capabilities: ['tools', 'events'] }),
    );
    expect(effective).toEqual(['tools']);
  });

  it('两边一致时没有差异文案', () => {
    expect(
      grantMismatch(plugin({ capabilities: ['tools'], granted_capabilities: ['tools'] })),
    ).toBeNull();
  });

  it('少授予与多授予都写出来', () => {
    const text = grantMismatch(
      plugin({ capabilities: ['tools', 'commands'], granted_capabilities: ['tools', 'events'] }),
    );
    expect(text).toContain('未授予 命令');
    expect(text).toContain('事件 已不在声明里');
  });
});

/* -------------------------------------------------------------- 端点校验 */

describe('validateEndpointInput：提前反馈，不替代服务端', () => {
  it('合法地址返回 null', () => {
    expect(validateEndpointInput('http://127.0.0.1:8096')).toBeNull();
    expect(validateEndpointInput('  https://plugin.internal/v1  ')).toBeNull();
  });

  it.each([
    ['', '填一个'],
    ['127.0.0.1:8096', 'http://'],
    ['ftp://127.0.0.1', 'http 与 https'],
    ['http://user:pw@host', '用户名或密码'],
    ['http://host/v1?x=1', '查询参数'],
    ['http://host/v1#frag', '锚点'],
    ['http://host:0', '端口 0'],
    ['http://ho st', '空格'],
  ])('拒绝 %s', (input, expected) => {
    expect(validateEndpointInput(input)).toContain(expected);
  });

  it('提示里写明会探测哪个路径', () => {
    expect(ENDPOINT_PROBE_HINT).toContain('/v1/manifest');
  });
});

describe('buildRegisterBody：只发契约里的键', () => {
  it('空 expected_id 不发（发空串会被模式校验拒掉）', () => {
    const body = buildRegisterBody({
      endpoint: 'http://127.0.0.1:8096',
      expectedId: '  ',
      granted: ['tools'],
      enabled: false,
    });
    expect('expected_id' in body).toBe(false);
    expect(Object.keys(body).sort()).toEqual(['enabled', 'endpoint', 'granted_capabilities']);
  });

  it('尾部斜杠去掉（服务端会自己拼 /v1/manifest，重复的斜杠没必要保留）', () => {
    const body = buildRegisterBody({
      endpoint: 'http://127.0.0.1:8096///',
      expectedId: 'example.notes',
      granted: ['tools'],
      enabled: true,
    });
    expect(body.endpoint).toBe('http://127.0.0.1:8096');
    expect(body.expected_id).toBe('example.notes');
    expect(body.enabled).toBe(true);
  });
});

/* ------------------------------------------------------------------ 文案 */

describe('失败与健康文案', () => {
  it('身份不一致时说明服务端已经停用（失败不等于没生效）', () => {
    expect(failureNote({ message: '插件 ID 与端点声明不一致，已停用', code: 'invalid_request' })).toContain(
      '已停用',
    );
  });

  it('403 指出去找所有者/管理员', () => {
    expect(failureNote({ message: '外部插件设置仅管理员可访问', code: 'forbidden' })).toContain('所有者和管理员');
  });

  it('可达与不可达是两句不同的话', () => {
    const base = plugin();
    expect(healthLine({ ...base, reachable: true, health_checked_at: null })).toContain('可达');
    expect(healthLine({ ...base, reachable: false, health_checked_at: null })).toContain('不可达');
  });

  it('能力的中文名与说明都取得到；未知能力不显示空白', () => {
    for (const cap of CAPABILITY_ORDER) {
      expect(capabilityName(cap).length).toBeGreaterThan(0);
      expect(capabilityDetail(cap).length).toBeGreaterThan(0);
    }
    expect(capabilityName('future.cap' as never)).toBe('future.cap');
  });

  it('从未检查过显示「从未」而不是空', () => {
    expect(formatWhen(null)).toBe('从未');
  });
});

/* ------------------------------------------------------------------ 结构 */

describe('渲染：一条登记必须同时露出声明与授予', () => {
  const markup = renderToStaticMarkup(
    h(PluginRow, { ...rowProps, plugin: plugin({ granted_capabilities: ['tools'] }) }),
  );

  it('四个能力都出现（宣告过的与没宣告过的都在）', () => {
    for (const cap of CAPABILITY_ORDER) {
      expect(markup).toContain(capabilityName(cap));
    }
  });

  it('已授予的有勾，未授予的没有——两种状态在 DOM 上可区分', () => {
    expect(markup).toContain('data-on="true"');
    expect(markup).toContain('data-on="false"');
    // 只画一列的实现会少掉"声明但未授予"的虚线态。
    expect(markup).toContain('data-declared="true"');
  });

  it('少授予时给出差异说明', () => {
    expect(markup).toContain('未授予');
  });

  it('测试锚点带在插件 id 上，多行不会串', () => {
    expect(markup).toContain('data-testid="ep-row-example.notes"');
  });

  it('能工作的插件不画失败块', () => {
    expect(markup).not.toContain('data-testid="ep-fix-example.notes"');
  });
});

describe('渲染：失败行带原因与下一步按钮', () => {
  const markup = renderToStaticMarkup(
    h(PluginRow, {
      ...rowProps,
      plugin: plugin({
        enabled: false,
        status: 'identity_mismatch',
        last_error: '插件 ID 与端点声明不一致，已停用',
      }),
    }),
  );

  it('原因是服务端给的那句，不是前端另编的', () => {
    expect(markup).toContain('插件 ID 与端点声明不一致，已停用');
  });

  it('下一步是「取消登记」这个动作出现在失败块里', () => {
    expect(markup).toContain('data-testid="ep-fix-example.notes"');
    expect(markup).toContain('取消登记');
  });

  it('行状态是 blocked，不只是"停用"', () => {
    expect(markup).toContain('data-state="blocked"');
  });
});

describe('渲染：探测结果不能与失败状态打架', () => {
  it('记录已失败时，不展示旧的探测结果', () => {
    // 真实联调里出现过：服务端已把记录置成 identity_mismatch，而上一刻的探测
    // 结果还写着「端点可达，manifest 与登记的 id 一致」。两句话都各自为真，
    // 但它们说的是不同时刻——并排挂出来就是假话。
    const stale: ExternalPluginStatusResponse = {
      ...plugin({ status: 'identity_mismatch', enabled: false }),
      reachable: true,
      health_checked_at: '2026-11-23T15:40:14.568354+00:00',
    };
    const markup = renderToStaticMarkup(
      h(PluginRow, {
        ...rowProps,
        plugin: plugin({ status: 'identity_mismatch', enabled: false }),
        health: stale,
      }),
    );
    expect(markup).not.toContain('ep-health-example.notes');
    expect(markup).not.toContain('端点可达');
  });
});

describe('渲染：健康探测结果与已授予能力同屏', () => {
  const health: ExternalPluginStatusResponse = {
    ...plugin(),
    reachable: false,
    health_checked_at: '2026-11-23T15:40:14.568354+00:00',
  };
  const markup = renderToStaticMarkup(h(PluginRow, { ...rowProps, plugin: plugin(), health }));

  it('不可达要说出来', () => {
    expect(markup).toContain('data-testid="ep-health-example.notes"');
    expect(markup).toContain('端点不可达');
  });
});

describe('页面：空列表时给出下一步', () => {
  it('首帧（未加载）不白屏，有标题与登记入口', () => {
    const markup = renderToStaticMarkup(h(ExternalPluginsPage, { onBack: noop }));
    expect(markup).toContain('外部插件');
    expect(markup).toContain('data-testid="ep-register"');
    expect(markup).toContain('正在读取');
  });
});
