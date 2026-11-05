"""受控 Web 更新通道（API 侧只读写固定共享目录，不接触 Docker）。

模块边界：

- ``protocol``：**纯标准库**的共享协议（请求/状态文件格式、校验、文件锁、
  原子写入）。宿主执行器 ``deploy/update/executor.py`` 直接导入这一份文件，
  因此这里不允许 import FastAPI / SQLAlchemy / ling.config。
- ``service``：API 侧的只读状态视图与单飞提交；只依赖 ``protocol`` 和
  ``ling.config.Settings``。
"""

from __future__ import annotations

__all__: list[str] = []
