"""设置控制面：草稿 → 校验 → 发布 → 回滚。

唯一权威。环境变量只负责启动必需项，不能覆盖这里发布的修订。

不变式：
- 同一时刻只有一个 ``published`` 修订。
- 已发布修订不可变；改动一律新建草稿。
- 密钥只存密文 + 后四位 + 指纹；快照里只有 ``has_secret`` 与 ``secret_last4``。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.errors import LingError
from ling.contracts.v1 import ErrorCode, ModelRole
from ling.infra.models import ConfigRevision, ProviderCredential, utcnow
from ling.infra.security import SecretBox, new_id
from ling.models.registry import bootstrap_config
from ling.ports.config import (
    VOICE_KINDS,
    BindingMode,
    DialoguePolicy,
    EffectiveConfig,
    LifePolicy,
    MemoryPolicy,
    ModelSlot,
    ProfileBinding,
    ProviderConfig,
    ProviderModel,
    VoiceKind,
    VoiceSetting,
    VoiceWillingness,
    voice_requirement_issue,
)
from ling.ports.model import (
    SLOT_LABELS,
    SLOT_ORDER,
    ModelCapability,
    ProviderKind,
    ProviderSlot,
    kind_supports_profile,
    provider_capabilities,
    required_capability,
    slot_for_profile,
)

# 可用的供应商协议：**从 `ProviderKind` 推导**，不在这里再抄一份。
#
# 原来是一份手写集合，于是每加一个 kind 都要记得来改这里——忘了的症状是
# "新供应商能存进草稿，却校验不过"，而错误信息只说"未知供应商类型"，
# 完全指向不存在的拼写问题。唯一真源放在 `ports/model.ProviderKind`。
#
# `anthropic` 与 `chat_responses` 是**独立适配器**，不是给 OpenAI 兼容
# 换个地址就能顶替的：请求体、SSE 事件名、系统提示位置、图片编码、用量字段
# 全都不同（见 `ling/models/anthropic.py` 与 `chat_responses.py` 的模块注释）。
KNOWN_PROVIDER_KINDS = {kind.value for kind in ProviderKind}
KNOWN_PROFILES = {role.value for role in ModelRole}

# 这些角色在设置页**不再展示**：绑定了也没有任何效果。
#
# 两类原因，不能混着说：
# - 无实现：reflection / planning / moderation.advisory / dialogue.deep
#   至今没有任何代码请求它们。展示出来等于承诺一个不存在的能力。
# - 没有 provider：语音三项的入站链路是真的，但没有任何代码按模型职责
#   去构造 provider，绑上去不会有任何代码读它。
#
# （memory.embed / memory.rerank 曾经也在这里，理由是"配置走环境变量"。
#  现在它们有绑定入口了：`domains/memory/providers.py` 先读绑定、读不到
#  才退回环境变量，所以从保留名单挪到了可绑清单。）
#
# `KNOWN_PROFILES` 仍然接受它们（兼容已存在的草稿），只是不再主动提供。
# 某个职责没绑定模型时，**具体会失去什么**。
#
# 为什么必须明说：系统不会因此报错，它会走降级路径——看起来一切正常，
# 只是能力悄悄变窄。图片就是活例子：perception.vision 没绑时媒体描述
# 始终为空，用户只会觉得"她看不见我发的图"，无从判断是没配模型。
#
# 注意这里混了两类，不能混着说：
# - 能力缺失（没绑就真的做不到）：vision、memory.embed、memory.rerank
# - 质量降级（还能工作，但退回一条更笨的路）：memory.extract（退回正则）、
#   dialogue.group（群里干脆不回）。
#
# 这两类从前是"退回另一个角色的模型"（extract/group 都借 dialogue.fast），
# 现在**没有那条路了**：`EffectiveConfig.resolve_profile` 只回退一层，
# 借别人的模型就是套娃——界面会显示"不可用"，实际却在跑另一个角色的模型。
#
# 只列**有明确降级后果**的职责；没有列的角色缺绑定不会静默降级。
#
# 语音三项（speech.asr / speech.tts / realtime.voice）**不在这里**，
# 也不在可绑清单里：它们绑上之后仍然换不来用户能看见的效果。
# 见 RESERVED_PROFILES。
PROFILE_DEGRADATIONS: dict[str, str] = {
    # 记忆提炼**不再借主对话模型代跑**。从前未绑定时它偷偷用 `dialogue.fast`，
    # 而设置页把这条说成"改用主对话模型代跑"——那正是这次要消灭的东西：
    # 那是一层跨角色回退，用户看到的"生效模型"会依赖一条他看不见的链条。
    # 现在没配就真的没有，退回正则，并在这里说清丢掉的是什么。
    "memory.extract": (
        "提炼退回正则：只认「我叫/我喜欢/我住在」几种句式，"
        "「下周三去成都」这种自由文本的事实会整条丢掉"
    ),
    "perception.vision": "看不到用户发来的图片内容，媒体描述始终为空",
    # 视频**故意不在这里**：它现在没有任何调用方（用户要求"她想看才看"，
    # 媒体描述链路不按 kind 自动读视频）。给它写一句"没配就看不到视频内容"
    # 听起来像"配了就能看到"——那是一句现在不成立的话。
    # 等"她主动想看"的入口接线时，再连同那条降级说明一起加。
    "dialogue.fast": "无法对话：这是主回复走的路由",
    "dialogue.group": "群里不会回复：它只跟随自己的「日常」槽位，不再借主对话的模型",
    # 嵌入/重排的降级是**静默**的：检索照常返回结果，只是只剩字面匹配。
    # 用户感知是"换个说法就想不起来"，而这恰恰是语义检索要解决的问题。
    # 注意：只有环境变量也没配时才报降级（见 `/settings/config` 的
    # `memory_sources`），否则会把 env 部署误报成没配。
    "memory.embed": "向量检索不可用：同义词、换个说法就召回不到（只剩字面匹配）",
    "memory.rerank": "不做精排：候选集按融合分数排序，真正相关的那条可能排在后面",
}

RESERVED_PROFILES: dict[str, str] = {
    "reflection": "尚无实现：目前没有任何代码请求这个角色",
    "planning": "尚无实现：目前没有任何代码请求这个角色",
    "moderation.advisory": "尚无实现：目前没有任何代码请求这个角色",
    # 深度对话：**设置页曾经列着它**，但全仓没有任何代码按这个角色发起
    # 请求（`turn.py` 的默认值恒为 `dialogue.fast`，没有任何调用方传 deep）。
    # 列出来等于承诺"绑上就会更聪明"，实际零效果——和语音那三项同一个错。
    # 等真的做出快/慢路由（并且能解释"为什么这次走了 deep"）再挪回可绑清单。
    "dialogue.deep": "尚无实现：目前没有任何代码请求这个角色",
    # 语音三项：**入站链路是真的**（`POST /speech/segments` 落库，
    # `SpeechService.record_partial/record_final`），但绑一个模型仍然换不来
    # 用户能看见的效果。展示出来等于承诺"绑上就能听见/说话"，而实际零效果——
    # 比没有这个入口更糟：用户会以为自己配错了。
    # `speech.tts` 与另两项**不再是同一个原因**（2026-11-12 拆开写）：
    # 出站 provider 已经能按绑定真的构造出来了
    # （`domains/speech/providers.py::build_tts_provider`，`GET /v1/speech`
    # 读的就是它），三家的配置、密钥、模型都能通到底。
    # **缺的是"谁在什么时候调它"**：`[speak]` 能被
    # `capabilities/expression_choice.py` 解析成 `ExpressionIntent`，但没有任何
    # 生产代码把结果交给 `SpeechOrchestrator.synthesize()`（那个方法在 `src/`
    # 里零调用点，只有集成测试手工构造它）。所以绑上也**不会有任何朗读**。
    # 等出站触发链路接上（`REQUIREMENTS-v0.0.7.md` REQ-10 的"完整回复链"），
    # 再把这条挪回 `api/routes/settings.py::_OFFERABLE_PROFILES`。
    "speech.tts": (
        "尚无实现：TTS provider 已能按绑定构造，但没有出站触发链路"
        "（[speak] 的解析结果没有任何代码交给合成），绑定后仍不会有任何朗读"
    ),
    "realtime.voice": "尚无实现：没有实时语音会话实现，绑定后不会有任何效果",
}


@dataclass
class ValidationIssue:
    """配置校验问题。

    ``severity=error`` 阻断发布；``warning`` 只提示（例如供应商还没存密钥，
    发布后对话会给出可执行失败提示，而不是把整份配置锁死）。
    """

    field: str
    message: str
    severity: str = "error"

    @property
    def blocking(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "message": self.message, "severity": self.severity}


@dataclass
class DraftResult:
    revision: int
    state: str
    validation_errors: list[ValidationIssue] = field(default_factory=list)


def _provider_shape_issues(pid: str, cfg: dict) -> list[ValidationIssue]:
    """按**能力**校验供应商的必填项。

    不同能力要的连接信息不一样，不能共用一套规则（那正是"把嵌入塞进对话
    下拉"会踩的坑）：

    - ``chat``（openai_compatible / local / anthropic / chat_responses）：
      必须给 base_url——它们的端点没有可推导的默认值。
    - ``embed``（Cloudflare）：要 ``account_id``（存在 ``extra.account_id``，
      对应环境变量 ``LING_EMBEDDING_ACCOUNT_ID``）。base_url 可以不给：
      ``CloudflareEmbedding`` 会用 account_id 拼出官方端点；给了就必须是
      http(s)。只给 base_url 不给 account_id 也放行——环境变量那套一直
      支持这种配置（``LING_EMBEDDING_API_BASE``），不能因为进了界面就
      要求用户多填一项。
    - ``rerank``（SiliconFlow）：必须给 base_url，它没有"按账号拼端点"的规则。

    密钥一律只 **warning**：发布不该因为"还没填密钥"被锁死（错误分类会在
    调用时给 ``secret_missing``），但界面上必须能看出"还没配完"。
    """

    issues: list[ValidationIssue] = []
    kind = str(cfg.get("kind") or "")
    # fake 是进程内的确定性实现：没有端点，也不需要密钥。原来那套 chat
    # 校验把它排除在外，这里保持同一行为——否则每个测试配置都会多出一条
    # "还没保存密钥"的警告，而那条警告对 fake 没有意义。
    if kind == ProviderKind.fake.value:
        return issues
    capabilities = provider_capabilities(kind)
    base_url = str(cfg.get("base_url") or "").strip()

    if ModelCapability.chat in capabilities or ModelCapability.rerank in capabilities:
        # 提示语按能力分开写："兼容接口"是 chat 的说法，套在重排供应商上
        # 会让人以为要配的是一家 OpenAI 兼容端点。
        missing_hint = (
            "兼容接口必须提供 base_url"
            if ModelCapability.chat in capabilities
            else "重排供应商必须提供 base_url（例如 https://api.siliconflow.cn/v1）"
        )
        if not base_url:
            issues.append(ValidationIssue(f"providers.{pid}.base_url", missing_hint))
        elif not (base_url.startswith("http://") or base_url.startswith("https://")):
            issues.append(
                ValidationIssue(
                    f"providers.{pid}.base_url", "base_url 必须以 http:// 或 https:// 开头"
                )
            )

    if ModelCapability.embed in capabilities:
        extra = cfg.get("extra") if isinstance(cfg.get("extra"), dict) else {}
        account_id = str((extra or {}).get("account_id") or "").strip()
        if not account_id and not base_url:
            issues.append(
                ValidationIssue(
                    f"providers.{pid}.extra.account_id",
                    "Cloudflare 嵌入需要 account_id（供应商的 extra.account_id，"
                    "对应环境变量 LING_EMBEDDING_ACCOUNT_ID），或直接给出完整 base_url",
                )
            )
        if base_url and not (
            base_url.startswith("http://") or base_url.startswith("https://")
        ):
            issues.append(
                ValidationIssue(
                    f"providers.{pid}.base_url", "base_url 必须以 http:// 或 https:// 开头"
                )
            )

    if ModelCapability.asr in capabilities and base_url and not (
        base_url.startswith("http://") or base_url.startswith("https://")
    ):
        issues.append(
            ValidationIssue(
                f"providers.{pid}.base_url", "base_url 必须以 http:// 或 https:// 开头"
            )
        )

    if capabilities and not cfg.get("has_secret"):
        issues.append(
            ValidationIssue(
                f"providers.{pid}.secret",
                "该供应商还没有保存密钥，发布后调用会返回 secret_missing",
                severity="warning",
            )
        )
    return issues


def _resolves(payload: dict, profile: str) -> bool:
    """这份 payload 里，这个职责最终有没有模型可用。

    复用**运行时那个**解析函数，而不是在这里再写一条"有没有绑定"的判断：
    后者在上槽位之后就不再等价了——`dialogue.fast` 完全可以没有任何显式
    绑定却照常工作（跟随「快速」槽位），此时报"必须绑定"是在拦一份能用的
    配置。
    """
    try:
        config = effective_from_payload(
            payload, revision=0, persona_default="", log_denials_default=True
        )
    except (TypeError, ValueError, KeyError, AttributeError):
        # payload 本身有毛病时另有专门的错误，这里不重复报。
        return True
    return config.resolve_profile(profile).usable


def _voice_issues(
    dialogue: object, providers: dict[str, dict]
) -> list[ValidationIssue]:
    """音色的校验：**明确的错误，不是静默丢弃**。

    三条规则，各自的理由：

    1. ``kind`` 必须是已知的三家之一。写错了（比如 ``minimax``）如果静默
       忽略，运行时就只是"没有音色"→ 降级发文字，用户看到的是"语音功能坏了"，
       而不是"我把音色名打错了"。
    2. ``voice_id`` / ``reference_asset_id`` 必须与 ``kind`` **配套**——
       `ports/speech.voice_requirement_issue` 是这条的唯一判定，运行时降级
       也调它，所以不会出现"保存时说过没问题、跑起来降级"。
    3. ``reference_asset_id`` 必须是媒体的 sha256（64 位十六进制），且
       **真的在 media_assets 里**。这一条在这里判不了（要查库，而这函数
       是纯的），所以只判形态；"素材读不到"由运行时降级处理。

    ## 不跨厂商猜

    ``kind=mimo_tts`` 却只填了 ``voice_id`` **不报错**：那个 id 可能是用户
    从 MiniMax 那边抄过来的残留。运行时会因为缺 ``reference_asset_id`` 降级，
    但**不会**拿那个 ``voice_id`` 去喂别家——§17.2 撤销了映射表，代码不替
    用户做这种翻译。所以这里只在"缺该有的"时报错，不在"多填了别人的"时报错。
    """
    data = dialogue if isinstance(dialogue, dict) else {}
    raw = data.get("voice")
    if raw is None:
        # 没配过音色：这是**合法状态**（最多降级发文字），不是校验错误。
        # 强制每个用户先选音色，会让只想用文字的人连草稿都存不下。
        return []
    if not isinstance(raw, dict):
        return [ValidationIssue("dialogue.voice", "音色配置必须是一个对象")]

    issues: list[ValidationIssue] = []
    voice = _voice_setting(raw)

    kind_issue = voice_requirement_issue(voice)
    if kind_issue is not None:
        field = "dialogue.voice.kind"
        if voice.kind in VOICE_KINDS:
            field = (
                "dialogue.voice.reference_asset_id"
                if voice.kind == "mimo_tts"
                else "dialogue.voice.voice_id"
            )
        issues.append(ValidationIssue(field, kind_issue))
        # 已报过"这套音色不能用"，下面的形态检查没有意义。
        return issues

    if voice.kind is None:
        # 没选类型却填了参数：多半是清空了一半。说出来，别让用户以为已生效。
        leftovers = [
            key
            for key in ("voice_id", "reference_asset_id")
            if getattr(voice, key)
        ]
        if leftovers:
            issues.append(
                ValidationIssue(
                    "dialogue.voice.kind",
                    "填了音色参数却没有选音色类型（kind）：选了类型才会被使用",
                )
            )
        return issues

    asset_id = voice.reference_asset_id
    if voice.kind == "mimo_tts" and asset_id is not None:
        looks_like_digest = len(asset_id) == _ASSET_ID_HEX_LEN and all(
            ch in "0123456789abcdef" for ch in asset_id.lower()
        )
        if not looks_like_digest:
            issues.append(
                ValidationIssue(
                    "dialogue.voice.reference_asset_id",
                    "参考素材 id 应该是媒体库里那份音频的 sha256（64 位十六进制）。"
                    "上传参考音频后取它的 digest 填在这里；不要填文件路径或网址。",
                )
            )
    return issues


def _slot_issues(payload: dict, providers: dict[str, dict]) -> list[ValidationIssue]:
    """默认槽位的校验。

    槽位是**回退的唯一去处**，所以错的槽位比错的绑定更贵：绑定错了只影响
    一个职责，槽位错了会把所有跟随它的职责一起带进"不可用"。
    """
    issues: list[ValidationIssue] = []
    known = {slot.value for slot in SLOT_ORDER}
    seen: set[str] = set()
    for raw in payload.get("slots") or []:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("slot") or "").strip()
        if key not in known:
            issues.append(ValidationIssue(f"slots.{key or '?'}", f"未知槽位: {key}"))
            continue
        if key in seen:
            issues.append(ValidationIssue(f"slots.{key}", "同一槽位只能配一次"))
        seen.add(key)
        field = f"slots.{key}"
        label = SLOT_LABELS[ProviderSlot(key)]
        pid = str(raw.get("provider_id") or "").strip()
        mid = str(raw.get("model_id") or "").strip()
        if not pid and not mid:
            continue
        if not pid:
            issues.append(
                ValidationIssue(f"{field}.provider_id", f"「{label}」槽位给了模型却没给供应商")
            )
            continue
        cfg = providers.get(pid)
        if cfg is None:
            issues.append(
                ValidationIssue(f"{field}.provider_id", f"「{label}」槽位的供应商未登记: {pid}")
            )
            continue
        if not mid:
            issues.append(
                ValidationIssue(f"{field}.model_id", f"「{label}」槽位必须填写模型 ID")
            )
            continue
        kind = str(cfg.get("kind") or "")
        if not payload.get("test_mode") and kind == "fake":
            issues.append(
                ValidationIssue(
                    f"{field}.provider_id",
                    "非测试模式下不能把默认槽位配成 fake provider（会冒充模型能力）",
                )
            )
        # 四个槽位全部服务于对话类职责，所以它们要的是 `chat` 能力。
        # 把嵌入/重排供应商填进槽位，发布能成功、调用必失败。
        if kind in KNOWN_PROVIDER_KINDS and ModelCapability.chat not in provider_capabilities(
            kind
        ):
            offered = "/".join(
                sorted(c.value for c in provider_capabilities(kind))
            ) or "无"
            issues.append(
                ValidationIssue(
                    f"{field}.provider_id",
                    f"「{label}」槽位要用对话模型，而供应商 {pid} 是 {kind}"
                    f"（提供：{offered}）。换个对话供应商，否则跟随它的职责全都跑不起来。",
                )
            )
        if not cfg.get("enabled", True):
            issues.append(
                ValidationIssue(
                    f"{field}.provider_id",
                    f"「{label}」槽位的供应商 {pid} 已停用，跟随它的职责都会变成不可用",
                    severity="warning",
                )
            )
        registered = {
            str(m.get("model_id", "")).strip(): m
            for m in cfg.get("models") or []
            if isinstance(m, dict)
        }
        if mid in registered and not registered[mid].get("enabled", True):
            issues.append(
                ValidationIssue(
                    f"{field}.model_id",
                    f"「{label}」槽位用的模型 {mid} 已在 {pid} 的清单里停用",
                    severity="warning",
                )
            )
    return issues


class ConfigService:
    def __init__(
        self, session: AsyncSession, secret_box: SecretBox, *, test_mode_default: bool = True,
        # ``None`` = 读 `Settings.persona_id`（即 ``LING_PERSONA_ID``）。
        # 默认值不再写字面量 ``"persona_ling"``：那会让"环境变量里换了人格"
        # 在没发布过修订的部署上静默失效。
        persona_id: str | None = None, log_denials_default: bool = True
    ) -> None:
        self._session = session
        self._box = secret_box
        self._test_mode_default = test_mode_default
        # 没显式给就取环境变量（`Settings.persona_id` ← `LING_PERSONA_ID`）。
        # **只在"没有已发布修订"时用得到**：发布过修订之后，人格以修订里的
        # `persona_id` 为准（见 `effective()` 传给 `effective_from_payload`
        # 的 `persona_default`）。这正是"环境变量只作初始默认"的那条线。
        if persona_id is None:
            from ling.config import get_settings

            persona_id = get_settings().persona_id
        self._persona_id = persona_id
        # `LING_LOG_ACCESS_DENIALS`。只在"没有已发布修订"或"修订里没有这个键"
        # （旧修订）时兜底；发布过带该键的修订之后以修订为准。
        self._log_denials_default = log_denials_default

    # ---------------------------------------------------------- 读取

    async def published_revision(self) -> ConfigRevision | None:
        return (
            await self._session.execute(
                select(ConfigRevision)
                .where(ConfigRevision.state == "published")
                .order_by(ConfigRevision.revision.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def effective(self) -> EffectiveConfig:
        """生效快照。没有已发布修订时返回引导配置（测试模式下绑定 fake）。"""
        published = await self.published_revision()
        if published is None:
            return bootstrap_config(
                test_mode=self._test_mode_default,
                persona_id=self._persona_id,
                log_access_denials=self._log_denials_default,
            )
        return effective_from_payload(
            dict(published.payload or {}),
            revision=published.revision,
            persona_default=self._persona_id,
            log_denials_default=self._log_denials_default,
        )

    async def list_revisions(self, limit: int = 20) -> list[ConfigRevision]:
        return list(
            (
                await self._session.execute(
                    select(ConfigRevision)
                    .order_by(ConfigRevision.revision.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    # ---------------------------------------------------------- 写入

    async def save_draft(
        self,
        *,
        providers: list[dict] | None = None,
        profiles: list[dict] | None = None,
        slots: list[dict] | None = None,
        test_mode: bool | None = None,
        log_access_denials: bool | None = None,
        memory: dict | None = None,
        life: dict | None = None,
        dialogue: dict | None = None,
        note: str | None = None,
        created_by: str | None = None,
    ) -> DraftResult:
        base = await self.effective()
        current_payload = payload_from_effective(base)
        _apply_patch(
            current_payload,
            providers=providers,
            profiles=profiles,
            slots=slots,
            memory=memory,
            life=life,
            dialogue=dialogue,
            test_mode=test_mode,
            log_access_denials=log_access_denials,
        )

        issues = await self.validate_payload(current_payload)
        next_rev = await self._next_revision_number()
        row = ConfigRevision(
            id=new_id("cfg"),
            revision=next_rev,
            state="draft",
            payload=current_payload,
            validation_errors=[i.to_dict() for i in issues],
            created_by=created_by,
            note=note,
        )
        self._session.add(row)
        await self._session.flush()
        return DraftResult(revision=row.revision, state=row.state, validation_errors=issues)

    async def resolve_preview(
        self,
        *,
        providers: list[dict] | None = None,
        profiles: list[dict] | None = None,
        slots: list[dict] | None = None,
        test_mode: bool | None = None,
    ) -> EffectiveConfig:
        """按**当前草稿**算每个职责的生效模型。**只算，不写库。**

        界面上的「实际生效」列、快速配置的二次确认，全都要一个答案：
        "我这样改完，每个职责最后会跑哪个模型？"。

        这个答案**不能在浏览器里算**：回退规则要是前端再实现一遍，两边
        迟早不一致——而这类不一致的症状是"界面说在用 A，实际调的是 B"，
        用户永远查不出来。所以让前端把草稿发过来，服务端用**与保存完全
        同一段合并逻辑**（`_apply_patch`）拼出 payload，再用**与运行时
        完全同一个** `resolve_profile` 解析。

        合并逻辑共用是关键：另写一份"预览用的合并"迟早与 save_draft 分叉，
        于是预览说"会生效"、保存后却是另一回事。
        """
        base = await self.effective()
        payload = payload_from_effective(base)
        _apply_patch(
            payload,
            providers=providers,
            profiles=profiles,
            slots=slots,
            test_mode=test_mode,
        )
        return effective_from_payload(
            payload,
            revision=base.revision,
            persona_default=self._persona_id,
            log_denials_default=self._log_denials_default,
        )

    async def validate_payload(self, payload: dict) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        providers = {p.get("provider_id"): p for p in payload.get("providers", [])}
        for pid, cfg in providers.items():
            kind = cfg.get("kind")
            if kind not in KNOWN_PROVIDER_KINDS:
                issues.append(ValidationIssue(f"providers.{pid}.kind", f"未知供应商类型: {kind}"))
                continue
            issues.extend(_provider_shape_issues(pid, cfg))

            # 传输与额度：-1 表示不限制，其余必须是正数。
            # 这些校验放在**供应商**这一层，因为三项都跟着供应商走；
            # 职责那边只回答"交给谁"，不回答"怎么连、能用多少"。
            timeout = cfg.get("timeout_seconds", 60.0)
            try:
                timeout_val = float(timeout)
            except (TypeError, ValueError):
                issues.append(
                    ValidationIssue(
                        f"providers.{pid}.timeout_seconds", "timeout_seconds 必须是数字"
                    )
                )
            else:
                if not 1.0 <= timeout_val <= 600.0:
                    issues.append(
                        ValidationIssue(
                            f"providers.{pid}.timeout_seconds",
                            "timeout_seconds 必须在 1~600 秒之间",
                        )
                    )
            for key, label in (
                ("max_output_tokens", "单次输出上限"),
                ("daily_token_limit", "日限额"),
            ):
                raw_value = cfg.get(key)
                if raw_value is None:
                    continue
                try:
                    value = int(raw_value)
                except (TypeError, ValueError):
                    issues.append(
                        ValidationIssue(f"providers.{pid}.{key}", f"{label}必须是整数")
                    )
                    continue
                # -1 是"不限制"的约定值；0 和 -2 之类没有含义，拦下来。
                if value == -1:
                    continue
                if value <= 0:
                    issues.append(
                        ValidationIssue(
                            f"providers.{pid}.{key}",
                            f"{label}必须为正整数，或填 -1 表示不限制",
                        )
                    )

            # 模型清单：空 id 是错误（前端不会提交，但 API 可能）；
            # 重复 id 在 `_clean_models` 里已按"最后一条生效"归一化，
            # 所以这里只需要拦住空值。
            models = cfg.get("models") or []
            if not isinstance(models, list):
                issues.append(
                    ValidationIssue(f"providers.{pid}.models", "models 必须是数组")
                )
            else:
                for index, model in enumerate(models):
                    if not isinstance(model, dict) or not str(
                        model.get("model_id", "")
                    ).strip():
                        issues.append(
                            ValidationIssue(
                                f"providers.{pid}.models[{index}].model_id",
                                "模型 ID 不能为空",
                            )
                        )

        # 同一时刻只启用一家：这是设置页的默认心智模型（另一家留着备用，
        # 而不是同时分流）。做成 warning 而不是 error，是因为跨供应商回退
        # 需要两家都启用；直接阻断会让已有配置无法再保存。
        #
        # **按能力分组**，不能把所有启用的供应商混在一起数：嵌入与重排是
        # 另一套接口（cloudflare_embedding / siliconflow_rerank），同时启用
        # 「一家对话 + 一家嵌入」是**正常配置**，混着数会把它报成"同时启用
        # 2 家"，用户照着提示去停用，语义检索就没了。
        enabled_by_capability: dict[ModelCapability, list[str]] = {}
        for pid, cfg in providers.items():
            if not cfg.get("enabled", True):
                continue
            for capability in sorted(provider_capabilities(str(cfg.get("kind") or ""))):
                enabled_by_capability.setdefault(capability, []).append(pid)
        for capability, pids in enabled_by_capability.items():
            if len(pids) < 2:
                continue
            label = "" if capability is ModelCapability.chat else f"{capability.value} 能力的"
            issues.append(
                ValidationIssue(
                    "providers",
                    f"同时启用了 {len(pids)} 家{label}供应商"
                    f"（{'、'.join(pids)}）。同一时刻只建议启用一家；"
                    "只有在配置跨供应商回退时才需要多家同时启用。",
                    severity="warning",
                )
            )

        seen_profiles: set[str] = set()
        for raw in payload.get("profiles", []):
            prof = raw.get("profile", "")
            if prof not in KNOWN_PROFILES:
                issues.append(ValidationIssue(f"profiles.{prof}", f"未知职责: {prof}"))
                continue
            if prof in seen_profiles:
                issues.append(ValidationIssue(f"profiles.{prof}", "同一职责只能绑定一次"))
            seen_profiles.add(prof)
            mode = _binding_mode(raw)
            if mode is BindingMode.slot:
                # 「跟随槽位」时 provider/model 不参与解析，校验它们毫无意义
                # （而且会误导：填着旧值却根本不生效）。只需要确认这个职责
                # **有槽位可跟**——`memory.embed` 之类没有默认值的角色，
                # 选"跟随"就等于把自己关掉。
                if slot_for_profile(prof) is None:
                    issues.append(
                        ValidationIssue(
                            f"profiles.{prof}.mode",
                            f"{prof} 没有默认槽位（嵌入/重排走另一套协议），"
                            "必须单独指定模型，不能选「跟随默认」",
                        )
                    )
                continue
            pid = raw.get("provider_id")
            if not pid or pid not in providers:
                issues.append(
                    ValidationIssue(f"profiles.{prof}.provider_id", f"未登记的 provider: {pid}")
                )
                continue
            if not payload.get("test_mode") and providers[pid].get("kind") == "fake":
                issues.append(
                    ValidationIssue(
                        f"profiles.{prof}.provider_id",
                        "非测试模式下不能把职责绑定到 fake provider（会冒充模型能力）",
                    )
                )
            # 能力必须对得上：把 memory.embed 绑到对话供应商，发布也不会报错，
            # 但装配时只会拿到 None——语义检索静默失效。这正是这次要修的毛病，
            # 所以在发布前就拦下（error，不是 warning）。
            kind = str(providers[pid].get("kind") or "")
            need = required_capability(prof)
            if (
                need is not None
                and kind in KNOWN_PROVIDER_KINDS
                and not kind_supports_profile(kind, prof)
            ):
                offered = provider_capabilities(kind)
                issues.append(
                    ValidationIssue(
                        f"profiles.{prof}.provider_id",
                        f"{prof} 需要「{need.value}」能力，而供应商 {pid} 是 {kind}"
                        f"（提供：{'/'.join(sorted(c.value for c in offered)) or '无'}）。"
                        "选一个具备该能力的供应商，否则发布后这个职责不会真正生效。",
                    )
                )
            # 停用 ≠ 解绑：旧绑定还在，调用会失败。发布时不拦（用户可能
            # 只是临时停用再切回来），但必须在设置页说清"需要检查"。
            if not providers[pid].get("enabled", True):
                issues.append(
                    ValidationIssue(
                        f"profiles.{prof}.provider_id",
                        f"供应商 {pid} 已停用，这个职责调用时会失败",
                        severity="warning",
                    )
                )
            registered = {
                str(m.get("model_id", "")).strip(): m
                for m in providers[pid].get("models") or []
                if isinstance(m, dict)
            }
            bound_model = (raw.get("model_id") or "").strip()
            if bound_model and bound_model in registered and not registered[
                bound_model
            ].get("enabled", True):
                issues.append(
                    ValidationIssue(
                        f"profiles.{prof}.model_id",
                        f"模型 {bound_model} 已在 {pid} 的清单里停用，这个职责仍指向它",
                        severity="warning",
                    )
                )
            if not (raw.get("model_id") or "").strip():
                issues.append(ValidationIssue(f"profiles.{prof}.model_id", "必须填写模型 ID"))
            # 超时 / 输出上限 / 日额度**不在这里校验**：它们属于供应商
            # （见下面 providers 那一轮）。旧修订里可能还留着这几个键，
            # 读的时候被忽略即可——不必因为一份旧 payload 就报错。
            fb_pid = raw.get("fallback_provider_id")
            if fb_pid:
                if fb_pid not in providers:
                    issues.append(
                        ValidationIssue(
                            f"profiles.{prof}.fallback_provider_id",
                            f"回退 provider 未登记: {fb_pid}",
                        )
                    )
                elif not payload.get("test_mode") and providers[fb_pid].get("kind") == "fake":
                    issues.append(
                        ValidationIssue(
                            f"profiles.{prof}.fallback_provider_id",
                            "非测试模式下不能用 fake provider 作为回退（会冒充模型能力）",
                        )
                    )
                elif not providers[fb_pid].get("enabled", True):
                    issues.append(
                        ValidationIssue(
                            f"profiles.{prof}.fallback_provider_id",
                            f"回退供应商 {fb_pid} 已停用，回退不会生效",
                            severity="warning",
                        )
                    )
                elif (
                    need is not None
                    and str(providers[fb_pid].get("kind") or "") in KNOWN_PROVIDER_KINDS
                    and not kind_supports_profile(
                        str(providers[fb_pid].get("kind") or ""), prof
                    )
                ):
                    # 回退也必须是同一个能力：换一家"能连但不具备该能力"的
                    # 供应商，等于把主路失败偷偷换成另一种失败。
                    issues.append(
                        ValidationIssue(
                            f"profiles.{prof}.fallback_provider_id",
                            f"回退供应商 {fb_pid} 不具备「{need.value}」能力",
                        )
                    )
                if not (raw.get("fallback_model_id") or "").strip():
                    issues.append(
                        ValidationIssue(
                            f"profiles.{prof}.fallback_model_id", "配置了回退 provider 就必须给回退模型"
                        )
                    )

        if not payload.get("test_mode") and not _resolves(payload, "dialogue.fast"):
            issues.append(
                ValidationIssue(
                    "profiles.dialogue.fast",
                    "非测试模式下 dialogue.fast 必须有一个能用的模型："
                    "单独指定它，或者把「快速」槽位配好（主回复走的就是这条路）",
                )
            )

        issues.extend(_voice_issues(payload.get("dialogue"), providers))
        issues.extend(_slot_issues(payload, providers))
        return issues

    async def publish(self, revision: int, *, published_by: str | None = None) -> int:
        row = (
            await self._session.execute(
                select(ConfigRevision).where(ConfigRevision.revision == revision)
            )
        ).scalar_one_or_none()
        if row is None:
            raise LingError(ErrorCode.not_found, f"修订 {revision} 不存在", status_code=404)
        issues = await self.validate_payload(dict(row.payload or {}))
        blocking = [i for i in issues if i.blocking]
        row.validation_errors = [i.to_dict() for i in issues]
        if blocking:
            await self._session.flush()
            raise LingError(
                ErrorCode.config_invalid,
                "配置校验未通过，未发布",
                status_code=422,
                detail="; ".join(f"{i.field}: {i.message}" for i in blocking),
            )
        await self._session.execute(
            update(ConfigRevision)
            .where(ConfigRevision.state == "published")
            .values(state="superseded")
        )
        row.state = "published"
        row.published_at = utcnow()
        row.validation_errors = []
        if published_by:
            row.created_by = row.created_by or published_by
        await self._session.flush()
        return row.revision

    async def rollback(self, to_revision: int) -> int:
        """回滚 = 把历史修订重新发布为当前生效（不复制 payload 内容）。"""
        return await self.publish(to_revision)

    async def set_active_persona(self, persona_id: str, *, changed_by: str | None = None) -> int:
        """切换当前激活的人格 = 改 ``persona_id`` 并**发布一条新修订**。

        ## 为什么走"保存草稿 + 立即发布"，而不是直接改已发布那行

        1. **原子**：两条 SQL 在同一个事务里，中间没有"草稿已存、还没发布"的
           半成状态；任一步失败整笔回滚，人格不会停在"改了一半"。
        2. **可回滚**：人格跟着修订走，``POST /settings/rollback`` 回到上一版
           修订时，人格也一起回去。这是"当前人格即配置"这个设计真正的好处。
        3. **跨进程可见**：别的副本与 worker 靠修订号变化感知，不需要新机制。

        只改 ``persona_id`` 一个键：其余键从当前生效快照原样带过来
        （``payload_from_effective``），供应商、职责、记忆策略一个字都不动。

        不在这里校验"这一行存在"：那是人格域的判据
        （``PersonaService.get`` / 路由层），重复一份会分叉。
        """
        persona_id = str(persona_id or "").strip()
        if not persona_id:
            raise LingError(
                ErrorCode.invalid_request, "persona_id 不能为空", status_code=422
            )
        base = await self.effective()
        payload = payload_from_effective(base)
        payload["persona_id"] = persona_id
        issues = await self.validate_payload(payload)
        blocking = [i for i in issues if i.blocking]
        if blocking:
            # 与手动「保存草稿」同一条路：切换人格不能绕过发布前校验，
            # 否则会出现"点一下切换就把配置发布成非法状态"。
            raise LingError(
                ErrorCode.config_invalid,
                "当前配置校验未通过，不能切换人格",
                status_code=422,
                detail="; ".join(f"{i.field}: {i.message}" for i in blocking),
            )
        row = ConfigRevision(
            id=new_id("cfg"),
            revision=await self._next_revision_number(),
            state="draft",
            payload=payload,
            validation_errors=[i.to_dict() for i in issues],
            created_by=changed_by,
            note=f"切换人格 → {persona_id}",
        )
        self._session.add(row)
        await self._session.flush()
        return await self.publish(row.revision, published_by=changed_by)

    async def _next_revision_number(self) -> int:
        latest = (
            await self._session.execute(
                select(ConfigRevision).order_by(ConfigRevision.revision.desc()).limit(1)
            )
        ).scalar_one_or_none()
        return int(latest.revision) + 1 if latest else 1

    # ---------------------------------------------------------- 密钥

    async def set_secret(self, provider_id: str, secret: str) -> dict[str, str]:
        if not provider_id:
            raise LingError(ErrorCode.invalid_request, "provider_id 不能为空", status_code=422)
        secret = secret.strip()
        if not secret:
            raise LingError(ErrorCode.invalid_request, "密钥不能为空", status_code=422)
        row = (
            await self._session.execute(
                select(ProviderCredential).where(ProviderCredential.provider_id == provider_id)
            )
        ).scalar_one_or_none()
        ciphertext = self._box.encrypt(secret)
        last4 = SecretBox.last4(secret)
        fingerprint = SecretBox.fingerprint(secret)
        if row is None:
            row = ProviderCredential(
                id=new_id("cred"),
                provider_id=provider_id,
                ciphertext=ciphertext,
                last4=last4,
                fingerprint=fingerprint,
            )
            self._session.add(row)
        else:
            row.ciphertext = ciphertext
            row.last4 = last4
            row.fingerprint = fingerprint
            row.updated_at = utcnow()
        await self._session.flush()
        await self._sync_secret_flags(provider_id, has_secret=True, last4=last4)
        await self._bump_for_secret_change(provider_id, note=f"密钥变更：{provider_id}")
        return {"last4": last4, "fingerprint": fingerprint}

    async def delete_secret(self, provider_id: str) -> None:
        row = (
            await self._session.execute(
                select(ProviderCredential).where(ProviderCredential.provider_id == provider_id)
            )
        ).scalar_one_or_none()
        if row is not None:
            await self._session.delete(row)
            await self._session.flush()
        await self._sync_secret_flags(provider_id, has_secret=False, last4="")
        await self._bump_for_secret_change(provider_id, note=f"密钥删除：{provider_id}")

    async def _bump_for_secret_change(self, provider_id: str, *, note: str) -> int | None:
        """密钥变更后**发布一个新修订**，好让其他进程能感知到。

        为什么必须升修订号，而不是只写凭据表：跨进程传播的唯一信号就是
        ``refresh.current_revision()`` 的返回值——API 的 ``RevisionWatcher``
        与 worker 的 ``refresh_memory_router_if_stale()`` 都比对修订号。
        只 upsert 凭据表的话，别的副本与 worker 永远认为"配置没变"，继续
        用旧密钥：表现是换密钥后一部分请求失败、worker 的记忆提炼静默退回
        正则（用户看到的是"它有时能聊有时不能，还记不住话"）。

        为什么选"新建已发布修订"而不是"在 ``stale()`` 里比对密钥指纹"：
        指纹方案要改 ``RevisionWatcher`` 的调用约定（``mark_loaded`` 需要
        拿到当时的指纹，调用点在 ``app.py`` / ``chat.py``），而且 worker
        那条路径根本不用 ``RevisionWatcher``，它只看修订号——指纹方案对
        worker 完全无效。修订号是两条路径共用的唯一信号，改在源头最省。

        代价：每次换密钥会多出一条"内容与上一版相同"的修订。这是有意的——
        修订历史里留下"谁在何时换了密钥"是可审计的事实，不是噪音。
        ``note`` 写明是密钥变更，避免运维以为是用户改了配置。

        没有已发布修订时**不造修订**：此时进程都跑在引导配置上，没有生效
        配置可改，凭空发布一份反而会把 ``test_mode`` 等引导行为固化下来。
        """
        published = await self.published_revision()
        if published is None:
            return None
        payload = copy.deepcopy(dict(published.payload or {}))
        next_rev = await self._next_revision_number()
        # 先降级旧的，再插入新的——顺序与 ``publish()`` 一致。反过来写的话，
        # ``execute()`` 的自动 flush 会把新行按 ``published`` 落库，
        # 紧接着的 update 又会把它自己降级成 superseded。
        await self._session.execute(
            update(ConfigRevision)
            .where(ConfigRevision.state == "published")
            .values(state="superseded")
        )
        row = ConfigRevision(
            id=new_id("cfg"),
            revision=next_rev,
            state="published",
            payload=payload,
            validation_errors=[],
            published_at=utcnow(),
            note=note,
        )
        self._session.add(row)
        await self._session.flush()
        return next_rev

    async def read_secret(self, provider_id: str) -> str | None:
        row = (
            await self._session.execute(
                select(ProviderCredential).where(ProviderCredential.provider_id == provider_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        try:
            return self._box.decrypt(row.ciphertext)
        except Exception:  # noqa: BLE001 - 根密钥变化后旧密文不可解
            return None

    async def secret_diagnosis(self, provider_id: str) -> str:
        """密钥状态：``ok`` / ``missing`` / ``undecryptable``。

        为什么要区分：两者都让 ``read_secret`` 返回 None，但原因完全不同。
        - ``missing``：从来没存过 → "去设置里填密钥"
        - ``undecryptable``：存过但解不开（根密钥变了，通常是数据目录被换过）
          → "密钥还在，但根密钥不匹配"，让用户去查 root.key 而不是重复填密钥

        不区分的话，第二种会显示成"还没有保存密钥"——用户明明存过，
        会被这句话带偏，反复重填也修不好。
        """
        row = (
            await self._session.execute(
                select(ProviderCredential).where(
                    ProviderCredential.provider_id == provider_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return "missing"
        try:
            self._box.decrypt(row.ciphertext)
        except Exception:  # noqa: BLE001 - 根密钥变化后旧密文不可解
            return "undecryptable"
        return "ok"

    async def secret_status(self, provider_id: str) -> dict[str, object]:
        row = (
            await self._session.execute(
                select(ProviderCredential).where(ProviderCredential.provider_id == provider_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return {"has_secret": False, "last4": "", "fingerprint": ""}
        return {"has_secret": True, "last4": row.last4, "fingerprint": row.fingerprint}

    async def _sync_secret_flags(self, provider_id: str, *, has_secret: bool, last4: str) -> None:
        """把密钥状态同步进当前草稿与已发布修订的 provider 描述（不含密文）。

        必须**深拷贝 + 重建内层结构**，浅拷贝在这里是静默失效的：
        ``ConfigRevision.payload`` 是普通 ``JSON`` 列，**没有** ``MutableDict``，
        SQLAlchemy 判"变没变"看的是"有没有被赋新对象 + 内容是否相等"。
        ``dict(row.payload)`` 只换了外层 dict，``providers`` 列表里的内层
        ``dict`` 还是原来那些对象；原地改它们之后，新赋的 ``payload`` 与
        原值内容"相等" → **不产生 UPDATE**。

        后果全是静默的：``has_secret`` / ``secret_last4`` 永远停在旧值，
        ``validate_payload`` 的"还没保存密钥"warning 永远为真，前端"已配置/
        未配置"状态错误，而且 ``_bump_for_secret_change`` deepcopy 的是这份
        没同步进去的 payload——密钥变更造出的新已发布修订里标记同样是错的
        （而其他副本与 worker 读的恰恰是那一版）。

        所以这里用 ``copy.deepcopy`` 拿到全新的内层 dict，并给 ``row.payload``
        赋一个**全新的顶层对象**。
        """
        rows = list(
            (await self._session.execute(select(ConfigRevision))).scalars()
        )
        for row in rows:
            payload = copy.deepcopy(dict(row.payload or {}))
            providers = payload.get("providers") or []
            changed = False
            for entry in providers:
                if entry.get("provider_id") == provider_id:
                    entry["has_secret"] = has_secret
                    entry["secret_last4"] = last4
                    changed = True
            if changed:
                payload["providers"] = providers
                row.payload = payload
        await self._session.flush()


__all__ = [
    "ConfigService",
    "DraftResult",
    "KNOWN_PROFILES",
    "PROFILE_DEGRADATIONS",
    "RESERVED_PROFILES",
    "ValidationIssue",
    "effective_from_payload",
    "payload_from_effective",
]


# ------------------------------------------------------- 修订 payload ⇄ 快照
#
# 这一对函数是**唯一**的翻译层。从前 `effective()` 里手写一份解析、
# `save_draft()` 里手写一份反向拼装，两边靠人肉对齐：加一个字段（这次是
# `slots` 与 `mode`）就要记得两边都改，漏改的那一边不会报错，只会静默
# 丢字段——用户保存一次，槽位就没了。


def payload_from_effective(config: EffectiveConfig) -> dict:
    """生效快照 → 可写进修订的 payload。草稿以它为底。

    **必须双向完备**：`effective_from_payload(payload_from_effective(x))`
    要能还原 ``x``。少写一个键的代价不是报错，而是"用户只改了模型清单，
    顺手把槽位清空了"——这种 bug 没有异常可看，只能靠这条不变式守住。
    """
    slots = dict(config.slots)
    return {
        "providers": [
            {
                "provider_id": p.provider_id,
                "kind": p.kind,
                "base_url": p.base_url,
                "has_secret": p.has_secret,
                "secret_last4": p.secret_last4,
                "enabled": p.enabled,
                "extra": dict(p.extra),
                "models": [m.to_dict() for m in p.models],
                "timeout_seconds": p.timeout_seconds,
                "max_output_tokens": p.max_output_tokens,
                "daily_token_limit": p.daily_token_limit,
            }
            for p in config.providers.values()
        ],
        "profiles": [
            {
                "profile": b.profile,
                "provider_id": b.provider_id,
                "model_id": b.model_id,
                "fallback_provider_id": b.fallback_provider_id,
                "fallback_model_id": b.fallback_model_id,
                "enabled": b.enabled,
                "mode": b.mode.value,
            }
            for b in config.profiles.values()
        ],
        # 只写已配置的槽位：空槽位写进 payload 只会让修订历史里多四条噪音，
        # 而且"删掉一条槽位"和"把它置空"会变成两种状态。
        "slots": [
            {
                "slot": key.value,
                "provider_id": slots[key.value].provider_id,
                "model_id": slots[key.value].model_id,
            }
            for key in SLOT_ORDER
            if key.value in slots and slots[key.value].configured
        ],
        "test_mode": config.test_mode,
        "log_access_denials": config.log_access_denials,
        "persona_id": config.persona_id,
        "memory": _policy_to_dict(config.memory),
        "life": _life_to_dict(config.life),
        "dialogue": _dialogue_to_dict(config.dialogue),
    }


def _apply_patch(
    payload: dict,
    *,
    providers: list[dict] | None = None,
    profiles: list[dict] | None = None,
    slots: list[dict] | None = None,
    memory: dict | None = None,
    life: dict | None = None,
    dialogue: dict | None = None,
    test_mode: bool | None = None,
    log_access_denials: bool | None = None,
) -> None:
    """把一次改动合进 payload。**原地改**，调用方持有同一个 dict。

    ``save_draft`` 与 ``resolve_preview`` 共用它——预览和保存要是各写一份
    合并逻辑，迟早分叉，于是预览说"会生效"、保存后却是另一回事。
    """
    if memory is not None:
        payload["memory"] = _merge_memory(payload["memory"], memory)

    if life is not None:
        payload["life"] = _merge_fields(payload["life"], life, _LIFE_BOUNDS)

    if dialogue is not None:
        payload["dialogue"] = _merge_dialogue(payload["dialogue"], dialogue)

    if providers is not None:
        merged: dict[str, dict] = {p["provider_id"]: p for p in payload["providers"]}
        for raw in providers:
            pid = raw.get("provider_id")
            if not pid:
                continue
            existing = merged.get(pid, {})
            # 模型清单：**没传就是"没改"**，不能把旧清单清空。
            # 旧版控制台 / 第三方客户端保存供应商时不会带 models，
            # 若按 pydantic 默认值 [] 处理，一次无关的保存就会把目录清掉。
            models = (
                _clean_models(raw["models"])
                if "models" in raw
                else list(existing.get("models", []))
            )
            # 密钥字段不由前端传入；保留库中已知状态
            merged[pid] = {
                "provider_id": pid,
                "kind": raw.get("kind", existing.get("kind", "fake")),
                "base_url": raw.get("base_url", existing.get("base_url")),
                "has_secret": existing.get("has_secret", False),
                "secret_last4": existing.get("secret_last4", ""),
                "enabled": bool(raw.get("enabled", existing.get("enabled", True))),
                "extra": dict(raw.get("extra", existing.get("extra", {}))),
                "models": models,
                "timeout_seconds": float(
                    raw.get("timeout_seconds", existing.get("timeout_seconds", 60.0))
                ),
                "max_output_tokens": raw.get(
                    "max_output_tokens", existing.get("max_output_tokens")
                ),
                "daily_token_limit": raw.get(
                    "daily_token_limit", existing.get("daily_token_limit")
                ),
            }
        payload["providers"] = list(merged.values())

    if profiles is not None:
        merged_profiles: dict[str, dict] = {p["profile"]: p for p in payload["profiles"]}
        for raw in profiles:
            prof = raw.get("profile")
            if not prof:
                continue
            existing = merged_profiles.get(prof, {"profile": prof})
            mode = raw.get("mode", existing.get("mode", BindingMode.explicit.value))
            merged_profiles[prof] = {
                "profile": prof,
                # `mode=slot` 时这两项**没有意义**，一律清成空串：留着旧值
                # 会让"跟随槽位"看起来像是还在用那个模型（修订历史里也是）。
                "provider_id": (
                    ""
                    if mode == BindingMode.slot.value
                    else raw.get("provider_id", existing.get("provider_id", ""))
                ),
                "model_id": (
                    ""
                    if mode == BindingMode.slot.value
                    else raw.get("model_id", existing.get("model_id", ""))
                ),
                "fallback_provider_id": raw.get(
                    "fallback_provider_id", existing.get("fallback_provider_id")
                ),
                "fallback_model_id": raw.get(
                    "fallback_model_id", existing.get("fallback_model_id")
                ),
                "enabled": bool(raw.get("enabled", existing.get("enabled", True))),
                "mode": mode,
            }
        payload["profiles"] = list(merged_profiles.values())

    if slots is not None:
        merged_slots: dict[str, dict] = {
            str(entry.get("slot")): dict(entry) for entry in payload.get("slots") or []
        }
        for raw in slots:
            key = str(raw.get("slot") or "").strip()
            if not key:
                continue
            provider_id = str(raw.get("provider_id") or "").strip()
            model_id = str(raw.get("model_id") or "").strip()
            if not provider_id and not model_id:
                # 两项都空 = 把这个槽位清回"没配"。
                merged_slots.pop(key, None)
                continue
            merged_slots[key] = {
                "slot": key,
                "provider_id": provider_id,
                "model_id": model_id,
            }
        # 按固定顺序写回：修订历史里同一份配置不该因为 dict 顺序不同
        # 显示成两次不一样的改动。
        payload["slots"] = [
            merged_slots[key.value] for key in SLOT_ORDER if key.value in merged_slots
        ] + [
            entry
            for key, entry in merged_slots.items()
            if key not in {slot.value for slot in SLOT_ORDER}
        ]

    if test_mode is not None:
        payload["test_mode"] = test_mode

    if log_access_denials is not None:
        payload["log_access_denials"] = bool(log_access_denials)


def _binding_mode(raw: dict) -> BindingMode:
    """修订里的一条 profile 是显式绑定还是跟随槽位。

    缺键 → ``explicit``：这个键是跟着默认槽位一起加的，之前的每条绑定
    都是显式选择，所以"没写"只能按显式读——按 slot 读会把所有老部署的
    绑定一次性作废。
    """
    raw_mode = str(raw.get("mode") or "").strip()
    try:
        return BindingMode(raw_mode)
    except ValueError:
        return BindingMode.explicit


def _slot_entries(raw: object) -> dict[str, ModelSlot]:
    """修订里的 ``slots`` → 快照里的槽位表。脏数据一律忽略，不炸。"""
    slots: dict[str, ModelSlot] = {}
    if not isinstance(raw, list):
        return slots
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("slot") or "").strip()
        if key not in {slot.value for slot in SLOT_ORDER}:
            continue
        provider_id = str(item.get("provider_id") or "").strip()
        model_id = str(item.get("model_id") or "").strip()
        if not provider_id or not model_id:
            continue
        slots[key] = ModelSlot(slot=key, provider_id=provider_id, model_id=model_id)
    return slots


def effective_from_payload(
    payload: dict,
    *,
    revision: int,
    persona_default: str,
    log_denials_default: bool,
) -> EffectiveConfig:
    """修订 payload → 生效快照。运行期与设置页预览共用这一个解析。"""
    providers: dict[str, ProviderConfig] = {}
    for raw in list(payload.get("providers") or []):
        pid = raw.get("provider_id")
        if not pid:
            continue
        providers[pid] = ProviderConfig(
            provider_id=pid,
            kind=raw.get("kind", "fake"),
            base_url=raw.get("base_url"),
            has_secret=bool(raw.get("has_secret")),
            secret_last4=raw.get("secret_last4", ""),
            enabled=bool(raw.get("enabled", True)),
            extra=dict(raw.get("extra") or {}),
            models=_provider_models(raw.get("models")),
            timeout_seconds=float(raw.get("timeout_seconds", 60.0)),
            max_output_tokens=raw.get("max_output_tokens"),
            daily_token_limit=raw.get("daily_token_limit"),
        )
    profiles: dict[str, ProfileBinding] = {}
    for raw in list(payload.get("profiles") or []):
        prof = raw.get("profile")
        if not prof:
            continue
        mode = _binding_mode(raw)
        profiles[prof] = ProfileBinding(
            profile=prof,
            provider_id="" if mode is BindingMode.slot else raw.get("provider_id", ""),
            model_id="" if mode is BindingMode.slot else raw.get("model_id", ""),
            fallback_provider_id=raw.get("fallback_provider_id"),
            fallback_model_id=raw.get("fallback_model_id"),
            enabled=bool(raw.get("enabled", True)),
            mode=mode,
        )
    return EffectiveConfig(
        revision=revision,
        providers=providers,
        profiles=profiles,
        slots=_slot_entries(payload.get("slots")),
        test_mode=bool(payload.get("test_mode", False)),
        # 旧修订没有这个键：退回环境变量给的默认值（默认 True），
        # 而不是把"没配过"当成"关掉"。
        log_access_denials=bool(payload.get("log_access_denials", log_denials_default)),
        persona_id=payload.get("persona_id", persona_default),
        memory=_memory_policy(payload.get("memory")),
        life=_life_policy(payload.get("life")),
        dialogue=_dialogue_policy(payload.get("dialogue")),
    )


# ------------------------------------------------------------------ 记忆策略

# 读的时候一律夹到合法区间：库里出现脏值（或旧修订没有这个键）时，
# 记忆检索不能因此崩掉或退化成"什么都召回"。
_MEMORY_BOUNDS: dict[str, tuple[float, float]] = {
    "relative_score_floor": (0.0, 1.0),
    "rerank_top_n": (1, 100),
    "recency_half_life_days": (1.0, 3650.0),
    "max_retention_days": (1, 3650),
}


# 对话节奏的取值范围。同样单独一张表。
_DIALOGUE_BOUNDS: dict[str, tuple[float, float]] = {
    "group_turn_budget": (1, 20),
    "proactive_daily_limit": (0, 20),
    "quiet_start_minute": (0, 1439),
    "quiet_end_minute": (0, 1439),
    # 自主度开关是布尔，但现有钳制机制是数值区间。用 0/1 表示，
    # 避免为两个布尔再写一套读写分支。
    "private_may_stay_silent": (0, 1),
    "group_mention_requires_reply": (0, 1),
    "rest_may_skip": (0, 1),
    "group_attention_deferred": (0, 1),
    "proactive_delivery_enabled": (0, 1),
    # 群聊「决定回了」是否真的发出去。同样是 0/1 的布尔。
    "group_reply_delivery_enabled": (0, 1),
    # 同轮原生工具调用的总开关（默认关，见 DialoguePolicy 的 docstring）。
    "tools_enabled": (0, 1),
}

# 对话策略里的**非数值**键：枚举/字符串，没有 `(low, high)` 可钳制，
# 所以进不了 `_DIALOGUE_BOUNDS`，但必须能被补丁覆盖。
#
# 单独列一张表而不是把它们混进 `_DIALOGUE_BOUNDS`：那个表的所有成员都会被
# `float()` + `min/max` 处理，混进一个枚举值会在钳制时炸掉。
_DIALOGUE_ENUM_KEYS: frozenset[str] = frozenset({"voice_willingness"})

# 音色的承载位置：`dialogue.voice`。**不是** `dialogue.voice_kind` 那种平铺。
#
# 平铺会让 `_dialogue_to_dict` 的键集合不再是"一个字段一个键"（`voice` 这一个
# 字段对应八个键），`test_dialogue_serde_covers_every_field` 那条守卫就失效了。
# 而且"这一整套音色"在语义上是一个整体：`kind` 一换，另外几个参数就属于另一家。
_DIALOGUE_OBJECT_KEYS: frozenset[str] = frozenset({"voice"})

# 音色里可以钳制的参数。**只有 MiniMax 用得上**，但这不妨碍统一归一化：
# 别的家读进这些值也不会发出去（运行时按值对象的形状构造）。
_VOICE_BOUNDS: dict[str, tuple[float, float]] = {
    "speed": (0.5, 2.0),
    "vol": (0.0, 10.0),
    "pitch": (-12, 12),
}

# 音色里**原样保留的字符串键**：id 与枚举值不做范围钳制（钳不了），
# 由 `validate_payload` 判合法性与配套关系。
_VOICE_TEXT_KEYS: frozenset[str] = frozenset(
    {"kind", "voice_id", "reference_asset_id", "emotion"}
)

# 参考素材 id 的形态：`media_assets.digest` 是 sha256 十六进制。
# 在这里写死长度是有意的——把用户填的文件路径/URL 当 asset id 存进去，
# 运行时才会发现"找不到素材"，而那已经是一次降级了。
_ASSET_ID_HEX_LEN = 64

# 生活节奏的取值范围。与记忆策略分开一张表，避免两类的键名撞车。
_LIFE_BOUNDS: dict[str, tuple[float, float]] = {
    "scene_ttl_minutes": (1, 1440),
    "energy_decay_per_hour": (0.0, 100.0),
    "social_load_per_turn": (0.0, 100.0),
    "social_load_decay_per_hour": (0.0, 100.0),
    "max_live_plans": (1, 20),
}


def _clean_models(raw: object) -> list[dict[str, object]]:
    """把模型清单归一化成可以写进修订 payload 的形状。

    规则（都是为了让"同一份清单"在前后端是同一个东西）：

    - 兼容旧客户端直接传 ``["m1", "m2"]``；
    - ``model_id`` / ``note`` 去首尾空白，note 最多 200 字；
    - 同一个 id 只留最后一条——重名不会变成两行幽灵条目；
    - ``enabled`` 只认显式的 ``false``，其余都当启用；
    - 空 id **保留**，交给 ``validate_payload`` 报错，不静默吞掉。
    """
    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, object]] = []
    index: dict[str, int] = {}
    for item in raw:
        if isinstance(item, str):
            item = {"model_id": item}
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("model_id", "") or "").strip()
        entry: dict[str, object] = {
            "model_id": model_id,
            "note": str(item.get("note", "") or "").strip()[:200],
            "enabled": item.get("enabled", True) is not False,
        }
        if model_id and model_id in index:
            cleaned[index[model_id]] = entry
        elif model_id:
            index[model_id] = len(cleaned)
            cleaned.append(entry)
        else:
            cleaned.append(entry)
    return cleaned


def _provider_models(raw: object) -> tuple[ProviderModel, ...]:
    """修订 payload → 生效快照里的模型清单（空 id 不进入运行期快照）。"""
    return tuple(
        ProviderModel(
            model_id=str(item["model_id"]),
            note=str(item.get("note", "")),
            enabled=bool(item.get("enabled", True)),
        )
        for item in _clean_models(raw)
        if item["model_id"]
    )


def _clamp_in(
    bounds: dict[str, tuple[float, float]], name: str, value: object, default: float
) -> float:
    low, high = bounds[name]
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return min(max(num, low), high)


def _clamp(name: str, value: object, default: float) -> float:
    return _clamp_in(_MEMORY_BOUNDS, name, value, default)


def _memory_policy(raw: object) -> MemoryPolicy:
    """从修订 payload 读出记忆策略。缺键 → 默认值（行为与抽取前一致）。"""
    data = raw if isinstance(raw, dict) else {}
    defaults = MemoryPolicy()
    return MemoryPolicy(
        relative_score_floor=_clamp(
            "relative_score_floor",
            data.get("relative_score_floor"),
            defaults.relative_score_floor,
        ),
        rerank_top_n=int(
            _clamp("rerank_top_n", data.get("rerank_top_n"), defaults.rerank_top_n)
        ),
        recency_half_life_days=_clamp(
            "recency_half_life_days",
            data.get("recency_half_life_days"),
            defaults.recency_half_life_days,
        ),
        max_retention_days=int(
            _clamp(
                "max_retention_days",
                data.get("max_retention_days"),
                defaults.max_retention_days,
            )
        ),
    )


def _policy_to_dict(policy: MemoryPolicy) -> dict[str, float | int]:
    return {
        "relative_score_floor": policy.relative_score_floor,
        "rerank_top_n": policy.rerank_top_n,
        "recency_half_life_days": policy.recency_half_life_days,
        "max_retention_days": policy.max_retention_days,
    }


def _merge_memory(current: object, patch: dict) -> dict:
    return _merge_fields(current, patch, _MEMORY_BOUNDS)


async def published_memory_policy(session: AsyncSession) -> MemoryPolicy:
    """已发布修订里的 ``memory`` 段（没有已发布修订 → 代码默认值）。

    给**没有 ``EffectiveConfig`` 可用**的进程内路径用：worker 的
    ``memory.extract`` / ``memory.archive`` 每个 outbox 事件都是一次独立事务，
    没有一个"轮次级快照"可以继承，所以它们在处理事件时**现读一次**最新
    已发布修订——发布后下一个事件就生效，不需要重启、也没有进程内缓存
    需要失效（每个进程各自查库，见 ``docs/SETTINGS-TAXONOMY.md`` ④）。

    会话内核（``ContextCompiler``）**不用**这个函数：它从本轮
    ``EffectiveConfig.memory`` 拿快照，保证一轮之内策略不漂移。
    """
    revision = (
        await session.execute(
            select(ConfigRevision)
            .where(ConfigRevision.state == "published")
            .order_by(ConfigRevision.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if revision is None:
        return MemoryPolicy()
    return _memory_policy(dict(revision.payload or {}).get("memory"))


# ------------------------------------------------------------------ 生活策略


def _life_policy(raw: object) -> LifePolicy:
    """从修订 payload 读出生活节奏。缺键 → 默认值（行为与抽取前一致）。"""
    data = raw if isinstance(raw, dict) else {}
    d = LifePolicy()
    return LifePolicy(
        scene_ttl_minutes=int(
            _clamp_in(_LIFE_BOUNDS, "scene_ttl_minutes", data.get("scene_ttl_minutes"), d.scene_ttl_minutes)
        ),
        energy_decay_per_hour=_clamp_in(
            _LIFE_BOUNDS, "energy_decay_per_hour", data.get("energy_decay_per_hour"), d.energy_decay_per_hour
        ),
        social_load_per_turn=_clamp_in(
            _LIFE_BOUNDS, "social_load_per_turn", data.get("social_load_per_turn"), d.social_load_per_turn
        ),
        social_load_decay_per_hour=_clamp_in(
            _LIFE_BOUNDS,
            "social_load_decay_per_hour",
            data.get("social_load_decay_per_hour"),
            d.social_load_decay_per_hour,
        ),
        max_live_plans=int(
            _clamp_in(_LIFE_BOUNDS, "max_live_plans", data.get("max_live_plans"), d.max_live_plans)
        ),
    )


def _life_to_dict(policy: LifePolicy) -> dict[str, float | int]:
    return {
        "scene_ttl_minutes": policy.scene_ttl_minutes,
        "energy_decay_per_hour": policy.energy_decay_per_hour,
        "social_load_per_turn": policy.social_load_per_turn,
        "social_load_decay_per_hour": policy.social_load_decay_per_hour,
        "max_live_plans": policy.max_live_plans,
    }


def _merge_fields(
    current: object,
    patch: dict,
    bounds: dict[str, tuple[float, float]],
    extra_keys: frozenset[str] = frozenset(),
) -> dict:
    """只覆盖显式给出的字段。不传的字段保持原值——不是重置成默认。

    ``extra_keys`` 是**不属于数值区间表但可被补丁覆盖**的键（例如枚举/字符串
    字段，它们没有 `(low, high)` 可钳制，所以进不了 `bounds`）。

    为什么必须有这个参数：原来只遍历 ``bounds`` 的键，而 `_DIALOGUE_BOUNDS`
    里**只有数值字段**。于是字符串/枚举字段即使出现在补丁里也会被静默丢掉——
    症状是"改了下拉框、保存成功、刷新之后还是旧值"，而权重一类数值字段
    却生效了，看起来像"只有一部分设置不生效"。
    """
    base = current if isinstance(current, dict) else {}
    merged = dict(base)
    for key in (*bounds, *extra_keys):
        if key in patch and patch[key] is not None:
            merged[key] = patch[key]
    return merged


def _merge_dialogue(current: object, patch: dict) -> dict:
    """对话策略的补丁：数值/枚举逐键覆盖，`voice` **交给 `_merge_voice`**。

    `voice` 不能走 `_merge_fields`：它的值是一个 dict，而 `_merge_fields`
    对 ``None`` 的处理是"当成没改"——那样"取消音色"（显式传 ``None``）
    永远存不下去。音色要能**真的删掉**，所以它有自己的一份合并。
    """
    merged = _merge_fields(current, patch, _DIALOGUE_BOUNDS, _DIALOGUE_ENUM_KEYS)
    if "voice" in patch:
        base = current if isinstance(current, dict) else {}
        merged["voice"] = _merge_voice(base.get("voice"), patch["voice"])
    return merged


# ------------------------------------------------------------------ 对话策略


def _dialogue_policy(raw: object) -> DialoguePolicy:
    """从修订 payload 读出对话节奏与主动门槛。缺键 → 默认值。"""
    data = raw if isinstance(raw, dict) else {}
    d = DialoguePolicy()
    return DialoguePolicy(
        group_turn_budget=int(
            _clamp_in(
                _DIALOGUE_BOUNDS,
                "group_turn_budget",
                data.get("group_turn_budget"),
                d.group_turn_budget,
            )
        ),
        proactive_daily_limit=int(
            _clamp_in(
                _DIALOGUE_BOUNDS,
                "proactive_daily_limit",
                data.get("proactive_daily_limit"),
                d.proactive_daily_limit,
            )
        ),
        quiet_start_minute=int(
            _clamp_in(
                _DIALOGUE_BOUNDS,
                "quiet_start_minute",
                data.get("quiet_start_minute"),
                d.quiet_start_minute,
            )
        ),
        quiet_end_minute=int(
            _clamp_in(
                _DIALOGUE_BOUNDS,
                "quiet_end_minute",
                data.get("quiet_end_minute"),
                d.quiet_end_minute,
            )
        ),
        private_may_stay_silent=bool(
            int(
                _clamp_in(
                    _DIALOGUE_BOUNDS,
                    "private_may_stay_silent",
                    data.get("private_may_stay_silent"),
                    int(d.private_may_stay_silent),
                )
            )
        ),
        group_mention_requires_reply=bool(
            int(
                _clamp_in(
                    _DIALOGUE_BOUNDS,
                    "group_mention_requires_reply",
                    data.get("group_mention_requires_reply"),
                    int(d.group_mention_requires_reply),
                )
            )
        ),
        rest_may_skip=bool(
            int(
                _clamp_in(
                    _DIALOGUE_BOUNDS,
                    "rest_may_skip",
                    data.get("rest_may_skip"),
                    int(d.rest_may_skip),
                )
            )
        ),
        group_attention_deferred=bool(
            int(
                _clamp_in(
                    _DIALOGUE_BOUNDS,
                    "group_attention_deferred",
                    data.get("group_attention_deferred"),
                    int(d.group_attention_deferred),
                )
            )
        ),
        proactive_delivery_enabled=bool(
            int(
                _clamp_in(
                    _DIALOGUE_BOUNDS,
                    "proactive_delivery_enabled",
                    data.get("proactive_delivery_enabled"),
                    int(d.proactive_delivery_enabled),
                )
            )
        ),
        group_reply_delivery_enabled=bool(
            int(
                _clamp_in(
                    _DIALOGUE_BOUNDS,
                    "group_reply_delivery_enabled",
                    data.get("group_reply_delivery_enabled"),
                    int(d.group_reply_delivery_enabled),
                )
            )
        ),
        tools_enabled=bool(
            int(
                _clamp_in(
                    _DIALOGUE_BOUNDS,
                    "tools_enabled",
                    data.get("tools_enabled"),
                    int(d.tools_enabled),
                )
            )
        ),
        voice_willingness=_voice_willingness(data.get("voice_willingness"), d.voice_willingness),
        voice=_voice_setting(data.get("voice")),
    )


_VOICE_WILLINGNESS_VALUES: frozenset[str] = frozenset({"reserved", "natural", "expressive"})


def _voice_willingness(raw: object, default: VoiceWillingness) -> VoiceWillingness:
    """读语音意愿档位。**非法值退回默认，不抛异常。**

    非法/缺失时退回 ``natural``（§17.4：「三档配置缺省读为 natural」）。
    与 `_clamp_in` 同一取舍：配置里的脏值不该让整个设置页保存失败
    （那样用户会卡在"怎么都存不上"，还看不出是哪一项坏了）。

    §17.4 还要求旧 numeric/off/always 草稿**不继续作为行为源**：那些值不在
    这里的合法集合里，会自然落到 ``natural``——正是裁决要的结果，不需要
    额外写一个"识别旧值再转换"的分支。
    """
    if isinstance(raw, str) and raw in _VOICE_WILLINGNESS_VALUES:
        return cast("VoiceWillingness", raw)
    return default


_DialogueValue = int | bool | float | str


# ------------------------------------------------------------------ 音色配置
#
# 音色是 `dialogue.voice` 里的**一个嵌套 dict**。读/写/补丁三处都要覆盖它：
#
# - 读：`_dialogue_policy` → `_voice_setting`
# - 写：`_dialogue_to_dict` → `_voice_to_dict`
# - 补丁：`_apply_patch` → `_merge_voice`（整段替换，不按键合并）
#
# 为什么补丁是**整段替换**而不是像别的字段那样逐键覆盖：音色参数是一个整体，
# `kind` 从 minimax 换成 mimo 时，旧的 `voice_id` 就不再属于这一套。逐键合并
# 会把它们留在 dict 里，运行时虽然按 kind 只取该取的字段，但设置页读回来
# 会显示"你还填着一个 MiniMax 的 id"，让人以为自己保存错了东西。


def _voice_text(raw: object) -> str | None:
    """把字符串类音色字段归一化：去空白、空串 → ``None``。

    空串与 `None` 必须合成同一个值：否则"清空输入框"会存成 ``""``，而运行时
    判 `if not value` 与 `if value is None` 的写法各有一套——两套迟早不一致。
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


