import { describe, expect, it } from 'vitest';
import {
  accountTail,
  buildPersonGroups,
  distinctHints,
  isVerified,
  personAccounts,
  platformLabel,
  summarizePlatforms,
  summarizeSubject,
} from './relationshipSummary';
import type { AccountRow, SubjectRow } from './types';

function account(over: Partial<AccountRow> = {}): AccountRow {
  return {
    account_id: 'acc-1',
    connector_instance: 'napcat-2',
    external_account_id: '30000001',
    proof: 'proven',
    revoked: false,
    ...over,
  };
}

function subject(over: Partial<SubjectRow> = {}): SubjectRow {
  return {
    subject_id: 'subj_0001',
    display_name: '小明',
    trust_level: 'known',
    merged_into: null,
    accounts: [account()],
    ...over,
  };
}

describe('platformLabel', () => {
  it('实例名取平台段，不带实例序号', () => {
    expect(platformLabel('napcat-2')).toBe('napcat');
    expect(platformLabel('onebot-default')).toBe('onebot');
    expect(platformLabel('qqbot')).toBe('qqbot');
  });

  it('没有分隔符时原样返回；空串给一个可读的兜底而不是空标签', () => {
    expect(platformLabel('wechat')).toBe('wechat');
    expect(platformLabel('')).toBe('未知平台');
    expect(platformLabel('   ')).toBe('未知平台');
  });

  it('下划线、点、冒号也当分隔符（实例命名不止一种）', () => {
    expect(platformLabel('napcat.prod')).toBe('napcat');
    expect(platformLabel('onebot:cn')).toBe('onebot');
    expect(platformLabel('wechat_work')).toBe('wechat');
  });
});

describe('isVerified', () => {
  it('proven 与 admin 算已验证', () => {
    expect(isVerified({ proof: 'proven' })).toBe(true);
    expect(isVerified({ proof: 'admin' })).toBe(true);
  });

  it('pending 与未知都不算——「未验证」必须是真实状态而不是默认值', () => {
    expect(isVerified({ proof: 'pending' })).toBe(false);
    expect(isVerified({ proof: '' })).toBe(false);
    expect(isVerified({ proof: 'something-new' })).toBe(false);
  });
});

describe('summarizePlatforms', () => {
  it('按平台聚合，并把多实例的同平台账号算在一起', () => {
    const summary = summarizePlatforms([
      account({ connector_instance: 'napcat-2', external_account_id: '1' }),
      account({ connector_instance: 'napcat-3', external_account_id: '2' }),
      account({ connector_instance: 'onebot-default', external_account_id: '3', proof: 'pending' }),
    ]);
    expect(summary).toEqual([
      { platform: 'napcat', accounts: 2, verified: 2 },
      { platform: 'onebot', accounts: 1, verified: 0 },
    ]);
  });

  it('同账号数时按平台名稳定排序：后端顺序变一下，徽标不能跟着跳', () => {
    const a = summarizePlatforms([
      account({ connector_instance: 'onebot-default', external_account_id: '1' }),
      account({ connector_instance: 'napcat-2', external_account_id: '2' }),
    ]);
    const b = summarizePlatforms([
      account({ connector_instance: 'napcat-2', external_account_id: '2' }),
      account({ connector_instance: 'onebot-default', external_account_id: '1' }),
    ]);
    expect(a).toEqual(b);
    expect(a.map((row) => row.platform)).toEqual(['napcat', 'onebot']);
  });

  it('已解绑的账号仍计数：解绑是事实，不该从"他有几个号"里消失', () => {
    const summary = summarizePlatforms([account({ revoked: true })]);
    expect(summary).toEqual([{ platform: 'napcat', accounts: 1, verified: 1 }]);
  });

  it('没有账号时是空数组，不是一条空平台', () => {
    expect(summarizePlatforms([])).toEqual([]);
  });
});

