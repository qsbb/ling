import 'package:flutter/material.dart';

import '../api/ling_api.dart';
import '../models/contracts.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key, required this.api, required this.onSignedIn});

  final LingApi api;
  final ValueChanged<AuthState> onSignedIn;

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _username = TextEditingController();
  final _password = TextEditingController();
  bool _pending = false;
  String? _error;
  String? _hint;

  @override
  void dispose() {
    _username.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_pending) return;
    setState(() {
      _pending = true;
      _error = null;
      _hint = null;
    });
    try {
      final auth = await widget.api.login(_username.text.trim(), _password.text);
      widget.onSignedIn(auth);
    } on LingApiException catch (exc) {
      setState(() {
        _error = exc.message;
        _hint = exc.retriable ? '可以稍后重试' : null;
      });
    } catch (exc) {
      setState(() {
        _error = '无法连接服务端';
        _hint = '检查 LING_BASE_URL 与网络';
      });
    } finally {
      if (mounted) setState(() => _pending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const Text('ling', style: TextStyle(fontSize: 30, fontWeight: FontWeight.w600)),
                  const SizedBox(height: 8),
                  const Text('登录后继续对话', style: TextStyle(fontSize: 18)),
                  const SizedBox(height: 4),
                  Text(
                    '会话与历史保存在服务端，手机只负责显示。',
                    style: TextStyle(color: Colors.black.withValues(alpha: 0.55)),
                  ),
                  const SizedBox(height: 24),
                  TextField(
                    controller: _username,
                    decoration: const InputDecoration(labelText: '用户名', border: OutlineInputBorder()),
                    textInputAction: TextInputAction.next,
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: _password,
                    decoration: const InputDecoration(labelText: '密码', border: OutlineInputBorder()),
                    obscureText: true,
                    onSubmitted: (_) => _submit(),
                  ),
                  if (_error != null) ...[
                    const SizedBox(height: 12),
                    Container(
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: const Color(0xFFFBE9E7),
                        borderRadius: BorderRadius.circular(10),
                      ),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(_error!, style: const TextStyle(fontWeight: FontWeight.w600)),
                          if (_hint != null) Text(_hint!),
                        ],
                      ),
                    ),
                  ],
                  const SizedBox(height: 20),
                  FilledButton(
                    onPressed: _pending ? null : _submit,
                    child: Text(_pending ? '登录中…' : '登录'),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