def _voice_setting(raw: object) -> VoiceSetting:
    """从修订 payload 读出音色。缺键 / 脏值 → 默认值。**不抛异常。**

    与 `_voice_willingness` 同一取舍：配置里的脏值不该让整个设置页读崩。
    但这里**只做归一化**，合法性（kind 是否已知、id 与 kind 是否配套）交给
    `validate_payload` 报错——读的时候静默退回默认，会让"存了却读不回来"
    变成两处不一致的静默行为，而不是一条用户能看到的校验错误。
    """
    data = raw if isinstance(raw, dict) else {}
    defaults = VoiceSetting()

    def _num(key: str, default: float) -> float:
        value = data.get(key)
        if value is None:
            return default
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        low, high = _VOICE_BOUNDS[key]
        return min(max(number, low), high)

    pitch_raw = data.get("pitch")
    try:
        pitch = int(pitch_raw) if pitch_raw is not None else defaults.pitch
    except (TypeError, ValueError):
        pitch = defaults.pitch
    low, high = _VOICE_BOUNDS["pitch"]
    pitch = int(min(max(pitch, low), high))

    kind = _voice_text(data.get("kind"))
    return VoiceSetting(
        # 未知 kind 原样保留（`cast`）：读进来时"说它是什么"，判定交给校验。
        # 退回 None 会让"填错了一家名字"看起来像"没填"，用户看不到任何提示。
        kind=cast("VoiceKind", kind) if kind else None,
        voice_id=_voice_text(data.get("voice_id")),
        reference_asset_id=_voice_text(data.get("reference_asset_id")),
        speed=_num("speed", defaults.speed),
        vol=_num("vol", defaults.vol),
        pitch=pitch,
        emotion=_voice_text(data.get("emotion")),
    )


