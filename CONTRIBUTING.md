# 贡献指南

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
| `chore` | 构建、依赖、工具 |

**首行不超过 50 个字符**（最长 72），否则 GitHub 会截断成一行省略号。
body 每行 72 字符内换行。

## 代码要求

提交前跑：

```bash
ruff check . && mypy src connectors && pytest tests
cd clients/web-console && pnpm typecheck && pnpm build
```

## 改行为之前先改契约

`contracts/` 下的 13 份 v1 schema 是接口的单一真源。改动流程：

1. 改 `src/ling/contracts/v1.py`
2. 跑 `python scripts/export_contracts.py`
3. 提交契约与代码

契约测试会拦住"代码改了但契约没跟上"。

## 测试的底线

**只测 helper 不算测接线。** 新增一个函数并给它写单元测试，但调用点写错了，
测试照样绿——这种假绿比没测试更危险。

所以涉及"某个能力是否真的被用上"的改动，要断言到**最终结果**（落库的行、
发出去的报文、返回给用户的文本），而不是中间函数的返回值。
每条新测试最好能回答：把它要保护的那行代码改坏，这条测试会红吗？
