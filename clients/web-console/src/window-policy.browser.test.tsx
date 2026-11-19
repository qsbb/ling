import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { fileURLToPath } from 'node:url';
import { chromium, type Browser, type BrowserContext } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';

const root = fileURLToPath(new URL('..', import.meta.url));
let browser: Browser | null = null;
let server: ViteDevServer | null = null;
let address = '';
let error: string | null = null;
const contexts: BrowserContext[] = [];

type TestPolicyValue = string | boolean | number | null;
type TestFieldKey =
  | 'persona_id'
  | 'private_may_stay_silent'
  | 'group_mention_requires_reply'
  | 'group_attention_deferred'
  | 'group_turn_budget'
  | 'rest_may_skip';
type TestField = {
  global_value: TestPolicyValue;
  instance_override: TestPolicyValue;
  has_instance_override: boolean;
  session_override: TestPolicyValue;
  has_session_override: boolean;
  effective: TestPolicyValue;
  source: 'global' | 'instance' | 'session';
};
type TestFields = Record<TestFieldKey, TestField>;

const initialFields: TestFields = {
  persona_id: {
    global_value: 'p-global', instance_override: null, has_instance_override: false,
    session_override: null, has_session_override: false, effective: 'p-global', source: 'global',
  },
  private_may_stay_silent: {
    global_value: true, instance_override: null, has_instance_override: false,
    session_override: false, has_session_override: true, effective: false, source: 'session',
  },
  group_mention_requires_reply: {
    global_value: true, instance_override: null, has_instance_override: false,
    session_override: null, has_session_override: false, effective: true, source: 'global',
  },
  group_attention_deferred: {
    global_value: true, instance_override: false, has_instance_override: true,
    session_override: null, has_session_override: false, effective: false, source: 'instance',
  },
  group_turn_budget: {
    global_value: 4, instance_override: null, has_instance_override: false,
    session_override: 5, has_session_override: true, effective: 5, source: 'session',
  },
  rest_may_skip: {
    global_value: true, instance_override: null, has_instance_override: false,
    session_override: null, has_session_override: false, effective: true, source: 'global',
  },
};

beforeAll(async () => {
  try {
    server = await createServer({ configFile: false, root, plugins: [react()], logLevel: 'silent', server: { host: '127.0.0.1', port: 0 } });
    await server.listen();
    const socket = server.httpServer?.address();
    if (!socket || typeof socket === 'string') throw new Error('Vite 没有可用端口');
    address = `http://127.0.0.1:${socket.port}`;
    browser = await chromium.launch();
  } catch (cause) {
    error = cause instanceof Error ? cause.message : String(cause);
  }
}, 90000);

afterEach(async () => {
  while (contexts.length) await contexts.pop()?.close();
});
afterAll(async () => { await browser?.close(); await server?.close(); });

const json = (body: unknown, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) });

function resolvedFields(fields: TestFields): TestFields {
  return Object.fromEntries(Object.entries(fields).map(([key, field]) => {
    const result = { ...field };
    if (result.has_session_override) {
      result.effective = result.session_override;
      result.source = 'session';
    } else if (result.has_instance_override) {
      result.effective = result.instance_override;
      result.source = 'instance';
    } else {
      result.effective = result.global_value;
      result.source = 'global';
    }
    return [key, result];
  })) as TestFields;
}

function withoutInstanceOverrides(fields: TestFields): TestFields {
  return resolvedFields(Object.fromEntries(Object.entries(fields).map(([key, field]) => [key, {
    ...field,
    instance_override: null,
    has_instance_override: false,
  }])) as TestFields);
}

function policyView(
  fields: TestFields,
  revision: number,
  originStatus: 'known' | 'unknown' | 'not_applicable',
) {
  const privateField = fields.private_may_stay_silent;
  return {
    field: 'private_may_stay_silent',
    global_value: privateField.global_value,
    override: privateField.has_session_override ? privateField.session_override : null,
    effective: privateField.effective,
    source: privateField.source,
    fields,
    origin_status: originStatus,
    instance_scope: originStatus === 'known' ? 'onebot' : null,
    instance_id: originStatus === 'known' ? 'test-instance' : null,
    global_revision: 0,
    instance_revision: 0,
    session_revision: revision,
  };
}

function applyChange(
  change: { set: Partial<Record<TestFieldKey, TestPolicyValue>>; clear: TestFieldKey[] },
  base: TestFields = initialFields,
): TestFields {
  const fields = structuredClone(base);
  for (const key of change.clear) {
    const field = fields[key];
    field.session_override = null;
    field.has_session_override = false;
  }
  for (const [key, value] of Object.entries(change.set)) {
    const field = fields[key as TestFieldKey];
    field.session_override = value ?? null;
    field.has_session_override = true;
  }
  return resolvedFields(fields);
}

