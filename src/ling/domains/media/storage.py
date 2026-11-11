"""生成媒体的**磁盘布局与原子写入**（TTS 等模型产出的二进制）。

## 与入站媒体的关键区别

入站媒体（用户发来的图片/视频）**从不落本地盘**：`media_evidence.media_ref`
存的是**远程 URL 字符串**，用时才由连接器去下载。所以这个项目此前没有任何
"本地媒体目录"的概念。

生成的媒体不一样：它是我们自己的产物，没有上游 URL 可取，必须存在本地。
于是这里第一次引入一个受管的磁盘布局，并且必须比"临时文件"严格得多——
它要能被 Web 播放、被渠道取走、能过期清理，还要能防越权读取。

## 布局：按内容 hash 分片

```
<data_dir>/generated_media/
  ab/
    abcdef0123...                    # 文件名就是 sha256 十六进制（64 字符）
  cd/
    cdef2345...
```

**文件名 = 内容的 sha256**，这一点决定了三件事：

1. **去重免费**：同一句 TTS 文本合成两次，落同一路径，不占第二份空间。
2. **校验免费**：读出时重算 hash 就能确认"这个字节串就是当初存的那份"，
   不需要额外的完整性列。
3. **路径不可猜**：外部拿不到别的用户的文件名，也就无法通过遍历目录探测。

分片（前 2 个十六进制字符）是为了避免单目录堆到几十万文件——ext4/xfs 在
单目录几十万条目后 `lookup` 会明显变慢，而清理任务要按目录扫。

**注意：hash 名字不等于访问控制。** 知道 hash 的人就能算出路径，所以对外
一律走 `exports.py` 的授权检查或签名 URL，绝不把磁盘路径暴露给调用方。

## 原子写入

`put()` 先写同目录的临时文件，`fsync` 后 `os.replace` 到最终名。
`os.replace` 在同一文件系统内是原子操作，所以任何时刻其他进程看到的都是
「完整文件」或「不存在」，不存在「写了一半」。

顺序很重要：**先落盘、后写库**（见 `assets.py`）。这样数据库里出现记录时，
文件一定已经完整；反过来会出现"有记录、没文件"的悬空引用，那是每次读取
都要处理的错误路径。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: 生成媒体在 `data_dir` 下的子目录名。
MEDIA_ROOT = "generated_media"

#: sha256 十六进制的严格形态。用它校验"外部传进来的 digest"：
#: 只接受这 64 个字符，斜杠、点、反斜杠都不可能进到路径里。
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")

#: 分片目录取前几位。2 位 = 256 个桶，单桶十万文件量级才需要再加一层。
_SHARD_LEN = 2


class MediaStorageError(Exception):
    """存储层的可预期失败（不可写、内容不合法、引用不存在）。"""


@dataclass(frozen=True)
class StoredFile:
    """一次落盘的结果。`digest` 同时是文件名。"""

    digest: str
    size_bytes: int
    #: 相对 `data_dir` 的路径（形如 `generated_media/ab/abcdef…`）。
    #: **这是要进数据库的形态**：绝对路径会在换机器/换挂载点后失效。
    relative_path: str
    #: 本次是否真的写了新文件（False = 内容已存在，去重命中）。
    created: bool


def _validate_digest(digest: str) -> str:
    """校验并归一化 digest。**任何**不匹配都拒绝，不做"尽力修正"。

    这是路径穿越的唯一闸门：`digest` 来自数据库、配置或调用方，只要它可能
    含 `..`、`/` 或绝对路径前缀，拼进 `Path` 之后就能逃出媒体根目录。
    """
    if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        raise MediaStorageError(f"非法的媒体摘要（只接受 64 位小写十六进制）：{digest!r}")
    return digest


class GeneratedMediaStorage:
    """生成媒体的本地存储。无状态，可随时构造（路径来自配置）。"""

    def __init__(self, data_dir: Path | str) -> None:
        self._root = Path(data_dir) / MEDIA_ROOT

    @property
    def root(self) -> Path:
        return self._root

    # -------------------------------------------------------------- 布局

    def path_for(self, digest: str) -> Path:
        """digest → 绝对路径。**不检查存在性**（存在性由 `exists()` 说）。"""
        safe = _validate_digest(digest)
        return self._root / safe[:_SHARD_LEN] / safe

    def relative_path_for(self, digest: str) -> str:
        """digest → 入库用的相对路径（POSIX 分隔符，跨平台一致）。"""
        safe = _validate_digest(digest)
        return f"{MEDIA_ROOT}/{safe[:_SHARD_LEN]}/{safe}"

    def absolute_path_of(self, relative_path: str) -> Path:
        """库里的相对路径 → 绝对路径，并**确认它没有逃出媒体根目录**。

        为什么还要检查：相对路径虽然由我们自己写入，但它终究是数据库里的一
        个字符串。备份恢复、手工改库、未来某次迁移都可能让它变成
        `../../etc/passwd`。用 `resolve()` 后再比较前缀是唯一可靠的判据——
        字符串前缀比较会被 `generated_media/../..` 这类写法绕过。
        """
        candidate = (Path(self._root).parent / relative_path).resolve()
        root = self._root.resolve()
        if candidate != root and root not in candidate.parents:
            raise MediaStorageError(f"媒体路径越界（拒绝读取）：{relative_path!r}")
        return candidate

    def exists(self, digest: str) -> bool:
        try:
            return self.path_for(digest).is_file()
        except MediaStorageError:
            return False

    # -------------------------------------------------------------- 写入

    def put(self, data: bytes, *, expected_digest: str | None = None) -> StoredFile:
        """原子写入并返回内容摘要。

        ``expected_digest``：调用方已经算过摘要时传进来，省一次遍历，同时
        用来**校验**"你算的和实际写的是不是同一份"（不一致直接报错，避免
        库里记一个对不上内容的 hash）。
        """
        if not data:
            raise MediaStorageError("拒绝写入空内容：空文件不是有效产物")

        digest = hashlib.sha256(data).hexdigest()
        if expected_digest is not None and _validate_digest(expected_digest) != digest:
            raise MediaStorageError("调用方给的摘要与实际内容不一致")

        target = self.path_for(digest)
        if target.is_file():
            # 去重命中：内容一样的文件已经在了，不重写（重写反而制造竞态窗口）。
            return StoredFile(
                digest=digest,
                size_bytes=target.stat().st_size,
                relative_path=self.relative_path_for(digest),
                created=False,
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件必须与目标**同目录**：`os.replace` 只在同一文件系统内原子，
        # 跨设备会退化成 copy+unlink，那就又有"写了一半"的窗口了。
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                # fsync 之后再 rename：否则机器掉电时可能留下一个长度对、
                # 内容全是零的文件，而目录项已经指向它了。
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            # 任何失败（含取消）都要清掉临时文件，否则磁盘上会积累 .part 垃圾。
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

        return StoredFile(
            digest=digest,
            size_bytes=len(data),
            relative_path=self.relative_path_for(digest),
            created=True,
        )

    def put_stream(
        self, chunks: Iterable[bytes], *, expected_digest: str | None = None
    ) -> StoredFile:
        """把可迭代的字节块写成**一个**文件，返回摘要。

        断流（迭代中途抛异常）时**不留下任何最终文件**——只有临时的 `.part`
        被删掉。这是"断流时无 ready 文件"的实现位置：调用方看到异常，就不会
        去写数据库记录，磁盘上也确实什么都没有。
        """
        payload = b"".join(bytes(chunk) for chunk in chunks)
        return self.put(payload, expected_digest=expected_digest)

    # -------------------------------------------------------------- 读取

    def read(self, digest: str) -> bytes:
        """按摘要读取。**调用方必须先完成授权**（见 `exports.py`）。

        这里只做两件事：拒绝非法摘要、确认文件存在。授权不是存储层的职责——
        存储层不知道"谁在问"，让它去查权限会把两个不变量缠在一起。
        """
        path = self.path_for(digest)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise MediaStorageError(f"媒体文件不存在：{digest}") from exc

    def verify(self, digest: str) -> bool:
        """重算磁盘内容的 hash，确认与文件名一致（完整性自检）。"""
        try:
            data = self.read(digest)
        except MediaStorageError:
            return False
        return hashlib.sha256(data).hexdigest() == digest

    # -------------------------------------------------------------- 删除

    def delete(self, digest: str) -> bool:
        """删除单个文件。不存在时返回 False（不抛错，便于清理任务幂等）。"""
        try:
            path = self.path_for(digest)
        except MediaStorageError:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    def remove_orphan_temps(self, *, older_than_seconds: float = 3600.0) -> int:
        """清掉崩溃遗留的 `.part` 临时文件，返回清理个数。

        进程在 `mkstemp` 与 `os.replace` 之间被杀时会留下这些文件。它们不
        影响正确性（永远不会被当成产物），但会慢慢吃满磁盘。设一个年龄门槛，
        避免删掉别的进程**正在写**的临时文件。
        """
        if not self._root.is_dir():
            return 0
        cutoff = time.time() - older_than_seconds
        removed = 0
        for part in self._root.glob("*/.tmp-*.part"):
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