def _voice_to_dict(voice: VoiceSetting) -> dict[str, object]:
    """`VoiceSetting` → 可入库的 dict。与 `_dialogue_to_dict` 同一条纪律：
    每个字段都要在这里列一次，漏了就会"存得住但读不回"。"""
    return {
        "kind": voice.kind,
        "voice_id": voice.voice_id,
        "reference_asset_id": voice.reference_asset_id,
        "speed": voice.speed,
        "vol": voice.vol,
        "pitch": voice.pitch,
        "emotion": voice.emotion,
    }


def _merge_voice(current: object, patch: object) -> dict[str, object]:
    """音色补丁：**整段替换**，但保留未出现在补丁里的键。

    与 `_merge_fields` 的差别只在"没传的键"：这里传了 ``{"kind": None}`` 就是
    "清空音色"，与"没传 kind"不同。`_merge_fields` 把 ``None`` 当成"没改"，
    那样"取消音色"就永远存不下去——用户选了"不使用"，刷新后还是旧音色。
    """
    base = dict(current) if isinstance(current, dict) else {}
    if not isinstance(patch, dict):
        return base
    merged = dict(base)
    for key in (*_VOICE_BOUNDS, *_VOICE_TEXT_KEYS):
        if key in patch:
            merged[key] = patch[key]
    # 允许一次补丁里带任意组合；归一化交给 `_voice_setting`（读路径只有一处）。
    return merged


