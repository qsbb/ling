/**
 * 接入器（设置 → 接入与连接 → 接入器）：**她从哪条线收发消息**。
 *
 * 为什么需要这一页：渠道配置全在宿主部署目录的 `.env`（OneBot 的
 * `LING_ONEBOT_*`、QQ 官方机器人的 `LING_QQBOT_*`），密钥只在那里（0600）。
 * 之前网页上完全没有它——"她收不到 QQ 消息"只能登宿主机翻文件或猜，
 * 而猜错的方向很贵（去改 NapCat，实际是 api 容器没重启）。
 *
 * ## 这一页只读，而且是**故意**只读
 *
 * 做成可编辑要同时回答三个问题：值存哪（DB 配置修订？宿主 `.env`？）、
 * 密钥怎么加密、改完怎么让进程重读。三个都没有现成答案，所以这一轮先让现状
 * **看得见**。因此这里没有输入框、没有保存按钮，也没有"显示密钥"——后端的
 * 视图对象里根本不含密钥值（见 `ling/settings/connectors.py`），前端也就
 * 不存在泄露它的路径。
 *
 * ## 四态，不是一个"不可用"
 *
 * `not_configured`（还没填）/ `disabled`（填了但开关关着）/
 * `configured_disconnected`（填了、开着、但没连上）/ `connected`。
 * 混成一句"不可用"，用户就分不清是漏填变量还是连接坏了——这两件事的下一步
 * 完全相反。视觉上除了颜色，徽标文案与左侧色条也各自不同（不只靠颜色区分）。
 *
 * 后端已经给了每个原因的**中文说明**与"怎么改"，这里原样展示，不在前端另编
 * 一套理由：判定只有一份，翻译也只有一份。
 */
import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import type { ConnectorField, ConnectorRuntime, ConnectorsResponse, ConnectorView } from '../types';
import { AlertIcon, ArrowLeftIcon, CheckIcon, RefreshIcon } from './Icons';
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

/**
 * 一项配置的"值"栏文案。
 *
 * 密钥只可能是"已设置/未设置"——后端不回值，这里也就没有可显示的值。
 * 开关显示开/关（它有默认值，"未设置"没有意义）；其余显示真实值。
 */
export function connectorFieldState(field: ConnectorField): string {
  if (field.kind === 'switch') return field.value ?? '—';
  if (field.kind === 'secret') return field.set ? '已设置' : '未设置';
  return field.value ?? '未设置';
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

/* ------------------------------------------------------------------ 卡片 */

export function ConnectorCard({ connector }: { connector: ConnectorView }) {
  const presentation = describeConnectorStatus(connector.status);
  const runtime = connectorRuntimeLine(connector.runtime);
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

      <h3 className="connector-card__subtitle">配置项</h3>
      <p className="connector-card__note">
        来自宿主部署目录的 <code>.env</code>。凭据只报"设了没有"，值不在网页上出现。
      </p>
      <ul className="connector-fields">
        {connector.fields.map((field) => {
          const state = connectorFieldState(field);
          const unset = field.kind !== 'switch' && !field.set;
          return (
            <li
              key={field.name}
              className="connector-field"
              data-testid={`connector-${connector.id}-field-${field.name}`}
            >
              <code className="connector-field__name" title={field.name}>
                {field.name}
              </code>
              <span className="connector-field__label">{field.label}</span>
              <span
                className={`connector-field__state${
                  unset ? ' connector-field__state--unset' : ''
                }${field.kind === 'secret' && field.set ? ' connector-field__state--set' : ''}`}
              >
                {state}
              </span>
            </li>
          );
        })}
      </ul>

      <p className="connector-card__guidance" data-testid={`connector-${connector.id}-guidance`}>
        <strong>怎么改：</strong>
        {renderInlineCode(connector.guidance)}
      </p>
    </section>
  );
}

/* ------------------------------------------------------------------ 页面 */

export function ConnectorsPage({ onBack }: ConnectorsPageProps) {
  const [state, setState] = useState<LoadState>({ status: 'loading' });

  const load = useCallback(async () => {
    setState({ status: 'loading' });
    try {
      setState({ status: 'ready', body: await api.connectors() });
    } catch (err) {
      setState({ status: 'error', error: explainError(err) });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section className="page connectors-page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回设置
      </button>

      <h1 className="page__title">接入器</h1>
      <p className="page__hint">
        她从哪条线收发消息：OneBot（NapCat 等）与 QQ 官方机器人。凭据、端口、地址都在
        宿主部署目录的 <code>.env</code> 里，<strong>本页只读</strong>——只报"设了没有、
        通没通"，不显示任何密钥值。改完环境变量要重启 api 服务才会生效。
      </p>

      <div className="connectors-page__actions">
        <button
          type="button"
          className="button button--small"
          data-testid="connectors-refresh"
          onClick={() => void load()}
          disabled={state.status === 'loading'}
        >
          <RefreshIcon /> 重新读取
        </button>
        {state.status === 'ready' ? (
          <span className="panel__count" data-testid="connectors-as-of">
            读取于 {formatAsOf(state.body.as_of)}
          </span>
        ) : null}
      </div>

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
        ? state.body.connectors.map((connector) => (
            <ConnectorCard key={connector.id} connector={connector} />
          ))
        : null}

      <p className="page__hint">
        为什么不能在这里改：改完值要写回宿主的 <code>.env</code> 并让进程重读，
        密钥还要先想清楚怎么存。那是另一轮的事；这一步先让它看得见。
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
