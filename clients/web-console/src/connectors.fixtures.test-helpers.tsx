/**
 * 接入器页测试用的**真实契约固件**（SSR 测试与真浏览器测试共用）。
 *
 * 为什么单独一个文件：真浏览器测试要给 `page.route` 返回一整份
 * `GET /v1/settings/connectors` 的 body，SSR 测试要构造单个字段/卡片；
 * 两边手抄一份必然走散，而"固件与契约不一致"会让这些测试越绿越没用。
 *
 * 字段名全部取自 `src/ling/settings/connector_fields.py` 的 REGISTRY
 * （12 / 12 / 2 项），不是编的。
 */
import type { ReactElement } from 'react';
import {
  AccessCard,
  ConnectorCard,
  draftFromFields,
  type ConnectorFormProps,
} from './components/ConnectorsPage';
import type {
  ConnectorAccessGroup,
  ConnectorActivation,
  ConnectorField,
  ConnectorView,
  ConnectorsResponse,
} from './types';

/** REGISTRY 的键名（顺序与后端一致）。 */
export const FIELD_IDS_FOR_TESTS = {
  onebot: [
    'LING_ONEBOT_API_BASE',
    'LING_ONEBOT_ACCESS_TOKEN',
    'LING_ONEBOT_SELF_ID',
    'LING_ONEBOT_SECRET',
    'LING_ONEBOT_WS_PORT',
    'LING_ONEBOT_WS_TOKEN',
    'LING_ONEBOT_WS_URL',
    'LING_ONEBOT_WS_HOST',
    'LING_ONEBOT_INSTANCE_ID',
    'LING_ONEBOT_SUPPORTS_GROUP',
    'LING_ONEBOT_PROACTIVE',
    'LING_ONEBOT_MEDIA_HOSTS',
  ],
  qqbot: [
    'LING_QQBOT_ENABLED',
    'LING_QQBOT_APP_ID',
    'LING_QQBOT_CLIENT_SECRET',
    'LING_QQBOT_BOT_SECRET',
    'LING_QQBOT_WEBHOOK_PUBLIC_KEY',
    'LING_QQBOT_INSTANCE_ID',
    'LING_QQBOT_SANDBOX',
    'LING_QQBOT_API_BASE',
    'LING_QQBOT_TOKEN_URL',
    'LING_QQBOT_ALLOW_UNSIGNED_VALIDATION',
    'LING_QQBOT_PROACTIVE',
    'LING_QQBOT_SUPPORTS_GROUP',
  ],
  access: ['LING_ACCESS_MODE', 'LING_ACCESS_LIST'],
} as const;

/** 密钥项（`_secret(...)` 定义的那些）。 */
export const SECRET_KEYS = new Set([
  'LING_ONEBOT_ACCESS_TOKEN',
  'LING_ONEBOT_SECRET',
  'LING_ONEBOT_WS_TOKEN',
  'LING_ONEBOT_WS_URL',
  'LING_QQBOT_APP_ID',
  'LING_QQBOT_CLIENT_SECRET',
  'LING_QQBOT_BOT_SECRET',
]);

/** 开关项（`_switch(...)` 定义的）。 */
export const SWITCH_KEYS = new Set([
  'LING_ONEBOT_SUPPORTS_GROUP',
  'LING_ONEBOT_PROACTIVE',
  'LING_QQBOT_ENABLED',
  'LING_QQBOT_SANDBOX',
  'LING_QQBOT_ALLOW_UNSIGNED_VALIDATION',
  'LING_QQBOT_PROACTIVE',
  'LING_QQBOT_SUPPORTS_GROUP',
]);

/** 名单项（`_list(...)` 定义的）。 */
export const LIST_KEYS = new Set(['LING_ONEBOT_MEDIA_HOSTS', 'LING_ACCESS_LIST']);

export function makeField(
  overrides: Partial<ConnectorField> & { key: string },
): ConnectorField {
  const kind =
    overrides.kind ??
    (SECRET_KEYS.has(overrides.key)
      ? 'secret'
      : SWITCH_KEYS.has(overrides.key)
        ? 'switch'
        : LIST_KEYS.has(overrides.key)
          ? 'list'
          : 'text');
  const control =
    overrides.control ??
    (overrides.key === 'LING_ACCESS_MODE'
      ? 'select'
      : kind === 'secret'
        ? 'password'
        : kind === 'switch'
          ? 'switch'
          : kind === 'list'
            ? 'list'
            : 'text');
  return {
    key: overrides.key,
    name: overrides.key,
    scope: overrides.scope ?? 'onebot',
    kind,
    label: overrides.label ?? '说明',
    control,
    editable: overrides.editable ?? true,
    source: overrides.source ?? 'env',
    configured: overrides.configured ?? true,
    set: overrides.set ?? overrides.configured ?? true,
    // 密钥的 value 恒为 null（后端 `to_public_value` 的决定，不是前端挑选的）。
    value: overrides.value === undefined ? null : overrides.value,
    preview: overrides.preview ?? null,
  };
}

