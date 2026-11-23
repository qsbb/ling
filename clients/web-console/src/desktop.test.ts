/**
 * Windows 桌面任务的语义层测试。
 *
 * 这里测的是"判断"而不是"渲染"：`capabilityOf` 的组合、`stepsOf` 的步进、
 * `outcomeOf` 的文案、`conflictHint` 的翻译。渲染层由
 * `desktop-render.test.tsx`（SSR）与 `desktop.browser.test.tsx`（真 Chromium）覆盖。
 *
 * 选取输入的方法遵循仓库约定：**一个断言能证明什么，取决于错误实现会给出什么值**。
 * 所以设备三态各来一条、`paused × ready` 的四种组合各来一条，并且刻意让
 * "显式值"与"默认推断"给出**相反**结果的输入存在（例如 `unavailable + paused`
 * 与 `offline + paused` 的 blocker 文案不同，只断言"不能提交"是证明不了区别的）。
 */
import { describe, expect, it } from 'vitest';
import {
  activeTaskOf,
  capabilityOf,
  checkGrantWindow,
  checkNote,
  conflictHint,
  formatRange,
  grantIsLive,
  grantView,
  isoToLocalInput,
  launchCommand,
  localInputToIso,
  maskToken,
  noteLength,
  outcomeOf,
  reasonText,
  sinceText,
  stepsOf,
} from './desktop';
import type { DesktopActivityGrant, DesktopDevice, DesktopStatus, DesktopTask } from './desktop';

function device(state: DesktopDevice['state'], name = '客厅 Windows'): DesktopDevice {
  return { name, state, last_seen_at: state === 'offline' ? null : '2026-11-24T01:00:00Z' };
}

function status(patch: Partial<DesktopStatus> = {}): DesktopStatus {
  return { device: device('ready'), paused: false, tasks: [], ...patch };
}

