"""E01 冻结的语音与媒体契约：把不变量钉在测试里。

这个文件守的不是"功能跑通了没有"（三家 adapter 另有测试），而是
**契约本身不许被悄悄放宽**。E02–E17 都依赖这里冻结的端口，一旦某条
不变量被改松，问题会以"某个渠道静默发错东西"的形式很晚才暴露。

对应 §17.8 E01 的验收要求：

- 三家仅 TTS；
- `speech.tts` 接受 buffered；
- 三档全量往返、非法档值拒绝；
- 显式空能力不被 legacy true 放大；
- voice DTO 不含正文；
- schema 导出与类型一致（由 tests/contract 单独守）。
"""

from __future__ import annotations

import dataclasses
from dataclasses import fields

import pytest

from ling.contracts.v1 import (
    AttachmentKind,
    CapabilityDescriptor,
    Modality,
    OutboundAttachment,
)
from ling.ports.channel import OutboundMessage, iter_outbound_attachments
from ling.ports.config import (
    VOICE_WILLINGNESS_PROMPTS,
    DialoguePolicy,
    VoiceWillingness,
)
from ling.ports.model import (
    LEGACY_PROVIDER_KIND_ALIASES,
    PROFILE_CAPABILITIES,
    PROVIDER_CAPABILITIES,
    ModelCapability,
    ProviderKind,
    kind_supports_profile,
    normalize_provider_kind,
    provider_capabilities,
)
from ling.ports.speech import (
    SPEAK_TAG,
    SPEECH_PROFILE_REQUIREMENTS,
    AudioInput,
    AudioSpec,
    ExpressionIntent,
    FishAudioVoice,
    MimoReferenceVoice,
    MiniMaxVoice,
    SpeechErrorKind,
    SpeechUsage,
    TtsCapabilities,
    TtsChunk,
    TtsEnd,
    TtsRequest,
    TtsStart,
    validate_speech_profile,
)

TTS_KINDS = {"minimax_tts", "fishaudio_tts", "mimo_tts"}

# 内部 adapter 的错误分类（models/tts_common.py）。端口必须**无损**覆盖它们，
# 否则 adapter 边界那一次映射就会有损。
ADAPTER_ERROR_KINDS = {
    "authentication",
    "rate_limited",
    "invalid_request",
    "unavailable",
    "timeout",
    "protocol",
    "not_configured",
}


# --------------------------------------------------------------------------- #
# 三家 TTS 供应商与能力标记
# --------------------------------------------------------------------------- #


def test_tts_kinds_are_exactly_the_three_vendors() -> None:
    """提供 tts 能力的 kind 就是这三家，不多不少（§17.2 定名）。"""
    tts = {k.value for k, caps in PROVIDER_CAPABILITIES.items() if ModelCapability.tts in caps}
    assert tts == TTS_KINDS


@pytest.mark.parametrize("kind", sorted(TTS_KINDS))
def test_tts_kinds_declare_only_tts(kind: str) -> None:
    """三家**只**声明 tts。"""
    assert PROVIDER_CAPABILITIES[ProviderKind(kind)] == frozenset({ModelCapability.tts})


def test_no_kind_holds_both_chat_and_tts() -> None:
    """**契约核心不变量**：没有 kind 同时提供 chat 与 tts。

    一个 kind 同时持两种能力，会让 `kind_supports_profile` 对"TTS 绑聊天
    槽位"和"聊天绑 TTS 槽位"都放行，fail-closed 直接作废。
    """
    both = {
        kind.value
        for kind, caps in PROVIDER_CAPABILITIES.items()
        if ModelCapability.chat in caps and ModelCapability.tts in caps
    }
    assert not both, f"这些 kind 同时声明了 chat 与 tts: {sorted(both)}"


@pytest.mark.parametrize("kind", sorted(TTS_KINDS))
def test_tts_kinds_cannot_serve_chat_roles(kind: str) -> None:
    for profile in ("dialogue.fast", "dialogue.deep", "memory.extract"):
        assert not kind_supports_profile(kind, profile), f"{kind} 不该能服务 {profile}"


