/**
 * e2e 隔离工具（TASK128）。
 *
 * 这里只做两件事，因为这两件事都曾让 e2e「假失败 / 假成功」：
 *
 * 1. **每次运行自己的数据库**：库名 = `<前缀>_<运行 id>`（UTC 时间戳 + pid + 随机），
 *    由 `CREATE DATABASE` **新建**；只有本次运行亲手建出来的库，结束时才会被
 *    `DROP`（且带 `WITH (FORCE)` 收掉残留连接）。**绝不**去 DROP 一个同名但
 *    不是本次创建的库——那是别人（或上一次崩溃的运行）的数据。
 *
 *    为什么不是「先 DROP 再 CREATE」：两个实例并行时，后启动的那个会把前一个
 *    正在用的库删掉，于是两边互相把对方的数据清空，测试报出「320px 找不到
 *    98 字长正文条目」这种与被测代码毫无关系的假失败。
 *
 * 2. **截图 / 日志默认不写仓库**：默认落在 `<tmp>/ling-e2e/<tag>-<运行 id>/`。
 *    只有显式给 `SHOTS_DIR` 才写指定位置（可能是仓库里的证据目录，那是调用方
 *    的决定，脚本只负责说明自己写了哪儿）。
 *
 * 数据库操作走仓库自带的 `.venv/bin/python` + asyncpg（e2e 已经依赖它），
 * 通过 stdin 传 JSON、stdout 回 JSON，避免把 SQL 拼进命令行。
 *
 * 环境变量：
 *   E2E_DB_NAME   指定库名（不含后缀）。若该库已存在——**不会删它**，而是自动
 *                 退到 `<E2E_DB_NAME>_<运行 id>`，并打印原因。留空则用
 *                 `ling_<tag>_e2e_<运行 id>`。
 *   SHOTS_DIR     截图 / 日志目录（默认临时目录）。
 *   LING_PGPORT   PostgreSQL 端口（各脚本默认 55432）。
 */
import { spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

/** 这些库名不是「测试库」：即使有人把它填进 E2E_DB_NAME，也拒绝使用（更不会删）。 */
export const PROTECTED_DATABASES = new Set(['postgres', 'template0', 'template1', 'ling', 'ling_test']);

/** 本次进程亲手创建的库。DROP 的唯一边界就是这个集合。 */
const createdByThisRun = new Set();

export function createdDatabases() {
  return [...createdByThisRun];
}

const PG_SCRIPT = String.raw`
import asyncio, json, sys
import asyncpg

PROTECTED = {"postgres", "template0", "template1", "ling", "ling_test"}


async def connect(payload, database):
    return await asyncpg.connect(
        user=payload.get("user", "ling"),
        host=payload.get("host", "127.0.0.1"),
        port=int(payload.get("port", 55432)),
        database=database,
    )


async def main():
    payload = json.load(sys.stdin)
    op = payload["op"]
    out = {"ok": True}
    admin_db = payload.get("adminDatabase", "postgres")
    if op == "list":
        conn = await connect(payload, admin_db)
        try:
            rows = await conn.fetch("select datname from pg_database")
            out["databases"] = sorted(row["datname"] for row in rows)
        finally:
            await conn.close()
        return out

    if op in ("create", "drop", "exists"):
        name = payload["database"]
        if not name or name != name.strip():
            raise ValueError("database name must be non-empty without surrounding spaces")
        if op == "drop":
            if name in PROTECTED:
                raise PermissionError("refusing to drop protected database: %r" % name)
            if payload.get("confirm") != name:
                raise PermissionError("refusing to drop unconfirmed database: %r" % name)
        conn = await connect(payload, admin_db)
        try:
            exists = await conn.fetchval("select 1 from pg_database where datname = $1", name) is not None
            out["existsBefore"] = exists
            if op == "exists":
                out["exists"] = exists
            elif op == "create":
                # 直接 CREATE，不先查存在：两个并行实例同时「查不到 → 都去建」时，
                # 后到的那个会撞 unique 约束。这一步必须把两种报错都归成「已被占用」：
                # - 42P04 duplicate_database（正常撞名）；
                # - 23505 unique_violation on pg_database_datname_index——两个实例同时
                #   检查「不存在」再同时 CREATE 时实测就是这条（TASK128 自测抓到）。
                try:
                    await conn.execute('CREATE DATABASE "%s"' % name)
                    out["created"] = True
                except (asyncpg.exceptions.DuplicateDatabaseError, asyncpg.exceptions.UniqueViolationError):
                    out["ok"] = False
                    out["error"] = "database already exists"
                    out["sqlstate"] = "42P04"
            else:  # drop
                if not exists:
                    out["ok"] = False
                    out["error"] = "database does not exist"
                    out["sqlstate"] = "3D000"
                else:
                    # FORCE 只针对「本次创建的、名字唯一的」库：结束前收掉可能残留的
                    # 连接（例如 API 进程被 SIGKILL 后还没回收的 socket）。
                    await conn.execute('DROP DATABASE "%s" WITH (FORCE)' % name)
                    out["dropped"] = True
                    out["existsAfter"] = await conn.fetchval(
                        "select 1 from pg_database where datname = $1", name
                    ) is not None
        finally:
            await conn.close()
        return out

    if op == "exec":
        conn = await connect(payload, payload["database"])
        try:
            results = []
            for statement in payload.get("statements", []):
                results.append(await conn.execute(statement))
            out["results"] = results
        finally:
            await conn.close()
        return out

    raise ValueError("unknown op: %r" % op)


try:
    print(json.dumps(asyncio.run(main()), ensure_ascii=False))
except Exception as exc:  # noqa: BLE001 - 错误要回给 JS 侧判断类型
    print(json.dumps({
        "ok": False,
        "error": "%s: %s" % (type(exc).__name__, exc),
        "sqlstate": getattr(exc, "sqlstate", None),
    }, ensure_ascii=False))
`;

function pg(payload, { python, repoRoot, pgPort }) {
  const result = spawnSync(python, ['-c', PG_SCRIPT], {
    cwd: repoRoot,
    encoding: 'utf8',
    input: JSON.stringify({ port: pgPort, ...payload }),
    timeout: 60_000,
  });
  if (result.error) {
    throw new Error(`调用 python/asyncpg 失败：${result.error.message}`);
  }
  const lines = (result.stdout || '').trim().split('\n').filter(Boolean);
  const last = lines.at(-1) || '';
  let data = null;
  try {
    data = JSON.parse(last);
  } catch {
    data = null;
  }
  if (!data) {
    throw new Error(
      `数据库操作 ${payload.op} 没有得到 JSON 结果（python exit=${result.status}）：` +
        `${(result.stderr || result.stdout || '').slice(0, 800)}`,
    );
  }
  return data;
}

/** 运行 id：UTC 时间戳 + 进程号 + 随机后缀；小写、只含 [a-z0-9_]。 */
export function makeRunId() {
  const stamp = new Date()
    .toISOString()
    .replace(/[-:]/g, '')
    .replace(/\.\d+Z$/, 'z')
    .toLowerCase();
  return `${stamp}_${process.pid.toString(36)}_${randomBytes(3).toString('hex')}`;
}

function sanitizeIdentifier(raw) {
  const cleaned = String(raw)
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, '_')
    .replace(/^_+/, '')
    .replace(/_+$/, '');
  const withHead = /^[a-z_]/.test(cleaned) ? cleaned : `db_${cleaned}`;
  // PostgreSQL 标识符上限 63 字节；名字里全是 ASCII，切片长度即字节数。
  return withHead.slice(0, 63);
}

function assertSafeName(name) {
  if (!/^[a-z_][a-z0-9_]{0,62}$/.test(name)) {
    throw new Error(`库名不合法（只允许小写字母/数字/下划线，≤63）：${name}`);
  }
}

