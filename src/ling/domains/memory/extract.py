"""断言提炼：模型提炼 + 代码闸门。

## 为什么改掉正则

旧实现对「我叫 / 我喜欢 / 我住在」三个正则句式做匹配。参照
同类产品早期的公开做法，那条路线
的失败是明确的：

> 以前只能靠一张正则表抽取画像，它要求「以我开头 + 命中固定动词」，于是
> 「我一累就咬后槽牙」「他生气时会先沉默三秒」「我妈忌日是每年十一月三号」
> 这类长期细节全部落空，且落空后没有兜底通道。现在由总结模型直接提炼断言，
> 并用代码再做一道闸门。

ling 原先就是那个状态。本模块把提炼交给模型，同时保留正则作为**降级路径**
（模型不可用时不至于什么都记不到）。

## 为什么提炼完还要代码闸门

模型会过度概括。参照稿列的"不允许的写入"六条必须在代码里硬拦，不能靠提示词：

1. 仅凭关键词命中写入
2. 仅凭 Bot 自己生成的情绪句子写入
3. 把一次性寒暄升级为稳定偏好
4. 把未确认的计划写成已完成事实
5. 把群聊中的他人信息归入当前用户档案
6. 把模型的理由/思维链当事实证据

闸门只看结构化字段与原文，**不读模型的 reason 去推断**——沿用参照稿的原则：
"Memory Writer 不读取 reason 来推断 operation"。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# 允许的槽位。与契约 Skill/Assertion 的稳定标识对应，**不含文本**，
# 这样换措辞、换模型都不影响去重（文本作键的老问题已修过一次）。
ALLOWED_SLOTS = {
    "name",
    "likes",
    "dislikes",
    "lives_in",
    "works_at",
    "has_pet",
    "family",
    "birthday",
    "health",
    "habit",
    "ongoing_project",
    "important_date",
    "boundary",
}

# 一次性状态标记：出现这些词说明是"此刻"而非"长期"。
_TRANSIENT_MARKERS = (
    "今天", "今晚", "刚才", "刚刚", "现在", "这会儿", "待会", "等一下",
    "暂时", "一时", "这几天", "最近有点",
)

# 未来计划标记：未兑现的计划不能写成已完成事实。
_FUTURE_MARKERS = ("打算", "准备", "计划", "想要", "可能会", "明天要", "下周要", "回头")

# 这些槽位**不可能**是"计划"：姓名、生日、家人、宠物不会因为
# "我打算明天去苏州"而变成未兑现。整句一律拒绝会让无关事实一起丢掉
# （实测："我叫阿澄，我打算明天去苏州" 连 name 都被拒）。
_NON_PLANNABLE_SLOTS = {"name", "birthday", "family", "has_pet"}

# 分句符：只判断**含该值的那一句**有没有未来标记，
# 而不是整条消息。同一个句子里并列两件事很常见。
_CLAUSE_SPLIT = re.compile(r"[，,。.！!？?；;\n]+")

# 第三人称标记：他人信息不能归入当前用户档案。
# 注意区分两类——
#   * 纯粹的"别人"（朋友/同事/他们）：任何槽位都不该记成用户的事实
#   * 亲属（我妈/我爸）：对 family 槽位**是**用户档案的一部分，
#     但对"我自己的"槽位（宠物/住址/工作）就不该记成用户的
# 原实现只在 cand.value 里查这些词，而用户原话里的"我朋友"根本没被看到，
# 于是"我朋友的猫叫咪咪"会被记成"用户养了咪咪"。
_OTHERS_MARKERS = ("他说", "她说", "他们", "她们", "我朋友", "我同事", "我室友", "我同学")
_KINSHIP_MARKERS = ("我爸", "我妈", "我哥", "我姐", "我弟", "我妹", "我爷", "我奶", "我姥")
# 这些槽位默认指"用户自己的"，亲属语境下不成立
_SELF_POSSESSION_SLOTS = {"has_pet", "lives_in", "works_at"}

# 角色扮演 / 台词标记：引号内的台词不是关于用户的事实。
_ROLEPLAY_PATTERN = re.compile(r"[「『\"'][^」』\"']{4,}[」』\"']")

MAX_CANDIDATES = 6
MAX_VALUE_CHARS = 40


@dataclass
class Candidate:
    """一条候选断言。"""

    slot: str
    value: str
    confidence: float = 0.7
    # 归属：user / third_party / bot。只有 user 能进用户档案。
    attribution: str = "user"
    # 模型给的可审计短理由（**不参与判定**，只留档）
    reason: str = ""

    def text(self) -> str:
        return SLOT_TEMPLATES.get(self.slot, "用户提到 {0}").format(self.value)


SLOT_TEMPLATES = {
    "name": "用户的名字是 {0}",
    "likes": "用户喜欢 {0}",
    "dislikes": "用户不喜欢 {0}",
    "lives_in": "用户住在 {0}",
    "works_at": "用户在 {0} 工作/学习",
    "has_pet": "用户养了 {0}",
    "family": "用户的家人：{0}",
    "birthday": "用户的生日：{0}",
    "health": "用户的健康情况：{0}",
    "habit": "用户的习惯：{0}",
    "ongoing_project": "用户在做：{0}",
    "important_date": "用户的重要日子：{0}",
    "boundary": "用户的边界：不要 {0}",
}


@dataclass
class GateResult:
    accepted: list[Candidate] = field(default_factory=list)
    rejected: list[tuple[Candidate, str]] = field(default_factory=list)


def build_prompt(*, user_text: str, actor_text: str, is_group: bool = False) -> str:
    """构造提炼提示。

    只让模型看**用户说的话**为主：角色自己生成的句子不能反过来变成用户的档案
    （参照稿的"不允许的写入"第 2 条）。
    """
    slots = "、".join(sorted(ALLOWED_SLOTS))
    return (
        "从下面这段对话里，提炼**关于用户本人**的长期事实。\n"
        f"只输出 JSON 数组，每项形如："
        '{"slot": "...", "value": "...", "confidence": 0.0-1.0, '
        '"attribution": "user|third_party", "reason": "简短理由"}\n'
        f"slot 只能取：{slots}\n"
        "规则：\n"
        "- 只写长期有效的事实与偏好；一次性的状态（今天很累）不要写\n"
        "- 未兑现的计划（打算去、准备做）不要写\n"
        "- 别人的信息写 attribution=third_party，不要算成用户本人\n"
        "- 引号里的台词、角色扮演不算事实\n"
        "- 没有可提炼的就输出 []\n"
        "\n"
        f"用户说：{user_text[:1200]}\n"
        f"你说：{actor_text[:600]}\n"
        + ("（这是群聊，只提炼其中明确属于用户本人的内容）\n" if is_group else "")
        + "JSON："
    )


def parse_candidates(raw: str) -> list[Candidate]:
    """解析模型输出。容错：截取第一个 JSON 数组，忽略多余文字。"""
    if not raw:
        return []
    text = raw.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[Candidate] = []
    for item in data[: MAX_CANDIDATES * 2]:
        if not isinstance(item, dict):
            continue
        slot = str(item.get("slot") or "").strip()
        value = str(item.get("value") or "").strip()
        if not slot or not value:
            continue
        try:
            confidence = float(item.get("confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7
        attribution = str(item.get("attribution") or "user").strip()
        out.append(
            Candidate(
                slot=slot,
                value=value[:MAX_VALUE_CHARS],
                confidence=max(0.0, min(1.0, confidence)),
                attribution=attribution if attribution in {"user", "third_party", "bot"} else "user",
                reason=str(item.get("reason") or "")[:120],
            )
        )
    return out


def gate(candidates: list[Candidate], *, user_text: str, is_group: bool = False) -> GateResult:
    """代码闸门：拦住不该进长期记忆的候选。

    每条拒绝都带原因，便于排查"为什么这条没记住"——
    参照稿的分层检索也有"过滤原因可观测"这个要求。
    """
    result = GateResult()
    for cand in candidates:
        reason = _reject_reason(cand, user_text=user_text, is_group=is_group)
        if reason:
            result.rejected.append((cand, reason))
        else:
            result.accepted.append(cand)
        if len(result.accepted) >= MAX_CANDIDATES:
            break
    return result


# 常见同义替换：模型倾向于把口语换成书面语（妈→母亲、爸→父亲）。
# 字符级匹配看不见这种等价，不归一化会把正常提炼误判成臆造。
_SYNONYMS = (
    ("母亲", "妈"), ("父亲", "爸"), ("祖母", "奶奶"), ("祖父", "爷爷"),
    ("外祖母", "姥姥"), ("外祖父", "姥爷"), ("妻子", "老婆"), ("丈夫", "老公"),
    ("儿子", "孩子"), ("女儿", "孩子"), ("兄长", "哥"), ("姐姐", "姐"),
    ("公司", "单位"), ("居住", "住"), ("喜爱", "喜欢"), ("厌恶", "讨厌"),
)


def _normalize(text: str) -> str:
    out = text
    for canonical, colloquial in _SYNONYMS:
        out = out.replace(canonical, colloquial)
    return out


def _value_clause(value: str, text: str) -> str:
    """取包含 ``value`` 的那个分句；找不到就退回整条消息。

    退回整条是保守选择：宁可按严格判断（可能误拒）也不放过
    真正的未兑现计划——但绝大多数情况能靠分句做出正确区分。
    """
    if not value:
        return text
    for clause in _CLAUSE_SPLIT.split(text):
        if value in clause:
            return clause
    return text


def _grounded(value: str, source: str, *, min_ratio: float = 0.6) -> bool:
    """值是否有原话依据（容忍改写，拦住编造）。

    以最长公共子序列占 value 的比例衡量：
    - "母亲忌日每年十一月三号" vs "我妈忌日是每年十一月三号" → 高
    - "写关于机器人的小说" vs "我最近在写一个关于机器人的小说" → 高
    - "阿澄" vs "今天天气不错" → 0
    """
    v = "".join(ch for ch in _normalize(value) if ch.strip())
    src = "".join(ch for ch in _normalize(source) if ch.strip())
    if not v:
        return False
    if v in src:
        return True
    # 经典 LCS 长度 DP（value 很短，成本可忽略）
    prev = [0] * (len(src) + 1)
    for ch in v:
        cur = [0]
        for j, sch in enumerate(src, start=1):
            cur.append(prev[j - 1] + 1 if ch == sch else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1] / len(v) >= min_ratio


def _reject_reason(cand: Candidate, *, user_text: str, is_group: bool) -> str | None:
    if cand.slot not in ALLOWED_SLOTS:
        # 第 1 条：不接受未登记的槽位（等价于"仅凭关键词命中就写"）
        return f"未登记的槽位: {cand.slot}"
    if not cand.value.strip():
        return "空值"
    if cand.attribution == "bot":
        # 第 2 条：Bot 自己生成的内容不能变成用户事实
        return "来源是 Bot 自身输出，不能写成用户事实"
    if cand.attribution == "third_party":
        # 第 5 条：他人信息不进当前用户档案（保留候选但拒绝入库）
        return "归属他人，不写入当前用户档案"
    # 值必须**有据可依**地来自原话：防模型凭空补全。
    # 但不能要求逐字子串——模型会做合理改写（妈→母亲、省略量词），
    # 逐字匹配会把这些正常提炼全判成臆造（实测过，过拒很严重）。
    # 用最长公共子序列占比判断：改写仍高度相关，编造则几乎无重叠。
    if cand.value and not _grounded(cand.value, user_text):
        return "值与原话重叠过低（疑似模型臆造）"
    if _ROLEPLAY_PATTERN.search(cand.value):
        return "引号内台词/角色扮演不算事实"
    # 第 5 条：他人信息不进当前用户档案。
    # 只在**含该值的那一句**里看，避免整句连坐；同时区分他人与亲属。
    _clause = _value_clause(cand.value, user_text)
    if any(m in _clause for m in _OTHERS_MARKERS):
        return "原话是他人情况，不写成用户事实"
    if cand.slot in _SELF_POSSESSION_SLOTS and any(
        m in _clause for m in _KINSHIP_MARKERS
    ):
        return "原话是家人情况，不写成用户自己的事实"

    # 第 4 条：未兑现的计划不写成已完成事实。
    # 但只在**含该值的那一句**里判断——整句判定会误伤并列的无关事实。
    if cand.slot not in {"boundary", "important_date"} | _NON_PLANNABLE_SLOTS and any(
        m in _value_clause(cand.value, user_text) for m in _FUTURE_MARKERS
    ):
        return "原话含未兑现计划，不写成既定事实"
    if any(m in cand.value for m in _TRANSIENT_MARKERS):
        # 第 3 条：一次性寒暄/临时状态不升级为稳定偏好
        return "含临时状态标记，不写成长期偏好"
    # 第 5 条在下面按"值所在的分句"判断（见 clause 变量）；
    # 这里保留对值本身的老规则（值里直接出现"我朋友"之类）。
    if any(m in cand.value for m in _OTHERS_MARKERS):
        return "值里含第三人称指代，归属不明"
    if is_group and cand.slot in {"boundary", "health", "family", "birthday"}:
        # 群聊里这些更敏感：宁可不记，也不要在群里沉淀别人的隐私
        return "群聊场景下的敏感槽位，不写入长期记忆"
    if len(cand.value) > MAX_VALUE_CHARS:
        return "值过长，疑似整句而非事实"
    return None


# ---------------------------------------------------------------- 降级路径

_FALLBACK_PATTERNS = (
    ("name", r"我(?:的名字)?叫\s*([^\s，。,.！!？?]{1,20})"),
    ("likes", r"我(?:很)?喜欢\s*([^\s，。,.！!？?]{1,20})"),
    # 「不吃 / 不碰 / 受不了 / 讨厌」都是长期忌口或偏好，不只是"不喜欢"
    (
        "dislikes",
        r"我(?:不喜欢|不爱吃|不吃|不碰|受不了|讨厌)\s*([^\s，。,.！!？?]{1,20})",
    ),
    ("lives_in", r"我住在\s*([^\s，。,.！!？?]{1,20})"),
    ("works_at", r"我在\s*([^\s，。,.！!？?]{1,20})\s*(?:工作|上班|上学|读书)"),
    # 去掉数量词："养了只猫" → "猫"；"养了两只狗" → "狗"
    ("has_pet", r"我养了?\s*(?:[一二两三几\d]*\s*[只条匹])?\s*([^\s，。,.！!？?]{1,12})"),
    ("ongoing_project", r"我(?:最近|正在)(?:在)?(?:写|做|搞|弄)\s*([^\s，。,.！!？?]{1,24})"),
)


def fallback_candidates(user_text: str) -> list[Candidate]:
    """模型不可用时的降级提炼。

    覆盖面远小于模型，但比"什么都记不到"好，且同样要过闸门。
    """
    out: list[Candidate] = []
    for slot, pattern in _FALLBACK_PATTERNS:
        match = re.search(pattern, user_text)
        if not match:
            continue
        out.append(Candidate(slot=slot, value=match.group(1)[:MAX_VALUE_CHARS], confidence=0.6))
        if len(out) >= MAX_CANDIDATES:
            break
    return out


__all__ = [
    "ALLOWED_SLOTS",
    "Candidate",
    "GateResult",
    "build_prompt",
    "fallback_candidates",
    "gate",
    "parse_candidates",
]
