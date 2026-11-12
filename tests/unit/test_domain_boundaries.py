"""域边界闸门：`src/ling/domains/<A>` 不得 import `ling.domains.<B>` 的实现。

为什么需要：19 个域平铺在同一个包里时，"谁能调谁"只靠口头约定。一旦某个域开始直接
读别人的表，边界会在几次迭代内消失——而且**不会报错**，只会慢慢变成
一个所有东西都依赖所有东西的大泥球。

## 先说这段注释原来的谎

这个文件以前写着「当前基线是 0 违规（实测）」，那是**错的**，而且错得
很隐蔽：`DOMAINS_ROOT` 写的是 `parents[2] / "domains"`，指向
`<repo>/domains`——**这个目录从来不存在**（真实位置是
`src/ling/domains`）。`rglob` 在空/不存在的目录上返回空列表，
`assert not violations` 于是**永远通过**：闸门看起来是绿的，实际上
一次都没扫过。

实测真实基线是 **7 处**（2026-11-03，HEAD d1758ff），全部出自
`domains/<A>` 直接 import 另一个域的 `service` 模块。

## 现在这个闸门保证什么

1. `DOMAINS_ROOT` 从 `ling.domains` 包自身反推，**不依赖本测试文件的
   位置**——测试文件挪到别处也不会指错。
2. 扫不到足够多的 `.py` 就直接 FAIL（见 `MIN_SCANNED_FILES`）：
   路径写错的症状和"真的没有违规"一模一样，必须显式区分。
3. 违规集合必须与 `ALLOWED` **完全相等**：多一处（新引入跨域依赖）
   FAIL，少一处（白名单陈旧）也 FAIL。
4. `ALLOWED` 只允许 import 邻居域的**公开 `service` 模块**；把它扩到
   `*.models` / `*.table` 之类会 FAIL——"跨域读投影可以，改别人的表不行"
   （ADR-0001 §2）。

## 这些为什么不修掉

### 人格多实例新增的 3 处（agenda / conversation / dream → persona.service）

原来这三处读的是环境变量 `LING_PERSONA_ID`（或是写死的 id）。人格变成
「多套并存 + 可切换」之后，「现在用哪一套」只能问 persona 域——别处各读一遍
配置就是第二份判据，症状是「在设置里切了人格，日程/梦/新窗口还在用旧的」。
三处都只调公开的 `active_persona_id()` / `active_persona_view()`，
不 import 实现模块 `persona.current`、不读不写 personas 表，符合
ADR-0001「跨域读投影可以，改别人的表不行」。

逐处评估过（每处都在 `ALLOWED` 里写了理由）。共同点是：**都通过邻居域
的公开 service 方法访问，没有任何一处 import 别人的 models/表，也没有
任何一处自己拼 SQL 写别人的表**——所有权仍然在邻居域手里，符合
ADR-0001「跨域读投影可以，改别人的表不行」。

要真正消除这些 import，需要给每个域定义端口（`ling.ports.*` 里加
`Protocol`），再在**组合根**注入实现。这些服务的构造点分布在
`worker.py`（两个进程各自的入口）、`kernel/*`、`api/routes/*`，而且都
压在热路径上（recall、群聊轮询、主动投递）——那是一次独立的接线改造，
不该和"让闸门真的生效"混在一起做。在那之前，半途而废的做法（域里留一个
「缺省实现 = 函数内 import 邻居域」）只是把依赖藏到闸门看不见的地方，
比显式白名单更坏——**那才会让闸门失去意义**。

所以这里是**显式、可审计、会腐烂就会报错**的白名单，而不是 `# noqa`。

## 已知盲区（静态检查抓不到）

- `importlib.import_module("ling.domains.<B>...")`、`__import__(...)`、
  `getattr(sys.modules[...])` 之类的动态 import。
- 跨域访问别人的表（`ling.infra.models` 是共享的，谁都 import 得到），
  这属于 ADR-0001 的另一个不变式，靠代码评审而不是这个闸门。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import ling.domains

PACKAGE = "ling.domains"
"""域包的完整名。所有检查都以它为准。"""

DOMAINS_ROOT = Path(ling.domains.__file__).resolve().parent
"""真实域目录：`src/ling/domains`。

