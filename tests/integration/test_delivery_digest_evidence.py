"""投递操作记录的**对账证据**：`text_digest` 什么时候该写、什么时候绝不能写。

## 这个文件在守什么

`DeliveryService.deliver` 原来无条件写
``payload["text_digest"] = delivery_text_digest(message.text)``。而
``delivery_text_digest`` 对空串/纯空白**恒定**返回
``sha256:e3b0c44298fc1c149afbf4c8996fb924``（空输入的 sha256），于是：

- 语音消息（``text=""``，§17.5 规定的 wire 形态）**每一条**都带同一个摘要；
- ``connectors/onebot/connector.py::_scan_history`` 拿摘要逐条比对平台历史
  （``delivery_text_digest(parsed.text) != digest`` → ``continue``），空摘要会命中
  **任何一条正文为空**的历史消息，``hits`` 里于是装进了别人的 message_id。

设计文档 `docs/design/SPEECH-IO-ARCHITECTURE.md:1884` 原文：

> 语音对账使用平台 message_id 或真实媒体证据，**禁止空 text_digest 匹配所有语音**

所以"摘要键存在"必须**蕴含**"它确实是某一条非空正文的摘要"。本文件把那句话
拆成可执行的四条：

1. 语音 payload **没有** `text_digest`（连键都没有）；
2. 纯文本 payload 的 `text_digest` **逐字节不变**（钉死字面量，防止顺手改坏）；
3. 两条**不同**的语音 payload 不靠同一个摘要混同——且各自有可区分的语音标识；
4. 缺摘要时 `_scan_history` 返回"没有证据"（``ok=False``），**不是**匹配到任意一条。

## 它**不能**证明什么

- 真实 OneBot/NapCat 的历史接口行为：第 4 条是单元级构造（``_call`` 被替换），
  没有真的去平台查历史；
- "语音送达后被正确判定"：这里只证明**没有证据时不乱认**，不证明有证据时一定认对；
- 媒体证据（音频）能被拿去平台比对：``payload["speech"]["digest"]`` 只是本地资产
  digest，平台侧的消息里没有它，**目前无法用它做内容比对**（见文件末尾说明）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ling.capabilities.delivery import DeliveryService
from ling.contracts.v1 import (
    AttachmentKind,
    CapabilityDescriptor,
    DeliveryReceipt,
    DeliveryState,
    Modality,
    OperationState,
    OutboundAttachment,
)
from ling.infra.db import session_scope
from ling.infra.models import Operation
from ling.ports.channel import (
    OutboundMessage,
    ReceiptQuery,
    delivery_text_digest,
)

#: 空串／纯空白的 ``delivery_text_digest`` 值。**字面量不从被测代码取**：
#: 被测代码里改坏了它，测试要跟着报警，而不是跟着坏。
#:
#: 它是 ``sha256("")`` 的前 32 个十六进制字符——所以它跟"某条正文"没有关系，
#: 它是**所有**空正文共用的值。两条语音 operation 带上它，摘要就只剩"非空"这
#: 一个比特的信息。
EMPTY_TEXT_DIGEST = "sha256:e3b0c44298fc1c149afbf4c8996fb924"

#: 纯文本正文的摘要**字面量**（改动前后都必须等于它）。
#:
#: 钉字面量是为了抓"顺手改坏了纯文本路径"这一类回归：如果只写
#: ``payload["text_digest"] == delivery_text_digest(TEXT)``，那么把
#: ``delivery_text_digest`` 本身改坏（例如换掉哈希截断长度）测试仍然全绿。
PLAIN_TEXT = "今天有点累，但看到你消息还是挺高兴的。"
PLAIN_TEXT_DIGEST = "sha256:404d38e64ce1d0b77957cf16f3740311"
SECOND_TEXT = "好"
SECOND_TEXT_DIGEST = "sha256:f867f34178594f891eb70a4260f62b1d"

WINDOW_REF = "onebot-test:group:100200300"


class _RecordingConnector:
    """假渠道：记下外发消息，回一个**没有平台 id** 的已送达回执。

    没有平台 id 是刻意的：那正是"只能靠内容摘要/媒体证据对账"的那条路。
    """

    connector_id = "onebot"
    instance_id = "onebot-digest-evidence"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    def capabilities(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            capability_id="onebot",
            version="1.0",
            enabled=True,
            supports_private=True,
            supports_group=True,
            supported_media=[Modality.audio, Modality.image],
            max_text_length=2000,
        )

    async def send(self, message):  # noqa: ANN001,ANN201
        self.sent.append(message)
        return DeliveryReceipt(
            receipt_id=f"rcpt_{len(self.sent)}",
            operation_id=message.operation_id,
            channel=self.connector_id,
            state=DeliveryState.delivered,
            platform_message_id=None,
            observed_at=datetime.now(UTC),
            detail="",
        )

    async def query_receipt(self, query):  # noqa: ANN001,ANN201
        raise NotImplementedError("本文件不对账，只验证 payload 里放了什么证据")


async def _deliver(
    actor_ids: dict[str, str | None],
    *,
    message: OutboundMessage,
    payload_extra: dict | None = None,
    plan_id: str | None = None,
    part_ordinal: int | None = None,
) -> Operation:
    """跑一次真实 ``DeliveryService.deliver``，返回落库的那条 operation。"""
    connector = _RecordingConnector()
    async with session_scope() as session:
        outcome = await DeliveryService(session).deliver(
            connector=connector,
            message=message,
            actor_id=str(actor_ids["actor_id"]),
            subject_id=actor_ids["subject_id"],
            payload_extra=payload_extra,
            plan_id=plan_id,
            part_ordinal=part_ordinal,
        )
        await session.commit()
    assert outcome.operation_id, outcome
    # 正向配对：消息**真的**发出去过。否则下面"没写摘要"在"根本没投递"时也成立。
    assert len(connector.sent) == 1, f"消息没被发出：{connector.sent}"
    async with session_scope() as session:
        row = await session.get(Operation, outcome.operation_id)
    assert row is not None
    return row


def _voice_message(*, audio_digest: str) -> OutboundMessage:
    """§17.5 规定的语音外发形态：``text=""`` + 一个 audio 附件，无 caption。"""
    return OutboundMessage(
        operation_id="",
        window_ref=WINDOW_REF,
        text="",
        attachments=[
            OutboundAttachment(
                kind=AttachmentKind.audio,
                ref=f"asset:{audio_digest}",
                mime_type="audio/mpeg",
                caption=None,
            )
        ],
        is_group=True,
    )


@pytest.fixture()
async def actor_ids(logged_in):  # noqa: ANN001,ANN201
    """一个 actor/subject 身份：``deliver`` 要求它们非空。"""
    return {"actor_id": "actor_digest", "subject_id": logged_in.subject_id}


# --------------------------------------------------------------------------- #
# 一、语音：不写空正文摘要
# --------------------------------------------------------------------------- #


async def test_voice_payload_has_no_empty_text_digest(logged_in, actor_ids) -> None:  # noqa: ANN001
    """语音 operation 的 payload **不含** `text_digest`——连键都不出现。

    **必须失败于**「无条件写 ``delivery_text_digest(message.text)``」：那时
    ``payload["text_digest"] == EMPTY_TEXT_DIGEST``，而那个值会匹配所有空正文的
    历史消息（见文件头）。

    为什么连键都不出现而不是写 ``None``：见 `delivery.py` 里那段说明——缺键让
    "修复前的存量行"（本来就没有这个键）与"新的语音行"**同一种表示**，读侧
    （``payload.get(...)``）不用为两种空值分叉。
    """
    row = await _deliver(
        actor_ids,
        message=_voice_message(audio_digest="sha256:" + "a" * 64),
        payload_extra={"speech": {"digest": "sha256:" + "a" * 64, "duration_ms": 1800}},
        plan_id="plan_voice_1",
        part_ordinal=1,
    )
    payload = dict(row.payload or {})

    assert "text_digest" not in payload, (
        f"语音 operation 落了正文摘要（会匹配所有空正文消息）：{payload.get('text_digest')!r}"
    )
    assert payload.get("text_digest") is None, payload

    # 正向配对：这条 operation 确实是那条语音的，不是"空 operation 碰巧没这个键"。
    assert payload["speech"]["digest"] == "sha256:" + "a" * 64, payload
    assert payload["chars"] == 0, payload
    assert payload["media_count"] == 1, f"语音没被记成媒体意图：{payload}"


async def test_whitespace_only_text_also_gets_no_digest(logged_in, actor_ids) -> None:  # noqa: ANN001
    """纯空白正文与空串**同一个摘要**，所以同样不写。

    **必须失败于**「只判断 ``message.text == ""``」：``"   "`` 不满足它，会落
    ``EMPTY_TEXT_DIGEST``。判据必须跟 ``delivery_text_digest`` 的归一化一致
    （折叠所有空白），否则换一个空值绕过整条防线。
    """
    row = await _deliver(
        actor_ids,
        message=OutboundMessage(
            operation_id="", window_ref=WINDOW_REF, text="  \n\t ", is_group=True
        ),
    )
    payload = dict(row.payload or {})
    assert "text_digest" not in payload, (
        f"纯空白正文落了摘要（与空串同值，会匹配所有空正文消息）：{payload.get('text_digest')!r}"
    )
    # 正向配对：这条 operation 确实是那条消息的。
    assert payload["chars"] == 5, payload


# --------------------------------------------------------------------------- #
# 二、纯文本：逐字节不变
# --------------------------------------------------------------------------- #


async def test_plain_text_payload_is_byte_identical(logged_in, actor_ids) -> None:  # noqa: ANN001
    """纯文本 payload **整套**（键集合、键顺序、每个值）与改动前逐字节相同。

    不只断言摘要那一个值：修语音时很容易顺手改坏相邻字段（例如把
    ``media_count`` 换个数法、把键序打乱）。而 key 顺序进了 JSON 列，序列化
    出来的字节就不同——虽然对读者无意义，但"逐字节不变"是这里唯一能被机器
    验证的说法，所以照它验。
    """
    row = await _deliver(
        actor_ids,
        message=OutboundMessage(
            operation_id="", window_ref=WINDOW_REF, text=PLAIN_TEXT, is_group=True
        ),
        plan_id="plan_plain_1",
        part_ordinal=1,
    )
    payload = dict(row.payload or {})

    assert payload == {
        "channel": "onebot",
        "instance": "onebot-digest-evidence",
        "window_ref": WINDOW_REF,
        "chars": len(PLAIN_TEXT),
        "is_group": True,
        "media_count": 0,
        "media_refs": [],
        "reply_plan": "plan_plain_1",
        "part_ordinal": 1,
        "text_digest": PLAIN_TEXT_DIGEST,
    }, payload
    # 键**顺序**也钉住：JSON 列的序列化按插入顺序。
    assert list(payload) == [
        "channel",
        "instance",
        "window_ref",
        "chars",
        "is_group",
        "media_count",
        "media_refs",
        "reply_plan",
        "part_ordinal",
        "text_digest",
    ], list(payload)


async def test_plain_text_digest_is_a_real_digest_not_the_empty_one(
    logged_in,  # noqa: ANN001
    actor_ids,  # noqa: ANN001
) -> None:
    """另一条纯文本 → 另一个摘要。这两条同时成立才排除"恒等于某个常量"。

    单看一条"摘要等于某个固定串"的断言，在实现"对所有正文都返回同一个常量"
    时也成立——**那个缺陷正是本文件要抓的形状**。所以必须有第二条正文，且两条
    的摘要互不相同、都不等于空摘要。
    """
    first = await _deliver(
        actor_ids,
        message=OutboundMessage(
            operation_id="", window_ref=WINDOW_REF, text=PLAIN_TEXT, is_group=True
        ),
        plan_id="plan_plain_a",
        part_ordinal=1,
    )
    second = await _deliver(
        actor_ids,
        message=OutboundMessage(
            operation_id="", window_ref=WINDOW_REF, text=SECOND_TEXT, is_group=True
        ),
        plan_id="plan_plain_b",
        part_ordinal=1,
    )
    first_payload = dict(first.payload or {})
    second_payload = dict(second.payload or {})

    assert first_payload["text_digest"] == PLAIN_TEXT_DIGEST, first_payload
    assert second_payload["text_digest"] == SECOND_TEXT_DIGEST, second_payload
    assert first_payload["text_digest"] != second_payload["text_digest"]
    assert EMPTY_TEXT_DIGEST not in (
        first_payload["text_digest"],
        second_payload["text_digest"],
    )


# --------------------------------------------------------------------------- #
# 三、两条不同的语音不靠同一个摘要混同
# --------------------------------------------------------------------------- #


async def test_two_different_voices_are_not_conflated_by_digest(
    logged_in,  # noqa: ANN001
    actor_ids,  # noqa: ANN001
) -> None:
    """两条**不同**的语音 operation：都没有空摘要，且各自带可区分的语音标识。

    缺陷下它们的 ``text_digest`` **相同**（都是空摘要）——摘要对它们零区分力，
    这正是"对账把别人那条空消息认成自己这条"的前提。这里同时断言两面：

    - 负向：两条都没有 ``text_digest``（没有"万能通行证"）；
    - 正向：两条的 ``speech.digest`` 不同（媒体标识还能区分它们）。

    **正向那一半不是装饰**：它证明这两条 operation 确实来自两段不同的音频，
    否则"都没有摘要"可能只是"两次跑的是同一条消息"。
    """
    first = await _deliver(
        actor_ids,
        message=_voice_message(audio_digest="sha256:" + "1" * 64),
        payload_extra={"speech": {"digest": "sha256:" + "1" * 64}},
        plan_id="plan_voice_a",
        part_ordinal=1,
    )
    second = await _deliver(
        actor_ids,
        message=_voice_message(audio_digest="sha256:" + "2" * 64),
        payload_extra={"speech": {"digest": "sha256:" + "2" * 64}},
        plan_id="plan_voice_b",
        part_ordinal=1,
    )
    first_payload = dict(first.payload or {})
    second_payload = dict(second.payload or {})

    assert "text_digest" not in first_payload, first_payload
    assert "text_digest" not in second_payload, second_payload
    # 两条 operation 确实是两条不同的投递。
    assert first.id != second.id
    assert first_payload["reply_plan"] != second_payload["reply_plan"]
    # 媒体标识仍可区分（这是"两条不同的语音"的证据，不是断言缺陷）。
    assert first_payload["speech"]["digest"] != second_payload["speech"]["digest"]
    assert first_payload["speech"]["digest"] == "sha256:" + "1" * 64
    assert second_payload["speech"]["digest"] == "sha256:" + "2" * 64


async def test_voice_payload_records_audio_asset_digest_not_the_url(
    logged_in,  # noqa: ANN001
    actor_ids,  # noqa: ANN001
) -> None:
    """语音 operation 记**资产 digest**，不记签名 URL／base64／file_info 凭据。

    §17.5「投递 operation／parts 保存 asset ID／hash……不保存 base64／签名 URL／
    file_info 凭据」。这条同时守两面：正向要 digest 在，负向要凭据不在。

    **必须失败于**「把 ``message.attachments[*].ref`` 整个抄进 payload」：那时
    签名 URL（带 ``sig=``／``expires=``）会出现在库里，等于把可重放的凭据写进
    了投递记录。
    """
    asset = "sha256:" + "c" * 64
    row = await _deliver(
        actor_ids,
        message=_voice_message(audio_digest=asset),
        payload_extra={"speech": {"digest": asset, "transport": "signed_url"}},
        plan_id="plan_voice_cred",
        part_ordinal=1,
    )
    payload = dict(row.payload or {})
    assert payload["speech"]["digest"] == asset, payload
    # 负向：payload 里不能出现带凭据的引用。上面那条 message 的 ref 是
    # `asset:<digest>`（无凭据），所以这里另外证明"没有把它之外的形态记进来"。
    blob = repr(payload)
    for forbidden in ("sig=", "expires=", "file_info", "base64", "data:"):
        assert forbidden not in blob, f"payload 里出现了凭据形态 {forbidden!r}：{blob}"


# --------------------------------------------------------------------------- #
# 四、缺摘要 → `_scan_history` 是"没有证据"，不是"匹配到任意一条"
# --------------------------------------------------------------------------- #
#
# 这一节不看 payload，只看**结果**：一条语音 operation 没有平台 id 时对账会走
# 历史比对，摘要缺失必须让那条路**放弃比对**（`_HistoryScan(ok=False)`），而不是
# 把历史里任何一条正文为空的消息认成自己。
#
# 用**真实入口**（`OneBotV11Connector.query_receipt`）+ httpx `MockTransport`
# 喂一份历史，不直接调 `_scan_history`：私有方法的返回值得靠人解释"ok=False 意味着
# 什么"，而回执的 state/detail 是产品真正给出的答案。


def _onebot_connector(handler):  # noqa: ANN001,ANN202
    from connectors.onebot.connector import OneBotV11Connector
    from connectors.onebot.settings import OneBotConfig

    return OneBotV11Connector(
        OneBotConfig(
            api_base="http://onebot.test:5700",
            access_token="",
            self_id="10001",
            instance_id="onebot-digest-evidence",
        ),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _ok_history(messages: list[dict]) -> object:  # noqa: ANN001
    return httpx.Response(
        200, json={"status": "ok", "retcode": 0, "data": {"messages": messages}}
    )


#: 一份"看起来很像"的历史：里面有几条**正文为空**的自己的消息。
#:
#: 这几条正是缺陷下会被误认的东西——它们与语音 operation 的空摘要相等。
#: `sender.user_id == self_id` 才进比对，所以它们都被算作"我们自己发的"。
_EMPTY_BODY_HISTORY = [
    {"message_id": 7101, "sender": {"user_id": 10001}, "message": []},
    {
        "message_id": 7102,
        "sender": {"user_id": 10001},
        "message": [{"type": "text", "data": {"text": "   "}}],
    },
    {
        "message_id": 7103,
        "sender": {"user_id": 10001},
        # 只有 @ 与回复段：正文解析出来是空。
        "message": [
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "reply", "data": {"id": "7000"}},
        ],
    },
]


@pytest.mark.asyncio
async def test_missing_digest_makes_history_scan_report_no_evidence() -> None:
    """摘要缺失（`None`）→ **一次历史 API 都不查**，回执是 `uncertain`。

    **必须失败于**「把 `text_digest=None` 当成 `""` 之外的东西照常比对」：如果
    实现把缺失摘要归一成空摘要（``query.text_digest or delivery_text_digest("")``），
    它会去查历史、把那几条空正文消息认成自己，状态就变成 ``delivered`` 并带上
    别人的 message_id。

    断言"没查历史"是这条的判据核心：可以查却查了、并且查出来"没命中"，在
    实现上是**另一个状态**（``ok=True`` + ``saw_self``），会把后续推向 ``failed``。
    """
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url).rsplit("/", 1)[-1])
        return _ok_history(_EMPTY_BODY_HISTORY)

    receipt = await _onebot_connector(handler).query_receipt(
        ReceiptQuery(
            operation_id="op_voice_no_digest",
            window_ref="onebot-digest-evidence:group:100200300",
            text_digest=None,
            sent_at=datetime.now(UTC) - timedelta(minutes=30),
            is_group=True,
        )
    )
    assert seen == [], f"摘要是 None，却仍然去查了历史：{seen}"
    assert receipt.state is DeliveryState.uncertain, receipt
    assert "第二个否证" in (receipt.detail or ""), receipt.detail
    assert receipt.platform_message_id is None, receipt


@pytest.mark.asyncio
async def test_empty_digest_string_also_reports_no_evidence() -> None:
    """空串摘要同样不查历史（`or` 归一的那一半）。

    缺陷修复前没有人会传空串，所以这条**不是**在守缺陷本身；它守的是"两条空值
    都不能变成万能匹配"。如果实现改成 ``if query.text_digest is None: ...`` 而
    放过了 ``""``，这里会红。
    """
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url).rsplit("/", 1)[-1])
        return _ok_history(_EMPTY_BODY_HISTORY)

    receipt = await _onebot_connector(handler).query_receipt(
        ReceiptQuery(
            operation_id="op_voice_empty_digest",
            window_ref="onebot-digest-evidence:group:100200300",
            text_digest="",
            sent_at=datetime.now(UTC) - timedelta(minutes=30),
            is_group=True,
        )
    )
    assert seen == [], f"摘要是空串，却仍然去查了历史：{seen}"
    assert receipt.state is DeliveryState.uncertain, receipt


@pytest.mark.asyncio
async def test_stored_empty_digest_constants_are_ignored_like_missing() -> None:
    """**存量行**：摘要键存在但值是空摘要**常量** → 同样"没有证据"。

    只修写侧（`8e5b84b`）挡不住这个形状：修复前写下的语音 operation 里仍然躺着
    ``EMPTY_TEXT_DIGEST``。它非空，所以绕过"摘要是空值"那个开关，下去逐条比对
    历史——任何一条正文为空的消息都会 ``== digest``，`hits` 于是装进**别人的**
    message_id。所以读侧（`_scan_history`）必须把它与"没有摘要"同等处理。

    这条替换掉了本文件原来的
    ``test_the_empty_digest_would_have_matched_strangers_message``。那条断言
    "空摘要常量**确实会**命中外人的消息"，并预言"如果哪天连接器改成空摘要也不
    匹配，这条会红"——**它红了，而且它描述的那个行为正是设计文档禁止的**，
    所以按它自己的指示改写成现在这条，而不是放宽它。

    同时验证**不查历史**：可以查却查了、只是恰好没命中，在实现上是另一个状态
    （``ok=True`` + ``saw_self``），会把后续推向 ``failed``。

    **必须失败于**"去掉空摘要常量判断"：那时下面这份历史里三条空正文消息都会被
    认成自己 → ``delivered``（唯一命中）或 ``ambiguous``，且会真的发出历史请求。
    """
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url).rsplit("/", 1)[-1])
        return _ok_history(_EMPTY_BODY_HISTORY)

    receipt = await _onebot_connector(handler).query_receipt(
        ReceiptQuery(
            operation_id="op_voice_empty_constant",
            window_ref="onebot-digest-evidence:group:100200300",
            text_digest=EMPTY_TEXT_DIGEST,
            sent_at=datetime.now(UTC) - timedelta(minutes=30),
            is_group=True,
        )
    )
    assert seen == [], f"摘要是空正文常量，却仍然去查了历史：{seen}"
    assert receipt.state is DeliveryState.uncertain, receipt
    assert "第二个否证" in (receipt.detail or ""), receipt.detail
    assert receipt.platform_message_id is None, (
        f"把历史里别人的空消息认成了自己：{receipt.platform_message_id}"
    )


@pytest.mark.asyncio
async def test_real_digest_still_matches_through_the_same_scan() -> None:
    """**配对正向断言**：正常非空摘要 + 正常匹配 → 仍然**唯一命中**那条历史消息。

    没有这一条，"空摘要常量一律返回 ``ok=False``"也能让上面那条通过——那时
    历史比对这条证据链等于被整条关掉，语音/纯文本一旦没有平台 id 就永远
    ``uncertain``。所以这条不是陪衬，是那一刀没跑偏的唯一保证。

    ## 构造方式：同一份历史里**既有**空正文消息**也有**目标消息

    单单"历史里只有一条同文消息"是不够的：那种历史在"错误地忽略所有摘要"的
    实现下会报"没命中"，在这条实现下报"命中"——能区分；但它区分不了"命中"来自
    内容比对还是来自"附近只有一条自己的消息"。真正需要的是：把空正文消息
    **放在一起**，让错误的实现先去把它们数进 ``hits``。

    - 若实现把空正文也算命中 → ``hits`` 有 4 条（3 条空正文 + 1 条正文）→
      ``ambiguous`` 或认错 id，这条红；
    - 若实现完全不按摘要匹配 → 0 条 → ``ok=True, saw_self=True``，回执是
      ``uncertain``/``failed``，不会带 message_id，这条也红。

    只有"只认那个正文的摘要、跳过空正文"才得到唯一的那个 id。

    **必须失败于**"凡是摘要就 return ``ok=False``" 与 "把空正文也算命中"。
    """
    target_text = "我到家了"
    history = [
        # 三条别人的空正文消息（sender 仍是我们自己，否则它们连比对都进不去）。
        *_EMPTY_BODY_HISTORY,
        # 目标：正文与 query 的摘要一致。
        {
            "message_id": 7301,
            "sender": {"user_id": 10001},
            "message": [{"type": "text", "data": {"text": target_text}}],
        },
    ]
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url).rsplit("/", 1)[-1])
        return _ok_history(history)

    receipt = await _onebot_connector(handler).query_receipt(
        ReceiptQuery(
            operation_id="op_plain_positive",
            window_ref="onebot-digest-evidence:group:100200300",
            text_digest=delivery_text_digest(target_text),
            sent_at=datetime.now(UTC) - timedelta(minutes=30),
            is_group=True,
        )
    )
    # 正向：真的走了历史查询这条路（不是靠什么捷径拿到 id）。
    assert seen, "非空摘要却没有查历史，下面的命中不可能是内容比对得来的"
    assert receipt.state is DeliveryState.delivered, receipt
    assert receipt.platform_message_id == "7301", (
        f"内容比对没有唯一命中目标消息：{receipt.platform_message_id!r}"
    )
    assert "历史内容比对" in (receipt.detail or ""), receipt.detail


@pytest.mark.asyncio
async def test_voice_delivery_then_reconcile_does_not_invent_a_match(
    logged_in,  # noqa: ANN001
    actor_ids,  # noqa: ANN001
) -> None:
    """**端到端**：真实 ``DeliveryService`` 投一条语音 → 真实 ``reconcile``。

    这是"改错方向不是报错，是悄悄把别人的消息认成自己的"那条纪律的直接对应：
    投递时没有平台 id，平台上有一条正文为空的**别人的**消息，对账不能把它认下来。

    历史里只有一条空正文消息（``saw_self=True``），所以如果摘要还在，`_scan_history`
    会**唯一命中**它并给出 ``delivered`` + 那个 message_id——这正是要防的结论。

    **必须失败于**「回到无条件写摘要」：那时 operation 带着空摘要，对账把历史里
    那条空消息认成自己，``state`` 变 ``succeeded``、回执带上别人的 id。
    """
    from ling.capabilities.delivery import DeliveryService

    class _NoIdConnector(_RecordingConnector):
        """发送成功但**没给平台 id**——对账只能靠内容证据的那条路。"""

        def __init__(self) -> None:
            super().__init__()
            self.queries = []

        async def query_receipt(self, query):  # noqa: ANN001,ANN201
            self.queries.append(query)
            # 委托给真实 OneBot 连接器的对账逻辑：历史里只有一条自己的空正文消息。
            async def handler(request: httpx.Request) -> httpx.Response:
                return _ok_history(
                    [{"message_id": 7201, "sender": {"user_id": 10001}, "message": []}]
                )

            return await _onebot_connector(handler).query_receipt(
                ReceiptQuery(
                    operation_id=query.operation_id,
                    window_ref=query.window_ref,
                    text_digest=query.text_digest,
                    sent_at=query.sent_at,
                    is_group=query.is_group,
                )
            )

    connector = _NoIdConnector()
    async with session_scope() as session:
        outcome = await DeliveryService(session).deliver(
            connector=connector,
            message=_voice_message(audio_digest="sha256:" + "d" * 64),
            actor_id=str(actor_ids["actor_id"]),
            subject_id=actor_ids["subject_id"],
            payload_extra={"speech": {"digest": "sha256:" + "d" * 64}},
            plan_id="plan_voice_e2e",
            part_ordinal=1,
        )
        await session.commit()
    assert len(connector.sent) == 1, "语音没被发出，下面的对账证明不了任何事"

    async with session_scope() as session:
        reconciled = await DeliveryService(session).reconcile(
            connector, outcome.operation_id
        )
        await session.commit()

    # 正向配对：对账**确实**带着一个 query 跑过（否则"没认错"在"根本没对账"时也成立）。
    assert len(connector.queries) == 1, connector.queries
    assert connector.queries[0].text_digest is None, connector.queries[0]
    # 结论：没有证据 → 保持 uncertain，**不**把历史里那条空消息认成自己。
    assert reconciled.state is OperationState.uncertain, reconciled
    assert reconciled.receipt is not None
    assert reconciled.receipt.platform_message_id is None, (
        f"把历史里别人的空消息认成了自己这条语音：{reconciled.receipt.platform_message_id}"
    )
