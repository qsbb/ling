/**
 * 接入器页（`/settings/connectors`）的渲染断言、变更请求构造与固定 API 路径。
 *
 * 这里保护的是**四态互不串**与**保存结果不撒谎**，不是样式细节：
 * - "未配置"绝不能说成"连接失败/未连接"——那会让人去查一条从未建立的连接；
 * - "已配置未连接"与"已关闭"必须是两个态（后者连接根本没被尝试过）；
 * - "已连接"不能再提示去补变量；
 * - 标记互斥：一张卡只带它自己那个状态的 class 与文案；
 * - **`rolled_back` 必须被说成失败**（后端此时仍返回 202，最容易骗人）；
 * - 密钥在数据结构上就不可能回显（`FieldDraft` 的 secret 初值恒为空串）。
 *
 * 交互（真的输入、真的点保存）在 `connectors.browser.test.tsx` 里用真 Chromium
 * 跑；这里用 `react-dom/server` 断言"画了什么"，与 `dashboard.test.ts` 一致。
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError, connectorErrorField } from './api';
import {
  ACCESS_MODES,
  buildConnectorChange,
  connectorFieldState,
  connectorRuntimeLine,
  describeChangeResult,
  describeConnectorStatus,
  describeResetResult,
  describeSaveError,
  describeVerifyState,
  draftFromFields,
  ensureInstanceForm,
  formatAsOf,
  formsOf,
} from './components/ConnectorsPage';
import { SETTINGS_GROUPS } from './components/SettingsHub';
import {
  FIELD_IDS_FOR_TESTS,
  makeAccessGroup,
  makeConnector,
  makeField,
  makeResponse,
  onebotFields,
  renderAccessCard,
  renderConnectorCard,
} from './connectors.fixtures.test-helpers';
import { parseRoute, routePath } from './route';
import type { ConnectorChangeResult, ConnectorView } from './types';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const NOT_CONFIGURED = makeConnector();
const DISABLED = makeConnector({
  id: 'qqbot',
  label: 'QQ 官方机器人（QQ 开放平台）',
  configured: true,
  enabled: false,
  status: 'disabled',
  status_reason: 'qqbot_channel_disabled',
  status_message: '凭据已配好，但渠道开关是关的',
  guidance: '凭据已就位，只差开关：把 `LING_QQBOT_ENABLED` 打开',
});
const DISCONNECTED = makeConnector({
  status: 'configured_disconnected',
  status_reason: 'onebot_ws_server_waiting',
  status_message: 'WS 服务端在监听，但还没有客户端连入',
  configured: true,
  enabled: true,
  guidance: '服务端已在监听，等 NapCat 连入',
  runtime: { connections: 0, received: 0 },
});
const CONNECTED = makeConnector({
  status: 'connected',
  status_reason: 'onebot_ws_server_client',
  status_message: 'NapCat 已连入 WS 服务端',
  configured: true,
  enabled: true,
  guidance: '已连接，没有需要改的。',
  runtime: { connections: 1, received: 132 },
});

function render(view: ConnectorView): string {
  return renderToStaticMarkup(renderConnectorCard(view));
}

/** 取出「怎么改」那一段原文，单独对它断言。 */
function guidanceOf(markup: string): string {
  const match = /<p class="connector-card__guidance"[^>]*>([\s\S]*?)<\/p>/.exec(markup);
  return match?.[1] ?? '';
}

/** 取某个 data-testid 元素的**开标签**（断言 disabled/aria 时用，不猜属性顺序）。 */
function openTagOf(markup: string, testid: string): string {
  const index = markup.indexOf(`data-testid="${testid}"`);
  if (index < 0) return '';
  const start = markup.lastIndexOf('<', index);
  const end = markup.indexOf('>', index);
  return markup.slice(start, end + 1);
}

