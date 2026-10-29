import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../models/contracts.dart';

/// ling HTTP/SSE 客户端。
///
/// 与 Web 端使用**同一套**契约（docs/API-WEB-V1.md）：
/// 认证走 cookie，聊天走 `POST /v1/sessions/{id}/turns` 的 SSE。
/// 本类不缓存权威历史；刷新即重取。
class LingApi {
  LingApi({required this.baseUrl, http.Client? client})
      : _client = client ?? http.Client();

  final String baseUrl;
  final http.Client _client;
  String? _sessionCookie;

  bool get isAuthenticated => _sessionCookie != null;

  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        if (_sessionCookie != null) 'Cookie': _sessionCookie!,
      };

  void _absorbCookies(http.BaseResponse response) {
    final raw = response.headers['set-cookie'];
    if (raw == null) return;
    // 只取 ling_sid，忽略其它 cookie 属性
    for (final part in raw.split(';')) {
      final trimmed = part.trim();
      if (trimmed.startsWith('ling_sid=')) {
        _sessionCookie = trimmed.split(';').first;
        return;
      }
    }
  }

  Future<AuthState> login(String username, String password) async {
    final response = await _client.post(
      Uri.parse('$baseUrl/v1/auth/login'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'username': username, 'password': password}),
    );
    if (response.statusCode != 200) {
      throw LingApiException.fromResponse(response);
    }
    _absorbCookies(response);
    return AuthState.fromJson(
      jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, dynamic>,
    );
  }

  Future<AuthState?> me() async {
    if (_sessionCookie == null) return null;
    final response = await _client.get(
      Uri.parse('$baseUrl/v1/auth/me'),
      headers: _headers,
    );
    if (response.statusCode == 401) {
      _sessionCookie = null;
      return null;
    }
    if (response.statusCode != 200) {
      throw LingApiException.fromResponse(response);
    }
    return AuthState.fromJson(
      jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, dynamic>,
    );
  }

  Future<void> logout() async {
    await _client.post(Uri.parse('$baseUrl/v1/auth/logout'), headers: _headers);
    _sessionCookie = null;
  }

  Future<List<ChatSession>> sessions() async {
    final response = await _client.get(
      Uri.parse('$baseUrl/v1/sessions'),
      headers: _headers,
    );
    if (response.statusCode != 200) {
      throw LingApiException.fromResponse(response);
    }
    final data = jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, dynamic>;
    return (data['sessions'] as List<dynamic>)
        .map((e) => ChatSession.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<ChatSession> createSession({String? title}) async {
    final response = await _client.post(
      Uri.parse('$baseUrl/v1/sessions'),
      headers: _headers,
      body: jsonEncode({'title': title}),
    );
    if (response.statusCode != 201) {
      throw LingApiException.fromResponse(response);
    }
    return ChatSession.fromJson(
      jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, dynamic>,
    );
  }

  Future<MessagePage> messages(String sessionId, {int limit = 100}) async {
    final response = await _client.get(
      Uri.parse('$baseUrl/v1/sessions/$sessionId/messages?limit=$limit'),
      headers: _headers,
    );
    if (response.statusCode != 200) {
      throw LingApiException.fromResponse(response);
    }
    return MessagePage.fromJson(
      jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, dynamic>,
    );
  }

  /// 发送并流式接收。事件名与 Web 完全一致。
  Stream<ChatEvent> send(String sessionId, String text, {required String clientTurnId}) async* {
    final request = http.Request(
      'POST',
      Uri.parse('$baseUrl/v1/sessions/$sessionId/turns'),
    );
    request.headers.addAll(_headers);
    request.body = jsonEncode({'client_turn_id': clientTurnId, 'text': text});
    final response = await _client.send(request);
    if (response.statusCode != 200) {
      throw LingApiException(
        code: 'provider_error',
        message: '发送失败 HTTP ${response.statusCode}',
        retriable: true,
      );
    }
    yield* _parseSse(response.stream);
  }

  Stream<ChatEvent> retry(String turnId) async* {
    final request = http.Request(
      'POST',
      Uri.parse('$baseUrl/v1/turns/$turnId/retry'),
    );
    request.headers.addAll(_headers);
    final response = await _client.send(request);
    if (response.statusCode != 200) {
      throw LingApiException.fromResponse(response);
    }
    yield* _parseSse(response.stream);
  }

  Future<void> stop(String turnId) async {
    await _client.post(
      Uri.parse('$baseUrl/v1/turns/$turnId/stop'),
      headers: _headers,
    );
  }

  Stream<ChatEvent> _parseSse(Stream<List<int>> bytes) async* {
    var buffer = '';
    String eventName = '';
    await for (final chunk in bytes.transform(utf8.decoder)) {
      buffer += chunk;
      while (buffer.contains('\n\n')) {
        final index = buffer.indexOf('\n\n');
        final raw = buffer.substring(0, index);
        buffer = buffer.substring(index + 2);
        for (final line in raw.split('\n')) {
          if (line.startsWith('event: ')) {
            eventName = line.substring(7).trim();
          } else if (line.startsWith('data: ')) {
            yield ChatEvent(
              name: eventName,
              data: jsonDecode(line.substring(6)) as Map<String, dynamic>,
            );
          }
        }
      }
    }
  }

  void dispose() => _client.close();
}

class LingApiException implements Exception {
  LingApiException({required this.code, required this.message, required this.retriable});

  final String code;
  final String message;
  final bool retriable;

  factory LingApiException.fromResponse(http.Response response) {
    try {
      final body = jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, dynamic>;
      final error = body['error'] as Map<String, dynamic>?;
      if (error != null) {
        return LingApiException(
          code: error['code'] as String? ?? 'internal',
          message: error['message'] as String? ?? '请求失败',
          retriable: error['retriable'] as bool? ?? false,
        );
      }
    } catch (_) {
      // 响应不是契约形状，落到下面的兜底
    }
    return LingApiException(
      code: 'internal',
      message: '请求失败 HTTP ${response.statusCode}',
      retriable: response.statusCode >= 500,
    );
  }

  @override
  String toString() => '$code: $message';
}
