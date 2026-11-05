# ling 手机参考客户端（Flutter）

**状态：已实现、未测试。** 没有真机/模拟器联调，没有开发者签名，没有语音设备验证。

## 职责

- 与 Web 使用**同一套**认证与 Session 契约（`docs/API-WEB-V1.md`），不另建数据源。
- 文字对话：发送、流式接收、停止、失败重试、会话切换。
- 语音（面对面）：开麦 → VAD → ASR → final 才进主链 → TTS → 播放回执。
  相机/位置/空间感知默认关闭，需在设备上逐项授权。
- 推送与前台状态上报（在场证据有时效）。

## 本地开发

```bash
cd clients/mobile
flutter pub get
flutter run --dart-define=LING_BASE_URL=http://192.0.2.10:8080
```

**未验证项**：真机运行、麦克风权限、后台切换、断线重连、TTS 播放回执、
安全区与刘海适配、iOS 签名。这些都需要设备与平台账号，属 W3。

## 结构

| 目录 | 内容 |
| --- | --- |
| `lib/api/` | HTTP + SSE 客户端，与 Web 同契约 |
| `lib/models/` | 契约对象（消息、会话、认证） |
| `lib/screens/` | 登录、聊天、设置、授权 |
| `lib/widgets/` | 消息气泡、输入栏、状态条 |
| `lib/voice/` | 语音采集与播放（默认关闭） |
