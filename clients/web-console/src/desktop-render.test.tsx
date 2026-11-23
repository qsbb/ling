/**
 * 桌面页各块在**真实数据形状**下的渲染断言（SSR，`react-dom/server`）。
 *
 * 为什么与 `desktop.test.ts` 分开：那边测判断（能不能提交、走到第几步），
 * 这边测"同一份判断画出来是什么"——尤其是有几条要求**只体现在文案里**：
 *
 * - 成功必须是「已写入并从记事本回读」，并说清此刻是**待保存**内容；
 * - 令牌只在配对响应里出现，页面上其余地方只露掩码；
 * - 暂停/恢复、取消、在途时的其它提交反馈都不能只说"按钮变灰"；
 * - 授权 `queued` 必须与"已写入"区分开。
 *
 * 载荷形状取自 `contracts/desktop.py`（`DesktopTask` / `DesktopStatus` /
 * `DesktopActivityGrant`），不是手写的近似结构。
 */
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  DevicePanel,
  GrantPanel,
  HistoryPanel,
  PairingPanel,
  StepTrack,
  SubmitPanel,
  TaskRow,
  ErrorBanner,
} from './components/DesktopPage';
import type { DesktopActivityGrant, DesktopStatus, DesktopTask } from './desktop';

const NOOP = () => undefined;
const nowAsync = async () => undefined;

function task(patch: Partial<DesktopTask> = {}): DesktopTask {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    kind: 'notepad_write',
    text: '第一行\n  两个空格  😀',
    state: 'queued',
    step: null,
    reason: null,
    created_at: '2026-11-24T01:00:00Z',
    started_at: null,
    finished_at: null,
    source: 'manual',
    source_segment_id: null,
    ...patch,
  };
}

function status(patch: Partial<DesktopStatus> = {}): DesktopStatus {
  return {
    device: { name: '客厅 Windows', state: 'ready', last_seen_at: '2026-11-24T01:00:00Z' },
    paused: false,
    tasks: [],
    ...patch,
  };
}

function deviceHtml(s: DesktopStatus): string {
  return renderToStaticMarkup(
    h(DevicePanel, { status: s, busy: false, onResume: NOOP, onPause: NOOP }),
  );
}

describe('设备块：五种状态各有恰当的下一步', () => {
  it('未配对：说"还没有配对"，给出那条唯一的下一步，且不给注定 409 的开关', () => {
    const html = deviceHtml({ device: null, paused: true, tasks: [] });
    expect(html).toContain('还没有配对任何电脑');
    expect(html).toContain('生成令牌');
    // 未配对时既不该有"恢复接收"（会 409 not_paired），也不该有"暂停接收"——
    // paused=true 在这里的含义是"还没配过"，不是"所有者暂停了"。
    expect(html).not.toContain('恢复接收');
    expect(html).not.toContain('暂停接收');
    expect(html).not.toContain('data-testid="desktop-receive"');
  });

  it('"不能提交"的原因只出现一次（在提交区，紧贴被禁用的按钮）', () => {
    // 同一句话在两块里各说一遍，会让人以为是两个不同的原因；结论只留在提交区。
    const s = status({ device: { name: 'x', state: 'unavailable', last_seen_at: '2026-11-24T01:00:00Z' } });
    const device = deviceHtml(s);
    const submit = renderToStaticMarkup(
      h(SubmitPanel, { status: s, busy: false, onSubmit: async () => true, onCancel: NOOP, cancelBusy: false }),
    );
    expect(submit).toContain('锁屏');
    expect(device).not.toContain('锁屏');
  });

  it('可以提交时，设备块给出那唯一的正向结论', () => {
    expect(deviceHtml(status())).toContain('可以接一条测试任务');
  });

  it('就绪且允许：给暂停按钮，结论说"可以接一条测试任务"', () => {
    const html = deviceHtml(status());
    expect(html).toContain('已允许');
    expect(html).toContain('暂停接收');
    expect(html).toContain('可以接一条测试任务');
    expect(html).toContain('已连接，桌面可用');
  });

  it('就绪但暂停：给恢复按钮，且它可用', () => {
    const html = deviceHtml(status({ paused: true }));
    expect(html).toContain('已暂停');
    expect(html).toContain('data-testid="desktop-resume"');
    expect(html).not.toContain('disabled');
  });

  it('离线且暂停：恢复按钮存在但被禁用（后端会 409 desktop_not_ready）', () => {
    const html = deviceHtml(
      status({ device: { name: '客厅 Windows', state: 'offline', last_seen_at: null }, paused: true }),
    );
    expect(html).toContain('data-testid="desktop-resume"');
    expect(html).toContain('disabled');
    expect(html).toContain('离线');
  });

  it('在线但桌面不可用：状态徽标说清"桌面不可用"，而不是"离线"', () => {
    const html = deviceHtml(
      status({ device: { name: '客厅 Windows', state: 'unavailable', last_seen_at: '2026-11-24T01:00:00Z' } }),
    );
    expect(html).toContain('伴生端在线，桌面不可用');
    expect(html).not.toContain('>离线<');
    // "回 Windows 解锁"那句在提交区（结论只说一处），这里只要求徽标区分得开。
  });
});

