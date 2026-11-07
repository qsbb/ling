/**
 * 接入器页（`/settings/connectors`）的渲染断言与固定 API 路径。
 *
 * 这里保护的是**三态（四态）互不串**，不是样式细节：
 * - "未配置"绝不能说成"连接失败/未连接"——那会让人去查一条从未建立的连接；
 * - "已配置未连接"与"已关闭"必须是两个态（后者连接根本没被尝试过）；
 * - "已连接"不能再提示去补变量；
 * - 标记互斥：一张卡只带它自己那个状态的 class 与文案。
 *
 * 密钥的边界在两端都钉住：后端只回 `set`（`test_settings_connectors.py` 遍历整份
 * JSON 检查），前端在 `kind==='secret'` 时只可能画"已设置/未设置"。
 *
 * 不引入 DOM 测试库：用 `react-dom/server` 渲染成字符串断言，与 `dashboard.test.ts`
 * / `imageUpdate.test.tsx` 一致。
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './api';
import {
  ConnectorCard,
  connectorFieldState,
  connectorRuntimeLine,
  describeConnectorStatus,
  formatAsOf,
} from './components/ConnectorsPage';
import { SETTINGS_GROUPS } from './components/SettingsHub';
import { parseRoute, routePath } from './route';
import type { ConnectorField, ConnectorView } from './types';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function field(overrides: Partial<ConnectorField> & { name: string }): ConnectorField {
  return {
    label: '说明',
    kind: 'text',
    set: true,
    value: null,
    source: 'env',
    ...overrides,
  };
}

function connector(overrides: Partial<ConnectorView> = {}): ConnectorView {
  return {
    id: 'onebot',
    label: 'OneBot（NapCat / Lagrange / go-cqhttp）',
    configured: false,
    enabled: false,
    status: 'not_configured',
    status_reason: 'onebot_no_inbound_path',
    status_message: '没有任何入站通道：WS 端口、WS 地址、HTTP 上报密钥三项都没配',
    guidance: '在宿主部署目录的 `.env` 里任选一条入站路径：`LING_ONEBOT_WS_PORT` …',
    editable: false,
    storage: 'env',
    fields: [
      field({ name: 'LING_ONEBOT_WS_PORT', kind: 'text', set: false, value: null }),
      field({ name: 'LING_ONEBOT_WS_TOKEN', kind: 'secret', set: false, value: null }),
    ],
    runtime: { connections: null, received: null },
    ...overrides,
  };
}

const NOT_CONFIGURED = connector();
const DISABLED = connector({
  id: 'qqbot',
  label: 'QQ 官方机器人（QQ 开放平台）',
  configured: true,
  enabled: false,
  status: 'disabled',
  status_reason: 'qqbot_channel_disabled',
  status_message: '凭据已配好，但渠道开关是关的（LING_QQBOT_ENABLED=0）',
  guidance: '凭据已就位，只差开关：在宿主部署目录的 `.env` 里把 `LING_QQBOT_ENABLED` 设为 `1`',
});
const DISCONNECTED = connector({
  status: 'configured_disconnected',
  status_reason: 'onebot_ws_server_waiting',
  status_message: 'WS 服务端在监听，但还没有客户端连入',
  configured: true,
  enabled: true,
  guidance: '服务端已在监听，等 NapCat 连入',
  runtime: { connections: 0, received: 0 },
});
const CONNECTED = connector({
  status: 'connected',
  status_reason: 'onebot_ws_server_client',
  status_message: 'NapCat 已连入 WS 服务端',
  configured: true,
  enabled: true,
  guidance: '已连接，没有需要改的。',
  runtime: { connections: 1, received: 132 },
});

function render(view: ConnectorView): string {
  return renderToStaticMarkup(<ConnectorCard connector={view} />);
}

/** 取出「怎么改」那一段原文，单独对它断言（"配置项"那段固定说明里有 `.env`）。 */
function guidanceOf(markup: string): string {
  const match = /<p class="connector-card__guidance"[^>]*>([\s\S]*?)<\/p>/.exec(markup);
  return match?.[1] ?? '';
}