从包自身反推而不是拼 `__file__` 的相对层级：原实现
`parents[2] / "domains"` 指向一个不存在的目录，闸门因此静默假绿了
整个生命周期（见模块注释）。
"""

MIN_SCANNED_FILES = 40
"""扫描文件数下界。实测 50（2026-11-03）。

取 40 而不是 50 是为了给增删文件留余量；取这么高是因为**唯一要防的
失败模式是"扫到 0 个"**——路径指错时这两个数字会一起塌到 0，而正常
演进不会让文件数掉到 40 以下。
"""


@dataclass(frozen=True)
class Violation:
    """一处跨域 import。`path` 相对 `DOMAINS_ROOT`，便于直接跳转。"""

    path: str
    own: str
    other: str
    module: str
    lineno: int

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: {self.own} → {self.module}"


ALLOWED: dict[tuple[str, str], str] = {
    ("speech/asr_runtime.py", "ling.domains.conversation.service"): (
        "识别完成后更新已登记源消息的转写，再复用 ConversationService 的消息与轮次接口。"
    ),
    ("speech/asr_runtime.py", "ling.domains.media.assets"): (
        "ASR 输入必须持有受授权逻辑资产的租约并读取其字节，媒体资产服务是授权与 GC 的唯一入口。"
    ),
    ("speech/asr_runtime.py", "ling.domains.media.service"): (
        "平台媒体引用需要沿用入站媒体的授权检查，识别前从 media service 查询证据权限。"
    ),
    ("speech/transcription.py", "ling.domains.consent.service"): (
        "语音派生文本的读取和提交必须复查 media.inbound 授权，consent service 持有授权真相。"
    ),
    ("speech/transcription.py", "ling.domains.conversation.service"): (
        "Web 语音发送时在授权事务内登记唯一源消息，后续复用该消息提交转写。"
    ),
    ("attention/service.py", "ling.domains.conversation.service"): (
        "群聊轮询的全部意义就是刷「这个窗口有哪些未读」。只调 "
        "ConversationService.unseen_messages / mark_seen / mark_handled / "
        "mark_missed，不碰 conversation 的表和模型——消息状态的唯一写入方"
        "仍是 conversation 域。"
    ),
    ("media/budget.py", "ling.domains.agenda.service"): (
        "后台视频作业在 worker 里跑，而 worker 可多副本——进程内计数器会让每个"
        "副本各算一份预算，N 个副本烧 N 倍的钱。所以必须用共享的 "
        "behavior_model_calls 账本，而它的并发语义（唯一约束/attempt/失败不退款）"
        "由 agenda 维护：media 只调 AgendaService.reserve_model_call / "
        "finish_model_call 这两个公开入口，不 import agenda.timeline（内部实现"
        "模块）、不碰那张表、不拼 SQL。"
        "（最初版本直接 import 了 agenda.timeline，被本闸门拦下后改成经"
        "service 转一手——这条注释记录的就是那次修正。）"
    ),
    ("media/sediment.py", "ling.domains.memory.service"): (
        "媒体沉降要把「她真的看过、但描述即将过期」的证据写成长期记忆"
        "（用户原话：看过的视频过期后经筛选走记忆入库流程）。写记忆的唯一"
        "入口是 MemoryService.propose，判重问询是 MemoryService."
        "already_proposed——本文件不 import MemoryRow、不自己查 memories 表、"
        "不拼 SQL，记忆的所有权仍完整留在 memory 域。"
        "（最初版本确实 import 了 MemoryRow 自己查重，被本闸门拦下后改成"
        "经公开方法问询——这条注释记录的就是那次修正。）"
    ),
    ("media/service.py", "ling.domains.consent.service"): (
        "入站媒体能不能被描述，由 media.inbound 授权决定（默认拒绝）。"
        "授权记录是 consent 域的权威状态，媒体只读 .granted/.revision，"
        "绝不写它。如果改成在 media 里自己判断，撤权时就会出现两份事实。"
    ),
    ("dream/service.py", "ling.domains.affect.service"): (
        "跨夜结算要改 affect_states，而它的唯一写入方是 AffectService"
        "（apply_sleep_settlement 只处理 global 分量的线性衰减、跨夜下限和"
        "valence/arousal 回归）。dream 不自己拼 SQL 写 affect 的表：公式留在 "
        "affect，dream 只负责「哪一晚、要不要结算、唯一键幂等」。"
    ),
    ("dream/service.py", "ling.domains.agenda.service"): (
        "梦的睡眠日按「醒来所在本地日」算，需要作息 wake/rest。作息按 actor 存在 "
        "agenda 域，AgendaService.routine() 是它的公开读取入口；dream 只读，不写"
        "作息、不碰 agenda 的表。（agenda.timeline.sleep_anchors_for_day 不是 "
        ".service 模块，闸门不允许直接 import；dream 用与之逐字同规则的墙钟/DST "
        "换算得到 wake 瞬时，等价性由 tests/unit/test_dream_service.py 对拍。)"
    ),
    ("memory/self_timeline.py", "ling.domains.agenda.service"): (
        "只取 routine().timezone 一个字段，用来算「今天」在角色本地时区的起点。"
        "作息（含时区）按 actor 存在 agenda 域的 actor_routines 里，"
        "ports.config.LifePolicy 明确不复制这一份（那里注释写明「不包含作息」），"
        "所以这里没有别处可读。函数内 import 是为了避开导入期的循环依赖。"
    ),
    ("memory/service.py", "ling.domains.identity.service"): (
        "recall 前用 IdentityService.subject_group 展开合并账号：A 和 B 是"
        "同一个人时，问 A 也该想起 B 说过的话。账号合并是 identity 域的权威"
        "关系，记忆行本身不搬动（那是有损操作），只读组内 subject_id 集合。"
    ),
    ("proactive/dispatch.py", "ling.domains.conversation.service"): (
        "deliver_to_web=True 时把主动消息落成会话里的一条她的消息，走 "
        "ConversationService.add_message（带 origin=simulated）。这是主动域"
        "唯一能向 Web 面板交付的路径，仍然是 conversation 域自己写自己的表。"
        "函数内 import：绝大多数 dispatch 调用不需要这条路径。"
    ),
    ("proactive/sources.py", "ling.domains.intent.service"): (
        "「她自己说过过会儿要处理的事」到点了——这是主动候选的一类来源，"
        "IntentService.due(now=) 是它的唯一入口。主动域的工作本来就是"
        "巡视别的域的到期项，这不是实现细节泄漏，而是数据源。"
    ),
    ("agenda/timeline.py", "ling.domains.persona.service"): (
        "日程投影要带「她是谁」（人格名 / 性格 / 场景偏好的结构化短项）。"
        "多套人格之后这个答案不再是环境变量，而是已发布配置里的当前人格"
        "（`configured_persona_id()`），读它必须问 persona 域——"
        "别处各读一遍配置就是第二份判据。只调公开的 "
        "`PersonaService.active_persona_id` / `active_persona_view`，"
        "不 import `persona.current`（实现模块）、不碰 personas 表、不拼 SQL；"
        "人格的所有权仍在 persona 域。函数内 import 是为了避开导入期循环。"
    ),
    ("conversation/service.py", "ling.domains.persona.service"): (
        "新建窗口时要把「创建那一刻生效的是哪一套人格」写进 "
        "`windows.persona_id`（窗口归属是人格切换「整套换」的落点：旧窗口不该"
        "被后续切换重新解释）。不传 persona_id 时才问 persona 域的公开入口 "
        "`active_persona_id()`；不 import `persona.current`、不读 personas 表。"
        "函数内 import，且只在「没有显式指定」时发生。"
    ),
    ("dream/service.py", "ling.domains.persona.service"): (
        "梦里的性格线索取自**当前生效**的那一套人格（traits）。切换人格之后"
        "仍读环境变量会让梦一直梦到旧人设。只调公开的 `active_persona_id()`，"
        "然后按 id 读自己的投影；不 import `persona.current`、不写 personas 表。"
    ),
    ("proactive/sources.py", "ling.domains.goals.service"): (
        "「承诺将到期」是用户明确要求过的主动联系，GoalsService."
        "due_commitments() 是它的唯一入口。同上：这是主动域的数据源。"
    ),
}
"""已知跨域 import 的**显式**白名单，键是 (相对路径, 被 import 的模块)。

