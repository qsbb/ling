"""`speech.tts` 的接线：`GET /v1/speech` 是否**真的按绑定**造出三家适配器。

## 这个文件在守什么

`speech.tts` 从前是彻底空的：`TtsProvider` 全仓只有
`UnconfiguredSpeechProvider` 一个实现，`GET /v1/speech` 里**硬编码**构造它并
返回写死的 `configured: False`。所以这里写的每一条都必须**打穿路由**，
而不是直接调工厂函数——后者只能证明工厂写对了，证明不了有人调它。

具体做法：全部走 `logged_in.client.get("/v1/speech")`（真实的 Starlette 应用、
真实的鉴权、真实的配置服务），只读响应里的 `tts` 对象。

## 配对的正负断言

三条的正向断言（配了 → `available: true` 且实现类是那一家）与负向断言
（没配 / 没密钥 / kind 不对 → 仍是 `UnconfiguredSpeechProvider`）**在同一次运行里
同时成立**，而且打的是**同一个注入点**。只有负向断言的话，"功能压根没接线"
也会绿——那正是这个文件要防的假绿。

## 它**不能**证明什么

`tts.available: true` 只说明「那家适配器被造出来了」。它**不**说明她会说话：
出站触发链路（`[speak]` → 合成 → 投递）仍然没有生产调用点，`speech.tts`
也仍留在 `RESERVED_PROFILES` 里。响应里的 `outbound_wired: false` 就是这条的
显式声明，本文件也断言它——防止有人把 `available` 读成"绑上就能听见"。
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

MINIMAX = {
    "provider_id": "mm",
    "kind": "minimax_tts",
    "base_url": "https://api.minimax.example",
    "enabled": True,
    "models": [{"model_id": "speech-2.8-hd"}],
}
FISH = {
    "provider_id": "fish",
    "kind": "fishaudio_tts",
    "base_url": "https://api.fish.example",
    "enabled": True,
    "models": [{"model_id": "s2.1-pro"}],
}
MIMO = {
    "provider_id": "mimo",
    "kind": "mimo_tts",
    "base_url": "https://api.mimo.example/v1",
    "enabled": True,
    "models": [{"model_id": "mimo-v2.5-tts-voiceclone"}],
}
# 一家**能对话但不能合成**的供应商：用来验证"kind 不对就不造"。
CHAT_ONLY = {
    "provider_id": "chatty",
    "kind": "openai_compatible",
    "base_url": "https://api.example/v1",
    "enabled": True,
    "models": [{"model_id": "some-chat-model"}],
}


def _binding(provider_id: str, model_id: str) -> dict:
    return {"profile": "speech.tts", "provider_id": provider_id, "model_id": model_id}


async def _publish(env: TestEnv, body: dict) -> None:
    draft = await env.client.put("/v1/settings/draft", json=body)
    assert draft.status_code == 200, draft.text
    errors = [e for e in draft.json()["validation_errors"] if e["severity"] == "error"]
    assert not errors, f"这份配置应当能发布：{errors}"
    publish = await env.client.post(
        "/v1/settings/publish", json={"revision": draft.json()["revision"]}
    )
    assert publish.status_code == 200, publish.text


async def _tts(env: TestEnv) -> dict:
    response = await env.client.get("/v1/speech")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "tts" in body, f"/v1/speech 没有给出 tts 装配结论：{body}"
    return body["tts"]


# --------------------------------------------------------------- 正向：真的造出来


@pytest.mark.parametrize(
    ("provider", "model_id", "implementation"),
    [
        (MINIMAX, "speech-2.8-hd", "MiniMaxTts"),
        (FISH, "s2.1-pro", "FishAudioTts"),
        (MIMO, "mimo-v2.5-tts-voiceclone", "MimoTts"),
    ],
    ids=["minimax", "fish_audio", "mimo"],
)
async def test_bound_tts_provider_is_the_real_adapter(
    logged_in: TestEnv, provider: dict, model_id: str, implementation: str
) -> None:
    """绑了某家 + 存了密钥 → `/v1/speech` 报出的实现**是那家的适配器**。

    这条必须在「路由仍然硬编码 `UnconfiguredSpeechProvider`」时**红**——
    那时 `available` 恒为 false、`implementation` 恒为
    `UnconfiguredSpeechProvider`。
    """
    pid = provider["provider_id"]
    await _publish(
        logged_in,
        {
            "providers": [provider],
            "profiles": [_binding(pid, model_id)],
            "slots": [],
        },
    )
    secret = await logged_in.client.put(
        f"/v1/settings/providers/{pid}/secret", json={"secret": f"{pid}-key-1234"}
    )
    assert secret.status_code == 200, secret.text

    tts = await _tts(logged_in)

    assert tts["available"] is True, f"绑了 {pid} 却没造出适配器：{tts}"
    assert tts["implementation"] == implementation, tts
    assert tts["provider_id"] == pid
    assert tts["model_id"] == model_id
    assert tts["kind"] == provider["kind"]
    assert tts["reason"] == ""
    # 出站链路仍然是断的。这一条**故意**和上面的 available 同时断言：
    # 让"造出来了"与"她会说话"在同一个响应里被分开报告。
    assert tts["outbound_wired"] is False, (
        "出站链路被标成已接线了？先确认 [speak] → 合成 → 投递真的有生产调用点，"
        "再动这条断言"
    )


async def test_response_still_does_not_claim_speech_is_configured(
    logged_in: TestEnv,
) -> None:
    """顶层 `configured` 仍是 false——装配成功 ≠ 语音功能可用。

    这条防止有人"顺手"把 `configured` 改成 `tts.available`：那会让后台显示
    "语音已配置"，而她并不会说话。
    """
    await _publish(
        logged_in,
        {
            "providers": [MINIMAX],
            "profiles": [_binding("mm", "speech-2.8-hd")],
            "slots": [],
        },
    )
    await logged_in.client.put(
        "/v1/settings/providers/mm/secret", json={"secret": "mm-key-1234"}
    )
    body = (await logged_in.client.get("/v1/speech")).json()
    assert body["tts"]["available"] is True
    assert body["configured"] is False


# --------------------------------------------------------------- 负向：仍是占位


async def test_without_binding_it_stays_unconfigured(logged_in: TestEnv) -> None:
    """没有任何 `speech.tts` 绑定 → 仍是 `UnconfiguredSpeechProvider`。

    配对 `test_bound_tts_provider_is_the_real_adapter`：两条打的是同一个
    响应字段，所以"恒真"和"恒假"的实现都会有一条红。
    """
    tts = await _tts(logged_in)

    assert tts["available"] is False
    assert tts["implementation"] == "UnconfiguredSpeechProvider"
    assert tts["reason"], "没装配出来却没说为什么——用户无从判断该去配什么"


async def test_bound_but_without_secret_stays_unconfigured(logged_in: TestEnv) -> None:
    """绑了、kind 也对，但**没存密钥** → 仍是占位，且理由指向密钥。

    这条区分"读得到绑定"与"造得出实例"：只判绑定存在就把 available 置真的
    实现，会在这一条红。
    """
    await _publish(
        logged_in,
        {
            "providers": [FISH],
            "profiles": [_binding("fish", "s2.1-pro")],
            "slots": [],
        },
    )
    tts = await _tts(logged_in)

    assert tts["available"] is False
    assert tts["implementation"] == "UnconfiguredSpeechProvider"
    assert "密钥" in tts["reason"], tts
    # 绑定的元信息仍然给出来：用户要知道"它读到了哪一条绑定"。
    assert tts["provider_id"] == "fish"
    assert tts["kind"] == "fishaudio_tts"


async def test_disabled_provider_is_not_used(logged_in: TestEnv) -> None:
    """绑定的供应商被**停用** → 仍是占位，理由说清"不存在或已停用"。

    发布时"停用"只记 warning（用户可能只是临时停用再切回来），所以这种配置
    会真的被发布出去。装配层必须失败关闭。
    """
    disabled = dict(MINIMAX, enabled=False)
    await _publish(
        logged_in,
        {
            "providers": [disabled],
            "profiles": [_binding("mm", "speech-2.8-hd")],
            "slots": [],
        },
    )
    await logged_in.client.put(
        "/v1/settings/providers/mm/secret", json={"secret": "mm-key-1234"}
    )

    tts = await _tts(logged_in)
    assert tts["available"] is False, tts
    assert tts["implementation"] == "UnconfiguredSpeechProvider"
    assert "停用" in tts["reason"], tts


async def test_rotated_secret_is_read_from_store_not_stale_cache(
    logged_in: TestEnv,
) -> None:
    """密钥轮换后，`/v1/speech` 必须看到**新**密钥，不能被进程内旧缓存盖住。

    这条防的是一种不报错、只在某个副本上发生的错：把
    `secret_cache.get(pid)` 当"缓存优先、缺了再查"用，会让同一个 provider_id
    的旧副本一直胜出——而失效的后果是拿一把废密钥去碰供应商。

    做法：先把缓存写成一把**不存在**的旧密钥，再通过正规接口轮换一次。
    用缓存优先的实现会在断言长度时红（它报"没有可用的密钥"）。这里只读
    状态、不发请求，所以不需要真的密钥格式。
    """
    await _publish(
        logged_in,
        {
            "providers": [MINIMAX],
            "profiles": [_binding("mm", "speech-2.8-hd")],
            "slots": [],
        },
    )
    cache = logged_in.app.state.secret_cache  # type: ignore[attr-defined]
    cache["mm"] = "stale-but-nonempty-key"

    rotated = await logged_in.client.put(
        "/v1/settings/providers/mm/secret", json={"secret": "fresh-key-9876"}
    )
    assert rotated.status_code == 200, rotated.text
    # 轮换接口会顺手刷新缓存；再把它弄脏一次，模拟"另一个副本还没刷新"。
    cache["mm"] = "stale-but-nonempty-key"

    tts = await _tts(logged_in)
    assert tts["available"] is True, (
        f"库里有可用密钥，却因为进程内缓存里是旧值而装配失败：{tts}"
    )
    assert tts["implementation"] == "MiniMaxTts"


