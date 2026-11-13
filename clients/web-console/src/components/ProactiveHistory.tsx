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

function outcome(record: ProactiveRecord): string {
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
    default: return '发送结果未知';
  }
}

export function ProactiveHistory({ records }: { records: ProactiveRecord[] }) {
  if (!records.length) return <p className="page__hint">还没有主动联系记录。</p>;
  return (
    <details className="proactive-history" data-testid="proactive-history">
      <summary>最近的主动联系记录（{records.length}，最多 50 条）</summary>
      <p className="page__hint">检查结果是当时的记录，当前条件可能已经改变。</p>
      <ul className="proactive-history__list">
        {records.map((record) => (
          <li className="proactive-history__item" key={record.opportunity_id}>
            <div className="proactive-history__heading">
              <strong>{outcome(record)}</strong>
              <time dateTime={record.created_at}>{formatSessionTime(record.created_at)}</time>
            </div>
            <p>来源：{record.has_source ? (SOURCES[record.reason_code ?? ''] ?? '已关联来源') : '未记录'}</p>
            <p>缘由：{record.reason}</p>
            <p>
              上次检查：{record.gate.allowed === null ? '没有记录' : record.gate.allowed ? '允许继续' : '暂缓'}
              {record.gate.reasons.length ? ` · ${record.gate.reasons.join('；')}` : ''}
              {record.gate.checked_at ? ` · ${formatSessionTime(record.gate.checked_at)}` : ''}
            </p>
            {record.terminal_reason ? <p>结果说明：{record.terminal_reason}</p> : null}
          </li>
        ))}
      </ul>
    </details>
  );
}
