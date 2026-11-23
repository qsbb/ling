/**
 * 外部插件页的**纯逻辑**：把服务端状态翻译成"现在发生了什么、下一步做什么"。
 *
 * 这些函数不碰 React、不发请求，所以可以在没有 jsdom 的环境里断言
 * （仓库 `pnpm test` 就是那套环境）。页面只负责把它们的结果画出来。
 *
 * 取舍说明（每条都对应一次真实 API 观测，见
 * `scripts/probe_external_plugins_api.py` 的 `/tmp/ep-probe.json`）：
 *
 * 1. **能力是两列，不是一列。** 服务端同时给 `capabilities`（插件声明的）与
 *    `granted_capabilities`（管理员授予的）。只显示其中一列都会骗人：
 *    只看声明，用户会以为已经能调用；只看授予，用户会看不出"插件本来还能做什么"。
 * 2. **`enabled` 与 `status` 是两个维度。** `enabled=true` 只表示"没被关掉"，
 *    真正能不能调用还要求 `status ∈ {ready, registered}`（见服务端 `allows()`）。
 *    所以"开着但不可用"必须单独成一态，不能合并成"已启用"。
 * 3. **`last_error` 是权威原因，错误文案不是。** 同步一个坏 manifest 时，接口
 *    只回"插件同步失败，请检查端点是否可达且符合 v1 契约"，真正的原因是记录里的
 *    `status='identity_mismatch'` + `last_error`。页面据此决定下一步。
 * 4. **端点永远拿不到。** 任何响应都不含 `endpoint`，所以"这个插件指向哪里"
 *    只能由登记时的人自己记住；界面上不编一个占位。
 */
import type {
  ExternalCapability,
  ExternalPlugin,
  ExternalPluginRegisterBody,
  ExternalPluginStatusResponse,
} from './types';

/** 四个能力的中文名与"它到底让插件做什么"。顺序即页面上的展示顺序。 */
export const CAPABILITY_LABELS: Record<ExternalCapability, { label: string; detail: string }> = {
  tools: {
    label: '工具',
    detail: '模型可以调用它声明的工具；结果按不可信数据包裹后回灌。',
  },
  commands: {
    label: '命令',
    detail: '聊天里用 /plugincmd 前缀手动触发它声明的命令。',
  },
  events: {
    label: '事件',
    detail: '轮次完成后由 worker 投递 turn.completed；失败按 2/4/8/16 秒退避。',
  },
  'host.state': {
    label: '宿主状态',
    detail: '插件能在调用中读写自己的键值（按插件隔离，默认 7 天过期）。',
  },
};

/** 能力的中文名；未知字符串原样返回（后端加了新能力时不至于显示空白）。 */
export function capabilityName(cap: ExternalCapability): string {
  return CAPABILITY_LABELS[cap]?.label ?? cap;
}

/** 能力的一句话说明；未知能力没有说明，返回空串。 */
export function capabilityDetail(cap: ExternalCapability): string {
  return CAPABILITY_LABELS[cap]?.detail ?? '';
}

/** 能力顺序：与 manifest 的常规声明顺序一致，便于对照文档。 */
export const CAPABILITY_ORDER: readonly ExternalCapability[] = [
  'tools',
  'commands',
  'events',
  'host.state',
];

/**
 * 一条登记的**实际可调用性**。
 *
 * 与服务端 `allows()` 同构，但拆成三态而不是布尔：`off` 与 `blocked` 的下一步
 * 完全不同（一个是"去启用"，一个是"去修端点/版本/标识"）。
 */
export type PluginState = 'off' | 'active' | 'blocked';

/**
 * 服务端会**自己**把记录置成这几种状态，并顺手把 `enabled` 设成 false
 * （`set_enabled` / `_verify_identity` 都这么写）。它们与"人手动关掉"不是一回事：
 * 手动关掉要人点一下就能开；这几种是"开了也不会成功"，必须先去修原因。
 *
 * 所以判定顺序是**先看状态再看开关**。反过来写（先看 `enabled`）会把
 * `identity_mismatch` 画成普通的"已停用"——用户点「启用」，再撞一次 422。
 */
const FAILED_STATUSES: ReadonlySet<string> = new Set([
  'unavailable',
  'incompatible',
  'identity_mismatch',
]);