async function openChat(
  role: 'owner' | 'member',
  width: number,
  originStatus: 'known' | 'unknown' | 'not_applicable' = 'not_applicable',
  channel: 'web' | 'onebot' | 'qqbot' = 'web',
  peerId?: string,
) {
  if (!browser) throw new Error(error ?? '浏览器未启动');
  const context = await browser.newContext({ viewport: { width, height: 850 } });
  contexts.push(context);
  const page = await context.newPage();
  const requests: Array<{ method: string; body: unknown }> = [];
  let currentOriginStatus = originStatus;
  let manuallyBound = originStatus === 'known';
  let savedFields = originStatus !== 'known'
    ? withoutInstanceOverrides(structuredClone(initialFields))
    : structuredClone(initialFields);
  let savedRevision = 0;
  await page.route('**/*', async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    if (path === '/v1/auth/me') return route.fulfill(json({ user: { id: 'u', username: 'test', role, subject_id: 's', display_name: '用户' }, test_mode: false }));
    if (path === '/v1/sessions') return route.fulfill(json({ sessions: [{ schema_version: 'v1', session_id: 'w', window_id: 'w', actor_id: 'a', subject_id: 's', title: peerId ? `onebot-test:group:${peerId}` : '测试', channel, peer_id: peerId ?? null, created_at: '2026-11-14T00:00:00Z', updated_at: '2026-11-14T00:00:00Z', message_count: 0, archived: false }] }));
    if (path === '/v1/sessions/w/messages') return route.fulfill(json({ messages: [], active_turn: null }));
    if (path === '/v1/personas') return route.fulfill(json({ items: [{ id: 'p-global', persona_id: 'p-global', name: 'default', display_name: '全局人格' }], active_persona_id: 'p-global', total: 1 }));
    if (path === '/v1/sessions/w/dialogue-policy' && req.method() === 'GET') {
      const fields = resolvedFields(savedFields);
      return route.fulfill(json(policyView(fields, savedRevision, currentOriginStatus)));
    }
    if (path === '/v1/sessions/w/dialogue-policy/preview' && req.method() === 'POST') {
      const body = req.postDataJSON() as { set: Partial<Record<TestFieldKey, TestPolicyValue>>; clear: TestFieldKey[] };
      requests.push({ method: 'preview', body });
      const proposedFields = applyChange(body, savedFields);
      return route.fulfill(json({
        current: policyView(resolvedFields(savedFields), savedRevision, currentOriginStatus),
        proposed: {
          override: proposedFields.private_may_stay_silent.session_override,
          effective: proposedFields.private_may_stay_silent.effective,
          source: proposedFields.private_may_stay_silent.source,
          fields: proposedFields,
        },
        preview_token: `token-${requests.length}`,
        expires_at: 9999999999,
      }));
    }
    if (path === '/v1/sessions/w/dialogue-policy' && req.method() === 'PUT') {
      const body = req.postDataJSON() as { set: Partial<Record<TestFieldKey, TestPolicyValue>>; clear: TestFieldKey[]; preview_token: string };
      requests.push({ method: 'save', body });
      savedFields = applyChange(body, savedFields);
      savedRevision += 1;
      return route.fulfill(json(policyView(savedFields, savedRevision, currentOriginStatus)));
    }
    if (path === '/v1/sessions/w/origin-binding' && req.method() === 'GET') {
      return route.fulfill(json({
        scope: channel,
        candidates: [{ scope: channel, instance_id: 'test-instance', display_name: '测试实例', enabled: true }],
        manually_bound: manuallyBound,
      }));
    }
    if (path === '/v1/sessions/w/origin-binding/preview' && req.method() === 'POST') {
      const body = req.postDataJSON() as { scope: 'onebot' | 'qqbot'; instance_id: string };
      requests.push({ method: 'origin-preview', body });
      const target = { scope: body.scope, instance_id: body.instance_id, display_name: '测试实例', enabled: true };
      return route.fulfill(json({
        current: policyView(resolvedFields(savedFields), savedRevision, currentOriginStatus),
        proposed: policyView(resolvedFields(savedFields), savedRevision, 'known'),
        target,
        preview_token: 'origin-token',
        expires_at: 9999999999,
      }));
    }
    if (path === '/v1/sessions/w/origin-binding' && req.method() === 'PUT') {
      const body = req.postDataJSON() as { scope: 'onebot' | 'qqbot' | null; instance_id: string | null; preview_token: string };
      requests.push({ method: 'origin-save', body });
      manuallyBound = body.scope !== null && body.instance_id !== null;
      currentOriginStatus = manuallyBound ? 'known' : 'unknown';
      return route.fulfill(json(policyView(resolvedFields(savedFields), savedRevision, currentOriginStatus)));
    }
    if (path.startsWith('/v1/')) return route.fulfill(json({ error: { code: 'not_found', message: '目标不存在', retriable: false } }, 404));
    return route.continue();
  });
  await page.goto(`${address}/s/w`, { waitUntil: 'domcontentloaded' });
  await page.locator('[data-testid="message-scroll"]').waitFor();
  return { page, requests };
}

