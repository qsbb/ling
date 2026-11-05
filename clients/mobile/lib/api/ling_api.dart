import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

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
      // `_client.send` 给的是 StreamedResponse，而 `fromResponse` 只收 Response：
      // 错误分支必须先把 body 读出来，才能取到服务端的错误契约（code/message）。
      throw LingApiException.fromResponse(await _readErrorResponse(response));
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

/// 把**非 200** 的流式响应读成 [http.Response]，交给
/// [LingApiException.fromResponse] 解析。
///
/// 只在错误分支调用：此时服务端返回的是一段契约形状的错误 JSON，不会开始 SSE，
/// 所以把 body 读进内存是安全的。但仍设 [maxBytes] 上限——代理的错误页、被劫持
/// 的流都可能给出没有尽头的 body，移动端不该为此把内存耗光。
Future<http.Response> _readErrorResponse(
  http.StreamedResponse response, {
  int maxBytes = 64 * 1024,
}) async {
  final builder = BytesBuilder(copy: false);
  await for (final chunk in response.stream) {
    if (builder.length + chunk.length > maxBytes) {
      builder.add(chunk.sublist(0, maxBytes - builder.length));
      break;
    }
    builder.add(chunk);
  }
  return http.Response.bytes(
    builder.takeBytes(),
    response.statusCode,
    request: response.request,
    headers: response.headers,
    // 故意不复制 isRedirect / persistentConnection / reasonPhrase：
    // fromResponse 只读 bodyBytes 与 statusCode，多传参数只会让这段代码
    // 绑死在较新的 http 版本上（pubspec 的下限是 ^1.2.2，而 CI 不锁 lockfile）。
  );
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
