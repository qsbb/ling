/**
 * 设置页布局（分组 / 网格 / 就地偏好）与权限页一行式列表的回归测试。
 *
 * 这一轮的用户原话是「设置界面重新设计一下布局」。原文案与入口一个都没删，
 * 变的**只有版面**，所以测试盯的也全是版面能表达的东西：
 *
 * 1. 三组存在、顺序是「关于她 → 安全 → 连接与运行」（常用的在上）；
 * 2. 卡片是等宽 2 列（≤760px 单列），卡内容纳"标题 + 一句说明 + 箭头"；
 * 3. 时区与视频两格**就地可改**（不是跳转按钮），且不再重复标题；
 * 4. 权限页一条权限 = 一行，已授权/未授权有状态标记；
 * 5. 375 与 1440 都不横向溢出（真浏览器，见 `settings-layout.browser.test.tsx`）。
 *
 * SSR 只能证明"画出来是什么"，宽度/列数交给真浏览器那组。
 */
import { readFileSync } from 'node:fs';
import { createElement as h, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import {
  SETTINGS_GROUPS,
  SettingsCard,
  SettingsHub,
  settingsSections,
  type SettingsCardSpec,
} from './components/SettingsHub';

const NOOP = () => undefined;

/** 渲染设置页外壳（`/v1/settings/taxonomy` 拉不到不影响分组渲染）。 */
function renderHub(): string {
  return renderToStaticMarkup(h(SettingsHub, { onBack: NOOP, onOpen: NOOP }));
}

/** 渲染一张跳转卡片。 */
function renderCard(spec: SettingsCardSpec, tile?: ReactNode): string {
  return renderToStaticMarkup(
    h(SettingsCard, {
      spec,
      onOpen: NOOP,
      decorate: (item: SettingsCardSpec) => item,
      ...(tile === undefined ? {} : { tile }),
    }),
  );
}

describe('设置页：分组与顺序', () => {
  it('三组存在，顺序是「关于她 → 安全 → 连接与运行」', () => {
    const sections = settingsSections();
    expect(sections.map((section) => section.key)).toEqual(['about-her', 'security', 'runtime']);
    expect(sections.map((section) => section.title)).toEqual(['关于她', '安全', '连接与运行']);
    // 每组都有一句说明（不做成没有注解的裸标题）
    for (const section of sections) {
      expect(section.note.length, `${section.title} 缺说明`).toBeGreaterThan(4);
    }
  });

  it('低频的「维护与关于」压在最下面，常用的时区在最上面', () => {
    const cards = settingsSections().flatMap((section) => section.cards.map((card) => card.key));
    expect(cards[0]).toBe('timezone');
    expect(cards[cards.length - 1]).toBe('about');
    // 模型与生成在"连接与运行"组里，排在维护之前
    expect(cards.indexOf('model')).toBeGreaterThan(-1);
    expect(cards.indexOf('model')).toBeLessThan(cards.indexOf('about'));
  });

  it('页面渲染出三组标题与说明（不是只有一个组）', () => {
    const html = renderHub();
    for (const title of ['关于她', '安全', '连接与运行']) {
      expect(html, `缺少分组标题 ${title}`).toContain(title);
    }
    expect(html).toContain('data-settings-group="about-her"');
    expect(html).toContain('data-settings-group="security"');
    expect(html).toContain('data-settings-group="runtime"');
  });

  it('入口一个都没少：SETTINGS_GROUPS 里的每个 testid 都出现在页面上', () => {
    const html = renderHub();
    for (const entry of SETTINGS_GROUPS.flatMap((group) => group.entries)) {
      const testid = entry.testid ?? `open-${entry.key}`;
      expect(html, `缺少入口 ${testid}`).toContain(`data-testid="${testid}"`);
    }
  });
});

describe('设置页：卡片是等宽网格里的一格', () => {
  it('跳转卡片是按钮，组件挂 `.settings-card` 并由网格决定宽度', () => {
    const html = renderCard({
      key: 'model',
      label: '模型与生成',
      verb: '配置 provider 与密钥。',
      path: '/settings/model',
      testid: 'open-model',
    });
    expect(html).toContain('<button');
    expect(html).toContain('class="settings-card"');
    expect(html).toContain('模型与生成');
    expect(html).toContain('配置 provider 与密钥。');
    // 箭头是"这张卡会跳走"的提示
    expect(html).toContain('settings-card__arrow');
  });

  it('就地可改的块是 section，不是按钮套按钮', () => {
    const html = renderCard(
      {
        key: 'timezone',
        label: '你在哪个时区',
        verb: '「今天」的边界按它算。',
        path: '',
        testid: 'settings-card-timezone',
        tile: 'timezone',
      },
      h('form', null, h('input', { defaultValue: 'Asia/Shanghai' })),
    );
    expect(html).toContain('<section');
    expect(html).not.toContain('<button');
    // 就地可改的块没有"跳走"箭头
    expect(html).not.toContain('settings-card__arrow');
    expect(html).toContain('settings-card--tile');
  });

  it('每组卡片都带 data-settings-card，网格才知道谁是谁', () => {
    const html = renderHub();
    expect(html).toContain('data-settings-card="timezone"');
    expect(html).toContain('data-settings-card="video"');
    expect(html).toContain('data-settings-card="privacy"');
    expect(html).toContain('data-settings-card="model"');
  });

  it('「关于她」两格里不再重复卡片标题（一格只说一遍可见文字）', () => {
    const html = renderHub();
    /*
     * 只数**可见标题**（`.settings-card__label`）：卡片的 `aria-label` 与可见
     * 标题同名是正常的（读屏需要它），不能算"写了两遍"。原来这两格在块内还各
     * 有一个 `<h3>`/`<legend>` 写着同一句话，那才是重复。
     */
    const visibleLabels = (text: string) =>
      (html.match(new RegExp(`settings-card__label">${text}<`, 'g')) ?? []).length;
    expect(visibleLabels('你在哪个时区')).toBe(1);
    expect(visibleLabels('她怎么看视频')).toBe(1);
    // 块内不再有第二份标题
    expect(html).not.toContain('settings-tile__title');
  });
});

describe('权限页：一条权限 = 一行', () => {
  /*
   * ConsentPage 的所有状态都在组件内部（要 fetch），SSR 只能看到"加载中"。
   * 这里改断言它**渲染用的类名与结构约定**：一行 = 主信息（名称 + 状态徽标 +
   * 说明）+ 一个按钮容器。真实两态渲染由 `settings-layout.browser.test.tsx`
   * 在浏览器里点进去量（已授权/未授权各一行、高度不再是三行）。
   */
  const source = readFileSync(new URL('./components/ConsentPage.tsx', import.meta.url), 'utf8');

  it('行结构：consent-row + 主信息 + 按钮容器（按钮不再裸露在行里）', () => {
    expect(source).toContain('className="consent-rows"');
    expect(source).toContain('className="consent-row"');
    expect(source).toContain('consent-row__main');
    expect(source).toContain('consent-row__action');
    // 旧的"三行一条"结构必须消失（名称/说明/scope=… 各占一行）
    expect(source).not.toContain('consent-list__item');
    expect(source).not.toContain('scope = <code>');
  });

  it('状态用可区分的徽标（已授权 / 未授权），不只靠颜色', () => {
    expect(source).toContain("{granted ? '已授权' : '未授权'}");
    // 徽标类名是模板串拼出来的，按"两种状态都拼进同一个 className"断言
    expect(source).toContain("consent-row__badge consent-row__badge--${granted ? 'granted' : 'denied'}");
    expect(source).toContain("data-state={granted ? 'granted' : 'denied'}");
  });

  it('已授权那一行的动作是「撤回」，未授权是「授予」', () => {
    expect(source).toContain("granted ? '撤回' : '授予'");
  });

  it('授权页引了自己的样式表（否则规则静默不生效，列表会退化成带圆点的裸 <ul>）', () => {
    expect(source).toContain("import '../settings.css'");
  });
});

describe('CSS：网格列数只声明一处，窄屏只做 2 → 1', () => {
  const css = readFileSync(new URL('./settings.css', import.meta.url), 'utf8');

  it('桌面是 2 列，且没有把列数塞进别的断点', () => {
    expect(css).toContain('repeat(2, minmax(0, 1fr))');
    // 上一轮总览出过的事故：把列模板写进手机断点，窄屏反而退化成 12 列细条
    expect(css).not.toMatch(/grid-template-columns:\s*repeat\(12/);
  });

  it('≤760px 单列', () => {
    expect(css).toMatch(/@media \(max-width: 760px\)[\s\S]{0,200}grid-template-columns: minmax\(0, 1fr\)/);
  });

  it('设置页用常数 gutter：只写 max-width 会被 .page 的百分比 padding 吃掉宽度', () => {
    expect(css).toContain('.page.settings-page');
    expect(css).toMatch(/\.page\.settings-page \{[\s\S]{0,120}padding-left: 24px/);
  });

  it('卡片有 min-height（等高）且内容顶对齐', () => {
    expect(css).toMatch(/\.settings-card \{[\s\S]{0,400}min-height: 92px/);
    expect(css).toMatch(/\.settings-card \{[\s\S]{0,400}justify-content: flex-start/);
  });
});
