/**
 * 对话页「她这次看到了什么」：入口、请求、四态、分区折叠与截断提示。
 *
 * 这里保护的是**语义**，不是像素：
 * - 入口只出现在她（`role=actor`）的消息下，且只有 owner 看得见；
 * - 404 是「这次没有记录」，不是"读取失败"，更不是空白；
 * - `expired` / `unavailable` 各有一句人话，绝不渲染成"没有条目"；
 * - 分区顺序、label、token、未知 key、折叠/展开都按后端给的原样呈现；
 * - 发言人优先 `sender`，没有才回落到 `role`；换行必须保留。
 *
 * 与 `dialogue.test.tsx` 一致，不引入 DOM 测试库：用 `react-dom/server`
 * 渲染字符串断言；真实点击 / Esc / 焦点 / 375px 测量在
 * `prompt.browser.test.tsx` 里跑（仓库只有 playwright，没有 jsdom）。
 */
import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { api, ApiError } from './api';
import { MessageList } from './components/MessageList';
import {
  canOpenPromptSnapshot,
  formatTokenCount,
  loadPromptSnapshot,
  PROMPT_COLLAPSED_PREVIEW,
  promptEntryAction,
  promptItemSpeaker,
  promptModelLabel,
  PromptDrawer,
  PromptDrawerView,
  PromptSectionBlock,
  promptSectionKey,
  PromptSnapshotBody,
  promptStateNotice,
  sectionCollapsedByDefault,
  visiblePromptItems,
} from './components/PromptDrawer';
import type { PromptItem, PromptSection, PromptSnapshotOut, UserInfo } from './types';
import type { UiMessage } from './useChat';

// `usePersonaName` 会请求 /v1/persona；渲染断言里不需要网络，固定成「灵」。
vi.mock('./personaName', () => ({ usePersonaName: () => '灵' }));

const CHAT_TIME = '2026-11-07T04:23:00Z';
const ENTRY_TEXT = '她这次看到了什么';

afterEach(() => {
  vi.unstubAllGlobals();
});

function messageFixture(patch: Partial<UiMessage> & { key: string }): UiMessage {
  const { key, ...rest } = patch;
  return {
    key,
    messageId: key,
    turnId: 'turn-1',
    clientTurnId: null,
    role: 'user',
    system: false,
    text: '内容',
    state: 'completed',
    createdAt: CHAT_TIME,
    modelLabel: null,
    errorCode: null,
    errorMessage: null,
    errorDetail: null,
    errorTraceId: null,
    retriable: false,
    local: false,
    attempt: 1,
    silenceReason: null,
    senderName: null,
    senderId: null,
    ...rest,
  };
}

function item(patch: Partial<PromptItem> & { content: string }): PromptItem {
  return { role: null, sender: null, at: null, ...patch };
}

function section(patch: Partial<PromptSection> & { key: string; items: PromptItem[] }): PromptSection {
  return {
    label: patch.key,
    role: null,
    tokens: null,
    collapsed: false,
    ...patch,
  };
}

const HISTORY_ITEMS = Array.from({ length: 14 }, (_, index) =>
  item({ role: index % 2 === 0 ? 'user' : 'actor', content: `历史第 ${index + 1} 条` }),
);

const SECTIONS: PromptSection[] = [
  section({
    key: 'system',
    label: '她是谁',
    role: 'system',
    tokens: 1024,
    items: [item({ role: 'system', content: '你是灵。\n保持安静。' })],
  }),
  section({
    key: 'memory',
    label: '她记得的事',
    tokens: 321,
    items: [item({ content: '你喜欢在晚上聊天。' })],
  }),
  section({
    key: 'history',
    label: '最近聊过什么',
    tokens: 4321,
    items: HISTORY_ITEMS,
  }),
  section({
    key: 'input',
    label: '你这次说的话',
    tokens: 12,
    items: [item({ role: 'user', content: '在吗' })],
  }),
  section({
    key: 'tool_calls',
    label: '工具调用记录',
    items: [item({ content: 'search("天气")' })],
  }),
];

function snapshotFixture(patch: Partial<PromptSnapshotOut> = {}): PromptSnapshotOut {
  return {
    state: 'available',
    message_id: 'msg-actor',
    turn_id: 'turn-1',
    created_at: '2026-11-07T04:23:00Z',
    expires_at: '2026-11-14T04:23:00Z',
    model: { provider_id: 'hy3gw2', model_id: 'hy3', profile: 'chat' },
    token_total: 12345,
    truncated: false,
    sections: SECTIONS,
    ...patch,
  };
}

