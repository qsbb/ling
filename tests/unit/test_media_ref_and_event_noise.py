"""媒体引用长度/协议、心跳降噪、白名单拒绝日志开关。

三件事的共同点：都在**入站第一道闸**上，出错时表现为「机器人毫无反应」或
「日志被无信息量的行刷满」，而不是抛异常——所以必须有测试钉住。
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from ling.contracts.v1 import InteractionEnvelope
from ling.kernel import inbound_intake


def _envelope(**kw):
    base = dict(
        connector_instance="connector:onebot:test",
        external_event_id="evt_1",
        window_ref="window:group:700000001",
        account_ref="100200300",
    )
    base.update(kw)
    return InteractionEnvelope(**base)


def _onebot_connector():  # noqa: ANN202
    from connectors.onebot.connector import OneBotV11Connector
    from connectors.onebot.settings import OneBotConfig

    return OneBotV11Connector(
        OneBotConfig(
            api_base="http://onebot.test:5700",
            access_token="",
            secret="hmac-secret",
            self_id="10001",
            instance_id="onebot-unit",
        )
    )


def _onebot_event(segments: list[dict], *, text: str = "") -> dict:
    return {
        "time": 0,
        "self_id": 10001,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 1,
        "user_id": 20002,
        "message": segments,
        "raw_message": text or ";".join(seg.get("type", "") for seg in segments),
        "sender": {"nickname": "小不点"},
    }


# ---------------------------------------------------------------- 媒体引用


def test_long_qq_media_url_is_accepted() -> None:
    """真实的 QQ 图片 URL 有 300+ 字符，不能被 128 的 IdStr 卡掉。

    这条以前会整条消息被 pydantic 拒掉——正文一起丢，用户看到的是
    「发了一张图 + 一句话，机器人毫无反应」。
    """
    url = (
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid="
        + "AbCdEf" * 45
    )
    assert len(url) > 300
    env = _envelope(media_refs=[url])
    assert env.media_refs == [url]


def test_onebot_long_image_url_survives_normalize_inbound() -> None:
    """**端到端**：从 connector.py 的解析入口进，而不是直接构造契约对象。

    生产上的失败点正是 ``normalize_inbound`` 构造信封的那一步：以前它抛
    pydantic 的 ``ValidationError``，WS 循环只记一条「入站处理失败」，
    正文和图片一起丢。只测 ``InteractionEnvelope(...)`` 会绕过这段。
    """
    url = (
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid="
        + "AbCdEf" * 45
    )
    envelope = _onebot_connector().normalize_inbound(
        _onebot_event(
            [
                {"type": "text", "data": {"text": "这图什么意思"}},
                {"type": "image", "data": {"file": "abc.image", "url": url}},
            ],
            text="这图什么意思",
        )
    )
    assert envelope.media_refs == [url]
    assert any("这图什么意思" in (part.text or "") for part in envelope.parts)


def test_onebot_file_field_without_url_is_kept() -> None:
    """OneBot 的 ``file`` 是「文件名或文件 id」，可以**没有 url**。

    ``connectors/onebot/message.py`` 的 ``data.get("url") or data.get("file")``
    就是为这类实现兜底的（CQ 字符串 ``[CQ:image,file=abc.jpg]`` 同样只有
    file）。把它们也拒掉，等于对另一批部署重新制造「发图无反应」的 bug。
    """
    envelope = _onebot_connector().normalize_inbound(
        _onebot_event([{"type": "image", "data": {"file": "abc.jpg"}}])
    )
    assert envelope.media_refs == ["abc.jpg"]


def test_media_ref_rejects_dangerous_schemes() -> None:
    """``file:``/``data:`` 之类会被 ``fetch_media`` 拿去打开，必须拒。"""
    for bad in (
        "file:///etc/passwd",
        "file:/etc/passwd",
        "data:text/html,<script>",
        "javascript:alert(1)",
        "vbscript:msgbox(1)",
        "ftp://example.com/x",
        "blob:https://example.com/abc",
        "https:///no-host",
    ):
        with pytest.raises(ValidationError):
            _envelope(media_refs=[bad])


def test_media_ref_keeps_opaque_ids() -> None:
    """不带 scheme 的是不透明 id / 文件名，真实存在于 OneBot 的 file 字段。"""
    env = _envelope(media_refs=["obj/a.jpg", "abc.jpg", "AbCdEf0123.image"])
    assert env.media_refs == ["obj/a.jpg", "abc.jpg", "AbCdEf0123.image"]


def test_media_ref_rejects_overlong() -> None:
    with pytest.raises(ValidationError):
        _envelope(media_refs=["https://x/" + "y" * 2050])


def test_bad_media_ref_drops_the_attachment_not_the_message() -> None:
    """坏引用只丢**这个附件**，正文照常进信封。

    契约层会拒绝带 ``file:`` 引用的信封；如果渠道不先过滤，用户发
    "看看这个 file:///etc/passwd" 时整条消息（包括正文）都会被拒——
    那正是长 URL 那个 bug 的同一类伤害。
    """
    envelope = _onebot_connector().normalize_inbound(
        _onebot_event(
            [
                {"type": "text", "data": {"text": "这图什么意思"}},
                {"type": "image", "data": {"file": "file:///etc/passwd"}},
            ],
            text="这图什么意思",
        )
    )
    assert envelope.media_refs == []
    assert any("这图什么意思" in (part.text or "") for part in envelope.parts)


def test_one_bad_ref_does_not_drop_the_good_one() -> None:
    """一条消息两个附件：坏的走、好的留。不能一颗老鼠屎坏一锅汤。"""
    good = "https://gchat.qpic.cn/gchatpic_new/1/2-3-4/0?term=2"
    envelope = _onebot_connector().normalize_inbound(
        _onebot_event(
            [
                {"type": "text", "data": {"text": "两张图"}},
                {"type": "image", "data": {"url": good}},
                {"type": "image", "data": {"url": "data:image/png;base64,AAAA"}},
            ],
            text="两张图",
        )
    )
    assert envelope.media_refs == [good]


def test_overlong_media_ref_drops_the_attachment_not_the_message() -> None:
    """长度超限同理：拒绝这条引用，不拒整条消息（截断会指向别处，更危险）。"""
    envelope = _onebot_connector().normalize_inbound(
        _onebot_event(
            [
                {"type": "text", "data": {"text": "看"}},
                {"type": "image", "data": {"url": "https://x/" + "y" * 2050}},
            ],
            text="看",
        )
    )
    assert envelope.media_refs == []
    assert any("看" in (part.text or "") for part in envelope.parts)


@pytest.mark.asyncio
async def test_contract_violation_is_rejected_not_raised() -> None:
    """最后一道闸：其它生产者绕过渠道过滤时，也不能让上报路径抛 500。

    ValidationError 漏出去的话，HTTP 上报会 500，平台按"投递失败"反复重投
    同一条坏事件——一次坏输入变成重试风暴。契约校验失败要收成
    ``accepted=False``（200），与其它业务失败同一个出口。
    """

    class _BadConnector:
        connector_id = "bad"
        instance_id = "connector:bad:test"

        def normalize_inbound(self, payload):  # noqa: ANN001, ANN201
            # 模拟一个没做过滤、直接构造信封的生产者
            return _envelope(media_refs=["file:///etc/passwd"])

    app = type("App", (), {"state": object()})()
    result = await inbound_intake.process_inbound_payload(
        app, _BadConnector(), {"post_type": "message"}
    )
    assert result == {"accepted": False, "reason": "事件格式不合法"}


# ---------------------------------------------------------------- 心跳降噪


class _HeartbeatConnector:
    """最小 connector：只会对 meta_event 抛 ignored。"""

    connector_id = "onebot"
    instance_id = "connector:onebot:test"

    def normalize_inbound(self, payload):  # noqa: ANN001, ANN201
        from ling.ports.channel import ChannelError, ChannelErrorKind

        if payload.get("post_type") == "meta_event":
            raise ChannelError(ChannelErrorKind.ignored, "meta_event:heartbeat")
        raise AssertionError("本用例不该走到这里")


@pytest.mark.asyncio
async def test_heartbeat_produces_no_log(caplog: pytest.LogCaptureFixture) -> None:
    """心跳每 30 秒一条，记下来会把 500 条缓冲里的有效日志全挤出去。"""
    app = type("App", (), {"state": object()})()
    with caplog.at_level(logging.DEBUG, logger="ling.kernel.inbound"):
        result = await inbound_intake.process_inbound_payload(
            app, _HeartbeatConnector(), {"post_type": "meta_event"}
        )
    assert result["accepted"] is False
    assert [r for r in caplog.records if r.name == "ling.kernel.inbound"] == []


def test_onebot_meta_event_is_silently_ignored() -> None:
    """真实的 OneBot connector 必须把心跳归到 ``ignored``，不是 ``unsupported``。"""
    from ling.ports.channel import ChannelError, ChannelErrorKind

    with pytest.raises(ChannelError) as exc:
        _onebot_connector().normalize_inbound(
            {"post_type": "meta_event", "meta_event_type": "heartbeat"}
        )
    assert exc.value.kind is ChannelErrorKind.ignored


# ---------------------------------------------------------------- 拒绝日志


# ---------------------------------------------------------------- 日志分流


def test_behavior_and_runtime_logs_are_separated() -> None:
    """运行日志与处理日志必须能分开取。

    「她刚才在忙什么」和「api 刚才 500 了几次」是两种问题，混在一条流里
    谁也答不好：运维按 level 过滤会被行为日志淹掉，看行为又得在
    `/v1/version 200` 里翻。
    """
    import logging

    from ling.audit import behavior_event
    from ling.logging_setup import clear_log_buffer, recent_logs, setup_logging

    setup_logging("api")
    clear_log_buffer()
    behavior_event("attention_watch", "看了下 测试群，他们在聊游戏，插不上嘴")
    logging.getLogger("ling.api.access").info("请求完成")

    behavior = [e.message for e in recent_logs(kind="behavior")]
    runtime = [e.message for e in recent_logs(kind="runtime")]
    assert behavior == ["看了下 测试群，他们在聊游戏，插不上嘴"]
    assert runtime == ["请求完成"]
    # 不传 kind 时两条都在（默认视图不该丢东西）
    assert len(recent_logs()) == 2


def test_log_kind_is_exposed_on_every_entry() -> None:
    """分类由后端算好下发，前端不再从 attributes 里猜一遍。"""
    import logging

    from ling.logging_setup import clear_log_buffer, recent_logs, setup_logging

    setup_logging("api")
    clear_log_buffer()
    logging.getLogger("ling.api.access").info("请求完成")
    assert recent_logs()[0].to_dict()["kind"] == "runtime"


def test_unknown_log_kind_is_rejected() -> None:
    """不认识的筛选值必须报错——静默忽略会让人以为在看处理日志。"""
    from ling.logging_setup import recent_logs

    with pytest.raises(ValueError):
        recent_logs(kind="不存在的分类")


def test_attention_narration_is_human_readable() -> None:
    """轮询结果要能读成人话，而不是 `decision=silent`。"""
    from ling.domains.attention.service import PollResult, _narrate_poll

    skipped = _narrate_poll(
        PollResult(window_id="w", unseen_count=7, decision="skipped",
                   reason="他们在聊游戏，插不上嘴")
    )
    assert "7" in skipped and "没接话" in skipped and "插不上嘴" in skipped

    replied = _narrate_poll(
        PollResult(window_id="w", unseen_count=2, decision="replied")
    )
    assert "接话" in replied

    # 空轮询不该产生叙述（否则每 30 秒一条废话）
    assert _narrate_poll(PollResult(window_id="w", decision="empty")) == ""
