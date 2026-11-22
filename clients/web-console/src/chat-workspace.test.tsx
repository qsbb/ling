/**
 * 对话页重组的判据（服务端渲染层）。
 *
 * 这一组钉的是**信息位置**，不是像素：
 *
 * 1. 会话列表主行只读「名称 + 群/私聊 + 近期活动」，外部键默认不出现；
 *    重命名/置顶/排序/外部键都在「更多」菜单与「会话信息」里按需展开。
 * 2. 消息主行只留时间与需要动手的状态；模型与「她这次看到了什么」收进
 *    每条消息自己的详情折叠区。
 * 3. 正文里的 `[image]` / `[voice]` 渲染成可读的媒体块（不是假图片，
 *    也不是一串裸露的方括号）。
 * 4. 历史边界：有更早的一页时给「读取更早的消息」，到头时给「这里是开头」。
 * 5. 输入区默认只露工具按钮；限制文案只在展开后出现。
 *
 * 每个断言都配一条反例说明它失败于哪种错误实现，见各用例注释。
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { Composer } from './components/Composer';
import { MessageList } from './components/MessageList';
import { buildSessionRows, SessionSidebar } from './components/SessionSidebar';
import type { SessionRecord } from './types';
import type { UiMessage } from './useChat';

vi.mock('./personaName', () => ({ usePersonaName: () => '灵' }));

const CREATED = '2026-11-07T04:23:00Z';
const GROUP_REF = 'onebot-default:group:100200400';

function session(patch: Partial<SessionRecord> & { session_id: string }): SessionRecord {
  return {
    schema_version: 'v1',
    actor_id: 'actor_ling',
    subject_id: 'subject_owner',
    window_id: patch.session_id,
    title: '新的对话',
    created_at: '2026-11-06T01:00:00Z',
    updated_at: CREATED,
    message_count: 12,
    archived: false,
    ...patch,
  };
}

function message(patch: Partial<UiMessage> & { key: string }): UiMessage {
  const { key, ...rest } = patch;
  return {
    key,
    messageId: key,
    turnId: null,
    clientTurnId: null,
    role: 'user',
    system: false,
    text: '内容',
    state: 'completed',
    createdAt: CREATED,
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
    recalledAt: null,
    recalledByActor: false,
    sequence: 1,
    ...rest,
  };
}

function renderMessages(props: {
  messages: UiMessage[];
  loadingOlder?: boolean;
  hasMoreHistory?: boolean;
  promptEnabled?: boolean;
  variant?: 'web' | 'group' | 'private';
}): string {
  return renderToStaticMarkup(
    <MessageList
      messages={props.messages}
      activeTurn={null}
      live={null}
      loading={false}
      loadingOlder={props.loadingOlder ?? false}
      hasMoreHistory={props.hasMoreHistory ?? false}
      onLoadOlder={() => undefined}
      onRetry={() => undefined}
      onResend={() => undefined}
      onStop={() => undefined}
      onOpenSettings={() => undefined}
      variant={props.variant ?? 'web'}
      promptEnabled={props.promptEnabled ?? false}
      onOpenPrompt={() => undefined}
    />,
  );
}

const countOf = (html: string, needle: string): number => html.split(needle).length - 1;

describe('会话行：主行只读名称/群聊/近期活动', () => {
  const sessions = [
    session({
      session_id: 'g1',
      title: GROUP_REF,
      display_name: '阿懒的家',
      channel: 'onebot-default',
      peer_id: '100200400',
      message_count: 42,
      pinned: true,
    }),
    session({ session_id: 'w1', title: '深夜聊天', channel: 'web', message_count: 7 }),
  ];

  it('主行没有外部键，信息按需展开后仍完整', () => {
    const rows = buildSessionRows(sessions, 'w1', 'owner');
    // 主行读"名称"：群名优先，没有名字才回退号码。
    expect(rows.map((row) => row.heading.title)).toEqual(['阿懒的家', '深夜聊天']);
    expect(rows.map((row) => row.heading.kind)).toEqual(['group', 'web']);
    // 选中状态由 currentId 决定，不靠颜色猜。
    expect(rows.map((row) => row.active)).toEqual([false, true]);
    // 外部键仍在（会话信息里给），但**不在主行渲染**——反例：把 subtitle
    // 直接塞回主行，下面这条会立刻红。
    expect(rows[0]?.heading.subtitle).toBe(GROUP_REF);
    // 会话列表主行：外部键（窗口键）不得出现在渲染结果里——反例：把
    // subtitle 塞回 `.session__subtitle`，这条会红。
    const sidebar = renderToStaticMarkup(
      <SessionSidebar
        sessions={sessions}
        currentId="w1"
        open={false}
        user={{ id: 'u', username: 'o', role: 'owner', subject_id: 's', display_name: '主人' }}
        onClose={() => undefined}
        onSelect={() => undefined}
        onCreate={() => undefined}
        onRename={async () => true}
        onPin={async () => undefined}
        onMove={async () => undefined}
        onNavigateSettings={() => undefined}
        onNavigatePersona={() => undefined}
        onLogout={() => undefined}
      />,
    );
    expect(sidebar).toContain('阿懒的家');
    expect(sidebar).not.toContain(GROUP_REF);
    expect(sidebar).toContain('42 条');
  });

  it('置顶只用一个标记，不额外占一行文字', () => {
    const rows = buildSessionRows(sessions, null, 'owner');
    // 置顶组在前；组内顺序保持后端给的顺序。
    expect(rows.map((row) => row.session.session_id)).toEqual(['g1', 'w1']);
    expect(rows[0]?.session.pinned).toBe(true);
  });

  it('排序项只在同组邻居存在时出现（跨置顶组不串）', () => {
    const rows = buildSessionRows(
      [
        session({ session_id: 'p1', title: '置顶一', pinned: true }),
        session({ session_id: 'p2', title: '置顶二', pinned: true }),
        session({ session_id: 'n1', title: '普通一' }),
      ],
      null,
      'admin',
    );
    // 置顶组内可以互换；跨组（p2 → n1）不给"下移"，否则会出现"移到另一组"
    // 这种后端不支持的语义。
    expect(rows[0]?.next?.session_id).toBe('p2');
    expect(rows[1]?.next).toBeNull();
    expect(rows[2]?.previous).toBeNull();
  });

  it('成员没有排序能力，但网页会话仍可改名', () => {
    const rows = buildSessionRows(sessions, null, 'member');
    expect(rows.every((row) => row.next === null && row.previous === null)).toBe(true);
    expect(rows.map((row) => row.renamable)).toEqual([false, true]);
  });
});

describe('消息行：主行只留状态与时间', () => {
  it('模型名与入口都在详情折叠区，不在主行', () => {
    const html = renderMessages({
      messages: [
        message({
          key: 'a1',
          role: 'actor',
          text: '在。',
          modelLabel: 'hy3gw2/hy3',
          messageId: 'msg-a1',
        }),
      ],
      promptEnabled: true,
    });

    // 主行（气泡下方那行 meta）不得出现模型名——反例：把 modelLabel 放回
    // meta，这条会红。
    const metaStart = html.indexOf('class="message__meta message__meta--own"');
    const metaEnd = html.indexOf('</div>', metaStart);
    const meta = html.slice(metaStart, metaEnd);
    expect(meta).not.toContain('hy3');
    // 详情里既给模型、也给完整标识（悬停可核对）。
    expect(html).toContain('data-testid="message-details"');
    expect(html).toContain('title="hy3gw2/hy3"');
    expect(html).toContain('>hy3<');
    // 入口在详情里，且详情默认收起（`<details>` 没有 open 属性）。
    expect(html).toContain('data-testid="prompt-entry"');
    expect(html).not.toMatch(/<details[^>]*\sopen/);
  });

  it('没有模型也没有入口的消息不渲染空详情（否则每条都多一行折叠）', () => {
    const html = renderMessages({
      messages: [message({ key: 'a1', role: 'actor', text: '嗯' })],
      promptEnabled: true,
    });
    // promptEnabled 时她有入口，所以详情在；换成非 owner 就没有任何详情。
    expect(countOf(html, 'data-testid="message-details"')).toBe(1);
    const memberView = renderMessages({
      messages: [message({ key: 'a1', role: 'actor', text: '嗯' })],
      promptEnabled: false,
    });
    expect(countOf(memberView, 'data-testid="message-details"')).toBe(0);
  });

  it('失败消息仍把错误与重试放在主区，不藏进详情', () => {
    const html = renderMessages({
      messages: [
        message({
          key: 'a1',
          role: 'actor',
          text: '',
          state: 'failed',
          turnId: 'turn-1',
          retriable: true,
          errorCode: 'provider_error',
          errorMessage: '模型服务不可用',
        }),
      ],
    });
    expect(html).toContain('模型服务不可用');
    expect(html).toContain('打开设置');
    // 重试按钮直接可见（不需要展开详情才能救场）。
    expect(html).toContain('message-error__actions');
  });
});

describe('正文里的媒体占位', () => {
  it('[image] / [voice] 渲染成可读媒体块，而不是裸露标记', () => {
    const html = renderMessages({
      messages: [
        message({
          key: 'm1',
          role: 'user',
          text: '看这个 [image] 还有 [voice]',
          senderName: '阿青',
          senderId: '100200302',
        }),
      ],
      variant: 'group',
    });
    expect(countOf(html, 'data-testid="message-media"')).toBe(2);
    expect(html).toContain('data-kind="image"');
    expect(html).toContain('data-kind="voice"');
    expect(html).toContain('图片');
    expect(html).toContain('语音');
    // 方括号原文不再裸露（反例：只做 CSS 不拆 token）。
    expect(html).not.toContain('[image]');
    expect(html).not.toContain('[voice]');
    // 同一句里其余文字原样保留，顺序不被打乱。
    expect(html.indexOf('看这个')).toBeLessThan(html.indexOf('图片'));
    expect(html.indexOf('图片')).toBeLessThan(html.indexOf('还有'));
    expect(html.indexOf('还有')).toBeLessThan(html.indexOf('语音'));
  });

  it('普通文本不产生媒体块（不误伤方括号）', () => {
    const html = renderMessages({
      messages: [message({ key: 'm1', text: '数组 a[i] 和 [草稿] 都不是媒体' })],
    });
    expect(countOf(html, 'data-testid="message-media"')).toBe(0);
  });
});

describe('历史边界', () => {
  it('还有更早的一页时给按钮；正在读时按钮禁用并换文案', () => {
    const idle = renderMessages({
      messages: [message({ key: 'm1' })],
      hasMoreHistory: true,
    });
    expect(idle).toContain('data-testid="history-edge"');
    expect(idle).toContain('读取更早的消息');
    expect(idle).not.toContain('data-testid="history-start"');

    const busy = renderMessages({
      messages: [message({ key: 'm1' })],
      hasMoreHistory: true,
      loadingOlder: true,
    });
    expect(busy).toContain('正在读取更早的消息…');
    expect(busy).toContain('disabled=""');
  });

  it('到头时说清"这里是开头"（反例：一直显示"读取更早"，点了永远没结果）', () => {
    const html = renderMessages({ messages: [message({ key: 'm1' })], hasMoreHistory: false });
    expect(html).toContain('data-testid="history-start"');
    expect(html).toContain('这里是这段对话的开头');
    expect(html).not.toContain('data-testid="history-edge"');
  });
});

describe('输入区：工具默认收起，限制按需表达', () => {
  function renderComposer(busy = false): string {
    return renderToStaticMarkup(
      <Composer
        busy={busy}
        stopping={false}
        disabled={false}
        sessionId="s1"
        onSend={() => undefined}
        onStop={() => undefined}
      />,
    );
  }

  it('默认只露附件与语音两个入口，不铺时长/大小限制', () => {
    const html = renderComposer();
    expect(html).toContain('data-testid="composer-attach-toggle"');
    expect(html).toContain('data-testid="composer-audio-toggle"');
    expect(html).toContain('aria-label="消息输入框"');
    expect(html).toContain('aria-label="发送消息"');
    // 限制文案只在展开后出现——反例：把这段写死在收起态，这条会红。
    expect(html).not.toContain('10 MiB');
    expect(html).not.toContain('PNG');
    expect(countOf(html, 'data-testid="composer-panel')).toBe(0);
  });

  it('生成中给停止键，发送键禁用', () => {
    const html = renderComposer(true);
    expect(html).toContain('停止');
    expect(html).toMatch(/<button[^>]*disabled[^>]*aria-label="发送消息"/);
  });

  it('换行提示与发送提示同处一行（保留中文输入法约定）', () => {
    const html = renderComposer();
    expect(html).toContain('Enter 发送 · Shift + Enter 换行');
  });
});
