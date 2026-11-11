"""音色配置：`dialogue.voice` 的读写、校验，以及 `worker._voice_for_tts` 的解析。

## 这个文件在守什么

`[speak]` 这条链上最后一环是**音色**：`SpeechOrchestrator._run_synthesis` 在
`request.voice is None` 时抛 `not_configured` → 降级发文字。所以"配置里有音色"
与"这一轮的 TTS 与该音色配套"两件事都成立，语音才会真的出去。

每条测试的 docstring 里都写了**它必须失败于哪个缺陷**。没有这一句的测试，
在"功能未实现"时也可能是绿的（见 `tests/integration/test_speech_reply_wiring.py`
开头对假绿的说明）。

## 它**不能**证明什么

- 三家真实协议：全程不碰网络，`_voice_for_tts` 只造值对象；
- 音频内容对不对：管的是"交给 provider 的是哪一家的哪个值对象"；
- 设置页渲染：那是前端的活（`clients/web-console/`）。
"""

from __future__ import annotations

import hashlib
from dataclasses import fields

import pytest

from ling.ports.config import DialoguePolicy, EffectiveConfig, VoiceSetting
from ling.ports.speech import FishAudioVoice, MimoReferenceVoice, MiniMaxVoice

#: 一份合法的 asset id：`media_assets.digest` 是 sha256 十六进制。
ASSET_ID = hashlib.sha256(b"reference-audio").hexdigest()


# --------------------------------------------------------------------------- #
# 一、配置字段本身
# --------------------------------------------------------------------------- #


def test_default_voice_is_unset_not_a_default_timbre() -> None:
    """默认**没有**音色。

    必须失败于「给一个听起来还行的默认嗓音」：那种实现下 `kind` 不会为空，
    而用户既不知道她换了声音，日志里也看不出异常——`ports/speech.py` 反复
    要防的正是这类自我欺骗。
    """
    voice = DialoguePolicy().voice
    assert voice.kind is None
    assert voice.voice_id is None
    assert voice.reference_asset_id is None
    assert voice.emotion is None


def test_voice_is_a_field_of_dialogue_policy() -> None:
    """音色挂在 `DialoguePolicy.voice` 上，不是散在 `EffectiveConfig` 顶层。"""
    names = {f.name for f in fields(DialoguePolicy)}
    assert "voice" in names
    assert "voice_kind" not in names, "别把音色摊成八个平铺字段（见 service.py 的说明）"


def test_effective_config_carries_voice_through_dialogue() -> None:
    """生效快照读得到音色——这是 `_voice_for_tts` 唯一的来源。"""
    policy = DialoguePolicy(voice=VoiceSetting(kind="minimax_tts", voice_id="v1"))
    effective = EffectiveConfig(revision=1, dialogue=policy)
    assert effective.dialogue.voice.voice_id == "v1"


# --------------------------------------------------------------------------- #
# 二、serde：存得住也要读得回
# --------------------------------------------------------------------------- #


def test_serde_covers_every_voice_field() -> None:
    """`_voice_to_dict` 的键集合 == `VoiceSetting` 的全部字段。

    必须失败于「加了字段忘了补 serde」：那种缺陷是**静默**丢——契约加了、
    UI 显示了、用户选了却没存住。
    """
    from ling.settings.service import _voice_to_dict

    emitted = set(_voice_to_dict(VoiceSetting()).keys())
    declared = {f.name for f in fields(VoiceSetting)}
    assert emitted == declared, (
        f"serde 漏了这些字段（会静默存不住）: {sorted(declared - emitted)}；"
        f"多了这些不存在的字段: {sorted(emitted - declared)}"
    )


def test_dialogue_serde_still_covers_every_field() -> None:
    """加了 `voice` 之后，`_dialogue_to_dict` 仍与 `DialoguePolicy` 一一对应。"""
    from ling.settings.service import _dialogue_to_dict

    emitted = set(_dialogue_to_dict(DialoguePolicy()).keys())
    declared = {f.name for f in fields(DialoguePolicy)}
    assert emitted == declared


