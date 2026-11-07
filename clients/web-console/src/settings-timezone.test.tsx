/**
 * 「你的本地时间」（时区）的渲染断言。
 *
 * 这里保护的是**三种来源不能混成一句话**：
 * - 你自己设的（DB 偏好）→ 不许说成"默认值"；
 * - 环境变量给的 → 必须点名 `LING_USER_TIMEZONE`，否则用户不知道去改哪儿；
 * - 默认值 → 必须说"没有设置过"，否则用户以为是自己设的（这正是这次要修的
 *   那类错觉：设置里根本没有这一项，界面却按某个值在算）。
 *
 * 不引入 DOM 测试库：用 `react-dom/server` 渲染字符串断言，与
 * `connectors.test.tsx` / `dashboard.test.ts` 一致。交互（保存）由页面
 * 走真实接口，不在这里模拟点击。
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './api';
import {
  SettingsHub,
  TimezoneForm,
  TimezoneSection,
  type TimezoneFormProps,
} from './components/SettingsHub';

const COMMON = ['Asia/Shanghai', 'Asia/Tokyo', 'Europe/London', 'UTC'];

function form(overrides: Partial<TimezoneFormProps> = {}): string {
  const props: TimezoneFormProps = {
    draft: '',
    effective: 'Asia/Shanghai',
    source: 'default',
    env: null,
    common: COMMON,
    saving: false,
    dirty: false,
    message: null,
    onChange: vi.fn(),
    onSave: vi.fn(),
    onReset: vi.fn(),
    ...overrides,
  };
  return renderToStaticMarkup(<TimezoneForm {...props} />);
}

describe('时区表单：三种来源各说各的', () => {
  it('默认值时明说「没有设置过」，不冒充用户设置', () => {
    const html = form();
    expect(html).toContain('没有设置过');
    expect(html).toContain('Asia/Shanghai');
    expect(html).not.toContain('你自己设的');
  });

  it('环境变量时点名变量名，用户才知道去改哪个', () => {
    const html = form({ source: 'env', env: 'Europe/London', effective: 'Europe/London' });
    expect(html).toContain('LING_USER_TIMEZONE=Europe/London');
    // 说清"填一次就以你为准"，否则用户以为只能改 .env
    expect(html).toContain('填一个就以你为准');
    expect(html).not.toContain('没有设置过');
  });

  it('自己设过时说是你设的（并说明回滚配置不会变）', () => {
    const html = form({
      source: 'preference',
      effective: 'Asia/Tokyo',
      draft: 'Asia/Tokyo',
    });
    expect(html).toContain('你自己设的：Asia/Tokyo');
    expect(html).toContain('重启和回滚都不变');
    expect(html).not.toContain('没有设置过');
  });
});

describe('时区表单：输入与提交', () => {
  it('输入框显示库里存的原值，常用项作建议（datalist，不是白名单）', () => {
    const html = form({ draft: 'Europe/London' });
    // 值是 draft（存的原值），不是生效值——否则默认值看起来像"我设的"
    expect(html).toContain('value="Europe/London"');
    expect(html).toContain('settings-timezone-options');
    for (const tz of COMMON.slice(0, 3)) {
      expect(html).toContain(tz);
    }
    // 说明里写明可以填任意 IANA 名
    expect(html).toContain('IANA');
  });

  it('空串草稿时用生效值作占位提示，而不是把默认值写进 value', () => {
    const html = form({ draft: '' });
    expect(html).toContain('placeholder="Asia/Shanghai"');
    // 输入框自己的 value 必须是空的（datalist 里的 option 不算）
    expect(html).toContain('data-testid="settings-timezone-input" value=""');
  });

  it('窄屏靠 form-grid（auto-fit）排：不写死列数', () => {
    // 375px 下写死两列会把"Asia/Shanghai"挤到换行/溢出；这里钉住用的是
    // 自适应网格，改回固定列数时这条会红。
    expect(form()).toContain('class="form-grid"');
  });

  it('保存中：按钮禁用，写得出现在在干什么', () => {
    const html = form({ saving: true });
    expect(html).toContain('保存中…');
    expect(html).toContain('disabled');
  });

  it('有改动时提示「有未保存的改动」，没有时说明不走发布', () => {
    expect(form({ dirty: true })).toContain('有未保存的改动');
    const clean = form({ dirty: false });
    expect(clean).toContain('保存即生效');
    expect(clean).toContain('不走草稿/发布');
  });

  it('保存结果显示在表单里；错误要带原因', () => {
    const ok = form({ message: { kind: 'ok', text: '已保存：现在按 Asia/Shanghai 计算' } });
    expect(ok).toContain('已保存：现在按 Asia/Shanghai 计算');
    const bad = form({
      message: { kind: 'error', text: '保存失败：不是合法的时区名', detail: '例：Asia/Shanghai' },
    });
    expect(bad).toContain('不是合法的时区名');
    expect(bad).toContain('例：Asia/Shanghai');
  });
});

describe('偏好接口：两个字段各发各的（验收 B1）', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('setVideoMode 只带 video_mode，setTimezone 只带 timezone', async () => {
    // 后端把"缺省"当"不改"；前端如果顺手把另一个字段也塞进来（哪怕是空串），
    // 就会把它清掉。这里钉住两条请求体本身。
    const sent: Array<Record<string, unknown>> = [];
    const prefs = {
      schema_version: 'v1',
      timezone: 'Asia/Shanghai',
      source: 'default',
      stored: null,
      env: null,
      default: 'Asia/Shanghai',
      common: ['Asia/Shanghai'],
      video_mode: 'frames',
      video_modes: ['frames', 'native'],
      video_mode_default: 'frames',
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init?: RequestInit) => {
        sent.push(JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>);
        return new Response(JSON.stringify(prefs), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }),
    );

    await api.setVideoMode('native');
    await api.setTimezone('');

    expect(sent[0]).toEqual({ video_mode: 'native' });
    expect(sent[1]).toEqual({ timezone: '' });
  });
});

describe('设置页文案：JSX 里的 Markdown 不会变粗体', () => {
  it('正文不出现字面 `**`（验收 O4）', () => {
    const html = renderToStaticMarkup(<SettingsHub onBack={vi.fn()} onOpen={vi.fn()} />);
    // 页面里出现 2 个 `**` 是"漏渲染"的证据：JSX 不做 Markdown
    expect(html).not.toContain('**');
    expect(html).toContain('<strong>你</strong>');
  });
});

describe('时区区块：读不到不挡路', () => {
  it('首帧是读取态，不是空白（静态渲染没有 effect）', () => {
    const html = renderToStaticMarkup(<TimezoneSection />);
    expect(html).toContain('正在读取时区');
  });
});
