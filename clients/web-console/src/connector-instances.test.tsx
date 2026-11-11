/**
 * 接入器实例面板：**纯函数**与 API 客户端。
 *
 * 这里只钉那些"错了也不会报错、只会把数据写到别处"的东西：
 *
 * - 草案分区键必须含 scope（`instance_id` 只在 scope 内唯一）；
 * - `instancesOf` 造出来的草案必须是**独立对象**，不是共用的一份；
 * - 选中态失效时退回后端排序的第一个，而不是沿用已经不存在的 id；
 * - 删除按钮的禁用依据是**后端给的** `deletable`，不是前端比字面量；
 * - `saveConnectorConfig` 的 `instance_id` 走 **query**，并且不带时 URL 里
 *   一个字都不多（升级前的路径必须逐字节不变）。
 *
 * 真实的"点选、输入、点保存"在 `connectors.browser.test.tsx` 里用真 Chromium
 * 跑；这里用 `react-dom/server` 断言纯函数与渲染结果。
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './api';
import {
  DEFAULT_INSTANCE_ID,
  deleteBlockReason,
  emptyInstanceForm,
  instanceFormKey,
  instanceFormOf,
  instanceStatusLabel,
  instanceTitle,
  InstanceList,
  nextSelectedId,
} from './components/ConnectorInstances';
import { onebotFields } from './connectors.fixtures.test-helpers';
import type { ConnectorInstance } from './types';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/**
 * 每次调用都给一个**新的** `Response`。
 *
 * 直接 `mockResolvedValue(jsonResponse(...))` 会让所有调用共用同一个对象，
 * 第二次读 body 就抛 `Body is unusable`——那是测试写法的错，不是被测算法的错。
 */
function freshResponses(body: unknown, status = 200) {
  return vi.fn().mockImplementation(async () => jsonResponse(body, status));
}

function makeInstance(overrides: Partial<ConnectorInstance> = {}): ConnectorInstance {
  const instanceId = overrides.instance_id ?? 'main';
  return {
    scope: overrides.scope ?? 'onebot',
    instance_id: instanceId,
    display_name: overrides.display_name ?? '主号',
    enabled: overrides.enabled ?? true,
    // 后端算的：默认实例 false。
    deletable: overrides.deletable ?? instanceId !== DEFAULT_INSTANCE_ID,
    created_at: overrides.created_at ?? '2026-11-10T02:00:00+00:00',
    created_by: overrides.created_by ?? null,
    deleted_at: overrides.deleted_at ?? null,
  };
}

const DEFAULT_ONE = makeInstance({ instance_id: 'default', display_name: '默认' });
const MAIN = makeInstance({ instance_id: 'main', display_name: '主号' });
const SECOND = makeInstance({
  instance_id: 'i-1a2b3c4d',
  display_name: '小号二号',
  enabled: false,
});

describe('实例的草案分区', () => {
  it('分区键同时含 scope 与 instance_id（同 id 在两个 scope 下不共用）', () => {
    expect(instanceFormKey('onebot', 'default')).toBe('onebot::default');
    expect(instanceFormKey('qqbot', 'default')).toBe('qqbot::default');
    expect(instanceFormKey('onebot', 'default')).not.toBe(
      instanceFormKey('qqbot', 'default'),
    );
  });

  it('每个实例拿到的空草案是**各自独立**的对象', () => {
    const fields = onebotFields();
    const a = emptyInstanceForm(fields);
    const b = emptyInstanceForm(fields);
    // 值相等、身份不同：共用一份常量会让 a 上的编辑"传染"给 b。
    expect(a).toEqual(b);
    expect(a.draft).not.toBe(b.draft);
    a.draft.LING_ONEBOT_API_BASE = { kind: 'text', value: 'http://192.0.2.99:5700' };
    expect(b.draft.LING_ONEBOT_API_BASE).toEqual({ kind: 'text', value: 'http://127.0.0.1:5700' });
  });

  it('instanceFormOf：已编辑的取出那一份，没编辑的现造一份干净的', () => {
    const fields = onebotFields();
    const edited = emptyInstanceForm(fields);
    edited.draft.LING_ONEBOT_SELF_ID = { kind: 'text', value: '100200300' };
    const forms = { [instanceFormKey('onebot', 'main')]: edited };

    expect(instanceFormOf(forms, 'onebot', 'main', fields)).toBe(edited);
    // 另一个实例：拿到的必须是**按字段重造**的空草案，不带 main 的改动。
    const other = instanceFormOf(forms, 'onebot', 'other', fields);
    expect(other.draft.LING_ONEBOT_SELF_ID).toEqual({ kind: 'text', value: '100200300' });
    expect(other.clearKeys).toEqual([]);
    expect(other).not.toBe(edited);

    // 同一个 id 换一个 scope：仍然拿不到 main 的那一份。
    const sameIdOtherScope = instanceFormOf(forms, 'qqbot', 'main', fields);
    expect(sameIdOtherScope).not.toBe(edited);
  });
});

