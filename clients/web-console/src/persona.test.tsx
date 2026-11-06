/**
 * 人设页「场景处理」区的回归测试。
 *
 * 这里保护三件事，都是会静默出错的那种：
 * 1. 空输入框 = 用默认（不能把默认文本写进 payload，"恢复默认"就失效了）；
 * 2. 默认文本必须出现在 placeholder 里——用户不删自己的话也该看得到默认；
 * 3. 只有真的写过内容才出现「恢复默认」，否则那是一个点了也没反应的按钮。
 *
 * 渲染断言仍用 `react-dom/server`（仓库没有 jsdom 依赖，与 dashboard/about
 * 同一取舍）；真实交互由 typecheck/build 与手工验收兜底。
 */
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  ScenarioEditor,
  scenarioDraftsFrom,
  scenarioPayload,
} from './components/PersonaPage';
import type { Persona, PersonaScenario } from './types';

const SCENARIOS: PersonaScenario[] = [
  {
    key: 'private',
    label: '私聊',
    when: '一对一聊天时，回复按这条来',
    text: '私聊默认：慢慢说',
    default_text: '私聊默认：慢慢说',
    custom: false,
  },
  {
    key: 'group',
    label: '群聊',
    when: '在群里说话时，回复按这条来',
    text: '群里默认：少说一句',
    default_text: '群里默认：少说一句',
    custom: false,
  },
];

function persona(overrides: Record<string, string>): Persona {
  return {
    persona_id: 'persona_ling',
    revision: 3,
    name: 'ling',
    display_name: 'ling',
    system_prompt: '',
    traits: {},
    boundaries: [],
    scenario_prompts: overrides,
    scenarios: SCENARIOS,
  };
}

function render(params: {
  drafts: Record<string, string>;
  onChange?: (key: string, value: string) => void;
}): string {
  return renderToStaticMarkup(
    h(ScenarioEditor, {
      scenarios: SCENARIOS,
      drafts: params.drafts,
      onChange: params.onChange ?? (() => {}),
    }),
  );
}

describe('场景草稿与 payload', () => {
  it('只把服务端存过的覆盖带进草稿，默认值不进草稿', () => {
    expect(scenarioDraftsFrom(persona({ group: '群里只接一句' }))).toEqual({
      group: '群里只接一句',
    });
    expect(scenarioDraftsFrom(persona({}))).toEqual({});
  });

  it('去空白后为空的项不发送——空等于"没有覆盖"', () => {
    expect(scenarioPayload({ private: '   ', group: '  群里少说  ' })).toEqual({
      group: '群里少说',
    });
  });

  it('全部清空时发送空对象（= 全部恢复默认），而不是不带这个字段', () => {
    expect(scenarioPayload({ private: '', group: '' })).toEqual({});
  });
});

describe('场景编辑区渲染', () => {
  it('每个场景都给出名字、生效时机与默认文本', () => {
    const html = render({ drafts: {} });
    for (const scenario of SCENARIOS) {
      expect(html).toContain(scenario.label);
      expect(html).toContain(scenario.when);
      expect(html).toContain(`placeholder="${scenario.default_text}"`);
    }
  });

  it('没写过时标「默认」，且不出现「恢复默认」', () => {
    const html = render({ drafts: {} });
    expect(html).toContain('全部用默认');
    expect(html).not.toContain('恢复默认');
  });

  it('写过的那条标「自己写的」，并给出「恢复默认」', () => {
    const html = render({ drafts: { group: '群里只接技术问题' } });
    expect(html).toContain('自己写的');
    expect(html).toContain('1 条自己写的');
    expect(html).toContain('群里只接技术问题');
    expect(html).toContain('恢复默认');
    // 只有改过的那条有按钮，没改的没有
    expect(html.match(/恢复默认/g) ?? []).toHaveLength(1);
  });

  it('恢复默认是清空输入框，而不是把默认文本填进去', () => {
    const seen: Array<[string, string]> = [];
    // onChange 在静态渲染里不会被调用，这里直接验证它与 payload 的约定：
    // 清空 → payload 里没有这个键 → 服务端回落到默认。
    const html = render({ drafts: { group: '自己写的' }, onChange: (k, v) => seen.push([k, v]) });
    expect(html).toContain('data-testid="scenario-reset-group"');
    expect(scenarioPayload({ group: '' })).toEqual({});
  });
});
