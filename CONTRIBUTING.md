# 贡献指南

## 本地启动

从[部署手册的本地开发章节](docs/DEPLOYMENT.md#1-本地开发无-docker)配置 Python、PostgreSQL 和 Web 控制台。开发默认开启 `LING_TEST_MODE=1`；外部凭据、私有数据和本地设计草稿不要提交。

## 提交信息

遵循 [Conventional Commits](https://www.conventionalcommits.org/)：

```
<type>(<scope>): <简短说明>

<body 说明为什么这么改>
```

| type | 用途 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修 bug |
| `refactor` | 重构（不改行为） |
| `perf` | 性能 |
| `test` | 只改测试 |
| `docs` | 只改文档 |
| `build` / `ci` / `chore` | 构建、CI 或其他维护 |

首行不超过 50 个字符，正文每行不超过 72 个字符。不要顺手修改版本号。

## 本地检查

后端测试使用隔离 PostgreSQL。使用 `scripts/dev_pg.sh start` 的默认配置时，它会创建 `ling` 和 `ling_test` 两个库；**不要把任一测试变量指向真实业务库**。共享测试库运行 pytest 前需按 [AGENTS.md](AGENTS.md#跑-pytest-必须持锁) 持锁。

```bash
export LING_TEST_DATABASE_URL='postgresql+asyncpg://ling@127.0.0.1:55432/ling_test'
export LING_DATABASE_URL="$LING_TEST_DATABASE_URL"
ruff check . && mypy src connectors && python -m pytest tests
git diff --check
```

语音编解码测试另需 `ffmpeg` 与 `.[dev,speech-codecs]`。Web 改动可从仓库根目录执行：

```bash
(cd clients/web-console && pnpm install --frozen-lockfile && pnpm exec playwright install chromium && pnpm typecheck && pnpm test && pnpm build)
```

提交说明标明实际执行的检查，不把 stub 测试、真实浏览器验收和生产部署混为一谈。CI 入口见 [ci.yml](.github/workflows/ci.yml)。

## 改行为之前先改契约

`src/ling/contracts/v1.py` 是 v1 模型源文件，`contracts/` 保存导出的 JSON Schema。改动流程：

1. 改 `src/ling/contracts/v1.py`
2. 跑 `python scripts/export_contracts.py`
3. 提交契约与代码

契约测试会拦住"代码改了但契约没跟上"。

## 平台接入规范

本规范适用于所有现有和新增接入器，不因平台或传输方式而异。

- **默认不启用 Ling 附加的校验。** 密钥、Token 和来源 IP 白名单均为可选项，默认留空。填哪项就校验哪项；配置多项时须全部通过，不能在校验失败时降级放行。升级保留已有配置。
- **无凭据入站仅用于受信的本机或内网直连。** 不将免凭据入口暴露到公网或经反代转发。来源 IP 校验不能信任客户端声明的转发头；QQ 号、账号绑定和会话白名单也不是传输认证。
- **区分平台必需和 Ling 可选。** 平台协议要求的应用凭据或回调验签材料单独标为「平台必需」，保留平台要求的校验。配置页面和示例不得把 Ling 的可选项标成必填。
- 接入页按连接方向展示地址和各自状态；未配置、等待连入、连接已建立但未收到事件要能区分。HTTP 上报没有长连接，不能显示为断线。

新增或修改接入器时，通过实际接入入口验证：可选项留空能在受信直连场景使用；
启用任一可选项后，匹配时通过，缺失凭据、凭据错误或来源不匹配时拒绝；
公网或反代请求不享受直连豁免。只测校验函数不算通过。

## 测试的底线

**只测 helper 不算测接线。** 新增一个函数并给它写单元测试，但调用点写错了，
测试照样绿——这种假绿比没测试更危险。

所以涉及"某个能力是否真的被用上"的改动，要断言到**最终结果**（落库的行、
发出去的报文、返回给用户的文本），而不是中间函数的返回值。
每条新测试最好能回答：把它要保护的那行代码改坏，这条测试会红吗？