describe('配对块', () => {
  it('未配对时只有生成入口，不显示"重新配对/解除配对"', () => {
    const html = renderToStaticMarkup(
      h(PairingPanel, {
        status: { device: null, paused: true, tasks: [] },
        token: null,
        busy: false,
        url: 'https://ling.example',
        onPair: nowAsync,
        onUnpair: nowAsync,
        onTokenDone: NOOP,
      }),
    );
    expect(html).toContain('生成配对令牌');
    expect(html).not.toContain('data-testid="desktop-repair-toggle"');
    expect(html).not.toContain('data-testid="desktop-unpair"');
  });

  it('有令牌时：给出令牌、启动命令、粘贴位置与三个步骤', () => {
    const token = 'AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABCDEFG';
    const html = renderToStaticMarkup(
      h(PairingPanel, {
        status: { device: { name: '客厅 Windows', state: 'offline', last_seen_at: null }, paused: true, tasks: [] },
        token,
        busy: false,
        url: 'https://ling.example',
        onPair: nowAsync,
        onUnpair: nowAsync,
        onTokenDone: NOOP,
      }),
    );
    // 令牌本体只在这一次交互里出现（复制按钮要用的就是它）
    expect(html).toContain(token);
    // 命令里有地址、没有令牌；粘贴位置点明是"配对令牌"提示
    expect(html).toContain('py -m connectors.desktop --url https://ling.example');
    expect(html).toContain('LING_DESKTOP_TOKEN');
    expect(html).toContain('配对令牌');
    // 掩码提示：页面上不止一处 token，其余地方只能是掩码
    expect(html).toContain('仅显示 AbCd…DEFG');
  });

  it('已配对但无令牌（刷新之后）：说明令牌已不可见，并给重新配对入口', () => {
    const html = renderToStaticMarkup(
      h(PairingPanel, {
        status: status(),
        token: null,
        busy: false,
        url: 'https://ling.example',
        onPair: nowAsync,
        onUnpair: nowAsync,
        onTokenDone: NOOP,
      }),
    );
    expect(html).toContain('data-testid="desktop-repair-toggle"');
    expect(html).toContain('data-testid="desktop-unpair"');
    expect(html).not.toContain('data-testid="desktop-token-value"');
    // 重新配对会中断在途任务的后果，写在"点它之前"看不见的地方是不够的：
    // 这里断言常驻那句提示在（展开后的红色警告由交互触发，SSR 看不到）。
    expect(html).toContain('中断');
  });
});

