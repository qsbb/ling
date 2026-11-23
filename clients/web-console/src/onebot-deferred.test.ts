/**
 * 「允许晚点回」出现在哪些会话里的判定。
 *
 * 这条判定的成本不对称：**多显示**会摆出一个注定 422 的开关（用户点了才知道
 * 不行），**少显示**会让 OneBot 私聊用户找不到功能。所以每条反例都要有对应的
 * 正例，且正反例只在**一个**维度上不同——否则绿了也不知道是哪一维在起作用。
 */
import { describe, expect, it } from 'vitest';
import { supportsOneBotDeferred } from './components/SessionSidebar';

const base = {
  session_id: 's',
  window_id: 's',
  subject_id: 's',
  title: 'onebot-default:private:100200300',
  channel: 'onebot-default',
  peer_id: '100200300',
};

describe('supportsOneBotDeferred', () => {
  it('OneBot 私聊：显示', () => {
    expect(supportsOneBotDeferred(base)).toBe(true);
  });

  it('同一实例的群聊：不显示', () => {
    expect(supportsOneBotDeferred({
      ...base, title: 'onebot-default:group:100200400', peer_id: '100200400',
    })).toBe(false);
  });

  it('官方 QQ 机器人的私聊：不显示（与 OneBot 私聊只差实例前缀）', () => {
    expect(supportsOneBotDeferred({
      ...base, title: 'qqbot-main:private:100200301', channel: 'qqbot-main', peer_id: '100200301',
    })).toBe(false);
  });

  it('网页会话：不显示，哪怕标题恰好长得像窗口键', () => {
    expect(supportsOneBotDeferred({
      ...base, title: 'onebot-default:private:100200300', channel: 'web', peer_id: null,
    })).toBe(false);
  });

  it('拆不出窗口键的旧渠道窗口：不猜，按不显示处理', () => {
    expect(supportsOneBotDeferred({
      ...base, title: '被改过的标题', channel: 'onebot-default',
    })).toBe(false);
  });

  it('title 不可用时回退到 window_id，判定仍然成立', () => {
    expect(supportsOneBotDeferred({
      ...base, title: '', window_id: 'onebot-default:private:100200300',
    })).toBe(true);
  });
});
