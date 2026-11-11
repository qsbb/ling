/**
 * ling API v1 客户端。
 *
 * 只调用 docs/API-WEB-V1.md 冻结的端点；不缓存权威历史。
 * 401 统一交给应用层处理（显示登录表单并记住当前会话）。
 */
import type {
  AuthResponse,
  MediaUploadResponse,
  DraftResponse,
  MessagesResponse,
  Persona,
  PersonaCreate,
  PersonaSummary,
  PersonaUpdate,
  ProbeResult,
  PromptSnapshotOut,
  DialoguePolicy,
  LifePolicy,
  MemoryPolicy,
  ProfileDraftInput,
  ProviderDraftInput,
  QuickConfigPreview,
  ResolveResponse,
  SlotDraftInput,
  SecretStatus,
  SessionRecord,
  SettingsConfig,
  ModelHealthResponse,
  ConsentsResponse,
  ConsentUpdate,
  ConnectorChangeRequest,
  ConnectorChangeResult,
  ConnectorResetResponse,
  ConnectorsResponse,
  ConnectorInstanceResponse,
  ConnectorInstancesResponse,
  StopResponse,
  TaxonomyResponse,
  UserPreferencesResponse,
  VideoMode,
  ActivityResponse,
  GoalsResponse,
  ProactiveResponse,
  TimelineResponse,
  LifeTimelineOut,
  OutlineJobOut,
  SubjectsResponse,
  SubjectsQuery,
  MergeResult,
  UnmergeResult,
  UserInfo,
  DashboardResponse,
  VersionInfo,
  ReleaseCheck,
  ImageUpdateV2,
  ImageUpdateCheckAck,
  ImageUpdateSubmission,
  ImageUpdateSubmissionAck,
  UpdateJob,
  UpdateStatus,
  UpdateRequest,
  CoreMemoryInput,
  CoreMemoryListResponse,
  MemoryInclude,
  MemoryListResponse,
  LogQuery,
  LogsResponse,
  RecallProposal,
  RecallResult,
  VoiceReferenceUpload,
} from './types';

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retriable: boolean;
  readonly detail: string | null;
  readonly traceId: string | null;

  constructor(
    status: number,
    code: string,
    message: string,
    retriable = false,
    detail: string | null = null,
    traceId: string | null = null,
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.retriable = retriable;
    this.detail = detail;
    this.traceId = traceId;
  }
}

/** `POST /v1/memory/forget` 的两段式返回（请求阶段）。 */
export interface ForgetAcceptedResponse {
  tombstone_id: string;
  generation: number;
  events_cancelled: number;
  state: string;
}

/**
 * 旧后端（两段式之前）的返回：只做了软清除，`retracted` 是四类受影响行数之和。
 * 保留这个分支不是为了兼容，而是为了让页面能**如实指出**"这个后端还没真删"。
 */
export interface ForgetLegacyResponse {
  retracted: number;
}

export type ForgetResponse = ForgetAcceptedResponse | ForgetLegacyResponse;

/** `GET /v1/memory/purge/status` 的返回：最近的清理记录。 */
export interface PurgeStatusItem {
  tombstone_id: string;
  state: string;
  requested_at: string | null;
  completed_at: string | null;
  memories_deleted: number;
  episodes_deleted: number;
  core_deleted: number;
  awareness_deleted: number;
  embeddings_deleted: number;
  entities_deleted: number;
  /** 后端墓碑表里有这一列；老后端不返回时按 0 处理。 */
  retrieval_logs_deleted?: number | null;
  events_cancelled: number;
  events_scrubbed?: number | null;
  error_code: string | null;
}

export interface PurgeStatusResponse {
  items: PurgeStatusItem[];
}

let unauthorizedHandler: (() => void) | null = null;

/** 应用层注册：任何请求 401 时回到登录表单（保留当前路由）。 */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export async function parseErrorResponse(response: Response): Promise<ApiError> {
  let text = '';
  try {
    text = await response.text();
  } catch {
    text = '';
  }
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }
  const error = (payload as { error?: Record<string, unknown> } | null)?.error;
  if (error && typeof error === 'object') {
    const code = typeof error.code === 'string' ? error.code : 'internal';
    const message = typeof error.message === 'string' ? error.message : response.statusText;
    return new ApiError(
      response.status,
      code,
      message || `请求失败（HTTP ${response.status}）`,
      error.retriable === true,
      typeof error.detail === 'string' ? error.detail : null,
      typeof error.trace_id === 'string' ? error.trace_id : null,
    );
  }
  return new ApiError(
    response.status,
    response.status === 401 ? 'unauthorized' : 'internal',
    text.slice(0, 300) || response.statusText || `请求失败（HTTP ${response.status}）`,
    response.status >= 500,
  );
}

async function requestResponse(path: string, init: RequestInit = {}): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(path, { credentials: 'same-origin', ...init });
  } catch (cause) {
    // 主动取消（unmount/轮询换挡）不是网络故障，交给调用方识别并忽略。
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new ApiError(
      0,
      'network_error',
      '无法连接服务端，请确认后端已启动或网络可用',
      true,
      cause instanceof Error ? cause.message : null,
    );
  }
  if (response.status === 401) {
    const error = await parseErrorResponse(response);
    unauthorizedHandler?.();
    throw error;
  }
  if (!response.ok) {
    throw await parseErrorResponse(response);
  }
  return response;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await requestResponse(path, init);
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

/**
 * 响应里的服务端时刻（HTTP `Date` 头，毫秒时间戳）。
 *
 * 为什么需要它：撤回窗口是 120 秒，而**浏览器时钟不可信**（时区设错、机器
 * 休眠后漂移、手动改过时间）。拿 `Date.now()` 减服务端给的 `created_at`，
 * 时钟一歪就会把"还来得及"判成"来不及"，或者反过来让用户点一个注定失败的
 * 按钮。`Date` 头是服务端自己盖的时间，与 `created_at` 同源，差值才是真的。
 *
 * 拿不到（代理剥掉、CORS 未暴露）时返回 null，调用方退回保守策略。
 */
