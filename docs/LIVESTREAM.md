# 自定义接入与直播

在 Web 控制台打开「设置 → 接入器 → 自定义接入与直播」。选择 B 站直播并保存启用会注册接入器。同一 ID 再次启用时沿用原入口和观众账号前缀。其他服务可创建自定义 HTTP 类型。

## 在直播电脑上运行舞台

准备 OBS 28 或更新版，开启 WebSocket v5。在节目场景加入一个独立的媒体源和一个文本源。媒体源应开启直播音轨，字幕源使用直接文本。需要皮套时，在 VTube Studio 加载模型并打开插件 API，于 OBS 场景加入皮套采集源。Windows 可用 Spout2，macOS 可用窗口采集。 [VTS 采集说明](https://github.com/DenchiSoft/VTubeStudio/wiki/Recording-Streaming-with-OBS)

在本项目检出目录安装舞台依赖。仅处理本机音频和字幕时可以先省略 VTS 参数。

```bash
uv pip install -e '.[stage]'
ling-stage --scene 'Ling 直播' --audio-source 'Ling 语音' --caption-source 'Ling 字幕' \
  ./sample.wav '试听字幕'
```

为接入器设置独立的舞台令牌。将同一个值设为舞台进程的 `LING_STAGE_TOKEN`。OBS 密码通过 `LING_STAGE_OBS_PASSWORD` 传递。启动常驻舞台后在 Web 控制台开预览场次。

```bash
ling-stage --server http://127.0.0.1:8080 --instance bili-live \
  --scene 'Ling 直播' --audio-source 'Ling 语音' --caption-source 'Ling 字幕' \
  --avatar-source 'Ling 皮套' --vts-model '实际模型 ID' --mouth-parameter MouthOpen
```

首次连接 VTS 时批准 Ling Stage 插件。默认令牌保存在 `~/.ling-stage/vts-token`。常驻端下载的是本场语音，字幕在 OBS 开始播放后显示，完成或取消后清除。VTS 口型只取指定语音源音量。Ling 的模型和语音供应商沿用已发布的对话与 TTS 配置。

## B 站采集

使用单独的 Python 环境运行采集程序，配置开放平台应用的 `BILIBILI_ACCESS_KEY_ID`、`BILIBILI_ACCESS_KEY_SECRET`、`BILIBILI_APP_ID` 和主播的 `BILIBILI_ANCHOR_CODE`。接入器应填写平台返回的规范房间 ID。

安装以下版本的 blivedm。固定版本包含弹幕、礼物和房间开停播事件。采集命令从项目检出目录执行。

```bash
uv pip install 'httpx>=0.27,<1' 'aiohttp>=3.9,<4' 'Brotli>=1.1,<2' 'pure-protobuf>=3.1.2,<4'
uv pip install --no-deps 'https://github.com/xfgryujk/blivedm/archive/3bf17fe862b8c2fc6bc04c81c3f0c5db95de93d6.tar.gz'
python -m connectors.bilibili --server http://127.0.0.1:8080 --instance bili-live
```

如配置了上报令牌或签名密钥，采集端分别读取 `LING_INGRESS_TOKEN` 和 `LING_INGRESS_SIGNING_SECRET`。观众可以从本场已播出的互动中选择记住，并到「关系」合并账号。系统按房间保存公开备注。

## 其他服务上报

读 `GET /v1/custom/{id}/session` 获得当前 `session_id` 与 `room_id`。向接入器显示的 `POST /v1/custom/{id}/events` 上报 JSON，事件时间使用带时区的 ISO 8601 时间。

```json
{
  "event_id": "message-123",
  "kind": "comment",
  "room_id": "100200300",
  "session_id": "live_当前场次",
  "occurred_at": "2026-11-22T10:00:00+08:00",
  "sender": {"id": "viewer-42", "name": "阿溪"},
  "text": "今天的配乐是什么？"
}
```

`sender.id` 使用来源提供的稳定账号 ID，缺失时按匿名处理。其他事件类型为 `entrance`、`like`、`gift`、`live_start`、`live_end`。配置令牌时发送 `Authorization: Bearer <token>`。配置签名时发送 `X-Ling-Signature: sha256=<hex>`，摘要用配置密钥对请求原始字节计算 HMAC-SHA256，读取场次时签空正文。

## 开停播

确认 OBS 使用专门的节目场景、正确的推流目标和目标直播间的串流配置。舞台命令增加 `--stream-server`，填 OBS 内配置的推流服务地址。随后在网页允许启停 OBS 推流，可手动开播，或为一次自主开播授权开始、结束时间。自主模式由模型在有效时段内选择开场或等待，实际播出同时受单场上限和授权结束时间约束。

B 站采集用开放平台的房间开停播通知，并用房间状态补查。平台若要求主播先在官方界面开房，需要在那里完成该步骤。部署时先验证该账号能否通过已配置的 OBS 推流方式自动开房与停播，再授权自主模式。控制台分别显示 OBS 推流和目标房间状态。 [B 站开放平台接口](https://open-live.bilibili.com/document/eba8e2e1-847d-e908-2e5c-7a1ec7d9266f)