/**
 * 建一个只属于本次运行的库。
 *
 * @returns {{name: string, runId: string, note: string}}
 */
export function createIsolatedDatabase({
  tag,
  pgPort,
  python,
  repoRoot,
  requestedName = process.env.E2E_DB_NAME,
  runId = makeRunId(),
  log = console.log,
}) {
  const base = sanitizeIdentifier(`ling_${tag}_e2e`);
  const candidates = [];
  let note = '';
  if (requestedName) {
    const requested = sanitizeIdentifier(requestedName);
    if (PROTECTED_DATABASES.has(requested)) {
      throw new Error(
        `E2E_DB_NAME=${requestedName} 是保留库名，拒绝把它当测试库（更不会 DROP 它）。` +
          '请换一个专用名字，或留空让脚本自动生成。',
      );
    }
    candidates.push(requested);
    // E2E_DB_NAME 已存在 = 不是本次创建的库：改名（加后缀），而不是删掉它。
    candidates.push(sanitizeIdentifier(`${requested}_${runId}`));
    note = process.env.E2E_DB_NAME ? `E2E_DB_NAME=${requestedName}` : `指定库名=${requestedName}`;
  }
  // 默认名也可能撞上一次崩溃留下的同名库（概率极低），再留一个带随机数的候选。
  for (const candidate of [
    sanitizeIdentifier(`${base}_${runId}`),
    sanitizeIdentifier(`${base}_${runId}_${randomBytes(2).toString('hex')}`),
  ]) {
    candidates.push(candidate);
  }

  const seen = new Set();
  let lastError = null;
  for (const name of candidates) {
    if (seen.has(name)) continue;
    seen.add(name);
    assertSafeName(name);
    const result = pg({ op: 'create', database: name }, { python, repoRoot, pgPort });
    if (result.ok) {
      createdByThisRun.add(name);
      const detail = note ? `（${note}）` : '';
      log(`隔离库：${name}（本次新建${detail}；结束时只 DROP 这一个）`);
      return { name, runId, note };
    }
    // 42P04 = duplicate_database；23505 = 两个实例同时 CREATE 时撞上的 unique 约束。
    if (result.sqlstate === '42P04' || result.sqlstate === '23505') {
      log(`隔离库：${name} 已存在 → **不是本次创建的，不会 DROP 它**，换下一个名字`);
      continue;
    }
    lastError = result;
    break;
  }
  throw new Error(
    `无法创建隔离数据库（先确认 PostgreSQL 在 127.0.0.1:${pgPort} 上跑）：` +
      `${lastError?.error || '所有候选名都不可用'}`,
  );
}

/**
 * 删掉本次运行建的库，并复查它真的没了。任何「不是本次创建的」都快进拒绝。
 */
export function dropOwnedDatabase({ name, pgPort, python, repoRoot, log = console.log }) {
  if (!name) return { dropped: false, reason: '没有自建库（复用外部服务模式）' };
  if (!createdByThisRun.has(name)) {
    throw new Error(
      `拒绝 DROP ${name}：它不是本次运行创建的库。` +
        '（唯一允许的 DROP 边界是 createdByThisRun；这条断言就是边界本身。）',
    );
  }
  if (PROTECTED_DATABASES.has(name)) {
    throw new Error(`拒绝 DROP ${name}：保留库名。`);
  }
  assertSafeName(name);
  const result = pg(
    { op: 'drop', database: name, confirm: name },
    { python, repoRoot, pgPort },
  );
  if (!result.ok) {
    throw new Error(`DROP 自建库 ${name} 失败：${result.error}`);
  }
  createdByThisRun.delete(name);
  const check = pg({ op: 'exists', database: name }, { python, repoRoot, pgPort });
  if (check.ok && check.exists) {
    throw new Error(`DROP 自建库 ${name} 之后它还在：收尾失败`);
  }
  log(`隔离库：${name} 已删除（复查 pg_database 无此库）`);
  return { dropped: true };
}