def test_voice_round_trip_through_payload() -> None:
    """三个值写进 payload 再读回来，逐字段相同（含 MiMo 的 asset id）。"""
    from ling.settings.service import _dialogue_policy, _dialogue_to_dict

    policy = DialoguePolicy(
        voice=VoiceSetting(
            kind="minimax_tts",
            voice_id="voice_1",
            speed=1.25,
            vol=2.0,
            pitch=-3,
            emotion="happy",
        )
    )
    back = _dialogue_policy(_dialogue_to_dict(policy)).voice
    assert back == policy.voice

    mimo = DialoguePolicy(
        voice=VoiceSetting(kind="mimo_tts", reference_asset_id=ASSET_ID)
    )
    assert _dialogue_policy(_dialogue_to_dict(mimo)).voice == mimo.voice

    fish = DialoguePolicy(voice=VoiceSetting(kind="fishaudio_tts", voice_id="ref-9"))
    assert _dialogue_policy(_dialogue_to_dict(fish)).voice == fish.voice


def test_voice_empty_string_normalises_to_none() -> None:
    """清空输入框（``""``）与没填（``None``）必须变成同一个值。

    必须失败于「空串原样入库」：那会让运行时的 `if not x` 与设置页的
    `if x is None` 两套判断各走一边，症状是"界面上看着清空了，其实还在用"。
    """
    from ling.settings.service import _voice_setting

    voice = _voice_setting({"kind": "minimax_tts", "voice_id": "  ", "emotion": ""})
    assert voice.voice_id is None
    assert voice.emotion is None


def test_voice_numeric_params_are_clamped() -> None:
    """速度/音量/音高夹到区间内：脏值不该让整页读崩。"""
    from ling.settings.service import _voice_setting

    voice = _voice_setting({"speed": 99, "vol": -5, "pitch": 100})
    assert voice.speed == 2.0
    assert voice.vol == 0.0
    assert voice.pitch == 12


# --------------------------------------------------------------------------- #
# 三、补丁：能改，也能**真的删掉**
# --------------------------------------------------------------------------- #


def test_patch_replaces_voice_object() -> None:
    """补丁里的 `voice` 进得了 payload（不靠 `_DIALOGUE_ENUM_KEYS` 那套）。"""
    from ling.settings.service import _apply_patch

    payload: dict = {"dialogue": {"voice_willingness": "natural"}}
    _apply_patch(payload, dialogue={"voice": {"kind": "minimax_tts", "voice_id": "v1"}})
    assert payload["dialogue"]["voice"]["voice_id"] == "v1"
    # 同一次补丁里没提到的键保持原值。
    assert payload["dialogue"]["voice_willingness"] == "natural"


def test_patch_can_clear_the_voice() -> None:
    """显式传 `kind: None` = 取消音色，必须存得下去。

    必须失败于「`None` 一律当没改」：`_merge_fields` 就是这个语义，用它合并
    `voice` 会让"取消音色"永远存不住——用户选了，刷新后又回来了。
    """
    from ling.settings.service import _apply_patch, _dialogue_policy

    payload: dict = {
        "dialogue": {"voice": {"kind": "minimax_tts", "voice_id": "v1"}}
    }
    _apply_patch(payload, dialogue={"voice": {"kind": None}})
    assert _dialogue_policy(payload["dialogue"]).voice.kind is None


# --------------------------------------------------------------------------- #
# 四、校验：不合法要**明确报错**，不是静默丢弃
# --------------------------------------------------------------------------- #


def _voice_fields(dialogue: dict) -> dict[str, str]:
    from ling.settings.service import _voice_issues

    return {issue.field: issue.message for issue in _voice_issues(dialogue, {})}