export function serverNowMsFromResponse(response: Response): number | null {
  const header = response.headers.get('date');
  if (!header) return null;
  const parsed = Date.parse(header);
  return Number.isNaN(parsed) ? null : parsed;
}

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
}

/**
 * 读响应体里**未被 schema 声明**的 `error.field`（哪个键错了）。
 *
 * `parseErrorResponse` 只抬 `code/message/detail/trace_id`，会丢掉 `field`；
 * 而"逐字段指出哪个错了"必须靠它。这里读的是**另一份** body 文本，所以由
 * `connectorWriteError` 统一读一次再同时交给两边，不重复消费流。
 */
function fieldFromPayload(payload: unknown): string | null {
  const field = (payload as { error?: { field?: unknown } } | null)?.error?.field;
  return typeof field === 'string' && field !== '' ? field : null;
}

/**
 * 保存接入器失败时的错误：与原 `ApiError` **同一个对象**，只补一个 `field`。
 *
 * 为什么不是包一层新类：`explainError` 按 `instanceof ApiError` 分支、
 * 测试按 message/code 判等，包一层会让"同一个 422"在两处的身份不同。
 * 直接挂在原实例上，`connectorErrorField()` 读得到，其余行为一字不变。
 */
async function connectorWriteError(response: Response): Promise<ApiError> {
  const raw = await response.clone().text();
  let payload: unknown = null;
  if (raw) {
    try {
      payload = JSON.parse(raw);
    } catch {
      payload = null;
    }
  }
  const error = await parseErrorResponse(response);
  const field = fieldFromPayload(payload);
  if (field !== null) {
    Object.defineProperty(error, 'field', { value: field, enumerable: false });
  }
  return error;
}

/** 保存结果里被抬出来的字段名（只有 `api.saveConnectorConfig` 会带）。 */
export function connectorErrorField(error: unknown): string | null {
  if (!error || typeof error !== 'object') return null;
  const field = (error as { field?: unknown }).field;
  return typeof field === 'string' && field !== '' ? field : null;
}

/**
 * 实例子资源的路径。`instance_id` 直接进路径段，所以**必须编码**：
 * 它由后端派生（`i-<hex>` 或 slug），但客户端不该假设其中没有 `/` 或空格。
 */
function connectorInstancePath(scope: string, instanceId: string): string {
  return `/v1/settings/connectors/${encodeURIComponent(scope)}/instances/${encodeURIComponent(
    instanceId,
  )}`;
}

/** 发送/重试返回原始 SSE 响应，由 sse.ts 解析。 */
async function postStream(path: string, body: unknown | null, signal: AbortSignal): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...(body === null ? { method: 'POST' } : jsonInit('POST', body)),
      credentials: 'same-origin',
      headers: { Accept: 'text/event-stream', ...(body === null ? {} : { 'Content-Type': 'application/json' }) },
      signal,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') {
      throw cause;
    }
    throw new ApiError(
      0,
      'network_error',
      '无法建立流式连接，请确认服务端可用',
      true,
      cause instanceof Error ? cause.message : null,
    );
  }
  if (response.status === 401) {
    const error = await parseErrorResponse(response);
    unauthorizedHandler?.();
    throw error;
  }
  if (!response.ok) {
    throw await parseErrorResponse(response);
  }
  return response;
}

/**
 * 人格的 id（新契约 `id`，旧实现 `persona_id`）。
 *
 * 只在这里读这两个键：两个都有时以 `id` 为准，只有一个时用那个，都没有时
 * 给空串——空串的 id 任何写操作都会 404，比拿 `undefined` 拼出
 * `/v1/personas/undefined` 更容易在日志里看出来。
 */
export function personaId(persona: { id?: string; persona_id?: string }): string {
  const id = persona.id ?? persona.persona_id ?? '';
  return typeof id === 'string' ? id : '';
}

/**
 * 收下一个"人格列表"响应体。
 *
 * 后端实际回的是 `PersonaListOut`：`{items, active_persona_id, total}`
 * （`routes/persona.py`）。契约文档原本写"直接回 `Persona[]`"，所以裸数组
 * 与 `{personas: [...]}` 也一并接受。**其余形状一律抛错**，不默默返回空数组：
 * 返回空数组会让页面显示「暂无人设」，而那是一个会误导用户去重建人设的结论。
 */
export function personaListFrom(payload: unknown): {
  items: PersonaSummary[];
  activePersonaId: string | null;
  /** 后端 `total`；没给时取 `items.length`（裸数组形状只能这样）。 */
  total: number;
} {
  let value: unknown = payload;
  let activePersonaId: string | null = null;
  let reportedTotal: number | null = null;
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const obj = value as Record<string, unknown>;
    const active = obj.active_persona_id ?? obj.active_id;
    if (typeof active === 'string' && active !== '') activePersonaId = active;
    if (typeof obj.total === 'number' && Number.isFinite(obj.total)) reportedTotal = obj.total;
    value = obj.items ?? obj.personas ?? obj.data;
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const nested = value as Record<string, unknown>;
      value = nested.items ?? nested.personas;
    }
  }
  if (!Array.isArray(value)) {
    throw new ApiError(
      200,
      'invalid_request',
      '服务端返回的人格列表格式无法识别（期望 {items: [...]} 或数组）',
    );
  }
  const items = value.filter(
    (item): item is PersonaSummary => !!item && typeof item === 'object',
  );
  return { items, activePersonaId, total: reportedTotal ?? items.length };
}