/** 在指定库上执行若干条固定 SQL（真 403 / 真 500 用例的「制造故障」用）。 */
export function execDbStatements({ database, statements, pgPort, python, repoRoot }) {
  const result = pg({ op: 'exec', database, statements }, { python, repoRoot, pgPort });
  if (!result.ok) throw new Error(`在 ${database} 执行 SQL 失败：${result.error}`);
  return result.results;
}

export function listDatabases({ pgPort, python, repoRoot }) {
  const result = pg({ op: 'list' }, { python, repoRoot, pgPort });
  if (!result.ok) throw new Error(`列出数据库失败：${result.error}`);
  return result.databases;
}

/**
 * 截图 / 日志目录。默认临时目录（绝不默认写仓库里的证据目录）。
 * @returns {{dir: string, fromEnv: boolean}}
 */
export function resolveShotsDir(tag, runId = makeRunId()) {
  const explicit = process.env.SHOTS_DIR;
  const dir = explicit
    ? path.resolve(explicit)
    : path.join(os.tmpdir(), 'ling-e2e', `${tag}-${runId}`);
  fs.mkdirSync(dir, { recursive: true });
  return { dir, fromEnv: Boolean(explicit) };
}

/**
 * 收到 SIGINT / SIGTERM 时也走同一套收尾（杀服务、删自建库），
 * 避免「Ctrl-C 之后留下一堆 e2e 库」。handler 必须自己保证幂等。
 */
export function onTermination(handler) {
  let fired = false;
  const run = async (signal) => {
    if (fired) return;
    fired = true;
    try {
      await handler(signal);
    } catch (error) {
      console.error(`[${signal}] 收尾失败：${error?.message || error}`);
    } finally {
      process.exit(signal === 'SIGINT' ? 130 : 143);
    }
  };
  process.once('SIGINT', () => void run('SIGINT'));
  process.once('SIGTERM', () => void run('SIGTERM'));
}

/* ------------------------------------------------------------------ 自检 CLI */

/**
 * `node tests/e2e/lib/isolation.mjs guard-check`
 * 证明 DROP 边界：不删「不是本次创建」的库（含保留库），也不建保留库名。
 * 只读，不产生任何数据库改动。
 */
async function guardCheck() {
  const python = path.join(process.cwd(), '.venv', 'bin', 'python');
  const pgPort = Number(process.env.LING_PGPORT || 55432);
  const opts = { python, repoRoot: process.cwd(), pgPort, log: () => {} };
  const before = listDatabases(opts);
  const cases = ['ling', 'ling_test', 'postgres', 'template0', 'somebody_elses_db'];
  let failures = 0;
  for (const name of cases) {
    let refused = false;
    try {
      dropOwnedDatabase({ name, ...opts });
    } catch {
      refused = true;
    }
    console.log(`${refused ? 'PASS' : 'FAIL'}  拒绝 DROP 非本次创建的库：${name}`);
    if (!refused) failures += 1;
  }
  for (const name of ['ling', 'ling_test', 'postgres']) {
    let refused = false;
    try {
      createIsolatedDatabase({ tag: 'guard', requestedName: name, ...opts });
    } catch {
      refused = true;
    }
    console.log(`${refused ? 'PASS' : 'FAIL'}  拒绝把保留库名当测试库：${name}`);
    if (!refused) failures += 1;
  }
  const after = listDatabases(opts);
  // 只核对被探测的那几个库还在（并行跑 e2e 时全局清单本来就会变，不能拿来当断言）。
  for (const name of cases) {
    if (!before.includes(name)) continue;
    const still = after.includes(name);
    console.log(`${still ? 'PASS' : 'FAIL'}  自检后同名库仍在：${name}`);
    if (!still) failures += 1;
  }
  process.exit(failures === 0 ? 0 : 1);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const mode = process.argv[2];
  if (mode === 'guard-check') {
    await guardCheck();
  } else {
    console.error('用法：node tests/e2e/lib/isolation.mjs guard-check');
    process.exit(2);
  }
}
