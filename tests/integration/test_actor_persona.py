"""人设 API：场景提示词的存取与生效。

只覆盖"接口这一层能保证的东西"：写进去能读回来、空串等于恢复默认、
未知键必须报错而不是静默丢弃。至于提示词有没有进上下文，见
`test_context_compile.py` 里的场景车道用例。
"""

from __future__ import annotations

from tests.integration.conftest import TestEnv


async def test_renaming_persona_invalidates_name_cache(logged_in) -> None:
    """改名后必须失效名字缓存。

    缓存用于"剥掉她自己加的名字前缀"。不失效的话，改名后仍按旧名判断，
    新名字的前缀就漏过去了——而且这种错只在改名后才出现，很难联想到缓存。
    """
    from ling.capabilities import naming

    await naming.display_name_for("persona_ling")
    before = dict(naming._NAME_CACHE)  # noqa: SLF001 - 就是要看缓存
    assert before, "缓存没建立"

    await logged_in.client.put(
        "/v1/persona",
        json={"display_name": "新名字", "system_prompt": "你是新名字"},
    )
    assert naming._NAME_CACHE == {}, "改名后缓存没有失效"  # noqa: SLF001

    assert await naming.display_name_for("persona_ling") == "新名字"


async def test_persona_exposes_scenario_catalog(logged_in: TestEnv) -> None:
    """默认情况下四个场景都在，且都标着"用的是默认"。"""
    response = await logged_in.client.get("/v1/persona")
    assert response.status_code == 200, response.text
    body = response.json()

    scenarios = {s["key"]: s for s in body["scenarios"]}
    assert set(scenarios) == {
        "private",
        "group",
        "group_mentioned",
        "group_unaddressed",
    }
    for scenario in scenarios.values():
        assert scenario["text"] == scenario["default_text"]
        assert scenario["custom"] is False
        assert scenario["label"] and scenario["when"]


async def test_scenario_override_roundtrip(logged_in: TestEnv) -> None:
    """写一条场景覆盖：读回来要变、标记要变成 custom、其它场景不受影响。"""
    private_default = _default_of(await _persona(logged_in), "private")

    response = await logged_in.client.put(
        "/v1/persona",
        json={"scenario_prompts": {"group": "群里基本不接话，除非有人点名"}},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    scenarios = {s["key"]: s for s in body["scenarios"]}
    assert scenarios["group"]["text"] == "群里基本不接话，除非有人点名"
    assert scenarios["group"]["custom"] is True
    # 只覆盖了一条，别的仍该是默认
    assert scenarios["private"]["text"] == private_default
    assert scenarios["private"]["custom"] is False
    assert body["scenario_prompts"] == {"group": "群里基本不接话，除非有人点名"}


async def test_blank_override_restores_default(logged_in: TestEnv) -> None:
    """空串是"恢复默认"的唯一表示：库里不该留一个空串当覆盖。"""
    default = _default_of(await _persona(logged_in), "group_unaddressed")
    await logged_in.client.put(
        "/v1/persona", json={"scenario_prompts": {"group_unaddressed": "先插一句试试"}}
    )
    response = await logged_in.client.put(
        "/v1/persona", json={"scenario_prompts": {"group_unaddressed": "  "}}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    scenarios = {s["key"]: s for s in body["scenarios"]}
    assert scenarios["group_unaddressed"]["text"] == default
    assert scenarios["group_unaddressed"]["custom"] is False
    assert body["scenario_prompts"] == {}


async def test_unknown_scenario_key_is_rejected(logged_in: TestEnv) -> None:
    """拼错键名必须报错。

    静默丢弃的话，用户会看到"保存成功"，然后发现什么都没变——而且不知道
    该怀疑哪里。这类"接受了但不生效"的输入是这一页最该拒绝的东西。
    """
    response = await logged_in.client.put(
        "/v1/persona", json={"scenario_prompts": {"group_named": "写错了键"}}
    )
    assert response.status_code == 400, response.text
    assert "group_named" in response.text

    # 报错之后不能有半截写入
    body = (await logged_in.client.get("/v1/persona")).json()
    assert body["scenario_prompts"] == {}


async def test_scenario_update_bumps_revision_and_timeline(logged_in: TestEnv) -> None:
    """改场景也是一次人设修订：要能在时间线里追溯"为什么她群里话变了"。"""
    before = (await _persona(logged_in))["revision"]
    await logged_in.client.put(
        "/v1/persona", json={"scenario_prompts": {"group": "群里只接技术问题"}}
    )
    after = await _persona(logged_in)
    assert after["revision"] == before + 1

    events = (await logged_in.client.get("/v1/actor/timeline?limit=20")).json()["events"]
    updated = [e for e in events if e["type"] == "persona.updated"]
    assert updated, "没有留下 persona.updated 事件"


async def _persona(env: TestEnv) -> dict:
    response = await env.client.get("/v1/persona")
    assert response.status_code == 200, response.text
    return response.json()


def _default_of(body: dict, key: str) -> str:
    return next(s["default_text"] for s in body["scenarios"] if s["key"] == key)