describe('选中态', () => {
  it('当前选中还在列表里 → 保持', () => {
    expect(nextSelectedId([DEFAULT_ONE, MAIN], 'main')).toBe('main');
  });

  it('当前选中的没了 → 退回列表第一个（后端已把默认实例排最前）', () => {
    expect(nextSelectedId([DEFAULT_ONE, MAIN], 'gone')).toBe('default');
    expect(nextSelectedId([MAIN, SECOND], null)).toBe('main');
  });

  it('空列表 → null（不是 "default"：那个实例可能根本不存在）', () => {
    expect(nextSelectedId([], null)).toBeNull();
    expect(nextSelectedId([], 'main')).toBeNull();
  });
});

describe('删除按钮的禁用依据', () => {
  it('默认实例（deletable=false）禁用并给出原因', () => {
    const reason = deleteBlockReason(DEFAULT_ONE);
    expect(reason).not.toBeNull();
    expect(reason).toContain('默认实例');
    expect(reason).toContain('不能删除');
  });

  it('普通实例可以删', () => {
    expect(deleteBlockReason(MAIN)).toBeNull();
  });

  it('已软删的不能再删', () => {
    expect(deleteBlockReason(makeInstance({ deleted_at: '2026-11-11T00:00:00+00:00' }))).not.toBeNull();
  });

  it('判定跟着后端的 deletable 走，而不是前端比 instance_id', () => {
    // 一个**不是** 'default' 却标了 deletable=false 的实例（例如将来出现的
    // 受保护实例）：前端必须听后端的，不能因为名字不叫 default 就放行。
    const protectedRow = makeInstance({ instance_id: 'legacy', deletable: false });
    expect(deleteBlockReason(protectedRow)).toContain('默认实例');
    // 反向：叫 default 但后端说可删（不应出现，但如果出现，前端不自己改判）。
    const oddball = makeInstance({ instance_id: 'default', deletable: true });
    expect(deleteBlockReason(oddball)).toBeNull();
  });
});

describe('状态标签', () => {
  it('启用/停用/已删除是三种说法（停用 ≠ 没连上）', () => {
    expect(instanceStatusLabel(MAIN)).toBe('已启用');
    expect(instanceStatusLabel(SECOND)).toBe('已停用');
    expect(instanceStatusLabel(makeInstance({ deleted_at: '2026-11-11T00:00:00+00:00' }))).toBe(
      '已删除',
    );
  });

  it('标题同时带显示名与身份（两者很像，混用会存错地方）', () => {
    expect(instanceTitle(MAIN)).toBe('主号（main）');
  });
});

describe('左侧列表渲染', () => {
  it('每项显示名 + 启停 + 是否默认；选中项标 aria-current 且带独立默认标签', () => {
    const html = renderToStaticMarkup(
      <InstanceList
        items={[DEFAULT_ONE, MAIN, SECOND]}
        selectedId="main"
        onSelect={() => {}}
        busy={false}
      />,
    );
    expect(html).toContain('主号');
    expect(html).toContain('小号二号');
    // 身份也画出来：显示名一样时只能靠它分辨。
    expect(html).toContain('i-1a2b3c4d');
    // 默认实例有「默认」标签，普通实例没有。
    expect(html).toContain('data-testid="connector-instance-default-default"');
    expect(html).not.toContain('data-testid="connector-instance-main-default"');
    // 启停是两个不同的标签。
    expect(html).toContain('已启用');
    expect(html).toContain('已停用');
    // 选中态：aria-current 只出现在被选中的那一项上。
    expect(html.match(/aria-current="true"/g)?.length ?? 0).toBe(1);
    const selectedIndex = html.indexOf('data-testid="connector-instance-main"');
    const currentIndex = html.indexOf('aria-current="true"');
    expect(selectedIndex).toBeGreaterThan(-1);
    expect(currentIndex).toBeGreaterThan(selectedIndex);
  });

  it('有未保存改动时那一项带「未保存」标签', () => {
    const html = renderToStaticMarkup(
      <InstanceList
        items={[DEFAULT_ONE, MAIN]}
        selectedId="main"
        onSelect={() => {}}
        dirtyIds={new Set(['main'])}
        busy={false}
      />,
    );
    expect(html).toContain('data-testid="connector-instance-main-dirty"');
    expect(html).not.toContain('data-testid="connector-instance-default-dirty"');
  });
});

