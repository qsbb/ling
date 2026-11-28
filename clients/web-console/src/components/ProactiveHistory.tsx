import type { ProactiveRecord } from '../types';
import { formatSessionTime } from '../format';

const SOURCES: Record<string, string> = {
  commitment_due: '约定即将到期',
  activity_done: '有记录的活动',
  user_asked_later: '你请她稍后联系',
  intent_due: '她之前计划稍后提起',
};

const RECEIPTS: Record<string, string> = {
  queued: '等待投递',
  sent: '已发送',
  delivered: '已送达',
  failed: '发送失败',
  uncertain: '结果不确定',
  unsupported: '渠道不支持',
};

export type ProactiveOutcomeTone = 'delivered' | 'sent_unconfirmed' | 'uncertain' | 'failed' | 'pending' | 'other';

export function classifyProactiveOutcome(record: ProactiveRecord): { tone: ProactiveOutcomeTone; badge: string } {
  if (record.receipt) {
    switch (record.receipt.state) {
      case 'delivered':
        return { tone: 'delivered', badge: '确认送达' };
      case 'sent':
        return { tone: 'sent_unconfirmed', badge: '已发送待确认' };
      case 'queued':
        return { tone: 'pending', badge: '等待投递' };
      case 'uncertain':
        return { tone: 'uncertain', badge: '结果不确定' };
      case 'failed':
        return { tone: 'failed', badge: '发送失败' };
      case 'unsupported':
        return { tone: 'other', badge: '渠道不支持' };
      default:
        return { tone: 'other', badge: '状态未知' };
    }
  }
  if (record.web_saved) return { tone: 'delivered', badge: '已写入会话' };
  switch (record.state) {
    case 'candidate':
      return { tone: 'pending', badge: '待处理' };
    case 'dispatching':
      return { tone: 'pending', badge: '投递中' };
    case 'sent':
      return { tone: 'sent_unconfirmed', badge: '已发送待确认' };
    case 'continued':
      return { tone: 'delivered', badge: '已在对话中分享' };
    case 'uncertain':
      return { tone: 'uncertain', badge: '结果不确定' };
    case 'failed':
      return { tone: 'failed', badge: '发送失败' };
    case 'rejected':
      return { tone: 'other', badge: '已跳过' };
    case 'expired':
      return { tone: 'other', badge: '已过期' };
    default:
      return { tone: 'other', badge: '状态未知' };
  }
}

export function proactiveSourceLabel(record: ProactiveRecord): string {
  return record.has_source ? (SOURCES[record.reason_code ?? ''] ?? '已关联来源') : '未记录';
}

export function proactiveOutcomeText(record: ProactiveRecord): string {
  if (record.receipt) {
    return `平台回执：${RECEIPTS[record.receipt.state] ?? '状态未知'}`;
  }
  if (record.web_saved) return '已写入 Web 会话';
  switch (record.state) {
    case 'candidate': return '待处理，尚无投递回执';
    case 'dispatching': return '正在尝试发送，尚无投递回执';
    case 'sent': return '记录为已发送，未找到独立回执';
    case 'failed': return '发送失败，未找到独立回执';
    case 'uncertain': return '发送结果不确定';
    case 'rejected': return '已跳过';
    case 'expired': return '机会已过期';
    case 'continued': return '已在对话中分享';
    default: return '发送结果未知';
  }
}

export function ProactiveHistory({
  records,
  defaultOpen = false,
}: {
  records: ProactiveRecord[];
  defaultOpen?: boolean;
}) {
  if (!records.length) return <p className="page__hint">还没有主动联系记录。</p>;
  return (
    <details className="proactive-history" data-testid="proactive-history" open={defaultOpen || undefined}>
      <summary>最近的主动联系记录（{records.length}，最多 50 条）</summary>
      <p className="page__hint">检查结果是当时的记录，当前条件可能已经改变。</p>
      <ul className="proactive-history__list">
        {records.map((record) => {
          const classified = classifyProactiveOutcome(record);
          return (
            <li
              className={`proactive-history__item proactive-history__item--${classified.tone}`}
              data-outcome={classified.tone}
              key={record.opportunity_id}
            >
              <div className="proactive-history__heading">
                <div className="proactive-history__title">
                  <span className={`proactive-badge proactive-badge--${classified.tone}`} data-testid="proactive-outcome-badge">
                    {classified.badge}
                  </span>
                  <strong>{proactiveOutcomeText(record)}</strong>
                </div>
                <time dateTime={record.created_at}>{formatSessionTime(record.created_at)}</time>
              </div>
              <p>来源：{proactiveSourceLabel(record)}</p>
              <p>缘由：{record.reason}</p>
              {record.draft_origin === 'unknown' && !record.activity_id ? <p>草稿来源：未记录</p> : null}
              {record.related_memory_id ? <p>关联记忆 <code>{record.related_memory_id}</code></p> : null}
              {record.acknowledgement ? <p>{({
                received: '用户确认已收到', misunderstood: '用户说明自己理解不同', agreed: '用户明确认可',
              })[record.acknowledgement]}</p> : null}
              {record.draft_state === 'uncertain' ? <p>开场白生成结果暂未确认</p> : null}
              <p>
                上次检查：{record.gate.allowed === null ? '没有记录' : record.gate.allowed ? '允许继续' : '暂缓'}
                {record.gate.reasons.length ? ` · ${record.gate.reasons.join('；')}` : ''}
                {record.gate.checked_at ? ` · ${formatSessionTime(record.gate.checked_at)}` : ''}
              </p>
              {record.terminal_reason ? <p>结果说明：{record.terminal_reason}</p> : null}
            </li>
          );
        })}
      </ul>
    </details>
  );
}
