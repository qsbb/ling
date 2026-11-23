/**
 * 关系列表的**扫读层**：把一行 subject 压成「姓名 + 平台 + 账号数」。
 *
 * 这一层的存在理由是一条具体的失败：原来的卡片把
 * `napcat-2:30000000` 用 `<code>` 直接铺在正文里，一个人挂两个号就是两行等宽
 * 长串。用户想找「小明」，眼睛却要先跳过一串数字；想分清两个都叫「小明」的人，
 * 又只能去比对那串数字——**该扫读的信息（姓名）和该核对的信息（完整 ID）
 * 挤在同一层级**，两件事都做不好。
 *
 * 所以这里只产出「一眼能扫」的东西，完整 ID 留给详情：
 *
 * - `displayName`：自然人姓名，列表的首要信息。
 * - `platforms`：去重后的平台标识（`napcat-2` → 「napcat」），带账号数。
 * - `accountCount` / `verifiedCount`：扫读用的量词，不是核对用的证据。
 *
 * **不编造平台昵称**：`actor_accounts` 里没有昵称列，只有 `connector_instance`
 * 与 `external_account_id`。把账号 ID 当昵称显示，会让「平台昵称」这个标签
 * 说一句假话。这里只给「平台 + 账号」，昵称位留给真实的 `display_name`。
 */

import type { AccountRow, SubjectRow } from './types';

/** 一个人身上一个平台的账号概况。 */
export interface PlatformSummary {
  /** 平台标识（由 `connector_instance` 推导，见 {@link platformLabel}）。 */
  platform: string;
  /** 该平台下的账号数（含已解绑，解绑是事实，不该从计数里消失）。 */
  accounts: number;
  /** 其中已验证的账号数。 */
  verified: number;
}

/**
 * 实例名 → 平台标识。
 *
 * `connector_instance` 是**实例**名（`napcat-2`、`onebot-default`、`qqbot`），
 * 一个平台可以开多个实例。列表上写实例名等于把运维命名推给用户看，所以取
 * 第一段当平台名：`napcat-2` → `napcat`。
 *
 * 没有分隔符时原样返回；空串给「未知平台」而不是空标签（空标签看起来像渲染坏了）。
 */
export function platformLabel(connectorInstance: string): string {
  const raw = (connectorInstance ?? '').trim();
  if (!raw) return '未知平台';
  const head = raw.split(/[-_.:]/)[0] ?? '';
  return head || raw;
}

/** 一个账号是否算「已验证」。`pending` 与未知都不算。 */
export function isVerified(account: Pick<AccountRow, 'proof'>): boolean {
  return account.proof === 'proven' || account.proof === 'admin';
}

/**
 * 按平台聚合账号，按账号数降序（同数按平台名稳定排序）。
 *
 * 排序要**确定**：同一个人的平台徽标顺序不能因为后端返回顺序变一下就跳。
 */
export function summarizePlatforms(accounts: readonly AccountRow[]): PlatformSummary[] {
  const byPlatform = new Map<string, PlatformSummary>();
  for (const account of accounts) {
    const platform = platformLabel(account.connector_instance);
    const entry = byPlatform.get(platform) ?? { platform, accounts: 0, verified: 0 };
    entry.accounts += 1;
    if (isVerified(account)) entry.verified += 1;
    byPlatform.set(platform, entry);
  }
  return [...byPlatform.values()].sort((left, right) => {
    if (right.accounts !== left.accounts) return right.accounts - left.accounts;
    return left.platform.localeCompare(right.platform, 'zh-Hans-CN');
  });
}

/** 一行要显示的全部扫读信息。 */
export interface SubjectSummary {
  displayName: string;
  /** 没有姓名时列表要说明「还没起名」，而不是留空或显示 id。 */
  hasName: boolean;
  accountCount: number;
  verifiedCount: number;
  platforms: PlatformSummary[];
  /** 全部账号都未验证 / 有未验证账号：用于「未验证」这一真实状态。 */
  hasUnverified: boolean;
}

export function summarizeSubject(subject: SubjectRow): SubjectSummary {
  const accounts = subject.accounts ?? [];
  const verifiedCount = accounts.filter(isVerified).length;
  const name = (subject.display_name ?? '').trim();
  return {
    displayName: name || '未命名',
    hasName: name.length > 0,
    accountCount: accounts.length,
    verifiedCount,
    platforms: summarizePlatforms(accounts),
    hasUnverified: verifiedCount < accounts.length,
  };
}

/**
 * 账号的短标识：列表上只用它做**区分**，不做核对。
 *
 * 取尾部 4 位而不是开头：`30000000` 与 `30000001` 开头一样、结尾不同，
 * 截头会让两个不同的人看起来完全相同——那正是同名区分要避免的。
 */
export function accountTail(externalAccountId: string): string {
  const raw = (externalAccountId ?? '').trim();
  if (raw.length <= 4) return raw;
  return `…${raw.slice(-4)}`;
}