describe('状态映射：四态各自不同', () => {
  it('四种状态给四种徽标，未知码不伪装成任何一态', () => {
    const labels = [
      describeConnectorStatus('not_configured'),
      describeConnectorStatus('disabled'),
      describeConnectorStatus('configured_disconnected'),
      describeConnectorStatus('connected'),
      describeConnectorStatus('some_future_state'),
    ].map((item) => item.label);

    expect(new Set(labels).size).toBe(labels.length);
    expect(describeConnectorStatus('connected').tone).toBe('ok');
    expect(describeConnectorStatus('configured_disconnected').tone).toBe('warn');
    expect(describeConnectorStatus('disabled').tone).toBe('muted');
    expect(describeConnectorStatus('not_configured').tone).toBe('neutral');
    // 认不出来的码：明确说"未知"，而不是挑一个看起来最像的。
    expect(describeConnectorStatus('some_future_state').label).toBe('状态未知');
  });
});

describe('三态渲染互不串', () => {
  it('未配置：不说"连接失败/未连接"，也不带成功或警告底色', () => {
    const markup = render(NOT_CONFIGURED);

    expect(markup).toContain('data-status="not_configured"');
    expect(markup).toContain('connector-card--neutral');
    // **这一条是重点**：没填凭据时不能出现"失败/未连接/已连接"这类误导文案。
    expect(markup).not.toContain('连接失败');
    expect(markup).not.toContain('未连接');
    expect(markup).not.toContain('已连接');
    expect(markup).not.toContain('connector-card--warn');
    expect(markup).not.toContain('connector-card--ok');
    expect(markup).toContain('未配置');
    // 指引里的变量名要用等宽渲染，不能把反引号当正文画出来（像没渲染的 Markdown）。
    expect(markup).toContain('connector-inline-code');
    expect(markup).not.toContain('`');
  });

  it('已配置未连接：说"未连接"，不复用"未配置"的文案', () => {
    const markup = render(DISCONNECTED);

    expect(markup).toContain('data-status="configured_disconnected"');
    expect(markup).toContain('connector-card--warn');
    expect(markup).toContain('已配置 · 未连接');
    expect(markup).not.toContain('未配置');
    expect(markup).not.toContain('已连接');
    expect(markup).not.toContain('connector-card--ok');
  });

  it('已打开但渠道关闭：不是"未连接"，也不是"未配置"', () => {
    const markup = render(DISABLED);

    expect(markup).toContain('data-status="disabled"');
    expect(markup).toContain('connector-card--muted');
    expect(markup).toContain('已配置 · 渠道已关闭');
    expect(markup).not.toContain('未连接');
    expect(markup).not.toContain('未配置');
  });

  it('已连接：不再提示补变量，只带成功标记', () => {
    const markup = render(CONNECTED);

    expect(markup).toContain('data-status="connected"');
    expect(markup).toContain('connector-card--ok');
    expect(markup).toContain('已连接');
    expect(markup).not.toContain('未配置');
    expect(markup).not.toContain('未连接');
    // 已连接时指引必须是"没有需要改的"，不能还是"去宿主 .env 里补"。
    expect(guidanceOf(markup)).toContain('已连接，没有需要改的');
    expect(guidanceOf(markup)).not.toContain('宿主部署目录');
  });
});