def test_legacy_fish_kind_value_still_resolves() -> None:
    """旧的 `fish_audio` 值必须还能读——老配置里存着它。

    但**不能同时发布两个同义 kind**：别名只用于读取，写入一律规范值。
    """
    assert normalize_provider_kind("fish_audio") == "fishaudio_tts"
    assert provider_capabilities("fish_audio") == frozenset({ModelCapability.tts})
    assert kind_supports_profile("fish_audio", "speech.tts")
    # 别名不能是真实成员，否则设置页会出现两个同义选项
    assert "fish_audio" not in {k.value for k in ProviderKind}
    assert set(LEGACY_PROVIDER_KIND_ALIASES) & {k.value for k in ProviderKind} == set()


def test_unknown_provider_kind_offers_nothing() -> None:
    """未知 kind 失败关闭。"""
    assert provider_capabilities("no_such_tts") == frozenset()
    assert provider_capabilities("") == frozenset()


def test_every_kind_is_declared() -> None:
    """漏登记会让 kind 静默不可选。`ports/model.py` 里也有导入期断言。"""
    assert set(PROVIDER_CAPABILITIES) == set(ProviderKind)


def test_speech_tts_accepts_buffered_provider() -> None:
    """`speech.tts` **只要求能产出音频**，不要求 native streaming。

    §17.2 原话：「`speech.tts` 只要求能产音频，**不要求 native streaming**」。
    要求 streaming 会让所有只支持一次性返回的厂商永远绑不上。
    """
    tts_requirements = SPEECH_PROFILE_REQUIREMENTS["speech.tts"]
    assert tts_requirements["produces_audio"] is True
    assert tts_requirements.get("streaming") is not True
    assert tts_requirements.get("cancellable") is not True

    from ling.ports.speech import SpeechProfile

    buffered = SpeechProfile(profile="speech.tts", provider_id="p", model_id="m", streaming=True)
    assert validate_speech_profile(buffered, {"produces_audio": True}) == []


def test_file_asr_is_distinct_from_realtime_voice_requirements() -> None:
    """文件识别具备独立适配器，实时会话仍须满足插话和重连要求。"""
    realtime = SPEECH_PROFILE_REQUIREMENTS["realtime.voice"]
    assert realtime["streaming"] is True
    assert realtime["barge_in"] is True
    assert realtime["reconnect"] is True
    asr_kinds = {
        k.value for k, caps in PROVIDER_CAPABILITIES.items() if ModelCapability.asr in caps
    }
    assert asr_kinds == {"siliconflow_asr", "dashscope_asr"}


# --------------------------------------------------------------------------- #
# 失败协议：对外只有一种，且能无损承接 adapter 分类
# --------------------------------------------------------------------------- #


def test_port_error_kinds_cover_adapter_kinds_losslessly() -> None:
    """端口分类必须**无损覆盖** adapter 内部那七类。

    否则 adapter 边界那一次映射就是有损的：某个 401 会被压成"未知错误"，
    用户看到"网络有问题"而实际是密钥错了，排查方向完全错。
    """
    port = {k.value for k in SpeechErrorKind}
    assert port >= ADAPTER_ERROR_KINDS, (
        f"端口缺少这些 adapter 分类（映射会有损）: {sorted(ADAPTER_ERROR_KINDS - port)}"
    )


def test_port_error_variants_are_distinct() -> None:
    """`protocol` 不能与 `unavailable` 合并：前者是"对方没按约定说话"。"""
    assert SpeechErrorKind.protocol != SpeechErrorKind.unavailable
    assert len({k.value for k in SpeechErrorKind}) == len(list(SpeechErrorKind))


# --------------------------------------------------------------------------- #
# 音色：三家各自的配置分支，没有统一 voice_key
# --------------------------------------------------------------------------- #


def test_voice_types_are_distinct_per_vendor() -> None:
    """三家音色是**三个不同的值对象**，没有跨厂商映射（§17.2 撤销统一 voice_key）。"""
    assert {MiniMaxVoice, FishAudioVoice, MimoReferenceVoice} == {
        MiniMaxVoice,
        FishAudioVoice,
        MimoReferenceVoice,
    }
    # 各自字段不同，不能互换
    assert "voice_id" in {f.name for f in fields(MiniMaxVoice)}
    assert "reference_id" in {f.name for f in fields(FishAudioVoice)}
    assert "audio" in {f.name for f in fields(MimoReferenceVoice)}


def test_mimo_voice_holds_audio_input_not_bytes_in_config() -> None:
    """MiMo 请求里带的是**已授权的 AudioInput**。

    保存配置时只存 `reference_asset_id`——把字节塞进配置库会让配置体积
    失控，也会让"授权"退化成"字节还在就算有权限"。
    """
    assert dataclasses.is_dataclass(AudioInput)
    mimo_fields = {f.name for f in fields(MimoReferenceVoice)}
    assert "audio" in mimo_fields
    assert "reference_asset_id" not in mimo_fields, (
        "请求值对象不该带 asset id：那是配置层的东西，agreement 由编排层解析"
    )


