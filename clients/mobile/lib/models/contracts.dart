/// 与 Web 共享的契约对象（docs/API-WEB-V1.md）。
///
/// 不在这里加业务规则；只做解析与展示。
library;

class AuthState {
  const AuthState({
    required this.userId,
    required this.username,
    required this.displayName,
    required this.subjectId,
    required this.role,
    required this.configRevision,
    required this.testMode,
  });

  final String userId;
  final String username;
  final String displayName;
  final String subjectId;
  final String role;
  final int configRevision;
  final bool testMode;

  factory AuthState.fromJson(Map<String, dynamic> json) {
    final user = json['user'] as Map<String, dynamic>;
    return AuthState(
      userId: user['id'] as String,
      username: user['username'] as String,
      displayName: user['display_name'] as String? ?? user['username'] as String,
      subjectId: user['subject_id'] as String,
      role: user['role'] as String? ?? 'member',
      configRevision: json['config_revision'] as int? ?? 0,
      testMode: json['test_mode'] as bool? ?? false,
    );
  }
}

class ChatSession {
  const ChatSession({
    required this.sessionId,
    required this.title,
    required this.messageCount,
    required this.updatedAt,
    required this.archived,
  });

  final String sessionId;
  final String title;
  final int messageCount;
  final DateTime updatedAt;
  final bool archived;

  factory ChatSession.fromJson(Map<String, dynamic> json) => ChatSession(
        sessionId: json['session_id'] as String,
        title: json['title'] as String? ?? '新的对话',
        messageCount: json['message_count'] as int? ?? 0,
        updatedAt: DateTime.tryParse(json['updated_at'] as String? ?? '') ?? DateTime.now(),
        archived: json['archived'] as bool? ?? false,
      );
}

class ChatMessage {
  const ChatMessage({
    required this.messageId,
    required this.turnId,
    required this.role,
    required this.text,
    required this.state,
    required this.sequence,
    required this.createdAt,
    this.modelLabel,
    this.errorCode,
  });

  final String messageId;
  final String? turnId;
  final String role;
  final String text;
  final String state;
  final int sequence;
  final DateTime createdAt;
  final String? modelLabel;
  final String? errorCode;

  bool get isActor => role == 'actor';
  bool get isFailed => state == 'failed';
  bool get isStopped => state == 'stopped';

  factory ChatMessage.fromJson(Map<String, dynamic> json) => ChatMessage(
        messageId: json['message_id'] as String,
        turnId: json['turn_id'] as String?,
        role: json['role'] as String,
        text: json['text'] as String? ?? '',
        state: json['state'] as String? ?? 'completed',
        sequence: json['sequence'] as int? ?? 0,
        createdAt: DateTime.tryParse(json['created_at'] as String? ?? '') ?? DateTime.now(),
        modelLabel: json['model_label'] as String?,
        errorCode: json['error_code'] as String?,
      );
}

class TurnView {
  const TurnView({
    required this.turnId,
    required this.state,
    required this.attempt,
    required this.retriable,
    this.errorCode,
    this.modelLabel,
  });

  final String turnId;
  final String state;
  final int attempt;
  final bool retriable;
  final String? errorCode;
  final String? modelLabel;

  factory TurnView.fromJson(Map<String, dynamic> json) => TurnView(
        turnId: json['turn_id'] as String,
        state: json['state'] as String,
        attempt: json['attempt'] as int? ?? 1,
        retriable: json['retriable'] as bool? ?? false,
        errorCode: json['error_code'] as String?,
        modelLabel: json['model_label'] as String?,
      );
}

class MessagePage {
  const MessagePage({required this.messages, this.activeTurn});

  final List<ChatMessage> messages;
  final TurnView? activeTurn;

  factory MessagePage.fromJson(Map<String, dynamic> json) => MessagePage(
        messages: (json['messages'] as List<dynamic>)
            .map((e) => ChatMessage.fromJson(e as Map<String, dynamic>))
            .toList(),
        activeTurn: json['active_turn'] == null
            ? null
            : TurnView.fromJson(json['active_turn'] as Map<String, dynamic>),
      );
}

class ChatEvent {
  const ChatEvent({required this.name, required this.data});

  final String name;
  final Map<String, dynamic> data;

  String? get turnId => data['turn_id'] as String?;
  String get delta => data['text'] as String? ?? '';
  ChatMessage? get message => data['message'] == null
      ? null
      : ChatMessage.fromJson(data['message'] as Map<String, dynamic>);
  String? get errorMessage {
    final error = data['error'];
    if (error is Map<String, dynamic>) return error['message'] as String?;
    return null;
  }

  bool get isFailure => name == 'turn.failed';
  bool get isTerminal => name == 'turn.completed' || name == 'turn.stopped' || isFailure;
}
