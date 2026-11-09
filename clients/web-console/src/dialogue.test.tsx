/**
 * 对话页「谁在哪个群说」的渲染断言（用户需求第 8 条）。
 *
 * 这里保护的是**语义**，不是像素：
 *
 * - 渠道会话的标题必须是能读的名字（群名/昵称），机器键只能当副标题；
 *   名字读不到时按号码回退成「群 100200400」，绝不把裸 subject 键当标题；
 * - 群成员的显示名和号码必须分层，且不同的人不能被合并成一条署名；
 * - 她自己的回复（`role=actor`）不挂发言人行；
 * - 「她没回」的原因只在一段连续相同原因的最后一条说一次，并且与时间分开。
 *
 * 与 `connectors.test.tsx` / `model-settings.test.tsx` 一致，不引入 DOM 测试库：
 * 用 `react-dom/server` 渲染成字符串断言。
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import {
  buildDialogueRows,
  MessageList,
  speakerLabel,
  statusText,
} from './components/MessageList';
import {
  canRenameSession,
  describeSession,
  parseChannelRef,
  SessionSidebar,
} from './components/SessionSidebar';
import type { SessionRecord, UserInfo } from './types';
import type { UiMessage } from './useChat';

// `usePersonaName` 会请求 /v1/persona；渲染断言里不需要网络，固定成「灵」。
vi.mock('./personaName', () => ({ usePersonaName: () => '灵' }));

const GROUP_REF = 'onebot-default:group:100200400';
const PRIVATE_REF = 'onebot-default:private:100200301';
const CHAT_TIME = '2026-11-07T04:23:00Z';

function sessionFixture(patch: Partial<SessionRecord> & { session_id: string }): SessionRecord {
  return {
    schema_version: 'v1',
    actor_id: 'actor_ling',
    subject_id: 'subject_owner',
    window_id: patch.session_id,
    title: '新的对话',
    created_at: '2026-11-06T01:00:00Z',
    updated_at: '2026-11-07T04:23:00Z',
    message_count: 12,
    archived: false,
    ...patch,
  };
}

function messageFixture(patch: Partial<UiMessage> & { key: string }): UiMessage {
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
    recalledAt: null,
    recalledByActor: false,
    ...rest,
  };
}

const USER: UserInfo = {
  id: 'user-1',
  username: 'owner',
  role: 'owner',
  subject_id: 'subject_owner',
  display_name: '主人',
};

/** 从渲染结果里取会话标题（避免把副标题里的机器键也匹配进来）。 */
function sessionTitles(html: string): string[] {
  return [...html.matchAll(/<span class="session__title"[^>]*>([^<]*)<\/span>/g)].map(
    (matched) => matched[1] ?? '',
  );
}

function renderSidebar(sessions: SessionRecord[]): string {
  return renderToStaticMarkup(
    <SessionSidebar
      sessions={sessions}
      currentId={null}
      open={false}
      user={USER}
      onClose={() => undefined}
      onSelect={() => undefined}
      onCreate={() => undefined}
      onRename={async () => undefined}
      onNavigateSettings={() => undefined}
      onNavigatePersona={() => undefined}
      onLogout={() => undefined}
    />,
  );
}

/**
 * 按 `<li>` 切开渲染结果，一条消息一片。
 *
 * 断言"这一行是左还是右"必须落在**行自身**的 class 上：直接 `indexOf` 再
 * 前后截 200 字，会截到相邻行的 class 里去（改左右后第一次就是这么误判的）。
 */
