// 契约解析单元测试（W2：已编写，未运行 —— 需要 Flutter SDK）。
//
// 只测"解析服务端返回的契约形状"，不测业务逻辑，因此不依赖真实后端。

import 'package:flutter_test/flutter_test.dart';
import 'package:ling_mobile/models/contracts.dart';

void main() {
  test('AuthState 解析登录响应', () {
    final auth = AuthState.fromJson({
      'schema_version': 'v1',
      'user': {
        'id': 'user_1',
        'username': 'ling',
        'role': 'owner',
        'subject_id': 'subj_1',
        'display_name': 'ling',
      },
      'config_revision': 3,
      'test_mode': true,
    });
    expect(auth.username, 'ling');
    expect(auth.configRevision, 3);
    expect(auth.testMode, isTrue);
  });

  test('MessagePage 解析活动轮次', () {
    final page = MessagePage.fromJson({
      'messages': [
        {
          'message_id': 'msg_1',
          'turn_id': 'turn_1',
          'role': 'user',
          'text': '你好',
          'state': 'completed',
          'sequence': 1,
          'created_at': '2026-10-26T07:00:00Z',
        }
      ],
      'active_turn': {
        'turn_id': 'turn_2',
        'state': 'streaming',
        'attempt': 1,
        'retriable': false,
      },
    });
    expect(page.messages, hasLength(1));
    expect(page.activeTurn?.turnId, 'turn_2');
  });

  test('ChatEvent 区分终态与失败', () {
    const delta = ChatEvent(name: 'delta', data: {'text': '嗯'});
    const done = ChatEvent(name: 'turn.completed', data: {});
    const failed = ChatEvent(
      name: 'turn.failed',
      data: {
        'error': {'code': 'provider_timeout', 'message': '超时', 'retriable': true}
      },
    );
    expect(delta.isTerminal, isFalse);
    expect(delta.delta, '嗯');
    expect(done.isTerminal, isTrue);
    expect(failed.isFailure, isTrue);
    expect(failed.errorMessage, '超时');
  });
}
