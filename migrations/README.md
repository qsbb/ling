# 数据库迁移

生产唯一迁移入口：`python -m ling.migrate upgrade`（容器 entrypoint 自动执行）。

- schema owner：主智能体协调，同一批次只有一人写迁移。
- 新增表/列必须先改 `src/ling/infra/models.py`，再 `alembic revision --autogenerate`。
- 不可逆迁移必须先做恢复演练；回滚只在 schema 兼容窗口内自动执行。
- `versions/` 目前为空：首次部署使用 `metadata.create_all`（幂等）建基线，
  第一个 Alembic 修订在加入不可自动生成的约束时引入。