function liChunks(html: string): string[] {
  return html
    .split(/<li class="/)
    .slice(1)
    .map((chunk) => chunk);
}

function renderMessages(messages: UiMessage[], variant: 'web' | 'group' | 'private'): string {
  return renderToStaticMarkup(
    <MessageList
      messages={messages}
      activeTurn={null}
      live={null}
      loading={false}
      variant={variant}
      onRetry={() => undefined}
      onResend={() => undefined}
      onStop={() => undefined}
      onOpenSettings={() => undefined}
    />,
  );
}

function countOf(html: string, needle: string): number {
  return html.split(needle).length - 1;
}

describe('会话命名：群名作标题，机器键只当副标题', () => {
  it('display_name 存在时直接当标题，完整会话键退居副标题', () => {
    const heading = describeSession(
      sessionFixture({
        session_id: 'win_group',
        title: GROUP_REF,
        display_name: '阿懒的家',
        channel: 'onebot-default',
        peer_id: '100200400',
      }),
    );
    expect(heading).toEqual({
      title: '阿懒的家',
      subtitle: GROUP_REF,
      kind: 'group',
      badge: '群',
    });
  });

  it('读不到群名时按号码回退，绝不把裸机器键当标题', () => {
    const heading = describeSession(
      sessionFixture({ session_id: 'win_group', title: GROUP_REF, display_name: null }),
    );
    expect(heading.title).toBe('群 100200400');
    expect(heading.title).not.toContain('onebot');
    expect(heading.subtitle).toBe(GROUP_REF);
    expect(heading.badge).toBe('群');
  });

  it('私聊同样回退成「私聊 <QQ号>」，纯空白名字也算没有名字', () => {
    const heading = describeSession(
      sessionFixture({ session_id: 'win_private', title: PRIVATE_REF, display_name: '   ' }),
    );
    expect(heading.title).toBe('私聊 100200301');
    expect(heading.kind).toBe('private');
    expect(heading.subtitle).toBe(PRIVATE_REF);
  });

  it('display_name 误传成窗口键时也当没名字，换成号码回退', () => {
    const heading = describeSession(
      sessionFixture({ session_id: 'win_g', title: GROUP_REF, display_name: GROUP_REF }),
    );
    expect(heading.title).toBe('群 100200400');
  });

  it('服务端给的 peer_id 优先于从机器键里抠出来的号码', () => {
    const heading = describeSession(
      sessionFixture({ session_id: 'win_group', title: GROUP_REF, peer_id: '999888777' }),
    );
    expect(heading.title).toBe('群 999888777');
  });

  it('web 会话保留用户起的标题，副标题是会话 id', () => {
    const heading = describeSession(
      sessionFixture({ session_id: 'win_web', title: '深夜聊天', channel: 'web' }),
    );
    expect(heading).toEqual({
      title: '深夜聊天',
      subtitle: 'win_web',
      kind: 'web',
      badge: '网页',
    });
  });

  it('只有 channel 字段没有窗口键时也能认出群/私聊', () => {
    expect(
      describeSession(
        sessionFixture({ session_id: 'win_x', title: '被改过名的群', channel: 'group' }),
      ).kind,
    ).toBe('group');
    expect(
      describeSession(
        sessionFixture({ session_id: 'win_y', title: '被改过名的私聊', channel: 'private' }),
      ).kind,
    ).toBe('private');
  });

  it('渠道窗口的标题被人改过、拆不出群/私聊时，仍按渠道会话排版', () => {
    const heading = describeSession(
      sessionFixture({
        session_id: 'win_old',
        title: '我给它改的名字',
        channel: 'onebot-default',
        peer_id: '100200400',
      }),
    );
    expect(heading).toEqual({
      title: '我给它改的名字',
      subtitle: 'win_old',
      kind: 'channel',
      badge: '渠道',
    });
    expect(canRenameSession(sessionFixture({ session_id: 'win_old', title: '我给它改的名字', peer_id: '1' }))).toBe(false);
  });

  it('渠道窗口不允许在界面上改名（改标题会让 bridge 找不到同一个群）', () => {
    expect(canRenameSession(sessionFixture({ session_id: 'win_g', title: GROUP_REF }))).toBe(false);
    expect(canRenameSession(sessionFixture({ session_id: 'win_w', title: '深夜聊天' }))).toBe(true);
  });

  it('parseChannelRef 只认 <实例>:group|private:<号> 的键', () => {
    expect(parseChannelRef(GROUP_REF)).toEqual({
      raw: GROUP_REF,
      instance: 'onebot-default',
      kind: 'group',
      peerId: '100200400',
    });
    expect(parseChannelRef('深夜聊天')).toBeNull();
    expect(parseChannelRef('onebot-default:channel:123')).toBeNull();
    expect(parseChannelRef(null)).toBeNull();
  });
});

describe('会话列表：标题、副标题、渠道标签、键盘可达', () => {
  it('列表项的标题是群名/回退名，副标题才是完整会话键', () => {
    const html = renderSidebar([
      sessionFixture({
        session_id: 'win_group',
        title: GROUP_REF,
        display_name: '阿懒的家',
        channel: 'onebot-default',
        peer_id: '100200400',
      }),
      sessionFixture({ session_id: 'win_private', title: PRIVATE_REF }),
      sessionFixture({ session_id: 'win_web', title: '深夜聊天', channel: 'web' }),
    ]);

    expect(sessionTitles(html)).toEqual(['阿懒的家', '私聊 100200301', '深夜聊天']);
    // 悬停拿全量：标题与副标题都带 title 属性
    expect(html).toContain('title="阿懒的家"');
    expect(html).toContain(`title="${GROUP_REF}"`);
    expect(html).toContain(`title="${PRIVATE_REF}"`);
    // 群/私聊/网页三种会话在列表里一眼可分
    expect(countOf(html, 'class="session__channel"')).toBe(3);
    expect(html).toContain('>群<');
    expect(html).toContain('>私聊<');
    expect(html).toContain('>网页<');
  });

  it('列表项与会话按钮都是原生 button，且渠道会话没有改名入口', () => {
    const html = renderSidebar([
      sessionFixture({ session_id: 'win_group', title: GROUP_REF }),
      sessionFixture({ session_id: 'win_web', title: '深夜聊天', channel: 'web' }),
    ]);
    expect(countOf(html, '<button type="button" class="session__main"')).toBe(2);
    // 只有网页会话能改名，且改名按钮本身是 button
    expect(countOf(html, 'aria-label="改名：')).toBe(1);
    expect(html).toContain('aria-label="改名：深夜聊天"');
  });
});

describe('发言人分层', () => {
  it('群成员名字与号码分成两项；号码不重复、不加 @', () => {
    const label = speakerLabel(
      messageFixture({ key: 'm1', senderName: '阿青', senderId: '100200302' }),
      'group',
    );
    expect(label).toEqual({ name: '阿青', id: '100200302' });
  });

  it('没有群名片时只报号码，不编名字', () => {
    expect(
      speakerLabel(messageFixture({ key: 'm1', senderName: '  ', senderId: '100200300' }), 'group'),
    ).toEqual({ name: '成员 100200300', id: null });
    expect(speakerLabel(messageFixture({ key: 'm1' }), 'group')).toBeNull();
  });

  it('她自己的话与网页自己发的话都没有发言人', () => {
    expect(
      speakerLabel(
        messageFixture({ key: 'a1', role: 'actor', senderName: '灵', senderId: '0' }),
        'group',
      ),
    ).toBeNull();
    expect(
      speakerLabel(messageFixture({ key: 'u1', senderName: '主人', senderId: 'web' }), 'web'),
    ).toBeNull();
  });

  it('同一个人连续多条只署一次，换人一定重新署名', () => {
    const rows = buildDialogueRows(
      [
        messageFixture({ key: 'm1', senderName: '阿青', senderId: '100200302' }),
        messageFixture({ key: 'm2', senderName: '阿青', senderId: '100200302' }),
        messageFixture({ key: 'm3', senderName: '阿青', senderId: '100200302' }),
        messageFixture({ key: 'm4', senderName: '阿白', senderId: '100200300' }),
        messageFixture({ key: 'a1', role: 'actor', text: '嗯' }),
        messageFixture({ key: 'm5', senderName: '阿青', senderId: '100200302' }),
      ],
      'group',
    );
    expect(rows.map((row) => row.showSpeaker)).toEqual([true, false, false, true, false, true]);
    expect(rows.filter((row) => row.inbound).map((row) => row.speaker?.name)).toEqual([
      '阿青',
      '阿青',
      '阿青',
      '阿白',
      '阿青',
    ]);
  });

  it('同名不同号码不会被合并：署名键带号码', () => {
    const rows = buildDialogueRows(
      [
        messageFixture({ key: 'm1', senderName: '阿懒', senderId: '100200300' }),
        messageFixture({ key: 'm2', senderName: '阿懒', senderId: '400500600' }),
      ],
      'group',
    );
    expect(rows.map((row) => row.showSpeaker)).toEqual([true, true]);
  });

  it('左右只按"谁说的"分：她自己（actor）在右，别人（user）在左——网页会话', () => {
    const rows = buildDialogueRows(
      [messageFixture({ key: 'u1', text: '在吗' }), messageFixture({ key: 'a1', role: 'actor' })],
      'web',
    );
    // 用户原话：左边是其他人，右边是 bot 自己。这一条以前是反的。
    expect(rows[0]?.own).toBe(false);
    expect(rows[0]?.inbound).toBe(false);
    expect(rows[1]?.own).toBe(true);
    expect(rows[0]?.speaker).toBeNull();
  });

  it('渠道会话同一套规则：她的回复在右，群成员的发言在左', () => {
    const rows = buildDialogueRows(
      [
        messageFixture({ key: 'm1', senderName: '阿懒', senderId: '100200300' }),
        messageFixture({ key: 'a1', role: 'actor', text: '嗯' }),
      ],
      'group',
    );
    // 以前群会话里 user 与 actor 全都靠左（row.own 恒 false），分不出谁是谁
    expect(rows.map((row) => row.own)).toEqual([false, true]);
    expect(rows[0]?.inbound).toBe(true);
    expect(rows[1]?.speaker).toBeNull();
  });
});

describe('沉默原因降噪', () => {
  const REASON = '群聊里的普通聊天，没有指向角色，不搭话';

  it('同一段连续相同原因只在最后一条说一次，并标出条数', () => {
    const rows = buildDialogueRows(
      [
        messageFixture({ key: 'm1', silenceReason: REASON }),
        messageFixture({ key: 'm2', silenceReason: REASON }),
        messageFixture({ key: 'm3', silenceReason: REASON }),
      ],
      'group',
    );
    expect(rows.map((row) => row.silence?.run ?? null)).toEqual([null, null, 3]);
    expect(rows[2]?.silence?.text).toBe(REASON);
  });

  it('中间夹着她的一句回复就断段：两段各自说明', () => {
    const rows = buildDialogueRows(
      [
        messageFixture({ key: 'm1', silenceReason: REASON }),
        messageFixture({ key: 'a1', role: 'actor', text: '嗯' }),
        messageFixture({ key: 'm2', silenceReason: REASON }),
      ],
      'group',
    );
    expect(rows[0]?.silence).toEqual({ text: REASON, run: 1 });
    expect(rows[1]?.silence).toBeNull();
    expect(rows[2]?.silence).toEqual({ text: REASON, run: 1 });
  });

  it('原因只是空白/换行不同也算同一段', () => {
    const rows = buildDialogueRows(
      [
        messageFixture({ key: 'm1', silenceReason: `  ${REASON.replace('，', '，\n')}  ` }),
        messageFixture({ key: 'm2', silenceReason: REASON.replace('，', '，   ') }),
      ],
      'group',
    );
    expect(rows[1]?.silence?.run).toBe(2);
    expect(rows[0]?.silence).toBeNull();
  });

  it('不同原因不合并', () => {
    const rows = buildDialogueRows(
      [
        messageFixture({ key: 'm1', silenceReason: REASON }),
        messageFixture({ key: 'm2', silenceReason: '在休息，先不回' }),
      ],
      'group',
    );
    expect(rows[0]?.silence?.run).toBe(1);
    expect(rows[1]?.silence?.run).toBe(1);
  });
});

describe('状态文案：渠道会话不再逐条刷「已送达」', () => {
  it('群里的消息不显示送达状态，网页自己的消息才显示', () => {
    const message = messageFixture({ key: 'm1' });
    expect(statusText(message, null, 'group')).toBe('');
    expect(statusText(message, null, 'web')).toBe('已送达');
  });

  it('失败与停止在渠道会话里仍要说清楚', () => {
    expect(statusText(messageFixture({ key: 'f1', state: 'failed' }), null, 'group')).toBe('发送失败');
    expect(statusText(messageFixture({ key: 's1', state: 'stopped' }), null, 'group')).toBe('已停止');
  });
});

describe('消息列表渲染', () => {
  const GROUP_MESSAGES: UiMessage[] = [
    messageFixture({
      key: 'm1',
      text: '在吗',
      senderName: '阿青',
      senderId: '100200302',
      silenceReason: '群聊里的普通聊天，没有指向角色，不搭话',
    }),
    messageFixture({
      key: 'm2',
      text: '今天好热',
      senderName: '阿青',
      senderId: '100200302',
      silenceReason: '群聊里的普通聊天，没有指向角色，不搭话',
    }),
    messageFixture({ key: 'a1', role: 'actor', text: '嗯，开空调', state: 'completed' }),
    messageFixture({ key: 'm3', text: '我也在', senderName: '阿白', senderId: '100200300' }),
  ];

  it('群消息署名：名字与号码分两行、不同人分开署名、她的回复不署名', () => {
    const html = renderMessages(GROUP_MESSAGES, 'group');

    // 两处署名：阿青（连续两条只署一次）+ 阿白
    expect(countOf(html, 'class="message__sender"')).toBe(2);
    expect(html).toContain('>阿青<');
    expect(html).toContain('>100200302<');
    expect(html).toContain('>阿白<');
    expect(html).toContain('>100200300<');
    // 不能再出现「@号码 名字」挤在一起的老样子
    expect(html).not.toContain('@100200302');
    expect(html).not.toContain('@阿青');
    // 她自己的气泡没有发言人行，但头像仍标明是她
    expect(countOf(html, 'aria-label="灵"')).toBeGreaterThan(0);
    expect(html).not.toContain('class="message__sender-name" title="灵"');
  });

  it('重复原因只在段末出现一次，且与时间分层（时间行在前，原因是独立一段）', () => {
    const html = renderMessages(GROUP_MESSAGES, 'group');
    expect(countOf(html, 'data-testid="silence-reason"')).toBe(1);
    expect(html).toContain('data-run="2"');
    expect(html).toContain('连续 2 条');
    expect(html).toContain('她没回');

    const timeAt = html.indexOf('class="message__time"');
    const silenceAt = html.indexOf('data-testid="silence-reason"');
    expect(timeAt).toBeGreaterThan(-1);
    expect(silenceAt).toBeGreaterThan(timeAt);
    // 群里不再逐条刷「已送达」
    expect(html).not.toContain('已送达');
  });

  it('网页会话：她的回复在右（message--own / row-reverse），我的话在左', () => {
    const html = renderMessages(
      [
        messageFixture({ key: 'u1', text: '在吗', senderName: '主人', senderId: 'web' }),
        messageFixture({ key: 'a1', role: 'actor', text: '在', state: 'completed' }),
      ],
      'web',
    );
    expect(html).not.toContain('message__sender');
    // 她的行：靠右那一套；我的行：靠左那一套
    const rows = liChunks(html);
    const actorRow = rows.find((row) => row.includes('data-role="actor"')) ?? '';
    const userRow = rows.find((row) => row.includes('data-role="user"')) ?? '';
    expect(actorRow).toContain('message--own');
    expect(userRow).toContain('message--inbound');
    expect(userRow).not.toContain('message--own');
    // `data-role` 不变（选择器契约）
    expect(html).toContain('data-role="actor"');
    expect(html).toContain('data-role="user"');
  });

  it('她的消息在右时头像仍在行内（与气泡同侧）且 aria-label 是她', () => {
    const html = renderMessages(
      [messageFixture({ key: 'a1', role: 'actor', text: '在', state: 'completed' })],
      'web',
    );
    expect(html).toContain('aria-label="灵"');
  });

  it('群会话左右：她的话在右，群成员在左（同一份 DOM 两种方向）', () => {
    const html = renderMessages(
      [
        messageFixture({ key: 'm1', text: '在吗', senderName: '阿懒', senderId: '100200300' }),
        messageFixture({ key: 'a1', role: 'actor', text: '在', state: 'completed' }),
      ],
      'group',
    );
    const rows = liChunks(html);
    const member = rows.find((row) => row.includes('data-role="user"')) ?? '';
    const actor = rows.find((row) => row.includes('data-role="actor"')) ?? '';
    expect(member).toContain('message--inbound');
    expect(actor).toContain('message--own');
    // 群成员的在左、她的在右：DOM 里没有左右信息（顺序是稳定的阅读顺序），
    // 左右只由 CSS 决定——这里钉住两类行拿到了相反的 class。
    expect(member).not.toContain('message--own');
  });

  it('没有群名片时签名只用号码兜底，不出现空名字行', () => {
    const html = renderMessages(
      [messageFixture({ key: 'm1', text: '喂', senderId: '100200300' })],
      'group',
    );
    expect(html).toContain('成员 100200300');
    expect(countOf(html, 'class="message__sender-name"')).toBe(1);
    expect(countOf(html, 'class="message__sender-id"')).toBe(0);
  });
});