def test_tts_request_requires_voice_and_format() -> None:
    request = TtsRequest(
        model_id="speech-2.8",
        text="今天挺累的",
        voice=MiniMaxVoice(voice_id="v1"),
        response_format="mp3",
    )
    assert request.stream is False, "默认不要求下行增量"
    assert request.output_spec is None, "允许探测前采样率未知，不能谎报"


def test_tts_capabilities_streaming_is_three_valued() -> None:
    """`buffered` 必须能与 `native` 区分。

    把 buffered 说成"支持流式"是本项目最容易自我欺骗的地方：用户以为
    边说边出，实际是说完最后一个字才有音频。
    """
    assert {f.name for f in fields(TtsCapabilities)} == {
        "formats",
        "streaming",
        "cancellable_transport",
    }
    buffered = TtsCapabilities(
        formats=frozenset({"mp3"}), streaming="buffered", cancellable_transport=False
    )
    assert buffered.streaming != "native"


# --------------------------------------------------------------------------- #
# 事件流
# --------------------------------------------------------------------------- #


def test_tts_events_are_a_closed_union() -> None:
    """事件只有三种：Start → Chunk* → End。"""
    assert {f.name for f in fields(TtsStart)} == {"spec"}
    assert {f.name for f in fields(TtsChunk)} == {"sequence", "audio"}
    assert {f.name for f in fields(TtsEnd)} == {"spec", "provider_request_id", "usage"}


def test_tts_end_carries_final_spec_for_revalidation() -> None:
    """End 必须带最终规格：adapter 说 24k 不代表它真的重采样了，要再验一次。"""
    spec = AudioSpec(
        mime_type="audio/wav",
        container="wav",
        codec="pcm_s16le",
        sample_rate_hz=24000,
        channels=1,
    )
    end = TtsEnd(spec=spec, provider_request_id=None, usage=SpeechUsage())
    assert end.spec.sample_rate_hz == 24000
    assert end.provider_request_id is None, "没有远端 ID 就不伪造"


def test_speech_usage_does_not_pretend_characters_are_tokens() -> None:
    usage = SpeechUsage(characters=12, input_units=12.0, unit_kind="provider_characters")
    assert usage.input_tokens is None, "字符数不能顺手塞进 token 字段"
    assert usage.usage_source == "unknown", "默认不许声称是上游给的用量"


# --------------------------------------------------------------------------- #
# 语音意愿：三个语义档（§17.4）
# --------------------------------------------------------------------------- #


def test_voice_willingness_has_exactly_three_tiers() -> None:
    assert set(VOICE_WILLINGNESS_PROMPTS) == {"reserved", "natural", "expressive"}


def test_voice_willingness_default_is_natural() -> None:
    """不配置时读 `natural`——最接近"她现在这样"。"""
    assert DialoguePolicy().voice_willingness == "natural"


def test_voice_willingness_is_not_a_probability() -> None:
    """档位是**倾向**，不是概率：不能在契约里留下数字权重。"""
    names = {f.name for f in fields(DialoguePolicy)}
    assert "voice_willingness" in names
    assert "speech_reply_weight" not in names, "§17.4 撤销了浮点权重"
    assert "speech_reply_mode" not in names, "§17.4 撤销了旧四档"
    # 提示词文本里也不该出现概率口径
    for text in VOICE_WILLINGNESS_PROMPTS.values():
        assert "%" not in text


def test_each_tier_has_distinct_prompt_text() -> None:
    """三档文案必须真的不同，否则"换了档位没变化"。"""
    texts = list(VOICE_WILLINGNESS_PROMPTS.values())
    assert len(set(texts)) == 3


def test_dialogue_policy_voice_prompt_is_the_tier_text() -> None:
    """`voice_prompt()` 是档位的唯一消费入口，返回固定文本。"""
    for tier, text in VOICE_WILLINGNESS_PROMPTS.items():
        policy = DialoguePolicy(voice_willingness=tier)
        assert policy.voice_prompt() == text


def test_speak_tag_is_the_documented_marker() -> None:
    """她选择语音的标记是 `[speak]`，无参数。"""
    assert SPEAK_TAG == "[speak]"
    assert " " not in SPEAK_TAG


