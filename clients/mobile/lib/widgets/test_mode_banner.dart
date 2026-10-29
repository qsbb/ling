import 'package:flutter/material.dart';

/// 测试模式横幅。
///
/// 必须显著：fake provider 的回复不是模型能力，不能让人误以为是真实对话。
class TestModeBanner extends StatelessWidget {
  const TestModeBanner({super.key});

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      color: const Color(0xFFFDF3DC),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      child: const Text(
        '测试模式：回复由确定性 fake provider 生成，非真实模型能力',
        style: TextStyle(fontSize: 12, color: Color(0xFF7A5B12)),
      ),
    );
  }
}
