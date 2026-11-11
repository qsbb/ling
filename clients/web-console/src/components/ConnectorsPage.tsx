/**
 * 接入器（设置 → 接入与连接 → 接入器）：**她从哪条线收发消息**，以及在这里改。
 *
 * ## 为什么这里能改了
 *
 * 之前这一页只读，是因为"值存哪、密钥怎么加密、改完怎么让进程重读"三个问题
 * 都没有答案。现在后端把它们全答了（`ling/settings/connector_service.py`、
 * `PUT /v1/settings/connectors/{scope}`）：值落 DB 覆盖层、密钥只在受控路径解密、
 * 保存是一次"写候选修订 → 事务外切换 → 成功才提 generation，失败回滚旧配置"。
 * 页面这边只剩一件事：**把那次保存的真实结果如实画出来**。
 *
 * ## 四条不能违反的规矩
 *
 * 1. **密钥绝不回显**。后端视图里 `kind==='secret'` 的 `value` 恒为 null，
 *    前端也没有任何"显示密钥"的路径。密码框**永远是空的**，占位符写
 *    「已设置（留空则不修改）」；要清除只能点「清除」（走 `clear`）。
 *    这不只是美观问题：后端把"密钥给空串"定义成**不改**
 *    （`connector_service.py:198-205`），所以"清空输入框"本来也不该等于"删除"。
 *    可选的 `preview`（前 7 字符 + `••••`）只在输入框**旁边**作为"当前值…"
 *    出现，不塞进任何输入框——把可用凭据的前缀画成可编辑文本没有收益。
 * 2. **不乐观更新**。只有 `state==='active'` 才算生效；`rolled_back` 是失败
 *    （新配置没起来、已恢复旧配置），`degraded` 是"本机生效但远端没连上"
 *    （或回退本身失败）——三者文案必须分开，否则用户会以为保存成功了。
 *    提交期间按钮禁用、表单只读，避免连点产生第二次并发写。
 * 3. **状态码各说各的话**：`409` 是别处改过（要刷新后重试）、`422` 是字段不合法
 *    （逐个指出哪个键，并说明**此时一个连接都没停过**）、`503` 是切换器不可用
 *    （改不了，不是改错了）、`403` 要区分"不是 owner / 来源不可信"。
 * 4. **只发真正改过的键**。后端把"`set` 里没出现的键"定义成"不改"；把没动过的
 *    项一起发过去虽然也会被判成 `unchanged`，但会让审计里多出一堆"改了"的字段。
 *
 * ## 四态，不是一个"不可用"
 *
 * `not_configured`（还没填）/ `disabled`（填了但开关关着）/
 * `configured_disconnected`（填了、开着、但没连上）/ `connected`。
 * 混成一句"不可用"，用户就分不清是漏填变量还是连接坏了——这两件事的下一步
 * 完全相反。视觉上除了颜色，徽标文案与左侧色条也各自不同（不只靠颜色区分）。
 * 后端已经给了每个原因的**中文说明**与"怎么改"，这里原样展示，不在前端另编
 * 一套理由：判定只有一份，翻译也只有一份。
 *
 * ## 字段来源（`source`）
 *
 * `db`（网页上存过）/ `env`（进程启动时的环境变量）/ `default`（代码默认）。
 * "清除"一项 = 删掉 DB 覆盖、回退到 env 或默认，**不是写空串**——空串在密钥上
 * 是"没设过"、在文本上是"显式空值"，混起来会让"我明明清掉了"变成一个查不出来
 * 的怪现象（`connector_fields.py:19-21`）。
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { api, connectorErrorField, type ApiError } from '../api';
import { explainError, type ExplainedError } from '../errors';
import type {
  ConnectorAccessGroup,
  ConnectorChangeRequest,
  ConnectorChangeResult,
  ConnectorField,
  ConnectorResetResponse,
  ConnectorRuntime,
  ConnectorsResponse,
  ConnectorView,
} from '../types';
import { AlertIcon, ArrowLeftIcon, CheckIcon, RefreshIcon } from './Icons';
import {
  ConnectorInstancePanel,
  DEFAULT_INSTANCE_ID,
  instanceFormKey,
} from './ConnectorInstances';
import '../connectors.css';

interface ConnectorsPageProps {
  onBack: () => void;
}

type LoadState =
  | { status: 'loading' }
  | { status: 'ready'; body: ConnectorsResponse }
  | { status: 'error'; error: ExplainedError };

/* ------------------------------------------------------------------ 文案 */

/**
 * 把文案里反引号包起来的部分渲染成 `<code>`。
 *
 * 后端的 guidance 用反引号钉住**真实环境变量名**（`LING_QQBOT_APP_ID` 这类）。
 * 直接当纯文本显示会把反引号也画出来，读起来像 Markdown 没渲染；而整段用等宽
 * 又会让中文说明难读。这里只给变量名那一小段换字体。
 */
export function renderInlineCode(text: string): ReactNode[] {
  return text.split('`').map((part, index) =>
    index % 2 === 1 ? (
      <code key={index} className="connector-inline-code">
        {part}
      </code>
    ) : (
      part
    ),
  );
}

export type ConnectorTone = 'ok' | 'warn' | 'muted' | 'neutral';

export interface ConnectorStatusPresentation {
  /** 徽标文案。四态必须一字之差也读得出来，不能都叫"不可用"。 */
  label: string;
  /** 视觉基调（颜色 + 左侧色条）。**不只靠颜色**：文案本身也不同。 */
  tone: ConnectorTone;
  /** 徽标之外的一句解释："这意味着什么、现在该做什么"。 */
  hint: string;
}

/**
 * 状态码 → 徽标。**未知码不猜**：给一个明确的"状态未知"，而不是挑一个
 * 看起来最像的态（那会把"后端加了新状态"伪装成"一切正常/一切坏了"）。
 */
export function describeConnectorStatus(status: string): ConnectorStatusPresentation {
  switch (status) {
    case 'not_configured':
      return {
        label: '未配置',
        tone: 'neutral',
        hint: '还没填齐配置：要先把下面没设的项补上。这时还没有任何连接可谈，不用去查网络或 NapCat。',
      };
    case 'disabled':
      return {
        label: '已配置 · 渠道已关闭',
        tone: 'muted',
        hint: '凭据齐全，但渠道开关是关的：开关打开之前不会有任何连接被建立。',
      };
    case 'configured_disconnected':
      return {
        label: '已配置 · 未连接',
        tone: 'warn',
        hint: '配置齐了但当前没连上：看下面的原因和改法，不用重新填凭据。',
      };
    case 'connected':
      return {
        label: '已连接',
        tone: 'ok',
        hint: '当前连着，不需要改任何东西。',
      };
    default:
      return {
        label: '状态未知',
        tone: 'neutral',
        hint: '后端给了一个本页不认识的状态码：先刷新；仍然是它就看服务端日志。',
      };
  }
}

/** 开关的当前值（后端给布尔；旧后端给 `'1'`/`'0'` 字符串也认）。 */
export function switchOn(field: ConnectorField): boolean {
  const raw = field.value;
  if (typeof raw === 'boolean') return raw;
  if (typeof raw === 'string') return raw === '1' || raw === 'true';
  return false;
}

function textValueOf(field: ConnectorField): string {
  return typeof field.value === 'string' ? field.value : '';
}

function listValueOf(field: ConnectorField): string[] {
  const raw = field.value;
  if (Array.isArray(raw)) return raw.map((item) => String(item));
  if (typeof raw === 'string' && raw !== '') return raw.split(',');
  return [];
}

/** 这一项有没有生效值（`configured` 是契约名，`set` 是兼容别名）。 */
export function isFieldSet(field: ConnectorField): boolean {
  return field.configured === true || field.set === true;
}

/**
 * 一项配置的"值"栏文案（**只读**展示用；表单里由输入框本身显示值）。
 *
 * 密钥只可能是"已设置/未设置"——后端不回值，这里也就没有可显示的值。
 * 开关显示开/关（它有默认值，"未设置"没有意义）；名单显示成"a, b"；其余显示真实值。
 */