def test_minimax_without_voice_id_is_an_error() -> None:
    """kind=minimax_tts 但没填 voice_id → **明确报错**，指向 voice_id。

    必须失败于「静默丢弃」：那种实现下 `_voice_issues` 返回空表，草稿照常
    保存，运行起来才降级——用户看到"语音坏了"，而真正的原因是一个没填的格子。
    """
    issues = _voice_fields({"voice": {"kind": "minimax_tts"}})
    assert "dialogue.voice.voice_id" in issues
    assert "voice_id" in issues["dialogue.voice.voice_id"]


def test_fishaudio_without_reference_id_is_an_error() -> None:
    """kind=fishaudio_tts 要 `voice_id`（承载 FishAudio 的 reference_id）。"""
    issues = _voice_fields({"voice": {"kind": "fishaudio_tts"}})
    assert "dialogue.voice.voice_id" in issues


def test_mimo_without_asset_is_an_error() -> None:
    """kind=mimo_tts 要 `reference_asset_id`，不是 voice_id。"""
    issues = _voice_fields({"voice": {"kind": "mimo_tts"}})
    assert "dialogue.voice.reference_asset_id" in issues


def test_mimo_asset_id_must_look_like_a_digest() -> None:
    """参考素材填成文件路径/网址 → 报错，并说清该填什么。

    必须失败于「只判非空」：那样 `/tmp/voice.wav` 也能存进去，运行时才发现
    "找不到素材"，而那已经是一次降级了。
    """
    issues = _voice_fields(
        {"voice": {"kind": "mimo_tts", "reference_asset_id": "/tmp/voice.wav"}}
    )
    assert "dialogue.voice.reference_asset_id" in issues


def test_unknown_kind_is_an_error() -> None:
    """kind 拼错（`minimax` 而不是 `minimax_tts`）→ 明确报错。"""
    issues = _voice_fields({"voice": {"kind": "minimax", "voice_id": "v1"}})
    assert "dialogue.voice.kind" in issues
    assert "minimax" in issues["dialogue.voice.kind"]


@pytest.mark.parametrize(
    "dialogue",
    [
        {"voice": {"kind": "minimax_tts", "voice_id": "v1"}},
        {"voice": {"kind": "fishaudio_tts", "voice_id": "ref-9"}},
        {"voice": {"kind": "mimo_tts", "reference_asset_id": ASSET_ID}},
        {"voice": None},
        {},
    ],
)
def test_complete_voice_has_no_issue(dialogue: dict) -> None:
    """配齐了（或根本没配）→ 没有错误。

    与上面几条配对：只断言"错的会报错"时，一个"永远报错"的实现同样全绿。
    """
    assert _voice_fields(dialogue) == {}


def test_asset_id_uppercase_is_accepted() -> None:
    """sha256 大小写不敏感：别因为用户从别处粘贴了大写就报错。"""
    issues = _voice_fields(
        {"voice": {"kind": "mimo_tts", "reference_asset_id": ASSET_ID.upper()}}
    )
    assert issues == {}


def test_validate_payload_reports_voice_issues() -> None:
    """`_voice_issues` 的错误落在 `field` 上——设置页靠这个字段定位输入框。"""
    from ling.settings.service import _voice_issues

    issues = _voice_issues({"voice": {"kind": "minimax_tts"}}, {})
    assert [issue.field for issue in issues] == ["dialogue.voice.voice_id"]
    assert all(issue.severity == "error" for issue in issues), (
        "音色配错必须是 error（阻断发布），不是 warning：发布后语音会静默降级"
    )


# --------------------------------------------------------------------------- #
# 五、`worker._voice_for_tts`：解析出正确的值对象
# --------------------------------------------------------------------------- #


def _effective(voice: VoiceSetting) -> EffectiveConfig:
    return EffectiveConfig(revision=1, dialogue=DialoguePolicy(voice=voice))