export function pluginState(plugin: Pick<ExternalPlugin, 'enabled' | 'status'>): PluginState {
  // 未知状态一律按"开着也不可用"处理：它不可能被 `allows()` 接受，
  // 说成 off 等于告诉人去点「启用」——那一定失败。
  if (FAILED_STATUSES.has(plugin.status)) return 'blocked';
  const usable = plugin.status === 'ready' || plugin.status === 'registered';
  if (!usable) return 'blocked';
  return plugin.enabled ? 'active' : 'off';
}

/** 状态徽标：文案 + 色调。未识别的服务端状态**原样显示**。 */
export interface StatusBadge {
  tone: 'ok' | 'off' | 'error' | 'warn';
  label: string;
}

export function statusBadge(plugin: Pick<ExternalPlugin, 'enabled' | 'status'>): StatusBadge {
  const state = pluginState(plugin);
  if (state === 'active') return { tone: 'ok', label: '已启用' };
  if (state === 'off') return { tone: 'off', label: '已停用' };
  switch (plugin.status) {
    case 'incompatible':
      return { tone: 'warn', label: '版本不兼容' };
    case 'identity_mismatch':
      return { tone: 'error', label: '标识已变，已停用' };
    case 'unavailable':
      return { tone: 'error', label: '端点不可达' };
    default:
      // 未知状态不改写：把 `weird_state` 显示成「不可用」等于替服务端编了一个理由。
      return { tone: 'warn', label: plugin.status };
  }
}

/**
 * 这条登记**为什么不能工作**，以及下一步该干什么。
 *
 * 返回 null = 能工作（不管是否启用）。`action` 把建议落到页面上真实存在的按钮，
 * 不认识的原因给 `null` 动作，只描述现象——编一个按不动的按钮更糟。
 */
export interface Remedy {
  reason: string;
  next: string;
  action: PluginAction | null;
}

export type PluginAction = 'enable' | 'sync' | 'recheck' | 'unregister' | 'regrant';

export function remedy(
  plugin: Pick<ExternalPlugin, 'enabled' | 'status' | 'last_error' | 'capabilities' | 'granted_capabilities'>,
): Remedy | null {
  const state = pluginState(plugin);
  if (state === 'active') return null;
  /*
   * 就绪但关着**没有建议块**：行内那颗「启用」按钮就是全部信息。
   * 再画一块「已登记但没启用 / 请点重试启用」，等于同一件事说三遍
   * （徽标一次、按钮一次、建议块一次），还把真正需要解释的失败行挤下去。
   */
  if (state === 'off') return null;
  switch (plugin.status) {
    case 'identity_mismatch':
      return {
        reason: plugin.last_error || '端点现在声明的 id 与登记时不同，服务端已自动停用。',
        next: '确认新 id 是你想要的插件，再取消登记并按新 id 重新登记；否则检查端点是不是指向了别的服务。',
        action: 'unregister',
      };
    case 'incompatible':
      return {
        reason: plugin.last_error || '插件的版本区间不含当前 Ling 版本。',
        next: '让插件发布者放宽 min/max_host_version，或换用匹配当前版本的一版，然后同步。',
        action: 'sync',
      };
    case 'unavailable':
      return {
        reason: plugin.last_error || '最近一次探测没有连上插件端点。',
        next: '确认插件进程在跑、登记的地址对 API/worker 都可达（容器里 127.0.0.1 不是宿主），再启用。',
        action: 'enable',
      };
    default:
      return {
        reason: plugin.last_error || `服务端报告状态 ${plugin.status}。`,
        next: '同步一次以重新读取 manifest。',
        action: 'sync',
      };
  }
}

/** 声明了但没授予的能力（页面上要能看出"少给了什么"）。 */
export function ungrantedCapabilities(
  plugin: Pick<ExternalPlugin, 'capabilities' | 'granted_capabilities'>,
): ExternalCapability[] {
  const granted = new Set(plugin.granted_capabilities);
  return CAPABILITY_ORDER.filter((cap) => plugin.capabilities.includes(cap) && !granted.has(cap));
}

/**
 * 声明与授予**对不上**时的一句话（没有差异返回 null）。
 *
 * 两种情况都要说：少授予（插件的能力没放开），和多授予（授予了插件没声明的，
 * 服务端调用前会过滤，但记录里留着会让人误以为已生效）。
 */