export function connectorFieldState(field: ConnectorField): string {
  if (field.kind === 'switch') return switchOn(field) ? '开' : '关';
  if (field.kind === 'secret') return isFieldSet(field) ? '已设置' : '未设置';
  if (field.kind === 'list') {
    const items = listValueOf(field);
    return items.length === 0 ? '未设置' : items.join(', ');
  }
  const value = textValueOf(field);
  return value === '' ? '未设置' : value;
}

/**
 * 运行时一句话（连接数 + 已收帧数）。
 *
 * `connections === null` 表示这条入站路径没有可观察的连接对象（只配了 HTTP
 * 上报），此时**不编数字**：整行不显示。
 */
export function connectorRuntimeLine(runtime: ConnectorRuntime): string | null {
  if (runtime.connections === null) return null;
  const connections =
    runtime.connections === 0 ? '当前没有连接' : `当前 ${runtime.connections} 条连接`;
  const received = runtime.received === null ? '' : `，已收 ${runtime.received} 帧事件`;
  if (runtime.connections > 0 && runtime.received === 0) {
    return `${connections}${received}：连接是通的，但对面还没推过任何数据。`;
  }
  return `${connections}${received}`;
}

/** 来源徽标文案：DB 覆盖 / 环境变量 / 默认值。未知来源如实说"来源未知"。 */
export function sourceLabel(source: string): string {
  switch (source) {
    case 'db':
      return '网页上保存的';
    case 'env':
      return '来自环境变量';
    case 'default':
      return '默认值';
    default:
      return `来源未知（${source}）`;
  }
}

/* ------------------------------------------------------------------ 表单值 */

/**
 * 表单里的一个值。
 *
 * 密钥字段的值**只可能是"这次要写入的新串"**：初始恒为空串，从不为了回显
 * 而初始化。这是"密钥绝不回显"在数据结构上的保证，而不是靠渲染时记得别画。
 */
export type FieldDraft =
  | { kind: 'text'; value: string }
  | { kind: 'secret'; value: string }
  | { kind: 'switch'; value: boolean }
  | { kind: 'list'; items: string[] }
  | { kind: 'select'; value: string };

/**
 * `<select>` 里的"清除"选项值。
 *
 * 为什么需要它：`LING_ACCESS_MODE` 有代码默认值（`off`），空串是没法从下拉里
 * 表达的；而"清除覆盖、回退默认"本身是合法操作（走 `clear` 而不是 `set: ''`，
 * 后者会被字段校验拒成 422）。用一个不可能与真实取值相撞的哨兵值区分。
 */
export const SELECT_UNSET = '__unset__';

/** `LING_ACCESS_MODE` 的三个合法取值（见 `connector_fields.py:67`）。 */
export const ACCESS_MODES = ['off', 'whitelist', 'blacklist'] as const;

/** 从后端字段初始化表单：密钥是空串（**绝不预填**）。 */
export function draftFromFields(fields: ConnectorField[]): Record<string, FieldDraft> {
  const draft: Record<string, FieldDraft> = {};
  for (const field of fields) {
    const key = field.key || field.name;
    switch (field.kind) {
      case 'secret':
        draft[key] = { kind: 'secret', value: '' };
        break;
      case 'switch':
        draft[key] = { kind: 'switch', value: switchOn(field) };
        break;
      case 'list':
        draft[key] = { kind: 'list', items: listValueOf(field) };
        break;
      case 'text':
        draft[key] =
          field.control === 'select'
            ? { kind: 'select', value: textValueOf(field) }
            : { kind: 'text', value: textValueOf(field) };
        break;
      default:
        draft[key] = { kind: 'text', value: textValueOf(field) };
    }
  }
  return draft;
}

/** 名单去空（保留字符串与顺序：QQ 号可能有前导零，**绝不转数字**）。 */
function normalizedItems(items: readonly string[]): string[] {
  return items.map((item) => item.trim()).filter((item) => item !== '');
}

/**
 * 按 `ChangeRequest.build` 的真实语义算这次要发的请求体
 * （对齐 `connector_service.py:161-212`，以及 `settings.py:776-785` 的字段说明）。
 *
 * 规则逐条：
 * - 文本：变了且非空 → `set`；**清空**一个原本有值的项 → `clear`
 *   （= 删掉覆盖、回退 env/默认，不是写空串）；
 * - 名单：变了 → `set` 数组；清空 → `clear`；
 * - 开关：变了才发（布尔）；
 * - 下拉：选「清除」→ `clear`；其余 → `set`；
 * - 密钥：**空串一律不发**（后端同义：不改）；非空才进 `set`；
 *   点过「清除」→ 进 `clear`。
 *
 * 只发改过的键：没动过的项一个都不出现，审计里也就不会多出"改了"的字段。
 * 返回 `{ change, dirty }`，`dirty` 只用于"有未保存的改动"这句提示。
 */
export function buildConnectorChange(
  fields: ConnectorField[],
  draft: Record<string, FieldDraft>,
  clearKeys: readonly string[] = [],
): { change: ConnectorChangeRequest; dirty: boolean } {
  const change: ConnectorChangeRequest = { set: {}, clear: [] };
  const clearSet = new Set(clearKeys);
  const initial = draftFromFields(fields);
  let dirty = false;

  const addClear = (key: string) => {
    if (!change.clear.includes(key)) change.clear.push(key);
    delete change.set[key];
    dirty = true;
  };

  for (const field of fields) {
    const key = field.key || field.name;
    const current = draft[key];
    if (!current) continue;
    const before = initial[key];

    if (clearSet.has(key)) {
      addClear(key);
      continue;
    }

    switch (current.kind) {
      case 'secret': {
        // 空串 = 不改。这就是"密码框留空不修改"的全部实现。
        if (current.value !== '') {
          change.set[key] = current.value;
          dirty = true;
        }
        break;
      }
      case 'switch': {
        const previous = before?.kind === 'switch' ? before.value : false;
        if (current.value !== previous) {
          change.set[key] = current.value;
          dirty = true;
        }
        break;
      }
      case 'list': {
        const items = normalizedItems(current.items);
        const previous = before?.kind === 'list' ? normalizedItems(before.items) : [];
        const same =
          items.length === previous.length && items.every((item, i) => item === previous[i]);
        if (same) break;
        if (items.length === 0) addClear(key);
        else {
          change.set[key] = items;
          dirty = true;
        }
        break;
      }
      case 'select': {
        if (current.value === SELECT_UNSET) {
          addClear(key);
          break;
        }
        const previous = before?.kind === 'select' ? before.value : '';
        if (current.value !== previous) {
          change.set[key] = current.value;
          dirty = true;
        }
        break;
      }
      case 'text':
      default: {
        const value = (current as { value: string }).value;
        const previous = before && 'value' in before ? String(before.value) : '';
        if (value === previous) break;
        if (value === '') addClear(key);
        else {
          change.set[key] = value;
          dirty = true;
        }
        break;
      }
    }
  }

  // 显式标记要清除、但上面没走到的键（例如字段列表里没有它的旧草案）。
  for (const key of clearKeys) addClear(key);

  return { change, dirty };
}

/* ------------------------------------------------------------------ 保存结果 */

export type NoticeTone = 'ok' | 'warn' | 'error';

export interface SaveNotice {
  tone: NoticeTone;
  /** 一句话结论。必须能单独读：不能只说"完成"。 */
  text: string;
  /** 细节：后端给的安全文案 + 要用户做的下一步。 */
  detail?: string;
  /** 需要"刷新后重试"时，界面给一个刷新动作。 */
  retryWithReload?: boolean;
}

/** 生效终态的中文：`active` 之外一律不是成功。 */
export function describeVerifyState(state: string | null | undefined): string {
  switch (state) {
    case 'active':
      return '已生效';
    case 'unchanged':
      return '没有变化';
    case 'activating':
      return '正在切换（还没到终态）';
    case 'rolled_back':
      return '新配置没起来，已回滚';
    case 'degraded':
      return '本机生效但远端没连上，或回退本身失败';
    default:
      return '状态未知';
  }
}

/**
 * `PUT` 成功（HTTP 2xx）之后，按**返回的 `state`** 翻译成结论。
 *
 * 这正是最容易骗人的地方：后端在 `rolled_back` 时仍返回 **202**
 * （`settings.py:966-968`），只看 HTTP 状态码就会把"失败并已回滚"画成成功。
 */