describe('Web 会话对话设置', () => {
  it('快捷加入和移除当前群，不覆盖其他群或发送者', async (ctx) => {
    if (error) { console.warn(error); ctx.skip(); return; }
    const { page } = await openChat('owner', 375, 'known', 'onebot', '100200300');
    let groups = ['100200301'];
    let generation = 4;
    const writes: unknown[] = [];
    await page.route('**/v1/settings/connectors', (route) => route.fulfill(json({ access: {
      editable: true, generation, fields: [
        { key: 'LING_ACCESS_MODE', value: 'whitelist' },
        { key: 'LING_ACCESS_LIST', value: ['100200302'] },
        { key: 'LING_ACCESS_GROUPS', value: groups },
      ],
    } })));
    await page.route('**/v1/settings/connectors/access', (route) => {
      const req = route.request();
      const body = req.postDataJSON() as { set: { LING_ACCESS_GROUPS: string[] } };
      writes.push({ method: req.method(), generation: req.headers()['if-match'], body });
      groups = body.set.LING_ACCESS_GROUPS;
      generation += 1;
      return route.fulfill(json({ state: 'active', generation }));
    });
    await page.getByRole('button', { name: '会话设置' }).click();
    const control = page.getByRole('checkbox', { name: '本群在白名单中' });
    await expect.poll(() => control.isEnabled()).toBe(true);
    expect(await control.isChecked()).toBe(false);
    await control.click();
    await expect.poll(async () => await control.isEnabled() && await control.isChecked()).toBe(true);
    await control.click();
    await expect.poll(async () => await control.isEnabled() && !(await control.isChecked())).toBe(true);
    expect(writes).toEqual([
      { method: 'PUT', generation: '4', body: { set: { LING_ACCESS_GROUPS: ['100200301', '100200300'] }, clear: [] } },
      { method: 'PUT', generation: '5', body: { set: { LING_ACCESS_GROUPS: ['100200301'] }, clear: [] } },
    ]);
  }, 20000);

  for (const width of [375, 1440]) {
    it(`逐字段显示来源并预览保存 set 与 clear，宽度 ${width}`, async (ctx) => {
      if (error) { console.warn(error); ctx.skip(); return; }
      const { page, requests } = await openChat('owner', width, 'known', 'onebot');
      await page.getByRole('button', { name: '会话设置' }).click();
      const panel = page.getByRole('region', { name: '会话设置' });
      await panel.getByText('全局人格 · 全局').waitFor();
      await panel.getByText('不允许选择沉默 · 本会话').waitFor();
      await panel.getByText('即时判断 · 平台实例').waitFor();
      await panel.getByLabel('私聊可沉默').selectOption('inherit');
      await panel.getByLabel('人格').selectOption('persona:none');
      await panel.getByLabel('群聊判断').selectOption('value:false');
      await panel.getByLabel('群聊发言预算').selectOption('value:8');
      await panel.getByRole('button', { name: '预览更改' }).click();
      await panel.getByText('不允许选择沉默 · 本会话 → 允许沉默 · 全局').waitFor();
      await panel.getByText('即时判断 · 平台实例 → 即时判断 · 本会话').waitFor();
      expect(requests[0]).toEqual({
        method: 'preview',
        body: {
          set: { persona_id: null, group_attention_deferred: false, group_turn_budget: 8 },
          clear: ['private_may_stay_silent'],
        },
      });
      await panel.getByRole('button', { name: '确认保存' }).click();
      await panel.getByText('允许沉默 · 全局').waitFor();
      await panel.getByText('不注入人格 · 本会话').waitFor();
      expect(requests[1]).toEqual({
        method: 'save',
        body: {
          set: { persona_id: null, group_attention_deferred: false, group_turn_budget: 8 },
          clear: ['private_may_stay_silent'],
          preview_token: 'token-1',
        },
      });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    }, 20000);
  }

  it('编辑预览内容后会重新获取预览令牌', async (ctx) => {
    if (error) { console.warn(error); ctx.skip(); return; }
    const { page, requests } = await openChat('owner', 375, 'known', 'onebot');
    await page.getByRole('button', { name: '会话设置' }).click();
    const panel = page.getByRole('region', { name: '会话设置' });
    await panel.getByLabel('群聊发言预算').selectOption('value:8');
    await panel.getByRole('button', { name: '预览更改' }).click();
    await panel.getByRole('button', { name: '返回修改' }).click();
    await panel.getByLabel('群聊发言预算').selectOption('value:9');
    await panel.getByRole('button', { name: '预览更改' }).click();
    expect(requests).toEqual([
      { method: 'preview', body: { set: { group_turn_budget: 8 }, clear: [] } },
      { method: 'preview', body: { set: { group_turn_budget: 9 }, clear: [] } },
    ]);
    await panel.getByText('9 次 · 本会话').waitFor();
  }, 20000);

  it('历史平台窗口来源未知时不套用实例覆盖', async (ctx) => {
    if (error) { console.warn(error); ctx.skip(); return; }
    const { page } = await openChat('owner', 375, 'unknown', 'onebot');
    await page.getByRole('button', { name: '会话设置' }).click();
    const panel = page.getByRole('region', { name: '会话设置' });
    await panel.getByText('平台实例来源未知', { exact: false }).waitFor();
    await panel.getByText('先进入队列 · 全局', { exact: true }).waitFor();
    expect(await panel.getByText('即时判断 · 平台实例', { exact: true }).count()).toBe(0);
  }, 20000);

  it('历史平台会话可预览并确认来源绑定', async (ctx) => {
    if (error) { console.warn(error); ctx.skip(); return; }
    const { page, requests } = await openChat('owner', 375, 'unknown', 'onebot');
    await page.getByRole('button', { name: '会话设置' }).click();
    const panel = page.getByRole('region', { name: '会话设置' });
    const origin = panel.getByRole('region', { name: '平台来源' });
    await origin.getByLabel('平台实例来源').selectOption('onebot:test-instance');
    await origin.getByRole('button', { name: '预览绑定' }).click();
    await origin.getByText('来源未知 → 测试实例').waitFor();
    await origin.getByRole('button', { name: '确认' }).click();
    await origin.getByText('onebot/test-instance').waitFor();
    expect(requests).toEqual([
      { method: 'origin-preview', body: { scope: 'onebot', instance_id: 'test-instance' } },
      { method: 'origin-save', body: { scope: 'onebot', instance_id: 'test-instance', preview_token: 'origin-token' } },
    ]);
  }, 20000);

  for (const width of [375, 1440]) {
    it(`重复点击只切换同一个会话面板，宽度 ${width}`, async (ctx) => {
      if (error) { console.warn(error); ctx.skip(); return; }
      const { page } = await openChat('owner', width);
      const button = page.getByRole('button', { name: '会话设置' });
      const panel = page.locator('#active-window-policy .window-policy');
      for (const count of [1, 0, 1, 0, 1]) {
        await button.click();
        await expect.poll(() => button.getAttribute('aria-expanded')).toBe(String(count === 1));
        await expect.poll(() => panel.isVisible()).toBe(count === 1);
        expect(await panel.count()).toBe(1);
      }
    }, 20000);
  }

  it('成员看不到修改入口', async (ctx) => {
    if (error) { console.warn(error); ctx.skip(); return; }
    const { page } = await openChat('member', 1440);
    expect(await page.getByRole('button', { name: '会话设置' }).count()).toBe(0);
  }, 20000);
});