export function grantMismatch(
  plugin: Pick<ExternalPlugin, 'capabilities' | 'granted_capabilities'>,
): string | null {
  const declared = new Set(plugin.capabilities);
  const missing = ungrantedCapabilities(plugin);
  const extra = plugin.granted_capabilities.filter((cap) => !declared.has(cap));
  if (missing.length === 0 && extra.length === 0) return null;
  const parts: string[] = [];
  if (missing.length > 0) {
    parts.push(`未授予 ${missing.map(capabilityName).join('、')}`);
  }
  if (extra.length > 0) {
    parts.push(`${extra.map(capabilityName).join('、')} 已不在声明里`);
  }
  return parts.join('；');
}

/** 登记后**再也不会出现**的信息：端点地址。页面据此提醒用户自行记下。 */
export const ENDPOINT_NOT_RETURNED =
  '端点地址不会回显（服务端不回流），登记后请自行记住它指向哪里。';

/**
 * 校验一个用户输入的端点。返回错误文案或 null。
 *
 * 规则与服务端 `validate_endpoint()` 一致，但这里是**提前反馈**，不是替代：
 * 服务端仍然会再校验一次，页面不能因为本地通过了就假设一定成功。
 * 每一项都对应一个真实的 422：query/fragment/userinfo 会让 SSRF 面变大，
 * 服务端明确拒绝。
 */
export function validateEndpointInput(raw: string): string | null {
  const value = raw.trim();
  if (!value) return '填一个 http(s) 地址，例如 http://127.0.0.1:8096';
  if (/\s/.test(value)) return '地址里不能有空格';
  if (value.includes('\\')) return '地址里不能有反斜杠';
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return '这不是一个完整地址，需要带上 http:// 或 https://';
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return '只支持 http 与 https';
  }
  if (!url.hostname) return '地址里缺少主机名';
  if (url.username || url.password) return '地址里不能带用户名或密码';
  if (url.search) return '地址里不能带查询参数（? 之后的部分）';
  if (url.hash) return '地址里不能带锚点（# 之后的部分）';
  if (url.port === '0') return '端口 0 不合法';
  return null;
}

/** 端点输入框里的提示：告诉人它会被 GET 什么，而不是让人猜。 */
export const ENDPOINT_PROBE_HINT = '登记时会 GET {地址}/v1/manifest，4 秒超时、不跟随重定向。';

/** 登记请求体：只放契约里的键（服务端 `extra=forbid`，多一个就 422）。 */
export function buildRegisterBody(input: {
  endpoint: string;
  expectedId: string;
  granted: ExternalCapability[];
  enabled: boolean;
}): ExternalPluginRegisterBody {
  const body: ExternalPluginRegisterBody = {
    endpoint: input.endpoint.trim().replace(/\/+$/, ''),
    granted_capabilities: input.granted,
    enabled: input.enabled,
  };
  const expected = input.expectedId.trim();
  // 空串不发：`expected_id` 留空表示"以端点声明的为准"，发一个空串会被模式校验拒掉。
  if (expected) body.expected_id = expected;
  return body;
}

/**
 * 一次操作失败后，"服务端**已经**做了什么"。
 *
 * 身份不匹配为例：`enable`/`sync` 返回 422，但服务端**已经提交**了停用
 * （routes 里显式 `await session.commit()`）。页面如果只显示错误、
 * 不重读记录，会让人以为"还开着"。
 */
export function failureNote(error: { message: string; code: string }): string {
  if (error.code === 'forbidden') {
    return '只有所有者和管理员能动外部插件；这个账号没有权限。';
  }
  if (error.message.includes('ID 与端点声明不一致')) {
    return '服务端已停用这条登记（它会保持停用，直到你重新登记）。列表已重读。';
  }
  return '列表已重读——服务端可能已经改了状态（例如自动停用）。';
}

/** 健康探测结果压成一行；`reachable=false` 时要指出"记录还在，只是连不上"。 */
export function healthLine(status: ExternalPluginStatusResponse): string {
  if (status.reachable) return '端点可达，manifest 与登记的 id 一致。';
  return '端点不可达，或它现在声明的 id 与登记不一致。';
}

/** 已授予且插件声明过的能力——只有这些真的会被调用。 */
export function effectiveCapabilities(
  plugin: Pick<ExternalPlugin, 'capabilities' | 'granted_capabilities'>,
): ExternalCapability[] {
  const granted = new Set(plugin.granted_capabilities);
  return CAPABILITY_ORDER.filter(
    (cap) => plugin.capabilities.includes(cap) && granted.has(cap),
  );
}