/** 结论条：`data-tone` 与去掉标签后的文字。 */
function noticeOf(markup: string): { tone: string; text: string; detail: string } {
  const block = /<div class="connector-notice[^"]*"[^>]*data-testid="[^"]*notice"[^>]*>([\s\S]*?)<\/div><\/div>/.exec(
    markup,
  );
  const tone = /data-tone="(ok|warn|error)"/.exec(markup)?.[1] ?? '';
  const strong = /<strong>([\s\S]*?)<\/strong>/.exec(block?.[1] ?? '')?.[1] ?? '';
  const detail = /connector-notice__detail">([\s\S]*?)<\/div>/.exec(block?.[1] ?? '')?.[1] ?? '';
  const strip = (text: string) => text.replace(/<[^>]+>/g, '');
  return { tone, text: strip(strong), detail: strip(detail) };
}

function changeResult(overrides: Partial<ConnectorChangeResult> = {}): ConnectorChangeResult {
  return {
    schema_version: 'v2',
    operation_id: 'crev_1',
    scope: 'onebot',
    generation: 4,
    status: 'active',
    state: 'active',
    changed: true,
    revision_id: 'crev_1',
    active_revision_id: 'crev_1',
    ack_count: 1,
    expected_count: 1,
    started_at: '2026-11-10T00:00:00+00:00',
    finished_at: '2026-11-10T00:00:01+00:00',
    error_code: null,
    error_safe: null,
    timings: {},
    ...overrides,
  };
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

describe('四态渲染互不串', () => {
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
    // 指引里的变量名要用等宽渲染，不能把反引号当正文画出来。
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

describe('可编辑：每个字段都有控件，密钥只有密码框', () => {
  it('普通字段是可编辑输入框，带当前值（不是只读文本）', () => {
    const markup = render(
      makeConnector({
        fields: [makeField({ key: 'LING_ONEBOT_API_BASE', value: 'http://127.0.0.1:5700' })],
      }),
    );
    expect(markup).toContain('data-testid="connector-input-LING_ONEBOT_API_BASE"');
    expect(markup).toContain('type="text"');
    expect(markup).toContain('value="http://127.0.0.1:5700"');
    // 有输入框、有保存按钮，才叫"能配置"。
    expect(markup).toContain('data-testid="connector-onebot-save"');
    expect(markup).not.toContain('readonly');
  });

  it('12 个真实键名都渲染出了自己的输入控件', () => {
    const markup = render(makeConnector({ fields: onebotFields() }));
    expect(FIELD_IDS_FOR_TESTS.onebot).toHaveLength(12);
    for (const key of FIELD_IDS_FOR_TESTS.onebot) {
      expect(markup).toContain(`data-testid="connector-input-${key}"`);
    }
  });

  it('密钥是 password 框，值恒空，占位符说"已设置（留空则不修改）"', () => {
    const markup = render(
      makeConnector({
        fields: [
          makeField({
            key: 'LING_ONEBOT_WS_TOKEN',
            kind: 'secret',
            configured: true,
            preview: 'secret••••',
          }),
        ],
      }),
    );
    expect(markup).toContain('type="password"');
    expect(markup).toContain('已设置（留空则不修改）');
    // 输入框的值必须是空的：任何路径都不该把密钥（或它的预览）塞进 value。
    expect(markup).toMatch(/type="password"[^>]*value=""/);
    expect(markup).not.toMatch(/type="password"[^>]*value="secret/);
    // 预览只作为只读文本出现在旁边。
    expect(markup).toContain('当前值 secret••••');
    // 清除密钥走独立按钮（clear 语义），不是"把输入框清空"。
    expect(markup).toContain('data-testid="connector-clear-LING_ONEBOT_WS_TOKEN"');
  });

  it('草稿里密钥初值恒为空串（数据结构上就没有回显路径）', () => {
    const draft = draftFromFields([
      makeField({ key: 'LING_QQBOT_CLIENT_SECRET', kind: 'secret', configured: true }),
    ]);
    expect(draft['LING_QQBOT_CLIENT_SECRET']).toEqual({ kind: 'secret', value: '' });
  });

  it('开关是 checkbox（并写出开/关），名单是 textarea', () => {
    const markup = render(
      makeConnector({
        fields: [
          makeField({ key: 'LING_ONEBOT_SUPPORTS_GROUP', kind: 'switch', value: true }),
          makeField({ key: 'LING_ONEBOT_MEDIA_HOSTS', kind: 'list', value: ['a.example'] }),
        ],
      }),
    );
    expect(markup).toContain('type="checkbox"');
    expect(markup).toContain('connector-switch__text');
    expect(markup).toMatch(/connector-switch__text[^>]*>开</);
    expect(markup).toContain('<textarea');
    expect(markup).toContain('a.example');
  });

  it('权限模式是 select，且带"清除（回退默认）"这一项', () => {
    const markup = renderToStaticMarkup(renderAccessCard(makeAccessGroup()));
    expect(markup).toContain('<select');
    for (const mode of ACCESS_MODES) expect(markup).toContain(`value="${mode}"`);
    // 空串在下拉里表达不出来："清除覆盖"必须是独立选项（走 clear 而不是 set:''）。
    expect(markup).toContain('清除（回退默认 off）');
  });

  it('白名单组不是渠道：没有四态徽标，但有来源、控件与保存按钮', () => {
    const markup = renderToStaticMarkup(renderAccessCard(makeAccessGroup()));
    expect(markup).toContain('data-testid="connector-access"');
    expect(markup).toContain('data-testid="connector-access-save"');
    expect(markup).toContain('LING_ACCESS_MODE');
    expect(markup).toContain('LING_ACCESS_LIST');
    expect(markup).not.toContain('connector-card--warn');
  });

  it('非 owner 只读：控件与保存按钮 disabled，并说明原因', () => {
    const markup = render(makeConnector({ editable: false }));
    expect(markup).toContain('disabled');
    expect(markup).toContain('只有所有者可以修改');
  });

  it('点了「清除」：输入框换成一段说明（不会把空串当删除发给后端）', () => {
    const markup = renderToStaticMarkup(
      renderConnectorCard(makeConnector({ fields: onebotFields() }), {
        clearKeys: ['LING_ONEBOT_WS_TOKEN'],
      }),
    );
    expect(markup).toContain('data-testid="connector-cleared-LING_ONEBOT_WS_TOKEN"');
    expect(markup).toContain('保存后会清除这一项');
    expect(markup).not.toMatch(/data-testid="connector-input-LING_ONEBOT_WS_TOKEN"/);
  });
});

describe('变更请求体：只发改过的键，密钥空串=不改', () => {
  const fields = [
    makeField({ key: 'LING_ONEBOT_API_BASE', value: 'http://127.0.0.1:5700', source: 'env' }),
    makeField({ key: 'LING_ONEBOT_SELF_ID', value: '100200300', source: 'default' }),
    makeField({ key: 'LING_ONEBOT_WS_TOKEN', kind: 'secret', configured: true }),
    makeField({ key: 'LING_ONEBOT_SUPPORTS_GROUP', kind: 'switch', value: true }),
  ];

  it('什么都没改：请求体为空（不发没动过的项）', () => {
    const { change, dirty } = buildConnectorChange(fields, draftFromFields(fields));
    expect(change).toEqual({ set: {}, clear: [] });
    expect(dirty).toBe(false);
  });

  it('改一个文本字段：只发它', () => {
    const draft = draftFromFields(fields);
    draft['LING_ONEBOT_API_BASE'] = { kind: 'text', value: 'http://192.0.2.10:5700' };
    const { change, dirty } = buildConnectorChange(fields, draft);
    expect(change.set).toEqual({ LING_ONEBOT_API_BASE: 'http://192.0.2.10:5700' });
    expect(change.clear).toEqual([]);
    expect(dirty).toBe(true);
  });

  it('密钥留空 → 完全不出现在 set 里（不修改，不是删除）', () => {
    const draft = draftFromFields(fields);
    draft['LING_ONEBOT_API_BASE'] = { kind: 'text', value: 'http://192.0.2.10:5700' };
    const { change } = buildConnectorChange(fields, draft);
    expect('LING_ONEBOT_WS_TOKEN' in change.set).toBe(false);
    expect(change.clear).not.toContain('LING_ONEBOT_WS_TOKEN');
  });

  it('密钥填了新值 → 进 set；点「清除」→ 进 clear（不是 set:""）', () => {
    const filled = draftFromFields(fields);
    filled['LING_ONEBOT_WS_TOKEN'] = { kind: 'secret', value: 'new-token' };
    expect(buildConnectorChange(fields, filled).change.set).toEqual({
      LING_ONEBOT_WS_TOKEN: 'new-token',
    });

    const cleared = buildConnectorChange(fields, draftFromFields(fields), [
      'LING_ONEBOT_WS_TOKEN',
    ]);
    expect(cleared.change.set).toEqual({});
    expect(cleared.change.clear).toEqual(['LING_ONEBOT_WS_TOKEN']);
  });

  it('清空一个有值的文本项 → clear（回退 env/默认），不是 set:""', () => {
    const draft = draftFromFields(fields);
    draft['LING_ONEBOT_API_BASE'] = { kind: 'text', value: '' };
    const { change } = buildConnectorChange(fields, draft);
    expect(change.clear).toEqual(['LING_ONEBOT_API_BASE']);
    expect(change.set).toEqual({});
  });

  it('开关只在真的翻转时才发（布尔，不是 1/0 字符串）', () => {
    const flipped = draftFromFields(fields);
    flipped['LING_ONEBOT_SUPPORTS_GROUP'] = { kind: 'switch', value: false };
    expect(buildConnectorChange(fields, flipped).change.set).toEqual({
      LING_ONEBOT_SUPPORTS_GROUP: false,
    });
  });

  it('名单：trim + 去空，但保留字符串（QQ 号前导零不能变成数字）', () => {
    const listFields = [makeField({ key: 'LING_ACCESS_LIST', kind: 'list', value: [] })];
    const draft = draftFromFields(listFields);
    draft['LING_ACCESS_LIST'] = { kind: 'list', items: ['0123', '  ', '0456'] };
    expect(buildConnectorChange(listFields, draft).change.set).toEqual({
      LING_ACCESS_LIST: ['0123', '0456'],
    });
  });
});

describe('保存结果：不乐观更新，rolled_back 是失败', () => {
  it('active → 说"已生效"', () => {
    const notice = describeChangeResult(changeResult());
    expect(notice.tone).toBe('ok');
    expect(notice.text).toContain('已生效');
  });

  it('unchanged → 说"没有变化"，不说生效', () => {
    const notice = describeChangeResult(changeResult({ state: 'unchanged', changed: false }));
    expect(notice.tone).toBe('ok');
    expect(notice.text).toContain('没有变化');
  });

  it('rolled_back（HTTP 202）→ 明确说"没生效、已恢复旧配置"', () => {
    const notice = describeChangeResult(
      changeResult({
        state: 'rolled_back',
        changed: false,
        generation: 3,
        revision_id: 'crev_1',
        active_revision_id: 'crev_0',
        error_code: 'connector_start_failed',
        error_safe: '切换时发生异常：RuntimeError',
      }),
    );
    expect(notice.tone).toBe('error');
    expect(notice.text).toContain('新配置没起来');
    expect(notice.text).toContain('已恢复旧配置');
    expect(notice.text).toContain('没有生效');
    // 除了"没有生效"这句否定，不许再出现任何"已生效"。
    expect(notice.text.replace('没有生效', '')).not.toContain('已生效');
    expect(notice.detail).toContain('切换时发生异常');
  });

  it('degraded → 说"没能完全生效"，绝不说"连上了"', () => {
    const notice = describeChangeResult(
      changeResult({
        state: 'degraded',
        error_code: 'remote_unreachable',
        error_safe: '配置已在本机生效，但远端还没连上',
      }),
    );
    expect(notice.tone).toBe('warn');
    expect(notice.text).toContain('没能完全生效');
    expect(notice.text).not.toContain('已生效：');
  });

  it('不认识的终态 → 不当作成功', () => {
    const notice = describeChangeResult(changeResult({ state: 'something_new' }));
    expect(notice.tone).toBe('warn');
    expect(notice.text).toContain('不认识的终态');
    expect(notice.detail).toContain('不能当作成功');
  });

  it('生效终态的中文映射互不相同', () => {
    const labels = ['active', 'unchanged', 'activating', 'rolled_back', 'degraded', 'zzz'].map(
      describeVerifyState,
    );
    expect(new Set(labels).size).toBe(labels.length);
  });

  it('rolled_back 在卡片上就是红色失败，带"没生效"字样', () => {
    const markup = renderToStaticMarkup(
      renderConnectorCard(CONNECTED, { notice: describeChangeResult(changeResult({ state: 'rolled_back' })) }),
    );
    const notice = noticeOf(markup);
    expect(notice.tone).toBe('error');
    expect(notice.text).toContain('已恢复旧配置');
    expect(markup).toContain('role="alert"');
  });

  it('active 在卡片上是 ok 提示，不带 role="alert"', () => {
    const markup = renderToStaticMarkup(
      renderConnectorCard(CONNECTED, { notice: describeChangeResult(changeResult()) }),
    );
    const notice = noticeOf(markup);
    expect(notice.tone).toBe('ok');
    expect(notice.text).toContain('已生效');
    expect(markup).toContain('role="status"');
  });
});

describe('保存失败：状态码各说各的话', () => {
  function apiError(status: number, code: string, message: string, detail: string | null = null) {
    return new ApiError(status, code, message, false, detail);
  }

  it('409 并发冲突：说"别处改过了"并要求刷新后重试', () => {
    const notice = describeSaveError(
      apiError(409, 'conflict', '配置刚被别处改过，请刷新后再保存', 'generation_conflict'),
    );
    expect(notice.tone).toBe('error');
    expect(notice.text).toContain('别处已经改过了');
    expect(notice.text).toContain('没有提交');
    expect(notice.retryWithReload).toBe(true);
    expect(notice.detail).toContain('刷新状态');
  });

  it('409 另一处正在生效：与代际冲突分开说', () => {
    const notice = describeSaveError(
      apiError(409, 'conflict', '另一处保存正在生效，等它完成后再试', 'activation_in_progress'),
    );
    expect(notice.text).toContain('另一处保存正在生效');
    expect(notice.retryWithReload).toBe(true);
  });

  it('422 字段不合法：点名那个键，并说明一个连接都没停过', () => {
    const error = apiError(422, 'invalid_request', '只接受 http/https 地址', 'invalid_field');
    Object.defineProperty(error, 'field', { value: 'LING_ONEBOT_API_BASE', enumerable: false });
    expect(connectorErrorField(error)).toBe('LING_ONEBOT_API_BASE');

    const notice = describeSaveError(error);
    expect(notice.tone).toBe('error');
    expect(notice.text).toContain('LING_ONEBOT_API_BASE');
    expect(notice.text).toContain('一个连接都没停过');
    expect(notice.detail).toContain('只接受 http/https 地址');
  });

  it('422 没有 field：仍然说"没停过连接"，但不编字段名', () => {
    const notice = describeSaveError(apiError(422, 'invalid_request', '白名单模式但名单是空的'));
    expect(notice.text).toContain('一个连接都没停过');
    expect(notice.text).not.toContain('配置项 ');
  });

  it('422 的卡片渲染：只有出错那一行被标红（逐字段指出）', () => {
    const markup = renderToStaticMarkup(
      renderConnectorCard(makeConnector({ fields: onebotFields() }), {
        fieldError: 'LING_ONEBOT_WS_PORT',
      }),
    );
    expect(markup).toContain('data-invalid="true"');
    expect(markup).toContain('connector-field--invalid');
    // 只标一行：数一下 data-invalid 出现次数。
    expect(markup.match(/data-invalid="true"/g)).toHaveLength(1);
    expect(markup).toContain('这一项没通过校验');
  });

  it('503 切换器不可用：如实说是进程能力问题，不是填错了', () => {
    const notice = describeSaveError(
      apiError(503, 'invalid_request', '这个进程不负责切换连接器，改配置请到 API 进程'),
    );
    expect(notice.text).toContain('切换不了接入器');
    expect(notice.detail).toContain('不是你填错了');
  });

  it('403 两种原因分开说（来源 / 非 owner）', () => {
    const origin = describeSaveError(
      apiError(403, 'forbidden', '这个请求的来源不可信', 'bad_origin'),
    );
    expect(origin.text).toContain('来源不可信');
    const notOwner = describeSaveError(
      apiError(403, 'forbidden', '只有所有者可以修改', 'not_owner'),
    );
    expect(notOwner.text).toContain('只有所有者');
  });

  it('409 的卡片渲染：红色结论条 + 「刷新后重试」按钮', () => {
    const markup = renderToStaticMarkup(
      renderConnectorCard(CONNECTED, {
        notice: describeSaveError(
          apiError(409, 'conflict', '配置刚被别处改过，请刷新后再保存', 'generation_conflict'),
        ),
      }),
    );
    const notice = noticeOf(markup);
    expect(notice.tone).toBe('error');
    expect(notice.text).toContain('别处已经改过了');
    expect(markup).toContain('刷新后重试');
    expect(markup).toContain('data-testid="connector-onebot-notice-reload"');
  });
});

describe('提交中：按钮禁用、表单只读', () => {
  it('saving=true 时保存按钮禁用且文案变成"保存中…"', () => {
    const markup = renderToStaticMarkup(
      renderConnectorCard(CONNECTED, { saving: true, formNote: '正在保存：要等服务端切换完成才算生效…' }),
    );
    expect(openTagOf(markup, 'connector-onebot-save')).toContain('disabled');
    expect(markup).toContain('保存中…');
    expect(markup).toContain('正在保存');
    // 提交中输入框也禁用：不能在飞行中继续改（否则请求体与被展示的内容不一致）。
    expect(openTagOf(markup, 'connector-input-LING_ONEBOT_API_BASE')).toContain('disabled');
    // 「撤销修改」和「刷新状态」也禁用，避免飞行中改状态。
    expect(openTagOf(markup, 'connector-onebot-revert')).toContain('disabled');
    expect(openTagOf(markup, 'connector-onebot-reload')).toContain('disabled');
  });
});

describe('恢复默认：ok:false 绝不说成功', () => {
  it('全部清干净 → 说已恢复默认，并列出清掉的键', () => {
    const notice = describeResetResult({
      schema_version: 'v2',
      ok: true,
      results: [
        { scope: 'onebot', state: 'active', cleared: ['LING_ONEBOT_API_BASE'] },
        { scope: 'qqbot', state: 'unchanged', cleared: [] },
      ],
    });
    expect(notice.tone).toBe('ok');
    expect(notice.text).toContain('已恢复默认');
    expect(notice.detail).toContain('LING_ONEBOT_API_BASE');
  });

  it('有一个 scope 回滚 → 失败，不报"部分成功"', () => {
    const notice = describeResetResult({
      schema_version: 'v2',
      ok: false,
      results: [
        { scope: 'onebot', state: 'rolled_back', cleared: [], error_code: 'connector_start_failed' },
        { scope: 'qqbot', state: 'active', cleared: [] },
      ],
    });
    expect(notice.tone).toBe('error');
    expect(notice.text).toContain('没有全部成功');
    expect(notice.text).not.toContain('已恢复默认');
    expect(notice.detail).toContain('onebot');
  });
});

describe('字段：密钥只报"设没设"', () => {
  it('secret 只画已设置/未设置，永远不画值', () => {
    expect(connectorFieldState(makeField({ key: 'A', kind: 'secret', configured: true }))).toBe(
      '已设置',
    );
    expect(
      connectorFieldState(
        makeField({ key: 'A', kind: 'secret', configured: false, set: false }),
      ),
    ).toBe('未设置');

    const markup = render(
      makeConnector({
        fields: [
          makeField({ key: 'LING_QQBOT_CLIENT_SECRET', kind: 'secret', configured: true }),
        ],
      }),
    );
    expect(markup).toContain('password');
    expect(markup).not.toContain('should-not-render');
  });

  it('非密钥照原样显示，缺值说"未设置"；开关说开/关（不是"未设置"）', () => {
    expect(
      connectorFieldState(makeField({ key: 'A', kind: 'text', value: '127.0.0.1' })),
    ).toBe('127.0.0.1');
    expect(
      connectorFieldState(makeField({ key: 'A', kind: 'text', value: null, configured: false })),
    ).toBe('未设置');
    expect(connectorFieldState(makeField({ key: 'A', kind: 'switch', value: true }))).toBe('开');
    expect(connectorFieldState(makeField({ key: 'A', kind: 'switch', value: false }))).toBe('关');
    expect(
      connectorFieldState(makeField({ key: 'A', kind: 'list', value: ['0123', '0456'] })),
    ).toBe('0123, 0456');
  });

  it('字段名与来源都在渲染里（长变量名靠 CSS 换行，不裁掉）', () => {
    const markup = render(
      makeConnector({
        fields: [
          makeField({
            key: 'LING_QQBOT_WEBHOOK_PUBLIC_KEY',
            kind: 'text',
            value: null,
            configured: false,
          }),
        ],
      }),
    );
    expect(markup).toContain('LING_QQBOT_WEBHOOK_PUBLIC_KEY');
    expect(markup).toContain('connector-field__state--unset');
    expect(markup).toContain('来自环境变量');
  });
});

describe('运行时那一行：没有可观察的连接就不编数字', () => {
  it('connections 为 null（只配了 HTTP 上报）时整行不显示', () => {
    expect(connectorRuntimeLine({ connections: null, received: null })).toBeNull();
  });

  it('零连接与服务端在等客户端都能读出来', () => {
    expect(connectorRuntimeLine({ connections: 0, received: 0 })).toBe(
      '当前没有连接，已收 0 帧事件',
    );
  });

  it('已连接但一帧都没收到时，把这件事说出来', () => {
    expect(connectorRuntimeLine({ connections: 1, received: 0 })).toContain('还没推过任何数据');
    expect(connectorRuntimeLine({ connections: 1, received: 132 })).toBe(
      '当前 1 条连接，已收 132 帧事件',
    );
  });
});

describe('页面状态：草稿从 GET 来，按 (scope, instance_id) 分区', () => {
  it('formsOf 为两个渠道 + access 各建一份**默认实例**的草案；密钥初值为空', () => {
    const forms = formsOf(makeResponse());
    // 键是 `instanceFormKey(scope, 'default')`，不是裸 scope：草案必须按实例分区，
    // 否则在 A 里改了不保存、切到 B，B 的输入框会画 A 的草稿，保存还成功。
    expect(Object.keys(forms).sort()).toEqual([
      'access::default',
      'onebot::default',
      'qqbot::default',
    ]);
    expect(forms['onebot::default']?.draft['LING_ONEBOT_API_BASE']).toEqual({
      kind: 'text',
      value: 'http://127.0.0.1:5700',
    });
    expect(forms['onebot::default']?.draft['LING_ONEBOT_WS_TOKEN']).toEqual({
      kind: 'secret',
      value: '',
    });
    expect(forms['access::default']?.draft['LING_ACCESS_MODE']).toEqual({
      kind: 'select',
      value: 'off',
    });
    expect(forms['onebot::default']?.clearKeys).toEqual([]);
    // 非默认实例**不给**初始草案：它只能按自己那份视图在切换时建，
    // 拿默认实例的值预填就是冒充。
    expect(forms['onebot::alpha']).toBeUndefined();
  });

  it('GET 里缺 access 段也不崩（老后端），只是没有白名单卡', () => {
    const body = { ...makeResponse(), access: undefined as never };
    const forms = formsOf(body);
    expect(forms['access::default']).toEqual({ draft: {}, clearKeys: [] });
  });

  it('ensureInstanceForm：已建好的原样返回，没建好的按**自己的**字段建', () => {
    const fields = onebotFields();
    const edited = { draft: draftFromFields(fields), clearKeys: [] };
    edited.draft['LING_ONEBOT_API_BASE'] = { kind: 'text', value: 'aaa' };
    const forms = { 'onebot::alpha': edited };

    // 已存在 → 同引用（切回 A 时它的未保存内容还在）。
    expect(ensureInstanceForm(forms, 'onebot', 'alpha', fields)['onebot::alpha']).toBe(edited);

    // 不存在 → 按传进来的字段建，且**不是** alpha 那一份。
    const withBeta = ensureInstanceForm(forms, 'onebot', 'beta', fields);
    expect(withBeta['onebot::alpha']).toBe(edited);
    expect(withBeta['onebot::beta']?.draft['LING_ONEBOT_API_BASE']).toEqual({
      kind: 'text',
      value: 'http://127.0.0.1:5700',
    });

    // 拿不到某实例的视图（null → 传空数组）时，建出来的是空表单，不是别人的值。
    const blank = ensureInstanceForm(forms, 'onebot', 'gamma', []);
    expect(blank['onebot::gamma']).toEqual({ draft: {}, clearKeys: [] });
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
});

describe('api.connectors / saveConnectorConfig / resetConnectorConfig', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('读取只调用固定路径，不带参数', async () => {
    const body = makeResponse();
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(body));
    vi.stubGlobal('fetch', fetchMock);

    await expect(api.connectors()).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith('/v1/settings/connectors', {
      credentials: 'same-origin',
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('保存走 PUT + If-Match（当前 generation），请求体是 JSON', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(changeResult(), 202));
    vi.stubGlobal('fetch', fetchMock);

    const result = await api.saveConnectorConfig(
      'onebot',
      { set: { LING_ONEBOT_API_BASE: 'http://192.0.2.10:5700' }, clear: [] },
      7,
    );
    expect(result.state).toBe('active');
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/v1/settings/connectors/onebot');
    expect(init.method).toBe('PUT');
    expect((init.headers as Record<string, string>)['If-Match']).toBe('7');
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
    expect(JSON.parse(String(init.body))).toEqual({
      set: { LING_ONEBOT_API_BASE: 'http://192.0.2.10:5700' },
      clear: [],
    });
  });

  it('保存 409：抛 ApiError（status=409），detail 是稳定原因码', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse(
          {
            error: {
              code: 'conflict',
              message: '配置刚被别处改过，请刷新后再保存',
              detail: 'generation_conflict',
            },
          },
          409,
        ),
      ),
    );
    const error = await api
      .saveConnectorConfig('onebot', { set: {}, clear: [] }, 1)
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(409);
    expect((error as ApiError).detail).toBe('generation_conflict');
  });

  it('保存 422：error.field 被抬出来（逐字段定位靠它）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse(
          {
            error: {
              code: 'invalid_request',
              message: '端口必须是数字',
              detail: 'invalid_field',
              field: 'LING_ONEBOT_WS_PORT',
            },
          },
          422,
        ),
      ),
    );
    const error = await api
      .saveConnectorConfig('onebot', { set: { LING_ONEBOT_WS_PORT: 'abc' }, clear: [] }, 1)
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(connectorErrorField(error)).toBe('LING_ONEBOT_WS_PORT');
    // 非写路径的错误永远没有 field（不假装知道是哪个字段）。
    expect(connectorErrorField(new ApiError(422, 'invalid_request', 'x'))).toBeNull();
  });

  it('恢复默认走 POST /v1/settings/connectors/reset', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ schema_version: 'v2', ok: true, results: [] }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(api.resetConnectorConfig()).resolves.toEqual({
      schema_version: 'v2',
      ok: true,
      results: [],
    });
    expect(fetchMock).toHaveBeenCalledWith('/v1/settings/connectors/reset', {
      method: 'POST',
      credentials: 'same-origin',
    });
  });
});

describe('时间戳', () => {
  it('解析不了就照原样显示，不编一个时间', () => {
    expect(formatAsOf('not-a-date')).toBe('not-a-date');
    expect(formatAsOf('2026-11-07T01:02:03+00:00')).toMatch(
      /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/,
    );
  });
});