const OWNER: UserInfo = {
  id: 'user-1',
  username: 'lingxi',
  role: 'owner',
  subject_id: 'subject-owner',
  display_name: '主人',
};

function renderMessageList(options: {
  messages: UiMessage[];
  promptEnabled?: boolean;
  promptOpenKey?: string | null;
  onOpenPrompt?: (message: UiMessage, trigger: HTMLElement) => void;
}): string {
  return renderToStaticMarkup(
    <MessageList
      messages={options.messages}
      activeTurn={null}
      live={null}
      loading={false}
      onRetry={() => undefined}
      onResend={() => undefined}
      onStop={() => undefined}
      onOpenSettings={() => undefined}
      promptEnabled={options.promptEnabled ?? true}
      promptOpenKey={options.promptOpenKey ?? null}
      onOpenPrompt={options.onOpenPrompt ?? (() => undefined)}
    />,
  );
}

function count(haystack: string, needle: string): number {
  return haystack.split(needle).length - 1;
}

function sectionLabels(markup: string): string[] {
  return Array.from(
    markup.matchAll(/class="prompt-drawer__section-title">([^<]*)</g),
    (match) => match[1] ?? '',
  );
}

/* ------------------------------------------------------------ 入口 */

describe('入口：只在她的话下面，只有 owner 可见', () => {
  it('她的消息有入口，我的消息没有', () => {
    const markup = renderMessageList({
      messages: [
        messageFixture({ key: 'u1', role: 'user', text: '在吗' }),
        messageFixture({ key: 'a1', role: 'actor', text: '在。' }),
      ],
    });

    expect(count(markup, 'data-testid="prompt-entry"')).toBe(1);
    expect(markup).toContain(ENTRY_TEXT);
    expect(markup).toContain('aria-haspopup="dialog"');
    // 入口在 actor 那条的 body 里，不在 user 那条里
    const actorIndex = markup.indexOf('data-role="actor"');
    const entryIndex = markup.indexOf('data-testid="prompt-entry"');
    expect(actorIndex).toBeGreaterThan(-1);
    expect(entryIndex).toBeGreaterThan(actorIndex);
  });

  it('非 owner 不渲染入口（后端也会 404，入口不必先给一个死路）', () => {
    const markup = renderMessageList({
      messages: [messageFixture({ key: 'a1', role: 'actor' })],
      promptEnabled: false,
    });
    expect(markup).not.toContain('data-testid="prompt-entry"');
    expect(canOpenPromptSnapshot('owner')).toBe(true);
    expect(canOpenPromptSnapshot('member')).toBe(false);
    expect(canOpenPromptSnapshot(null)).toBe(false);
    expect(canOpenPromptSnapshot(OWNER.role)).toBe(true);
  });

  it('没有 turn_id、没有 messageId 的消息也能点（走 404 的降级文案）', () => {
    const markup = renderMessageList({
      messages: [
        messageFixture({
          key: 'a-old',
          role: 'actor',
          turnId: null,
          messageId: null,
          local: true,
        }),
      ],
    });
    expect(count(markup, 'data-testid="prompt-entry"')).toBe(1);
  });

  it('抽屉打开时入口报 aria-expanded=true', () => {
    const markup = renderMessageList({
      messages: [messageFixture({ key: 'a1', role: 'actor' })],
      promptOpenKey: 'a1',
    });
    expect(markup).toContain('aria-expanded="true"');
    expect(markup).toMatch(/data-testid="prompt-entry"[\s\S]*?aria-expanded="true"/);
  });

  it('再点同一条入口 = 收起，不是重新打开', () => {
    expect(promptEntryAction('a1', 'a1')).toBe('close');
    expect(promptEntryAction('a1', 'a2')).toBe('open');
    expect(promptEntryAction(null, 'a1')).toBe('open');
  });
});

/* ------------------------------------------------------------ API */

