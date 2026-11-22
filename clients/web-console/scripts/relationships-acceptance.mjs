/**
 * 关系页（图六）真实浏览器验收。
 *
 * 与 `src/identity.browser.test.tsx` 的区别：那个文件用 `page.route` **模拟**接口，
 * 追求的是"断言快而稳"；这个脚本**连真后端**（真实 PostgreSQL、真实分页、
 * 真实合并/改名/拆开），追求的是"这条流程在真系统里真的走得通"。
 *
 * 它不复用测试的 mock，所以能抓到一类 mock 抓不到的问题：
 * 前端假设的响应字段与后端真实返回不一致、真实数据下同名/跨平台的区分是否
 * 真的够用、分页边界上"人不在这一页"能不能搜到。
 *
 * 用法（后端已在 --base 上运行，且已灌入合成数据）：
 *
 *   node .acceptance/relationships.mjs \
 *     --base http://127.0.0.1:18091 \
 *     --user ling --password ling-accept-password \
 *     --out /tmp/ling-rel-shots
 */

import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

const args = Object.fromEntries(
  process.argv.slice(2).flatMap((arg, index, all) => {
    if (!arg.startsWith('--')) return [];
    const key = arg.slice(2);
    const value = all[index + 1] && !all[index + 1].startsWith('--') ? all[index + 1] : 'true';
    return [[key, value]];
  }),
);

const BASE = args.base ?? 'http://127.0.0.1:18091';
const USER = args.user ?? 'ling';
const PASSWORD = args.password ?? 'ling-accept-password';
const OUT = args.out ?? '/tmp/ling-rel-shots';

const steps = [];
function record(name, detail = {}) {
  const entry = { step: name, at: new Date().toISOString(), ...detail };
  steps.push(entry);
  console.log(`[acceptance] ${name}`, JSON.stringify(detail));
}


/**
 * 等"搜索后的结果稳定"。
 *
 * `waitFor()` 只等元素**存在**：搜索时列表会被卸载再重建，而重建前那一帧
 * `count()` 可能正好是 0 —— `waitFor` 却在元素重新出现的一瞬间返回，
 * 于是后面的 `count()` 读到 0，把"暂时为空"误判成"搜不到"。
 *
 * 所以这里等的是**结果本身稳定**：连续两次采样的卡片数与计数文案一致。
 */
async function waitForSettledList(page, timeoutMs = 20_000) {
  const started = Date.now();
  let previous = null;
  while (Date.now() - started < timeoutMs) {
    const snapshot = await page.evaluate(() => ({
      cards: document.querySelectorAll('[data-testid="subject-card"]').length,
      count: document.querySelector('[data-testid="subjects-count"]')?.textContent?.trim() ?? null,
      loading: Boolean(document.querySelector('[data-testid="subjects-loading"]')),
      empty: Boolean(document.querySelector('[data-testid="subjects-empty"]')),
    }));
    if (!snapshot.loading && previous
        && previous.cards === snapshot.cards
        && previous.count === snapshot.count) {
      return snapshot;
    }
    previous = snapshot;
    await page.waitForTimeout(150);
  }
  throw new Error('等搜索结果稳定超时');
}

/** 等某个弹层消失（`waitFor({ state: 'detached' })` 对 dialog 更直接）。 */
async function expectDialogGone(page, testId, timeoutMs = 10_000) {
  await page.waitForFunction(
    (id) => !document.querySelector(`[data-testid="${id}"]`),
    testId,
    { timeout: timeoutMs },
  );
}

async function shot(page, name, options = {}) {
  const file = path.join(OUT, `${name}.png`);
  await page.screenshot({ path: file, fullPage: options.fullPage ?? false });
  console.log(`[acceptance] screenshot -> ${file}`);
  return file;
}