it('日志详情支持键盘展开，缓冲淘汰旧行后仍展开原消息', async (ctx) => {
  if (error) { console.warn(error); ctx.skip(); return; }
  const { page } = await openChat('owner', 375);
  const makeLog = (second: number, message: string) => ({
    timestamp: `2026-11-14T00:00:0${second}Z`, level: 'info', logger: 'ling.behavior',
    kind: 'behavior', event_name: 'inbound_received', message,
    attributes: { attributes: { reason: '这个原因只在详情里', external_event_id: `fixture-${second}` } },
  });
  const current = makeLog(2, 'QQ 群里夜落星河说：「[image]」 · 暂缓处理');
  let items = [makeLog(1, '旧记录'), current];
  await page.route('**/v1/logs*', (route) => route.fulfill(json({ items, buffered: 2, capacity: 2 })));
  await page.goto(`${address}/logs`);
  const row = page.getByTestId('logs-row').filter({ hasText: current.message });
  const summary = row.locator('summary');
  await summary.waitFor();
  expect(await row.getByText('这个原因只在详情里', { exact: true }).isVisible()).toBe(false);
  await summary.press('Enter');
  await row.getByText('这个原因只在详情里', { exact: true }).waitFor();
  items = [current, makeLog(3, '新记录')];
  await page.getByText('新记录', { exact: true }).waitFor();
  expect(await row.getByText('这个原因只在详情里', { exact: true }).isVisible()).toBe(true);
  expect(await page.getByTestId('logs-row').filter({ hasText: '新记录' }).locator('details[open]').count()).toBe(0);
}, 20000);