describe('请求：路径与 404 映射', () => {
  it('messagePrompt 打的是 /v1/messages/{id}/prompt', async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify(snapshotFixture()), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const result = await api.messagePrompt('msg actor/1');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/messages/msg%20actor%2F1/prompt',
      expect.objectContaining({ credentials: 'same-origin' }),
    );
    expect(result.state).toBe('available');
    expect(result.token_total).toBe(12345);
    expect(result.model?.profile).toBe('chat');
  });

  it('404 → missing（「这次没有记录」），不是可重试的错误', async () => {
    const fetcher = vi.fn(async () => {
      throw new ApiError(404, 'not_found', '目标不存在');
    });
    const view = await loadPromptSnapshot('msg-gone', fetcher);
    expect(view.kind).toBe('missing');
  });

  it('消息 id 为空时不发请求，直接 missing', async () => {
    const fetcher = vi.fn(async () => snapshotFixture());
    const view = await loadPromptSnapshot(null, fetcher);
    expect(view.kind).toBe('missing');
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('500 才给错误态与重试文案', async () => {
    const fetcher = vi.fn(async () => {
      throw new ApiError(500, 'internal', '服务端内部错误', true);
    });
    const view = await loadPromptSnapshot('msg-1', fetcher);
    expect(view.kind).toBe('error');
    if (view.kind === 'error') {
      expect(view.error.reason).toBe('服务端内部错误');
      expect(view.error.retriable).toBe(true);
    }
  });

  it('成功时把快照原样交给界面', async () => {
    const fetcher = vi.fn(async () => snapshotFixture({ truncated: true }));
    const view = await loadPromptSnapshot('msg-1', fetcher);
    expect(view.kind).toBe('snapshot');
    if (view.kind === 'snapshot') {
      expect(view.snapshot.truncated).toBe(true);
      expect(fetcher).toHaveBeenCalledWith('msg-1');
    }
  });
});

/* ------------------------------------------------------------ 三态 */

describe('三态：各有各的话，绝不空白', () => {
  it('available 正常渲染分区', () => {
    const markup = renderToStaticMarkup(<PromptSnapshotBody snapshot={snapshotFixture()} />);
    expect(markup).toContain('data-testid="prompt-snapshot"');
    expect(count(markup, 'data-testid="prompt-section"')).toBe(SECTIONS.length);
    expect(promptStateNotice('available')).toBeNull();
  });

  it('expired 说「已经过期（保留 7 天）」，且不渲染任何分区', () => {
    const markup = renderToStaticMarkup(
      <PromptSnapshotBody snapshot={snapshotFixture({ state: 'expired' })} />,
    );
    expect(markup).toContain('这次记录已经过期（保留 7 天）');
    expect(markup).not.toContain('data-testid="prompt-section"');
  });

  it('unavailable 说「没有保存下来」，不是空内容', () => {
    const markup = renderToStaticMarkup(
      <PromptSnapshotBody snapshot={snapshotFixture({ state: 'unavailable' })} />,
    );
    expect(markup).toContain('这次没有保存下来');
    expect(markup).not.toContain('data-testid="prompt-section"');
    expect(markup).not.toContain('data-testid="prompt-snapshot"');
    // 「没有保存下来」不能同时说「记录保留至…」，那是自相矛盾
    expect(markup).not.toContain('记录保留至');
  });

  it('半上线/旧响应缺 sections 或 items 时不白屏', () => {
    const noSections = renderToStaticMarkup(
      <PromptSnapshotBody
        snapshot={snapshotFixture({ sections: undefined as unknown as PromptSection[] })}
      />,
    );
    expect(noSections).toContain('这次没有注入任何内容');

    const noItems = renderToStaticMarkup(
      <PromptSnapshotBody
        snapshot={snapshotFixture({
          sections: [
            {
              key: 'system',
              label: '她是谁',
              items: undefined as unknown as PromptItem[],
            },
          ],
        })}
      />,
    );
    expect(noItems).toContain('这一段没有内容');
  });

  it('未知 state 不吞：落到「没有保存下来」', () => {
    const markup = renderToStaticMarkup(
      <PromptSnapshotBody
        snapshot={snapshotFixture({ state: 'future_state' as PromptSnapshotOut['state'] })}
      />,
    );
    expect(markup).toContain('这次没有保存下来');
  });
});

/* ------------------------------------------------------------ 内容 */