export function describeChangeResult(result: ConnectorChangeResult): SaveNotice {
  const backend = result.error_safe ? `后端说明：${result.error_safe}` : '';
  switch (result.state) {
    case 'active':
      return {
        tone: 'ok',
        text: '已生效：新配置已经在运行，连接已按新配置重建。',
      };
    case 'unchanged':
      return {
        tone: 'ok',
        text: '没有变化：提交的值与当前生效的完全一致，没有重建连接。',
      };
    case 'rolled_back':
      return {
        tone: 'error',
        text: '新配置没起来，已恢复旧配置——这次保存没有生效。',
        detail: `她现在用的还是改之前那一份，旧连接也已经恢复。请按下面的原因改好再保存。${
          backend ? ` ${backend}` : ''
        }`,
      };
    case 'degraded':
      return {
        tone: 'warn',
        text: '配置已写入，但没能完全生效（远端没连上，或回退本身失败）。',
        detail: `这不等于"连上了"：看卡片上的状态与原因。${backend ? ` ${backend}` : ''}`,
      };
    case 'activating':
      return {
        tone: 'warn',
        text: '已提交，正在切换——还不算生效。',
        detail: '稍等几秒后点「刷新状态」看终态；在那之前不要说它已经生效。',
      };
    default:
      return {
        tone: 'warn',
        text: `保存返回了一个本页不认识的终态：${result.state}。`,
        detail: `不能当作成功：请刷新后用服务端日志确认。${backend ? ` ${backend}` : ''}`,
      };
  }
}

/**
 * `PUT` 失败（4xx/5xx）的状态码 → 原因与下一步。
 *
 * 422 的关键那句是"**一个连接都没停过**"：后端在静态校验阶段就返回
 * （`ChangeRequest.build` 与 `validate_full` 都在切换之前），所以修字段时
 * 不用担心她的连接被这段坏配置打断。
 */
export function describeSaveError(error: unknown): SaveNotice {
  const status = (error as Partial<ApiError> | null)?.status;
  const explained = explainError(error);
  const message = (error as Error | null)?.message ?? explained.reason;
  const field = connectorErrorField(error);

  if (status === 409) {
    // 两种 409 的下一步不一样：代际冲突要刷新后重试，另一处正在生效要等它完成。
    // 用后端给的**稳定原因码**判定（`error.detail`），文案只作兜底。
    const inProgress =
      explained.detail === 'activation_in_progress' || message.includes('正在生效');
    return {
      tone: 'error',
      text: inProgress
        ? '另一处保存正在生效，这次的改动没有提交。'
        : '别处已经改过了，这次的改动没有提交。',
      detail: inProgress
        ? `${message}。等那一处完成后点「刷新状态」，再按最新配置重试。`
        : `${message}。请先点「刷新状态」拿到最新配置，再重新修改并保存——直接重试会再次撞车。`,
      retryWithReload: true,
    };
  }
  if (status === 422) {
    return {
      tone: 'error',
      text: field
        ? `配置项 ${field} 不合法，这次没有保存（一个连接都没停过）。`
        : '有配置项不合法，这次没有保存（一个连接都没停过）。',
      detail: `${message}${field ? `（${field}）` : ''}。修好这一项再保存。`,
    };
  }
  if (status === 503) {
    return {
      tone: 'error',
      text: '这个进程现在切换不了接入器，配置没有写入。',
      detail: `${message}。这不是你填错了：请确认在 API 进程上操作（worker 进程不具备切换能力）。`,
    };
  }
  if (status === 403) {
    const detail = explained.detail;
    if (detail === 'bad_origin') {
      return {
        tone: 'error',
        text: '这个请求的来源不可信（来源校验没通过），改动没有提交。',
        detail: '请用部署时配置的正式地址打开控制台，不要用别的域名或端口转发。',
      };
    }
    if (detail === 'not_owner') {
      return {
        tone: 'error',
        text: '只有所有者可以修改接入器配置。',
        detail: '当前登录的账号不是所有者；请让所有者来改。',
      };
    }
    return { tone: 'error', text: '没有权限修改接入器配置。', detail: message };
  }
  if (status === 404) {
    return {
      tone: 'error',
      text: '后端没有这个接入器（版本可能不匹配）。',
      detail: `${message}。先确认前端与后端版本一致。`,
    };
  }
  return {
    tone: 'error',
    text: `保存失败：${explained.reason}`,
    detail: explained.detail ?? explained.hint ?? message,
  };
}

/** `POST /reset` 的结果 → 结论。`ok:false` 时**绝不能说"已恢复默认"**。 */
export function describeResetResult(result: ConnectorResetResponse): SaveNotice {
  const failed = result.results.filter((item) => item.state === 'failed');
  const rolled = result.results.filter(
    (item) => item.state === 'rolled_back' || item.state === 'degraded',
  );
  if (result.ok && rolled.length === 0) {
    const cleared = result.results.flatMap((item) => item.cleared);
    return {
      tone: 'ok',
      text:
        cleared.length > 0
          ? '已恢复默认：网页上存过的覆盖都清掉了。'
          : '网页上没有存过覆盖，配置本来就是环境变量 / 默认值。',
      ...(cleared.length > 0
        ? { detail: `回退到环境变量或代码默认：${cleared.join('、')}` }
        : {}),
    };
  }
  const parts = [...failed, ...rolled].map((item) => {
    const reason = item.error_safe ?? item.error_code ?? item.state;
    return `${item.scope}：${reason}`;
  });
  return {
    tone: 'error',
    text: '恢复默认没有全部成功——没清干净的部分仍按旧配置运行。',
    detail: `${parts.join('；')}。这不是"部分成功"：请刷新后逐个接入器检查。`,
  };
}

/* ------------------------------------------------------------------ 表单控件 */

export interface FieldControlProps {
  field: ConnectorField;
  draft: FieldDraft | undefined;
  cleared: boolean;
  disabled: boolean;
  invalid: boolean;
  onEdit: (value: FieldDraft) => void;
}

/** 一个字段的输入控件。密钥**只有**密码框，没有"显示原值"这条路径。 */
export function FieldControl({
  field,
  draft,
  cleared,
  disabled,
  invalid,
  onEdit,
}: FieldControlProps) {
  const key = field.key || field.name;
  const id = `connector-input-${key}`;
  const common = {
    id,
    disabled,
    'aria-invalid': invalid ? true : undefined,
    'aria-describedby': invalid ? `${id}-error` : undefined,
    'data-testid': `connector-input-${key}`,
  } as const;

  // `draft === undefined` 表示**这个实例的草案还没建好**（例如刚切到非默认实例、
  // 它的字段视图还没回来）。从前这里 `return null`，控件就**静默消失**：字段行、
  // 说明、「已设置」全在，只有一个空白的 `.connector-field__control`。
  //
  // 那正是本仓库反复要消灭的「名字比实现强」——一行长得跟正常字段一样的东西，
  // 里面什么都没有，而且不报错。改成**显式降级**：保住 `data-testid`（测试能
  // 看见它），写成 disabled，并说明为什么不能改。
  if (draft === undefined) {
    return (
      <input
        {...common}
        type="text"
        value=""
        placeholder="这个实例的配置还没读回来，暂时不能编辑"
        disabled
        data-draft-missing="true"
      />
    );
  }
  if (cleared) {
    return (
      <p className="connector-edit__cleared" data-testid={`connector-cleared-${key}`}>
        保存后会清除这一项：删掉网页上的覆盖，回退到环境变量或默认值。
      </p>
    );
  }

  switch (draft.kind) {
    case 'secret':
      return (
        <input
          {...common}
          type="password"
          className="connector-input"
          // **永远不回显**：值只来自用户这次输入，初始为空串。
          value={draft.value}
          placeholder={isFieldSet(field) ? '已设置（留空则不修改）' : '还没设置，填入新值后保存'}
          autoComplete="new-password"
          spellCheck={false}
          onChange={(event) => onEdit({ kind: 'secret', value: event.target.value })}
        />
      );
    case 'switch':
      return (
        <label className="connector-switch">
          <input
            {...common}
            type="checkbox"
            className="connector-switch__box"
            checked={draft.value}
            onChange={(event) => onEdit({ kind: 'switch', value: event.target.checked })}
          />
          <span className="connector-switch__text">{draft.value ? '开' : '关'}</span>
        </label>
      );
    case 'list':
      return (
        <textarea
          {...common}
          className="connector-input connector-input--list"
          value={draft.items.join('\n')}
          rows={Math.min(6, Math.max(3, draft.items.length + 1))}
          placeholder="每行一项（例如 QQ 号）。留空 = 回退到默认"
          spellCheck={false}
          onChange={(event) =>
            onEdit({
              kind: 'list',
              items: event.target.value.split('\n').map((line) => line.trim()),
            })
          }
        />
      );
    case 'select': {
      const known = (ACCESS_MODES as readonly string[]).includes(draft.value);
      return (
        <select
          {...common}
          className="connector-input connector-input--select"
          value={known ? draft.value : SELECT_UNSET}
          onChange={(event) => onEdit({ kind: 'select', value: event.target.value })}
        >
          {ACCESS_MODES.map((mode) => (
            <option key={mode} value={mode}>
              {mode}
            </option>
          ))}
          <option value={SELECT_UNSET}>清除（回退默认 off）</option>
        </select>
      );
    }
    case 'text':
    default:
      return (
        <input
          {...common}
          type="text"
          className="connector-input"
          value={(draft as { value: string }).value}
          spellCheck={false}
          autoComplete="off"
          placeholder={isFieldSet(field) ? '' : '未设置'}
          onChange={(event) => onEdit({ kind: 'text', value: event.target.value })}
        />
      );
  }
}