describe('字段：密钥只报"设没设"', () => {
  it('secret 只画已设置/未设置，永远不画值', () => {
    expect(connectorFieldState(field({ name: 'A', kind: 'secret', set: true }))).toBe('已设置');
    expect(connectorFieldState(field({ name: 'A', kind: 'secret', set: false }))).toBe('未设置');

    const markup = render(
      connector({
        fields: [
          field({ name: 'LING_QQBOT_CLIENT_SECRET', kind: 'secret', set: true, value: 'should-not-render' }),
        ],
      }),
    );
    expect(markup).toContain('已设置');
    expect(markup).not.toContain('should-not-render');
  });

  it('非密钥照原样显示，缺值说"未设置"；开关说开/关（不是"未设置"）', () => {
    expect(connectorFieldState(field({ name: 'A', kind: 'text', value: '127.0.0.1', set: true }))).toBe(
      '127.0.0.1',
    );
    expect(connectorFieldState(field({ name: 'A', kind: 'text', value: null, set: false }))).toBe(
      '未设置',
    );
    expect(connectorFieldState(field({ name: 'A', kind: 'switch', value: '开', set: true }))).toBe(
      '开',
    );
    expect(connectorFieldState(field({ name: 'A', kind: 'switch', value: '关', set: true }))).toBe(
      '关',
    );
  });

  it('字段名与状态都在渲染里（长变量名靠 CSS 换行，不裁掉）', () => {
    const markup = render(
      connector({
        fields: [field({ name: 'LING_QQBOT_WEBHOOK_PUBLIC_KEY', kind: 'text', value: null, set: false })],
      }),
    );
    expect(markup).toContain('LING_QQBOT_WEBHOOK_PUBLIC_KEY');
    expect(markup).toContain('connector-field__state--unset');
  });
});

describe('运行时那一行：没有可观察的连接就不编数字', () => {
  it('connections 为 null（只配了 HTTP 上报）时整行不显示', () => {
    expect(connectorRuntimeLine({ connections: null, received: null })).toBeNull();
  });

  it('零连接与服务端在等客户端都能读出来', () => {
    expect(connectorRuntimeLine({ connections: 0, received: 0 })).toBe('当前没有连接，已收 0 帧事件');
  });

  it('已连接但一帧都没收到时，把这件事说出来', () => {
    expect(connectorRuntimeLine({ connections: 1, received: 0 })).toContain('还没推过任何数据');
    expect(connectorRuntimeLine({ connections: 1, received: 132 })).toBe('当前 1 条连接，已收 132 帧事件');
  });
});

describe('路由与入口', () => {
  it('解析 /settings/connectors 并可反向生成同一路径', () => {
    expect(parseRoute('/settings/connectors')).toEqual({ name: 'connectors' });
    expect(parseRoute('/settings/connectors/')).toEqual({ name: 'connectors' });
    expect(routePath({ name: 'connectors' })).toBe('/settings/connectors');
  });

  it('设置中心的入口指向真实路由（不是拼错的地址）', () => {
    const entry = SETTINGS_GROUPS.flatMap((group) => group.entries).find(
      (item) => item.testid === 'open-connectors',
    );
    expect(entry).toBeDefined();
    expect(entry?.path).toBe('/settings/connectors');
    expect(parseRoute(entry?.path ?? '')).toEqual({ name: 'connectors' });
  });

  it('入口文案说的是"看"，不是承诺可编辑', () => {
    const entry = SETTINGS_GROUPS.flatMap((group) => group.entries).find(
      (item) => item.testid === 'open-connectors',
    );
    // 这一轮只有只读页：入口若写"配置/编辑"，点进去没有输入框就是骗人。
    expect(entry?.verb.startsWith('看')).toBe(true);
    expect(entry?.verb).not.toContain('在这里配置');
  });
});

describe('api.connectors', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('只调用固定的只读路径，不带参数', async () => {
    const body = {
      schema_version: 'v1',
      as_of: '2026-11-07T01:02:03+00:00',
      connectors: [NOT_CONFIGURED],
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(body));
    vi.stubGlobal('fetch', fetchMock);

    await expect(api.connectors()).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith('/v1/settings/connectors', {
      credentials: 'same-origin',
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('时间戳', () => {
  it('解析不了就照原样显示，不编一个时间', () => {
    expect(formatAsOf('not-a-date')).toBe('not-a-date');
    expect(formatAsOf('2026-11-07T01:02:03+00:00')).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  });
});