describe('summarizeSubject', () => {
  it('姓名是首要信息，账号数与验证数是扫读量词', () => {
    const summary = summarizeSubject(subject({
      accounts: [
        account({ account_id: 'a', external_account_id: '1' }),
        account({ account_id: 'b', external_account_id: '2', proof: 'pending' }),
      ],
    }));
    expect(summary.displayName).toBe('小明');
    expect(summary.hasName).toBe(true);
    expect(summary.accountCount).toBe(2);
    expect(summary.verifiedCount).toBe(1);
    expect(summary.hasUnverified).toBe(true);
  });

  it('没有姓名时明确说未命名，而不是回落到 id 或留空', () => {
    const summary = summarizeSubject(subject({ display_name: '' }));
    expect(summary.displayName).toBe('未命名');
    expect(summary.hasName).toBe(false);
  });

  it('全空白的姓名按未命名处理（后端 trim 之前的历史数据）', () => {
    expect(summarizeSubject(subject({ display_name: '   ' })).hasName).toBe(false);
  });

  it('没有账号的人 hasUnverified 为 false：没有账号不等于"有未验证账号"', () => {
    const summary = summarizeSubject(subject({ accounts: [] }));
    expect(summary.accountCount).toBe(0);
    expect(summary.hasUnverified).toBe(false);
  });

  it('accounts 缺失（旧响应）不炸，按空处理', () => {
    const row = subject();
    // 老版本响应没有 accounts 字段
    const summary = summarizeSubject({ ...row, accounts: undefined as unknown as AccountRow[] });
    expect(summary.accountCount).toBe(0);
  });
});

describe('accountTail', () => {
  it('取尾部：两个账号开头一样、结尾不同时必须能区分', () => {
    expect(accountTail('30000001')).toBe('…0001');
    expect(accountTail('30000002')).toBe('…0002');
    expect(accountTail('30000001')).not.toBe(accountTail('30000002'));
  });

  it('短账号原样返回，不加省略号', () => {
    expect(accountTail('42')).toBe('42');
    expect(accountTail('abcd')).toBe('abcd');
  });
});

describe('distinctHints', () => {
  it('没有同名者时不给任何区分文案（不需要时不占位置）', () => {
    const me = subject({ subject_id: 'a' });
    const other = subject({ subject_id: 'b', display_name: '小红' });
    expect(distinctHints(me, [me, other])).toEqual([]);
  });

  it('同名时用账号尾号区分；平台已在徽标里，不重复写', () => {
    const me = subject({ subject_id: 'a', accounts: [account({ external_account_id: '30000001' })] });
    const other = subject({ subject_id: 'b', accounts: [account({ external_account_id: '30000002' })] });
    expect(distinctHints(me, [me, other])).toEqual(['…0001']);
  });

  it('一个人跨多个平台时才把平台带进区分提示（那时平台才是区分依据）', () => {
    const me = subject({
      subject_id: 'a',
      accounts: [
        account({ account_id: 'a1', connector_instance: 'napcat-2', external_account_id: '30000001' }),
        account({ account_id: 'a2', connector_instance: 'qqbot', external_account_id: '50001' }),
      ],
    });
    const other = subject({ subject_id: 'b', accounts: [account({ external_account_id: '30000002' })] });
    expect(distinctHints(me, [me, other])).toEqual(['napcat …0001', 'qqbot …0001'].slice(0, 2));
  });

  it('两个人都挂同一个号时，不把"相同的号"当区分点', () => {
    const me = subject({
      subject_id: 'a',
      accounts: [
        account({ account_id: 'a1', external_account_id: '30000001' }),
        account({ account_id: 'a2', connector_instance: 'onebot-default', external_account_id: '9001' }),
      ],
    });
    const other = subject({
      subject_id: 'b',
      accounts: [account({ account_id: 'b1', external_account_id: '30000001' })],
    });
    // 跨平台 → 带上平台；napcat …0001 两边都有，只剩 onebot 那个号
    expect(distinctHints(me, [me, other])).toEqual(['onebot 9001']);
  });

  it('完全没有交集可用时，仍把全部账号列出来（总比两行一模一样强）', () => {
    const me = subject({
      subject_id: 'a',
      accounts: [account({ account_id: 'a1', external_account_id: '30000001' })],
    });
    const other = subject({
      subject_id: 'b',
      accounts: [account({ account_id: 'b1', external_account_id: '30000001' })],
    });
    expect(distinctHints(me, [me, other])).toEqual(['…0001']);
  });

  it('同名但没有账号的人，说明"没有绑定账号"', () => {
    const me = subject({ subject_id: 'a', accounts: [] });
    const other = subject({ subject_id: 'b' });
    expect(distinctHints(me, [me, other])).toEqual(['没有绑定账号']);
  });

  it('没有姓名的人不参与同名区分', () => {
    const me = subject({ subject_id: 'a', display_name: '' });
    const other = subject({ subject_id: 'b', display_name: '' });
    expect(distinctHints(me, [me, other])).toEqual([]);
  });
});

