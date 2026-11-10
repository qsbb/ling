/**
 * 延后回复的**文案**断言：钳过必须说出来。
 *
 * ## 这个文件存在的唯一理由
 *
 * 后端把 `requested_seconds` / `applied_seconds` / `clamped` 三个数都发过来了，
 * 但前端如果只显示 `applied_seconds`，用户看到的仍然是"她等了 5 分钟"——
 * 而她想等的是 16 分钟。**数据传到了不等于事实传到了。**
 *
 * 这一条与"不许把槽位忙包装成人格动作"是同一条底线：系统限制必须能被
 * 用户看见，不能被伪装成她的选择。
 *
 * ## 为什么直接测函数而不是渲染 DOM
 *
 * 文案是纯函数（`describeDeferral`），直接断言它比点 DOM 找文本更稳、更快，
 * 也更能表达"这句话为什么这么说"。渲染层的接入由 `MessageList` 已有的
 * `silenceReason` 通道保证（同一条路径已有用例覆盖）。
 */
import { describe, expect, it } from 'vitest';

import { describeDeferral } from './useChat';
import type { TurnDeferredData } from './types';

function deferred(patch: Partial<TurnDeferredData> = {}): TurnDeferredData {
  return { turn_id: 'turn_x', ...patch };
}

describe('describeDeferral', () => {
  it('没被钳时说实际时长', () => {
    const text = describeDeferral(
      deferred({ requested_seconds: 90, applied_seconds: 90, clamped: false }),
    );
    expect(text).toContain('等会儿再回');
    expect(text).toContain('1.5 分钟');
    expect(text).not.toContain('本来想等');
  });

  it('被钳时**必须**说出她本来想等多久', () => {
    const text = describeDeferral(
      deferred({ requested_seconds: 999, applied_seconds: 300, clamped: true }),
    );
    // 用户必须能看到"她要求过 16.7 分钟，系统只允许 5 分钟"。
    expect(text).toContain('本来想等');
    expect(text).toContain('16.7 分钟');
    expect(text).toContain('5 分钟');
  });

  it('clamped 为真但她要的比用的短时，不编造"本来想等更久"', () => {
    // 边界：clamped 与服务端一致，但不该反过来声称她想要更长。
    const text = describeDeferral(
      deferred({ requested_seconds: 30, applied_seconds: 300, clamped: true }),
    );
    expect(text).not.toContain('本来想等');
  });

  it('秒数缺失或为 0 时给一句可读的兜底，不显示 NaN', () => {
    for (const payload of [deferred(), deferred({ applied_seconds: 0 })]) {
      const text = describeDeferral(payload);
      expect(text).not.toContain('NaN');
      expect(text).toContain('一会儿');
    }
  });

  it('小于一分钟说秒，不硬凑成 0.x 分钟', () => {
    const text = describeDeferral(
      deferred({ requested_seconds: 45, applied_seconds: 45, clamped: false }),
    );
    expect(text).toContain('45 秒');
    expect(text).not.toContain('分钟');
  });
});