export function makeActivation(overrides: Partial<ConnectorActivation> = {}): ConnectorActivation {
  return {
    scope: overrides.scope ?? 'onebot',
    state: overrides.state ?? 'active',
    generation: overrides.generation ?? 3,
    active_generation: overrides.active_generation ?? 3,
    candidate_generation: overrides.candidate_generation ?? null,
    active_revision_id: overrides.active_revision_id ?? 'crev_0',
    candidate_revision_id: overrides.candidate_revision_id ?? null,
    last_revision_id: overrides.last_revision_id ?? 'crev_0',
    last_state: overrides.last_state ?? 'active',
    started_at: overrides.started_at ?? null,
    activated_at: overrides.activated_at ?? null,
    error_code: overrides.error_code ?? null,
    error_safe: overrides.error_safe ?? null,
    ack_count: overrides.ack_count ?? 1,
    expected_count: overrides.expected_count ?? 1,
  };
}

/** OneBot 的一组真实字段（值取常见的部署形态）。 */
export function onebotFields(): ConnectorField[] {
  return [
    makeField({
      key: 'LING_ONEBOT_API_BASE',
      value: 'http://127.0.0.1:5700',
      configured: true,
      source: 'env',
    }),
    makeField({ key: 'LING_ONEBOT_ACCESS_TOKEN', configured: true, preview: 's3cr3t••••' }),
    makeField({ key: 'LING_ONEBOT_SELF_ID', value: '100200300', configured: true, source: 'env' }),
    makeField({ key: 'LING_ONEBOT_SECRET', configured: true, preview: 'hmac-k••••' }),
    makeField({ key: 'LING_ONEBOT_WS_PORT', value: '6700', configured: true, source: 'env' }),
    makeField({ key: 'LING_ONEBOT_WS_TOKEN', configured: true, preview: 'ws-tok••••' }),
    makeField({ key: 'LING_ONEBOT_WS_URL', configured: false, set: false }),
    makeField({ key: 'LING_ONEBOT_WS_HOST', value: '127.0.0.1', configured: true, source: 'default' }),
    makeField({
      key: 'LING_ONEBOT_INSTANCE_ID',
      value: 'onebot-default',
      configured: true,
      source: 'default',
    }),
    makeField({ key: 'LING_ONEBOT_SUPPORTS_GROUP', value: true, configured: true, source: 'default' }),
    makeField({ key: 'LING_ONEBOT_PROACTIVE', value: false, configured: true, source: 'default' }),
    makeField({ key: 'LING_ONEBOT_MEDIA_HOSTS', value: [], configured: false, set: false }),
  ];
}

export function qqbotFields(): ConnectorField[] {
  return [
    makeField({ key: 'LING_QQBOT_ENABLED', scope: 'qqbot', value: false, configured: true, source: 'default' }),
    makeField({ key: 'LING_QQBOT_APP_ID', scope: 'qqbot', configured: true, preview: '1002003••••' }),
    makeField({ key: 'LING_QQBOT_CLIENT_SECRET', scope: 'qqbot', configured: true, preview: 'abcdEF••••' }),
    makeField({ key: 'LING_QQBOT_BOT_SECRET', scope: 'qqbot', configured: false, set: false }),
    makeField({ key: 'LING_QQBOT_WEBHOOK_PUBLIC_KEY', scope: 'qqbot', value: null, configured: false, set: false }),
    makeField({
      key: 'LING_QQBOT_INSTANCE_ID',
      scope: 'qqbot',
      value: 'qqbot-default',
      configured: true,
      source: 'default',
    }),
    makeField({ key: 'LING_QQBOT_SANDBOX', scope: 'qqbot', value: true, configured: true, source: 'default' }),
    makeField({ key: 'LING_QQBOT_API_BASE', scope: 'qqbot', value: null, configured: false, set: false }),
    makeField({ key: 'LING_QQBOT_TOKEN_URL', scope: 'qqbot', value: null, configured: false, set: false }),
    makeField({
      key: 'LING_QQBOT_ALLOW_UNSIGNED_VALIDATION',
      scope: 'qqbot',
      value: false,
      configured: true,
      source: 'default',
    }),
    makeField({ key: 'LING_QQBOT_PROACTIVE', scope: 'qqbot', value: false, configured: true, source: 'default' }),
    makeField({
      key: 'LING_QQBOT_SUPPORTS_GROUP',
      scope: 'qqbot',
      value: true,
      configured: true,
      source: 'default',
    }),
  ];
}

