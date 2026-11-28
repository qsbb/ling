import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { classifyProactiveOutcome, ProactiveHistory } from './components/ProactiveHistory';
import { ProactiveDraft } from './components/ProactiveDraft';
import type { ProactiveOpportunity, ProactiveRecord } from './types';

const base: ProactiveRecord = {
  opportunity_id: 'candidate', kind: 'check_in', reason: '合成来源', reason_code: 'intent_due',
  has_source: true, state: 'candidate', created_at: '2030-06-01T04:00:00Z', sent_at: null,
  gate: { allowed: false, reasons: ['勿扰时段'], checked_at: '2030-06-01T04:00:00Z' },
  delivery_state: null, web_saved: false, terminal_reason: null, receipt: null,
};

describe('主动联系记录', () => {
  it('区分排队、尝试发送、已发送、回执和会话内的分享', () => {
    const receipt = { channel: 'fake', observed_at: base.created_at };
    expect(classifyProactiveOutcome({ ...base, state: 'dispatching', receipt: { ...receipt, state: 'queued' } }))
      .toEqual({ tone: 'pending', badge: '等待投递' });
    expect(classifyProactiveOutcome({ ...base, state: 'dispatching' }))
      .toEqual({ tone: 'pending', badge: '投递中' });
    expect(classifyProactiveOutcome({ ...base, state: 'sent', receipt: { ...receipt, state: 'sent' } }))
      .toEqual({ tone: 'sent_unconfirmed', badge: '已发送待确认' });
    expect(classifyProactiveOutcome({ ...base, state: 'sent', receipt: { ...receipt, state: 'delivered' } }).badge)
      .toBe('确认送达');
    expect(classifyProactiveOutcome({ ...base, state: 'continued' }).badge).toBe('已在对话中分享');
  });

  it('区分检查、等待和真实成功或失败回执', () => {
    const html = renderToStaticMarkup(<ProactiveHistory records={[
      base,
      { ...base, opportunity_id: 'success', reason: '合成成功', state: 'sent',
        gate: { allowed: true, reasons: [], checked_at: base.created_at },
        receipt: { state: 'delivered', channel: 'fake', observed_at: base.created_at } },
      { ...base, opportunity_id: 'failure', reason: '合成失败', state: 'failed',
        receipt: { state: 'failed', channel: 'fake', observed_at: base.created_at } },
    ]} />);
    expect(html).toContain('待处理，尚无投递回执');
    expect(html).toContain('上次检查：暂缓');
    expect(html).toContain('勿扰时段');
    expect(html).toContain('上次检查：允许继续');
    expect(html).toContain('平台回执：已送达');
    expect(html).toContain('平台回执：发送失败');
    expect(html).toContain('她之前计划稍后提起');
  });

  it('旧记录缺少检查和回执时不伪造回执', () => {
    const html = renderToStaticMarkup(<ProactiveHistory records={[{
      ...base, state: 'sent', has_source: false,
      gate: { allowed: null, reasons: [], checked_at: null },
    }]} />);
    expect(html).toContain('来源：未记录');
    expect(html).toContain('上次检查：没有记录');
    expect(html).toContain('记录为已发送，未找到独立回执');
    expect(html).not.toContain('平台回执：已送达');
  });

  it('区分允许但无回执和真实送达', () => {
    const waiting: ProactiveRecord = {
      ...base, gate: { allowed: true, reasons: [], checked_at: base.created_at },
    };
    const waitingHtml = renderToStaticMarkup(<ProactiveHistory records={[waiting]} />);
    const sentHtml = renderToStaticMarkup(<ProactiveHistory records={[{
      ...waiting, state: 'sent',
      receipt: { state: 'delivered', channel: 'fake', observed_at: base.created_at },
    }]} />);
    expect(waitingHtml).toContain('上次检查：允许继续');
    expect(waitingHtml).toContain('待处理，尚无投递回执');
    expect(waitingHtml).not.toContain('平台回执：已送达');
    expect(sentHtml).toContain('平台回执：已送达');
  });

  it('区分已隐藏语音草稿、未生成以及旧接口未知状态', () => {
    const item: ProactiveOpportunity = {
      opportunity_id: 'voice', kind: 'check_in', reason: '合成来源', expires_at: base.created_at,
      output_modality: 'voice', draft_text: '', draft_available: true,
    };
    const hidden = renderToStaticMarkup(<ProactiveDraft item={item} />);
    const missing = renderToStaticMarkup(<ProactiveDraft item={{ ...item, draft_available: false }} />);
    const old = renderToStaticMarkup(<ProactiveDraft item={{ ...item, draft_available: undefined }} />);
    expect(hidden).toContain('语音开场白已生成，正文已隐藏');
    expect(hidden).not.toContain('还没生成');
    expect(missing).toContain('开场白还没生成');
    expect(old).toContain('开场白状态未知');
  });

  it('展示草稿结果未知与旧草稿来源未记录', () => {
    const item: ProactiveOpportunity = {
      opportunity_id: 'candidate', kind: 'check_in', reason: '合成来源', expires_at: base.created_at,
      draft_text: null, draft_available: false, draft_state: 'uncertain',
    };
    expect(renderToStaticMarkup(<ProactiveDraft item={item} />)).toContain('开场白生成结果暂未确认');
    expect(renderToStaticMarkup(<ProactiveDraft item={{ ...item, draft_text: '你好', draft_origin: 'unknown' }} />))
      .toContain('草稿来源未知');
    const history = renderToStaticMarkup(<ProactiveHistory records={[{ ...base, draft_origin: 'unknown' }]} />);
    expect(history).toContain('草稿来源：未记录');
  });
});
