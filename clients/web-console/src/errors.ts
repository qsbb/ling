/** 把服务端错误码翻译成用户能执行的提示；不吞掉具体原因。 */
import { ApiError } from './api';
import type { ErrorPayload } from './types';

export interface ExplainedError {
  /** 一句话原因。 */
  reason: string;
  /** 可执行的建议；没有就给空串。 */
  hint: string;
  retriable: boolean;
  code: string;
  detail: string | null;
  traceId: string | null;
}

const CODE_TEXT: Record<string, { reason: string; hint: string; retriable?: boolean }> = {
  unauthorized: {
    reason: '登录已过期，需要重新登录',
    hint: '重新登录后会自动回到当前会话。',
    retriable: false,
  },
  forbidden: { reason: '没有权限访问该会话', hint: '', retriable: false },
  not_found: { reason: '目标不存在或已被删除', hint: '刷新会话列表后重试。', retriable: false },
  conflict: { reason: '状态冲突', hint: '刷新后按最新状态操作。', retriable: false },
  rate_limited: { reason: '请求过于频繁，被服务端限流', hint: '稍等片刻再试。', retriable: true },
  provider_unavailable: {
    reason: '模型供应商不可用',
    hint: '检查设置页的供应商地址与密钥，或先切换到可用供应商。',
    retriable: true,
  },
  provider_timeout: {
    reason: '模型响应超时',
    hint: '供应商没有在限定时间内返回；可以直接重试，或在设置页调大超时时间。',
    retriable: true,
  },
  provider_error: {
    reason: '模型供应商返回错误',
    hint: '检查密钥、额度与模型 ID；也可以稍后重试。',
    retriable: true,
  },
  cancelled: { reason: '本轮已中断', hint: '已保留中断前生成的内容；可重试。', retriable: true },
  unsupported_capability: { reason: '当前配置不支持该能力', hint: '', retriable: false },
  config_invalid: {
    reason: '模型配置校验未通过',
    hint: '到设置页修正职责绑定或供应商后再试。',
    retriable: false,
  },
  secret_missing: {
    reason: '供应商还没有保存密钥',
    hint: '到设置页为对应供应商保存密钥。',
    retriable: false,
  },
  internal: { reason: '服务端内部错误', hint: '可以重试；持续失败请查看服务端日志。', retriable: true },
  network_error: {
    reason: '无法连接服务端',
    hint: '确认后端进程与网络可用后重试。',
    retriable: true,
  },
  invalid_request: { reason: '请求参数不合法', hint: '', retriable: false },
};

export function explainError(error: unknown): ExplainedError {
  if (error instanceof ApiError) {
    const known = CODE_TEXT[error.code];
    return {
      reason: known?.reason ?? (error.message || '请求失败'),
      hint: known?.hint ?? '',
      retriable: error.retriable || known?.retriable === true,
      code: error.code,
      detail: error.detail,
      traceId: error.traceId,
    };
  }
  if (error && typeof error === 'object' && 'code' in error) {
    const payload = error as Partial<ErrorPayload>;
    const code = typeof payload.code === 'string' ? payload.code : 'internal';
    const known = CODE_TEXT[code];
    return {
      reason: known?.reason ?? (payload.message || '请求失败'),
      hint: known?.hint ?? '',
      retriable: payload.retriable ?? known?.retriable === true,
      code,
      detail: typeof payload.detail === 'string' ? payload.detail : null,
      traceId: typeof payload.trace_id === 'string' ? payload.trace_id : null,
    };
  }
  return {
    reason: error instanceof Error ? error.message : '未知错误',
    hint: '',
    retriable: true,
    code: 'internal',
    detail: null,
    traceId: null,
  };
}

/** 失败轮次的重试入口文案。 */
export function retryLabel(state: string): string {
  return state === 'stopped' ? '继续这一轮' : '重试';
}
