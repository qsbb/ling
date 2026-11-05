import 'package:flutter/material.dart';

import '../models/contracts.dart';

/// 一条消息。状态必须可见：发送中 / 已完成 / 已停止 / 失败。
class MessageBubble extends StatelessWidget {
  const MessageBubble({super.key, required this.message, this.streaming = false});

  final ChatMessage message;
  final bool streaming;

  @override
  Widget build(BuildContext context) {
    final isUser = !message.isActor;
    final statusLabel = streaming
        ? '生成中'
        : switch (message.state) {
            'failed' => '失败',
            'stopped' => '已停止',
            'streaming' => '生成中',
            _ => '已完成',
          };

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Column(
        crossAxisAlignment: isUser ? CrossAxisAlignment.end : CrossAxisAlignment.start,
        children: [
          if (!isUser)
            const Padding(
              padding: EdgeInsets.only(left: 4, bottom: 4),
              child: Text('ling', style: TextStyle(fontSize: 12, color: Colors.black45)),
            ),
          Container(
            constraints: BoxConstraints(
              maxWidth: MediaQuery.of(context).size.width * 0.82,
            ),
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
            decoration: BoxDecoration(
              color: isUser ? const Color(0xFFE8E2D6) : Colors.white,
              borderRadius: BorderRadius.circular(14),
              border: Border.all(color: Colors.black.withValues(alpha: 0.06)),
            ),
            child: Text(
              message.text.isEmpty && streaming ? '…' : message.text,
              style: const TextStyle(fontSize: 15, height: 1.55),
            ),
          ),
          Padding(
            padding: const EdgeInsets.only(top: 3, left: 4, right: 4),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  statusLabel,
                  style: TextStyle(
                    fontSize: 11,
                    color: message.isFailed ? Colors.red.shade400 : Colors.black38,
                  ),
                ),
                if (message.modelLabel != null) ...[
                  const SizedBox(width: 6),
                  Text(
                    message.modelLabel!,
                    style: const TextStyle(fontSize: 11, color: Colors.black26),
                  ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}