describe('任务块：状态、原因、步进与结果', () => {
  it('成功：标题是「已写入并从记事本回读」，并说明是待保存内容', () => {
    const html = renderToStaticMarkup(
      h(TaskRow, { task: task({ state: 'succeeded', step: 'verified' }) }),
    );
    expect(html).toContain('已写入并从记事本回读');
    expect(html).toContain('待保存');
  });

  it('成功那条不再显示"取消"', () => {
    const html = renderToStaticMarkup(
      h(TaskRow, { task: task({ state: 'succeeded', step: 'verified' }), onCancel: NOOP }),
    );
    expect(html).not.toContain('data-testid="desktop-task-cancel"');
  });

  it('在途任务显示取消入口', () => {
    const html = renderToStaticMarkup(
      h(TaskRow, { task: task({ state: 'running', step: 'written' }), onCancel: NOOP }),
    );
    expect(html).toContain('data-testid="desktop-task-cancel"');
    // 当前步是回读校验
    expect(html).toContain('回读校验');
  });

  it('中断：显示原因机器值与一句下一步', () => {
    const html = renderToStaticMarkup(
      h(TaskRow, { task: task({ state: 'interrupted', step: 'written', reason: 'lease_expired' }) }),
    );
    expect(html).toContain('lease_expired');
    expect(html).toContain('不会重放');
  });

  it('生命周期的任务带「来自授权」标记，手动的没有', () => {
    const life = renderToStaticMarkup(
      h(TaskRow, { task: task({ state: 'succeeded', step: 'verified', source: 'life' }) }),
    );
    const manual = renderToStaticMarkup(
      h(TaskRow, { task: task({ state: 'succeeded', step: 'verified' }) }),
    );
    expect(life).toContain('来自授权');
    expect(manual).not.toContain('来自授权');
  });

  it('提交的原文逐字保留：换行、连续空格、emoji 都在 DOM 文本里', () => {
    const text = '第一行\n  两个空格  😀';
    const html = renderToStaticMarkup(h(TaskRow, { task: task({ text }) }));
    expect(html).toContain('第一行\n  两个空格  😀');
  });

  it('四步进度：当前格只有一个', () => {
    const html = renderToStaticMarkup(h(StepTrack, { task: task({ state: 'running', step: 'opened' }) }));
    expect((html.match(/data-current="true"/g) ?? []).length).toBe(1);
    expect((html.match(/data-done="true"/g) ?? []).length).toBe(2);
  });

  it('已结束的任务没有"正在做"的格子', () => {
    const html = renderToStaticMarkup(
      h(StepTrack, { task: task({ state: 'succeeded', step: 'verified' }) }),
    );
    expect(html).not.toContain('data-current="true"');
    expect((html.match(/data-done="true"/g) ?? []).length).toBe(4);
  });
});

describe('提交块', () => {
  function submitHtml(s: DesktopStatus, busy = false): string {
    return renderToStaticMarkup(
      h(SubmitPanel, { status: s, busy, onSubmit: async () => true, onCancel: NOOP, cancelBusy: false }),
    );
  }

  it('就绪时按钮可用，没有"不能提交"的说明', () => {
    const html = submitHtml(status());
    expect(html).toContain('data-testid="desktop-submit-button"');
    expect(html).not.toContain('data-testid="desktop-submit-blocked"');
  });

  it('在途时按钮禁用，并给出"其他提交会怎样"的明确反馈', () => {
    const s = status({ tasks: [task({ state: 'running', step: 'opened' })] });
    const html = submitHtml(s);
    expect(html).toContain('disabled');
    const blocked = html.slice(html.indexOf('data-testid="desktop-submit-blocked"'));
    expect(blocked).toContain('上一条还在等待或执行');
    // 同一块里给"取消这条任务"，不必滚回去找
    expect(html).toContain('data-testid="desktop-cancel-active"');
  });

  it('未就绪时禁用，并说明原因（未配对 / 离线 / 锁屏 各不同）', () => {
    const offline = submitHtml(status({ device: { name: 'x', state: 'offline', last_seen_at: null } }));
    const locked = submitHtml(status({ device: { name: 'x', state: 'unavailable', last_seen_at: '2026-11-24T01:00:00Z' } }));
    expect(offline).toContain('伴生端');
    expect(locked).toContain('锁屏');
    expect(offline).not.toBe(locked);
  });

  it('textarea 存在且带长度计数（上限 2000）', () => {
    const html = submitHtml(status());
    expect(html).toContain('<textarea');
    expect(html).toContain('0 / 2000');
  });
});