describe('buildPersonGroups', () => {
  const person = (id: string, over: Partial<SubjectRow> = {}): SubjectRow => subject({
    subject_id: id,
    display_name: id,
    accounts: [account({ account_id: `acc-${id}`, external_account_id: id })],
    ...over,
  });

  it('未合并的人是独立的一组，顺序与输入一致', () => {
    const rows = [person('a'), person('b')];
    const { groups, orphans } = buildPersonGroups(rows);
    expect(groups.map((g) => g.person.subject_id)).toEqual(['a', 'b']);
    expect(orphans).toEqual([]);
  });

  it('并入的记录不折进目标行：它自己出现在 orphans 里，可被拆开', () => {
    const target = person('target');
    const source = person('source', { merged_into: 'target' });
    const { groups, orphans } = buildPersonGroups([target, source]);
    expect(groups.map((g) => g.person.subject_id)).toEqual(['target']);
    // 成员**不**出现在目标行的 group 里：列表不再展开成员
    expect(groups[0]).not.toHaveProperty('mergedMembers');
    expect(orphans.map((row) => row.subject_id)).toEqual(['source']);
  });

  it('目标在别的页时进 orphans，不猜它属于谁', () => {
    const source = person('source', { merged_into: 'subj_on_page_3' });
    const other = person('other');
    const { groups, orphans } = buildPersonGroups([source, other]);
    expect(groups.map((g) => g.person.subject_id)).toEqual(['other']);
    expect(orphans.map((row) => row.subject_id)).toEqual(['source']);
  });

  it('成环不吞记录：环上的行进 orphans，而不是无限循环或静默丢弃', () => {
    const a = person('a', { merged_into: 'b' });
    const b = person('b', { merged_into: 'a' });
    const { groups, orphans } = buildPersonGroups([a, b]);
    expect(groups).toEqual([]);
    expect(orphans.map((row) => row.subject_id).sort()).toEqual(['a', 'b']);
  });

  it('并入数量取自服务端的 merged_member_count，不依赖成员是否在本页', () => {
    const target = person('target', { merged_member_count: 3 });
    const { groups } = buildPersonGroups([target]);
    expect(groups[0]?.mergedMemberCount).toBe(3);
  });

  it('服务端没给计数时按 0 处理（旧响应不炸）', () => {
    const { groups } = buildPersonGroups([person('a')]);
    expect(groups[0]?.mergedMemberCount).toBe(0);
  });

  it('空输入给空结果', () => {
    expect(buildPersonGroups([])).toEqual({ groups: [], orphans: [] });
  });
});

describe('personAccounts', () => {
  it('只返回这个主体自己的账号（并入者的明细在详情里）', () => {
    const target = subject({
      subject_id: 'target',
      accounts: [account({ account_id: 't1', external_account_id: '1111' })],
    });
    const { groups } = buildPersonGroups([target]);
    expect(personAccounts(groups[0]!).map((row) => row.account_id)).toEqual(['t1']);
  });
});