/**
 * 列表行怎么来：**一行就是一个主体**，被合并的记录单独成行、标注它并入了谁。
 *
 * ## 为什么不再把成员折进"一个人"
 *
 * 一开始我按"人"聚合：把 `merged_into` 指向同一目标的记录折进目标那一行，
 * 账号数合计，成员以缩进列出。这在**整页数据都在手上**时是对的，但它对
 * 服务端的真实行为提了一个不成立的前提：列表是分页的、可按 `q` 筛选的，
 * 目标那一行与它的成员**不一定同时出现在结果里**（搜索只命中目标时，成员
 * 分布在第 3 页甚至被筛掉）。于是同一份代码会给出两种自相矛盾的呈现：
 *
 * - 整页浏览时，目标行显示「并入 1 个号」并列出成员；
 * - 按目标账号搜索时，同一行变成「什么都没有」。
 *
 * 后者不是边界情况，它正是"合并完再确认"的路径。
 *
 * ## 现在的分工
 *
 * - **列表**：每个主体一行；被合并的行自己出现在列表里，写「已并入某人」。
 *   合并进来的数量由服务端给的 `merged_member_count` 表达（「已并入 N 个号」），
 *   这个数不依赖成员是否在本页。
 * - **详情**：按整组返回账号，成员与完整 ID 都在那儿。
 *
 * `orphans` 仍然保留：`merged_into` 指向本页之外的人时，它不能折进任何人，
 * 但用户必须能看到它、并拆开它。
 */
export interface PersonGroups {
  /** 未被合并的主体（真正的"人"）。 */
  groups: PersonGroup[];
  /** `merged_into` 指向不在本页的人。 */
  orphans: SubjectRow[];
}

/** 一行列表要画的东西。 */
export interface PersonGroup {
  person: SubjectRow;
  /**
   * 已并入它名下的记录**数**（来自服务端 `merged_member_count`）。
   * 成员明细不在列表里——见上面 `PersonGroups` 的说明。
   */
  mergedMemberCount: number;
}

/** 跟随 `merged_into` 走到本页内能到达的终点；成环或出页时返回 `null`。 */
function resolveWithin(rows: readonly SubjectRow[], start: SubjectRow): SubjectRow | null {
  const byId = new Map(rows.map((row) => [row.subject_id, row]));
  const seen = new Set<string>([start.subject_id]);
  let current = start;
  while (current.merged_into) {
    if (seen.has(current.merged_into)) return null;
    seen.add(current.merged_into);
    const next = byId.get(current.merged_into);
    if (!next) return null;
    current = next;
  }
  return current;
}

/**
 * 把一页身份记录分成「独立的人」与「并入本页之外的人」。
 *
 * 顺序保持输入顺序（服务端按创建时间给的稳定全序）：分组不该让列表跳。
 */
export function buildPersonGroups(rows: readonly SubjectRow[]): PersonGroups {
  const groups: PersonGroup[] = [];
  const orphans: SubjectRow[] = [];

  for (const row of rows) {
    if (!row.merged_into) {
      groups.push({ person: row, mergedMemberCount: row.merged_member_count ?? 0 });
      continue;
    }
    if (resolveWithin(rows, row)) {
      // 目标在本页：它自己会在下面作为独立的人出现，这里只标出"它并入了谁"。
      orphans.push(row);
    } else {
      orphans.push(row);
    }
  }
  return { groups, orphans };
}

/**
 * 一个自然人的账号：自己的 + 并入者的。
 *
 * 列表只显示自己的账号（并入者的明细在详情里）。这个函数保留给
 * "详情打开前先给出一个近似值"的场景，不影响列表计数。
 */
export function personAccounts(group: PersonGroup): AccountRow[] {
  return [...(group.person.accounts ?? [])];
}

/**
 * 同名区分：这一行与**同名的其他人**靠什么分开。
 *
 * 返回的是给人读的短语，例如「napcat …0001 / onebot …0002」。没有同名者时返回
 * 空数组——列表就不显示任何区分文案（区分只在需要时才占位置）。
 *
 * `others` 传的是**同一页里**同名的其他人；跨页的同名由搜索与合并确认负责，
 * 这里只解决「一眼看过去两行一模一样」的问题。
 */
export function distinctHints(
  subject: SubjectRow,
  others: readonly SubjectRow[],
): string[] {
  const name = (subject.display_name ?? '').trim();
  if (!name) return [];
  const sameName = others.filter(
    (other) => other.subject_id !== subject.subject_id
      && (other.display_name ?? '').trim() === name,
  );
  if (sameName.length === 0) return [];

  /*
   * 只用**账号尾号**当区分点，不再重复平台名：平台已经在上面的徽标里写过一次，
   * 这里再写「napcat …0000」等于同一信息说两遍。若这一行的账号跨多个平台、
   * 尾号又可能撞，才把平台带上——那种情况下平台才是真正的区分依据。
   */
  const platforms = new Set(
    (subject.accounts ?? []).map((account) => platformLabel(account.connector_instance)),
  );
  const usePlatform = platforms.size > 1;
  const own = (subject.accounts ?? []).map((account) => (usePlatform
    ? `${platformLabel(account.connector_instance)} ${accountTail(account.external_account_id)}`
    : accountTail(account.external_account_id)));
  if (own.length === 0) return ['没有绑定账号'];

  // 只保留**能把自己和同名者分开**的那些账号：若两个人都有 `napcat`，
  // 把「napcat」当区分点是在误导。
  const othersKeys = new Set(
    sameName.flatMap((other) => (other.accounts ?? []).map(
      (account) => (usePlatform
        ? `${platformLabel(account.connector_instance)} ${accountTail(account.external_account_id)}`
        : accountTail(account.external_account_id)),
    )),
  );
  const unique = own.filter((key) => !othersKeys.has(key));
  return unique.length > 0 ? unique : own;
}
