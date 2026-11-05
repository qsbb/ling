/**
 * playwright 解析（e2e 共享工具）。
 *
 * 为什么需要这个文件：`w0_browser.mjs` 曾经直接 `import { chromium } from 'playwright'`，
 * 而 playwright 只装在 `clients/web-console/node_modules`（devDependency + lockfile 的
 * 正式位置），**仓库根没有 `node_modules`**。于是干净检出里照 README 跑默认命令就是
 * `ERR_MODULE_NOT_FOUND`（exit 1）——承诺的「一条命令」根本不成立。
 *
 * 解析顺序（依次尝试，成功即返回）：
 *   ① `clients/web-console`：playwright 的正式安装位置（devDependency + lockfile）；
 *   ② 仓库根 `node_modules`：有人照老习惯在根上装过；
 *   ③ 直连 `import('playwright')`：全局装、或 `tests/e2e/node_modules` 自带的那份。
 * 三者都失败 → 打印每次尝试的原因 + 可照做的修复命令，并以 exit 2 退出，
 * **不再**让 ESM 抛一个裸的 `ERR_MODULE_NOT_FOUND`（那条报错不告诉人该做什么）。
 *
 * 每一步都**先确认该位置的 `playwright/package.json` 真的存在**再加载，而不是只靠
 * `createRequire` 去解析：Node 的解析会从基点逐级向上找 `node_modules`，若不先确认，
 * ① 完全可能在仓库根命中，却被打上「clients/web-console」的标签——日志说错来源，
 * 排查时反而误导。确认过再加载，`origin` 才名副其实。
 *
 * 返回 `{ chromium, origin }`：`origin` 由各脚本打印，说明这次用的是哪份 playwright。
 */
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
/** `tests/e2e/lib` → 仓库根。 */
const REPO_ROOT = path.resolve(HERE, '..', '..', '..');

/** 三个来源都失败时给的修复命令：playwright 是 `clients/web-console` 的 devDependency。 */
const FIX_HINT = 'pnpm -C clients/web-console install（playwright 已列为 devDependency）';

/** 候选安装位置：`node_modules/playwright` 的绝对目录。 */
const WEB_CONSOLE_DIR = path.join(REPO_ROOT, 'clients', 'web-console', 'node_modules', 'playwright');
const REPO_ROOT_DIR = path.join(REPO_ROOT, 'node_modules', 'playwright');

/** 该候选位置是否真的装过 playwright（看它自己的 package.json）。 */
function installedAt(packageDir) {
  return fs.existsSync(path.join(packageDir, 'package.json'));
}

/** 版本号拿不到不算失败：能加载到主模块就已经能用了。 */
function versionAt(packageDir) {
  try {
    return JSON.parse(fs.readFileSync(path.join(packageDir, 'package.json'), 'utf8')).version ?? 'unknown';
  } catch {
    return 'unknown';
  }
}

/**
 * 从**指定**的安装目录加载 playwright：以该包的 `package.json` 作 `createRequire` 基点，
 * 这样 `exports` 映射照常生效，且解析结果必然来自这个目录（它自己就有 node_modules 邻域）。
 */
function requireFromDir(packageDir) {
  return createRequire(path.join(packageDir, 'package.json'))('playwright');
}

export async function loadChromium() {
  const attempts = [];

  // ① clients/web-console：正式安装位置（devDependency + lockfile）。
  if (installedAt(WEB_CONSOLE_DIR)) {
    try {
      const mod = requireFromDir(WEB_CONSOLE_DIR);
      const version = versionAt(WEB_CONSOLE_DIR);
      return { chromium: mod.chromium, origin: `clients/web-console (playwright ${version})` };
    } catch (error) {
      attempts.push(`clients/web-console → ${error?.code || error?.message}`);
    }
  } else {
    attempts.push(`clients/web-console → 未安装（${WEB_CONSOLE_DIR} 不存在）`);
  }

  // ② 仓库根 node_modules：有人照老习惯在根上装过。仓库根没有 package.json 也没关系，
  //    createRequire 只拿这个路径当解析基点。
  if (installedAt(REPO_ROOT_DIR)) {
    try {
      const mod = requireFromDir(REPO_ROOT_DIR);
      const version = versionAt(REPO_ROOT_DIR);
      return { chromium: mod.chromium, origin: `仓库根 node_modules (playwright ${version})` };
    } catch (error) {
      attempts.push(`仓库根 node_modules → ${error?.code || error?.message}`);
    }
  } else {
    attempts.push(`仓库根 node_modules → 未安装（${REPO_ROOT_DIR} 不存在）`);
  }

  // ③ 直连 import：全局安装、或 `tests/e2e/node_modules` 自带的那份（本文件在
  //    `tests/e2e/lib/`，向上两级就是 `tests/e2e/node_modules`）。
  try {
    const mod = await import('playwright');
    return { chromium: mod.chromium, origin: "本地 node_modules（import('playwright')）" };
  } catch (error) {
    attempts.push(`import('playwright') → ${error?.code || error?.message}`);
  }

  console.error('配置错误：找不到 playwright。');
  for (const line of attempts) console.error(` - ${line}`);
  console.error(`修复：${FIX_HINT}`);
  process.exit(2);
}