def test_expression_intent_carries_no_body_text() -> None:
    """表达意图**不携带正文**：正文在轮次里，复制一份必然不同步。

    用户要求「默认只发语音，后台显示语音标签 + 文字」——那个"文字"由
    后台投影从 Message.text 取，不是从意图对象里取。
    """
    intent = ExpressionIntent(output_modality="voice", source="model", config_revision=7)
    assert {f.name for f in fields(intent)} == {
        "output_modality",
        "source",
        "config_revision",
        "voice_config_hash",
    }
    assert not hasattr(intent, "text")


def test_expression_intent_hashes_voice_config_not_plain_id() -> None:
    """冻结的是音色配置**哈希**，不含明文 key/voice_id。"""
    intent = ExpressionIntent(
        output_modality="voice",
        source="model",
        config_revision=3,
        voice_config_hash="abc123",
    )
    assert intent.voice_config_hash == "abc123"


# --------------------------------------------------------------------------- #
# 媒体能力：显式空集合不被 legacy true 放大（§17.6 点名要求）
# --------------------------------------------------------------------------- #


def _caps(**kwargs: object) -> CapabilityDescriptor:
    return CapabilityDescriptor(capability_id="cap_test", version="1", **kwargs)


def test_legacy_supports_media_means_images_only() -> None:
    """旧声明 `supports_media=True` 只当图片。

    §17.6：「遗留未提供集合的兼容行为仅保留实际已验证图片路径」。
    """
    caps = _caps(supports_media=True)
    assert caps.media_supported(Modality.image) is True
    assert caps.media_supported(Modality.audio) is False
    assert caps.media_supported(Modality.video) is False


def test_explicit_empty_set_is_not_amplified_by_legacy_true() -> None:
    """**显式空集合**不能被 `supports_media=True` 重新放大成 image。

    这是 §17.6 明确点名的语义缺口：旧实现无法区分"没提供集合"与
    "显式给了空集合"，于是 qq/wechat 那些虚假的 `supports_media=True`
    永远会被当成"能发图片"。显式空集合必须表达"什么都不支持"。
    """
    caps = _caps(supports_media=True, supported_media=[])
    assert caps.media_supported(Modality.image) is False, (
        "显式空集合被 legacy supports_media=True 放大了 —— 虚假能力又回来了"
    )
    assert caps.media_supported(Modality.audio) is False
    assert caps.media_supported(Modality.video) is False


def test_explicit_set_takes_precedence_over_legacy_true() -> None:
    caps = _caps(supports_media=True, supported_media=[Modality.audio])
    assert caps.media_supported(Modality.audio) is True
    assert caps.media_supported(Modality.image) is False, (
        "显式集合里没有 image，就不能因为 supports_media=True 而放行"
    )


def test_nothing_declared_means_no_media() -> None:
    assert _caps().media_supported(Modality.image) is False


def test_audio_capability_means_native_voice_message() -> None:
    """`Modality.audio` 表示**能发原生语音消息**，不是"能发下载文件"（§17.6）。"""
    caps = _caps(supported_media=[Modality.audio])
    assert caps.media_supported(Modality.audio) is True
    assert caps.media_supported(Modality.image) is False


# --------------------------------------------------------------------------- #
# 类型化出站附件
# --------------------------------------------------------------------------- #


def _message(*, text: str = "hi", **kwargs: object) -> OutboundMessage:
    return OutboundMessage(operation_id="op_1", window_ref="w:private:u1", text=text, **kwargs)


def test_legacy_media_refs_are_typed_as_image_without_guessing() -> None:
    """legacy `media_refs` 统一当 image，**不从内容猜**。"""
    message = _message(media_refs=["asset:a", "asset:b"])
    assert [a.kind for a in iter_outbound_attachments(message)] == [
        AttachmentKind.image,
        AttachmentKind.image,
    ]


def test_typed_attachments_keep_their_kind() -> None:
    message = _message(
        attachments=[
            OutboundAttachment(kind=AttachmentKind.audio, ref="asset:voice"),
            OutboundAttachment(kind=AttachmentKind.video, ref="asset:clip"),
        ]
    )
    assert [a.kind for a in iter_outbound_attachments(message)] == [
        AttachmentKind.audio,
        AttachmentKind.video,
    ]


