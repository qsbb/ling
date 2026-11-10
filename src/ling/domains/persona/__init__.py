"""Persona 域：角色稳定设定与人格版本（唯一写入方）。

这里**没有** `DEFAULT_*` 常量：新装即无人设，不填就什么都不注入。理由与两次
踩过的坑见 `service.py` 文件头。
"""

from ling.domains.persona.service import (
    PersonaService,
    PersonaView,
    persona_is_empty,
    view_is_empty,
)

__all__ = [
    "PersonaService",
    "PersonaView",
    "persona_is_empty",
    "view_is_empty",
]