export const api = {
  /* ------------------------------------------------------------ 认证 */
  me(): Promise<AuthResponse> {
    return request<AuthResponse>('/v1/auth/me');
  },
  dashboard(): Promise<DashboardResponse> {
    return request<DashboardResponse>('/v1/dashboard');
  },
  changeAccount(current: string, username: string): Promise<{ username: string }> {
    return request<{ username: string }>(
      '/v1/auth/account',
      jsonInit('PATCH', { current, username }),
    );
  },

  changePassword(current: string, next: string): Promise<void> {
    return request<void>('/v1/auth/password', jsonInit('POST', { current, new: next }));
  },
  login(username: string, password: string): Promise<AuthResponse> {
    return request<AuthResponse>('/v1/auth/login', jsonInit('POST', { username, password }));
  },
  logout(): Promise<{ ok: boolean }> {
    return request<{ ok: boolean }>('/v1/auth/logout', { method: 'POST' });
  },

  /* ------------------------------------------------------------ 会话 */
  async listSessions(): Promise<SessionRecord[]> {
    const data = await request<{ sessions: SessionRecord[] }>('/v1/sessions');
    return data.sessions;
  },
  createSession(title?: string): Promise<SessionRecord> {
    return request<SessionRecord>('/v1/sessions', jsonInit('POST', title ? { title } : {}));
  },
  /**
   * 置顶 / 取消置顶。后端**幂等**：重复置顶只刷新 `pinned_at`（于是"刚置顶
   * 的那个"排到置顶组第一位），重复取消写回 NULL 也不报错。
   *
   * 返回整条 `SessionRecord`——前端直接替换列表里那一行，不必再拉一次列表。
   */
  pinSession(sessionId: string, pinned: boolean): Promise<SessionRecord> {
    return request<SessionRecord>(`/v1/sessions/${encodeURIComponent(sessionId)}/pin`, {
      ...jsonInit('POST', { pinned }),
    });
  },
  renameSession(sessionId: string, title: string): Promise<SessionRecord> {
    return request<SessionRecord>(`/v1/sessions/${encodeURIComponent(sessionId)}`, {
      ...jsonInit('PATCH', { title }),
    });
  },
  messages(sessionId: string, limit = 200): Promise<MessagesResponse> {
    const query = new URLSearchParams({ limit: String(limit) });
    return request<MessagesResponse>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/messages?${query.toString()}`,
    );
  },
  /**
   * 她这次回复时**完整看到的东西**（系统提示词、记忆、历史、用户输入…）。
   *
   * 只有消息所有者能读；不是自己的消息、消息不存在、快照已过保留期都由
   * 服务端决定，前端不按本地字段猜。404 在界面上的落点是「这次没有记录」，
   * 不是空白内容——见 `PromptDrawer.tsx` 的三态映射。
   */
  messagePrompt(messageId: string): Promise<PromptSnapshotOut> {
    return request<PromptSnapshotOut>(
      `/v1/messages/${encodeURIComponent(messageId)}/prompt`,
    );
  },

  /* ------------------------------------------------------- 她提议的撤回 */
  /**
   * 她提议撤回、还没被处理的那些（后端只回 `state='proposed'`）。
   *
   * 用途是"刷新页面后把没处理完的提议找回来"：流式事件只在一轮结束时给一次，
   * 关掉标签页就再也看不到了，而提议本身还挂在库里等着人确认。
   *
   * 失败**不该影响聊天**：调用方自己 catch 成"这块暂时读不到"。
   */
  async recallProposals(sessionId: string): Promise<{
    items: RecallProposal[];
    /** 服务端"现在"（HTTP Date 头）；null = 拿不到，调用方自己决定保守程度。 */
    serverNowMs: number | null;
  }> {
    const response = await requestResponse(
      `/v1/sessions/${encodeURIComponent(sessionId)}/recall-proposals`,
    );
    const items = (await response.json()) as RecallProposal[];
    return { items, serverNowMs: serverNowMsFromResponse(response) };
  },
  /**
   * 用户点「撤回」→ 这时才真的执行（后端用 `confirmed=True` 调同一个工具）。
   *
   * 返回的是**执行结果**而不是"提议状态"：`state` 可能是 `expired`
   * （超过平台窗口）、`unsupported`（这个渠道不支持）、`uncertain`
   * （没拿到回执）——三者的界面文案不同，不能一律说"已撤回"。
   */
  confirmRecall(sessionId: string, proposalId: string): Promise<RecallResult> {
    return request<RecallResult>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/recall-proposals/${encodeURIComponent(proposalId)}/confirm`,
      { method: 'POST' },
    );
  },
  /** 用户点「算了」：只把提议标成 declined，不发任何平台动作（204）。 */
  declineRecall(sessionId: string, proposalId: string): Promise<void> {
    return request<void>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/recall-proposals/${encodeURIComponent(proposalId)}/decline`,
      { method: 'POST' },
    );
  },

  /* ------------------------------------------------------------ 轮次 */
  sendTurn(sessionId: string, clientTurnId: string, text: string, signal: AbortSignal) {
    return postStream(
      `/v1/sessions/${encodeURIComponent(sessionId)}/turns`,
      { client_turn_id: clientTurnId, text },
      signal,
    );
  },
  retryTurn(turnId: string, signal: AbortSignal) {
    return postStream(`/v1/turns/${encodeURIComponent(turnId)}/retry`, null, signal);
  },
  stopTurn(turnId: string): Promise<StopResponse> {
    return request<StopResponse>(`/v1/turns/${encodeURIComponent(turnId)}/stop`, {
      method: 'POST',
    });
  },

  /* ------------------------------------------------------------ 设置 */
  settingsConfig(): Promise<SettingsConfig> {
    return request<SettingsConfig>('/v1/settings/config');
  },
  /**
   * 上传一段**参考音频**，返回可以填进 `dialogue.voice.reference_asset_id` 的 id。
   *
   * `POST /v1/settings/speech/voice-reference`（`routes/settings.py:1947`）。
   *
   * 三件事**不要**改：
   *
   * 1. 表单字段名是 **`upload`**（后端签名 `upload: UploadFile = File(...)`）。
   *    换个名字后端收不到，报的是 422「字段缺失」，看起来像"文件不合法"。
   * 2. **不设 `Content-Type`**：`multipart/form-data` 的 boundary 必须由浏览器
   *    生成。手写这个头会让 boundary 对不上，服务端解析出 0 字节 → 422
   *    `voice_reference_empty`。
   * 3. 文件名只作提示：后端**一次都不用** `upload.filename`（它只可能是
   *    `../../etc/passwd` 这类东西），容器按字节头嗅探。
   *
   * 失败形状（`error.detail` 是稳定原因码，界面按它给人话）：413
   * `voice_reference_too_large` / 422 `voice_reference_empty` /
   * 422 `voice_reference_invalid_audio` / 403 `not_owner` / 403 `bad_origin`。
   */
  uploadVoiceReference(file: File | Blob, filename = 'voice-reference'): Promise<VoiceReferenceUpload> {
    const form = new FormData();
    // 第三个参数是文件名：只影响后端日志之外的表象，判定全看字节。
    form.append('upload', file, file instanceof File ? file.name : filename);
    return request<VoiceReferenceUpload>('/v1/settings/speech/voice-reference', {
      method: 'POST',
      body: form,
    });
  },
  /**
   * 接入器（渠道）的配置 + 诊断（`GET /v1/settings/connectors`，v2）。
   *
   * 值落在 DB 覆盖层（容器根目录只读，写不了 `.env`）。这条读端点里**没有**
   * 密钥值：`kind==='secret'` 的字段连值都不进视图对象，只有 `configured`
   * 与（owner 才有的）`preview`。
   *
   * `instanceId` 走 **query 参数**：不给就是默认实例（升级前的行为）。给了
   * 就必须存在且未软删，否则 404 / 409——**不静默回退默认实例**。
   *
   * 注意这条端点的响应契约里目前**没有**回显实际读取的实例，所以调用方拿到
   * 一份 body 后**无法自证**它属于 `instanceId`。需要这个保证的地方必须自己
   * 标出来（见 `ConnectorInstances.tsx` 的 `viewMatches`），不要把"我传了参数"
   * 当成"我拿到了那一份"。
   */
  connectors(instanceId?: string | null): Promise<ConnectorsResponse> {
    const query =
      instanceId === undefined || instanceId === null || instanceId === ''
        ? ''
        : `?instance_id=${encodeURIComponent(instanceId)}`;
    return request<ConnectorsResponse>(`/v1/settings/connectors${query}`);
  },
  /**
   * 某个接入器下的实例（`GET /v1/settings/connectors/{scope}/instances`）。
   *
   * 默认**不含已软删的**（`include_deleted` 只给"查历史配置"那条路径用）。
   * **全新部署返回的是空列表**，而且默认实例也还不存在——后端只在
   * 保存配置（不带 `instance_id`）时才 `ensure_default` 补建它。
   * 所以空列表必须在页面上有一个"新建第一个实例"的入口，不能当作错误。
   */
  connectorInstances(scope: string): Promise<ConnectorInstancesResponse> {
    return request<ConnectorInstancesResponse>(
      `/v1/settings/connectors/${encodeURIComponent(scope)}/instances`,
    );
  },
  /**
   * 新建实例（`POST .../instances`，201）。**真的建行**：返回的 `instance_id`
   * 随后就能拿去 `saveConnectorConfig`。
   *
   * 不传 `instance_id` 时由后端按显示名派生（中文名派生不出时用 `i-<hex>`），
   * **不在这里猜**：派生规则只有一份，猜错就会建出两个"同名"实例。
   * 同 scope 下显示名重复 → 409 `duplicate_display_name`。
   */
  createConnectorInstance(
    scope: string,
    body: { display_name: string; enabled?: boolean },
  ): Promise<ConnectorInstanceResponse> {
    return request<ConnectorInstanceResponse>(
      `/v1/settings/connectors/${encodeURIComponent(scope)}/instances`,
      jsonInit('POST', body),
    );
  },
  /** 改显示名（身份不变，`instance_id` 一个字都不动）。 */
  renameConnectorInstance(
    scope: string,
    instanceId: string,
    displayName: string,
  ): Promise<ConnectorInstanceResponse> {
    return request<ConnectorInstanceResponse>(
      `${connectorInstancePath(scope, instanceId)}`,
      jsonInit('PUT', { display_name: displayName }),
    );
  },
  /** 启用/停用。停用**不动**已保存的配置与修订链。 */
  setConnectorInstanceEnabled(
    scope: string,
    instanceId: string,
    enabled: boolean,
  ): Promise<ConnectorInstanceResponse> {
    return request<ConnectorInstanceResponse>(
      `${connectorInstancePath(scope, instanceId)}/enabled`,
      jsonInit('PUT', { enabled }),
    );
  },
  /**
   * **软删**（置 `deleted_at`）：修订链与审计留着可查，名字从选择器里消失。
   *
   * 默认实例删不得（后端 409 `default_instance_protected`）——前端用列表里的
   * `deletable` 先把按钮置灰，别让用户点了才看到错误。
   */
  deleteConnectorInstance(
    scope: string,
    instanceId: string,
  ): Promise<ConnectorInstanceResponse> {
    return request<ConnectorInstanceResponse>(connectorInstancePath(scope, instanceId), {
      method: 'DELETE',
    });
  },
  /**
   * 保存某个接入器**某个实例**的配置并**立刻尝试生效**（`settings.py:830-1080`）。
   *
   * `ifMatch` 是卡片上的 `generation`：并发保存只有一个赢家，输了拿 409。
   * **202 不等于"已生效"**——终态看返回的 `state`，`rolled_back` 是失败。
   *
   * `instanceId` 是 **query 参数**（后端签名里 `instance_id: str | None = None`），
   * 不是 body 里的键：body 是 `ConnectorChangeBody`，多塞一个 `instance_id`
   * 只有被 pydantic 丢掉和被当成字段两种结局，两种都不是"指定了实例"。
   *
   * 不给 `instanceId` 时**必须**保持升级前那条路径（落到默认实例、默认实例
   * 不存在时后端补建）。给了就必须真的存在且未软删：后端**不会**静默退回默认
   * 实例——否则"保存到实例 B"会变成"覆盖了默认实例"，而写入方拿到的是一份
   * 正常的 202。404 `instance_not_found` / 409 `instance_deleted` 如实上抛。
   *
   * 422/409 的响应体里带 `error.field`（哪个键错了），用
   * `connectorErrorField()` 取；非写路径的错误永远返回 null。
   */
  async saveConnectorConfig(
    scope: string,
    change: ConnectorChangeRequest,
    ifMatch: number,
    instanceId?: string | null,
  ): Promise<ConnectorChangeResult> {
    const path = `/v1/settings/connectors/${encodeURIComponent(scope)}`;
    const query =
      instanceId === undefined || instanceId === null || instanceId === ''
        ? ''
        : `?instance_id=${encodeURIComponent(instanceId)}`;
    let response: Response;
    try {
      response = await fetch(`${path}${query}`, {
        method: 'PUT',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          // 后端接受不带引号的整数，也会 strip 掉引号（settings.py:878-889）。
          'If-Match': String(ifMatch),
        },
        body: JSON.stringify(change),
      });
    } catch (cause) {
      throw new ApiError(
        0,
        'network_error',
        '无法连接服务端，请确认后端已启动或网络可用',
        true,
        cause instanceof Error ? cause.message : null,
      );
    }
    if (response.status === 401) {
      unauthorizedHandler?.();
      throw await parseErrorResponse(response);
    }
    if (!response.ok) throw await connectorWriteError(response);
    return (await response.json()) as ConnectorChangeResult;
  },
  /**
   * 把三个 scope 的覆盖**全部清空**（回退到 env / 默认）。
   *
   * 一个操作跟踪三个 scope，全部成功才算成功；任一 scope 重建失败会保留原
   * active——所以 `ok:false` 必须如实展示，不能说成"已恢复默认"。
   */
  resetConnectorConfig(): Promise<ConnectorResetResponse> {
    return request<ConnectorResetResponse>('/v1/settings/connectors/reset', {
      method: 'POST',
    });
  },
  saveDraft(payload: {
    providers?: ProviderDraftInput[];
    profiles?: ProfileDraftInput[];
    slots?: SlotDraftInput[];
    test_mode?: boolean;
    log_access_denials?: boolean;
    memory?: Partial<MemoryPolicy>;
    life?: Partial<LifePolicy>;
    dialogue?: Partial<DialoguePolicy>;
    note?: string;
  }): Promise<DraftResponse> {
    return request<DraftResponse>('/v1/settings/draft', jsonInit('PUT', payload));
  },
  /**
   * 按**草稿**算每个职责的生效模型。**只算不写**。
   *
   * 界面上的「实际生效」列靠它：改了下拉还没保存，那一列要立刻跟着变。
   * 但回退规则**不在浏览器里实现**——服务端用与保存完全相同的合并 + 解析
   * 跑一遍，前端只显示结论。各算一次迟早不一致，而那种不一致查不出来。
   */
  settingsResolve(payload: {
    providers?: ProviderDraftInput[];
    profiles?: ProfileDraftInput[];
    slots?: SlotDraftInput[];
    test_mode?: boolean;
  }): Promise<ResolveResponse> {
    return request<ResolveResponse>('/v1/settings/resolve', jsonInit('POST', payload));
  },
  /**
   * 「快速配置」的二次确认：把每个职责指回它自己的槽位会发生什么。
   *
   * 返回的 `profiles` 就是点确认后要写入的那份草稿，**不要在浏览器里再拼一次**。
   */
  quickConfigPreview(payload: {
    providers?: ProviderDraftInput[];
    profiles?: ProfileDraftInput[];
    slots?: SlotDraftInput[];
    test_mode?: boolean;
  }): Promise<QuickConfigPreview> {
    return request<QuickConfigPreview>(
      '/v1/settings/quick-config/preview',
      jsonInit('POST', payload),
    );
  },
  publish(revision: number): Promise<{ published_revision: number }> {
    return request<{ published_revision: number }>(
      '/v1/settings/publish',
      jsonInit('POST', { revision }),
    );
  },
  rollback(toRevision: number): Promise<{ published_revision: number }> {
    return request<{ published_revision: number }>(
      '/v1/settings/rollback',
      jsonInit('POST', { to_revision: toRevision }),
    );
  },
  setSecret(providerId: string, secret: string): Promise<SecretStatus> {
    return request<SecretStatus>(
      `/v1/settings/providers/${encodeURIComponent(providerId)}/secret`,
      jsonInit('PUT', { secret }),
    );
  },
  deleteSecret(providerId: string): Promise<SecretStatus> {
    return request<SecretStatus>(
      `/v1/settings/providers/${encodeURIComponent(providerId)}/secret`,
      { method: 'DELETE' },
    );
  },
  probeProvider(providerId: string): Promise<ProbeResult> {
    return request<ProbeResult>(
      `/v1/settings/providers/${encodeURIComponent(providerId)}/probe`,
      { method: 'POST' },
    );
  },
  /** 今日各供应商已用 token。取不到时页面照常工作，只是不显示用量。 */
  settingsUsage(): Promise<{
    day: string | null;
    usage: Record<string, { day: string; used_tokens: number; estimated: boolean }>;
    limits: Record<string, number | null>;
  }> {
    return request('/v1/settings/usage');
  },
  /** 最近调用健康度：键是 `provider_id/model_id`，不含错误原文。 */
  settingsHealth(): Promise<ModelHealthResponse> {
    return request<ModelHealthResponse>('/v1/settings/health');
  },

  /* ------------------------------------------------------------ 媒体 */
  /** 上传一张图片。返回的 evidence_id 挂在当前会话上，她下一轮就能看到。 */
  uploadMedia(sessionId: string, file: File): Promise<MediaUploadResponse> {
    const form = new FormData();
    form.append('upload', file);
    // **不要**手写 Content-Type：multipart 的 boundary 由浏览器生成，
    // 自己设会把 boundary 弄丢，服务端解析失败。
    return request<MediaUploadResponse>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/media`,
      { method: 'POST', body: form },
    );
  },

  /* ------------------------------------------------------------ 生活 */
  activity(): Promise<ActivityResponse> {
    return request<ActivityResponse>('/v1/activity');
  },
  goals(): Promise<GoalsResponse> {
    return request<GoalsResponse>('/v1/goals');
  },
  proactive(): Promise<ProactiveResponse> {
    return request<ProactiveResponse>('/v1/proactive');
  },
  /**
   * 一天的**只读**生活时间轴（`GET /v1/life/timeline`）。
   *
   * 不传 `date` = 服务端按**登录者时区**的今天。前端不自己算"今天"：
   * 同一个请求在两台机器上会落到两个日期（见 docs/API-WEB-V1.md §11.1）。
   * 这个端点不生成、不重排、不调模型，读到的就是库里的投影。
   */
  lifeTimeline(date?: string): Promise<LifeTimelineOut> {
    const query = date ? `?date=${encodeURIComponent(date)}` : '';
    return request<LifeTimelineOut>(`/v1/life/timeline${query}`);
  },
  /**
   * 手动生成某一天的日程（owner only，`POST /v1/life/outline-jobs`）。
   *
   * **`plan_day` 必须来自时间轴的 `plan_day`**：那是角色计划日。按浏览器
   * 时区算"今天"在跨日附近会落到另一个日期，后端只会回 422。
   *
   * 202 = 已入队（不是已生成）：拿 `job_id` 去轮询；200 = 已经生成过、
   * 这次什么都没做（`state=unchanged`）。失败码由调用方翻译成人话。
   */
  createOutlineJob(body: {
    plan_day: string;
    force: boolean;
    request_id: string;
  }): Promise<OutlineJobOut> {
    return request<OutlineJobOut>('/v1/life/outline-jobs', jsonInit('POST', body));
  },
  /** 按作业 id 读生成进度（终态后调用方重读时间轴）。 */
  outlineJob(jobId: string): Promise<OutlineJobOut> {
    return request<OutlineJobOut>(`/v1/life/outline-jobs/${encodeURIComponent(jobId)}`);
  },

  /* ------------------------------------------------ 设置分类法与授权 */
  /** 分类法是导航的唯一来源；前端不自己抄类名。 */
  taxonomy(): Promise<TaxonomyResponse> {
    return request<TaxonomyResponse>('/v1/settings/taxonomy');
  },
  /**
   * 用户自己的偏好（当前只有时区）。
   *
   * **不进 `/settings/draft`**：时区是用户的事实，不是配置修订；跟发布/回滚
   * 绑定会让"改个时区"变成一次部署动作。
   */
  userPreferences(): Promise<UserPreferencesResponse> {
    return request<UserPreferencesResponse>('/v1/settings/preferences');
  },
  /**
   * 写时区。传空串 = 清掉偏好，回退到环境变量/默认值。
   *
   * 非法 IANA 名由服务端拒绝（400 `invalid_request`），前端**不自己维护
   * 时区清单**：那份清单一定会过期，而服务端有 tzdata。
   */
  setTimezone(timezone: string): Promise<UserPreferencesResponse> {
    return request<UserPreferencesResponse>(
      '/v1/settings/preferences',
      jsonInit('PUT', { timezone }),
    );
  },
  /**
   * 改"怎么看视频"（用户偏好，**不在模型页**）。
   *
   * 与 {@link setTimezone} 共用一条 PUT：两者都是 `user_preferences` 的一列，
   * 分开两个端点只会让"保存一半"变得可能。
   */
  setVideoMode(videoMode: VideoMode): Promise<UserPreferencesResponse> {
    return request<UserPreferencesResponse>(
      '/v1/settings/preferences',
      jsonInit('PUT', { video_mode: videoMode }),
    );
  },
  consents(): Promise<ConsentsResponse> {
    return request<ConsentsResponse>('/v1/consents');
  },
  setConsent(body: ConsentUpdate): Promise<{ scope: string; granted: boolean; revision: number }> {
    return request('/v1/consents', jsonInit('PUT', body));
  },

  /* ------------------------------------------------------------ 多用户与账号 */
  /**
   * 一页关系（默认 50 条）。**必须看 `has_more`**：这个列表天然无界
   * （群里见到的每个人都有一行），一次全取会让页面卡死。
   *
   * `q` 由**服务端**筛选（昵称 / subject_id / 账号外部 id）；前端不做本地过滤，
   * 否则"只搜当前页"会骗人。
   */
  subjects(query: SubjectsQuery = {}): Promise<SubjectsResponse> {
    const params = new URLSearchParams();
    if (query.limit !== undefined) params.set('limit', String(query.limit));
    if (query.offset !== undefined) params.set('offset', String(query.offset));
    const q = query.q?.trim();
    if (q) params.set('q', q);
    const suffix = params.toString();
    return request<SubjectsResponse>(`/v1/accounts/subjects${suffix ? `?${suffix}` : ''}`);
  },
  mergeSubjects(body: { source_id: string; target_id: string }): Promise<MergeResult> {
    return request<MergeResult>('/v1/accounts/subjects/merge', jsonInit('POST', body));
  },
  unmergeSubject(sourceId: string): Promise<UnmergeResult> {
    return request<UnmergeResult>(
      `/v1/accounts/subjects/${encodeURIComponent(sourceId)}/unmerge`,
      jsonInit('POST', {}),
    );
  },

  /* ------------------------------------------------------------ 关于与版本 */
  version(): Promise<VersionInfo> {
    return request<VersionInfo>('/v1/version');
  },
  /**
   * 镜像更新卡片（四态）：只读快照，`no-store`。
   *
   * 后端做白名单投影；前端不解析目录、不拼摘要、不比较版本。
   */
  imageUpdate(signal?: AbortSignal): Promise<ImageUpdateV2> {
    return request<ImageUpdateV2>('/v1/version/image-update', {
      cache: 'no-store',
      ...(signal ? { signal } : {}),
    });
  },
  /**
   * 触发一次**真正的按需检查**：后端只把请求原子写进 requests 目录，更新器认领后
   * 重新取目录、解析摘要，结果落回 `status.json`。回执只表示已写入（200/201/202），
   * 是否检查完还要重读 {@link imageUpdate} 看 `checked_at`。
   */
  imageUpdateCheck(requestId: string): Promise<ImageUpdateCheckAck> {
    return request<ImageUpdateCheckAck>(
      '/v1/version/image-update-checks',
      jsonInit('POST', { request_id: requestId }),
    );
  },
  /**
   * 提交一次镜像更新请求。自定义头 X-Ling-Update 是后端的防误触标记；
   * 浏览器会自动带上 Origin，服务端据此做同源校验。
   */
  requestImageUpdate(body: ImageUpdateSubmission): Promise<ImageUpdateSubmissionAck> {
    return request<ImageUpdateSubmissionAck>('/v1/version/image-update-requests', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Ling-Update': '1' },
      body: JSON.stringify(body),
    });
  },
  /** 手动触发的只读检查；不下载、不应用更新。 */
  checkRelease(): Promise<ReleaseCheck> {
    return request<ReleaseCheck>('/v1/version/check-release');
  },
  /** 只读的更新通道状态；不创建任务。 */
  updateStatus(signal?: AbortSignal): Promise<UpdateStatus> {
    return request<UpdateStatus>('/v1/version/update', {
      cache: 'no-store',
      ...(signal ? { signal } : {}),
    });
  },
  /**
   * 创建一次更新请求。自定义头 X-Ling-Update 是后端的防误触标记；
   * 浏览器会自动带上 Origin，服务端据此做同源校验。
   */
  requestUpdate(body: UpdateRequest): Promise<UpdateJob> {
    return request<UpdateJob>('/v1/version/update-requests', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Ling-Update': '1' },
      body: JSON.stringify(body),
    });
  },

  /* ------------------------------------------------------------ 记忆
   *
   * 只调用 docs/API-WEB-V1.md §10 冻结的 8 个端点。刻意**没有**：
   * 单条详情（GET /v1/memory/{id} 不存在）、分页（没有 offset/cursor）、
   * memory_episodes / 实体 / 向量 / 检索日志的读取端点。
   */

  /** 记忆列表。`include` 不传 = 只看现行记忆（未撤回且未归档）。 */
  listMemories(options: { limit?: number; include?: MemoryInclude | null } = {}): Promise<MemoryListResponse> {
    const query = new URLSearchParams({ limit: String(options.limit ?? 50) });
    if (options.include) query.set('include', options.include);
    return request<MemoryListResponse>(`/v1/memory?${query.toString()}`);
  },

  /** 撤回一条记忆：必填理由；重复撤回幂等。 */
  retractMemory(memoryId: string, reason: string): Promise<{ memory_id: string; retracted: boolean }> {
    return request(`/v1/memory/${encodeURIComponent(memoryId)}/retract`, jsonInit('POST', { reason }));
  },

  /** 纠正（supersede）：旧行被置撤回并指向新行，新行 `supersedes` 旧 id。 */
  supersedeMemory(
    memoryId: string,
    text: string,
    reason = '用户纠正',
  ): Promise<{ memory_id: string; supersedes: string }> {
    return request(
      `/v1/memory/${encodeURIComponent(memoryId)}/supersede`,
      jsonInit('POST', { text, reason }),
    );
  },

  /**
   * 遗忘全部（两段式的**请求阶段**）：真删当前 subject 的记忆与派生数据。
   *
   * 这个调用返回时物理删除**还没有做**——后台清理需要几秒到几分钟，进度走
   * `purgeStatus()`。返回里唯一能显示的是 `events_cancelled`（在途事件数），
   * 它**不是**"删掉了多少条记忆"，页面不许把它当计数用。
   *
   * 旧后端（两段式之前）只回 `{retracted}`（软清除的受影响行数）：这里如实
   * 透传，由页面识别并说明"这个后端还在软清除"，不假装已经真删。
   */
  forgetAllMemories(): Promise<ForgetResponse> {
    return request('/v1/memory/forget', { method: 'POST' });
  },

  /** 最近的清理记录：`{items: [...]}`，按时间倒序；没配信号 = 不取消。 */
  purgeStatus(signal?: AbortSignal): Promise<PurgeStatusResponse> {
    return request('/v1/memory/purge/status', {
      cache: 'no-store',
      ...(signal ? { signal } : {}),
    });
  },

  listCoreMemory(): Promise<CoreMemoryListResponse> {
    return request<CoreMemoryListResponse>('/v1/core-memory');
  },

  /** 同 label 覆盖（并重新激活），不是新增第二条。 */
  upsertCoreMemory(body: CoreMemoryInput): Promise<{ core_id: string; label: string; text: string }> {
    return request('/v1/core-memory', jsonInit('PUT', body));
  },

  /** 软删除：active=false，数据仍在库里。 */
  deleteCoreMemory(coreId: string): Promise<{ core_id: string; active: boolean }> {
    return request(`/v1/core-memory/${encodeURIComponent(coreId)}`, { method: 'DELETE' });
  },

  /* ------------------------------------------------------------ 角色
   *
   * 两套端点并存，**不是二选一**：
   *
   * - 旧：`GET|PUT /v1/persona` = 当前生效的那一套（单一人设时代留下的）。
   *   聊天页的称呼（`personaName.ts`）仍然只读它，所以这里保留不动。
   * - 新：`/v1/personas` 集合 + `/v1/personas/{id}/activate`。切换时是**整套**
   *   换掉（不是合并字段），所以"改哪套"必须由 id 指明，不能靠"当前那套"。
   *
   * 所有路径都带 `/v1`：本文件其余端点一律如此，漏掉前缀会得到 index.html
   * 和一条看不懂的 JSON 解析错误。
   */
  persona(): Promise<Persona> {
    return request<Persona>('/v1/persona');
  },
  /**
   * 旧端点：更新**当前生效**的那一套。人设页改版后不再用它写（改哪一套由
   * 卡片决定），保留是为了别的调用方与旧后端仍能用。
   */
  updatePersona(patch: PersonaUpdate): Promise<Persona> {
    return request<Persona>('/v1/persona', jsonInit('PUT', patch));
  },
  /**
   * 全部人格（`GET /v1/personas`）。
   *
   * 后端回 `{items, active_persona_id, total}`；裸数组与 `{personas}` 也接受
   * （见 `personaListFrom`）。`active_persona_id` 一起交出去：它是"哪套生效"
   * 的第二判据，页面在列表项没带 `is_active` 时用它。
   *
   * **列表项没有正文**（后端刻意不带，`PersonaListItem`）：卡片要摘要时页面
   * 自己按 id 调 `getPersona`。
   */
  async listPersonas(signal?: AbortSignal): Promise<{
    items: PersonaSummary[];
    activePersonaId: string | null;
    total: number;
  }> {
    const data = await request<unknown>('/v1/personas', {
      cache: 'no-store',
      ...(signal ? { signal } : {}),
    });
    return personaListFrom(data);
  },
  createPersona(body: PersonaCreate): Promise<PersonaSummary> {
    return request<PersonaSummary>('/v1/personas', jsonInit('POST', body));
  },
  getPersona(personaId: string): Promise<PersonaSummary> {
    return request<PersonaSummary>(`/v1/personas/${encodeURIComponent(personaId)}`);
  },
  updatePersonaById(personaId: string, patch: PersonaUpdate): Promise<PersonaSummary> {
    return request<PersonaSummary>(
      `/v1/personas/${encodeURIComponent(personaId)}`,
      jsonInit('PUT', patch),
    );
  },
  /** 删除一套人格。**当前生效的那套后端可能拒绝**（409/422），由调用方翻译。 */
  deletePersona(personaId: string): Promise<void> {
    return request<void>(`/v1/personas/${encodeURIComponent(personaId)}`, {
      method: 'DELETE',
    });
  },
  /**
   * 把某一套设为生效（整套替换，不合并）。
   *
   * 返回 `{persona_id}` 是后端回执，不是完整对象——切换后列表要重读一次，
   * 别拿回执拼一个"已激活"的本地状态。
   */
  activatePersona(personaId: string): Promise<{ persona_id: string }> {
    return request<{ persona_id: string }>(
      `/v1/personas/${encodeURIComponent(personaId)}/activate`,
      jsonInit('POST', {}),
    );
  },
  timeline(limit = 50): Promise<TimelineResponse> {
    return request<TimelineResponse>(`/v1/actor/timeline?limit=${limit}`);
  },

  /* ------------------------------------------------------------ 运行日志
   *
   * 只读**本进程**的环形缓冲（`GET /v1/logs`）：没有分页、没有游标、
   * 没有持久化，缓冲被覆盖的旧日志再也取不回来。要更早的历史得看容器日志
   * 采集，此处不假装能翻页。
   *
   * 只有 owner/admin 能调；普通成员收到 403，不是空列表。
   */

  /**
   * 取最近日志。空参数**不出现在 query 里**：传 `level=` 会让后端收到
   * 空串而不是"不筛选"，那是一个语义不同的请求。
   *
   * `signal` 用于轮询换挡/卸载时真正取消在途请求——否则暂停后仍会有一条
   * 迟到的响应把状态写回去，界面看起来"暂停了还在动"。
   */
  getLogs(params: LogQuery = {}, signal?: AbortSignal): Promise<LogsResponse> {
    const query = new URLSearchParams();
    if (params.limit !== undefined) query.set('limit', String(params.limit));
    if (params.level) query.set('level', params.level);
    if (params.logger) query.set('logger', params.logger);
    if (params.q) query.set('q', params.q);
    if (params.kind) query.set('kind', params.kind);
    const suffix = query.toString();
    return request<LogsResponse>(`/v1/logs${suffix ? `?${suffix}` : ''}`, {
      cache: 'no-store',
      ...(signal ? { signal } : {}),
    });
  },
};

export type { UserInfo };