describe('内容：顺序、人话标题、token、未知 key 原样', () => {
  it('分区按后端给的顺序渲染，label 用人话，未知 key 不吞', () => {
    const markup = renderToStaticMarkup(<PromptSnapshotBody snapshot={snapshotFixture()} />);
    expect(sectionLabels(markup)).toEqual([
      '她是谁',
      '她记得的事',
      '最近聊过什么',
      '你这次说的话',
      '工具调用记录',
    ]);
    expect(markup).toContain('data-key="tool_calls"');
  });

  it('顶部摘要给模型、职责、总 token、创建时间', () => {
    const markup = renderToStaticMarkup(<PromptSnapshotBody snapshot={snapshotFixture()} />);
    expect(markup).toContain('hy3gw2/hy3');
    expect(markup).toContain('chat');
    expect(markup).toContain('总 token');
    expect(markup).toContain(`约 ${formatTokenCount(12345)} tokens`);
    expect(markup).toContain('创建于');
    expect(formatTokenCount(12345)).toBe('12,345');
  });

  it('每个 section 显示 token 估算，没有就不编', () => {
    const markup = renderToStaticMarkup(<PromptSnapshotBody snapshot={snapshotFixture()} />);
    expect(markup).toContain('约 1,024 tokens');
    expect(markup).toContain('约 4,321 tokens');
    // 没有 tokens 的 tool_calls 不该出现一个 0
    const toolSection = markup.slice(markup.indexOf('data-key="tool_calls"'));
    expect(toolSection).not.toContain('prompt-section-tokens');
  });

  it('truncated=true 给「太长」提示；false 不给', () => {
    const on = renderToStaticMarkup(
      <PromptSnapshotBody snapshot={snapshotFixture({ truncated: true })} />,
    );
    const off = renderToStaticMarkup(
      <PromptSnapshotBody snapshot={snapshotFixture({ truncated: false })} />,
    );
    expect(on).toContain('这次内容太长，只保留了开头和最后几条');
    expect(off).not.toContain('这次内容太长');
  });

  it('有 sender 显示「发言人：内容」，没有按 role 显示', () => {
    expect(promptItemSpeaker(item({ sender: '小明', role: 'user', content: 'x' }))).toBe('小明');
    expect(promptItemSpeaker(item({ role: 'system', content: 'x' }))).toBe('系统');
    expect(promptItemSpeaker(item({ role: 'user', content: 'x' }))).toBe('用户');
    expect(promptItemSpeaker(item({ role: 'actor', content: 'x' }))).toBe('她');
    expect(promptItemSpeaker(item({ content: 'x' }))).toBeNull();
    // 未知 role 原样显示，不编一个「成员」
    expect(promptItemSpeaker(item({ role: 'tool', content: 'x' }))).toBe('tool');

    const markup = renderToStaticMarkup(
      <PromptSnapshotBody
        snapshot={snapshotFixture({
          sections: [
            section({
              key: 'history',
              label: '最近聊过什么',
              collapsed: true,
              items: [item({ sender: '小明', content: '第一行\n第二行' })],
            }),
          ],
        })}
      />,
    );
    expect(markup).toContain('小明：');
    expect(markup).toContain('第一行\n第二行');
  });
});

/* ------------------------------------------------------------ 折叠 */

describe('折叠：history 与超长分区默认折起，展开后还能收起', () => {
  const history = SECTIONS.find((entry) => entry.key === 'history') as PromptSection;

  it('默认折叠判定：history / 后端建议 / 超过 12 条', () => {
    expect(sectionCollapsedByDefault(history)).toBe(true);
    expect(sectionCollapsedByDefault(section({ key: 'memory', items: [] }))).toBe(false);
    expect(
      sectionCollapsedByDefault(section({ key: 'memory', collapsed: true, items: [item({ content: 'x' })] })),
    ).toBe(true);
    expect(
      sectionCollapsedByDefault(
        section({
          key: 'other',
          items: Array.from({ length: 13 }, (_, index) => item({ content: `第 ${index} 条` })),
        }),
      ),
    ).toBe(true);
    expect(
      sectionCollapsedByDefault(
        section({
          key: 'other',
          items: Array.from({ length: 12 }, (_, index) => item({ content: `第 ${index} 条` })),
        }),
      ),
    ).toBe(false);
  });

  it('折叠只给开头几条（不是 0 条），展开给全部', () => {
    expect(visiblePromptItems(history, false)).toHaveLength(PROMPT_COLLAPSED_PREVIEW);
    expect(visiblePromptItems(history, true)).toHaveLength(history.items.length);

    const collapsed = renderToStaticMarkup(
      <PromptSectionBlock section={history} expanded={false} onToggle={() => undefined} />,
    );
    expect(collapsed).toContain('历史第 1 条');
    expect(collapsed).toContain('历史第 2 条');
    expect(collapsed).not.toContain('历史第 3 条');
    expect(collapsed).toContain('展开全部（14 条）');
    expect(collapsed).toContain('aria-expanded="false"');

    const expanded = renderToStaticMarkup(
      <PromptSectionBlock section={history} expanded onToggle={() => undefined} />,
    );
    expect(expanded).toContain('历史第 14 条');
    expect(expanded).toContain('收起');
    expect(expanded).toContain('aria-expanded="true"');
  });

  it('整份快照里 history 默认折起，展开表里给它 true 才展开', () => {
    const collapsed = renderToStaticMarkup(<PromptSnapshotBody snapshot={snapshotFixture()} />);
    expect(collapsed).not.toContain('历史第 14 条');
    expect(collapsed).toContain('展开全部（14 条）');
    // 短分区不受影响：input 的 1 条照常显示
    expect(collapsed).toContain('在吗');

    const historyIndex = SECTIONS.findIndex((entry) => entry.key === 'history');
    const key = promptSectionKey(historyIndex, history);
    const expanded = renderToStaticMarkup(
      <PromptSnapshotBody snapshot={snapshotFixture()} expanded={{ [key]: true }} />,
    );
    expect(expanded).toContain('历史第 14 条');
    expect(expanded).toContain('收起');
  });

  it('短分区没有「展开全部」按钮', () => {
    const short = renderToStaticMarkup(
      <PromptSectionBlock
        section={section({ key: 'input', label: '你这次说的话', items: [item({ content: '在吗' })] })}
        expanded={false}
        onToggle={() => undefined}
      />,
    );
    expect(short).not.toContain('prompt-section-toggle');
  });
});

