"""去掉她回复里给自己加的名字前缀。

## 为什么需要这个

人格文档里有一段「示例语气」，是**带名字的对话记录**：

    对方：在吗？
    她：对方，紧急求救！
    她：小吃街的婆婆你记得吧……

模型会把这种格式当成模板照着学——实测她开始在每条消息前加「她：」，
而代码里没有任何地方给她加前缀。这是**输出格式的残留物**，不是她的表达。

剥掉前缀是格式清理，不是替她决定说什么：正文一个字都不动。
"""

from __future__ import annotations

import re


def strip_self_prefix(text: str, display_name: str) -> tuple[str, bool]:
    """去掉每行开头的「<自己的名字>：」。返回 (清理后文本, 是否改动过)。

    只认**自己的名字**做前缀。别人名字开头的行保持不动——那可能是她
    在引用对方说的话（"对方：你说的对"），属于正常表达。
    """
    name = (display_name or "").strip()
    if not name or not text:
        return text, False

    pattern = re.compile(rf"^\s*{re.escape(name)}\s*[：:]\s?", re.M)
    cleaned, count = pattern.subn("", text)
    if count == 0:
        return text, False
    # 整行都是前缀时（"她：" 后面没内容）会留下空行，收一下
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, True


# 显示名缓存：每轮都查一次数据库不值得，改名时由路由主动失效。
_NAME_CACHE: dict[str, str] = {}


def invalidate_names() -> None:
    """人格改名后调用，避免继续用旧名字判断前缀。"""
    _NAME_CACHE.clear()


async def display_name_for(persona_id: str) -> str:
    """取她的显示名（带缓存）。取不到就返回空串——此时不剥任何前缀。"""
    if persona_id in _NAME_CACHE:
        return _NAME_CACHE[persona_id]
    try:
        from ling.domains.persona.service import PersonaService
        from ling.infra.db import session_scope

        async with session_scope() as session:
            persona = await PersonaService(session, persona_id).get()
            _NAME_CACHE[persona_id] = persona.display_name or ""
    except Exception:  # noqa: BLE001 - 取不到名字不影响出话
        return ""
    return _NAME_CACHE[persona_id]


__all__ = ["display_name_for", "invalidate_names", "strip_self_prefix"]