def test_legacy_and_typed_coexist() -> None:
    message = _message(
        media_refs=["asset:img"],
        attachments=[OutboundAttachment(kind=AttachmentKind.audio, ref="asset:voice")],
    )
    assert [(a.kind.value, a.ref) for a in iter_outbound_attachments(message)] == [
        ("image", "asset:img"),
        ("audio", "asset:voice"),
    ]


def test_attachment_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        OutboundAttachment(kind="voice", ref="r")  # type: ignore[arg-type]


def test_voice_wire_message_has_no_text_and_no_caption() -> None:
    """§17.5：对方的平台消息是 `text=""` + 一个 audio 附件，**无 caption**。

    后台能看正文是**另一个投影**，不能靠往 wire 消息里塞文字实现。
    """
    message = _message(
        text="",
        attachments=[OutboundAttachment(kind=AttachmentKind.audio, ref="asset:voice")],
    )
    attachments = iter_outbound_attachments(message)
    assert message.text == ""
    assert len(attachments) == 1
    assert attachments[0].kind is AttachmentKind.audio
    assert attachments[0].caption is None


# --------------------------------------------------------------------------- #
# DialoguePolicy serde 全字段守卫（防"加了字段忘了同步"）
# --------------------------------------------------------------------------- #


def test_dialogue_serde_covers_every_field() -> None:
    """`_dialogue_to_dict` 的键集合必须等于 `DialoguePolicy` 的全部字段。

    没有这条守卫时，加字段忘改 serde 的症状是：契约加了、UI 显示了、
    用户选了却存不住——**静默**丢，最难查的一种。
    """
    from ling.settings.service import _dialogue_to_dict

    emitted = set(_dialogue_to_dict(DialoguePolicy()).keys())
    declared = {f.name for f in fields(DialoguePolicy)}
    assert emitted == declared, (
        f"serde 漏了这些字段（会静默存不住）: {sorted(declared - emitted)}；"
        f"多了这些不存在的字段: {sorted(emitted - declared)}"
    )


def test_dialogue_policy_covers_every_enum_key() -> None:
    """`_DIALOGUE_ENUM_KEYS` 必须都是真实字段，否则 `_merge_fields` 白列。"""
    from ling.settings.service import _DIALOGUE_ENUM_KEYS

    declared = {f.name for f in fields(DialoguePolicy)}
    assert declared >= _DIALOGUE_ENUM_KEYS, (
        f"这些枚举键不是 DialoguePolicy 的字段: {sorted(_DIALOGUE_ENUM_KEYS - declared)}"
    )


def test_merge_fields_keeps_enum_values() -> None:
    """**第三处遗漏**：`_merge_fields` 原来只遍历数值 bounds。

    枚举不在 `_DIALOGUE_BOUNDS` 里，于是补丁里的枚举会被静默丢掉——
    症状是"改了下拉、保存成功、刷新还是旧值"，而同一页的数值字段却生效了。
    """
    from ling.settings.service import (
        _DIALOGUE_BOUNDS,
        _DIALOGUE_ENUM_KEYS,
        _merge_fields,
    )

    merged = _merge_fields(
        {}, {"voice_willingness": "reserved"}, _DIALOGUE_BOUNDS, _DIALOGUE_ENUM_KEYS
    )
    assert merged.get("voice_willingness") == "reserved"


def test_merge_fields_does_not_invent_missing_keys() -> None:
    """没传的字段保持原值，不重置成默认。"""
    from ling.settings.service import (
        _DIALOGUE_BOUNDS,
        _DIALOGUE_ENUM_KEYS,
        _merge_fields,
    )

    current = {"voice_willingness": "expressive", "group_turn_budget": 5}
    merged = _merge_fields(current, {}, _DIALOGUE_BOUNDS, _DIALOGUE_ENUM_KEYS)
    assert merged["voice_willingness"] == "expressive"
    assert merged["group_turn_budget"] == 5


def test_dialogue_voice_round_trip() -> None:
    """设成 expressive → 序列化 → 反序列化 → 还是 expressive。"""
    from ling.settings.service import _dialogue_policy, _dialogue_to_dict

    saved = _dialogue_to_dict(_dialogue_policy({"voice_willingness": "expressive"}))
    assert saved["voice_willingness"] == "expressive"
    assert _dialogue_policy(saved).voice_willingness == "expressive"