/* ------------------------------------------------------------ 抽屉壳 */

describe('抽屉壳：关闭时不渲染，打开时是 dialog', () => {
  it('open=false 什么都不渲染', () => {
    expect(renderToStaticMarkup(<PromptDrawer open={false} messageId="m1" onClose={() => undefined} />)).toBe('');
  });

  it('open=true 是 aria-modal dialog，标题与结构齐备', () => {
    const markup = renderToStaticMarkup(
      <PromptDrawer open messageId="m1" onClose={() => undefined} />,
    );
    expect(markup).toContain('data-testid="prompt-drawer"');
    expect(markup).toContain('role="dialog"');
    expect(markup).toContain('aria-modal="true"');
    expect(markup).toContain(`id="prompt-drawer-title"`);
    expect(markup).toContain(ENTRY_TEXT);
    expect(markup).toContain('data-testid="prompt-scrim"');
    expect(markup).toContain('data-testid="prompt-drawer-close"');
    // 初始是加载态（SSR 不跑 effect）
    expect(markup).toContain('data-testid="prompt-loading"');
  });

  it('404 的文案是「这次没有记录」', () => {
    const markup = renderToStaticMarkup(<PromptDrawerView view={{ kind: 'missing' }} />);
    expect(markup).toContain('data-testid="prompt-missing"');
    expect(markup).toContain('这次没有记录');
  });
});

/* ------------------------------------------------------------ 样式契约 */

describe('样式契约：宽度三段、内部滚动、保留换行', () => {
  const css = readFileSync(new URL('./prompt-drawer.css', import.meta.url), 'utf8');

  it('宽屏 420–560，≤1170 用 min(88vw, 520px)，≤560 占满', () => {
    expect(css).toContain('width: clamp(420px, 34vw, 560px)');
    expect(css).toMatch(/@media \(max-width: 1170px\)[\s\S]*?width: min\(88vw, 520px\)/);
    expect(css).toMatch(/@media \(max-width: 560px\)[\s\S]*?width: 100%/);
  });

  it('抽屉内部独立滚动，且不锁背景滚动（没有 body overflow:hidden）', () => {
    expect(css).toMatch(/\.prompt-drawer__body\s*\{[^}]*overflow-y: auto/);
    expect(css).not.toMatch(/(^|\n)\s*body\s*\{[^}]*overflow:\s*hidden/);
  });

  it('遮罩与 wrapper 不接指针，只有面板接：背景仍可滚动/可读', () => {
    expect(css).toMatch(/\.prompt-drawer\s*\{[^}]*pointer-events: none/);
    expect(css).toMatch(/\.prompt-drawer__scrim\s*\{[^}]*pointer-events: none/);
    expect(css).toMatch(/\.prompt-drawer__panel\s*\{[^}]*pointer-events: auto/);
  });

  it('内容保留换行，长串不撑破', () => {
    expect(css).toMatch(/\.prompt-drawer__item-content\s*\{[^}]*white-space: pre-wrap/);
    expect(css).toMatch(/\.prompt-drawer__item-content\s*\{[^}]*overflow-wrap: anywhere/);
  });
});

/* 保持 `promptModelLabel` 也被覆盖到：以后字段改名时测试会先红。 */
describe('模型标识：缺一半不编', () => {
  it('provider/model 全给才拼完整，否则给有的那一半', () => {
    expect(promptModelLabel({ provider_id: 'p', model_id: 'm', profile: null })).toBe('p/m');
    expect(promptModelLabel({ provider_id: 'p', model_id: null, profile: null })).toBe('p');
    expect(promptModelLabel({ provider_id: null, model_id: 'm', profile: null })).toBe('m');
    expect(promptModelLabel(null)).toBeNull();
  });
});