export function accessFields(): ConnectorField[] {
  return [
    makeField({
      key: 'LING_ACCESS_MODE',
      scope: 'access',
      value: 'off',
      configured: true,
      source: 'default',
      control: 'select',
      label: '出入许可模式',
    }),
    makeField({
      key: 'LING_ACCESS_LIST',
      scope: 'access',
      value: [],
      configured: false,
      set: false,
      label: '发送者白名单（QQ 号，按字符串比较）',
    }),
  ];
}

export function makeConnector(overrides: Partial<ConnectorView> = {}): ConnectorView {
  const id = overrides.id ?? 'onebot';
  return {
    id,
    label: overrides.label ?? 'OneBot（NapCat / Lagrange / go-cqhttp）',
    configured: overrides.configured ?? false,
    enabled: overrides.enabled ?? false,
    status: overrides.status ?? 'not_configured',
    status_reason: overrides.status_reason ?? 'onebot_no_inbound_path',
    status_message:
      overrides.status_message ?? '没有任何入站通道：WS 端口、WS 地址、HTTP 上报密钥三项都没配',
    guidance:
      overrides.guidance ??
      '任选一条入站路径：`LING_ONEBOT_WS_PORT`（+ 令牌）或 `LING_ONEBOT_WS_URL`',
    editable: overrides.editable ?? true,
    storage: overrides.storage ?? 'db',
    generation: overrides.generation ?? 3,
    activation: overrides.activation ?? makeActivation({ scope: id, generation: overrides.generation ?? 3 }),
    fields: overrides.fields ?? onebotFields(),
    runtime: overrides.runtime ?? { connections: null, received: null },
  };
}

export function makeAccessGroup(overrides: Partial<ConnectorAccessGroup> = {}): ConnectorAccessGroup {
  return {
    id: 'access',
    generation: overrides.generation ?? 1,
    activation: overrides.activation ?? makeActivation({ scope: 'access', generation: 1 }),
    editable: overrides.editable ?? true,
    fields: overrides.fields ?? accessFields(),
  };
}

/**
 * 一份完整的 `GET /v1/settings/connectors` body（三个 scope）。
 *
 * 真浏览器测试把它整个交给 `page.route`；SSR 测试用它给页面级组件。
 */
export function makeResponse(overrides: Partial<ConnectorsResponse> = {}): ConnectorsResponse {
  return {
    schema_version: 'v2',
    as_of: overrides.as_of ?? '2026-11-10T02:00:00+00:00',
    can_edit: overrides.can_edit ?? true,
    connectors:
      overrides.connectors ??
      [
        makeConnector({
          status: 'configured_disconnected',
          status_reason: 'onebot_ws_server_waiting',
          status_message: 'WS 服务端在监听，但还没有客户端连入',
          configured: true,
          enabled: true,
          guidance: '服务端已在监听，等 NapCat 连入',
          runtime: { connections: 0, received: 0 },
        }),
        makeConnector({
          id: 'qqbot',
          label: 'QQ 官方机器人（QQ 开放平台）',
          status: 'disabled',
          status_reason: 'qqbot_channel_disabled',
          status_message: '凭据已配好，但渠道开关是关的',
          configured: true,
          enabled: false,
          guidance: '凭据已就位，只差开关：把 `LING_QQBOT_ENABLED` 打开',
          fields: qqbotFields(),
        }),
      ],
    access: overrides.access ?? makeAccessGroup(),
  };
}

/** 一个"什么都没动"的表单状态。 */
export function idleForm(): ConnectorFormProps {
  return {
    draft: {},
    clearKeys: [],
    onEdit: () => {},
    onToggleClear: () => {},
    onResetDraft: () => {},
    onSave: () => {},
    saving: false,
    formNote: null,
    notice: null,
    fieldError: null,
    onReload: () => {},
  };
}

/** 渲染一张接入器卡（配真实的表单状态）。 */
export function renderConnectorCard(
  connector: ConnectorView,
  form: Partial<ConnectorFormProps> = {},
): ReactElement {
  const props = { ...idleForm(), draft: draftOf(connector.fields), ...form };
  return <ConnectorCard connector={connector} {...props} />;
}

export function renderAccessCard(
  group: ConnectorAccessGroup,
  form: Partial<ConnectorFormProps> = {},
): ReactElement {
  const props = { ...idleForm(), draft: draftOf(group.fields), ...form };
  return <AccessCard group={group} {...props} />;
}

function draftOf(fields: ConnectorField[]) {
  return draftFromFields(fields);
}
