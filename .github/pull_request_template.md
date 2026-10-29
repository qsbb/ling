## 这个 PR 做了什么

## 为什么

## 检查

- [ ] 提交信息符合 Conventional Commits，首行 ≤ 50 字符
- [ ] `ruff check . && mypy src connectors` 通过
- [ ] `pytest tests` 通过
- [ ] 改了行为的话，契约（`contracts/`）同步更新
- [ ] 新增测试能回答：把要保护的那行改坏，这条测试会红吗？
