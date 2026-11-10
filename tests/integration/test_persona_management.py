"""多套人格并存 + 切换：人设 API 的服务端语义。

覆盖的是**接口这一层能保证的东西**，每条对应一个"以前会静默错"的坑：

- 列表里的 ``is_active`` 与 ``active_persona_id`` 一致（"当前"只能有一个判据）；
- 新建**不会**自动生效（创建完用户还没写内容，不能把正在用的人格换掉）；
- 激活 = 发布一条新配置修订（跨副本/worker 生效、可回滚的依据）；
- 删当前激活的人格 → 409，不许删；
- 读过 / 点开过但没聊过的空窗口不阻止删除，**聊过**的会（409 + 数量）；
- 新装零人格：``GET /persona`` 返回空视图 + ``exists=false``，且**不建行**；
- ``PUT /persona`` 在新装时按当前激活 id 建行（编辑器的语义），
  而 ``PUT /personas/{id}`` 对不存在的 id 报 404（不建行）。
"""

from __future__ import annotations

from tests.integration.conftest import TestEnv


async def _active_id(env: TestEnv) -> str:
    response = await env.client.get("/v1/personas")
    assert response.status_code == 200, response.text
    return str(response.json()["active_persona_id"])


async def _persona_row_count(env: TestEnv) -> int:
    from sqlalchemy import func, select

    from ling.infra.db import session_scope
    from ling.infra.models import Persona

    async with session_scope() as session:
        return int((await session.execute(select(func.count()).select_from(Persona))).scalar_one())


# ---------------------------------------------------------------- 新装：暂无人设


async def test_fresh_install_has_no_persona_rows(logged_in: TestEnv) -> None:
    """新装**一行人格都没有**，`GET /persona` 也不建行。

    旧版 `ensure_default()` 会在启动时建一行 ``persona_ling``，于是"暂无人设"
    这个空状态在界面上永远不出现。这条锁住新行为。
    """
    assert await _persona_row_count(logged_in) == 0, "启动时不该建人格行"

    response = await logged_in.client.get("/v1/persona")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["exists"] is False, "新装应报告「这一行还不存在」"
    assert body["active"] is True
    assert body["system_prompt"] == "" and body["traits"] == {} and body["boundaries"] == []
    assert body["persona_id"] == logged_in.settings.persona_id
    # 读一次不会写库
    assert await _persona_row_count(logged_in) == 0, "GET 不该建行"

    listing = (await logged_in.client.get("/v1/personas")).json()
    assert listing == {
        "items": [],
        "active_persona_id": logged_in.settings.persona_id,
        "total": 0,
    }


