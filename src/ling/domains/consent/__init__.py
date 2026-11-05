"""Consent 域：授权范围、有效期、撤权与受众规则（唯一写入方）。"""

from ling.domains.consent.service import KNOWN_SCOPES, ConsentService, ConsentView

__all__ = ["KNOWN_SCOPES", "ConsentService", "ConsentView"]
