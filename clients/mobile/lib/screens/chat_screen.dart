import 'dart:async';

import 'package:flutter/material.dart';

import '../api/ling_api.dart';
import '../models/contracts.dart';
import '../widgets/message_bubble.dart';
import '../widgets/test_mode_banner.dart';

/// 聊天主界面：首屏直接可聊。
///
/// 权威历史来自服务端；流式增量只用于即时展示，刷新即重建。
class ChatScreen extends StatefulWidget {
  const ChatScreen({
    super.key,
    required this.api,
    required this.auth,
    required this.onSignOut,
  });

  final LingApi api;
  final AuthState auth;
  final Future<void> Function() onSignOut;

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  final _input = TextEditingController();
  final _scroll = ScrollController();

  List<ChatSession> _sessions = const [];
  String? _currentSessionId;
  List<ChatMessage> _messages = const [];
  bool _loading = true;
  bool _sending = false;
  String? _activeTurnId;
  String _streamingText = '';
  String? _error;
  String? _lastFailedTurnId;

  @override
  void initState() {
    super.initState();
    unawaited(_bootstrap());
  }

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _bootstrap() async {
    setState(() => _loading = true);
    try {
      var sessions = await widget.api.sessions();
      if (sessions.isEmpty) {
        final created = await widget.api.createSession();
        sessions = [created];
      }
      setState(() {
        _sessions = sessions;
        _currentSessionId = sessions.first.sessionId;
      });
      await _loadMessages(sessions.first.sessionId);
    } on LingApiException catch (exc) {
      setState(() => _error = exc.message);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _loadMessages(String sessionId) async {
    try {
      final page = await widget.api.messages(sessionId);
      if (!mounted) return;
      setState(() {
        _messages = page.messages;
        _activeTurnId = page.activeTurn?.turnId;
        _streamingText = '';
        _error = null;
      });
      _scrollToBottom();
    } on LingApiException catch (exc) {
      setState(() => _error = exc.message);
    }
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.animateTo(
          _scroll.position.maxScrollExtent,
          duration: const Duration(milliseconds: 180),
          curve: Curves.easeOut,
        );
      }
    });
  }

  Future<void> _switchSession(String sessionId) async {
    setState(() => _currentSessionId = sessionId);
    await _loadMessages(sessionId);
  }

  Future<void> _createSession() async {
    final created = await widget.api.createSession();
    setState(() {
      _sessions = [created, ..._sessions];
      _currentSessionId = created.sessionId;
      _messages = const [];
      _streamingText = '';
      _activeTurnId = null;
    });
  }

  Future<void> _send() async {
    final text = _input.text.trim();
    final sessionId = _currentSessionId;
    if (text.isEmpty || sessionId == null || _sending) return;
    setState(() {
      _sending = true;
      _error = null;
      _streamingText = '';
      _lastFailedTurnId = null;
    });
    _input.clear();
    final clientTurnId = DateTime.now().microsecondsSinceEpoch.toString();
    try {
      await for (final event in widget.api.send(sessionId, text, clientTurnId: clientTurnId)) {
        if (!mounted) return;
        if (event.name == 'turn.started') {
          final userMessage = event.data['user_message'];
          setState(() {
            _activeTurnId = event.turnId;
            if (userMessage is Map<String, dynamic>) {
              _messages = [..._messages, ChatMessage.fromJson(userMessage)];
            }
          });
        } else if (event.name == 'delta') {
          setState(() => _streamingText += event.delta);
          _scrollToBottom();
        } else if (event.isTerminal) {
          setState(() {
            _streamingText = '';
            _activeTurnId = null;
            _lastFailedTurnId = event.isFailure ? event.turnId : null;
            if (event.errorMessage != null) _error = event.errorMessage;
          });
          await _loadMessages(sessionId);
        }
      }
    } on LingApiException catch (exc) {
      setState(() => _error = exc.message);
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  Future<void> _stop() async {
    final turnId = _activeTurnId;
    if (turnId == null) return;
    await widget.api.stop(turnId);
    setState(() {
      _activeTurnId = null;
      _streamingText = '';
    });
    final sessionId = _currentSessionId;
    if (sessionId != null) await _loadMessages(sessionId);
  }

  Future<void> _retry() async {
    final turnId = _lastFailedTurnId;
    final sessionId = _currentSessionId;
    if (turnId == null || sessionId == null) return;
    setState(() {
      _sending = true;
      _error = null;
      _streamingText = '';
    });
    try {
      await for (final event in widget.api.retry(turnId)) {
        if (!mounted) return;
        if (event.name == 'delta') {
          setState(() => _streamingText += event.delta);
        } else if (event.isTerminal) {
          setState(() {
            _streamingText = '';
            _lastFailedTurnId = event.isFailure ? turnId : null;
            _error = event.errorMessage;
          });
          await _loadMessages(sessionId);
        }
      }
    } on LingApiException catch (exc) {
      setState(() => _error = exc.message);
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('ling'),
        actions: [
          IconButton(
            tooltip: '新建对话',
            onPressed: _createSession,
            icon: const Icon(Icons.add_comment_outlined),
          ),
          IconButton(
            tooltip: '退出',
            onPressed: () => unawaited(widget.onSignOut()),
            icon: const Icon(Icons.logout),
          ),
        ],
      ),
      drawer: Drawer(
        child: SafeArea(
          child: ListView(
            children: [
              const ListTile(title: Text('我的会话'), dense: true),
              ..._sessions.map(
                (session) => ListTile(
                  selected: session.sessionId == _currentSessionId,
                  title: Text(session.title, maxLines: 1, overflow: TextOverflow.ellipsis),
                  subtitle: Text('${session.messageCount} 条'),
                  onTap: () {
                    Navigator.of(context).pop();
                    unawaited(_switchSession(session.sessionId));
                  },
                ),
              ),
            ],
          ),
        ),
      ),
      body: SafeArea(
        child: Column(
          children: [
            if (widget.auth.testMode) const TestModeBanner(),
            if (_error != null)
              MaterialBanner(
                content: Text(_error!),
                actions: [
                  if (_lastFailedTurnId != null)
                    TextButton(onPressed: _retry, child: const Text('重试')),
                  TextButton(
                    onPressed: () => setState(() => _error = null),
                    child: const Text('知道了'),
                  ),
                ],
              ),
            Expanded(
              child: _loading
                  ? const Center(child: CircularProgressIndicator())
                  : ListView.builder(
                      controller: _scroll,
                      padding: const EdgeInsets.fromLTRB(12, 12, 12, 4),
                      itemCount: _messages.length + (_streamingText.isEmpty ? 0 : 1),
                      itemBuilder: (context, index) {
                        if (index >= _messages.length) {
                          return MessageBubble(
                            message: ChatMessage(
                              messageId: 'streaming',
                              turnId: _activeTurnId,
                              role: 'actor',
                              text: _streamingText,
                              state: 'streaming',
                              sequence: 1 << 30,
                              createdAt: DateTime.now(),
                            ),
                            streaming: true,
                          );
                        }
                        return MessageBubble(message: _messages[index]);
                      },
                    ),
            ),
            _composer(),
          ],
        ),
      ),
    );
  }

  Widget _composer() {
    final busy = _activeTurnId != null || _sending;
    return Padding(
      padding: EdgeInsets.fromLTRB(
        12,
        4,
        12,
        8 + MediaQuery.of(context).padding.bottom * 0.2,
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          Expanded(
            child: TextField(
              controller: _input,
              minLines: 1,
              maxLines: 5,
              textInputAction: TextInputAction.newline,
              decoration: InputDecoration(
                hintText: busy ? '正在回应…可随时停止' : '说点什么吧',
                border: const OutlineInputBorder(),
                isDense: true,
              ),
            ),
          ),
          const SizedBox(width: 8),
          if (busy)
            IconButton.filledTonal(
              tooltip: '停止生成',
              onPressed: _stop,
              icon: const Icon(Icons.stop),
            )
          else
            IconButton.filled(
              tooltip: '发送',
              onPressed: _send,
              icon: const Icon(Icons.arrow_upward),
            ),
        ],
      ),
    );
  }
}