/** 字段行：变量名 / 说明 / 来源（+ 密钥预览）/ 控件 / 密钥清除按钮。 */
export function FieldRow({
  field,
  draft,
  cleared,
  disabled,
  invalid,
  onEdit,
  onToggleClear,
}: FieldControlProps & { onToggleClear: () => void }) {
  const key = field.key || field.name;
  const id = `connector-input-${key}`;
  const set = isFieldSet(field);
  return (
    <li
      className={`connector-field${invalid ? ' connector-field--invalid' : ''}`}
      data-testid={`connector-field-${key}`}
      data-source={field.source}
      data-invalid={invalid ? 'true' : undefined}
    >
      <label className="connector-field__name" htmlFor={cleared ? undefined : id} title={key}>
        {key}
      </label>
      <span className="connector-field__label">
        <span>{field.label}</span>
        {field.kind === 'secret' && field.preview ? (
          <span className="connector-field__preview" data-testid={`connector-preview-${key}`}>
            当前值 {field.preview}
          </span>
        ) : null}
      </span>
      <span className="connector-field__meta">
        <span className="connector-field__source" data-testid={`connector-source-${key}`}>
          {sourceLabel(field.source)}
        </span>
        {field.kind !== 'switch' ? (
          <span
            className={`connector-field__state connector-field__state--${set ? 'set' : 'unset'}`}
            data-testid={`connector-state-${key}`}
          >
            {set ? '已设置' : '未设置'}
          </span>
        ) : null}
      </span>
      <div className="connector-field__control">
        <FieldControl
          field={field}
          draft={draft}
          cleared={cleared}
          disabled={disabled}
          invalid={invalid}
          onEdit={onEdit}
        />
        {field.kind === 'secret' ? (
          <button
            type="button"
            className="button button--small connector-field__clear"
            disabled={disabled}
            data-testid={`connector-clear-${key}`}
            aria-pressed={cleared}
            onClick={onToggleClear}
          >
            {cleared ? '不清除了' : '清除'}
          </button>
        ) : null}
      </div>
      {invalid ? (
        <p className="connector-field__error" id={`${id}-error`} role="alert">
          这一项没通过校验：按上面的说明改好再保存。
        </p>
      ) : null}
    </li>
  );
}

/* ------------------------------------------------------------------ 通知条 */

