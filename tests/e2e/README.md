# W0 浏览器实测

`w0_browser.mjs` 是 W0 的浏览器端到端实测脚本，覆盖 W0 的验收门槛：

| 项 | 断言 |
| --- | --- |
| 登录 | 口令登录后进入聊天 |
| 测试模式 | 界面显著位置显示"测试模式：回复由确定性 fake provider 生成" |
| 发送 | 点击发送后进入生成状态 |
| 流式 | 回复分多段到达（非一次性） |
| 刷新历史 | reload 后消息仍在（服务端权威） |
| 停止 | 生成中可见停止按钮，停止后显示已停止 |
| 会话切换 | 新建对话后是空白对话 |
| 失败提示 | 模型故障时显示可执行提示 |
| 重试 | 失败后点重试收到正常回复 |
| 设置页 | 可打开、显示职责绑定、不回显密钥 |
| 手机 375×667 | 发送/接收可用、输入框在视口内、无横向溢出、会话列表可打开 |
| 前端异常 | 无未捕获异常（未登录探测 401 属预期） |

## 运行

```bash
# 1) 后端（含前端构建产物）
. .venv/bin/activate && . scripts/dev_env.sh
export LING_WEB_DIST=$PWD/clients/web-console/dist
python -m ling

# 2) 实测（在任意目录，安装 playwright 后）
pnpm add -D playwright@1.56.0     # 与本机缓存的 chromium 版本匹配
node tests/e2e/w0_browser.mjs
```

环境变量：`BASE_URL`（默认 http://127.0.0.1:8080）、`LING_USER`、`LING_PASS`、
`SHOTS_DIR`（截图目录）、`CHROME_PATH`（chromium headless shell 路径）。

截图保存在 `SHOTS_DIR`，脚本失败时以非 0 退出。
