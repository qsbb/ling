"""媒体域的错误类型。

## 为什么 ``MediaRefError`` 单独成一个模块

它由**取媒体内容的函数**（``ling.worker.make_media_fetcher``）抛出，却要被
**描述层**（``ling.domains.media.describe``）识别成"永久错误"。放在
``ling.worker`` 里的话，描述层要么反向 import worker（循环依赖），要么
只能按 ``RuntimeError`` 一概而论——后者正是原来的 bug：越界/超限被降级成
"可重试"，于是每 60 秒重试一次，永远重试不完。

单独一个无依赖的小模块，两边都 import 它，没有循环。

## 为什么失败要分「来源」而不是只看 ``retriable``

``retriable`` 只回答"重试同一个请求有没有用"，**不回答**"该不该把这条
证据标成终态"。模型侧的 ``auth``（401）和 ``missing_secret`` 都是
``retriable=False``（重试同一个请求确实没用），但它们是**配置/模型**的问题，
不是**这个引用**的问题：

- 密钥配错一次 ⇒ 队列里**所有**待描述证据一次性被标死；
- 修好密钥后它们已不在 ``pending()`` 队列里，**再也不会重试**。

所以终态判据必须是"错误来源"：只有**引用本身**不可用（越界、超限、形式
不支持、内容为空）才标终态；模型/配置侧失败一律保留在队列里，等配置修好
后下一轮重来。
"""

from __future__ import annotations

from ling.ports.model import ModelErrorKind

# 失败来源。**这是终态判据**，不是 retriable 布尔值。
SOURCE_REF = "ref"  # 引用本身不可用（越界/超限/形式不支持/内容为空）
SOURCE_MODEL = "model"  # 模型或模型配置侧（401、密钥缺失、职责未绑定、超时）
SOURCE_BUSINESS = "business"  # 业务拒绝（撤权、过期、窗口不匹配）
SOURCE_OTHER = "other"  # 其余（网络抖动等）

# 模型/配置侧的"整轮必然全失败"错误：所有证据都会以同样方式失败，
# 逐条跳过等于把 limit 条全试一遍。命中这些时整轮中止，等配置修好再来。
CONFIG_ERROR_KINDS = frozenset(
    {
        ModelErrorKind.auth,
        ModelErrorKind.missing_secret,
        ModelErrorKind.unsupported,
    }
)


class MediaRefError(RuntimeError):
    """媒体引用被拒：越界、超限、或形式不支持。

    单独一个类型是为了让上层能区分"引用本身不合法"（**永久错误**，重试
    没有意义）与"下载失败"（网络抖动，可能重试）。**不要**把它降级成
    "返回空"——静默返回空会让上层把"没拿到"当成"内容为空"，判定成不可
    重试的失败，而且真正的越界尝试在日志里一个字都不会留下。
    """


class MediaDescribeError(Exception):
    """描述失败。

    ## 三个字段各自回答一个问题

    - ``retriable``：**重试同一个请求**有没有用（网络抖动 True，401 False）。
    - ``terminal``：该不该把**这条证据**标成终态、移出待办队列。
    - ``source``：失败**来源**是引用、模型、还是业务拒绝（用来出日志与决定
      要不要整轮中止）。

    前两个**不能合并**。合并的后果是：一次密钥配错把整批证据标死，修好
    密钥后再也不会重试——`pending()` 只按 `description IS NULL` 且
    `describe_failed_at IS NULL` 过滤，标死的行不会再被捞出来。

    模型侧失败（``source=SOURCE_MODEL``）**永远** ``terminal=False``，
    哪怕它的 ``retriable`` 是 False：它表达的是"这一轮跳过，等配置修好后
    重来"，不是"这个引用永远不可用"。
    """

    def __init__(
        self,
        message: str,
        *,
        retriable: bool = True,
        terminal: bool = False,
        source: str = SOURCE_OTHER,
        model_kind: ModelErrorKind | None = None,
    ) -> None:
        super().__init__(message)
        self.retriable = retriable
        self.terminal = terminal
        self.source = source
        self.model_kind = model_kind

    @property
    def permanent_ref(self) -> bool:
        """终态的原因是**引用本身**不可用（而不是业务拒绝）。

        与 ``terminal`` 分开：撤权这类业务拒绝也该停掉重试（否则每 60 秒
        重试一条已经被禁止描述的记录，日志被淹、窗口永远占着队列），但
        它不说明"这个引用永远不可用"。
        """
        return self.terminal and self.source == SOURCE_REF

    @property
    def is_config_failure(self) -> bool:
        """是不是"整轮必然全失败"的模型/配置错误（401、密钥缺失、职责未绑定）。"""
        return self.source == SOURCE_MODEL and self.model_kind in CONFIG_ERROR_KINDS


class MediaRefPermanentError(MediaDescribeError):
    """**引用本身**不可用：越界、超限、形式不支持、内容为空。

    只有这一类该把证据标成终态。它是 ``MediaDescribeError`` 的子类，
    所以"捕获描述失败"的现有代码不必区分；要区分的地方看 ``permanent_ref``。
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            retriable=False,
            terminal=True,
            source=SOURCE_REF,
        )


__all__ = [
    "CONFIG_ERROR_KINDS",
    "SOURCE_BUSINESS",
    "SOURCE_MODEL",
    "SOURCE_OTHER",
    "SOURCE_REF",
    "MediaDescribeError",
    "MediaRefError",
    "MediaRefPermanentError",
]