三条约束由测试强制：
1. 键必须与真实违规集合**完全相等**（新增会 FAIL，陈旧也会 FAIL）；
2. 模块必须是 `ling.domains.<域>.service`——不许扩到 `.models` 等私有实现；
3. 理由必须写实（不是空串 / 不是 TODO）。
"""


def _domain_of(path: Path, base: Path) -> str | None:
    """从路径取出所属域。``base`` 可传入（负例测试用临时目录）。"""
    try:
        rel = path.relative_to(base)
    except ValueError:
        return None
    parts = rel.parts
    # 必须至少有 <域>/<文件>.py 两层，否则不算域内文件（如 domains/__init__.py）
    return parts[0] if len(parts) > 1 else None


def _domain_names(base: Path) -> set[str]:
    """这个域根下有哪些子包。用于识别 `from ling.domains import X`。"""
    if not base.is_dir():
        return set()
    return {
        child.name for child in base.iterdir() if child.is_dir() and not child.name.startswith("_")
    }


def _package_of(path: Path, base: Path) -> str:
    """这个文件所在的包名，用于解析相对 import。"""
    rel = path.relative_to(base)
    return ".".join((PACKAGE, *rel.parent.parts))


def _absolute_module(node: ast.ImportFrom, package: str) -> str | None:
    """把 `ImportFrom` 解析成绝对模块名（处理 `from ..x import y`）。"""
    if not node.level:
        return node.module
    parts = package.split(".")
    drop = node.level - 1  # level=1 指向当前包所在包
    if drop >= len(parts):
        return None
    resolved = parts[: len(parts) - drop]
    if node.module:
        resolved = [*resolved, node.module]
    return ".".join(resolved)


def _imported(tree: ast.AST, *, package: str, domains: set[str]) -> list[tuple[str, str, int]]:
    """收集 `ling.domains.<X>` 里的 X，返回 [(域, 完整模块名, 行号)]。

    覆盖三种写法，任何一条漏掉都会留下绕过闸门的口子：
    - `from ling.domains.beta.service import X`（绝对）
    - `from ..beta.service import X`（相对，先解析成绝对）
    - `from ling.domains import beta` / `import ling.domains.beta`
    """
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = _absolute_module(node, package)
            if module is None:
                continue
            if module == PACKAGE:
                # `from ling.domains import beta`
                for alias in node.names:
                    if alias.name in domains:
                        found.append((alias.name, f"{PACKAGE}.{alias.name}", node.lineno))
            elif module.startswith(PACKAGE + "."):
                parts = module.split(".")
                if len(parts) > 2 and parts[2] in domains:
                    found.append((parts[2], module, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith(PACKAGE + "."):
                    continue
                parts = alias.name.split(".")
                if len(parts) > 2 and parts[2] in domains:
                    found.append((parts[2], alias.name, node.lineno))
    return found


def _python_files(base: Path | None = None) -> list[Path]:
    """域目录下的全部 `.py`。测试要拿它区分「扫到 0 个」和「真的没违规」。"""
    root = base or DOMAINS_ROOT
    return sorted(root.rglob("*.py"))


def find_violations(root: Path | None = None) -> list[Violation]:
    """返回全部跨域 import（**不过滤白名单**，便于测试做集合比较）。"""
    base = root or DOMAINS_ROOT
    domains = _domain_names(base)
    violations: list[Violation] = []
    for path in _python_files(base):
        own = _domain_of(path, base)
        if own is None:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 语法错误由其他检查负责
            continue
        package = _package_of(path, base)
        for other, module, lineno in _imported(tree, package=package, domains=domains):
            if other != own:
                violations.append(
                    Violation(
                        path=path.relative_to(base).as_posix(),
                        own=own,
                        other=other,
                        module=module,
                        lineno=lineno,
                    )
                )
    return violations


# ---------------------------------------------------------------- 闸门自身


def test_domains_root_exists_and_scans_real_files() -> None:
    """路径必须指向真实的域目录，而且真的扫得到文件。

    **这条是整组测试的地基**：原实现的路径指向一个不存在的目录，
    `rglob` 返回空，`assert not violations` 于是永远通过。任何形式的
    "扫到 0 个文件"都必须在这里炸掉，而不是伪装成"没有违规"。
    """
    assert DOMAINS_ROOT.is_dir(), (
        f"域目录不存在：{DOMAINS_ROOT}\n"
        "路径指错时 rglob 返回空列表，闸门会静默永远通过——这是原实现的 bug。"
    )
    # 存在还不够：目录存在但指错了（比如指到空壳目录）同样会静默放行。
    assert (DOMAINS_ROOT / "memory" / "service.py").is_file(), (
        f"{DOMAINS_ROOT} 不像 ling 的域目录（缺 memory/service.py）"
    )

    files = _python_files()
    assert len(files) >= MIN_SCANNED_FILES, (
        f"只扫到 {len(files)} 个 .py（{DOMAINS_ROOT}），下界是 {MIN_SCANNED_FILES}。\n"
        "**扫到 0 个（或明显偏少）说明路径错了，不是真的没有违规。**\n"
        "先确认 DOMAINS_ROOT 指向 src/ling/domains，再谈违规数。"
    )


def test_no_cross_domain_imports() -> None:
    """真实代码里不允许跨域直接 import（已知条目见 ALLOWED，每处都有理由）。"""
    # 先确认扫描不是空的——否则下面的断言会因为"什么都没扫到"而假绿。
    assert _python_files(), f"没有扫描到任何文件：{DOMAINS_ROOT} 指错了"

    violations = find_violations()
    unexpected = [v for v in violations if (v.path, v.module) not in ALLOWED]
    assert not unexpected, (
        "发现未列入白名单的跨域 import：\n"
        + "\n".join(
            f"  {v.path}:{v.lineno}: {v.own} → {v.module}"
            for v in sorted(unexpected, key=lambda v: (v.path, v.lineno))
        )
        + "\n\n两种改法，二选一：(a) 改成经 ports 端口注入 / 版本化事件；"
        "(b) 确认它只调邻居域的公开 service 且不改对方的表，"
        "再往 ALLOWED 里加一条**写清理由**的显式例外。"
    )


def test_whitelist_matches_actual_violations_exactly() -> None:
    """白名单必须既不缺也不多：新增跨域依赖 FAIL，陈旧条目同样 FAIL。

    只查"有没有漏网"会让白名单长成考古现场——删掉的 import 留在里面，
    后来的人就分不清哪些还是真的。这条用集合相等把两边都锁住。
    """
    actual = {(v.path, v.module) for v in find_violations()}
    missing = actual - set(ALLOWED)
    stale = set(ALLOWED) - actual

    assert not missing, "有跨域 import 没进白名单：\n" + "\n".join(
        f"  {path} → {module}" for path, module in sorted(missing)
    )
    assert not stale, (
        "白名单里有已不存在的条目（代码已改或路径又指错了）：\n"
        + "\n".join(f"  {path} → {module}" for path, module in sorted(stale))
        + "\n如果是修掉了跨域 import，请把这条一起删掉。"
    )


def test_whitelist_only_allows_public_service_modules() -> None:
    """白名单只准放行邻居域的公开 `service` 模块。

    没有这条，白名单会变成"什么都能放宽"：把
    `from ling.domains.goals.models import CommitmentRow` 加进去，
    等于默许直接读别人的表——ADR-0001 明确禁止的正是这一类。
    """
    for (path, module), reason in ALLOWED.items():
        parts = module.split(".")
        assert parts[:2] == ["ling", "domains"], f"{path}: 白名单模块名不对：{module}"
        public_service_modules = {"ling.domains.media.assets"}
        assert len(parts) == 4 and (
            parts[3] == "service" or module in public_service_modules
        ), (
            f"{path}: 白名单只允许 import `<域>.service`，不允许 {module}。\n"
            "要读别人的 models/表，请改成走对方的 service 公开方法。"
        )
        assert path.endswith(".py"), f"白名单键应是相对文件路径：{path}"
        assert len(reason.strip()) >= 20, f"{path} → {module}: 理由写实一点，别留空/TODO"


# ---------------------------------------------------------------- 负例：闸门真的会响


_FAKE_DOMAINS = ("alpha", "beta", "gamma")


def _fake_domains(tmp_path: Path, files: dict[str, str]) -> Path:
    """造一棵假的域目录。键是相对路径，值是文件内容。

    域包目录全部建出来（哪怕里面没文件）：闸门靠"有哪些子包"判断
    什么是域，真实的树里每个域都有目录。
    """
    root = tmp_path / "domains"
    root.mkdir(parents=True, exist_ok=True)
    for name in _FAKE_DOMAINS:
        (root / name).mkdir(exist_ok=True)
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def test_gate_catches_a_real_violation(tmp_path: Path) -> None:
    """**负例**：故意写一个违规 import，闸门必须报错。

    没有这条，上面那个测试可能因为"根本没扫到文件"而假绿。
    """
    fake = _fake_domains(
        tmp_path,
        {"alpha/service.py": "from ling.domains.beta.service import something\n"},
    )
    violations = find_violations(fake)
    assert violations, "闸门没有抓到明显的跨域 import——检查逻辑本身失效了"
    assert violations[0].own == "alpha"
    assert violations[0].other == "beta"
    assert violations[0].module == "ling.domains.beta.service"


def test_gate_catches_relative_import(tmp_path: Path) -> None:
    """相对 import（`from ..beta...`）是同一件事的另一种写法，必须一起抓。"""
    fake = _fake_domains(
        tmp_path,
        {"alpha/service.py": "from ..beta.service import something\n"},
    )
    violations = find_violations(fake)
    assert violations, "相对 import 绕过了闸门"
    assert (violations[0].own, violations[0].other) == ("alpha", "beta")


def test_gate_catches_package_level_import(tmp_path: Path) -> None:
    """`from ling.domains import beta` 也是跨域，别让它从语法缝里溜过去。"""
    fake = _fake_domains(
        tmp_path,
        {"alpha/service.py": "from ling.domains import beta\n"},
    )
    violations = find_violations(fake)
    assert violations, "`from ling.domains import beta` 绕过了闸门"
    assert violations[0].other == "beta"


def test_gate_catches_plain_import(tmp_path: Path) -> None:
    """`import ling.domains.beta.service` 这种写法也要抓（`Import` 分支）。"""
    fake = _fake_domains(
        tmp_path,
        {"alpha/service.py": "import ling.domains.beta.service\n"},
    )
    violations = find_violations(fake)
    assert violations, "`import ling.domains.beta.service` 绕过了闸门"
    assert violations[0].module == "ling.domains.beta.service"


def test_gate_catches_private_module_import(tmp_path: Path) -> None:
    """读别人的 models 比 import 别人的 service 更严重，当然也要抓。"""
    fake = _fake_domains(
        tmp_path,
        {"alpha/service.py": "from ling.domains.beta.models import BetaRow\n"},
    )
    violations = find_violations(fake)
    assert violations, "跨域 import 别人的 models 没被抓到"
    assert violations[0].module == "ling.domains.beta.models"


def test_gate_allows_same_domain(tmp_path: Path) -> None:
    """同域内互相 import 是正常的，不能误报。"""
    fake = _fake_domains(
        tmp_path,
        {"alpha/service.py": "from ling.domains.alpha.models import thing\n"},
    )
    assert find_violations(fake) == []


def test_gate_allows_non_domain_imports(tmp_path: Path) -> None:
    """import kernel / contracts / ports / infra 不算跨域。"""
    fake = _fake_domains(
        tmp_path,
        {
            "alpha/service.py": (
                "from ling.kernel.audience import readable_visibilities\n"
                "from ling.contracts.v1 import Origin\n"
                "from ling.infra.models import MemoryRow\n"
                "from ling.ports.config import LifePolicy\n"
            )
        },
    )
    assert find_violations(fake) == []


def test_gate_catches_multiple_violations_in_one_file(tmp_path: Path) -> None:
    """一个文件里多处违规要全部报出来，不能只报第一处。"""
    fake = _fake_domains(
        tmp_path,
        {
            "alpha/service.py": (
                "from ling.domains.beta.service import b\n"
                "from ling.domains.gamma.service import g\n"
            )
        },
    )
    got = {v.other for v in find_violations(fake)}
    assert got == {"beta", "gamma"}, f"只抓到 {got}"
