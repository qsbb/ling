import type { ProactiveOpportunity } from '../types';

export function ProactiveDraft({ item }: { item: ProactiveOpportunity }) {
  if (item.draft_text) return <span data-testid="proactive-draft">「{item.draft_text}」{item.draft_origin === 'unknown' ? '（草稿来源未知）' : ''}</span>;
  let note: string;
  if (item.draft_state === 'uncertain') {
    note = '开场白生成结果暂未确认';
  } else if (item.draft_state === 'preparing' || item.draft_state === 'generating') {
    note = '正在准备开场白';
  } else if (item.draft_available === true) {
    note = item.output_modality === 'voice' ? '语音开场白已生成，正文已隐藏' : '开场白为空';
  } else if (item.draft_available === false || item.draft_text === null) {
    note = '开场白还没生成';
  } else {
    // 旧服务端没有 draft_available，空串不能推断是隐藏还是没生成。
    note = '开场白状态未知';
  }
  return <span className="life-list__note">（{note}）</span>;
}