def _dialogue_to_dict(policy: DialoguePolicy) -> dict[str, object]:
    """`DialoguePolicy` → 可入库的 dict。

    **每个字段都要在这里列一次。** 新增字段忘了补，会在"存得住但读不回"
    之间静默丢失——契约加了、UI 也显示了、用户选了却没生效。
    `tests/unit/test_speech_media_contracts.py` 有一条断言盯着这张键集合
    等于 `dataclasses.fields(DialoguePolicy)`，漏字段会立刻红。
    """
    return {
        "group_turn_budget": policy.group_turn_budget,
        "proactive_daily_limit": policy.proactive_daily_limit,
        "quiet_start_minute": policy.quiet_start_minute,
        "quiet_end_minute": policy.quiet_end_minute,
        "private_may_stay_silent": policy.private_may_stay_silent,
        "group_mention_requires_reply": policy.group_mention_requires_reply,
        "rest_may_skip": policy.rest_may_skip,
        "group_attention_deferred": policy.group_attention_deferred,
        "proactive_delivery_enabled": policy.proactive_delivery_enabled,
        "group_reply_delivery_enabled": policy.group_reply_delivery_enabled,
        "tools_enabled": policy.tools_enabled,
        "voice_willingness": policy.voice_willingness,
        "voice": _voice_to_dict(policy.voice),
    }
