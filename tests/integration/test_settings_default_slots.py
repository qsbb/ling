"""默认槽位：五个槽位怎么存、每个职责最后跑哪个模型。

## 为什么要有这个文件

改版的硬要求是「界面上写着什么，实际就跑什么」。这句话要成立，必须做到：

1. **槽位是配置的一部分**（跟着修订走），不是前端的临时状态；
2. **生效解析在服务端**：`/settings/config` 与 `POST /settings/resolve`
   返回每个职责的 `{effective, source}`，前端只显示、不自己算；
3. **只回退一层**：单独指定 → 所属槽位 → 不可用，不跨槽位、不借别的角色。

第 3 条的单测在 `tests/unit/test_default_slots.py`；这里验的是**接口上**
真的这么返回，以及"预览"与"保存后"是同一份结论。
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

MAIN = {
    "provider_id": "main",
    "kind": "openai_compatible",
    "base_url": "https://192.0.2.10/v1",
    "enabled": True,
    "models": [{"model_id": "m-fast"}, {"model_id": "m-vision"}],
}


async def _draft(env: TestEnv, body: dict) -> dict:
    response = await env.client.put("/v1/settings/draft", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _errors(body: dict) -> list[dict]:
    return [e for e in body["validation_errors"] if e["severity"] == "error"]


async def _config(env: TestEnv) -> dict:
    return (await env.client.get("/v1/settings/config")).json()


def _resolution(config: dict, profile: str) -> dict:
    return next(r for r in config["profile_resolutions"] if r["profile"] == profile)


def _slot(config: dict, key: str) -> dict:
    return next(s for s in config["slots"] if s["slot"] == key)


async def _publish_slots(env: TestEnv) -> dict:
    """把「快速」指到 main/m-fast、「识图」指到 main/m-vision，并让两个职责跟随。"""
    body = await _draft(
        env,
        {
            "providers": [MAIN],
            "slots": [
                {"slot": "fast", "provider_id": "main", "model_id": "m-fast"},
                {"slot": "vision", "provider_id": "main", "model_id": "m-vision"},
            ],
            "profiles": [
                {"profile": "dialogue.fast", "mode": "slot"},
                {"profile": "perception.vision", "mode": "slot"},
            ],
        },
    )
    assert not _errors(body), body
    response = await env.client.post("/v1/settings/publish", json={"revision": body["revision"]})
    assert response.status_code == 200, response.text
    return body


# ------------------------------------------------------------ 槽位怎么存


async def test_config_exposes_five_slots_with_labels(logged_in: TestEnv) -> None:
    """五个槽位要有稳定键、中文名、一句说明、以及"谁在跟着我"。"""
    config = await _config(logged_in)

    assert [s["slot"] for s in config["slots"]] == [
        "fast",
        "creation",
        "vision",
        "video",
        "daily",
    ]
    labels = {s["slot"]: s["label"] for s in config["slots"]}
    assert labels == {
        "fast": "快速",
        "creation": "创作",
        "vision": "识图",
        "video": "视频",
        "daily": "日常",
    }
    for slot in config["slots"]:
        assert slot["hint"].strip(), f"{slot['slot']} 没有说明"
        # 谁跟着它由服务端算（映射表在 ports/model.py，前端不该手抄一份）
        assert isinstance(slot["roles"], list)
    assert _slot(config, "fast")["roles"] == ["dialogue.fast"]
    assert _slot(config, "vision")["roles"] == ["perception.vision"]
    # 视频是独立槽位：只跟 perception.video，不是和识图共用
    assert _slot(config, "video")["roles"] == ["perception.video"]
    assert _slot(config, "creation")["roles"] == ["memory.extract"]


async def test_slots_are_published_config_not_client_state(logged_in: TestEnv) -> None:
    """槽位必须落进已发布修订——否则刷新页面就没了。"""
    await _publish_slots(logged_in)

    config = await _config(logged_in)
    fast = _slot(config, "fast")
    assert (fast["provider_id"], fast["model_id"]) == ("main", "m-fast")
    assert fast["configured"] is True
    assert fast["usable"] is True


# ------------------------------------------------------------ 生效解析


async def test_following_slot_reports_slot_source(logged_in: TestEnv) -> None:
    """跟随槽位的职责：`source=slot`，`effective` 是槽位里那个具体模型。"""
    await _publish_slots(logged_in)

    config = await _config(logged_in)
    fast = _resolution(config, "dialogue.fast")
    assert fast["source"] == "slot"
    assert fast["slot"] == "fast"
    assert fast["effective"] == {"provider_id": "main", "model_id": "m-fast"}
    # 没单独选过 → 不是"已回退"，界面标「继承」
    assert fast["requested"] is None
    assert fast["fell_back"] is False

    vision = _resolution(config, "perception.vision")
    assert vision["source"] == "slot"
    assert vision["effective"] == {"provider_id": "main", "model_id": "m-vision"}


async def test_explicit_binding_reports_explicit_source(logged_in: TestEnv) -> None:
    """单独指定 → `source=explicit`，槽位怎么配都不影响它。"""
    body = await _draft(
        logged_in,
        {
            "providers": [MAIN],
            "slots": [{"slot": "fast", "provider_id": "main", "model_id": "m-vision"}],
            "profiles": [
                {"profile": "dialogue.fast", "provider_id": "main", "model_id": "m-fast"},
            ],
        },
    )
    assert not _errors(body), body
    await logged_in.client.post("/v1/settings/publish", json={"revision": body["revision"]})

    config = await _config(logged_in)
    resolved = _resolution(config, "dialogue.fast")
    assert resolved["source"] == "explicit"
    assert resolved["effective"] == {"provider_id": "main", "model_id": "m-fast"}


async def test_broken_explicit_falls_back_and_is_marked(logged_in: TestEnv) -> None:
    """单独指定的模型被停用 → 落回槽位，并带上"你选的什么"（界面标「已回退」）。"""
    await _publish_slots(logged_in)
    # 把 m-fast 停用，同时 dialogue.fast 明确指定它
    disabled = dict(MAIN, models=[{"model_id": "m-fast", "enabled": False}, {"model_id": "m-vision"}])
    body = await _draft(
        logged_in,
        {
            "providers": [disabled],
            "slots": [{"slot": "fast", "provider_id": "main", "model_id": "m-vision"}],
            "profiles": [
                {"profile": "dialogue.fast", "provider_id": "main", "model_id": "m-fast"},
            ],
        },
    )
    await logged_in.client.post("/v1/settings/publish", json={"revision": body["revision"]})

    config = await _config(logged_in)
    resolved = _resolution(config, "dialogue.fast")
    assert resolved["source"] == "slot"
    assert resolved["effective"] == {"provider_id": "main", "model_id": "m-vision"}
    assert resolved["requested"] == {"provider_id": "main", "model_id": "m-fast"}
    assert resolved["fell_back"] is True


async def test_unavailable_says_which_slot_is_empty(logged_in: TestEnv) -> None:
    """槽位没配 → `effective` 为 null，且原因点名是哪个槽位。"""
    config = await _config(logged_in)

    vision = _resolution(config, "perception.vision")
    assert vision["effective"] is None
    assert vision["source"] == "unavailable"
    assert "识图" in vision["reason"], vision["reason"]


async def test_memory_extract_borrows_dialogue_fast_but_says_so(logged_in: TestEnv) -> None:
    """提炼的槽位（创作）没配时：**继续借主对话模型，但必须标出来**。

    这条是这一版最关键的取舍。删掉借用会让老部署升级后静默降级——提炼掉回
    正则、召回变差，而页面只显示"不可用"，用户不知道这个能力曾经在。那正是
    这一轮要消灭的那类问题（界面说的 ≠ 实际发生的）。

    反过来保留借用却不标出来，就是旧版的老毛病：页面说"不可用"、其实在跑
    主对话模型。所以现在两条都要：**行为不退，来源明说**。

    `legacy_fallback` 是独立一档，不是槽位链的一环——槽位之间仍然只回退一层
    （见 `test_no_second_hop_across_slots`）。
    """
    await _publish_slots(logged_in)
    body = await _draft(
        logged_in,
        {"profiles": [{"profile": "memory.extract", "mode": "slot"}]},
    )
    await logged_in.client.post("/v1/settings/publish", json={"revision": body["revision"]})

    config = await _config(logged_in)
    fast = _resolution(config, "dialogue.fast")
    assert fast["effective"] is not None
    extract = _resolution(config, "memory.extract")
    # 真的借到了主对话的模型（行为不退）
    assert extract["source"] == "legacy_fallback"
    assert extract["effective"] == fast["effective"]
    # 而且说清了这是旧版行为，不是用户配的
    assert "旧版行为" in extract["reason"], extract["reason"]
    # 标成 legacy 之后就不该再报"会退回正则"——它并没有退
    assert "memory.extract" not in config["unbound_degradations"]


async def test_disabled_role_is_reported_separately(logged_in: TestEnv) -> None:
    """职责被停用 → `enabled=false`，不会被槽位"修好"。"""
    await _publish_slots(logged_in)
    body = await _draft(
        logged_in,
        {"profiles": [{"profile": "perception.vision", "mode": "slot", "enabled": False}]},
    )
    await logged_in.client.post("/v1/settings/publish", json={"revision": body["revision"]})

    config = await _config(logged_in)
    vision = _resolution(config, "perception.vision")
    assert vision["enabled"] is False
    assert vision["source"] == "unavailable"


# ------------------------------------------------------------ 草稿预览


async def test_resolve_endpoint_does_not_write_anything(logged_in: TestEnv) -> None:
    """预览**只算不写**：不产生修订、不碰已发布配置。"""
    before = await _config(logged_in)
    response = await logged_in.client.post(
        "/v1/settings/resolve",
        json={
            "providers": [MAIN],
            "slots": [{"slot": "fast", "provider_id": "main", "model_id": "m-fast"}],
            "profiles": [{"profile": "dialogue.fast", "mode": "slot"}],
        },
    )
    assert response.status_code == 200, response.text
    preview = response.json()
    assert _resolution(preview, "dialogue.fast")["effective"] == {
        "provider_id": "main",
        "model_id": "m-fast",
    }

    after = await _config(logged_in)
    assert after["revision"] == before["revision"], "预览不该产生新修订"
    assert _slot(after, "fast")["configured"] is False, "预览不该写进已发布配置"


async def test_resolve_endpoint_matches_what_saving_produces(logged_in: TestEnv) -> None:
    """**预览与保存必须给同一个答案**。

    这条是这次改版的核心不变式：预览错了等于骗用户按保存。所以两者共用
    同一段合并 + 同一份解析，这里用"先问再存再问"来钉住。
    """
    payload = {
        "providers": [MAIN],
        "slots": [{"slot": "fast", "provider_id": "main", "model_id": "m-fast"}],
        "profiles": [{"profile": "dialogue.fast", "mode": "slot"}],
    }
    preview = (
        await logged_in.client.post("/v1/settings/resolve", json=payload)
    ).json()

    body = await _draft(logged_in, payload)
    await logged_in.client.post("/v1/settings/publish", json={"revision": body["revision"]})
    after = await _config(logged_in)

    assert _resolution(preview, "dialogue.fast")["effective"] == _resolution(
        after, "dialogue.fast"
    )["effective"]
    assert preview["slots"] == after["slots"]


# ------------------------------------------------------------ 快速配置


async def test_quick_config_covers_manual_choices(logged_in: TestEnv) -> None:
    """快速配置 = **全量覆盖**，逐条说清每条会变成什么。

    语义选"全量覆盖"而不是"只填空"：只填空的话点完看起来什么都没发生，
    用户会以为按钮坏了。代价是必须把"将要覆盖哪几条"摆在明面上——所以
    这里是逐行的 `change`，不是一句"已同步"。
    """
    await _publish_slots(logged_in)
    body = await _draft(
        logged_in,
        {
            "providers": [MAIN],
            "profiles": [
                # 用户特意把主对话换成了别的模型
                {"profile": "dialogue.fast", "provider_id": "main", "model_id": "m-vision"},
                {"profile": "perception.vision", "mode": "slot"},
            ],
        },
    )
    await logged_in.client.post("/v1/settings/publish", json={"revision": body["revision"]})

    response = await logged_in.client.post(
        "/v1/settings/quick-config/preview",
        json={"providers": [MAIN]},
    )
    assert response.status_code == 200, response.text
    preview = response.json()

    rows = {row["profile"]: row for row in preview["rows"]}
    # 被用户单独指定过的那条 → 「覆盖」
    assert rows["dialogue.fast"]["change_kind"] == "override"
    assert "m-vision" in rows["dialogue.fast"]["change"], rows["dialogue.fast"]
    assert rows["dialogue.fast"]["slot_label"] == "快速"
    assert rows["dialogue.fast"]["model_id"] == "m-fast"
    # 本来就是跟随 → 「与现在相同」
    assert rows["perception.vision"]["change_kind"] == "same"
    # 槽位没配的那条 → 「仍然不可用」，并给出原因
    # 旧版兼容：创作槽位没配，快速配置给不了它模型，但它仍然会借主对话。
    # 文案必须说清"还是借"，不能写成"不可用"——那会让人去修一个其实在工作的东西。
    assert rows["memory.extract"]["change_kind"] == "legacy"
    assert "借主对话" in rows["memory.extract"]["change"], rows["memory.extract"]
    assert "创作" in rows["memory.extract"]["change"]

    # 嵌入/重排不参与：它们没有槽位
    assert "memory.embed" not in rows
    assert "memory.rerank" not in rows

    assert preview["summary"]["total"] == len(rows)
    assert preview["summary"]["overrides"] == 1

    # 返回的 profiles 就是"点确认后要写入的那份"，全部改成跟随槽位
    assert {p["mode"] for p in preview["profiles"]} == {"slot"}
    assert all(p["provider_id"] == "" for p in preview["profiles"])


async def test_quick_config_result_actually_applies(logged_in: TestEnv) -> None:
    """把预览返回的 profiles 原样存下去，每条职责就都跟着槽位了。"""
    await _publish_slots(logged_in)
    body = await _draft(
        logged_in,
        {"providers": [MAIN], "profiles": [{"profile": "dialogue.fast", "provider_id": "main", "model_id": "m-vision"}]},
    )
    await logged_in.client.post("/v1/settings/publish", json={"revision": body["revision"]})

    preview = (
        await logged_in.client.post(
            "/v1/settings/quick-config/preview", json={"providers": [MAIN]}
        )
    ).json()
    applied = await _draft(logged_in, {"profiles": preview["profiles"]})
    assert not _errors(applied), applied
    await logged_in.client.post(
        "/v1/settings/publish", json={"revision": applied["revision"]}
    )

    config = await _config(logged_in)
    fast = _resolution(config, "dialogue.fast")
    assert fast["source"] == "slot"
    assert fast["effective"] == {"provider_id": "main", "model_id": "m-fast"}


# ------------------------------------------------------------ 校验


async def test_slot_with_unknown_provider_is_rejected(logged_in: TestEnv) -> None:
    body = await _draft(
        logged_in,
        {"slots": [{"slot": "fast", "provider_id": "ghost", "model_id": "m"}]},
    )
    fields = {e["field"] for e in _errors(body)}
    assert "slots.fast.provider_id" in fields, body


async def test_slot_needs_a_model(logged_in: TestEnv) -> None:
    """只给供应商不给模型，是"配了一半"，不能静默当成没配。"""
    body = await _draft(
        logged_in,
        {
            "providers": [MAIN],
            "slots": [{"slot": "fast", "provider_id": "main", "model_id": ""}],
        },
    )
    fields = {e["field"] for e in _errors(body)}
    assert "slots.fast.model_id" in fields, body


async def test_slot_rejects_non_chat_provider(logged_in: TestEnv) -> None:
    """嵌入供应商不能当默认槽位：发布能成功、调用必失败。"""
    body = await _draft(
        logged_in,
        {
            "providers": [
                {
                    "provider_id": "cf",
                    "kind": "cloudflare_embedding",
                    "enabled": True,
                    "extra": {"account_id": "acc"},
                    "models": [{"model_id": "@cf/baai/bge-m3"}],
                }
            ],
            "slots": [{"slot": "fast", "provider_id": "cf", "model_id": "@cf/baai/bge-m3"}],
        },
    )
    errors = _errors(body)
    assert any(
        e["field"] == "slots.fast.provider_id" and "对话模型" in e["message"] for e in errors
    ), errors


async def test_embed_role_cannot_follow_a_slot(logged_in: TestEnv) -> None:
    """嵌入/重排没有默认槽位，选「跟随默认」等于把自己关掉——要拦下来。"""
    body = await _draft(
        logged_in,
        {"profiles": [{"profile": "memory.embed", "mode": "slot"}]},
    )
    fields = {e["field"] for e in _errors(body)}
    assert "profiles.memory.embed.mode" in fields, body


async def test_unknown_slot_key_is_rejected(logged_in: TestEnv) -> None:
    body = await _draft(
        logged_in,
        {
            "providers": [MAIN],
            "slots": [{"slot": "fastt", "provider_id": "main", "model_id": "m-fast"}],
        },
    )
    fields = {e["field"] for e in _errors(body)}
    assert "slots.fastt" in fields, body


# ------------------------------------------------------------ 向后兼容


async def test_profile_without_mode_still_reads_as_explicit(logged_in: TestEnv) -> None:
    """旧客户端不带 `mode`：必须按**显式**读。

    按 slot 读的话，所有老部署的绑定会一次性作废——升级即静默失效。
    """
    body = await _draft(
        logged_in,
        {
            "providers": [MAIN],
            "profiles": [{"profile": "dialogue.fast", "provider_id": "main", "model_id": "m-fast"}],
        },
    )
    assert not _errors(body), body
    await logged_in.client.post("/v1/settings/publish", json={"revision": body["revision"]})

    config = await _config(logged_in)
    stored = next(p for p in config["profiles"] if p["profile"] == "dialogue.fast")
    assert stored["mode"] == "explicit"
    assert _resolution(config, "dialogue.fast")["source"] == "explicit"


async def test_dialogue_fast_can_follow_slot_outside_test_mode(logged_in: TestEnv) -> None:
    """非测试模式下，`dialogue.fast` 只要**能解析出模型**就行。

    从前的校验是"必须有一条 dialogue.fast 绑定"，上了默认槽位之后这句话
    不对了：跟随「快速」槽位同样是在工作。按绑定记录判会把一份能用的配置
    拦在发布外面。
    """
    body = await _draft(
        logged_in,
        {
            "test_mode": False,
            "providers": [MAIN],
            "slots": [{"slot": "fast", "provider_id": "main", "model_id": "m-fast"}],
            # 引导配置把这几条绑在 fake 上；非测试模式下 fake 是硬拦截，
            # 所以一并改成跟随槽位（真正要验的是 dialogue.fast 那条）。
            "profiles": [
                {"profile": "dialogue.fast", "mode": "slot"},
                {"profile": "dialogue.group", "mode": "slot"},
                {"profile": "memory.extract", "mode": "slot"},
            ],
        },
    )
    assert not _errors(body), body
    response = await logged_in.client.post(
        "/v1/settings/publish", json={"revision": body["revision"]}
    )
    assert response.status_code == 200, response.text

    config = await _config(logged_in)
    assert _resolution(config, "dialogue.fast")["source"] == "slot"
    # 收尾：把测试模式改回去，别影响同库的其它用例
    restore = await _draft(logged_in, {"test_mode": True})
    await logged_in.client.post("/v1/settings/publish", json={"revision": restore["revision"]})


async def test_quick_config_reports_mode_change_even_when_model_is_same(
    logged_in: TestEnv,
) -> None:
    """**关键**：单独指定的模型和槽位恰好相同时，快速配置仍然在改东西。

    它把"你的单独指定"换成"跟随默认"——模型这一刻一样，但以后改槽位它就会
    跟着漂。只比生效模型的话会报成「与现在相同」，用户点下去才发现自己的选择
    被换掉了，这跟不给 diff 没区别。

    这是视觉验收实测出来的：确认弹窗说"没有需要覆盖的单独指定"，点确认后那条
    从「单独指定」变成「继承」，产生一处未保存改动。
    """
    await _publish_slots(logged_in)
    # 「快速」槽位是 m-fast；把日常对话**单独指定**成同一个 m-fast
    body = await _draft(
        logged_in,
        {
            "providers": [MAIN],
            "profiles": [
                {"profile": "dialogue.fast", "provider_id": "main", "model_id": "m-fast"},
            ],
        },
    )
    await logged_in.client.post("/v1/settings/publish", json={"revision": body["revision"]})

    response = await logged_in.client.post(
        "/v1/settings/quick-config/preview",
        json={"providers": [MAIN]},
    )
    assert response.status_code == 200, response.text
    rows = {row["profile"]: row for row in response.json()["rows"]}
    row = rows["dialogue.fast"]
    # 模型没变，但绑定方式要变 —— 必须报成"会覆盖"，不能报"与现在相同"
    assert row["change_kind"] == "override", row
    assert "跟随默认" in row["change"], row
    assert "单独指定" in row["change"], row