async def test_minimax_voice_is_built_from_config() -> None:
    """配置齐 + kind 匹配 → 返回 `MiniMaxVoice`，参数照抄。

    必须失败于「恒返回 None」（= 补音色之前的状态）：那种实现下语音永远降级。
    """
    from ling.worker import _voice_for_tts

    voice = await _voice_for_tts(
        "minimax_tts",
        effective=_effective(
            VoiceSetting(
                kind="minimax_tts",
                voice_id="voice_1",
                speed=1.2,
                vol=1.5,
                pitch=-2,
                emotion="happy",
            )
        ),
    )
    assert isinstance(voice, MiniMaxVoice)
    assert voice == MiniMaxVoice(
        voice_id="voice_1", speed=1.2, vol=1.5, pitch=-2, emotion="happy"
    )


async def test_fish_voice_is_built_from_config() -> None:
    """FishAudio：`voice_id` 进 `reference_id`，**没有** speed/vol/pitch。"""
    from ling.worker import _voice_for_tts

    voice = await _voice_for_tts(
        "fishaudio_tts",
        effective=_effective(
            VoiceSetting(kind="fishaudio_tts", voice_id="ref-9", speed=1.9)
        ),
    )
    assert isinstance(voice, FishAudioVoice)
    assert voice == FishAudioVoice(reference_id="ref-9")
    assert not hasattr(voice, "speed"), "FishAudio 没有 speed：多带一个字段会是幻觉"


async def test_legacy_kind_alias_is_normalised() -> None:
    """老别名 `fish_audio` 与 `fishaudio_tts` 是同一家，不该判成不匹配。"""
    from ling.worker import _voice_for_tts

    voice = await _voice_for_tts(
        "fish_audio",
        effective=_effective(VoiceSetting(kind="fishaudio_tts", voice_id="ref-9")),
    )
    assert isinstance(voice, FishAudioVoice)


async def test_no_voice_configured_returns_none() -> None:
    """没配音色 → `None`（降级发文字）。

    必须失败于「给默认嗓音」：那种实现下这里会返回一个 `MiniMaxVoice`，
    于是"没配音色"和"配了音色"在调用方看来完全一样。
    """
    from ling.worker import _voice_for_tts

    assert await _voice_for_tts("minimax_tts", effective=_effective(VoiceSetting())) is None


async def test_voice_kind_mismatch_returns_none_and_does_not_guess() -> None:
    """配的是 MiniMax 的音色，这一轮 TTS 是 FishAudio → `None`。

    **不跨厂商猜**：§17.2 撤销了映射表，同一个"温柔"在三家是三个不同的值。
    必须失败于「跨厂商兜底」：那种实现会把 `MiniMaxVoice` 的 voice_id 塞进
    `FishAudioVoice.reference_id`（或反过来），合成出来的声音完全变了却不报错。
    """
    from ling.worker import _voice_for_tts

    voice = await _voice_for_tts(
        "fishaudio_tts",
        effective=_effective(VoiceSetting(kind="minimax_tts", voice_id="voice_1")),
    )
    assert voice is None


