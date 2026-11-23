# 自定义实例命名空间的晚回开关修复

## 观测到的失败

真实 Chromium 访问隔离 API（`channel=onebot`、窗口键 `fixture-bridge:private:100200303`）
时，GET/PUT `/v1/sessions/{id}/onebot-deferred` 均 200，但会话设置里没有开关。
默认命名空间（`onebot-default:private:…`）的同类会话能操作。

## 观测与归因

- **实测**：真实 API 返回的 `SessionRecord.channel` 是协议值 `onebot`（见
  `GET /v1/sessions` 输出），与窗口键的实例段无关；`fixture-bridge` 会话同样是
  `channel=onebot`、`peer_id=100200303`，GET/PUT 均 200。
- **实测**：前端判定 `supportsOneBotDeferred` 当时用 `ref.instance.startsWith('onebot-')`
  猜渠道，`fixture-bridge` 不匹配 → 不渲染。
- **依据**：`LING_ONEBOT_INSTANCE_ID` 是 actor 命名空间（见
  `src/ling/settings/connector_instances.py` 顶部说明），可以随便起名，不能用来判断协议。

## 修复

判定改为只看契约上稳定的两件事：`describeSession(session).kind === 'private'`，
以及 `session.channel === 'onebot'`。官方 QQ 机器人是 `channel=qqbot`，自然排除；
旧服务端没有 `channel` 时按不显示处理。

## 验证

- 单元判定 9 条；浏览器 11 条（含自定义命名空间开/刷/关、与默认命名空间各自保存）。
- 变异：改回前缀猜测 → 6 条红；改成「私聊即显示」→ 3 条红。
- 全量前端 1113 通过 / 1 跳过；类型检查与生产构建通过。
- 真实 Chromium（隔离 API + 同源代理）截图：`30-custom-off.png`（关闭）、
  `31-custom-on.png`（开启，键为 `fixture-bridge:private:100200303`）、
  `32-group.png`、`33-web.png`（不出现开关）。