async function main() {
  await mkdir(OUT, { recursive: true });
  const browser = await chromium.launch();
  const log = [];

  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  const page = await context.newPage();
  page.on('console', (message) => {
    if (message.type() === 'error') log.push(`console.error: ${message.text()}`);
  });
  page.on('pageerror', (error) => log.push(`pageerror: ${error.message}`));

  // 登录：走真实 API，拿到 cookie 再进页面
  const loginResponse = await page.request.post(`${BASE}/v1/auth/login`, {
    data: { username: USER, password: PASSWORD },
  });
  if (!loginResponse.ok()) {
    throw new Error(`登录失败 ${loginResponse.status()} ${await loginResponse.text()}`);
  }
  record('登录成功', { status: loginResponse.status() });

  await page.goto(`${BASE}/relationships`, { waitUntil: 'domcontentloaded' });
  await page.getByTestId('person-list').waitFor({ timeout: 30_000 });
  const firstLoadCards = await page.getByTestId('subject-card').count();
  record('首屏加载', { cards: firstLoadCards });
  await shot(page, '01-list-desktop');

  // ---------------------------------------------------------------- 1. 姓名优先 + 扫读
  const firstCard = page.getByTestId('subject-card').first();
  const identity = await firstCard.evaluate((element) => {
    const name = element.querySelector('[data-testid="person-name"]');
    return {
      name: name?.textContent?.trim() ?? '',
      accountsInList: element.querySelectorAll('code').length,
      platformChips: element.querySelectorAll('.platform-chip').length,
    };
  });
  record('姓名优先与扫读', identity);
  if (identity.accountsInList !== 0) {
    throw new Error('列表行不该出现完整外部 ID 的 <code>：核对信息应留在详情');
  }
  // 找一个**有账号**的人来验证"平台 + 账号数"扫读：
  // 第一行是 owner（没有绑定账号），它正确地显示成"没有绑定账号"。
  const withAccounts = page.getByTestId('subject-card').nth(1);
  const sweep = await withAccounts.evaluate((element) => ({
    name: element.querySelector('[data-testid="person-name"]')?.textContent?.trim(),
    chips: [...element.querySelectorAll('.platform-chip')].map((chip) => chip.textContent?.trim()),
    noAccounts: Boolean(element.querySelector('[data-testid="no-accounts"]')),
  }));
  record('平台与账号数扫读', sweep);
  if (sweep.chips.length === 0) throw new Error('有账号的人没有渲染平台徽标');
  if (sweep.noAccounts) throw new Error('有账号的人错误地显示"没有绑定账号"');

  // ---------------------------------------------------------------- 2. 同名区分
  await page.getByTestId('subjects-search').fill('小明01');
  await page.getByTestId('subjects-search').press('Enter');
  const twinSettled = await waitForSettledList(page);
  record('同名搜索结果', twinSettled);
  const twins = page.getByTestId('subject-card');
  const twinCount = await twins.count();
  const twinInfo = [];
  for (let index = 0; index < twinCount; index += 1) {
    twinInfo.push(await twins.nth(index).evaluate((element) => ({
      name: element.querySelector('[data-testid="person-name"]')?.textContent?.trim(),
      platforms: element.querySelector('[data-testid="person-platforms"]')?.textContent?.trim(),
      hint: element.querySelector('[data-testid="same-name-hint"]')?.textContent?.trim() ?? null,
    })));
  }
  record('同名区分', { count: twinCount, twins: twinInfo });
  if (twinInfo.length < 2) throw new Error('同名组少于两人，无法验证区分');
  /*
   * 同名区分靠的是**账号尾号**（`同名区分：napcat …0000`），不是平台名：
   * 这一组刚好两个人都只有 napcat、一个号，平台与数量完全相同——那正是
   * 最需要区分提示的场景。所以断言落在提示上，不落在平台上。
   */
  if (twinInfo[0].hint === null || twinInfo[1].hint === null) {
    throw new Error('同名的人没有渲染区分提示');
  }
  if (twinInfo[0].hint === twinInfo[1].hint) {
    throw new Error('两个同名人的区分提示相同，无法区分');
  }
  await shot(page, '02-same-name');

  // ---------------------------------------------------------------- 3. 搜索到"未加载的人"
  // 用分页边界之外的一个人：先确认它不在首屏已加载的 50 条里
  const lateSubject = await page.evaluate(async () => {
    const response = await fetch('/v1/accounts/subjects?limit=1&offset=900', { credentials: 'same-origin' });
    const data = await response.json();
    return data.subjects?.[0] ?? null;
  });
  if (!lateSubject) throw new Error('取不到第 901 条，无法验证跨页搜索');
  record('第 901 条（不在首屏）', {
    display_name: lateSubject.display_name,
    subject_id: lateSubject.subject_id,
  });

  await page.getByTestId('subjects-search').fill(lateSubject.display_name);
  await page.getByTestId('subjects-search').press('Enter');
  const searchSettled = await waitForSettledList(page);
  const searched = await page.getByTestId('subject-card').count();
  const searchedText = await page.getByTestId('person-list').innerText();
  record('搜索结果（服务端跨分页）', { cards: searched, contains: searchedText.includes(lateSubject.display_name) });
  if (searched === 0) throw new Error('搜索第 901 条没有结果');
  await shot(page, '03-search-late-subject');

  // ---------------------------------------------------------------- 4. 合并（目标跨分页）
  const sourceCard = page.getByTestId('subject-card').first();
  const sourceId = await sourceCard.getAttribute('data-subject-id');
  const sourceName = (await sourceCard.getByTestId('person-name').innerText()).trim();
  await sourceCard.getByTestId('merge-open').click();
  const dialog = page.getByTestId('merge-confirm');
  await dialog.waitFor();
  await shot(page, '04-merge-dialog-open');

  // 目标在别的页：用候选搜索找他
  const target = await page.evaluate(async () => {
    const response = await fetch('/v1/accounts/subjects?limit=1&offset=400', { credentials: 'same-origin' });
    const data = await response.json();
    const row = data.subjects?.[0];
    return row ? { id: row.subject_id, name: row.display_name, accounts: row.accounts } : null;
  });
  if (!target) throw new Error('取不到合并目标');
  await dialog.getByTestId('merge-candidate-search').fill(target.accounts?.[0]?.external_account_id ?? target.name);
  /*
   * 按 **subject_id** 点中目标，不点"第一个"。
   *
   * 搜索是按账号子串匹配的，命中集合可能不止一个；点第一个等于把"我以为
   * 的目标"和"实际合并的目标"混为一谈——后面的断言会对着另一个人的数据
   * 去验证，而失败信息还会说是"合并后找不到"。
   */
  const targetCandidate = dialog.locator(`[data-testid="merge-candidate"][data-subject-id="${target.id}"]`);
  await targetCandidate.waitFor();
  await targetCandidate.click();
  await page.getByTestId('merge-confirm-target').waitFor();
  const targetParty = await dialog.getByTestId('merge-confirm-target').innerText();
  record('合并确认：源/目标/账号', {
    sourceName,
    targetFilled: targetParty.includes(target.name),
    hasFullIds: /\d{6,}/.test(targetParty),
  });
  if (!targetParty.includes(target.name)) throw new Error('确认面板没有展示选中的目标');
  await shot(page, '05-merge-confirm-pair');

  await dialog.getByTestId('merge-confirm-ok').click();
  await page.getByTestId('identity-message').waitFor({ timeout: 20_000 });
  const mergeMessage = await page.getByTestId('identity-message').innerText();
  record('合并结果', { message: mergeMessage });
  await shot(page, '06-after-merge');

  // ---------------------------------------------------------------- 5. 合并后改名
  /*
   * 找"目标那一行"必须**按 subject_id**，不能按姓名：1224 人里有 40 组同名，
   * 按名字筛会命中别人。这里用目标自己的账号去搜（账号唯一），再按 id 认行。
   */
  const targetNow = await page.evaluate(async (subjectId) => {
    const detail = await fetch(
      `/v1/accounts/subjects/${encodeURIComponent(subjectId)}/details?section=accounts`,
      { credentials: 'same-origin' },
    ).then((response) => response.json());
    // 详情返回的是**整组**账号；用属于目标自己的那一个去搜，命中唯一
    const own = (detail.items ?? []).find((row) => row.subject_id === subjectId);
    if (!own) return null;
    const list = await fetch(
      `/v1/accounts/subjects?limit=20&q=${encodeURIComponent(own.external_account_id)}`,
      { credentials: 'same-origin' },
    ).then((response) => response.json());
    const row = list.subjects?.find((item) => item.subject_id === subjectId);
    return row ? { name: row.display_name, accounts: row.accounts, searchKey: own.external_account_id } : null;
  }, target.id);
  record('合并后目标当前状态', targetNow);
  if (!targetNow) throw new Error('合并后找不到目标 subject');

  await page.getByTestId('subjects-search').fill(targetNow.searchKey);
  await page.getByTestId('subjects-search').press('Enter');
  await waitForSettledList(page);
  const mergedTargetCard = page.locator(`[data-testid="subject-card"][data-subject-id="${target.id}"]`);
  await mergedTargetCard.waitFor();
  const memberBadge = await mergedTargetCard.getByTestId('merged-members-badge').count();
  const memberBadgeText = memberBadge > 0
    ? (await mergedTargetCard.getByTestId('merged-members-badge').innerText()).trim()
    : null;
  record('合并后目标行', { hasMembersBadge: memberBadge > 0, text: memberBadgeText });
  if (memberBadge === 0) throw new Error('合并后目标行没有显示"已并入 N 个号"');

  /*
   * 被并入的记录在哪：列表是**按主体**返回的，成员行只在它自己落在结果集里时
   * 出现。所以这里分两步：
   *
   * 1. 用源自己的账号搜索 —— 这一步必然命中源（服务端按账号 id 匹配）；
   * 2. 看它是否被标成"已并入 <目标>"。
   *
   * 这一步同时钉住一个真实边界：**按目标搜索时不会连出成员行**。所以目标行的
   * 「已并入 1 个号」不能靠成员在不在本页来算——它必须来自服务端计数。
   */
  const sourceOwnAccount = await page.evaluate(async (subjectId) => {
    const detail = await fetch(
      `/v1/accounts/subjects/${encodeURIComponent(subjectId)}/details?section=accounts`,
      { credentials: 'same-origin' },
    ).then((response) => response.json());
    const own = (detail.items ?? []).find((row) => row.subject_id === subjectId);
    return own?.external_account_id ?? null;
  }, sourceId);
  if (!sourceOwnAccount) throw new Error('拿不到源自己的账号');
  await page.getByTestId('subjects-search').fill(sourceOwnAccount);
  await page.getByTestId('subjects-search').press('Enter');
  await waitForSettledList(page);
  /*
   * 搜索源账号只命中源自己 → 目标不在结果里，页面把它放在
   * 「目标不在本次结果中的并入记录」那一段。这里同时验证那段文案没有胡说。
   */
  const orphanSection = page.getByLabel('目标不在本次结果中的并入记录');
  await orphanSection.waitFor();
  const orphanText = await orphanSection.innerText();
  record('目标不在结果时的说明', { text: orphanText.split('\n').slice(0, 2).join(' / ') });
  if (orphanText.includes('找不到目标')) {
    throw new Error('页面把"目标不在本次结果里"说成了"找不到目标"');
  }
  const mergedSourceRow = orphanSection.locator(`[data-testid="subject-card"][data-subject-id="${sourceId}"]`);
  await mergedSourceRow.waitFor();
  const mergedRowInfo = await mergedSourceRow.evaluate((element) => ({
    badge: element.querySelector('[data-testid="merged-badge"]')?.textContent?.trim() ?? null,
    hasUnmerge: Boolean(element.querySelector('[data-testid="unmerge-button"]')),
    hasRename: Boolean(element.querySelector('[data-testid="rename-open"]')),
  }));
  record('并入的记录行', mergedRowInfo);
  if (!mergedRowInfo.badge) throw new Error('并入的记录没有标注并入谁');
  if (!mergedRowInfo.hasUnmerge) throw new Error('并入的记录没有拆开入口');
  // 改名只给保留的一方：源的改名按钮不该出现（点它必然 409）
  if (mergedRowInfo.hasRename) throw new Error('已并入的记录不该出现改名按钮');
  await shot(page, '06b-merged-row');

  // 改名要在**目标那一行**上做：先搜回目标（上一步看到的可能是源）
  await page.getByTestId('subjects-search').fill(targetNow.searchKey);
  await page.getByTestId('subjects-search').press('Enter');
  await waitForSettledList(page);
  await mergedTargetCard.waitFor();
  await mergedTargetCard.getByTestId('rename-open').click();
  const renameDialog = page.getByTestId('rename-dialog');
  await renameDialog.waitFor();
  const newName = `合并后改名${Date.now() % 10000}`;
  await renameDialog.getByTestId('rename-input').fill(newName);
  await shot(page, '07-rename-dialog');
  await renameDialog.getByTestId('rename-save').click();
  await page.getByTestId('identity-message').waitFor({ timeout: 20_000 });
  const renameMessage = await page.getByTestId('identity-message').innerText();
  record('合并后改名', { newName, message: renameMessage });
  if (!renameMessage.includes(newName)) throw new Error('改名反馈里没有新姓名');

  await page.getByTestId('subjects-search').fill(newName);
  await page.getByTestId('subjects-search').press('Enter');
  await waitForSettledList(page);
  const renamedCard = page.locator(`[data-testid="subject-card"][data-subject-id="${target.id}"]`);
  await renamedCard.waitFor();
  const renamedName = (await renamedCard.getByTestId('person-name').innerText()).trim();
  record('改名后可搜到', { cards: await page.getByTestId('subject-card').count(), name: renamedName });
  if (renamedName !== newName) throw new Error('改名后搜到的行不是新姓名');
  await shot(page, '08-after-rename');

  // ---------------------------------------------------------------- 6. 刷新后状态仍在
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.getByTestId('subjects-search').fill(newName);
  await page.getByTestId('subjects-search').press('Enter');
  await waitForSettledList(page);
  const reloadedCard = page.locator(`[data-testid="subject-card"][data-subject-id="${target.id}"]`);
  await reloadedCard.waitFor();
  const afterReload = await reloadedCard.evaluate((element) => ({
    name: element.querySelector('[data-testid="person-name"]')?.textContent?.trim(),
    membersBadge: element.querySelector('[data-testid="merged-members-badge"]')?.textContent?.trim() ?? null,
  }));
  await page.getByTestId('subjects-search').fill(sourceOwnAccount);
  await page.getByTestId('subjects-search').press('Enter');
  await waitForSettledList(page);
  const reloadedSourceRow = page.locator(`[data-testid="subject-card"][data-subject-id="${sourceId}"]`);
  await reloadedSourceRow.waitFor();
  record('刷新后状态', { ...afterReload, sourceStillMerged: true });
  // 姓名来自库、并入来自指针：两件都得在刷新后还在
  if (afterReload.name !== newName || !afterReload.membersBadge) {
    throw new Error('刷新后改名或并入状态没有保留');
  }
  await shot(page, '09-after-reload');

  // ---------------------------------------------------------------- 7. 拆开
  await reloadedSourceRow.getByTestId('unmerge-button').first().click();
  await page.getByTestId('identity-message').waitFor({ timeout: 20_000 });
  const unmergeMessage = await page.getByTestId('identity-message').innerText();
  record('拆开结果', { message: unmergeMessage });
  await shot(page, '10-after-unmerge');

  // 拆开后源重新是独立的人：按它的 **id** 认回来（同名的太多，不能按名字）
  await page.getByTestId('subjects-search').fill(sourceName);
  await page.getByTestId('subjects-search').press('Enter');
  await waitForSettledList(page);
  const restoredCard = page.getByTestId('person-list')
    .locator(`[data-testid="subject-card"][data-subject-id="${sourceId}"]`);
  await restoredCard.waitFor();
  const restored = await restoredCard.evaluate((element) => ({
    name: element.querySelector('[data-testid="person-name"]')?.textContent?.trim(),
    merged: Boolean(element.querySelector('[data-testid="merged-badge"]')),
    hasMergeOpen: Boolean(element.querySelector('[data-testid="merge-open"]')),
  }));
  record('拆开后源记录', restored);
  if (restored.merged) throw new Error('拆开后源记录仍显示已并入');
  if (!restored.hasMergeOpen) throw new Error('拆开后源记录没有恢复"合并"入口');

  // ---------------------------------------------------------------- 8. 详情：完整 ID 与验证状态
  await page.getByTestId('subjects-search').fill('');
  await page.getByTestId('subjects-search').press('Enter');
  await waitForSettledList(page);
  const detailCard = page.getByTestId('subject-card').nth(2);
  await detailCard.getByTestId('detail-open').click();
  const detail = detailCard.getByTestId('subject-detail');
  await detail.waitFor();
  await page.waitForTimeout(400);
  const detailInfo = await detail.evaluate((element) => ({
    rows: element.querySelectorAll('.identity-detail__row').length,
    hasFullId: /[0-9]{6,}/.test(element.textContent ?? ''),
    hasProofLabel: /已证明|待验证|状态未知/.test(element.textContent ?? ''),
  }));
  record('详情：完整 ID 与验证状态', detailInfo);
  if (!detailInfo.hasFullId) throw new Error('详情里没有完整外部 ID');
  await shot(page, '11-detail');

  // 关闭详情后回到原记录（滚动位置与焦点）
  await detailCard.getByTestId('detail-open').click();
  await page.waitForTimeout(200);
  const back = await page.evaluate(() => ({
    open: document.querySelectorAll('[data-testid="subject-detail"]').length,
  }));
  record('关闭详情', back);
  if (back.open !== 0) throw new Error('关闭后详情仍然存在');

  // ---------------------------------------------------------------- 8b. 关闭弹层回到原记录
  /*
   * 从**远离那一行的地方**关闭：改名弹层位于顶层，关闭后浏览器把焦点清回
   * body。必须由 `returnTo` 逻辑把它带回原记录——否则键盘用户会在页面上
   * 失去位置。这条在真浏览器里量 `document.activeElement` 属于哪一行。
   */
  await page.getByTestId('subjects-search').fill('');
  await page.getByTestId('subjects-search').press('Enter');
  await waitForSettledList(page);
  const deepCard = page.getByTestId('subject-card').nth(20);
  await deepCard.scrollIntoViewIfNeeded();
  const deepId = await deepCard.getAttribute('data-subject-id');
  await deepCard.getByTestId('rename-open').click();
  await page.getByTestId('rename-dialog').waitFor();
  await page.getByTestId('rename-dialog').getByRole('button', { name: '取消' }).click();
  await expectDialogGone(page, 'rename-dialog');
  const returnState = await page.evaluate((id) => {
    const active = document.activeElement;
    const row = active?.closest?.('[data-subject-id]') ?? null;
    const target = document.querySelector(`[data-subject-id="${CSS.escape(id)}"]`);
    const rect = target?.getBoundingClientRect();
    return {
      focusRow: row?.getAttribute('data-subject-id') ?? null,
      tag: active?.tagName ?? null,
      inView: Boolean(rect && rect.top >= 0 && rect.bottom <= window.innerHeight),
    };
  }, deepId);
  record('关闭弹层回到原记录', { ...returnState, expected: deepId });
  if (returnState.focusRow !== deepId) throw new Error('焦点没有回到原记录所在的行');
  if (!returnState.inView) throw new Error('原记录不在视口内');
  await shot(page, '11b-return-to-record');

  // ---------------------------------------------------------------- 9. 窄屏
  const mobileContext = await browser.newContext({ viewport: { width: 375, height: 812 } });
  const mobile = await mobileContext.newPage();
  await mobile.request.post(`${BASE}/v1/auth/login`, { data: { username: USER, password: PASSWORD } }).catch(() => undefined);
  // 用同一个 context 的 cookie：新建 context 没有 cookie，直接带上 storageState
  await mobile.close();
  await mobileContext.close();

  const mobileContext2 = await browser.newContext({
    viewport: { width: 375, height: 812 },
    storageState: await context.storageState(),
  });
  const mobilePage = await mobileContext2.newPage();
  await mobilePage.goto(`${BASE}/relationships`, { waitUntil: 'domcontentloaded' });
  await mobilePage.getByTestId('person-list').waitFor({ timeout: 30_000 });
  const mobileWidths = await mobilePage.evaluate(() => ({
    doc: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }));
  record('窄屏横向溢出', mobileWidths);
  await shot(mobilePage, '12-mobile-list');
  await mobilePage.getByTestId('subject-card').first().getByTestId('merge-open').click();
  await mobilePage.getByTestId('merge-confirm').waitFor();
  const mobileDialogWidths = await mobilePage.getByTestId('merge-confirm').evaluate((element) => ({
    overflow: element.scrollWidth - element.clientWidth,
  }));
  record('窄屏合并面板溢出', mobileDialogWidths);
  await shot(mobilePage, '13-mobile-merge');
  if (mobileWidths.doc > 0) throw new Error('窄屏列表横向溢出');
  if (mobileDialogWidths.overflow > 1) throw new Error('窄屏合并面板横向溢出');
  await mobileContext2.close();

  await writeFile(
    path.join(OUT, 'acceptance-log.json'),
    JSON.stringify({ base: BASE, steps, consoleErrors: log }, null, 2),
    'utf8',
  );
  console.log(`[acceptance] 日志 -> ${path.join(OUT, 'acceptance-log.json')}`);
  if (log.length > 0) console.log('[acceptance] 浏览器控制台错误：', log);

  await context.close();
  await browser.close();
  console.log('[acceptance] 全部步骤通过');
}

main().catch((error) => {
  console.error('[acceptance] 失败：', error);
  process.exitCode = 1;
});