@pytest.mark.parametrize("tier", ["reserved", "natural", "expressive"])
def test_all_three_tiers_survive_round_trip(tier: str) -> None:
    """三档**全量**往返（§17.8 验收要求）。"""
    from ling.settings.service import _dialogue_policy, _dialogue_to_dict

    saved = _dialogue_to_dict(_dialogue_policy({"voice_willingness": tier}))
    assert _dialogue_policy(saved).voice_willingness == tier


@pytest.mark.parametrize("bad", ["banana", "", "weighted", "off", "always", 123, None, [], {}])
def test_invalid_tier_falls_back_to_natural(bad: object) -> None:
    """非法档值退回 `natural`（§17.4），且**不抛异常**。

    `weighted`/`off`/`always` 是 §17.4 判死的旧草稿值——它们必须落到
    `natural`，不能继续作为行为源。
    """
    from ling.settings.service import _dialogue_policy

    assert _dialogue_policy({"voice_willingness": bad}).voice_willingness == "natural"


def test_legacy_numeric_fields_are_not_behavior_source() -> None:
    """旧 `speech_reply_mode`/`speech_reply_weight` 不再被读成任何档位。"""
    from ling.settings.service import _dialogue_policy

    policy = _dialogue_policy({"speech_reply_mode": "always", "speech_reply_weight": 1.0})
    assert policy.voice_willingness == "natural", "旧字段不能影响新档位"


# --------------------------------------------------------------------------- #
# draft → validate → publish → serialize → reload 全链路
# --------------------------------------------------------------------------- #


def test_voice_willingness_survives_full_draft_publish_reload_chain() -> None:
    """**全链路**：草稿补丁 → 合并 → 校验 → 序列化 → 读回。

    只测 `_dialogue_policy` / `_dialogue_to_dict` 两个函数抓不到
    `_merge_fields` 那一层——枚举正是在那里被丢掉的。
    """
    from ling.settings.service import (
        _DIALOGUE_BOUNDS,
        _DIALOGUE_ENUM_KEYS,
        _dialogue_policy,
        _dialogue_to_dict,
        _merge_fields,
    )

    # 1) 现有草稿（空）
    draft: dict = {}

    # 2) 用户提交补丁：档位是枚举，不是数字
    patch = {"voice_willingness": "reserved", "group_turn_budget": 8}
    merged = _merge_fields(draft, patch, _DIALOGUE_BOUNDS, _DIALOGUE_ENUM_KEYS)

    # 3) 校验/解析（非法值在这里退回 natural）
    policy = _dialogue_policy(merged)
    assert policy.voice_willingness == "reserved"
    assert policy.group_turn_budget == 8

    # 4) 发布时序列化
    stored = _dialogue_to_dict(policy)

    # 5) 重新加载
    reloaded = _dialogue_policy(stored)
    assert reloaded.voice_willingness == "reserved"
    assert reloaded.group_turn_budget == 8


def test_dialogue_policy_has_no_numeric_weight_field() -> None:
    """§17.4 明确「不需要增加数字滑块」——契约里不许留权重字段。"""
    names = {f.name for f in fields(DialoguePolicy)}
    weightish = {n for n in names if "weight" in n}
    assert not weightish, f"DialoguePolicy 里还有权重字段: {sorted(weightish)}"


def test_voice_willingness_type_matches_prompt_table() -> None:
    """`Literal` 的取值必须与提示词表键完全一致（两处都得同步改）。"""
    import typing

    args = typing.get_args(VoiceWillingness)
    assert set(args) == set(VOICE_WILLINGNESS_PROMPTS)


# --------------------------------------------------------------------------- #
# wire 引用不进入模型 DTO
# --------------------------------------------------------------------------- #


def test_outbound_attachment_is_not_in_model_facing_dtos() -> None:
    """出站附件是**投递**概念，不该出现在模型上下文里。"""
    import ling.contracts.v1 as v1

    model_facing = [
        name
        for name, obj in vars(v1).items()
        if dataclasses.is_dataclass(obj)
        and "attachment" in str(getattr(obj, "__annotations__", {})).lower()
    ]
    assert model_facing == [], f"这些结构引用了出站附件: {model_facing}"


def test_profile_capabilities_reference_real_roles() -> None:
    """角色名必须真实存在（拼错的名字永远匹配不上，约束静默失效）。"""
    from ling.contracts.v1 import ModelRole

    known = {role.value for role in ModelRole}
    assert set(PROFILE_CAPABILITIES) <= known, (
        f"PROFILE_CAPABILITIES 里有不存在的角色: {sorted(set(PROFILE_CAPABILITIES) - known)}"
    )