function Notice({
  notice,
  testId,
  onReload,
}: {
  notice: SaveNotice;
  testId: string;
  onReload?: () => void;
}) {
  const ok = notice.tone === 'ok';
  return (
    <div
      className={`connector-notice connector-notice--${notice.tone}`}
      role={ok ? 'status' : 'alert'}
      aria-live={ok ? 'polite' : 'assertive'}
      data-testid={testId}
      data-tone={notice.tone}
    >
      <div className="connector-notice__body">
        <strong>{notice.text}</strong>
        {notice.detail ? <div className="connector-notice__detail">{notice.detail}</div> : null}
      </div>
      {notice.retryWithReload && onReload ? (
        <button
          type="button"
          className="button button--small"
          onClick={onReload}
          data-testid={`${testId}-reload`}
        >
          <RefreshIcon /> 刷新后重试
        </button>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ 卡片 */

export interface ConnectorFormProps {
  draft: Record<string, FieldDraft>;
  clearKeys: readonly string[];
  onEdit: (key: string, value: FieldDraft) => void;
  onToggleClear: (key: string) => void;
  onResetDraft: () => void;
  onSave: () => void;
  saving: boolean;
  /** 提交中的一句话（"正在保存…"），期间表单只读。 */
  formNote: string | null;
  notice: SaveNotice | null;
  /** 字段级错误（422 的 `error.field`）：只标对应那一行。 */
  fieldError: string | null;
  /** 「刷新状态」（409 之后要走这条）。 */
  onReload: () => void;
}

interface ConnectorCardProps extends ConnectorFormProps {
  /**
   * 这张卡片**表单**用的视图：选中的是默认实例时就是页面级视图；切到非默认
   * 实例时它的 `fields` 换成那个实例自己的（拿不到就是空数组——输入框空着，
   * 不画别人的值）。`key` 只用于重建表单子树。
   */
  connector: ConnectorView;
  /**
   * 给实例面板用的**默认实例**视图，应当始终是页面级那一次 GET 的结果。
   *
   * 为什么不直接用 `connector`：非默认实例的 `connector` 可能是一份空字段的
   * 视图，而面板会拿它 `draftFromFields()` 现造草案——空字段会让面板抛
   * `fields is not iterable` 并整块崩掉。面板的四态诊断描述的是"这条渠道"，
   * 也不该跟着实例走。不传时退回 `connector`（老调用点/测试）。
   */
  panelConnector?: ConnectorView;
  /**
   * 这张卡片的表单此刻属于哪个实例（`instanceFormKey` 的右半边）。
   *
   * **只用于表单子树的 `key`**，不画出来：`ConnectorView` 本身不带
   * `instance_id`（它是页面级/按实例读回来的同一张视图），实例身份只有页面
   * 知道，所以要显式给。
   */
  instanceId?: string;
  /**
   * 字段列表为空时，**为什么**空（非空字符串 = 原因）。
   *
   * 默认实例的字段列表不存在"空得可疑"这种情况（页面级 GET 一定给），所以
   * 只有切到非默认实例、视图还没到/读不到时才传。没有这一段，"空表单"与
   * "这个实例真的没配"在界面上不可区分。
   */
  emptyFieldsReason?: string | null;
  /**
   * 实例面板的上报，**原样透传**给 `ConnectorInstancePanel`。
   *
   * 卡片自己不消费这两条：它只负责把面板和页面的草案状态连起来。面板**不**
   * 在卡片里再渲染一张卡片（那会递归），所以这条链是单向的
   * 面板 → 卡片 → 页面。
   */
  onSelectInstance?: (instanceId: string) => void;
  onInstanceView?: (instanceId: string, fields: ConnectorField[] | null) => void;
}

/**
 * 一张接入器卡：上面是四态诊断（原样保留），下面是可编辑表单。
 *
 * 表单**始终展开**：用户点进来就是为了改，"点进去居然没有配置界面"正是这一轮
 * 要修的问题；再套一层"先点编辑"只会让人以为又是只读的。
 */
export function ConnectorCard({
  connector,
  panelConnector,
  instanceId,
  emptyFieldsReason = null,
  onSelectInstance,
  onInstanceView,
  ...form
}: ConnectorCardProps) {
  const presentation = describeConnectorStatus(connector.status);
  const runtime = connectorRuntimeLine(connector.runtime);
  const editable = connector.editable;
  const change = useMemo(
    () => buildConnectorChange(connector.fields, form.draft, form.clearKeys),
    [connector.fields, form.draft, form.clearKeys],
  );
  /**
   * 列表上要给哪些实例画脏标。
   *
   * 卡片是**每个 scope 一张**，而它的表单此刻属于 `instanceId` 那个实例——
   * 所以这里能确定"当前这个实例有没有未保存改动"（`change.dirty` /
   * `form.clearKeys`）。别的实例的草案也存在页面里，但卡片看不到，因此这里
   * 只报自己这一份；面板会把外部给的与自算的取并集。
   *
   * 为什么必须从这里给：从前面板拿**自己**的 `forms` 算脏标，而字段输入框读写
   * 的是页面的 `forms`——于是"卡片写着有未保存改动、列表上却没有脏标"。
   */
  const dirtyInstanceIds = useMemo(() => {
    const out = new Set<string>();
    const id = instanceId ?? DEFAULT_INSTANCE_ID;
    if (change.dirty || form.clearKeys.length > 0) out.add(id);
    return out;
  }, [change.dirty, form.clearKeys, instanceId]);
  const verifyState = connector.activation?.state ?? null;
  const submit = () => {
    if (!form.saving) form.onSave();
  };

  return (
    <section
      className={`connector-card connector-card--${presentation.tone}`}
      data-testid={`connector-${connector.id}`}
      data-status={connector.status}
      aria-labelledby={`connector-${connector.id}-title`}
    >
      <div className="connector-card__head">
        <h2 className="connector-card__title" id={`connector-${connector.id}-title`}>
          {connector.label}
        </h2>
        <span
          className={`connector-status connector-status--${presentation.tone}`}
          data-testid={`connector-${connector.id}-status`}
        >
          {presentation.tone === 'ok' ? <CheckIcon /> : null}
          {presentation.label}
        </span>
      </div>

      <p className="connector-card__hint" data-testid={`connector-${connector.id}-hint`}>
        {presentation.hint}
      </p>

      <p className="connector-card__reason" data-testid={`connector-${connector.id}-reason`}>
        <strong>当前情况：</strong>
        {renderInlineCode(connector.status_message)}
      </p>

      {runtime ? (
        <p className="connector-card__runtime" data-testid={`connector-${connector.id}-runtime`}>
          {runtime}
        </p>
      ) : null}

      {/*
        实例面板放在「配置项」**之前**：实例是账号级的，字段是账号内的配置。
        顺序反过来会让人以为"先改字段、再选账号"——实际保存是写进当前选中的
        那个实例的，选错了会把 A 的值存到 B 上。
      */}
      <ConnectorInstancePanel
        scope={connector.id}
        connector={panelConnector ?? connector}
        canEdit={editable}
        pageBusy={form.saving}
        onSelectInstance={onSelectInstance}
        onInstanceView={onInstanceView}
        dirtyInstanceIds={dirtyInstanceIds}
      />

      <h3 className="connector-card__subtitle">配置项</h3>
      <p className="connector-card__note">
        存在服务端的数据库覆盖层里，保存后立刻按新配置重建连接。密钥只报"设了没有"，
        网页上不存在回显——密码框留空表示不修改。
      </p>

      <form
        className="connector-form"
        // `key` 带上当前实例：切实例时 React **重建**这个表单子树，任何内部
        // 残留（将来有人加一个不受控输入/useState 缓冲）都带不过来。这是防串
        // 草稿的第二道闸，第一道是 `(scope, instanceId)` 分区键。
        //
        // **只加在这里，不能加在卡片或面板上**：那会连面板一起重建，面板的
        // 选中态与列表会被打回初始值（选中态变 null、列表退回 loading），
        // 切实例时看起来像"点一下就全清空了"。
        key={`${connector.id}::${instanceId ?? ''}`}
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        {emptyFieldsReason ? (
          <p
            className="connector-form__note connector-form__note--warn"
            role="status"
            data-testid={`connector-${connector.id}-fields-unverified`}
          >
            {emptyFieldsReason}
          </p>
        ) : null}
        <ul className="connector-fields">
          {connector.fields.map((field) => {
            const key = field.key || field.name;
            return (
              <FieldRow
                key={key}
                field={field}
                draft={form.draft[key]}
                cleared={form.clearKeys.includes(key)}
                disabled={!editable || form.saving}
                invalid={form.fieldError === key}
                onEdit={(value) => form.onEdit(key, value)}
                onToggleClear={() => form.onToggleClear(key)}
              />
            );
          })}
        </ul>

        <div className="connector-form__actions" role="group" aria-label="保存">
          <button
            type="submit"
            className="button button--primary button--small"
            disabled={!editable || form.saving}
            data-testid={`connector-${connector.id}-save`}
          >
            {form.saving ? '保存中…' : '保存并生效'}
          </button>
          <button
            type="button"
            className="button button--small"
            disabled={!editable || form.saving}
            data-testid={`connector-${connector.id}-revert`}
            onClick={form.onResetDraft}
          >
            撤销修改
          </button>
          <button
            type="button"
            className="button button--small"
            disabled={form.saving}
            data-testid={`connector-${connector.id}-reload`}
            onClick={form.onReload}
          >
            <RefreshIcon /> 刷新状态
          </button>
          <span className="connector-form__note" data-testid={`connector-${connector.id}-note`}>
            {form.formNote ??
              (editable
                ? change.dirty
                  ? '有未保存的改动'
                  : /*
                     * 保存被**挡下**时（"没有要保存的改动"这类 `warn`）必须说一句，
                     * 否则用户点了保存却什么反应都没有。
                     *
                     * 但**只有 `warn` 走这里**：`ok`（"已生效…"）已经由上面那块
                     * 通知区显示，挤进这一行会把版本号顶掉——而版本号是"这次生效的
                     * 是哪一版"的唯一可见证据，既有浏览器测试正是拿它断言的
                     * （`connectors.browser.test.tsx`：保存后 note 含"当前版本 4"）。
                     */
                    (form.notice?.tone === 'warn' ? form.notice.text : null) ??
                    `当前版本 ${connector.generation} · ${describeVerifyState(verifyState)}`
                : '只有所有者可以修改')}
          </span>
        </div>
      </form>

      {form.notice ? (
        <Notice
          notice={form.notice}
          testId={`connector-${connector.id}-notice`}
          onReload={form.onReload}
        />
      ) : null}

      <p className="connector-card__guidance" data-testid={`connector-${connector.id}-guidance`}>
        <strong>怎么改：</strong>
        {renderInlineCode(connector.guidance)}
      </p>
    </section>
  );
}

/* ------------------------------------------------------------------ 白名单组 */

/**
 * 「出入许可」组：`LING_ACCESS_MODE` / `LING_ACCESS_LIST`。
 *
 * 它不是一个"渠道"（后端也把它单独放在 `ConnectorsOut.access` 里，不混进
 * `connectors`），所以用一张更小的卡：没有四态、没有运行时数字，但同样参与
 * generation 与 `If-Match`，改动同样立刻生效（只是不重建长连接）。
 */
export function AccessCard({
  group,
  ...form
}: ConnectorFormProps & { group: ConnectorAccessGroup }) {
  const editable = group.editable;
  const change = useMemo(
    () => buildConnectorChange(group.fields, form.draft, form.clearKeys),
    [group.fields, form.draft, form.clearKeys],
  );
  const mode = group.fields.find((field) => field.key === 'LING_ACCESS_MODE');
  const list = group.fields.find((field) => field.key === 'LING_ACCESS_LIST');
  const items = list ? listValueOf(list) : [];
  return (
    <section
      className="connector-card connector-card--neutral"
      data-testid="connector-access"
      aria-labelledby="connector-access-title"
    >
      <div className="connector-card__head">
        <h2 className="connector-card__title" id="connector-access-title">
          出入许可（谁能跟她说话）
        </h2>
        <span
          className="connector-status connector-status--neutral"
          data-testid="connector-access-status"
        >
          {describeVerifyState(group.activation?.state ?? null)}
        </span>
      </div>
      <p className="connector-card__hint">
        这一组管的是"谁能给她发消息"。它与上面的渠道共用并发代际，但改动不重建长连接，
        所以这里没有"未连接"这种状态。
      </p>
      <p className="connector-card__reason" data-testid="connector-access-reason">
        <strong>当前情况：</strong>
        模式 <code className="connector-inline-code">{mode ? textValueOf(mode) || 'off' : '—'}</code>
        {list ? `，名单里 ${items.length} 项` : ''}
        {items.length === 0
          ? '。白名单模式下列表为空会导致谁都不能说话，这种组合后端会直接拒绝。'
          : ''}
      </p>

      <h3 className="connector-card__subtitle">配置项</h3>
      <form
        className="connector-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (!form.saving) form.onSave();
        }}
      >
        <ul className="connector-fields">
          {group.fields.map((field) => {
            const key = field.key || field.name;
            return (
              <FieldRow
                key={key}
                field={field}
                draft={form.draft[key]}
                cleared={form.clearKeys.includes(key)}
                disabled={!editable || form.saving}
                invalid={form.fieldError === key}
                onEdit={(value) => form.onEdit(key, value)}
                onToggleClear={() => form.onToggleClear(key)}
              />
            );
          })}
        </ul>
        <div className="connector-form__actions" role="group" aria-label="保存">
          <button
            type="submit"
            className="button button--primary button--small"
            disabled={!editable || form.saving}
            data-testid="connector-access-save"
          >
            {form.saving ? '保存中…' : '保存并生效'}
          </button>
          <button
            type="button"
            className="button button--small"
            disabled={!editable || form.saving}
            data-testid="connector-access-revert"
            onClick={form.onResetDraft}
          >
            撤销修改
          </button>
          <button
            type="button"
            className="button button--small"
            disabled={form.saving}
            data-testid="connector-access-reload"
            onClick={form.onReload}
          >
            <RefreshIcon /> 刷新状态
          </button>
          <span className="connector-form__note" data-testid="connector-access-note">
            {form.formNote ??
              (editable
                ? change.dirty
                  ? '有未保存的改动'
                  : `当前版本 ${group.generation}`
                : '只有所有者可以修改')}
          </span>
        </div>
      </form>

      {form.notice ? (
        <Notice notice={form.notice} testId="connector-access-notice" onReload={form.onReload} />
      ) : null}
    </section>
  );
}

/* ------------------------------------------------------------------ 页面 */

/** 一个 scope 的提交状态。 */
interface PendingState {
  saving: boolean;
  notice: SaveNotice | null;
  fieldError: string | null;
}

const IDLE: PendingState = { saving: false, notice: null, fieldError: null };

/**
 * 页面上的表单状态（草案 + 待清除的键），按 **`instanceFormKey(scope, instanceId)`**
 * 分开存——不是按 scope。
 *
 * 为什么必须带上实例：保存走的是 `PUT .../{scope}?instance_id=`，写的是**当前选中
 * 那个实例**。只按 scope 分区的话，在实例 A 里改了不保存、切到 B，B 的输入框画的
 * 就是 A 的草案，点保存就把 A 的改动存进了 B，而且服务端返回成功——这是一条
 * 会静默写错账号的路径。分区键用面板里那个 `instanceFormKey`，两处必须是
 * **同一个函数**，否则页面与面板各说一套。
 */
interface FormState {
  draft: Record<string, FieldDraft>;
  clearKeys: string[];
}

/**
 * 从一次 GET 构造各 scope **默认实例**的初始草案。
 *
 * 页面级 GET（不带 `?instance_id=`）读到的是默认实例的视图，所以这里只建
 * `default` 那一份键。非默认实例的草案由 `ensureInstanceForm` 在**切换时**
 * 按那个实例自己的字段视图按需建立——不从别处借。
 */
export function formsOf(body: ConnectorsResponse): Record<string, FormState> {
  const forms: Record<string, FormState> = {
    [instanceFormKey('access', DEFAULT_INSTANCE_ID)]: {
      draft: draftFromFields(body.access?.fields ?? []),
      clearKeys: [],
    },
  };
  for (const connector of body.connectors) {
    forms[instanceFormKey(connector.id, DEFAULT_INSTANCE_ID)] = {
      draft: draftFromFields(connector.fields),
      clearKeys: [],
    };
  }
  return forms;
}

/**
 * 按需给一个 `(scope, instanceId)` 建草案，**只用那个实例自己的字段视图**。
 *
 * - 已经建好 → 原样返回（切回 A 时 A 的未保存内容还在）。
 * - 没建好 → 用 `fields` 建一份。`fields` 必须是**这个实例**的已保存值；
 *   拿不到就传空数组，让输入框空着，**不要**借默认实例的值冒充它。
 */
export function ensureInstanceForm(
  forms: Record<string, FormState>,
  scope: string,
  instanceId: string,
  fields: ConnectorField[],
): Record<string, FormState> {
  const key = instanceFormKey(scope, instanceId);
  const existing = forms[key];
  if (existing !== undefined) {
    /*
     * 已有键时**不能无条件早退**。
     *
     * 这个键可能在"字段视图还没回来"时就用**空字段列表**建过了
     * （`handleInstanceView(..., null)` → `ensureInstanceForm(..., [])`）。
     * 之后视图真的到了，若这里直接 return，草案就永远停在空的那份：字段行
     * 照画（`connector.fields` 来自视图），输入框却全是空——
     * 用户看到 14 个空框，以为这个实例什么都没配，而保存会把空值写上去。
     *
     * 所以只在"现有草案是空的、而新字段不为空"时重建。用户已经改过的草案
     * （`draft` 非空或 `clearKeys` 非空）一律保留——那是他的未保存输入。
     */
    const untouched =
      existing.clearKeys.length === 0 && Object.keys(existing.draft).length === 0;
    if (!untouched || fields.length === 0) return forms;
    return { ...forms, [key]: { draft: draftFromFields(fields), clearKeys: [] } };
  }
  return {
    ...forms,
    [key]: { draft: draftFromFields(fields), clearKeys: [] },
  };
}

export function ConnectorsPage({ onBack }: ConnectorsPageProps) {
  const [state, setState] = useState<LoadState>({ status: 'loading' });
  const [forms, setForms] = useState<Record<string, FormState>>({});
  /**
   * 每个 scope 当前选中的实例。面板上报、页面存——保存要用它拼 `?instance_id=`，
   * 草案分区也要用它。
   *
   * 默认 `default`：面板第一帧选中后端排序第一个（默认实例排最前），在它上报
   * 之前保存只会落到默认实例上，与升级前（`PUT` 不带 `instance_id`）等价。
   */
  const [selectedInstance, setSelectedInstance] = useState<Record<string, string>>({});
  /**
   * 面板按选中实例重读到的字段视图：`scope → 该实例的字段`。
   *
   * 页面级 GET 只带默认实例的视图；非默认实例的"已保存的值"只有面板按
   * `?instance_id=` 读得到，所以由面板回灌到页面，页面再拿它给卡片当字段来源，
   * 并在切换（或视图迟到）时用**这个实例自己的值**建草案。
   */
  const [instanceFields, setInstanceFields] = useState<Record<string, ConnectorField[]>>({});
  /**
   * `scope → 选中实例的字段视图**不可得**`（读取失败，或响应没回显实例）。
   *
   * 与"这个实例确实什么都没配"是两件事，界面上也不能一样：前者要把输入框
   * 空着**并说明为什么空**，否则空表单会被读成"它真的是空的"。所以不能只靠
   * `instanceFields[scope] === undefined` 一个信号表达两种含义。
   */
  const [instanceUnverified, setInstanceUnverified] = useState<Record<string, boolean>>({});
  const [pending, setPending] = useState<Record<string, PendingState>>({});
  const [resetting, setResetting] = useState(false);
  const [resetNotice, setResetNotice] = useState<SaveNotice | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);

  const body = state.status === 'ready' ? state.body : null;
  const canEdit = body?.can_edit === true;

  /** 某个 scope 当前选中的实例 id（没有选中时按默认实例）。 */
  const instanceOf = useCallback(
    (scope: string) => selectedInstance[scope] ?? DEFAULT_INSTANCE_ID,
    [selectedInstance],
  );

  /**
   * 重新读一次 GET。
   *
   * `resetDrafts` 为真时丢弃默认实例的草案（刷新 = 按服务端现状重填）；为假时
   * 保留用户正在改的内容（保存失败后他还要接着改）。**notice 不动**：那条描述
   * 的是"上一次保存怎么了"，刷新不会让它过期，反而正是 409 之后要留下的信息。
   *
   * 非默认实例的草案**一律保留**：页面级 GET 不带实例，这里拿不到它们的现状，
   * 丢弃就等于悄悄替用户撤销了输入。
   */
  const reload = useCallback(async (resetDrafts: boolean) => {
    try {
      const next = await api.connectors();
      setState({ status: 'ready', body: next });
      setForms((previous) => {
        const fresh = formsOf(next);
        if (resetDrafts) {
          const kept: Record<string, FormState> = {};
          for (const [key, form] of Object.entries(previous)) {
            if (!Object.prototype.hasOwnProperty.call(fresh, key)) kept[key] = form;
          }
          return { ...kept, ...fresh };
        }
        const merged: Record<string, FormState> = {};
        for (const [key, form] of Object.entries(fresh)) merged[key] = previous[key] ?? form;
        return merged;
      });
    } catch (err) {
      if (resetDrafts) setState({ status: 'error', error: explainError(err) });
    }
  }, []);

  const load = useCallback(async () => {
    setState({ status: 'loading' });
    await reload(true);
  }, [reload]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * 面板上报：它按实例读到了什么。
   *
   * 只报选中、不报视图的话，切换后卡片没有这个实例的字段可分。收到视图后
   * 立刻按它建草案；拿不到视图就走"不可得"那条路：输入框空着、并说明为什么
   * 空。**空着是诚实的，画出默认实例的值不是。**
   *
   * `instanceFields`（画什么）与 `instanceUnverified`（能不能当真）必须成对
   * 更新：只改一份就会出现"空表单看起来像这个实例真的没配"。
   */
  const handleInstanceView = useCallback(
    (scope: string, instanceId: string, fields: ConnectorField[] | null) => {
      if (instanceId === DEFAULT_INSTANCE_ID) return;
      /*
       * 键**必须带 instanceId**。
       *
       * 从前这里是 `instanceFields[scope]`、`instanceUnverified[scope]`——一个
       * scope 只能记住"最后一次读到的那个实例"的字段。于是：
       *
       *   alpha（已保存 …21）→ 切 beta（读到 …22，写进 instanceFields['onebot']）
       *   → 切回 alpha
       *
       * 切回时页面拿到的 `fields` 还是 **beta 的 …22**，而 `form.draft` 是
       * alpha 的。用户看到的是 B 的值，点保存就把 B 的配置写进了 A——
       * 交接书点名的那个雷的**镜像形态**（显示 B、存进 A）。
       *
       * `instanceFormKey` 早就是 `(scope, instanceId)` 分区的，只有这两份没跟上。
       */
      const viewKey = instanceFormKey(scope, instanceId);
      setInstanceFields((previous) => {
        if (fields === null) {
          if (!Object.prototype.hasOwnProperty.call(previous, viewKey)) return previous;
          const next = { ...previous };
          delete next[viewKey];
          return next;
        }
        if (previous[viewKey] === fields) return previous;
        return { ...previous, [viewKey]: fields };
      });
      setInstanceUnverified((previous) => {
        const was = previous[viewKey] === true;
        if (fields === null) {
          return was ? previous : { ...previous, [viewKey]: true };
        }
        if (!was) return previous;
        const next = { ...previous };
        delete next[viewKey];
        return next;
      });
      setForms((previous) => ensureInstanceForm(previous, scope, instanceId, fields ?? []));
    },
    [],
  );

  const selectInstance = useCallback((scope: string, instanceId: string) => {
    setSelectedInstance((previous) =>
      previous[scope] === instanceId ? previous : { ...previous, [scope]: instanceId },
    );
  }, []);

  /**
   * 编辑某个 `(scope, instanceId)` 的一项。
   *
   * `instanceId` **由调用方显式传入**（取自渲染这一帧所用的实例），不在回调里
   * 现读 `selectedInstance`：回调可能晚于一次切换执行，现读会把 A 的输入写进
   * 刚刚选中的 B——这正是要防的那个串稿。
   */
  const editField = useCallback(
    (scope: string, instanceId: string, key: string, value: FieldDraft) => {
      setForms((previous) => {
        const form = previous[instanceFormKey(scope, instanceId)];
        if (!form) return previous;
        // 一旦手工编辑这一项，就不再是"待清除"。
        return {
          ...previous,
          [instanceFormKey(scope, instanceId)]: {
            draft: { ...form.draft, [key]: value },
            clearKeys: form.clearKeys.filter((item) => item !== key),
          },
        };
      });
      setPending((previous) => ({
        ...previous,
        [scope]: { ...(previous[scope] ?? IDLE), notice: null, fieldError: null },
      }));
    },
    [],
  );

  const toggleClear = useCallback((scope: string, instanceId: string, key: string) => {
    const formKey = instanceFormKey(scope, instanceId);
    setForms((previous) => {
      const form = previous[formKey];
      if (!form) return previous;
      const on = form.clearKeys.includes(key);
      const draft = form.draft[key];
      return {
        ...previous,
        [formKey]: {
          // 清除密钥时把输入框里没保存的内容一起清掉：留着它会让"清除"
          // 与"我要设置新值"这两种意图混在同一个表单里。
          draft:
            draft?.kind === 'secret'
              ? { ...form.draft, [key]: { kind: 'secret', value: '' } }
              : form.draft,
          clearKeys: on ? form.clearKeys.filter((item) => item !== key) : [...form.clearKeys, key],
        },
      };
    });
    setPending((previous) => ({
      ...previous,
      [scope]: { ...(previous[scope] ?? IDLE), notice: null, fieldError: null },
    }));
  }, []);

  /**
   * 撤销某个 `(scope, instanceId)` 的改动。
   *
   * 非默认实例的"已保存的值"页面级 GET 拿不到，只有面板按实例读回的那份
   * （`instanceFields[scope]`）。两处都没有就退化成空表单（"撤销"不是"回填
   * 别人的值"）。
   */
  const revertDraft = useCallback(
    (scope: string, instanceId: string, fields: ConnectorField[]) => {
      setForms((previous) => ({
        ...previous,
        [instanceFormKey(scope, instanceId)]: { draft: draftFromFields(fields), clearKeys: [] },
      }));
      setPending((previous) => ({ ...previous, [scope]: { ...IDLE } }));
    },
    [],
  );

  /**
   * 保存一个 `(scope, instanceId)`。
   *
   * **不乐观更新**：只有 `state==='active'` 才算生效，且生效后一定要重新读 GET
   * （卡片状态来自服务端，不能本地拼）。
   *
   * `instanceId` 显式传入并原样作为 `PUT` 的 query 参数：草案分区键、请求参数、
   * 生效后重建草案用的字段，三者必须是**同一个**实例——少一处就会出现
   * "改了 A、存到 B、界面还画着 A 的草稿"。
   */
  const save = useCallback(
    async (
      scope: string,
      instanceId: string,
      fields: ConnectorField[],
      generation: number,
    ) => {
      const formKey = instanceFormKey(scope, instanceId);
      const form = forms[formKey];
      if (!form) return;
      const { change } = buildConnectorChange(fields, form.draft, form.clearKeys);
      if (Object.keys(change.set).length === 0 && change.clear.length === 0) {
        setPending((previous) => ({
          ...previous,
          [scope]: {
            saving: false,
            fieldError: null,
            notice: {
              tone: 'warn',
              text: '没有要保存的改动。',
              detail: '填入新的值（或点「清除」）之后再来保存。',
            },
          },
        }));
        return;
      }
      setPending((previous) => ({
        ...previous,
        [scope]: { saving: true, notice: null, fieldError: null },
      }));
      try {
        const result = await api.saveConnectorConfig(scope, change, generation, instanceId);
        setPending((previous) => ({
          ...previous,
          [scope]: { saving: false, notice: describeChangeResult(result), fieldError: null },
        }));
        const settled = result.state === 'active' || result.state === 'unchanged';
        // 只有真正生效（或确实什么都没变）才丢掉草案；rolled_back / degraded
        // 一律保留用户填的内容，方便他改完再存。
        if (settled) {
          setForms((previous) => ({
            ...previous,
            [formKey]: { draft: draftFromFields(fields), clearKeys: [] },
          }));
        }
        await reload(settled);
      } catch (err) {
        const notice = describeSaveError(err);
        setPending((previous) => ({
          ...previous,
          [scope]: {
            saving: false,
            notice,
            fieldError: notice.tone === 'error' ? connectorErrorField(err) : null,
          },
        }));
      }
    },
    [forms, reload],
  );

  const runReset = useCallback(async () => {
    setResetting(true);
    setResetNotice(null);
    try {
      const result = await api.resetConnectorConfig();
      setResetNotice(describeResetResult(result));
      await reload(true);
    } catch (err) {
      const explained = explainError(err);
      setResetNotice({
        tone: 'error',
        text: '恢复默认失败，配置没有改动。',
        detail: explained.detail ?? explained.hint ?? explained.reason,
      });
    } finally {
      setResetting(false);
      setConfirmReset(false);
    }
  }, [reload]);

  const anySaving = resetting || Object.values(pending).some((item) => item.saving);

  /**
   * 渲染一张卡片需要的表单状态。
   *
   * `instanceId` 与 `fields` 由调用方给出**同一个实例的**两样东西：
   * 草案按 `(scope, instanceId)` 取，字段视图是同一实例的已保存值。
   * 两者错配就是"在 B 的界面上画 A 的值"，所以放在一个参数位上一起传。
   */
  const formPropsFor = (
    scope: string,
    instanceId: string,
    fields: ConnectorField[],
    generation: number,
  ): ConnectorFormProps => {
    const now = pending[scope] ?? IDLE;
    const form = forms[instanceFormKey(scope, instanceId)];
    return {
      draft: form?.draft ?? {},
      clearKeys: form?.clearKeys ?? [],
      onEdit: (key, value) => editField(scope, instanceId, key, value),
      onToggleClear: (key) => toggleClear(scope, instanceId, key),
      onResetDraft: () => revertDraft(scope, instanceId, fields),
      onSave: () => void save(scope, instanceId, fields, generation),
      saving: now.saving,
      formNote: now.saving ? '正在保存：要等服务端切换完成才算生效…' : null,
      notice: now.notice,
      fieldError: now.fieldError,
      onReload: () => void load(),
    };
  };

  return (
    <section className="page connectors-page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回设置
      </button>

      <h1 className="page__title">接入器</h1>
      <p className="page__hint">
        她从哪条线收发消息：OneBot（NapCat 等）与 QQ 官方机器人。配置存在服务端的数据库
        覆盖层里，<strong>保存后立刻尝试生效</strong>；密钥永远不回显，密码框留空表示不修改。
      </p>

      <div className="connectors-page__actions">
        <button
          type="button"
          className="button button--small"
          data-testid="connectors-refresh"
          onClick={() => void load()}
          disabled={state.status === 'loading' || anySaving}
        >
          <RefreshIcon /> 重新读取
        </button>
        {state.status === 'ready' ? (
          <span className="panel__count" data-testid="connectors-as-of">
            读取于 {formatAsOf(state.body.as_of)}
          </span>
        ) : null}
        {canEdit ? (
          <button
            type="button"
            className="button button--small"
            data-testid="connectors-reset"
            disabled={anySaving}
            onClick={() => setConfirmReset(true)}
          >
            恢复默认
          </button>
        ) : null}
      </div>

      {state.status === 'ready' && !canEdit ? (
        <p className="panel__note" data-testid="connectors-readonly">
          当前账号不是所有者：可以看，但不能改。修改接入器需要所有者账号。
        </p>
      ) : null}

      {confirmReset ? (
        <div
          className="connector-confirm"
          role="alertdialog"
          aria-labelledby="connector-reset-title"
        >
          <strong id="connector-reset-title">把三个接入器的网页覆盖全部清掉？</strong>
          <p>
            会回退到环境变量或代码默认值（不是清空成空白），并立刻按回退后的配置重建连接。
            这一步不能撤销。
          </p>
          <div className="connector-confirm__actions">
            <button
              type="button"
              className="button button--small button--danger"
              data-testid="connectors-reset-confirm"
              disabled={resetting}
              onClick={() => void runReset()}
            >
              {resetting ? '正在恢复…' : '确认恢复默认'}
            </button>
            <button
              type="button"
              className="button button--small"
              data-testid="connectors-reset-cancel"
              disabled={resetting}
              onClick={() => setConfirmReset(false)}
            >
              再想想
            </button>
          </div>
        </div>
      ) : null}

      {resetNotice ? <Notice notice={resetNotice} testId="connectors-reset-notice" /> : null}

      {state.status === 'error' ? (
        <div className="banner banner--error" role="status" data-testid="connectors-error">
          <AlertIcon />
          <div>
            <strong>{state.error.reason}</strong>
            {state.error.hint ? <div className="banner__detail">{state.error.hint}</div> : null}
          </div>
        </div>
      ) : null}

      {state.status === 'loading' ? (
        <p className="panel__note" data-testid="connectors-loading">
          正在读取各渠道的配置与连接状态…
        </p>
      ) : null}

      {state.status === 'ready' && state.body.connectors.length === 0 ? (
        <p className="panel__note" data-testid="connectors-empty">
          后端没有返回任何接入器。这通常意味着服务端版本比这个页面旧，先确认版本。
        </p>
      ) : null}

      {state.status === 'ready'
        ? state.body.connectors.map((connector) => {
            const instanceId = instanceOf(connector.id);
            /*
             * 卡片画哪个实例的字段：选中的是默认实例 → 页面级视图（本来就是它的）；
             * 其余 → 面板按 `?instance_id=` 读回、且**能证明**属于它的那份。
             * 还没有那份视图时用**空字段列表**，让输入框空着——画默认实例的值
             * 会让人以为 B 已经配好了，那正是"名字比实现强"。
             */
            const fields =
              instanceId === DEFAULT_INSTANCE_ID
                ? connector.fields
                : (instanceFields[instanceFormKey(connector.id, instanceId)] ?? []);
            const view: ConnectorView =
              instanceId === DEFAULT_INSTANCE_ID ? connector : { ...connector, fields };
            return (
              <ConnectorCard
                /*
                 * 卡片本身**不**带 `key={instanceId}`：那会连面板一起重建，
                 * 面板的选中态和列表会被打回初始值。切实例的"物理隔离"由表单
                 * 子树自己的 `key` 负责（见卡片里 `<form>` 上那条注释）。
                 */
                key={connector.id}
                connector={view}
                /*
                 * 面板自己那份**默认实例**视图：它不随选中变化，所以卡片上
                 * 「四态诊断」讲的始终是"这条渠道现在怎么样"，而不是去读一个
                 * 被换成**空字段数组**的视图（那会让面板 `draftFromFields([])`
                 * 拿不到字段、直接抛错整块崩掉）。
                 */
                panelConnector={connector}
                instanceId={instanceId}
                onSelectInstance={(next) => selectInstance(connector.id, next)}
                onInstanceView={(nextId, nextFields) =>
                  handleInstanceView(connector.id, nextId, nextFields)
                }
                /*
                 * 非默认实例、且它的字段视图还没读回（或读失败）：说明**为什么
                 * 空着**，而不是让一个空表单被读成"这个实例真的什么都没配"。
                 */
                emptyFieldsReason={
                  instanceId !== DEFAULT_INSTANCE_ID && fields.length === 0
                    ? instanceUnverified[instanceFormKey(connector.id, instanceId)] === true
                      ? '没读到这个实例的字段视图（读取失败，或响应没说明它读的是哪个实例）。下面的输入框因此是空的——它不代表这个实例的配置为空，保存前请先确认。'
                      : '正在读取这个实例的字段视图…读到之前下面的输入框是空的，它们不代表这个实例的配置为空。'
                    : null
                }
                {...formPropsFor(connector.id, instanceId, fields, connector.generation)}
              />
            );
          })
        : null}

      {state.status === 'ready' && state.body.access ? (
        <AccessCard
          group={state.body.access}
          {...formPropsFor('access', DEFAULT_INSTANCE_ID, state.body.access.fields, state.body.access.generation)}
        />
      ) : null}

      <p className="page__hint">
        保存是"一次切换"：先校验、再写候选修订、然后按新配置重建连接。
        <strong>只有返回「已生效」才算成功</strong>；新配置起不来时服务端会自动恢复旧配置，
        并在上面明确告诉你"没生效"。
      </p>
    </section>
  );
}

/** 时间戳只用于"这次读取发生在什么时候"；解析不了就照原样显示，不编时间。 */
export function formatAsOf(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(
    parsed.getHours(),
  )}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`;
}