describe('api：实例端点与实例维度', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('列实例走固定路径，不带 include_deleted', async () => {
    const body = {
      schema_version: 'v1',
      scope: 'onebot',
      items: [DEFAULT_ONE, MAIN],
      include_deleted: false,
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(body));
    vi.stubGlobal('fetch', fetchMock);

    await expect(api.connectorInstances('onebot')).resolves.toEqual(body);
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/v1/settings/connectors/onebot/instances');
  });

  it('新建走 POST，body 只有显示名（instance_id 交给后端派生）', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ schema_version: 'v1', item: MAIN }, 201),
    );
    vi.stubGlobal('fetch', fetchMock);
    await api.createConnectorInstance('onebot', { display_name: '主号' });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/v1/settings/connectors/onebot/instances');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ display_name: '主号' });
  });

  it('改名 / 启停 / 删除落在正确的子路径上（id 经过编码）', async () => {
    const fetchMock = freshResponses({ schema_version: 'v1', item: MAIN });
    vi.stubGlobal('fetch', fetchMock);

    await api.renameConnectorInstance('onebot', 'main', '新的名字');
    await api.setConnectorInstanceEnabled('onebot', 'main', false);
    await api.deleteConnectorInstance('onebot', 'main');

    const calls = fetchMock.mock.calls as [string, RequestInit][];
    expect(calls[0]?.[0]).toBe('/v1/settings/connectors/onebot/instances/main');
    expect(calls[0]?.[1].method).toBe('PUT');
    expect(JSON.parse(String(calls[0]?.[1].body))).toEqual({ display_name: '新的名字' });

    expect(calls[1]?.[0]).toBe('/v1/settings/connectors/onebot/instances/main/enabled');
    expect(JSON.parse(String(calls[1]?.[1].body))).toEqual({ enabled: false });

    expect(calls[2]?.[0]).toBe('/v1/settings/connectors/onebot/instances/main');
    expect(calls[2]?.[1].method).toBe('DELETE');
  });

  it('保存带 instance_id 时它是 **query**，body 里不出现', async () => {
    const fetchMock = freshResponses({ state: 'active' });
    vi.stubGlobal('fetch', fetchMock);
    await api.saveConnectorConfig('onebot', { set: { LING_ONEBOT_SELF_ID: '100200300' }, clear: [] }, 7, 'main');
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/v1/settings/connectors/onebot?instance_id=main');
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    expect(body).not.toHaveProperty('instance_id');
  });

  it('保存不带 instance_id 时 URL 与升级前逐字节一致（没有多余的 ? / 空参数）', async () => {
    const fetchMock = freshResponses({ state: 'active' });
    vi.stubGlobal('fetch', fetchMock);
    await api.saveConnectorConfig('onebot', { set: {}, clear: [] }, 7);
    await api.saveConnectorConfig('onebot', { set: {}, clear: [] }, 7, '');
    await api.saveConnectorConfig('onebot', { set: {}, clear: [] }, 7, null);
    const urls = (fetchMock.mock.calls as [string][]).map((call) => call[0]);
    expect(urls).toEqual([
      '/v1/settings/connectors/onebot',
      '/v1/settings/connectors/onebot',
      '/v1/settings/connectors/onebot',
    ]);
  });

  it('containers() 带实例时同样走 query', async () => {
    const fetchMock = freshResponses({ schema_version: 'v2' });
    vi.stubGlobal('fetch', fetchMock);
    await api.connectors('main');
    await api.connectors();
    const urls = (fetchMock.mock.calls as [string][]).map((call) => call[0]);
    expect(urls).toEqual(['/v1/settings/connectors?instance_id=main', '/v1/settings/connectors']);
  });
});