async def test_mimo_voice_with_unreadable_asset_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MiMo 配了 asset id 但素材读不到 → `None`，**不抛**。

    必须失败于「读不到就抛」：那条异常会被 `_prepare_speech_reply` 记成
    "装配失败"，而它其实是一个可预期的降级；更要紧的是，如果实现"读不到就
    用空字节"——`MimoReferenceVoice(audio=AudioInput(content=b""))` 同样是
    错的，会让 provider 收到一个空参考音频。

    素材不在库里是这条的输入（`session.get` 返回 None），所以这里**不建**
    任何媒体记录，用真实的读库路径。
    """
    from ling.worker import _voice_for_tts

    voice = await _voice_for_tts(
        "mimo_tts",
        effective=_effective(
            VoiceSetting(kind="mimo_tts", reference_asset_id=ASSET_ID)
        ),
    )
    assert voice is None


async def test_mimo_voice_reads_the_asset_into_audio_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """素材读得到 → `MimoReferenceVoice.audio` 带**真实字节**与资产记录的规格。

    这条与上一条配对：只测"读不到返回 None"，一个"永远返回 None"的实现
    同样全绿。这里断言 `content`、`mime_type`、`container`、`sample_rate`
    都来自资产行，而不是某个默认值。

    用 monkeypatch 替掉库会话与存储层（单元测试不连库），但**走的是
    `_load_reference_audio` 的真实代码路径**：替掉的是边界，不是被测逻辑。
    """
    import contextlib

    import ling.domains.media.storage as storage_module
    import ling.infra.db as db_module
    from ling.worker import _voice_for_tts

    content = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + bytes(64)

    class _Row:
        size_bytes = len(content)
        relative_path = f"generated_media/{ASSET_ID[:2]}/{ASSET_ID}"
        mime_type = "audio/wav"
        format = "wav"
        sample_rate = 24000

        def is_live(self) -> bool:
            return True

    class _Session:
        async def get(self, model: object, key: object) -> object:
            return _Row()

    @contextlib.asynccontextmanager
    async def _fake_scope():  # noqa: ANN202
        yield _Session()

    class _Storage:
        def __init__(self, data_dir: object) -> None:
            self._data_dir = data_dir

        def read(self, digest: str) -> bytes:
            return content if digest == ASSET_ID else b""

    monkeypatch.setattr(db_module, "session_scope", _fake_scope)
    monkeypatch.setattr(storage_module, "GeneratedMediaStorage", _Storage)

    voice = await _voice_for_tts(
        "mimo_tts",
        effective=_effective(VoiceSetting(kind="mimo_tts", reference_asset_id=ASSET_ID)),
    )

    assert isinstance(voice, MimoReferenceVoice), f"没读出来：{voice!r}"
    assert voice.audio.content == content
    assert voice.audio.spec.mime_type == "audio/wav"
    assert voice.audio.spec.container == "wav"
    assert voice.audio.spec.codec == "pcm_s16le"
    assert voice.audio.spec.sample_rate_hz == 24000
    # 文件名由服务端生成，带真实容器扩展名——不用用户上传的原始文件名。
    assert voice.audio.filename.endswith(".wav")
    assert "/" not in voice.audio.filename


def test_worker_voice_size_limit_matches_mimo_adapter() -> None:
    """worker 的字节上限与 MiMo 适配器的 `MAX_VOICE_BYTES` 是同值。

    两处各写一个数迟早会分叉：一边改了另一边没改时，症状是"worker 放行了、
    适配器拒绝"，用户看到的错误离原因很远。这里只在**能导入**时比较——
    导不进来就跳过，而不是让这条守卫依赖适配器能不能加载。
    """
    from ling.models.mimo_tts import MAX_VOICE_BYTES as ADAPTER_LIMIT
    from ling.worker import MAX_VOICE_BYTES as WORKER_LIMIT

    assert WORKER_LIMIT == ADAPTER_LIMIT


def test_upload_voice_size_limit_matches_read_side() -> None:
    """上传端（API）的上限 == 读端（worker）的上限。

    必须失败于「上传端点自己写一个数」：两个数不一致时，症状是**最坏的那一种**
    ——比读端松，用户能传上去、上传还返回 200，但 `_load_reference_audio`
    一直降级发文字，界面上没有任何错误；比读端紧，则是一段读端明明能用的音频
    在入口被拒。

    这条守的是**等式**本身，而不是"两个常量都存在"：删掉上传端那个常量时
    import 就会炸，红得很难看但红得对；把它改成另一个数时，这条是唯一会红的
    地方——两个端点各自都能正常跑，只有"能不能读回自己上传的东西"这件事坏了。
    """
    from ling.api.routes.chat import MAX_VOICE_BYTES as UPLOAD_LIMIT
    from ling.worker import MAX_VOICE_BYTES as WORKER_LIMIT

    assert UPLOAD_LIMIT == WORKER_LIMIT

