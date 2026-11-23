/**
 * 「允许晚点回」出现在哪些会话里的判定。
 *
 * 这条判定的成本不对称：**多显示**会摆出一个注定 422 的开关（用户点了才知道
 * 不行），**少显示**会让 OneBot 私聊用户找不到功能。所以每条反例都要有对应的
 * 正例，且正反例只在**一个**维度上不同——否则绿了也不知道是哪一维在起作用。
 *
 * 历史教训（2026-11-24 真实浏览器验收失败）：判定曾经按窗口键的实例段是否
 * 以 `onebot-` 开头来猜渠道，于是 `fixture-bridge:private:100200303` 这种
 * 自定义 `LING_ONEBOT_INSTANCE_ID` 的私聊**不显示**开关，而服务端对它是 200。
 * 下面「自定义命名空间」那组正例就是钉这条的。
 */
import { describe, expect, it } from 'vitest';
import { supportsOneBotDeferred } from './components/SessionSidebar';

/** `channel` 是契约里的协议值（web / onebot / qqbot），不是实例命名空间。 */
const base = {
  session_id: 's',
  window_id: 's',
  subject_id: 's',
  title: 'onebot-default:private:100200300',
  channel: 'onebot',
  peer_id: '100200300',
};

describe('supportsOneBotDeferred', () => {
  it('默认命名空间的 OneBot 私聊：显示', () => {
    expect(supportsOneBotDeferred(base)).toBe(true);
  });

  it('自定义实例命名空间的 OneBot 私聊：显示（验收失败的那条）', () => {
    expect(supportsOneBotDeferred({
      ...base, title: 'fixture-bridge:private:100200303', peer_id: '100200303',
    })).toBe(true);
  });

  it('实例段只是一个普通词（不带 onebot- 前缀）：显示', () => {
    expect(supportsOneBotDeferred({
      ...base, title: 'default:private:100200303', peer_id: '100200303',
    })).toBe(true);
  });

  it('同一渠道的群聊：不显示（与正例只差 kind）', () => {
    expect(supportsOneBotDeferred({
      ...base, title: 'onebot-default:group:100200400', peer_id: '100200400',
    })).toBe(false);
  });

  it('官方 QQ 机器人的私聊：不显示（与正例只差渠道）', () => {
    expect(supportsOneBotDeferred({
      ...base, title: 'qqbot-main:private:100200301', channel: 'qqbot', peer_id: '100200301',
    })).toBe(false);
  });

  it('网页会话：不显示，哪怕标题恰好长得像窗口键', () => {
    expect(supportsOneBotDeferred({
      ...base, title: 'onebot-default:private:100200300', channel: 'web', peer_id: null,
    })).toBe(false);
  });

  it('拆不出窗口键的旧渠道窗口：不猜是私聊，按不显示处理', () => {
    expect(supportsOneBotDeferred({
      ...base, title: '被改过的标题', peer_id: null,
    })).toBe(false);
  });

  it('旧服务端没有 channel 字段：无法确认渠道，不显示', () => {
    const { channel: _omitted, ...legacy } = base;
    expect(supportsOneBotDeferred(legacy)).toBe(false);
  });

  it('title 不可用时回退到 window_id，判定仍然成立', () => {
    expect(supportsOneBotDeferred({
      ...base, title: '', window_id: 'fixture-bridge:private:100200300',
    })).toBe(true);
  });
});
