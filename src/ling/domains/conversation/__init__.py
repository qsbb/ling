"""Conversation 域：窗口、轮次、消息、短记忆与交接胶囊（唯一写入方）。"""

from ling.domains.conversation.service import ConversationService, MessageView, TurnView

__all__ = ["ConversationService", "MessageView", "TurnView"]