async def test_put_persona_creates_row_at_active_id(logged_in: TestEnv) -> None:
    """`PUT /persona` 是"当前人格的编辑器"：新装时按当前 id 建行再写入。

    与 `PUT /personas/{id}`（必须已存在）刻意不同：人设页在新装上就是空表单，
    点保存时不该要求用户先点一次"创建人格"。
    """
    response = await logged_in.client.put(
        "/v1/persona", json={"display_name": "阿零", "system_prompt": "你是阿零。"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["exists"] is True
    assert body["persona_id"] == logged_in.settings.persona_id
    assert await _persona_row_count(logged_in) == 1

    again = await logged_in.client.put("/v1/persona", json={"display_name": "零"})
    assert again.status_code == 200, again.text
    assert again.json()["display_name"] == "零"
    assert await _persona_row_count(logged_in) == 1, "第二次保存不该再建行"


# ---------------------------------------------------------------- 增删改查


async def test_create_list_detail_roundtrip(logged_in: TestEnv) -> None:
    created = await logged_in.client.post(
        "/v1/personas",
        json={
            "display_name": "温柔",
            "system_prompt": "说话轻一点。",
            "traits": {"tone": "偏冷"},
            "boundaries": ["不编造现实履历"],
        },
    )
    assert created.status_code == 201, created.text
    view = created.json()
    pid = view["persona_id"]
    # id 由服务端生成：可读前缀 + 随机后缀，不是写死的 persona_ling
    assert pid.startswith("persona_"), pid
    assert pid != "persona_ling"
    assert view["display_name"] == "温柔"

    detail = await logged_in.client.get(f"/v1/personas/{pid}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["system_prompt"] == "说话轻一点。"

    listing = (await logged_in.client.get("/v1/personas")).json()
    assert listing["total"] == 1
    card = listing["items"][0]
    assert card["persona_id"] == pid
    assert card["has_content"] is True
    assert card["trait_count"] == 1
    assert card["boundary_count"] == 1
    # 新建不自动生效
    assert card["is_active"] is False
    assert listing["active_persona_id"] != pid


async def test_list_card_hides_prompt_body(logged_in: TestEnv) -> None:
    """列表不带正文：正文上限 20 万字符，卡片上只需要"有没有写"。"""
    await logged_in.client.post(
        "/v1/personas", json={"display_name": "长", "system_prompt": "设定" * 100}
    )
    card = (await logged_in.client.get("/v1/personas")).json()["items"][0]
    assert "system_prompt" not in card
    assert card["system_prompt_chars"] == 200
    assert card["has_content"] is True


async def test_empty_persona_card_reports_no_content(logged_in: TestEnv) -> None:
    """只有名字、三列全空 → `has_content=false`（与注入侧同一条判据）。"""
    created = await logged_in.client.post("/v1/personas", json={"display_name": "空壳"})
    assert created.status_code == 201, created.text
    card = (await logged_in.client.get("/v1/personas")).json()["items"][0]
    assert card["has_content"] is False


async def test_update_unknown_persona_is_404_and_does_not_create(logged_in: TestEnv) -> None:
    """`PUT /personas/{id}` 对不存在的 id 报 404，**不顺手建行**。

    旧版 `PersonaService.update()` 读不到就 `ensure_default()`，
    于是"传错 id"的结果是**静默造出一行空人格**并把它当成当前人格。
    """
    response = await logged_in.client.put(
        "/v1/personas/persona_does_not_exist", json={"display_name": "x"}
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["detail"] == "persona_not_found"
    assert await _persona_row_count(logged_in) == 0, "更新不得建行"


async def test_get_unknown_persona_is_404(logged_in: TestEnv) -> None:
    response = await logged_in.client.get("/v1/personas/persona_nope")
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------- 切换


async def test_activate_switches_and_publishes_revision(logged_in: TestEnv) -> None:
    """设为当前 = 改配置里的 persona_id **并发布一条新修订**。"""
    pid = (
        await logged_in.client.post(
            "/v1/personas", json={"display_name": "乙", "system_prompt": "你是乙。"}
        )
    ).json()["persona_id"]

    before = (await logged_in.client.get("/v1/settings/config")).json()["revision"]
    response = await logged_in.client.post(f"/v1/personas/{pid}/activate")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["persona_id"] == pid
    assert body["config_revision"] > before, "切换必须发布一条新修订"

    # 下一步就必须生效：`/persona` 读到的就是刚激活的那一套
    current = (await logged_in.client.get("/v1/persona")).json()
    assert current["persona_id"] == pid
    assert current["system_prompt"] == "你是乙。"
    assert current["exists"] is True

    listing = (await logged_in.client.get("/v1/personas")).json()
    assert listing["active_persona_id"] == pid
    assert [c["is_active"] for c in listing["items"]] == [True]


async def test_activate_unknown_persona_is_404(logged_in: TestEnv) -> None:
    """切到不存在的 id → 404：否则"当前人格指向空行"，看起来像人设被清空了。"""
    response = await logged_in.client.post("/v1/personas/persona_ghost/activate")
    assert response.status_code == 404, response.text


async def test_activate_is_reversible_by_rollback(logged_in: TestEnv) -> None:
    """切换人格跟着配置修订走，回滚修订时人格一起回去。"""
    a = (
        await logged_in.client.post("/v1/personas", json={"display_name": "A"})
    ).json()["persona_id"]
    b = (
        await logged_in.client.post("/v1/personas", json={"display_name": "B"})
    ).json()["persona_id"]
    await logged_in.client.post(f"/v1/personas/{a}/activate")
    target = (await logged_in.client.get("/v1/settings/config")).json()["revision"]
    await logged_in.client.post(f"/v1/personas/{b}/activate")
    assert await _active_id(logged_in) == b

    rolled = await logged_in.client.post("/v1/settings/rollback", json={"to_revision": target})
    assert rolled.status_code == 200, rolled.text
    assert await _active_id(logged_in) == a, "回滚后人格应当回到修订里的那一套"


# ---------------------------------------------------------------- 删除


async def test_delete_active_persona_is_refused(logged_in: TestEnv) -> None:
    """删当前激活的人格 → 409 `persona_active`，必须先切走。"""
    pid = (
        await logged_in.client.post("/v1/personas", json={"display_name": "当前"})
    ).json()["persona_id"]
    await logged_in.client.post(f"/v1/personas/{pid}/activate")

    response = await logged_in.client.delete(f"/v1/personas/{pid}")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["detail"] == "persona_active"
    # 还在
    assert (await logged_in.client.get(f"/v1/personas/{pid}")).status_code == 200


async def test_delete_unused_persona_succeeds(logged_in: TestEnv) -> None:
    """从未使用的人格可以删（响应 204，列表里也没了）。"""
    pid = (
        await logged_in.client.post("/v1/personas", json={"display_name": "弃用"})
    ).json()["persona_id"]
    response = await logged_in.client.delete(f"/v1/personas/{pid}")
    assert response.status_code == 204, response.text
    assert (await logged_in.client.get(f"/v1/personas/{pid}")).status_code == 404
    assert (await logged_in.client.get("/v1/personas")).json()["total"] == 0


async def test_delete_unknown_persona_is_404(logged_in: TestEnv) -> None:
    response = await logged_in.client.delete("/v1/personas/persona_missing")
    assert response.status_code == 404, response.text


async def test_delete_refused_when_windows_have_messages(logged_in: TestEnv) -> None:
    """名下还有**有内容的会话** → 409 `persona_in_use:<n>`，不删。

    空窗口（一次都没聊过）不该拦删除：人设页点开一张卡就会建窗口，
    拿它拦删除只会逼用户去手工删库。
    """
    from ling.infra.db import session_scope
    from ling.infra.models import Window

    pid = (
        await logged_in.client.post("/v1/personas", json={"display_name": "在用"})
    ).json()["persona_id"]
    async with session_scope() as session:
        # 空窗口：不拦
        session.add(
            Window(
                id="win_empty_persona_probe",
                actor_id=logged_in.settings.actor_id,
                persona_id=pid,
                subject_id=logged_in.subject_id,
                title="没聊过",
                channel="web",
                message_count=0,
            )
        )
    first = await logged_in.client.delete(f"/v1/personas/{pid}")
    assert first.status_code == 204, first.text

    pid2 = (
        await logged_in.client.post("/v1/personas", json={"display_name": "在用2"})
    ).json()["persona_id"]
    async with session_scope() as session:
        session.add(
            Window(
                id="win_used_persona_probe",
                actor_id=logged_in.settings.actor_id,
                persona_id=pid2,
                subject_id=logged_in.subject_id,
                title="聊过",
                channel="web",
                message_count=3,
            )
        )
    second = await logged_in.client.delete(f"/v1/personas/{pid2}")
    assert second.status_code == 409, second.text
    assert second.json()["error"]["detail"] == "persona_in_use:1"
    assert (await logged_in.client.get(f"/v1/personas/{pid2}")).status_code == 200


# ---------------------------------------------------------------- 硬编码清理


async def test_switching_persona_changes_what_new_windows_record(logged_in: TestEnv) -> None:
    """新建会话记录的是**当前激活**的人格，不是环境变量里的那个。

    回归点：`ConversationService` 原来默认 ``persona_id="persona_ling"``，
    切了人格之后新建的窗口仍挂旧 id，界面上完全看不出来。
    """
    from ling.domains.conversation.service import ConversationService
    from ling.infra.db import session_scope

    pid = (
        await logged_in.client.post("/v1/personas", json={"display_name": "丙"})
    ).json()["persona_id"]
    await logged_in.client.post(f"/v1/personas/{pid}/activate")

    async with session_scope() as session:
        window = await ConversationService(
            session, actor_id=logged_in.settings.actor_id
        ).create_window(subject_id=logged_in.subject_id, title="切换之后")
    assert window.persona_id == pid


async def test_persona_ling_literal_is_not_used_as_a_default() -> None:
    """写死的 ``persona_ling`` 只允许出现在两处"兼容默认值"里。

    用 **AST** 而不是 grep：注释与 docstring 里讨论这个字面量是正常的
    （它们正是在解释为什么不该再写死），只有真正的 **代码常量**才算硬编码。

    允许的两处：
    - `config.py`：`LING_PERSONA_ID` 的初始默认（用户要求保留为初始默认）；
    - `ports/config.py`：`EffectiveConfig.persona_id` 的字段默认值（数据契约）。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "ling"
    allowed = {"config.py", "ports/config.py"}  # 相对 `src/ling` 的路径
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value == "persona_ling" and id(node) not in docstrings:
                rel = path.relative_to(root)
                if str(rel) in allowed:
                    continue
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, "还有写死的 persona_ling 代码常量：" + "、".join(offenders)