function task(patch: Partial<DesktopTask> = {}): DesktopTask {
  return {
    id: '00000000-0000-4000-8000-000000000001',
    kind: 'notepad_write',
    text: '第一行\n  两个空格  ',
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

function grant(patch: Partial<DesktopActivityGrant> = {}): DesktopActivityGrant {
  return {
    request_id: '00000000-0000-4000-8000-00000000000a',
    text: '今天想写三行',
    start_at: '2026-11-24T01:00:00Z',
    end_at: '2026-11-24T02:00:00Z',
    decision: 'pending',
    task_id: null,
    ...patch,
  };
}

describe('文本校验：只拦后端一定会拒的，其余逐字保留', () => {
  it('空与全空白被拒，且提示指向"记事本里要有可回读的文字"', () => {
    expect(checkNote('')).toMatchObject({ ok: false });
    expect(checkNote('   \n\t  ')).toMatchObject({ ok: false });
    expect(checkNote('   \n\t  ')).toEqual(expect.objectContaining({ message: expect.stringContaining('回读') }));
  });

  it('换行、连续空格、中文、emoji 都通过（不 trim、不折叠）', () => {
    expect(checkNote('第一行\n  两格  😀')).toEqual({ ok: true });
  });

  it('长度按码点算：2000 个 emoji 通过，2001 个被拒', () => {
    // `'😀'.length === 2`（UTF-16 码元），按 length 算会把这 2000 个判成 4000。
    expect(noteLength('😀'.repeat(2000))).toBe(2000);
    expect(checkNote('😀'.repeat(2000))).toEqual({ ok: true });
    expect(checkNote('😀'.repeat(2001))).toMatchObject({ ok: false });
  });

  it('除换行/制表符外的控制字符被拒', () => {
    expect(checkNote('a\u0007b')).toMatchObject({ ok: false });
    expect(checkNote('a\tb\nc')).toEqual({ ok: true });
  });
});

describe('能力判定：设备三态 × 暂停（四种 paused×ready 组合都要各有一条）', () => {
  it('未配对：不能提交也不能恢复，下一步是配对', () => {
    const cap = capabilityOf(status({ device: null }));
    expect(cap).toMatchObject({ canSubmit: false, canResume: false, connection: '未配对' });
    expect(cap.blocker).toContain('配对');
  });

  it('status 为 null（首拍未回来）按未配对处理，不抛异常', () => {
    expect(capabilityOf(null).canSubmit).toBe(false);
  });

  it('ready + 未暂停：可以提交', () => {
    expect(capabilityOf(status())).toMatchObject({ canSubmit: true, canResume: false, blocker: null });
  });

  it('ready + 暂停：不能提交，但可以恢复', () => {
    const cap = capabilityOf(status({ paused: true }));
    expect(cap).toMatchObject({ canSubmit: false, canResume: true });
    expect(cap.blocker).toContain('恢复');
  });

  it('offline + 暂停：不能恢复（后端会 409 desktop_not_ready），下一步是确认伴生端在跑', () => {
    const cap = capabilityOf(status({ device: device('offline'), paused: true }));
    expect(cap).toMatchObject({ canSubmit: false, canResume: false });
    expect(cap.blocker).toContain('伴生端');
    // 断线不是锁屏：不要把人指去解锁屏幕。
    expect(cap.blocker).not.toContain('锁屏');
  });

  it('unavailable + 暂停：与 offline 一样不能恢复，但 blocker 必须指向"桌面不可用"', () => {
    // 这一条与上一条**判定值相同**（都 false），所以只断言 false 是证明不了区别的；
    // 文案必须能区分"离线"和"在线但锁屏"——否则用户会去重启一个正在运行的伴生端。
    const unavailable = capabilityOf(status({ device: device('unavailable'), paused: true }));
    const offline = capabilityOf(status({ device: device('offline'), paused: true }));
    expect(unavailable.canResume).toBe(false);
    expect(unavailable.blocker).not.toBe(offline.blocker);
    expect(unavailable.blocker).toContain('锁屏');
    expect(unavailable.blocker).not.toContain('离线');
  });

  it('unavailable + 未暂停：不能提交，提示直奔"锁屏/切窗口"', () => {
    const cap = capabilityOf(status({ device: device('unavailable') }));
    expect(cap.canSubmit).toBe(false);
    expect(cap.blocker).toContain('锁屏');
  });

  it('offline + 未暂停：不能提交，提示指向"伴生端仍在运行"而不是锁屏', () => {
    const cap = capabilityOf(status({ device: device('offline') }));
    expect(cap.canSubmit).toBe(false);
    expect(cap.blocker).toContain('伴生端');
    expect(cap.blocker).not.toContain('锁屏');
  });
});

describe('在途任务', () => {
  it('queued 与 running 算在途，其余都不算', () => {
    expect(activeTaskOf([task({ state: 'queued' })])?.state).toBe('queued');
    expect(activeTaskOf([task({ state: 'running' })])?.state).toBe('running');
    for (const state of ['succeeded', 'failed', 'interrupted', 'cancelled'] as const) {
      expect(activeTaskOf([task({ state })])).toBeNull();
    }
  });
});

describe('四步进度：跟着服务端已收到的回执走', () => {
  const keys = (t: DesktopTask) => stepsOf(t).map((s) => `${s.key}:${s.done ? 'done' : '-'}${s.current ? ':cur' : ''}`);

  it('step=null 的排队任务：第一格"等待领取"是当前步', () => {
    expect(keys(task({ state: 'queued', step: null }))).toEqual([
      'queued:-:cur', 'opened:-', 'written:-', 'verified:-',
    ]);
  });

  it('step=opened：领取与打开已回执，写入是当前步', () => {
    // 「当前步」= 下一份要等回执的动作：回执停在 opened，说明写入还没回执，
    // 所以"正在做"的是写入，而不是刚完成的打开。
    expect(keys(task({ state: 'running', step: 'opened' }))).toEqual([
      'queued:done', 'opened:done', 'written:-:cur', 'verified:-',
    ]);
  });

  it('step=written：回读是当前步', () => {
    expect(keys(task({ state: 'running', step: 'written' }))).toEqual([
      'queued:done', 'opened:done', 'written:done', 'verified:-:cur',
    ]);
  });

  it('step=verified 但状态 succeeded：四步全完成，没有当前步', () => {
    const steps = stepsOf(task({ state: 'succeeded', step: 'verified' }));
    expect(steps.every((s) => s.done)).toBe(true);
    expect(steps.some((s) => s.current)).toBe(false);
  });

  it('interrupted + written：进度停在写入已完成，不因为"中断"把回执抹掉', () => {
    // 真实组合（租期到期时文字已经写进编辑区）。错误实现常见两种：
    // 把进度清零（假装没发生），或把整条标成"打开失败"。
    const steps = stepsOf(task({ state: 'interrupted', step: 'written', reason: 'lease_expired' }));
    expect(steps.slice(0, 3).every((s) => s.done)).toBe(true);
    expect(steps[3]?.done).toBe(false);
    expect(steps.some((s) => s.current)).toBe(false);
  });

  it('失败的任务不显示"某一步正在做"', () => {
    expect(stepsOf(task({ state: 'failed', step: 'opened', reason: 'editor_error' })).some((s) => s.current)).toBe(false);
  });
});

describe('结果文案', () => {
  it('成功说的是"已写入并从记事本回读一致"，并且说明是待保存内容', () => {
    const out = outcomeOf(task({ state: 'succeeded', step: 'verified' }));
    expect(out.tone).toBe('ok');
    expect(out.title).toBe('已写入并从记事本回读一致');
    expect(out.detail).toContain('待保存');
  });

  it('回读不一致时给出具体差异方向，而不是泛泛的失败', () => {
    const out = outcomeOf(task({ state: 'failed', step: 'written', reason: 'readback_mismatch' }));
    expect(out.tone).toBe('error');
    expect(out.title).toContain('回读与提交内容不一致');
    expect(out.detail).toContain('输入法');
  });

  it('lease_expired 且已写入：提醒"文字可能已在编辑区，不会重放"', () => {
    const out = outcomeOf(task({ state: 'interrupted', step: 'written', reason: 'lease_expired' }));
    expect(out.tone).toBe('warn');
    expect(out.detail).toContain('不会重放');
  });

  it('owner_paused 的中断说明保持原结果、需要重新提交', () => {
    const out = outcomeOf(task({ state: 'interrupted', step: 'opened', reason: 'owner_paused' }));
    expect(out.detail).toContain('恢复接收后');
  });

  it('queue_expired：指向"确认终端仍在运行"', () => {
    const out = outcomeOf(task({ state: 'cancelled', reason: 'queue_expired' }));
    expect(out.title).toContain('排队超时');
    expect(out.detail).toContain('终端');
  });

  it('owner_cancelled 与 queue_expired 同属 cancelled，但两种说明必须不同', () => {
    // 同状态、不同原因：只断言 state 是证明不了提示正确与否的。
    const byOwner = outcomeOf(task({ state: 'cancelled', reason: 'owner_cancelled' }));
    const byExpiry = outcomeOf(task({ state: 'cancelled', reason: 'queue_expired' }));
    expect(byOwner.detail).not.toBe(byExpiry.detail);
  });

  it('未登记的原因原样显示，不被改写成"未知错误"', () => {
    expect(reasonText('some_future_reason')).toBe('some_future_reason');
    expect(reasonText(null)).toBe('未给出原因');
  });
});

describe('409 detail → 可行动提示', () => {
  it.each([
    ['not_paired', '配对'],
    ['paused', '恢复接收'],
    ['desktop_not_ready', '就绪'],
    ['task_in_progress', '取消'],
    ['request_id_conflict', '编号'],
    ['authorization_expired', '结束时间'],
  ])('%s 有自己的一句', (detail, word) => {
    expect(conflictHint(detail, '兜底')).toContain(word);
  });

  it('翻不出来的 detail 用调用方给的兜底句，而不是编一句', () => {
    expect(conflictHint('brand_new_detail', '兜底句')).toBe('兜底句');
    expect(conflictHint(null, '兜底句')).toBe('兜底句');
  });
});

describe('授权', () => {
  it('queued 明确说"入队不等于写成功"，并指向任务列表', () => {
    const view = grantView(grant({ decision: 'queued', task_id: '00000000-0000-4000-8000-0000000000ff' }));
    expect(view.detail).toContain('不等于');
    expect(view.taskId).toBe('00000000-0000-4000-8000-0000000000ff');
    expect(view.settled).toBe(false);
  });

  it('rest 与 unavailable 的说明不同（同为"没写"，原因不同）', () => {
    expect(grantView(grant({ decision: 'rest' })).detail).not.toBe(grantView(grant({ decision: 'unavailable' })).detail);
  });

  it('只有 pending/deciding/queued 算"还活着"', () => {
    for (const decision of ['pending', 'deciding', 'queued'] as const) {
      expect(grantIsLive(grant({ decision }))).toBe(true);
    }
    for (const decision of ['rest', 'expired', 'revoked', 'unavailable'] as const) {
      expect(grantIsLive(grant({ decision }))).toBe(false);
    }
    expect(grantIsLive(null)).toBe(false);
  });

  it('窗口校验：结束早于开始、已经过去、时间缺失各有不同的说法', () => {
    const now = Date.parse('2026-11-24T02:00:00Z');
    expect(checkGrantWindow('2026-11-24T03:00', '2026-11-24T02:30', now)).toMatchObject({
      ok: false, message: expect.stringContaining('晚于'),
    });
    expect(checkGrantWindow('2026-11-24T00:00', '2026-11-24T01:00', now)).toMatchObject({
      ok: false, message: expect.stringContaining('已经过去'),
    });
    expect(checkGrantWindow('', '2026-11-24T05:00', now)).toMatchObject({ ok: false });
    // 通道串是**本地墙钟**：拿本地串跟 UTC 的 now 比，结果取决于机器时区。
    // 所以这一条用本地时间构造"未来 1 小时"，才在不同时区的机器上都成立。
    const future = new Date(now + 3_600_000);
    const later = new Date(now + 7_200_000);
    expect(
      checkGrantWindow(isoToLocalInput(future.toISOString()), isoToLocalInput(later.toISOString()), now),
    ).toEqual({ ok: true });
  });

  it('本地时间 ↔ 带时区 ISO 往返保持同一时刻', () => {
    const local = isoToLocalInput('2026-11-24T01:30:00Z');
    const back = localInputToIso(local);
    expect(back).not.toBeNull();
    expect(Math.abs(Date.parse(back!) - Date.parse('2026-11-24T01:30:00Z'))).toBeLessThan(60_000);
  });

  it('本地时间串必须带时区后缀（后端是 AwareDatetime，裸串会 422）', () => {
    expect(localInputToIso('2026-11-24T01:30')).toMatch(/Z$|[+-]\d{2}:\d{2}$/);
  });

  it('时段显示当天时不重复日期', () => {
    const same = formatRange('2026-11-24T01:00:00Z', '2026-11-24T02:00:00Z');
    expect(same.match(/11 月 24 日/g) ?? []).toHaveLength(1);
  });
});

describe('杂项', () => {
  it('令牌掩码保留头尾，中间不可读', () => {
    const token = 'abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG';
    expect(maskToken(token)).toMatch(/^abcd…DEFG$/);
    expect(maskToken('short')).toBe('short');
  });

  it('启动命令只含地址，不含任何令牌形状的内容', () => {
    const command = launchCommand('https://ling.example');
    expect(command).toContain('py -m connectors.desktop --url https://ling.example');
    // 令牌是 43 字符的 base64url；命令里不该出现一段这么长的连续 token 字符。
    expect(command).not.toMatch(/[A-Za-z0-9_-]{43}/);
  });

  it('地址缺失时给占位符，不编一个域名', () => {
    expect(launchCommand('')).toContain('你的-ling-地址');
  });

  it('sinceText 对未来的时间不说"负数秒前"', () => {
    const now = Date.parse('2026-11-24T02:00:00Z');
    expect(sinceText('2026-11-24T02:00:03Z', now)).toBe('3 秒后');
    expect(sinceText(null, now)).toBe('从未连接');
    expect(sinceText('2026-11-24T01:59:58Z', now)).toBe('刚刚');
    expect(sinceText('2026-11-24T01:50:00Z', now)).toBe('10 分钟前');
  });
});
