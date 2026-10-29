import 'dart:async';

import 'package:flutter/material.dart';

import 'api/ling_api.dart';
import 'models/contracts.dart';
import 'screens/chat_screen.dart';
import 'screens/login_screen.dart';

/// ling 手机客户端入口。
///
/// 与 Web 同契约：认证 cookie + 服务端权威历史。
/// 语音、相机、位置默认关闭；打开需要在设置里逐项授权（W2 未联调）。
void main() {
  runApp(const LingApp());
}

class LingApp extends StatefulWidget {
  const LingApp({super.key});

  @override
  State<LingApp> createState() => _LingAppState();
}

class _LingAppState extends State<LingApp> {
  static const _baseUrl = String.fromEnvironment(
    'LING_BASE_URL',
    defaultValue: 'http://127.0.0.1:8080',
  );

  late final LingApi _api = LingApi(baseUrl: _baseUrl);
  AuthState? _auth;
  bool _booting = true;

  @override
  void initState() {
    super.initState();
    unawaited(_restore());
  }

  Future<void> _restore() async {
    try {
      final auth = await _api.me();
      if (mounted) setState(() => _auth = auth);
    } catch (_) {
      // 恢复失败按未登录处理，不阻塞启动
    } finally {
      if (mounted) setState(() => _booting = false);
    }
  }

  @override
  void dispose() {
    _api.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'ling',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF6B7F6E),
          brightness: Brightness.light,
        ),
        scaffoldBackgroundColor: const Color(0xFFF7F4EF),
        fontFamily: 'PingFang SC',
      ),
      home: _booting
          ? const Scaffold(body: Center(child: CircularProgressIndicator()))
          : (_auth == null
              ? LoginScreen(
                  api: _api,
                  onSignedIn: (auth) => setState(() => _auth = auth),
                )
              : ChatScreen(
                  api: _api,
                  auth: _auth!,
                  onSignOut: () async {
                    await _api.logout();
                    if (mounted) setState(() => _auth = null);
                  },
                )),
    );
  }
}