describe('历史块', () => {
  it('空历史说"还没有任务"，并预告成功长什么样', () => {
    const html = renderToStaticMarkup(h(HistoryPanel, { tasks: [] }));
    expect(html).toContain('还没有任务');
    expect(html).toContain('已写入并从记事本回读');
  });

  it('多条时按后端顺序渲染（新的在前由后端决定，界面不改）', () => {
    const html = renderToStaticMarkup(
      h(HistoryPanel, {
        tasks: [
          task({ id: 'a', text: '最新', state: 'succeeded', step: 'verified' }),
          task({ id: 'b', text: '较早', state: 'cancelled', reason: 'owner_cancelled' }),
        ],
      }),
    );
    expect(html.indexOf('最新')).toBeLessThan(html.indexOf('较早'));
  });
});

describe('授权块', () => {
  const grant = (patch: Partial<DesktopActivityGrant> = {}): DesktopActivityGrant => ({
    request_id: '22222222-2222-4222-8222-222222222222',
    text: '今天想写三行',
    start_at: '2026-11-24T01:00:00Z',
    end_at: '2026-11-24T02:00:00Z',
    decision: 'pending',
    task_id: null,
    ...patch,
  });

  function grantHtml(g: DesktopActivityGrant | null, s = status()): string {
    return renderToStaticMarkup(
      h(GrantPanel, {
        grant: g,
        status: s,
        busy: false,
        onSave: async () => true,
        onRevoke: async () => undefined,
      }),
    );
  }

  it('未授权：有开始/截止输入与原文输入，说明"写作或休息"由模型决定', () => {
    const html = grantHtml(null);
    expect(html).toContain('data-testid="desktop-grant-start"');
    expect(html).toContain('data-testid="desktop-grant-end"');
    expect(html).toContain('data-testid="desktop-grant-text"');
    expect(html).toContain('写作');
    expect(html).toContain('休息');
  });

  it('queued：明确"入队不等于写成功"，并指向任务列表', () => {
    const html = grantHtml(grant({ decision: 'queued', task_id: '33333333-3333-4333-8333-333333333333' }));
    expect(html).toContain('不等于');
    expect(html).toContain('来自授权');
  });

  it('rest 与 unavailable 显示为两种不同结果', () => {
    const rest = grantHtml(grant({ decision: 'rest' }));
    const unavailable = grantHtml(grant({ decision: 'unavailable' }));
    expect(rest).toContain('休息');
    expect(unavailable).toContain('无法安排');
    expect(rest).not.toBe(unavailable);
  });

  it('撤销后不再提供撤销入口', () => {
    const html = grantHtml(grant({ decision: 'revoked' }));
    expect(html).not.toContain('data-testid="desktop-grant-revoke"');
  });

  it('未配对时不提供保存（配对后才能授权）', () => {
    const html = grantHtml(null, { device: null, paused: true, tasks: [] });
    expect(html).toContain('配对一台电脑之后');
    expect(html).not.toContain('data-testid="desktop-grant-save"');
  });
});

describe('错误条', () => {
  it('409 的 detail 会显示出来（排查用），不吞掉', () => {
    const html = renderToStaticMarkup(
      h(ErrorBanner, {
        error: {
          reason: '已有一条桌面任务正在等待或执行',
          hint: '上一条还在等待或执行。等它结束，或先取消它，再提交新的。',
          retriable: false,
          code: 'conflict',
          detail: 'task_in_progress',
          traceId: null,
        },
      }),
    );
    expect(html).toContain('task_in_progress');
    expect(html).toContain('取消');
  });
});
